# =============================================================================
# 스타필드 번역 후처리 시스템 v2.1
# 신규 기능: 미번역 스캔 모드 & 선택 번역 모드
# =============================================================================

from pathlib import Path
import xml.etree.ElementTree as ET
import json, re, time, logging, signal, sys, argparse
from llm_backend import get_llm_backend
from typing import List, Tuple, Dict, Optional
from pipeline_runner import (
    EXIT_ARGUMENT_ERROR,
    EXIT_INPUT_MISSING,
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    ensure_parent,
    print_ok,
    require_file,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    parser.add_argument("--mode", choices=["xml", "step2"], default="xml", help="실행 모드 분리")
    parser.add_argument("--input-xml", dest="input_xml", default=None, help="Standardized input XML path")
    parser.add_argument("--output-xml", dest="output_xml", default=None, help="Standardized reviewed XML output path")
    parser.add_argument("--input-json", dest="input_json", default=None, help="Step2 translated JSON")
    parser.add_argument("--output-json", dest="output_json", default=None, help="Step2 reviewed JSON")
    parser.add_argument("--tone-profile", dest="tone_profile", default=None, help="Audio Tone Profile JSON")
    parser.add_argument("--scan-output", dest="scan_output", default=None, help="Standardized scan JSON output path")
    parser.add_argument("-i", "--input", required=False, help="입력 (XML or JSON)")
    parser.add_argument("-o", "--output", default=None, help="출력 (XML or JSON)")
    parser.add_argument("--scan-only", action="store_true", help="미번역/오류 항목 스캔 후 JSON 출력")
    parser.add_argument("--translate-indices", help="번역할 항목의 인덱스 리스트 (쉼표 구분)")
    args = parser.parse_args()

    if args.mode == "step2":
        return review_step2_json(args)

    args.input = args.input_xml or args.input
    args.output = args.output_xml or args.output or "postprocess_final.xml"
    if not args.input:
        print("Error: --input-xml is required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR

    try:
        input_path = require_file(args.input, "input XML")
    except FileNotFoundError as exc:
        log.error(f"Error: {exc}")
        return EXIT_INPUT_MISSING

    output_path = ensure_parent(args.output)
    # The scan JSON intentionally lives beside the reviewed XML so GUI/manual
    # follow-up work can reuse the same findings without reparsing logs.
    scan_result_path = ensure_parent(args.scan_output) if args.scan_output else input_path.parent / "step5_scan_results.json"

    try:
        tree = ET.parse(str(input_path))
        root = tree.getroot()
    except Exception as e:
        log.error(f"Error: XML Parse fail {e}")
        return EXIT_INTERNAL_ERROR

    # Build one flat issue list first; later branches can either export it
    # directly or use selected indices for a focused retry pass.
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

    # Scan mode exits without mutating the XML.
    # --scan-only 모드: JSON 결과만 출력하고 종료
    if args.scan_only:
        with open(scan_result_path, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        log.info(f"SCAN_COMPLETE: {len(all_items)} items found. Saved to {scan_result_path.name}")
        print_ok(scan_result_path)
        return EXIT_SUCCESS

    # --translate-indices 모드: 지정된 인덱스만 번역 실행
    if args.translate_indices:
        target_indices = [int(x.strip()) for x in args.translate_indices.split(",") if x.strip()]
        log.info(f"번역 시작: 총 {len(target_indices)}개 항목 선택됨.")

        # Step 5 is incremental by design: only the selected indices are retried,
        # everything else stays untouched in the current XML.
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
            return EXIT_INTERNAL_ERROR

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
        tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
        with open(scan_result_path, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        log.info(f"번역 완료! 저장됨: {output_path}")
        print_ok(output_path)
        return EXIT_SUCCESS

    with open(scan_result_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)
    tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
    log.info(f"Scan complete. Reviewed XML saved to {output_path}")
    print_ok(output_path)
    return EXIT_SUCCESS

def review_step2_json(args) -> int:
    input_path = args.input_json or args.input
    output_path = args.output_json or args.output
    if not input_path:
        log.error("Error: --input-json is required for mode step2")
        return EXIT_ARGUMENT_ERROR
        
    try:
        input_file = require_file(input_path, "input JSON")
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_INPUT_MISSING
        
    out_file = ensure_parent(output_path) if output_path else input_file.parent / "mod.step2.reviewed.json"
    scan_file = ensure_parent(args.scan_output) if args.scan_output else input_file.parent / "mod.step2.scan.json"
    
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    tone_profiles = {}
    if args.tone_profile:
        try:
            with open(args.tone_profile, "r", encoding="utf-8") as f:
                tp_data = json.load(f)
                tone_profiles = tp_data.get("speakers", {})
        except Exception as e:
            log.warning(f"Tone profile 로드 실패: {e}")
            
    # LLM 초기화 ("review" 역할 명시)
    try:
        cfg = {}
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        backend = get_llm_backend(cfg, "step4_prompt", role="review", max_retries=Config.MAX_RETRIES, retry_base_wait=Config.RETRY_BASE_WAIT)
    except Exception as e:
        log.error(f"LLM 초기화 실패: {e}")
        return EXIT_INTERNAL_ERROR

    scan_report = {"total": 0, "reviewed": 0, "untranslated_fixed": 0, "tag_errors_fixed": 0, "items": []}
    
    scenes = data.get("scenes", [])
    for scene in scenes:
        for idx, line in enumerate(scene.get("lines", [])):
            scan_report["total"] += 1
            src = line.get("english", "")
            dst = line.get("korean", "")
            speaker = line.get("speaker_name", "")
            form_id = line.get("form_id", "")
            
            needs_review = False
            issue_type = ""
            if is_untranslated(src, dst):
                needs_review = True
                issue_type = "untranslated"
            else:
                tag_err = check_tag_integrity(src, dst)
                if tag_err:
                    needs_review = True
                    issue_type = "tag_error"
                    
            if needs_review:
                tone = tone_profiles.get(speaker, {})
                tone_desc = json.dumps(tone, ensure_ascii=False) if tone else "기본(격식)"
                
                prompt = (f"아래 대사를 번역/교정하세요. 구조 보존을 최우선으로 하세요. 오직 교정된 한국어 번역만 답변하세요.\n"
                          f"원문: {src}\n"
                          f"번역: {dst}\n"
                          f"화자 톤 가이드: {tone_desc}")
                
                try:
                    res = backend.generate_content(prompt, temperature=0.2)
                    fixed = " ".join(res.split()) if res else dst
                    if fixed and fixed != dst:
                        line["korean"] = fixed
                        scan_report["reviewed"] += 1
                        if issue_type == "untranslated": scan_report["untranslated_fixed"] += 1
                        else: scan_report["tag_errors_fixed"] += 1
                        
                        scan_report["items"].append({
                            "form_id": form_id,
                            "speaker": speaker,
                            "issue": issue_type,
                            "original": src,
                            "fixed": fixed,
                            "tone_used": tone.get("tone", "unknown") if tone else "unknown"
                        })
                except Exception as e:
                    log.warning(f"교정 실패 ({form_id}): {e}")
                    
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    with open(scan_file, "w", encoding="utf-8") as f:
        json.dump(scan_report, f, ensure_ascii=False, indent=2)
        
    log.info(f"Step2 Review 완료: 총 교정 {scan_report['reviewed']}건. {out_file.name}")
    print_ok(out_file)
    return EXIT_SUCCESS

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
