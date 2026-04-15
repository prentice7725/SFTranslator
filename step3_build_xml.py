import os
import json
import argparse
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from xml.dom import minidom
from step0_extract_xml import EspParser, StringEntry, StringsLoader, write_xml, sanitize_xml_chars
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

def generate_text_hash(text: str) -> str:
    """원시 텍스트 식별을 위한 해시 키 생성. Step 2와 동일 로직이어야 함."""
    if not text:
        return "000000"
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:8]


def load_translation_map(trans_path: str) -> dict:
    """JSON 번역 파일을 로드하여 {key: translated_text} 딕셔너리로 반환."""
    translated_map = {}
    if not os.path.exists(trans_path):
        print(f"WARNING: Translation file not found: {trans_path}.")
        return translated_map

    with open(trans_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    # 평탄화된 과거 버전 (플랫 구조: {hash: translated_text}) 지원용
    if isinstance(raw_data, dict) and all(isinstance(v, str) for v in raw_data.values()):
        return raw_data

    # 새로운 구조 (QuestID -> Scenes -> Dials -> Dialogues 등 배열 기반) 지원
    def _add_translation(item):
        txt = item.get("Text", "")
        # Get translate text correctly; sometimes it is under Translate, sometimes handled otherwise.
        trans_text = item.get("Translate") or item.get("Translated")
        if not trans_text:
            # If "Translate" is not found, it might simply not be translated.
            return
            
        h_id = generate_text_hash(txt) if txt else "000000"
        translated_map[h_id] = trans_text

        s_id = item.get("StringID", "")
        if s_id and s_id != "000000":
            translated_map[s_id] = trans_text
            try:
                translated_map[f"{int(s_id, 16):06X}"] = trans_text
            except ValueError:
                pass
        if txt:
            translated_map[txt] = trans_text

    def _extract_translations_from_dialogues(dialogues):
        for item in dialogues:
            _add_translation(item)
            for choice in item.get("PlayerChoices", []):
                _add_translation(choice)

    # raw_data 타입에 따른 동적 파싱
    if isinstance(raw_data, list):
        # 배열 구조(현재 파이프라인의 완성형)
        for quest in raw_data:
            # Scenes -> Dials -> Dialogues
            for scene in quest.get("Scenes", []):
                for dial in scene.get("Dials", []):
                    _extract_translations_from_dialogues(dial.get("Dialogues", []))
                # 구버전 호환 (Scenes 안에 바로 dialogues가 있을 경우)
                if "dialogues" in scene:
                    _extract_translations_from_dialogues(scene["dialogues"])
            # StandaloneDials -> Dialogues
            for standalone in quest.get("StandaloneDials", []) + quest.get("Standalone", []):
                _extract_translations_from_dialogues(standalone.get("Dialogues", standalone.get("dialogues", [])))
                
    elif isinstance(raw_data, dict):
        # 구버전 딕셔너리 구조
        for d_key, groups_or_scenes in raw_data.items():
            if isinstance(groups_or_scenes, list):
                for group in groups_or_scenes:
                    if "dialogues" in group:
                        _extract_translations_from_dialogues(group["dialogues"])
                    elif "lines" in group:
                        _extract_translations_from_dialogues(group["lines"])
                    else:
                        _extract_translations_from_dialogues([group])

    return translated_map


def merge_json_into_xml(xml_path: str, translated_map: dict, output_xml: str):
    """
    기존 완성된 XML 파일을 읽어, JSON 번역 맵 기준으로 <Dest> 값만 업데이트합니다.
    매핑 우선순위: sID(StringID hex) > Source 텍스트 해시 > Source 텍스트 직접 비교
    """
    if not os.path.exists(xml_path):
        print(f"ERROR: XML file not found: {xml_path}")
        return

    # XML 파싱 (UTF-8-BOM 대응)
    with open(xml_path, 'r', encoding='utf-8-sig') as f:
        xml_content = f.read()

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        print(f"ERROR: Failed to parse XML: {e}")
        return

    content = root.find("Content")
    if content is None:
        print("ERROR: <Content> element not found in XML.")
        return

    strings = content.findall("String")
    total = len(strings)
    match_count = 0

    for s_node in strings:
        sID = s_node.get("sID", "")            # <String sID="XXXXXX">
        source_node = s_node.find("Source")
        dest_node   = s_node.find("Dest")

        if dest_node is None:
            continue

        source_text = source_node.text if source_node is not None else ""
        trans = None

        # 1순위: sID가 있으면 hex 키로 lookup
        if sID:
            trans = translated_map.get(sID)
            if trans is None:
                # 대소문자 정규화 시도
                trans = translated_map.get(sID.upper())

        # 2순위: Source 텍스트 해시로 lookup
        if trans is None and source_text:
            key_hash = generate_text_hash(source_text)
            trans = translated_map.get(key_hash)

        # 3순위: Source 텍스트 직접 비교 (fallback)
        if trans is None and source_text:
            trans = translated_map.get(source_text)

        if trans is not None:
            dest_node.text = sanitize_xml_chars(trans)
            match_count += 1

    print(f"Merged {match_count}/{total} strings from translation dictionary into XML.")

    # 다시 예쁘게 저장
    _write_xml_from_element(root, output_xml)
    print(f"Saved merged XML to {output_xml}")


def _write_xml_from_element(root: ET.Element, output_path: str):
    """ElementTree 루트 엘리먼트를 xTranslator 호환 XML 포맷으로 저장합니다."""
    raw_xml = ET.tostring(root, encoding='utf-8')
    try:
        xmlstr = minidom.parseString(raw_xml).toprettyxml(indent="  ")
        xmlstr = xmlstr.replace("'", "&apos;")
        xmlstr = re.sub(r'(?<=>)([^<]+)(?=<)', lambda m: m.group(0).replace('"', '&quot;'), xmlstr)
        lines = xmlstr.split('\n')
        if lines and lines[0].strip().startswith('<?xml'):
            lines = lines[1:]
        final_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     + '\n'.join(lines)).rstrip('\n')
    except Exception as e:
        print(f"  Warning: pretty-print failed ({e}). Writing raw XML.")
        final_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     + raw_xml.decode('utf-8', errors='replace'))

    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write(final_xml)


def main():
    parser = argparse.ArgumentParser(
        description="Step 3: ESM에서 XML 생성 또는 기존 XML에 JSON 번역을 머지합니다."
    )
    parser.add_argument("--input-esp", dest="input_esp", default=None)
    parser.add_argument("--base-xml", dest="base_xml", default=None)
    parser.add_argument("--input-json", dest="input_json", default=None)
    parser.add_argument("--output-xml", dest="output_xml", default=None)
    parser.add_argument("-i", "--input", default=None,
                        help="[ESM 모드] 원본 .esm 파일 경로")
    parser.add_argument("-x", "--merge-xml", default=None,
                        help="[XML 머지 모드] 기존 완성된 XML 파일 경로 (step4/5 결과물)")
    parser.add_argument("-t", "--translation", default=None,
                        help="번역 JSON 파일 경로 (step2 결과물, optional)")
    parser.add_argument("-o", "--output", default=None,
                        help="출력 XML 경로")
    parser.add_argument("--strings-dir", default=None,
                        help="[ESM 모드] .strings 파일 디렉토리 (optional)")
    parser.add_argument("--lang", default="en",
                        help="[ESM 모드] .strings 파일 언어 코드 (default: en)")
    parser.add_argument("--direct-build", action="store_true",
                        help="Step 2 번역 과정 없이 ESM/XML에서 직접 XML 생성")
    args = parser.parse_args()

    args.input = args.input_esp or args.input
    args.merge_xml = args.base_xml or args.merge_xml
    args.translation = args.input_json or args.translation
    args.output = args.output_xml or args.output

    # Step 3 is the bridge between scene translation and XML translation, so it
    # still carries both the standardized path and the older merge-only workflow.

    # ----------------------------------------------------------------
    # [XML 머지 모드] --merge-xml 지정 시: 기존 XML + JSON → XML 업데이트
    # ----------------------------------------------------------------
    if args.merge_xml:
        try:
            xml_path = str(require_file(args.merge_xml, "base XML"))
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INPUT_MISSING

        # 출력 경로: 지정 없으면 원본 XML 덮어쓰기
        if args.output:
            output_xml = str(ensure_parent(args.output))
        else:
            base, ext = os.path.splitext(xml_path)
            output_xml = xml_path  # 덮어쓰기 (백업 후)

        if args.direct_build:
            import shutil
            shutil.copyfile(xml_path, output_xml)
            print(f"[XML 머지 모드 - Direct Build]")
            print(f"  기존 XML 그대로 복사: {xml_path} -> {output_xml}")
            print_ok(output_xml)
            return EXIT_SUCCESS

        if not args.translation:
            if not args.direct_build:
                print("ERROR: --base-xml requires --input-json or --direct-build.", file=sys.stderr)
                return EXIT_ARGUMENT_ERROR

        try:
            trans_path = str(require_file(args.translation, "translation JSON"))
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INPUT_MISSING

        print(f"[XML 머지 모드]")
        print(f"  기존 XML  : {xml_path}")
        print(f"  번역 JSON : {trans_path}")
        print(f"  출력 XML  : {output_xml}")

        print("Loading translation JSON ...")
        translated_map = load_translation_map(trans_path)
        print(f" → {len(translated_map)} translation entries loaded.")

        merge_json_into_xml(xml_path, translated_map, output_xml)
        print("Done!")
        print_ok(output_xml)
        return EXIT_SUCCESS

    # ----------------------------------------------------------------
    # [ESM 모드] 기존 동작: ESM → XML 생성 (+ JSON 머지 선택사항)
    # ----------------------------------------------------------------
    if not args.input:
        print("ERROR: --input-esp or --base-xml is required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR

    try:
        input_path = str(require_file(args.input, "input ESM"))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INPUT_MISSING
    trans_path = str(require_file(args.translation, "translation JSON")) if args.translation else ""
    mod_stem = os.path.splitext(os.path.basename(input_path))[0]
    output_xml = str(ensure_parent(args.output)) if args.output else os.path.join(
        os.path.dirname(input_path), f"{mod_stem}_Translated.xml"
    )

    # 1. 번역된 딕셔너리 로드
    translated_map = {}
    if args.translation:
        print("Loading translation JSON ...")
        translated_map = load_translation_map(trans_path)
        print(f" → {len(translated_map)} translation entries loaded.")

    input_dir = os.path.dirname(input_path)
    strings_dir = args.strings_dir if args.strings_dir else input_dir

    loader = StringsLoader()
    found = loader.load(strings_dir, mod_stem, [args.lang])
    if not found:
        strings_subdir = os.path.join(input_dir, 'Strings')
        if os.path.isdir(strings_subdir):
            found = loader.load(strings_subdir, mod_stem, [args.lang])

    print(f"Parsing original ESM {mod_stem} with internal parser ...")
    esp = EspParser(input_path, strings_loader=(loader if found else None), lang=args.lang)
    esp.parse()

    entries = esp.entries
    print(f"Extracted {len(entries)} translatable string entries from ESM.")

    # Match translated entries in descending confidence order so broad text
    # fallbacks only run when stable identifiers are unavailable.
    # 2. 매핑 로직 (우선순위: StringID hex > 텍스트 해시 > 원문 직접 비교)
    match_count = 0
    for entry in entries:
        trans = None

        if entry.string_id > 0:
            key_hex = f"{entry.string_id:06X}"
            trans = translated_map.get(key_hex)

        if trans is None and entry.source_text:
            key_hash = generate_text_hash(entry.source_text)
            trans = translated_map.get(key_hash)

        if trans is None and entry.source_text:
            trans = translated_map.get(entry.source_text)

        if trans is not None:
            entry.dest_text = trans
            match_count += 1

    print(f"Mapped {match_count}/{len(entries)} strings from translation dictionary.")

    # 3. XML로 쓰기
    print(f"Exporting final XML to {output_xml}")
    write_xml(entries, output_xml, os.path.basename(input_path))
    print("Done!")
    print_ok(output_xml)
    return EXIT_SUCCESS


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
