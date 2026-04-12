import collections
import hashlib
import json
import logging
import os
import argparse
import signal
from pathlib import Path

import json_repair

# 로거 및 설정
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s | %(message)s")
log = logging.getLogger("SceneTranslator")

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"

b_stop_requested = False

def signal_handler(sig, frame):
    global b_stop_requested
    log.info("중단 신호 수신. 현재 작업을 마무리하고 저장합니다...")
    b_stop_requested = True

signal.signal(signal.SIGINT, signal_handler)

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
def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def generate_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]

def is_only_tags_and_punct(text: str) -> bool:
    """태그와 문장 부호만 있는 경우 제외 (예: <Alias=Name>.)"""
    if not text: return True
    import re
    # 1. 모든 <...> 형태의 태그 제거
    stripped = re.sub(r"<[^>]+>", "", text)
    # 2. 공백 및 일반적인 문장 부호 제거
    stripped = re.sub(r'[\s.,!?;:"\'-_\[\]()]+', "", stripped)
    # 3. 남은 문자가 없으면 태그/부호만 있는 것으로 간주
    return len(stripped) == 0

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

    profile_text = ""
    if scene_profile:
        profile_text = f"\n[말투 가이드]\n- 분위기: {scene_profile.get('scene_mood', '')}\n"
    char_guides = scene_profile.get("character_guidelines", {})
    if isinstance(char_guides, dict):
        for spk, guide in char_guides.items():
            # [음성 분석 결과 우선 적용]
            tone_guide = tone_profiles.get(spk)
            final_guide = f"[음성 분석 가이드] {tone_guide}" if tone_guide else guide
            profile_text += f"- {spk}: {final_guide}\n"

    dialogue_blocks.append("--- [이전 문맥] ---")
    for ctx in context_lines:
        dialogue_blocks.append(f"[{ctx.get('Speaker')}] {ctx.get('Text')}")

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
3. 용어집 준수: {glossary_text}
4. 결과는 오직 JSON: {{ "BatchID": "번역문" }}
"""
    return instructions + "\n\n" + "\n".join(dialogue_blocks), id_map

def translate_scene_recursive(chunk_items, backend, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles, depth=0):
    if not chunk_items or depth > 5 or b_stop_requested:
        return

    prompt, id_map = build_scene_prompt(chunk_items, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles)

    try:
        from orchestrator import TranslationOrchestrator
        if isinstance(backend, TranslationOrchestrator):
            # 오케스트레이터인 경우 문맥 정보를 명시적으로 분리하여 전달
            char_profile_info = f"씬 분위기: {scene_profile.get('scene_mood', '')}\n"
            for spk, guide in scene_profile.get("character_guidelines", {}).items():
                char_profile_info += f"- {spk}: {guide}\n"
                
            context_info = ""
            for ctx in context_lines:
                context_info += f"[{ctx.get('Speaker')}] {ctx.get('Text')}\n"
                
            raw = backend.translate_with_review(prompt, context_info=context_info, char_profile=char_profile_info)
        else:
            raw = backend.generate_content(prompt)
        
        res = json_repair.repair_json(raw, return_objects=True)

        if isinstance(res, dict):
            # 모든 요청 ID가 포함되었는지 확인
            missing_ids = [bid for bid in id_map if bid not in res or not str(res[bid]).strip()]
            if missing_ids:
                if len(chunk_items) > 1:
                    raise ValueError(f"Partial translation detected ({len(missing_ids)}/{len(id_map)} items missing).")
                else:
                    log.error(f"  [Error] 단일 항목 번역 실패: {chunk_items[0].get('Text')[:30]}...")
            
            for bid, trans in res.items():
                if bid in id_map:
                    id_map[bid]["Translate"] = trans
        else:
            raise ValueError("Invalid JSON response (not a dict)")

    except Exception as e:
        log.warning(f"Error at depth {depth}: {e}. Splitting chunk...")
        if len(chunk_items) > 1:
            mid = len(chunk_items) // 2
            translate_scene_recursive(chunk_items[:mid], backend, mod_stem, context_lines, scene_profile, glossary_text, rag, tone_profiles, depth + 1)
            translate_scene_recursive(chunk_items[mid:], backend, mod_stem, chunk_items[:mid], scene_profile, glossary_text, rag, tone_profiles, depth + 1)

# =============================================================================
# 메인 실행부
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True, help="Extract JSON path (Step 1 output)")
    parser.add_argument("-o", "--output", required=True, help="Output translated JSON path")
    parser.add_argument("--use-ja-ref", action="store_true", help="Use Japanese references")
    parser.add_argument("--profile-only", action="store_true", help="Generate profile only")
    parser.add_argument("--tone-profiles", default="tone_profiles.json", help="Path to tone_profiles.json")
    args = parser.parse_args()

    config = load_config()
    
    # llm_backend.py 의 get_llm_backend 사용
    try:
        from llm_backend import get_llm_backend
        from orchestrator import TranslationOrchestrator
        
        orch_cfg = config.get("orchestrator", {})
        if orch_cfg.get("enabled"):
            log.info("멀티 모델 오케스트레이터 모드 활성화")
            gen_backends = []
            for m in orch_cfg.get("generation_models", []):
                # 각 모델별 페르소나를 시스템 인스트럭션에 결합
                m_config = config.copy()
                m_config["api_provider"] = m["provider"]
                m_config["model_name"] = m["model"]
                persona_prompt = f"{config.get('step2_prompt', '')}\n\n[페르소나] {m['persona']}"
                # 임시 키로 step2_prompt를 덮어씌워서 get_llm_backend가 이를 사용하게 함
                m_config["_temp_prompt"] = persona_prompt
                gen_backends.append(get_llm_backend(m_config, "_temp_prompt"))
            
            review_cfg = orch_cfg.get("review_model", {})
            r_config = config.copy()
            r_config["api_provider"] = review_cfg["provider"]
            r_config["model_name"] = review_cfg["model"]
            review_backend = get_llm_backend(r_config, "step2_prompt")
            
            backend = TranslationOrchestrator(gen_backends, review_backend, glossary_text=glossary_text) 
        else:
            backend = get_llm_backend(config, "step2_prompt")
    except Exception as e:
        log.error(f"백엔드 또는 오케스트레이터 초기화 실패: {e}")
        import traceback
        log.error(traceback.format_exc())
        return

    rag = DBRAG()
    glossary_dict = load_glossary_db()
    glossary_text = "\n".join([f"- {k}: {v}" for k, v in glossary_dict.items()])

    in_path = Path(args.input)
    out_path = Path(args.output)
    mod_stem = in_path.stem.replace("_dump", "")

    if not in_path.exists():
        log.error(f"입력 파일을 찾을 수 없습니다: {in_path}")
        return

    with open(in_path, "r", encoding="utf-8") as f:
        scenes_data = json.load(f)

    if isinstance(scenes_data, list):
        quests = scenes_data
    else:
        quests = scenes_data.get("Quests", [])
    
    # 프로파일 경로: 출력 파일명 기반
    profile_path = out_path.with_name(out_path.stem + "_profile.json")
    all_profiles = {}
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                all_profiles = json.load(f)
        except:
            all_profiles = {}

    # 오디션 분석 결과(tone_profiles) 로드
    tone_profiles = {}
    tp_path = Path(args.tone_profiles)
    if tp_path.exists():
        log.info(f"음성 분석 프로필 로드 중: {tp_path}")
        with open(tp_path, "r", encoding="utf-8") as f:
            tone_profiles = json.load(f)

    for quest in quests:
        if b_stop_requested: break
        
        q_id = quest.get("QuestID", "Unknown")
        log.info(f"퀘스트 처리 중: {q_id}")

        # 프로파일 로드 또는 생성
        if q_id in all_profiles:
            q_profile = all_profiles[q_id]
        else:
            q_profile = profile_scene(quest, backend, args.use_ja_ref, mod_stem, rag)
            all_profiles[q_id] = q_profile
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(all_profiles, f, ensure_ascii=False, indent=2)

        if args.profile_only: continue

        dial_flat_list = []
        # Scenes 내부 데이터 평탄화
        for s in quest.get("Scenes", []):
            for d in s.get("Dials", []):
                for dial in d.get("Dialogues", []):
                    txt = dial.get("Text")
                    if txt:
                        if is_only_tags_and_punct(txt):
                            dial["Translate"] = txt
                        else:
                            dial_flat_list.append(dial)
                    for choice in dial.get("PlayerChoices", []):
                        ctxt = choice.get("Text")
                        if ctxt:
                            if is_only_tags_and_punct(ctxt):
                                choice["Translate"] = ctxt
                            else:
                                dial_flat_list.append(choice)
        
        # StandaloneDials 평탄화
        for dial in quest.get("StandaloneDials", []):
            for d in dial.get("Dialogues", []):
                txt = d.get("Text")
                if txt:
                    if is_only_tags_and_punct(txt):
                        d["Translate"] = txt
                    else:
                        dial_flat_list.append(d)
                for choice in d.get("PlayerChoices", []):
                    ctxt = choice.get("Text")
                    if ctxt:
                        if is_only_tags_and_punct(ctxt):
                            choice["Translate"] = ctxt
                        else:
                            dial_flat_list.append(choice)

        chunks = [dial_flat_list[i : i + 20] for i in range(0, len(dial_flat_list), 20)]
        recent_context = []

        num_chunks = len(chunks)
        for i, chunk in enumerate(chunks):
            if b_stop_requested: break
            log.info(f"  -> [청크 {i+1}/{num_chunks}] {len(chunk)}개 대사 번역 중...")
            
            # 번역 대상이 이미 채워져 있는지 확인 (중복 번역 방지 로직은 필요시 추가)
            translate_scene_recursive(chunk, backend, mod_stem, recent_context, q_profile, glossary_text, rag, tone_profiles)
            recent_context = chunk[-5:]

            # 중간 저장 (실시간 반영)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(scenes_data, f, ensure_ascii=False, indent=2)

    log.info("모든 작업이 완료되었습니다.")
    
    # 1min.ai 누적 크레딧 출력
    from llm_backend import Min1AIBackend
    if Min1AIBackend.total_used_credit > 0:
        log.info(f"\n[결산] 이번 세션에서 사용된 총 1min.ai 크레딧: {Min1AIBackend.total_used_credit}")

if __name__ == "__main__":
    main()
