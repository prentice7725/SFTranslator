# =============================================================================
# 스타필드 번역 후처리 시스템 v2.1
# 신규 기능: 미번역 스캔 모드 & 선택 번역 모드
# =============================================================================

from pathlib import Path
import xml.etree.ElementTree as ET
import json, re, time, logging, signal, sys, argparse
from llm_backend import get_llm_backend
from typing import List, Tuple, Dict, Optional

SCRIPT_DIR = Path(__file__).parent.resolve()

# =============================================================================
# 설정
# =============================================================================
class Config:
    GCP_PROJECT_ID  = "project-2c984893-491f-4636-adf"
    GCP_LOCATION    = "asia-northeast1"
    MODEL_NAME      = "gemini-2.5-flash"

    BATCH_SIZE      = 10
    RPM_DELAY       = 0.5
    MAX_RETRIES     = 3
    RETRY_BASE_WAIT = 60

    # Step 4와 공유하기 위해 기본값을 비워두고 로드 시 step4_prompt를 우선함
    STEP5_PROMPT = "" 

CONFIG_PATH = SCRIPT_DIR / "config.json"
if CONFIG_PATH.exists():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
            Config.GCP_PROJECT_ID = _cfg.get("gcp_project_id", Config.GCP_PROJECT_ID)
            Config.GCP_LOCATION   = _cfg.get("gcp_location",   Config.GCP_LOCATION)
            Config.MODEL_NAME     = _cfg.get("model_name",     Config.MODEL_NAME)
            # Step 4 프롬프트를 공유하여 사용
            Config.STEP5_PROMPT   = _cfg.get("step4_prompt", _cfg.get("step5_prompt", ""))
    except: pass

# =============================================================================
# 로거
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s", # GUI 연동을 위해 간단하게 출력
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("Step5_PostProcess")

# =============================================================================
# 용어집 & 태그 보호 (기존 코드 유지)
# =============================================================================
from db_manager import DBRAG, load_glossary_db
GLOSSARY = load_glossary_db()
_COMPILED_GLOSSARY = []
for _eng, _kor in GLOSSARY.items():
    _pat = re.escape(_eng)
    if re.search(r'^[a-zA-Z0-9_]', _eng): _pat = r'\b' + _pat
    if re.search(r'[a-zA-Z0-9_]$', _eng): _pat = _pat + r'\b'
    _COMPILED_GLOSSARY.append((re.compile(_pat, re.IGNORECASE), _kor))

def apply_terms(text: str) -> str:
    tags = []
    def _mask(m):
        tags.append(m.group(0))
        return f"\x00TAG{len(tags)-1}\x00"
    masked = re.sub(r'<[^>]+>', _mask, text)
    for pattern, kor in _COMPILED_GLOSSARY:
        masked = pattern.sub(kor, masked)
    for i, tag in enumerate(tags):
        masked = masked.replace(f"\x00TAG{i}\x00", tag)
    return masked

class TagPreserver:
    @staticmethod
    def mask_tags(text: str) -> Tuple[str, list]:
        tags = []
        def replacer(m):
            tags.append(m.group(0))
            return f"[[TAG_{len(tags)-1}]]"
        return re.sub(r'<[^>]+>', replacer, text), tags

    @staticmethod
    def restore_tags(text: str, tags: list) -> str:
        for i, tag in enumerate(tags):
            text = text.replace(f"[[TAG_{i}]]", tag)
        return text

def is_untranslated(src: str, dst: str) -> bool:
    if not dst or not dst.strip(): return bool(src and src.strip())
    if bool(re.search(r'[\uac00-\ud7a3]', dst)): return False
    return dst.strip() == src.strip()

def extract_tags(text: str) -> List[str]:
    return re.findall(r'<[^>]+>', text)

def check_tag_integrity(src: str, dst: str) -> Optional[str]:
    src_tags = extract_tags(src)
    dst_tags = extract_tags(dst)
    if src_tags == dst_tags: return None
    src_set, dst_set = set(src_tags), set(dst_tags)
    issues = []
    missing = src_set - dst_set
    extra = dst_set - src_set
    if missing: issues.append(f"누락: {', '.join(missing)}")
    if extra: issues.append(f"추가: {', '.join(extra)}")
    if not missing and not extra and src_tags != dst_tags: issues.append("순서 오류")
    return " | ".join(issues) if issues else None

# =============================================================================
# 번역 로직 (기사님, Step 4 구조 재활용)
# =============================================================================
# ... (기존 translate_batch_llm 및 build_batch_prompt 함수 내용 유지하되 프롬프트 로직 간소화 가능)
def build_batch_prompt(batch: List[Tuple[str, str]]) -> str:
    payload = [{"id": i, "text": txt, "rec": rec} for i, (txt, rec) in enumerate(batch)]
    return f"""번호 순서대로 주어진 'text' 요소들을 번역해 줘.
결과는 오직 JSON 배열 형태로만 줘. 예: [{{"id": 0, "result": "번역문"}}]

**번역 대상:**
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

def translate_batch_llm(needs: List[Tuple[str, str]], backend) -> List[str]:
    masked_batch, tag_maps, originals = [], [], []
    for src, rec in needs:
        originals.append(src)
        m, tags = TagPreserver.mask_tags(src)
        masked_batch.append((m, rec))
        tag_maps.append(tags)

    prompt = build_batch_prompt(masked_batch)
    try:
        raw_text = backend.generate_content(prompt)
        if not raw_text: return originals
        match = re.search(r'\[.*\]', raw_text, re.DOTALL)
        clean_json = match.group(0) if match else raw_text
        import json_repair
        parsed = json_repair.repair_json(clean_json, return_objects=True)
        if isinstance(parsed, dict) and "id" in parsed: parsed = [parsed]
        result_map = {item["id"]: item.get("result", "") for item in parsed if isinstance(item, dict) and "id" in item}
        
        final_results = []
        for i, (orig, tags) in enumerate(zip(originals, tag_maps)):
            trans = result_map.get(i, orig)
            trans = apply_terms(trans)
            final_results.append(TagPreserver.restore_tags(trans, tags))
        return final_results
    except:
        return originals

# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser("Step 5 리서치 및 선택 번역")
    parser.add_argument("-i", "--input", required=True, help="입력 XML")
    parser.add_argument("-o", "--output", default="postprocess_final.xml", help="출력 XML")
    parser.add_argument("--scan-only", action="store_true", help="미번역/오류 항목 스캔 후 JSON 출력")
    parser.add_argument("--translate-indices", help="번역할 항목의 인덱스 리스트 (쉼표 구분)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        log.error(f"Error: File not found {args.input}")
        return

    try:
        tree = ET.parse(str(input_path))
        root = tree.getroot()
    except Exception as e:
        log.error(f"Error: XML Parse fail {e}")
        return

    # 전수 조사 (스캔)
    all_items = []
    strings = root.findall(".//String")
    for idx, str_elem in enumerate(strings):
        src_node = str_elem.find("Source")
        dst_node = str_elem.find("Dest")
        rec_node = str_elem.find("REC")
        edid_node = str_elem.find("EDID")

        src_val = src_node.text or "" if src_node is not None else ""
        dst_val = dst_node.text or "" if dst_node is not None else ""
        rec_val = rec_node.text or "" if rec_node is not None else ""
        edid_val = edid_node.text or "" if edid_node is not None else ""

        if not src_val.strip(): continue

        status = "OK"
        error_msg = ""

        # 1. 미번역 체크 (Pending)
        if is_untranslated(src_val, dst_val):
            status = "Pending"
        
        # 2. 태그 무결성 체크 (TagError)
        tag_err = check_tag_integrity(src_val, dst_val)
        if tag_err:
            status = "TagError"
            error_msg = tag_err

        if status != "OK":
            all_items.append({
                "index": idx,
                "status": status,
                "edid": edid_val,
                "rec": rec_val,
                "error": error_msg,
                "source": src_val,
                "dest": dst_val
            })

    # --scan-only 모드: JSON 결과만 출력하고 종료
    if args.scan_only:
        scan_result_path = input_path.parent / "step5_scan_results.json"
        with open(scan_result_path, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        log.info(f"SCAN_COMPLETE: {len(all_items)} items found. Saved to {scan_result_path.name}")
        return

    # --translate-indices 모드: 지정된 인덱스만 번역 실행
    if args.translate_indices:
        target_indices = [int(x.strip()) for x in args.translate_indices.split(",") if x.strip()]
        log.info(f"번역 시작: 총 {len(target_indices)}개 항목 선택됨.")

        # LLM 백엔드 초기화
        try:
            cfg_for_llm = {}
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as _cf:
                    cfg_for_llm = json.load(_cf)
            backend = get_llm_backend(cfg_for_llm, "step4_prompt", # Step 4 프롬프트 공유
                                      max_retries=Config.MAX_RETRIES,
                                      retry_base_wait=Config.RETRY_BASE_WAIT)
        except Exception as e:
            log.error(f"LLM 초기화 실패: {e}")
            return

        rag = DBRAG()
        
        # 인덱스별로 엘리먼트 맵핑
        idx_to_str_node = {idx: strings[idx] for idx in target_indices}
        
        # 배치 처리
        for i in range(0, len(target_indices), Config.BATCH_SIZE):
            batch_idxs = target_indices[i : i + Config.BATCH_SIZE]
            llm_needs = []
            llm_nodes = []

            for b_idx in batch_idxs:
                node = idx_to_str_node[b_idx]
                src = node.find("Source").text or ""
                rec = node.find("REC").text or ""
                
                # RAG 우선 시도
                rag_match = rag.find_fuzzy(src)
                if rag_match:
                    node.find("Dest").text = apply_terms(rag_match)
                    log.info(f"[{b_idx}] RAG 적용 완료")
                else:
                    llm_needs.append((src, rec))
                    llm_nodes.append(node)

            if llm_needs:
                results = translate_batch_llm(llm_needs, backend)
                for node, trans in zip(llm_nodes, results):
                    node.find("Dest").text = trans
                    log.info(f"  √ LLM 번역 완료")

            time.sleep(Config.RPM_DELAY)

        # 결과 저장
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
        log.info(f"번역 완료! 저장됨: {args.output}")

if __name__ == "__main__":
    main()
