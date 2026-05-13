import collections
import copy
import hashlib
import json
import logging
import os
import argparse
import re
import signal
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import json_repair
from pipeline_runner import (
    EXIT_ARGUMENT_ERROR,
    EXIT_INPUT_MISSING,
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    ensure_parent,
    print_ok,
    require_file,
)
from prd_contract import (
    PROMPT_VERSION,
    TOOL_VERSION,
    add_item_contract,
    estimate_tokens,
    extract_preserved_tokens,
    risk_flags,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 로거 및 설정
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s | %(message)s")
log = logging.getLogger("SceneTranslator")

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"

b_stop_requested = False

FATAL_LLM_ERROR_KEYWORDS = (
    "iam_permission_denied",
    "permission 'aiplatform.endpoints.predict'",
    "permission denied",
    "defaultcredentialserror",
    "google_application_credentials",
)


def is_fatal_llm_error(exc) -> bool:
    err = str(exc).lower()
    return any(keyword in err for keyword in FATAL_LLM_ERROR_KEYWORDS)


def is_max_tokens_error(exc) -> bool:
    return "max_tokens" in str(exc).lower()


def signal_handler(sig, frame):
    global b_stop_requested
    log.info("중단 신호 수신. 현재 작업을 마무리하고 저장합니다...")
    b_stop_requested = True

signal.signal(signal.SIGINT, signal_handler)


def apply_translation_memory_hit(item: dict, text: str, rag, mod_stem: str, subrecord_path: str) -> bool:
    """완전 일치 TM이 있으면 LLM 큐에 넣지 않고 즉시 번역을 채운다."""
    if not rag or not hasattr(rag, "find_exact"):
        return False
    hit = rag.find_exact(text)
    if not hit:
        return False

    add_item_contract(
        item,
        plugin_name=mod_stem,
        record_type=item.get("record_type", "INFO"),
        subrecord_path=item.get("subrecord_path", subrecord_path),
    )
    item["Translate"] = hit
    item["translation"] = hit
    item["model"] = "translation_memory"
    item.setdefault("chunk_id", "tm_exact")
    item["risk_flags"] = risk_flags(str(text or ""), str(hit or ""), item)
    item.setdefault("glossary_hits", [])
    item["translation_class"] = "TM_EXACT"
    return True

# DB 및 RAG 로드 (프로젝트 내 db_manager.py 필요)
try:
    from db_manager import DBRAG, load_glossary_db
except ImportError:
    log.error("db_manager.py를 찾을 수 없습니다. 관련 기능을 비활성화합니다.")
    class DBRAG:
        def get_reference_string(self, *args): return None
    def load_glossary_db(): return {}

# =============================================================================
# 유틸리티
# =============================================================================
def load_config(config_path: str | Path | None = None):
    target = Path(config_path).expanduser().resolve() if config_path else CONFIG_FILE
    if target.exists():
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def generate_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]

def is_only_tags_and_punct(text: str) -> bool:
    """태그와 문장 부호만 있는 경우 제외 (예: <Alias=Name>.)"""
    if not text: return True
    # 1. 모든 <...> 형태의 태그 제거
    stripped = re.sub(r"<[^>]+>", "", text)
    # 2. 공백 및 일반적인 문장 부호 제거
    stripped = re.sub(r'[\s.,!?;:"\'-_\[\]()]+', "", stripped)
    # 3. 남은 문자가 없으면 태그/부호만 있는 것으로 간주
    return len(stripped) == 0


def assess_translation_risks(id_map: dict, translations: dict) -> list[dict]:
    risks = []
    for bid, item in id_map.items():
        src = str(item.get("Text", "") or "")
        dst = str(translations.get(bid, "") or "").strip()
        for flag in risk_flags(src, dst, item):
            risks.append({"batch_id": bid, "stable_id": item.get("stable_id"), **flag})
    return risks


def build_adaptive_chunks(items: list[dict], max_items: int, max_chars: int, max_tokens: int) -> list[list[dict]]:
    chunks = []
    current = []
    current_chars = 0
    current_tokens = 0
    for item in items:
        text = str(item.get("Text", "") or "")
        text_len = len(text)
        token_count = estimate_tokens(text) * 2
        would_exceed_items = len(current) >= max_items
        would_exceed_chars = current and current_chars + text_len > max_chars
        would_exceed_tokens = current and current_tokens + token_count > max_tokens
        if would_exceed_items or would_exceed_chars or would_exceed_tokens:
            chunks.append(current)
            current = []
            current_chars = 0
            current_tokens = 0
        current.append(item)
        current_chars += text_len
        current_tokens += token_count
    if current:
        chunks.append(current)
    return chunks


def build_chunk_log_entry(chunk_id: str, chunk: list[dict], model: str, retry_count: int = 0, split_reason: str = "") -> dict:
    source_chars = sum(len(str(item.get("Text", "") or "")) for item in chunk)
    input_tokens = sum(estimate_tokens(str(item.get("Text", "") or "")) for item in chunk)
    return {
        "chunk_id": chunk_id,
        "line_count": len(chunk),
        "source_chars": source_chars,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": input_tokens,
        "model": model,
        "retry_count": retry_count,
        "split_reason": split_reason,
    }

# =============================================================================
# 말투 프로파일링
# =============================================================================
def profile_scene(quest_data: dict, backend, use_ja_ref: bool = False, mod_stem: str = None, rag=None) -> dict:
    speaker_lines = collections.defaultdict(list)
    speaker_counts = collections.defaultdict(int)

    def _extract_lines(dialogues):
        for item in dialogues:
            spk = item.get("Speaker", "Unknown")
            txt = item.get("Text", "").strip()
            sid = item.get("StringID", "0")
            if txt:
                speaker_counts[spk] += 1
                if len(txt) <= 200 and len(speaker_lines[spk]) < 20:
                    ref = ""
                    if use_ja_ref and rag and str(sid).isdigit():
                        ja = rag.get_reference_string(mod_stem, int(sid), "ja")
                        if ja: ref = f" (Ref: {ja})"
                    speaker_lines[spk].append(f"{txt}{ref}")

    for scene in quest_data.get("Scenes", []):
        for dial in scene.get("Dials", []):
            _extract_lines(dial.get("Dialogues", []))
    for dial in quest_data.get("StandaloneDials", []):
        _extract_lines(dial.get("Dialogues", []))

    if not speaker_counts:
        return {}

    dialogue_blocks = [
        f"--- [{spk}] 샘플 ---\n" + "\n".join([f"- {l}" for l in speaker_lines[spk]])
        for spk in speaker_counts
    ]
    
    user_prompt = f"다음은 게임 내 등장하는 **모든** 인물들의 대사 발췌본입니다. 대사가 적은 인물도 포함되어 있습니다. 이를 읽고 전반적인 분위기와 각 캐릭터의 말투를 파악해서 '번역 말투 가이드라인'을 JSON 포맷으로 작성해.\n" + "\n".join(dialogue_blocks)
    
    instructions = """
[중요 지시사항]
1. 어떤 부가적인 설명 없이 오직 순수한 JSON 객체(단일 딕셔너리) 형태여야 해.
2. `scene_mood`: 이 퀘스트/씬 전체의 분위기를 **구체적이고 생생하게** 한 문장으로 묘사해 (예: "미스터리한 재난과 생존, 그리고 진실을 파헤치는 긴박한 조사").
3. `character_guidelines`: 각 캐릭터 설명은 반드시 **2문장 구조**로 작성해.
   - 1문장: 캐릭터의 성격·역할·태도를 간결하게 묘사 (예: "권위 있고 정중한 관리자.")
   - 2문장: 말투를 명확히 지정 — 반드시 아래 4가지 중 하나로만 끝낼 것:
     * "...반말만 사용함." / "...반말을 사용함."
     * "...해요체를 사용함."
     * "...하오체를 사용함." / "...하오체를 유지함."
     * "...하십시오체를 사용함." / "...하십시오체를 유지함."
4. ★★가장 중요한 규칙: 각 캐릭터는 퀘스트 내내 반드시 '반말' 혹은 '존댓말(해요체/하오체/하십시오체)' 하나로 완벽하게 말투를 통일해야 해. 상황에 따라 말투를 바꾸거나 섞어 쓰는 설정은 이 번역 시스템상 **엄격하게 금지**야!★★
5. 🌟 제공된 **모든 인물(대사가 단 한 줄뿐인 단역이나 과묵한 캐릭터 포함)**에 대해 단 한 명도 누락하지 말고 전부 가이드라인을 작성해! 단, 제공되지 않은 캐릭터를 지어내지는 마.
6. 데이터의 `StandaloneDials` 배열에 있는 대사들은 NPC 대화에 플레이어가 상호작용 가능한 선택지입니다. 태그([Attack], [Persuade] 등)를 상황과 매칭해서 화자의 말투를 정확히 파악해.

출력 예시:
{
  "scene_mood": "미스터리한 재난과 생존, 그리고 진실을 파헤치는 긴박한 조사",
  "character_guidelines": {
    "Player": "자신감 있고 직설적인 태도의 주인공. 상대방에 관계없이 짧고 퉁명스러운 반말만 사용함.",
    "Administrator Kirk": "권위 있고 정중한 관리자. 다소의 인간적인 면모를 보이지만, 시종일관 하오체를 유지함."
  }
}
"""

    try:
        raw = backend.generate_content(instructions + "\n\n" + user_prompt)
        return json_repair.repair_json(raw, return_objects=True) or {}
    except Exception as e:
        log.error(f"프로파일링 실패: {e}")
        return {}

# =============================================================================
# 번역 핵심 로직
# =============================================================================
def build_scene_prompt(chunk_items, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles):
    dialogue_blocks = []
    id_map = {}
    active_speakers = {
        str(item.get("Speaker", "Unknown"))
        for item in list(chunk_items) + list(context_lines)
        if item.get("Speaker")
    }
    scoped_glossary_text = filter_glossary_for_prompt(glossary_text, chunk_items, context_lines)

    profile_text = ""
    if scene_profile:
        profile_text = f"\n[말투 가이드]\n- 분위기: {scene_profile.get('scene_mood', '')}\n"
    char_guides = scene_profile.get("character_guidelines", {})
    if isinstance(char_guides, dict):
        for spk, guide in char_guides.items():
            if spk not in active_speakers:
                continue
            # [음성 분석 결과 우선 적용]
            tone_guide = compact_tone_profile(tone_profiles, spk)
            final_guide = f"[음성 분석 가이드] {tone_guide}" if tone_guide else guide
            profile_text += f"- {spk}: {final_guide}\n"

    dialogue_blocks.append("--- [이전 문맥] ---")
    for ctx in context_lines:
        source = ctx.get("_context_source") or "최근 문맥"
        dialogue_blocks.append(f"[{source} / {ctx.get('Speaker')}] {ctx.get('Text')}")

    dialogue_blocks.append("\n--- [번역 대상] ---")
    for i, item in enumerate(chunk_items):
        bid = f"B{i}"
        spk = item.get("Speaker", "Unknown")
        txt = item.get("Text", "")
        sid = item.get("StringID", "0")
        id_map[bid] = item

        ref_parts = []
        if rag and str(sid).isdigit():
            ja = rag.get_reference_string(mod_stem, int(sid), "ja")
            if ja: ref_parts.append(f"일본어: {ja}")

        ref_str = f" ({', '.join(ref_parts)})" if ref_parts else ""
        dialogue_blocks.append(f"[{spk}] BatchID:{bid} | 원문: {txt}{ref_str}")

    instructions = f"""
{profile_text}
[치명적 지시]
1. NPC는 지정된 말투를 끝까지 고수할 것. 
2. 단, 'Player'는 상대와 상황에 따라 말투를 유연하게 바꿀 것.
3. 용어집 준수: {scoped_glossary_text if scoped_glossary_text else "(이번 청크에 직접 등장하는 용어집 항목 없음)"}
4. 무조건 아래 JSON 형식으로만 응답할 것. 추가적인 부연 설명은 절대 금지. (BatchID 키는 B0, B1 등 원문에서 주어진 식별자를 그대로 사용하세요)
{{
  "B0": "번역문 0",
  "B1": "번역문 1"
}}
"""
    return instructions + "\n\n" + "\n".join(dialogue_blocks), id_map


def compact_tone_profile(tone_profiles: dict, speaker: str) -> str:
    if not tone_profiles:
        return ""
    profiles = tone_profiles.get("speakers", tone_profiles) if isinstance(tone_profiles, dict) else {}
    profile = profiles.get(speaker) if isinstance(profiles, dict) else None
    if not profile:
        return ""
    if isinstance(profile, str):
        return profile[:600]
    if not isinstance(profile, dict):
        return str(profile)[:600]

    keep = {}
    for key in ("tone", "gender", "age_group", "speech_style", "honorific_level", "confidence"):
        if key in profile:
            keep[key] = profile[key]

    acoustic = profile.get("acoustic_features")
    if isinstance(acoustic, dict):
        acoustic_keep = {}
        for key in (
            "sample_count",
            "f0_mean_hz_mean",
            "f0_std_hz_mean",
            "rms_mean",
            "spectral_centroid_hz_mean",
            "onset_rate_per_sec_mean",
            "approx_tokens_per_sec_mean",
        ):
            if key in acoustic:
                acoustic_keep[key] = acoustic[key]
        if acoustic_keep:
            keep["acoustic_features"] = acoustic_keep

    if not keep:
        return ""
    return json.dumps(keep, ensure_ascii=False, separators=(",", ":"))

def translate_scene_recursive(chunk_items, backend, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles, depth=0, orchestration_backend=None, risk_report=None):
    if not chunk_items or depth > 5 or b_stop_requested:
        return

    prompt, id_map = build_scene_prompt(chunk_items, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles)

    try:
        from orchestrator import TranslationOrchestrator

        char_profile_info = ""
        context_info = ""
        scoped_glossary_text = filter_glossary_for_prompt(glossary_text, chunk_items, context_lines)
        if isinstance(backend, TranslationOrchestrator) or orchestration_backend:
            active_speakers = {
                str(item.get("Speaker", "Unknown"))
                for item in list(chunk_items) + list(context_lines)
                if item.get("Speaker")
            }
            char_profile_info = f"씬 분위기: {scene_profile.get('scene_mood', '')}\n"
            for spk, guide in scene_profile.get("character_guidelines", {}).items():
                if spk not in active_speakers:
                    continue
                char_profile_info += f"- {spk}: {guide}\n"
            for ctx in context_lines:
                context_info += f"[{ctx.get('Speaker')}] {ctx.get('Text')}\n"

        def generate_with(selected_backend):
            if isinstance(selected_backend, TranslationOrchestrator):
                return selected_backend.translate_with_review(
                    prompt,
                    context_info=context_info,
                    char_profile=char_profile_info,
                    glossary_text=scoped_glossary_text,
                )
            return selected_backend.generate_content(prompt)

        raw = generate_with(backend)
        
        res = json_repair.repair_json(raw, return_objects=True)

        if isinstance(res, dict):
            # 모든 요청 ID가 포함되었는지 확인
            missing_ids = [bid for bid in id_map if bid not in res or not str(res[bid]).strip()]
            if missing_ids:
                if orchestration_backend:
                    raise ValueError(f"Risky translation: missing ids {missing_ids}")
                elif len(chunk_items) > 1:
                    raise ValueError(f"Partial translation detected ({len(missing_ids)}/{len(id_map)} items missing).")
                else:
                    log.error(f"  [Error] 단일 항목 번역 실패: {chunk_items[0].get('Text')[:30]}...")

            risks = assess_translation_risks(id_map, res)
            orchestration_risks = [r for r in risks if r.get("severity") in {"fatal", "quality"}]
            if orchestration_risks and orchestration_backend:
                risk_codes = [r.get("code", "") for r in orchestration_risks[:5]]
                log.info(f"  -> 위험 청크 감지 ({', '.join(risk_codes)}). 오케스트레이션 재번역을 시도합니다.")
                if risk_report is not None:
                    risk_report.append({"depth": depth, "size": len(chunk_items), "risks": risks})
                raw = generate_with(orchestration_backend)
                res = json_repair.repair_json(raw, return_objects=True)
                if not isinstance(res, dict):
                    raise ValueError("Invalid orchestration JSON response (not a dict)")
                missing_ids = [bid for bid in id_map if bid not in res or not str(res[bid]).strip()]
                if missing_ids and len(chunk_items) > 1:
                    raise ValueError(f"Partial orchestration translation detected ({len(missing_ids)}/{len(id_map)} items missing).")
            
            for bid, trans in res.items():
                if bid in id_map:
                    id_map[bid]["Translate"] = trans
                    id_map[bid]["translation"] = trans
                    id_map[bid]["model"] = getattr(backend, "model_name", backend.__class__.__name__)
                    id_map[bid].setdefault("chunk_id", "")
                    id_map[bid]["risk_flags"] = risk_flags(str(id_map[bid].get("Text", "") or ""), str(trans or ""), id_map[bid])
                    id_map[bid].setdefault("glossary_hits", [])
                    id_map[bid].setdefault("translation_class", "TRANSLATE")
        else:
            raise ValueError("Invalid JSON response (not a dict)")

    except Exception as e:
        if is_fatal_llm_error(e):
            log.error(f"치명적 LLM 인증/권한 오류로 번역을 중단합니다: {e}")
            raise
        log.warning(f"Error at depth {depth}: {e}. Splitting chunk...")
        if orchestration_backend and not is_max_tokens_error(e):
            try:
                log.info("  -> fast 번역 실패. 청크 분할 전에 오케스트레이션으로 복구를 시도합니다.")
                translate_scene_recursive(chunk_items, orchestration_backend, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles, depth, None, risk_report)
                return
            except Exception as orch_e:
                if is_fatal_llm_error(orch_e):
                    raise
                log.warning(f"오케스트레이션 복구 실패: {orch_e}")
        if len(chunk_items) > 1:
            mid = len(chunk_items) // 2
            translate_scene_recursive(chunk_items[:mid], backend, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles, depth + 1, orchestration_backend, risk_report)
            translate_scene_recursive(chunk_items[mid:], backend, mod_stem, chunk_items[:mid], scene_profile, glossary_text, rag, tone_profiles, depth + 1, orchestration_backend, risk_report)


def build_quest_translation_items(quest: dict, rag, mod_stem: str) -> list[dict]:
    dial_flat_list = []

    for s in quest.get("Scenes", []):
        for d in s.get("Dials", []):
            for dial in d.get("Dialogues", []):
                txt = dial.get("Text")
                field_type = dial.get("FieldType") or dial.get("subrecord_path") or "NAM1"
                if txt:
                    if dial.get("Translate"):
                        pass
                    elif is_only_tags_and_punct(txt):
                        dial["Translate"] = txt
                    elif apply_translation_memory_hit(dial, txt, rag, mod_stem, field_type):
                        pass
                    else:
                        add_item_contract(dial, plugin_name=mod_stem, record_type=dial.get("record_type", "INFO"), subrecord_path=dial.get("subrecord_path", field_type))
                        dial_flat_list.append(dial)
                for choice in dial.get("PlayerChoices", []):
                    ctxt = choice.get("Text")
                    if ctxt:
                        if choice.get("Translate"):
                            pass
                        elif is_only_tags_and_punct(ctxt):
                            choice["Translate"] = ctxt
                        elif apply_translation_memory_hit(choice, ctxt, rag, mod_stem, "CHOICE"):
                            pass
                        else:
                            add_item_contract(choice, plugin_name=mod_stem, record_type=choice.get("record_type", "INFO"), subrecord_path=choice.get("subrecord_path", "CHOICE"))
                            dial_flat_list.append(choice)

    for dial in quest.get("StandaloneDials", []):
        for d in dial.get("Dialogues", []):
            txt = d.get("Text")
            field_type = d.get("FieldType") or d.get("subrecord_path") or "NAM1"
            if txt:
                if d.get("Translate"):
                    pass
                elif is_only_tags_and_punct(txt):
                    d["Translate"] = txt
                elif apply_translation_memory_hit(d, txt, rag, mod_stem, field_type):
                    pass
                else:
                    add_item_contract(d, plugin_name=mod_stem, record_type=d.get("record_type", "INFO"), subrecord_path=d.get("subrecord_path", field_type))
                    dial_flat_list.append(d)
            for choice in d.get("PlayerChoices", []):
                ctxt = choice.get("Text")
                if ctxt:
                    if choice.get("Translate"):
                        pass
                    elif is_only_tags_and_punct(ctxt):
                        choice["Translate"] = ctxt
                    elif apply_translation_memory_hit(choice, ctxt, rag, mod_stem, "CHOICE"):
                        pass
                    else:
                        add_item_contract(choice, plugin_name=mod_stem, record_type=choice.get("record_type", "INFO"), subrecord_path=choice.get("subrecord_path", "CHOICE"))
                        dial_flat_list.append(choice)

    return dial_flat_list


def process_quest_translation(
    quest: dict,
    *,
    config: dict,
    mod_stem: str,
    profile_backend,
    backend,
    orchestration_backend,
    glossary_text: str,
    tone_profiles: dict,
    cached_profile: dict | None,
    use_ja_ref: bool,
    profile_only: bool,
) -> dict:
    if b_stop_requested:
        return {"quest_id": quest.get("QuestID", "Unknown"), "quest": quest, "stopped": True, "profile": cached_profile or {}, "risk_report": [], "chunk_log": [], "chunk_count": 0}

    rag = DBRAG()
    q_id = quest.get("QuestID", "Unknown")
    log.info(f"퀘스트 처리 중: {q_id}")

    q_profile = cached_profile
    if q_profile is None:
        q_profile = profile_scene(quest, profile_backend, use_ja_ref, mod_stem, rag)

    if profile_only:
        rag.close()
        return {"quest_id": q_id, "quest": quest, "profile": q_profile, "risk_report": [], "chunk_log": [], "chunk_count": 0}

    dial_flat_list = build_quest_translation_items(quest, rag, mod_stem)
    context_index = build_context_index(quest)
    chunk_size = int(config.get("step2_chunk_size", config.get("chunk_size", 40)))
    max_chunk_chars = int(config.get("step2_max_chunk_chars", 3500))
    max_input_tokens = int(config.get("step2_max_input_tokens", 6000))
    max_output_tokens = int(config.get("step2_max_output_tokens", 4000))
    chunks = build_adaptive_chunks(dial_flat_list, chunk_size, max_chunk_chars, max_input_tokens + max_output_tokens)
    recent_context = []
    risk_report = []
    chunk_log = []

    num_chunks = len(chunks)
    for i, chunk in enumerate(chunks):
        if b_stop_requested:
            break
        chunk_id = f"{q_id}_chunk_{i + 1:03d}"
        for item in chunk:
            item["chunk_id"] = chunk_id
        chunk_log.append(build_chunk_log_entry(chunk_id, chunk, str(config.get("model_name") or config.get("models", {}).get("translation", "fast_translation"))))
        log.info(f"  -> [{q_id} 청크 {i+1}/{num_chunks}] {len(chunk)}개 대사 번역 중...")
        prompt_context = build_pnam_context(chunk, recent_context, context_index)
        translate_scene_recursive(chunk, backend, mod_stem, prompt_context, q_profile, glossary_text, rag, tone_profiles, orchestration_backend=orchestration_backend, risk_report=risk_report)
        recent_context = chunk[-5:]

    rag.close()
    return {
        "quest_id": q_id,
        "quest": quest,
        "profile": q_profile,
        "risk_report": risk_report,
        "chunk_log": chunk_log,
        "chunk_count": len(chunks),
        "stopped": b_stop_requested,
    }


def _normalize_form_id(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return f"{int(text, 16):08X}"
    except ValueError:
        return text.upper()


def build_context_index(quest: dict) -> dict[str, dict]:
    index = {}

    def add_items(items):
        for item in items:
            form_id = _normalize_form_id(item.get("FormID") or item.get("form_id"))
            if form_id:
                index.setdefault(form_id, item)
            for choice in item.get("PlayerChoices", []):
                choice_id = _normalize_form_id(choice.get("FormID") or choice.get("form_id"))
                if choice_id:
                    index.setdefault(choice_id, choice)

    for scene in quest.get("Scenes", []):
        for dial_group in scene.get("Dials", []):
            for dial in dial_group.get("Dialogues", []):
                add_items([dial])
    for dial_group in quest.get("StandaloneDials", []):
        add_items(dial_group.get("Dialogues", []))
    return index


def build_pnam_context(chunk_items: list[dict], recent_context: list[dict], context_index: dict[str, dict], max_chain: int = 3) -> list[dict]:
    context = []
    seen = {id(item) for item in chunk_items}

    for item in chunk_items:
        current = item
        for _ in range(max_chain):
            prev_id = _normalize_form_id(current.get("InfoPNAM") or current.get("PNAM"))
            if not prev_id:
                break
            prev_item = context_index.get(prev_id)
            if not prev_item or id(prev_item) in seen:
                break
            context_item = dict(prev_item)
            context_item["_context_source"] = "PNAM 체인"
            context.append(context_item)
            seen.add(id(prev_item))
            current = prev_item

    if context:
        return context[-max_chain:] + recent_context[-3:]
    return recent_context


def filter_glossary_for_prompt(glossary_text: str, chunk_items: list[dict], context_lines: list[dict]) -> str:
    if not glossary_text:
        return ""
    haystack = "\n".join(
        str(item.get("Text", "") or "")
        for item in list(chunk_items) + list(context_lines)
    ).lower()
    selected = []
    for line in glossary_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        term = stripped[2:] if stripped.startswith("- ") else stripped
        term = term.split(":", 1)[0].strip().lower()
        if term and term in haystack:
            selected.append(stripped)
    return "\n".join(selected)

# =============================================================================
# 메인 실행부
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", dest="input_json", default=None, help="Standardized Step 1 JSON input path")
    parser.add_argument("--output-json", dest="output_json", default=None, help="Standardized translated JSON output path")
    parser.add_argument("--profile-json", dest="profile_json", default=None, help="Standardized scene profile JSON output path")
    parser.add_argument("--config", default=str(CONFIG_FILE), help="Config JSON path")
    parser.add_argument("-i", "--input", required=False, help="Extract JSON path (Step 1 output)")
    parser.add_argument("-o", "--output", required=False, help="Output translated JSON path")
    parser.add_argument("--use-ja-ref", action="store_true", help="Use Japanese references")
    parser.add_argument("--profile-only", action="store_true", help="Generate profile only")
    parser.add_argument("--tone-profile", dest="tone_profile", default=None, help="Path to audio tone profile JSON")
    parser.add_argument("--tone-profiles", default=None, help="Legacy alias for tone profile JSON path")
    args = parser.parse_args()
    args.input = args.input_json or args.input
    args.output = args.output_json or args.output
    if not args.input or not args.output:
        print("Error: --input-json and --output-json are required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR

    try:
        in_path = require_file(args.input, "input")
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_MISSING

    out_path = ensure_parent(args.output)
    profile_path = ensure_parent(args.profile_json or out_path.with_name(out_path.stem + "_profile.json"))
    tone_profile_arg = args.tone_profile or args.tone_profiles

    # The shared runner passes the config path explicitly so every entrypoint
    # resolves prompts and backend settings the same way.
    config = load_config(args.config)

    glossary_dict = load_glossary_db()
    glossary_text = "\n".join([f"- {k}: {v}" for k, v in glossary_dict.items()])
    
    # llm_backend.py 의 get_llm_backend 사용
    try:
        from llm_backend import get_llm_backend
        from orchestrator import TranslationOrchestrator
        
        orch_cfg = config.get("orchestrator", {})
        orch_mode = str(orch_cfg.get("mode", "always" if orch_cfg.get("enabled") else "off")).lower()
        orchestration_backend = None
        backend = get_llm_backend(config, "step2_prompt")
        profile_backend = get_llm_backend(config, "step2_prompt", role="audio_profile")

        if orch_cfg.get("enabled") and orch_mode in ("always", "risky_only"):
            log.info(f"멀티 모델 오케스트레이터 모드 활성화: {orch_mode}")
            gen_backends = []
            for m in orch_cfg.get("generation_models", []):
                # 각 모델별 페르소나를 시스템 인스트럭션에 결합
                m_config = config.copy()
                m_config["provider"] = m["provider"]
                m_config["api_provider"] = m["provider"]
                m_config["model_name"] = m["model"]
                persona_prompt = f"{config.get('step2_prompt', '')}\n\n[페르소나] {m['persona']}"
                # 임시 키로 step2_prompt를 덮어씌워서 get_llm_backend가 이를 사용하게 함
                m_config["_temp_prompt"] = persona_prompt
                gen_backends.append(get_llm_backend(m_config, "_temp_prompt"))
            
            review_cfg = orch_cfg.get("review_model", {})
            r_config = config.copy()
            r_config["provider"] = review_cfg["provider"]
            r_config["api_provider"] = review_cfg["provider"]
            r_config["model_name"] = review_cfg["model"]
            review_backend = get_llm_backend(r_config, "step2_prompt")
            
            review_skip_similarity = float(orch_cfg.get("review_skip_similarity", 0.85))
            orchestration_backend = TranslationOrchestrator(
                gen_backends,
                review_backend,
                glossary_text=glossary_text,
                work_dir=out_path.parent,
                review_skip_similarity=review_skip_similarity,
            )
            if orch_mode == "always":
                backend = orchestration_backend
    except Exception as e:
        log.error(f"백엔드 또는 오케스트레이터 초기화 실패: {e}")
        import traceback
        log.error(traceback.format_exc())
        import sys
        sys.exit(1)

    rag = DBRAG()

    in_path = Path(in_path)
    out_path = Path(out_path)
    mod_stem = in_path.stem.replace("_dump", "")
    progress_path = out_path.with_suffix(".progress.json")
    
    target_in = str(in_path)
    if progress_path.exists():
        log.info(f"임시 작업 파일 발견! 이어하기를 시도합니다: {progress_path.name}")
        target_in = str(progress_path)

    with open(target_in, "r", encoding="utf-8") as f:
        scenes_data = json.load(f)

    if isinstance(scenes_data, list):
        quests = scenes_data
    else:
        quests = scenes_data.get("Quests", [])
    
    # Quest profiles are reused across reruns so profile-only and full runs
    # can share the expensive analysis output.
    all_profiles = {}
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                all_profiles = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"프로필 캐시 로드 실패, 새로 생성합니다: {exc}")
            all_profiles = {}

    # Tone profiles come from the optional audio side pipeline and are safe to
    # omit; translation falls back to text-only context when absent.
    tone_profiles = {}
    if not tone_profile_arg:
        candidate_paths = [
            in_path.parent / f"{mod_stem}.audio.tone_profiles.json",
            in_path.parent / "tone_profiles.json",
        ]
        tone_profile_arg = next((str(candidate) for candidate in candidate_paths if candidate.exists()), None)
    tp_path = Path(tone_profile_arg) if tone_profile_arg else Path("")
    if tp_path.exists():
        log.info(f"음성 분석 프로필 로드 중: {tp_path}")
        with open(tp_path, "r", encoding="utf-8") as f:
            tone_profiles = json.load(f)

    save_interval = max(1, int(config.get("step2_save_interval", 5)))
    quest_workers = max(1, int(config.get("step2_quest_workers", 1 if args.profile_only else 4)))
    quest_workers = min(quest_workers, max(1, len(quests)))
    log.info(f"Quest 병렬 처리 workers={quest_workers}")

    all_risk_reports = []
    all_chunk_logs = []
    chunks_since_save = 0

    def persist_progress(force: bool = False):
        nonlocal chunks_since_save
        if args.profile_only:
            return
        if force or chunks_since_save >= save_interval:
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump(scenes_data, f, ensure_ascii=False, indent=2)
            chunks_since_save = 0

    with ThreadPoolExecutor(max_workers=quest_workers) as executor:
        future_map = {
            executor.submit(
                process_quest_translation,
                copy.deepcopy(quest),
                config=config,
                mod_stem=mod_stem,
                profile_backend=profile_backend,
                backend=backend,
                orchestration_backend=orchestration_backend,
                glossary_text=glossary_text,
                tone_profiles=tone_profiles,
                cached_profile=all_profiles.get(quest.get("QuestID", "Unknown")),
                use_ja_ref=args.use_ja_ref,
                profile_only=args.profile_only,
            ): quest_index
            for quest_index, quest in enumerate(quests)
        }

        for future in as_completed(future_map):
            quest_index = future_map[future]
            result = future.result()
            q_id = result.get("quest_id", "Unknown")
            quests[quest_index] = result.get("quest", quests[quest_index])
            all_profiles[q_id] = result.get("profile", {})
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(all_profiles, f, ensure_ascii=False, indent=2)

            all_risk_reports.extend(result.get("risk_report", []))
            all_chunk_logs.extend(result.get("chunk_log", []))
            chunks_since_save += result.get("chunk_count", 0)
            persist_progress(force=bool(result.get("stopped")))
            log.info(f"퀘스트 완료: {q_id}")
            if b_stop_requested:
                break

    persist_progress(force=True)

    if all_risk_reports:
        risk_path = out_path.with_suffix(".risk_report.json")
        with open(risk_path, "w", encoding="utf-8") as f:
            json.dump(all_risk_reports, f, ensure_ascii=False, indent=2)
    if all_chunk_logs:
        chunk_log_path = out_path.with_suffix(".chunk_log.json")
        with open(chunk_log_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "prompt_version": PROMPT_VERSION,
                    "tool_version": TOOL_VERSION,
                    "chunks": sorted(all_chunk_logs, key=lambda item: item.get("chunk_id", "")),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    if b_stop_requested:
        log.info(f"⚠️ 중지됨. 진행 상황 보존: {progress_path}")
        # 중지되었을 때는 out_path를 쓰지 않고 progress_path만 남깁니다.
    else:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(scenes_data, f, ensure_ascii=False, indent=2)
        if progress_path.exists():
            progress_path.unlink()
        
        log.info("모든 작업이 완료되었습니다.")
        
        # 성공적으로 완료된 경우 오케스트레이터의 임시 캐시 삭제
        from orchestrator import TranslationOrchestrator
        if isinstance(backend, TranslationOrchestrator):
            backend.cleanup()
        elif isinstance(orchestration_backend, TranslationOrchestrator):
            orchestration_backend.cleanup()

    # 1min.ai 누적 크레딧 출력
    from llm_backend import Min1AIBackend
    if Min1AIBackend.total_used_credit > 0:
        log.info(f"\n[결산] 이번 세션에서 사용된 총 1min.ai 크레딧: {Min1AIBackend.total_used_credit}")
    print_ok(out_path if not args.profile_only else profile_path)
    return EXIT_SUCCESS

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
