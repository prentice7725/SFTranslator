import argparse
import json
import logging
import re
import signal
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

from db_manager import DBRAG, load_glossary_db
from llm_backend import get_llm_backend

# =============================================================================
# ⚙️ 설정 (CONFIG)
# =============================================================================
SCRIPT_DIR = Path(__file__).parent.resolve()


class Config:
    GCP_PROJECT_ID = "project-2c984893-491f-4636-adf"
    GCP_LOCATION = "asia-northeast1"
    MODEL_NAME = "gemini-2.5-flash"

    BATCH_SIZE = 20
    RPM_DELAY = 0.5
    MAX_RETRIES = 3
    RETRY_BASE_WAIT = 60

    STEP4_PROMPT = (
        "당신은 베데스다 스타필드 게임의 로컬라이제이션 전문가입니다.\n"
        "[번역 핵심 원칙]\n"
        "1. 영문 직역체(번역투)를 절대 피하고, 한국어 원어민이 말하듯 '찰지고 자연스럽게(초월 번역)' 의역하십시오. (예: after being presented with the evidence -> 증거를 제시받은 후 (X) / 증거를 들이대자 (O))\n"
        "2. 제공된 **용어집(Glossary)** 및 스타필드 공식 용어를 무조건 준수하십시오.\n\n"
        "3. 만약 **일본어 참조(ja)** 데이터가 제공된다면, 이를 활용하여 영어 원문만으로는 파악하기 힘든 화자의 성별, 경어 체계(존댓말/반말), 사물의 뉘앙스를 한국어 번역에 적극 반영하십시오.\n"
        "4. 확신이 들지 않으면 억지로 창작하지 말고 **보수적으로 번역**하되, 미번역 상태를 남기지 마십시오.\n\n"
        "[REC 태그별 스타일 가이드]\n"
        "- `KYWD:*`, `GMST:*`, `MGEF:*`, `PERK:*` : 스킬/버프 등의 UI 텍스트입니다. **절대 서술형(~합니다)을 쓰지 말고, 간결한 명사형(~증가, ~추가, ~함)으로 끝맺으십시오.**\n"
        "- `*:FULL` : 아이템, 퀘스트 등의 고유 명칭. 명사형으로 간결하게 번역.\n"
        "- `BOOK:*`, `TERM:*`, `MESG:*` : 문서나 단말기 본문. 경찰/보안 보고서라면 딱딱한 문어체(~하였다)를 사용하십시오.\n"
        "결과는 오직 허용된 JSON 배열(id, result 속성)로만 반환하십시오. 절대 다른 말을 덧붙이지 마십시오."
    )


CONFIG_PATH = SCRIPT_DIR / "config.json"
if CONFIG_PATH.exists():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
            Config.GCP_PROJECT_ID = _cfg.get("gcp_project_id", Config.GCP_PROJECT_ID)
            Config.GCP_LOCATION = _cfg.get("gcp_location", Config.GCP_LOCATION)
            Config.MODEL_NAME = _cfg.get("model_name", Config.MODEL_NAME)
            Config.STEP4_PROMPT = _cfg.get("step4_prompt", Config.STEP4_PROMPT)
    except:
        pass

# =============================================================================
# 로거 설정
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("Step4_Mapper")

# =============================================================================
# 유틸리티: Glossary & Tag Preserver
# =============================================================================
GLOSSARY = load_glossary_db()

_COMPILED_GLOSSARY: List[Tuple[re.Pattern, str]] = []
for _eng, _kor in GLOSSARY.items():
    _pat = re.escape(_eng)
    if re.search(r"^[a-zA-Z0-9_]", _eng):
        _pat = r"\b" + _pat
    if re.search(r"[a-zA-Z0-9_]$", _eng):
        _pat = _pat + r"\b"
    _COMPILED_GLOSSARY.append((re.compile(_pat, re.IGNORECASE), _kor))


def apply_terms(text: str) -> str:
    tags: list = []

    def _mask(m):
        tags.append(m.group(0))
        return f"\x00TAG{len(tags) - 1}\x00"

    masked = re.sub(r"<[^>]+>", _mask, text)

    for pattern, kor in _COMPILED_GLOSSARY:
        masked = pattern.sub(kor, masked)

    for i, tag in enumerate(tags):
        masked = masked.replace(f"\x00TAG{i}\x00", tag)
    return masked


class TagPreserver:
    @staticmethod
    def mask_tags(text):
        tags = []

        def replacer(match):
            tags.append(match.group(0))
            return f"[[TAG_{len(tags) - 1}]]"

        masked = re.sub(r"<[^>]+>", replacer, text)
        return masked, tags

    @staticmethod
    def restore_tags(text, tags):
        for i, tag in enumerate(tags):
            text = text.replace(f"[[TAG_{i}]]", tag)
        if "[[TAG" in text:
            text = re.sub(r"\[\[TAG_\d+\]\]", "", text)
        return text


# =============================================================================
# 🛑 [Step 1 & 2] 필터링 규칙 (기 번역 감지 및 시스템 코드 컷)
# =============================================================================

# 모듈 레벨 정규식 (호출마다 컴파일 방지)
_KOREAN_RE = re.compile(r"[\uac00-\ud7a3]")
_PREFIX_PATTERN = re.compile(
    r"^(List|LVL|SEQ|GLOB|LMK|FST|Dialogue|EDID|REC|MGEF|STAT|ACTI|WRLD"
    r"|CELL|REFR|NPC|CONT|DOOR|MISC|AMMO|KEYM|ALCH|IDLM|COBJ|PROJ"
    r"|HAZD|SLGM|LVLN|LVLI|LVSP|GMST|KYWD|LCRT|AACT|TXST|GLOB|CLAS"
    r"|FACT|HDPT|EYES|RACE|SOUN|ASPC|MGEF|ENCH|SPEL|ACHR|REFR"
    r"|SCOL|LAND|NAVM|TLOD|DIAL|INFO|QUST|PACK|CSTY|LSCR|ANIO|WATR"
    r"|EFSH|EXPL|DEBR|IMGS|IMAD|FLST|PERK|BPTD|ADDN|AVIF|CAMS|CPTH"
    r"|VTYP|MATT|IPCT|IPDS|ARMA|ECZN|LCTN|MESG|RGDL|DOBJ|LGTM|MUSC"
    r"|FSTP|FSTT|SMBN|SMQN|SMEN|DLBR|MUST|DLVW|WOOP|SHOU|EQUP|RELA"
    r"|SCEN|ASTP|OTFT|ARTO|MATO|MOVT|SNDR|DUAL|SNCT|SOPM|COLL|CLFM"
    r"|REVB|PKIN|RFGP|AMDL|LAYR|COBJ|OMOD|INNR|KSSM|AECH|SCCO|AORU"
    r"|STAG|AIM|AISK|ARMP|ACED|MSWP|ZOOM|INNR|KNAM|GCVR|MFUC|RADS"
    r"|AVIF|FURN"
    r")_[A-Za-z0-9_]+",
    re.IGNORECASE,
)


def is_already_korean(text: str) -> bool:
    """[Step 1] 기 번역 완료 제외: 텍스트에 한글이 포함되어 있으면 스킵"""
    if not text:
        return False
    return bool(_KOREAN_RE.search(text))


def is_internal_code(text: str) -> bool:
    """[Step 2] 번역 제외: _ 가 중간에 포함된 코드/식별자 패턴 감지"""
    text = text.strip()
    if not text:
        return True

    # ── 언더바 포함 단일 토큰 ─────────────────────────────────────────────
    if " " not in text and "_" in text:
        return True

    # ── 알려진 접두어 패턴 (모듈 레벨 _PREFIX_PATTERN 참조) ───────────────
    if _PREFIX_PATTERN.match(text):
        return True

    return False


def is_only_tags_and_punct(text: str) -> bool:
    """[Step 2.5] 태그와 문장 부호만 있는 경우 제외 (예: <Alias=Name>.)"""
    # 1. 모든 <...> 형태의 태그 제거
    stripped = re.sub(r"<[^>]+>", "", text)
    # 2. 공백 및 일반적인 문장 부호 제거
    stripped = re.sub(r'[\s.,!?;:"\'-_\[\]()]+', "", stripped)
    # 3. 남은 문자가 없으면 태그/부호만 있는 것으로 간주
    return len(stripped) == 0


def should_translate(src_text: str, rec_val: str) -> bool:
    src_text = src_text.strip()
    if not src_text:
        return False

    if is_internal_code(src_text):
        return False

    if is_only_tags_and_punct(src_text):
        return False

    if not rec_val:
        return True

    parts = rec_val.split(":")
    rec_type = parts[0].strip()
    rec_field = parts[1].strip() if len(parts) > 1 else ""

    if "FULL" in rec_field or "FULL" in rec_type:
        # 명시적 번역 제외 시스템 레코드
        skip_types = {"LVLI", "COLL", "ASTR", "LMKS", "SEQN", "GMST", "GLOB", "FLST"}
        if rec_type in skip_types:
            return False

    return True


# =============================================================================
# LLM 프롬프트 및 호출 로직
# =============================================================================
def build_batch_prompt(masked_batch: list) -> str:
    payload = []
    for i, (txt, rec, ja) in enumerate(masked_batch):
        item = {"id": i, "text": txt, "rec": rec}
        if ja:
            item["ja"] = ja
        payload.append(item)

    glossary_str = "\n".join(f"- {k}: {v}" for k, v in GLOSSARY.items())

    prompt = f"""번호 순서대로 주어진 'text' 요소들을 하나도 빠짐없이 전부 번역해 줘.
작업할 개수: {len(masked_batch)}개

**[용어집]**
{glossary_str if glossary_str else "(비어 있음)"}

**지시사항:**
1. [[TAG_n]] 형태의 플레이스홀더는 **절대 번역하거나 수정하지 말고** 원문 그대로 복사해 출력해.
2. {"아래 용어집을 무조건 준수해:\n" + glossary_str if glossary_str else "용어집이 없습니다. 게임 문맥에 맞게 번역하세요."}
3. 모든 ID(0부터 {len(masked_batch) - 1}까지)를 결과 JSON 배열에 반드시 포함해야 해. 하나라도 누락되면 안 돼.
4. 결과는 아래 예시처럼 JSON 배열 형태로만 줘. 여분의 설명은 절대 하지 마.

예시:
[
  {{"id": 0, "result": "번역문1"}},
  {{"id": 1, "result": "번역문2"}}
]

**번역 대상:**
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""
    return prompt


def translate_single_item(src: str, rec: str, backend, ja: str = None) -> str:
    """[Step 4] 장문 전용 1:1 단독 번역 처리기"""
    masked, tags = TagPreserver.mask_tags(src)
    g_str = "\n".join(f"- {k}: {v}" for k, v in GLOSSARY.items())

    ja_info = f"\n**일본어 참조:**\n{ja}\n" if ja else ""

    prompt = f"""[단독 장문 번역] 아래 텍스트 하나만 한국어로 번역해주세요.
{ja_info}
**용어집:**
{g_str if g_str else "(없음)"}

**규칙:**
1. [[TAG_n]] 플레이스홀더 절대 수정 금지
2. {"아래 용어집을 무조건 준수해:\n" + g_str if g_str else "용어집이 없습니다."}
3. 번역문만 출력 (여타 설명 금지)
4. rec 타입: {rec}

**원문:**
{masked}

**번역:**"""
    try:
        result = backend.generate_content(prompt)
        if not result or not result.strip():
            return src

        clean_text = result.strip()
        # 1. Markdown 코드 블록 제거
        if clean_text.startswith("```"):
            clean_text = clean_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        # 2. JSON 형태 파싱 시도 ({...} 또는 [...] 형태인 경우)
        match = re.search(r"(\{.*\}|\[.*\])", clean_text, re.DOTALL)
        if match:
            candidate = match.group(0)
            try:
                # json_repair 시도, 실패 시 json.loads 폴백
                try:
                    import json_repair

                    parsed = json_repair.repair_json(candidate, return_objects=True)
                except (ImportError, ModuleNotFoundError):
                    parsed = json.loads(candidate, strict=False)

                if isinstance(parsed, dict) and "result" in parsed:
                    clean_text = str(parsed["result"])
                elif isinstance(parsed, list) and len(parsed) > 0:
                    item = parsed[0]
                    if isinstance(item, dict) and "result" in item:
                        clean_text = str(item["result"])
                    elif isinstance(item, str):
                        clean_text = item
            except Exception:
                # 파싱 에러 시 원본(clean_text) 그대로 사용
                pass

        clean_text = apply_terms(clean_text)
        return TagPreserver.restore_tags(clean_text, tags)
    except Exception as e:
        log.error(f"  장문 단독 번역 실패: {e}")
        return src


def translate_batch_llm(needs: list, backend, current_depth=0) -> list:
    """단거리 배치 처리기 (50개씩)"""
    masked_batch, tag_maps, original_texts = [], [], []
    for txt, rec, ja in needs:
        original_texts.append(txt)
        m, tags = TagPreserver.mask_tags(txt)
        masked_batch.append((m, rec, ja))
        tag_maps.append(tags)

    prompt = build_batch_prompt(masked_batch)

    for attempt in range(1, 3):
        try:
            raw_text = backend.generate_content(prompt)
            if not raw_text:
                raise ValueError("MAX_TOKENS")

            match = re.search(r"\[.*\]", raw_text, re.DOTALL)
            clean_json = match.group(0) if match else raw_text

            try:
                import json_repair

                parsed = json_repair.repair_json(clean_json, return_objects=True)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except ImportError:
                parsed = json.loads(clean_json, strict=False)

            if isinstance(parsed, dict) and "id" in parsed:
                parsed = [parsed]
            elif not isinstance(parsed, list):
                raise json.JSONDecodeError("Not a list", clean_json, 0)

            result_map = {
                item["id"]: item.get("result", "")
                for item in parsed
                if isinstance(item, dict) and "id" in item
            }

            final_results = []
            for i, (orig, tags) in enumerate(zip(original_texts, tag_maps)):
                trans_text = result_map.get(i, "")
                orig_clean = orig.strip()

                # 번역 실패 조건 정의
                is_missing = not trans_text.strip()
                is_echo = trans_text.strip() == orig_clean
                is_no_korean = not is_already_korean(trans_text)

                # [개선] 5자 이하의 아주 짧은 텍스트(고유명사 등)가 영어 그대로인 경우는 허용
                should_fallback = is_missing
                if not is_missing:
                    # 영문 그대로인 경우
                    if is_echo or is_no_korean:
                        if (
                            len(orig_clean) > 5
                        ):  # 5자 넘는 문장인데 한글이 없으면 실패로 간주
                            should_fallback = True
                        else:
                            # 5자 이하 짧은 영문은 그냥 통과 (Starfield, Bethesda 등)
                            should_fallback = False

                if should_fallback:
                    log.warning(
                        f"[{i}번 항목] 배치 번역 누락/품질 저하 감지! ('{orig_clean[:20]}...') 단독 모드로 복구합니다..."
                    )
                    single_src, single_rec, single_ja = needs[i]
                    restored = translate_single_item(
                        single_src, single_rec, backend, ja=single_ja
                    )
                    final_results.append(restored)
                else:
                    trans_masked = apply_terms(trans_text)
                    restored = TagPreserver.restore_tags(trans_masked, tags)
                    final_results.append(restored)
            return final_results

        except Exception as e:
            err_str = str(e).upper()
            if "MAX_TOKENS" in err_str or "RECITATION" in err_str or "JSON" in err_str:
                if len(needs) > 1 and current_depth < 3:
                    mid = len(needs) // 2
                    log.info(
                        f"  [Depth {current_depth}] 예외 발생({e}). 청크를 반으로 쪼개서 재시도합니다..."
                    )
                    return translate_batch_llm(
                        needs[:mid], backend, current_depth + 1
                    ) + translate_batch_llm(needs[mid:], backend, current_depth + 1)
                else:
                    # 끝까지 쪼개졌는데도 안되면 최후의 보루: 단독 번역 뺑뺑이
                    results = []
                    for i, (src, rec, ja) in enumerate(needs):
                        results.append(translate_single_item(src, rec, backend, ja=ja))
                    return results
            return original_texts
    return original_texts


# =============================================================================
# 종료 처리 핸들러
# =============================================================================
b_stop_requested = False


def signal_handler(sig, frame):
    global b_stop_requested
    log.warning("중지 요청을 받았습니다! 현재 큐까지만 처리하고 안전하게 저장합니다...")
    b_stop_requested = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# =============================================================================
# MAIN EXECUTOR (천재적 5단계 아키텍처)
# =============================================================================
def main():
    parser = argparse.ArgumentParser("Step 4 XML Translator (5-Step Optimized)")
    parser.add_argument("-i", "--input", required=True, help="Path to input XML")
    parser.add_argument(
        "-o", "--output", default="translate_full.xml", help="Path to output XML"
    )
    parser.add_argument(
        "--use-ja-ref", action="store_true", help="일본어 원문 참조 모드 활성"
    )
    parser.add_argument(
        "--mod-name", default=None, help="참조 DB 조회를 위한 모드 이름 (예: Starfield)"
    )
    args = parser.parse_args()

    target_xml = args.input
    progress_path = Path(args.output).with_suffix(".progress.xml")
    if progress_path.exists():
        log.info(f"임시 작업 파일 발견! 이어하기를 시도합니다: {progress_path.name}")
        target_xml = str(progress_path)

    tree = ET.parse(target_xml)
    root = tree.getroot()
    rag = DBRAG()

    config = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                config = json.load(_f)
        except Exception:
            pass
    from orchestrator import TranslationOrchestrator
    orch_cfg = config.get("orchestrator", {})
    if orch_cfg.get("enabled"):
        log.info("Step 4: 멀티 모델 오케스트레이터 모드 활성화")
        gen_backends = []
        glossary_dict = load_glossary_db()
        glossary_text = "\n".join([f"- {k}: {v}" for k, v in glossary_dict.items()])
        
        for m in orch_cfg.get("generation_models", []):
            m_config = config.copy()
            m_config["api_provider"] = m["provider"]
            m_config["model_name"] = m["model"]
            persona_prompt = f"{config.get('step4_prompt', '')}\n\n[페르소나] {m['persona']}"
            m_config["_temp_prompt"] = persona_prompt
            gen_backends.append(get_llm_backend(m_config, "_temp_prompt"))
        
        review_cfg = orch_cfg.get("review_model", {})
        r_config = config.copy()
        r_config["api_provider"] = review_cfg["provider"]
        r_config["model_name"] = review_cfg["model"]
        review_backend = get_llm_backend(r_config, "step4_prompt")
        
        backend = TranslationOrchestrator(gen_backends, review_backend, glossary_text=glossary_text)
    else:
        backend = get_llm_backend(
            config,
            "step4_prompt",
            max_retries=Config.MAX_RETRIES,
            retry_base_wait=Config.RETRY_BASE_WAIT,
        )

    # 모드 이름 결정
    mod_stem = args.mod_name
    if not mod_stem:
        # 입력 파일이 Starfield.xml 식이면 Starfield를 추출 시도
        mod_stem = Path(args.input).stem
    log.info(f"Using mod name for DB reference: {mod_stem}")

    # -------------------------------------------------------------
    # 🌟 [Step 5] 중복 제거 및 캐싱 큐 준비
    # -------------------------------------------------------------
    unique_translation_tasks: Dict[str, List[Tuple[ET.Element, str, str]]] = {}
    rag_hit_count = 0
    filtered_count = 0

    for str_elem in root.findall(".//String"):
        src_elem = str_elem.find("Source")
        dst_elem = str_elem.find("Dest")
        if src_elem is None or dst_elem is None:
            continue

        src_val = src_elem.text if src_elem.text is not None else ""
        dst_val = dst_elem.text if dst_elem.text is not None else ""
        rec_elem = str_elem.find("REC")
        rec_val = rec_elem.text if rec_elem is not None else ""

        # sID(StringID) 추출
        sid_str = str_elem.get("sID", "0")
        try:
            sid_int = int(sid_str, 16)
        except ValueError:
            sid_int = 0

        if not src_val.strip():
            continue

        # [Step 1] 기 번역 완료 제외
        if is_already_korean(dst_val):
            continue

        # [Step 2] 패턴/코드명 번역 제외
        if not should_translate(src_val, rec_val):
            dst_elem.text = src_val
            filtered_count += 1
            continue

        # [Step 3] RAG 100% 완전 일치만 즉시 적용 (유사도 기반 매칭 제외)
        rag_match = rag.find_exact(src_val)
        if rag_match:
            dst_elem.text = apply_terms(rag_match)
            rag_hit_count += 1
            continue

        # 🇯🇵 일본어 참조 준비
        ja_val = None
        if args.use_ja_ref and sid_int > 0:
            ja_val = rag.get_reference_string(mod_stem, sid_int, "ja")

        # [Step 5] 중복 번역 방지를 위해 원문을 Key로 요소들을 묶음 (Deduplication)
        if src_val not in unique_translation_tasks:
            unique_translation_tasks[src_val] = []
        unique_translation_tasks[src_val].append((dst_elem, rec_val, ja_val))

    log.info(
        f"필터링됨(시스템코드): {filtered_count}개 | RAG 캐시 적중: {rag_hit_count}개"
    )

    if not unique_translation_tasks:
        log.info("모든 텍스트가 번역/처리되었습니다!")
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
        return

    # -------------------------------------------------------------
    # 🌟 [Step 4] 장문(단독) / 단문(배치) 큐 분리
    # -------------------------------------------------------------
    single_queue = []
    batch_queue = []

    for src_text, elements in unique_translation_tasks.items():
        if len(src_text) >= 300:  # 300자 이상은 장문 VIP 큐로 이동
            single_queue.append((src_text, elements))
        else:
            batch_queue.append((src_text, elements))

    log.info(
        f"고유 번역 텍스트 - 장문(VIP): {len(single_queue)}개 | 단문(배치): {len(batch_queue)}개"
    )

    global b_stop_requested

    # --- 1. 단문 배치 처리 루프 ---
    for i in range(0, len(batch_queue), Config.BATCH_SIZE):
        if b_stop_requested:
            break

        chunk = batch_queue[i : i + Config.BATCH_SIZE]
        log.info(
            f"Batch Processing [{i + 1} ~ {min(i + Config.BATCH_SIZE, len(batch_queue))} / {len(batch_queue)}]..."
        )

        # LLM에게는 묶음의 첫 번째 요소의 REC 및 JA 정보만 대표로 보냄
        llm_needs = [(src, elems[0][1], elems[0][2]) for src, elems in chunk]
        translated_results = translate_batch_llm(llm_needs, backend)

        # [Step 5 핵심] 한 번 번역된 결과를 똑같은 원문을 가진 모든 XML 태그에 일괄 복사!
        for (src_text, elements), trans_text in zip(chunk, translated_results):
            for dst_elem, _, _ in elements:
                dst_elem.text = trans_text

        time.sleep(Config.RPM_DELAY)
        if i > 0 and i % (Config.BATCH_SIZE * 5) == 0:
            tree.write(str(progress_path), encoding="utf-8", xml_declaration=True)

    # --- 2. 장문(VIP) 단독 처리 루프 ---
    for i, (src_text, elements) in enumerate(single_queue):
        if b_stop_requested:
            break

        log.info(
            f"VIP Long-Text Processing [{i + 1} / {len(single_queue)}]: {src_text[:40]}..."
        )

        rec_val = elements[0][1]  # 대표 REC
        ja_val = elements[0][2]  # 대표 JA
        trans_text = translate_single_item(src_text, rec_val, backend, ja=ja_val)

        for dst_elem, _, _ in elements:
            dst_elem.text = trans_text

        time.sleep(Config.RPM_DELAY)
        if i > 0 and i % 10 == 0:
            tree.write(str(progress_path), encoding="utf-8", xml_declaration=True)

    # -------------------------------------------------------------
    # 💾 최종 마무리 및 저장
    # -------------------------------------------------------------
    if b_stop_requested:
        tree.write(str(progress_path), encoding="utf-8", xml_declaration=True)
        log.info(f"⚠️ 중지됨. 진행 상황 보존: {progress_path}")
    else:
        tree.write(str(args.output), encoding="utf-8", xml_declaration=True)
        log.info(f"✅ Step 4 완벽히 완료! 결과물 저장: {args.output}")
        if progress_path.exists():
            progress_path.unlink()

    # 1min.ai 누적 크레딧 출력
    from llm_backend import Min1AIBackend
    if Min1AIBackend.total_used_credit > 0:
        log.info(f"\n[결산] 이번 세션에서 사용된 총 1min.ai 크레딧: {Min1AIBackend.total_used_credit}")


if __name__ == "__main__":
    main()
