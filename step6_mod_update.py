import os
import argparse
import json
import sys
import xml.etree.ElementTree as ET
from llm_backend import get_llm_backend
from step1_extract_scene import StringsLoader
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

def load_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def extract_texts_from_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    texts = {}
    
    if ext == '.xml':
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        # 방식 1: <Content id="..."> 구조 (단순)
        # 방식 2: <Content><String><Source>...</Source></String></Content> 구조 (xTranslator)
        
        # 먼저 모든 잠재적 '엔트리'를 찾습니다.
        entries = root.findall('.//Content[@id]') # 방식 1
        if not entries:
            entries = root.findall('.//String') # 방식 2
            
        for entry in entries:
            # ID 결정: id 속성 우선, 없으면 EDID+REC 조합, 그것도 없으면 인덱스
            db_id = entry.get('id')
            if not db_id:
                edid = entry.find('EDID')
                rec = entry.find('REC')
                edid_val = edid.text if edid is not None else ""
                rec_val = rec.text if rec is not None else ""
                db_id = f"{edid_val}|{rec_val}" if edid_val or rec_val else f"idx_{len(texts)}"
                
            source = entry.find('Source')
            dest = entry.find('Dest')
            
            src_text = source.text if source is not None else ""
            dst_text = dest.text if dest is not None else ""
            
            if src_text or dst_text:
                texts[db_id] = {
                    "source": src_text,
                    "dest": dst_text,
                    "element": entry
                }
    elif ext in ['.strings', '.ilstrings', '.dlstrings']:
        loader = StringsLoader()
        # file_path format: modname_en.strings
        base_name = os.path.basename(file_path)
        lang = "en"
        if "_" in base_name:
            stem, lang_ext = base_name.rsplit("_", 1)
            lang = lang_ext.split(".")[0]
        else:
            stem = base_name.split(".")[0]
            
        loader.load(os.path.dirname(file_path), stem, [lang])
        # Combine all loaded tables
        for list_id in range(3):
            table = loader.tables[list_id]
            for str_id, text in table.items():
                texts[str(str_id)] = {
                    "source": text,
                    "dest": "",
                    "element": None
                }
    else:
        raise ValueError("Unsupported file format. Use .xml or .strings")
    
    return texts

def perform_refine(texts, profile_path, config):
    print("Mode: Refine (어투 교정)")
    system_prompt = config.get("step6_refine_prompt", "당신은 게임 전문 번역 교정자입니다. 원문과 번역문이 주어지면, 직역체나 오락가락하는 어투(존댓말/반말 혼용)를 일관성 있고 매끄러운 한국어로 교정하세요.")
    
    if profile_path and os.path.exists(profile_path):
        with open(profile_path, 'r', encoding='utf-8') as f:
            profile_data = json.load(f)
        system_prompt += f"\n[참고 어투 프로파일]:\n{json.dumps(profile_data, ensure_ascii=False)}"
        print(f"Loaded profile from {profile_path}")

    config["step6_refine_prompt"] = system_prompt
    backend = get_llm_backend(config, "step6_refine_prompt")
    
    for db_id, data in texts.items():
        # 교정 모드에서는 Dest가 있어야만 교정 가능
        if not data["dest"] or data["dest"] == data["source"]: 
            continue 
        
        prompt = f"원문: {data['source']}\n기존 번역본: {data['dest']}\n이를 더 자연스럽게 교정하여 결과 JSON만 반환하세요. 형식: {{\"translation\": \"교정된 텍스트\"}}"
        result = backend.generate_content(prompt)
        
        if result:
            try:
                # Remove json blocks if present
                result = result.replace('```json', '').replace('```', '')
                res_json = json.loads(result)
                data["dest"] = res_json.get("translation", data["dest"])
                print(f"[{db_id}] Refined.")
            except Exception as e:
                print(f"[{db_id}] JSON Parse failed: {e}")

def perform_update(texts, ref_path, config):
    print("Mode: Update (버전 업데이트 신규 번역)")
    system_prompt = config.get("step6_update_prompt", "당신은 게임 전문 번역가입니다. 제공된 주변 문맥(기존 번역본)을 참고하여, 새롭게 추가된 원문들의 톤앤매너를 기존 번역과 일치하게 번역하세요.")
    
    ref_texts = {}
    if ref_path and os.path.exists(ref_path):
        try:
            ref_texts = extract_texts_from_file(ref_path)
            print(f"Loaded {len(ref_texts)} reference strings from {ref_path}")
        except Exception as e:
            print(f"Failed to load reference file: {e}")

    backend = get_llm_backend(config, "step6_update_prompt")
    
    for db_id, data in texts.items():
        # Dest가 비어있거나 원문과 동일하면 '미번역'으로 간주하여 진행
        if data["dest"] and data["dest"] != data["source"]: 
            continue # 정식 번역본이 이미 있는 경우만 스킵
            
        if db_id in ref_texts and ref_texts[db_id]["dest"]:
            data["dest"] = ref_texts[db_id]["dest"]
            print(f"[{db_id}] Recovered from Reference file.")
            continue
            
        prompt = f"원문: {data['source']}\n"
        if ref_texts:
            prompt += "참고 문맥 (기존 번역본 중 일부 생략)\n"
        prompt += "위 원문을 한국어로 자연스럽게 번역하여 결과 JSON만 반환하세요. 형식: {{\"translation\": \"번역된 텍스트\"}}"
        
        result = backend.generate_content(prompt)
        if result:
            try:
                result = result.replace('```json', '').replace('```', '')
                res_json = json.loads(result)
                data["dest"] = res_json.get("translation", data["source"])
                print(f"[{db_id}] Translated: {data['dest']}")
            except Exception as e:
                print(f"[{db_id}] JSON Parse failed: {e}")


def _iter_scene_dialogues(data):
    quests = data if isinstance(data, list) else data.get("Quests", [])
    for quest in quests:
        if not isinstance(quest, dict):
            continue
        q_id = quest.get("QuestID", "Unknown")
        for scene in quest.get("Scenes", []):
            for dial in scene.get("Dials", []):
                for item in dial.get("Dialogues", []):
                    yield q_id, item
                    for choice in item.get("PlayerChoices", []):
                        yield q_id, choice
        for dial in quest.get("StandaloneDials", []):
            for item in dial.get("Dialogues", []):
                yield q_id, item
                for choice in item.get("PlayerChoices", []):
                    yield q_id, choice


def _expected_style(guide: str) -> str:
    if not guide:
        return ""
    if "반말" in guide:
        return "banmal"
    if "해요체" in guide:
        return "haeyo"
    if "하오체" in guide:
        return "hao"
    if "하십시오체" in guide or "격식" in guide:
        return "formal"
    return ""


def _looks_tone_risky(text: str, style: str) -> bool:
    if not text or not style:
        return False
    endings = text.strip()
    if style == "banmal":
        return any(token in endings for token in ["습니다", "습니까", "십시오", "세요", "해요", "예요", "이에요"])
    if style == "haeyo":
        return not any(token in endings for token in ["요", "죠", "네요"]) and any(token in endings for token in ["다.", "냐?", "라.", "군.", "지."])
    if style == "hao":
        return not any(token in endings for token in ["하오", "시오", "구려", "소.", "오."]) and any(token in endings for token in ["해요", "합니다", "했다", "해라"])
    if style == "formal":
        return not any(token in endings for token in ["습니다", "습니까", "십시오", "합니다", "됩니다"]) and any(token in endings for token in ["해요", "했다", "해라", "하오"])
    return False


def perform_scene_refine(input_json, output_json, profile_path, config):
    print("Mode: Scene Refine (화자별 JSON 어투 교정)")
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    profiles = {}
    if profile_path and os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        print(f"Loaded profile from {profile_path}")

    system_prompt = config.get(
        "step6_scene_refine_prompt",
        "당신은 게임 대사 로컬라이제이션 교정자입니다. 원문, 현재 번역, 화자 말투 가이드를 보고 의미와 태그를 보존하면서 말투만 자연스럽게 통일하세요."
    )
    config["step6_scene_refine_prompt"] = system_prompt
    backend = get_llm_backend(config, "step6_scene_refine_prompt", role="review")

    refined = 0
    checked = 0
    for q_id, item in _iter_scene_dialogues(data):
        src = item.get("Text", "")
        dst = item.get("Translate", "")
        speaker = item.get("Speaker", "Unknown")
        if not src or not dst or src == dst:
            continue
        if speaker == "Player":
            continue

        q_profile = profiles.get(q_id, {}) if isinstance(profiles, dict) else {}
        guide = q_profile.get("character_guidelines", {}).get(speaker, "")
        style = _expected_style(guide)
        if not _looks_tone_risky(dst, style):
            continue

        checked += 1
        prompt = (
            "아래 대사의 한국어 번역을 화자 말투 가이드에 맞게 교정하세요.\n"
            "의미, 고유명사, 변수, 태그는 보존하고 결과 JSON만 반환하세요.\n"
            f"화자: {speaker}\n"
            f"말투 가이드: {guide}\n"
            f"원문: {src}\n"
            f"현재 번역: {dst}\n"
            '형식: {"translation": "교정된 텍스트"}'
        )
        result = backend.generate_content(prompt, temperature=0.2)
        if not result:
            continue
        try:
            result = result.replace("```json", "").replace("```", "").strip()
            res_json = json.loads(result)
            fixed = res_json.get("translation", dst)
            if fixed and fixed != dst:
                item["Translate"] = fixed
                refined += 1
                print(f"[{q_id}/{speaker}] Refined.")
        except Exception as e:
            print(f"[{q_id}/{speaker}] JSON Parse failed: {e}")

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Scene refine checked={checked}, refined={refined}")

def main():
    parser = argparse.ArgumentParser(description="Step 6: Mod & DLC Translator / Text Refiner")
    parser.add_argument("--input-file", dest="input_file", default=None, help="Standardized Step 6 input path")
    parser.add_argument("--output-xml", dest="output_xml", default=None, help="Standardized Step 6 output XML path")
    parser.add_argument("--profile-json", dest="profile_json", default=None, help="Standardized profile JSON path")
    parser.add_argument("-i", "--input", required=False, help="Input XML or Strings file")
    parser.add_argument("-m", "--mode", choices=["refine", "update", "scene_refine"], required=True)
    parser.add_argument("--input-json", dest="input_json", default=None, help="Scene JSON input for scene_refine mode")
    parser.add_argument("--output-json", dest="output_json", default=None, help="Scene JSON output for scene_refine mode")
    parser.add_argument("-o", "--output", required=False, help="Target output XML file")
    parser.add_argument("-p", "--profile", help="(Refine Mode) JSON tone profile from Step 2")
    parser.add_argument("-r", "--reference", help="(Update Mode) Previous translated XML/Strings file")
    
    args = parser.parse_args()
    args.input = args.input_json or args.input_file or args.input
    args.output = args.output_json or args.output_xml or args.output
    args.profile = args.profile_json or args.profile
    if not args.input or not args.output:
        print("Error: input and output paths are required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR

    try:
        input_file = require_file(args.input, "input file")
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_MISSING

    config = load_config()

    if args.mode == "scene_refine":
        output_json = ensure_parent(args.output)
        perform_scene_refine(str(input_file), str(output_json), args.profile, config)
        print_ok(output_json)
        return EXIT_SUCCESS

    output_xml = ensure_parent(args.output)
    
    texts = extract_texts_from_file(str(input_file))
    print(f"Extracted {len(texts)} translation entries.")
    
    if args.mode == "refine":
        perform_refine(texts, args.profile, config)
    elif args.mode == "update":
        perform_update(texts, args.reference, config)
        
    print(f"Saving to {output_xml}...")
    if str(input_file).endswith('.xml'):
        tree = ET.parse(str(input_file))
        root = tree.getroot()
        
        # 위에서 정의한 것과 동일한 방식으로 엔트리 탐색
        entries = root.findall('.//Content[@id]')
        if not entries:
            entries = root.findall('.//String')
            
        for entry in entries:
            db_id = entry.get('id')
            if not db_id:
                edid = entry.find('EDID')
                rec = entry.find('REC')
                edid_val = edid.text if edid is not None else ""
                rec_val = rec.text if rec is not None else ""
                db_id = f"{edid_val}|{rec_val}" if edid_val or rec_val else None
            
            if not db_id: continue # 매칭 불가
            
            if db_id in texts and texts[db_id]["dest"]:
                dest = entry.find('Dest')
                if dest is None:
                    dest = ET.SubElement(entry, 'Dest')
                dest.text = texts[db_id]["dest"]
                
        tree.write(str(output_xml), encoding="utf-8", xml_declaration=True)
    else:
        # Save straight to an XML standard format anyway for compatibility
        root = ET.Element("SSTXMLRsrc")
        for db_id, data in texts.items():
            content = ET.SubElement(root, "Content", attrib={"id": db_id})
            src = ET.SubElement(content, "Source")
            src.text = data["source"]
            dst = ET.SubElement(content, "Dest")
            dst.text = data["dest"]
        tree = ET.ElementTree(root)
        tree.write(str(output_xml), encoding="utf-8", xml_declaration=True)
        
    print("Step 6 process completed.")
    print_ok(output_xml)
    return EXIT_SUCCESS

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
