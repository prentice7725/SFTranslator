import os
import sys
import struct
import zlib
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from pipeline_runner import (
    EXIT_ARGUMENT_ERROR,
    EXIT_INPUT_MISSING,
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    ensure_parent,
    print_ok,
    require_file,
)
from step0_extract_xml import is_translatable, FieldData, decode_string

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =========================================================================
# XML Translation Loader
# =========================================================================
class TranslationDB:
    def __init__(self):
        self.list0_map: Dict[int, str] = {} # strings
        self.list1_map: Dict[int, str] = {} # dlstrings
        self.list2_map: Dict[int, str] = {} # ilstrings
        self.text_map: Dict[str, str] = {}  # Source -> Dest (for unlocalized)

    def load_xml(self, xml_path: str):
        print(f"Loading XML translations from {xml_path}...")
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        # 방식 1: <String List="0" sID="123456"><Source>...</Source><Dest>...</Dest></String>
        # xTranslator SSTXMLRessources 형식 대응
        for s_node in root.findall('.//String'):
            list_id = s_node.get('List', '0')
            sID = s_node.get('sID', '')
            source_elem = s_node.find('Source')
            dest_elem = s_node.find('Dest')
            
            src_text = source_elem.text if source_elem is not None else ""
            dst_text = dest_elem.text if dest_elem is not None else ""
            
            # Dest가 비어있으면 원문 유지
            if not dst_text:
                dst_text = src_text
                
            if src_text:
                self.text_map[src_text] = dst_text
                
            if sID and sID != "000000":
                try:
                    num_id = int(sID, 16)
                    if list_id == '0':
                        self.list0_map[num_id] = dst_text
                    elif list_id == '1':
                        self.list1_map[num_id] = dst_text
                    elif list_id == '2':
                        self.list2_map[num_id] = dst_text
                except ValueError:
                    pass
                    
        print(f" Loaded {len(self.list0_map)} list0, {len(self.list1_map)} list1, {len(self.list2_map)} list2, {len(self.text_map)} text maps.")

# =========================================================================
# ESM AST Nodes
# =========================================================================
class EsmNode:
    def serialize(self) -> bytes:
        raise NotImplementedError

class Field(EsmNode):
    def __init__(self, f_type: bytes, f_data: bytes):
        self.f_type = f_type
        self.f_data = f_data

    def serialize(self) -> bytes:
        data_len = len(self.f_data)
        if data_len > 65535:
            xxxx_data = struct.pack('<I', data_len)
            xxxx_field = b'XXXX' + struct.pack('<H', 4) + xxxx_data
            actual_field = self.f_type + struct.pack('<H', 0) + self.f_data
            return xxxx_field + actual_field
        else:
            return self.f_type + struct.pack('<H', data_len) + self.f_data

class Record(EsmNode):
    def __init__(self, r_type: bytes, flags: int, form_id: int, extra_header: bytes, fields: List[Field]):
        self.r_type = r_type
        self.flags = flags
        self.form_id = form_id
        self.extra_header = extra_header # 8 bytes
        self.fields = fields

    def serialize(self) -> bytes:
        fields_data = b''.join(f.serialize() for f in self.fields)
        
        is_compressed = bool(self.flags & 0x00040000)
        if is_compressed:
            uncompressed_size = len(fields_data)
            comp_data = zlib.compress(fields_data)
            data = struct.pack('<I', uncompressed_size) + comp_data
        else:
            data = fields_data
            
        header = self.r_type + struct.pack('<I', len(data)) + struct.pack('<I', self.flags) + struct.pack('<I', self.form_id) + self.extra_header
        return header + data

class Group(EsmNode):
    def __init__(self, group_header_data: bytes, children: List[EsmNode]):
        self.group_header_data = group_header_data # 16 bytes after GRUP and size
        self.children = children

    def serialize(self) -> bytes:
        children_data = b''.join(c.serialize() for c in self.children)
        total_size = 24 + len(children_data)
        header = b'GRUP' + struct.pack('<I', total_size) + self.group_header_data
        return header + children_data

# =========================================================================
# ESM AST Parser & Builder
# =========================================================================
class EsmAST:
    def __init__(self, file_path: str):
        self.file_path = file_path
        with open(file_path, 'rb') as f:
            self.data = f.read()
        self.offset = 0
        self.length = len(self.data)
        self.is_localized = False
        self.nodes: List[EsmNode] = []

    def parse(self):
        print("Parsing ESM into memory AST...")
        while self.offset < self.length:
            self.nodes.append(self._parse_node())
        print("Parsing complete.")

    def _parse_node(self) -> EsmNode:
        if self.offset + 8 > self.length:
            raise EOFError("Unexpected EOF reading node header")
            
        n_type = self.data[self.offset : self.offset+4]
        n_size = struct.unpack('<I', self.data[self.offset+4 : self.offset+8])[0]
        
        if n_type == b'GRUP':
            # size includes header (24)
            group_header_data = self.data[self.offset+8 : self.offset+24]
            self.offset += 24
            end_offset = self.offset - 24 + n_size
            children = []
            while self.offset < end_offset:
                children.append(self._parse_node())
            return Group(group_header_data, children)
        else:
            flags = struct.unpack('<I', self.data[self.offset+8 : self.offset+12])[0]
            form_id = struct.unpack('<I', self.data[self.offset+12 : self.offset+16])[0]
            extra_header = self.data[self.offset+16 : self.offset+24]
            self.offset += 24
            
            if n_type == b'TES4':
                self.is_localized = bool(flags & 0x80)

            is_compressed = bool(flags & 0x00040000)
            record_data = self.data[self.offset : self.offset+n_size]
            self.offset += n_size
            
            if is_compressed:
                if len(record_data) >= 4:
                    raw_fields_data = zlib.decompress(record_data[4:])
                else:
                    raw_fields_data = b''
            else:
                raw_fields_data = record_data
                
            fields = self._parse_fields(raw_fields_data)
            return Record(n_type, flags, form_id, extra_header, fields)

    def _parse_fields(self, data: bytes) -> List[Field]:
        fields = []
        off = 0
        length = len(data)
        next_field_size = 0
        while off < length:
            f_type = data[off : off+4]
            f_size = struct.unpack('<H', data[off+4 : off+6])[0]
            off += 6
            actual_size = next_field_size if next_field_size else f_size
            next_field_size = 0
            
            if off + actual_size > length:
                break
                
            f_data = data[off : off+actual_size]
            off += actual_size
            
            if f_type == b'XXXX':
                if len(f_data) >= 4:
                    next_field_size = struct.unpack('<I', f_data)[0]
                continue
            
            fields.append(Field(f_type, f_data))
        return fields

    def apply_unlocalized_translations(self, db: TranslationDB):
        print("Applying translations directly into ESM AST (Unlocalized Mode)...")
        self._traverse_apply(self.nodes, db)

    def _traverse_apply(self, nodes: List[EsmNode], db: TranslationDB):
        for node in nodes:
            if isinstance(node, Group):
                self._traverse_apply(node.children, db)
            elif isinstance(node, Record):
                # 헤더(TES4)이거나 로컬라이즈된 모드면 내부 스트링을 건드리지 않는다.
                if node.r_type == b'TES4':
                    continue
                
                # 검사를 위해 FieldData 형식으로 변환
                field_datas = [FieldData(f.f_type, f.f_data) for f in node.fields]
                
                for idx, f in enumerate(node.fields):
                    is_str, list_id = is_translatable(node.r_type, f.f_type, field_datas, idx)
                    if is_str:
                        source_text = decode_string(f.f_data)
                        if source_text and source_text in db.text_map:
                            dest_text = db.text_map[source_text]
                            if dest_text != source_text:
                                # 문자열 인코딩 (Null-terminated)
                                encoded = dest_text.encode('utf-8') + b'\x00'
                                f.f_data = encoded

    def serialize(self) -> bytes:
        print("Serializing AST back to ESM binary...")
        return b''.join(n.serialize() for n in self.nodes)


# =========================================================================
# Strings File Builder
# =========================================================================
def build_strings_file(entries_map: Dict[int, str], file_type: str) -> bytes:
    sorted_ids = sorted(entries_map.keys())
    count = len(sorted_ids)
    
    directory = bytearray()
    data_section = bytearray()
    
    for s_id in sorted_ids:
        text = entries_map[s_id]
        encoded = text.encode('utf-8')
        
        offset = len(data_section)
        directory.extend(struct.pack('<II', s_id, offset))
        
        if file_type == 'strings':
            data_section.extend(encoded + b'\x00')
        else:
            # dlstrings / ilstrings
            data_section.extend(struct.pack('<I', len(encoded) + 1))
            data_section.extend(encoded + b'\x00')
            
    data_size = len(data_section)
    header = struct.pack('<II', count, data_size)
    return header + directory + data_section


def create_strings_files(db: TranslationDB, mod_stem: str, output_dir: Path, lang: str = "ko"):
    strings_dir = output_dir / "Strings"
    strings_dir.mkdir(parents=True, exist_ok=True)
    
    files = {
        'strings': db.list0_map,
        'dlstrings': db.list1_map,
        'ilstrings': db.list2_map
    }
    
    created_any = False
    for ext, t_map in files.items():
        if t_map:
            data = build_strings_file(t_map, ext)
            out_path = strings_dir / f"{mod_stem}_{lang}.{ext}"
            with open(out_path, "wb") as f:
                f.write(data)
            print(f" ✓ Generated {out_path.name}")
            created_any = True
            
    if not created_any:
        print(" ! No string maps found for localized output.")


def _verify_strings_file(path: Path, file_type: str) -> dict:
    result = {"file": str(path), "row_count": 0, "empty_count": 0, "encoding_errors": 0}
    raw = path.read_bytes()
    if len(raw) < 8:
        result["encoding_errors"] = 1
        return result
    count, _data_size = struct.unpack("<II", raw[:8])
    result["row_count"] = count
    data_base = 8 + count * 8
    for idx in range(count):
        off = 8 + idx * 8
        if off + 8 > len(raw):
            result["encoding_errors"] += 1
            break
        offset = struct.unpack("<I", raw[off + 4 : off + 8])[0]
        abs_pos = data_base + offset
        try:
            if file_type == "strings":
                end = raw.find(b"\x00", abs_pos)
                end = len(raw) if end == -1 else end
                text = raw[abs_pos:end].decode("utf-8")
            else:
                if abs_pos + 4 > len(raw):
                    result["encoding_errors"] += 1
                    continue
                size = struct.unpack("<I", raw[abs_pos : abs_pos + 4])[0]
                text = raw[abs_pos + 4 : abs_pos + 4 + max(0, size - 1)].decode("utf-8")
            if text == "":
                result["empty_count"] += 1
        except Exception:
            result["encoding_errors"] += 1
    return result


def write_step7_verify(output_dir: Path, input_esp: Path, dest_esm: Path, lang: str) -> Path:
    strings_dir = output_dir / "Strings"
    string_reports = []
    if strings_dir.exists():
        for ext in ("strings", "dlstrings", "ilstrings"):
            path = strings_dir / f"{input_esp.stem}_{lang}.{ext}"
            if path.exists():
                string_reports.append(_verify_strings_file(path, ext))
    report = {
        "step": "step7_verify",
        "output_dir": str(output_dir),
        "esm_exists": dest_esm.exists(),
        "esm_size": dest_esm.stat().st_size if dest_esm.exists() else 0,
        "strings": string_reports,
        "row_count_total": sum(item["row_count"] for item in string_reports),
        "empty_string_total": sum(item["empty_count"] for item in string_reports),
        "encoding_error_total": sum(item["encoding_errors"] for item in string_reports),
        "backup_created": (output_dir / f"{input_esp.name}.bak").exists(),
        "dry_run_import": dest_esm.exists() and (dest_esm.stat().st_size > 0 if dest_esm.exists() else False),
    }
    verify_path = output_dir / f"{input_esp.stem}.step7.verify.json"
    verify_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return verify_path


# =========================================================================
# Main
# =========================================================================
def main():
    parser = argparse.ArgumentParser(description="Step 7: 번역된 XML을 ESM/Strings 파일에 파이썬 네이티브로 빌드합니다.")
    parser.add_argument("--input-esp", dest="input_esp", required=True, help="원본 ESM/ESP 경로")
    parser.add_argument("--input-xml", dest="input_xml", required=True, help="최종 번역 완료된 XML 경로")
    parser.add_argument("--output-dir", dest="output_dir", required=False, help="출력 파일이 저장될 디렉토리")
    parser.add_argument("--lang", default="ko", help="출력 언어 코드 (기본: ko)")
    parser.add_argument("--config", default="config.json", help="호환성 유지를 위한 더미 인자")
    
    args = parser.parse_args()

    try:
        input_esp = require_file(args.input_esp, "원본 ESM")
        input_xml = require_file(args.input_xml, "번역된 XML")
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INPUT_MISSING

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = input_esp.parent / f"{input_esp.stem}_Translated"
        
    os.makedirs(output_dir, exist_ok=True)

    db = TranslationDB()
    db.load_xml(str(input_xml))

    ast = EsmAST(str(input_esp))
    
    # 1. ESM 로드 및 Localized 판단
    ast.parse()
    
    if ast.is_localized:
        print("\n[*] Detected Localized ESM (TES4 Header).")
        print("[*] Generating .strings files directly without modifying ESM...")
        create_strings_files(db, input_esp.stem, output_dir, args.lang)
        
        # ESM 복사 (옵션)
        # 로컬라이즈 플러그인이면 ESM은 동일하게 유지되므로 원본을 함께 배포 폴더에 복사
        import shutil
        dest_esm = output_dir / input_esp.name
        shutil.copyfile(input_esp, dest_esm)
        shutil.copyfile(input_esp, output_dir / f"{input_esp.name}.bak")
        print(f" ✓ Copied original ESM to {dest_esm.name}")
        
    else:
        print("\n[*] Detected Unlocalized ESM.")
        print("[*] Injecting translations directly into ESM fields...")
        ast.apply_unlocalized_translations(db)
        
        # 새 ESM 저장
        new_binary = ast.serialize()
        dest_esm = output_dir / input_esp.name
        import shutil
        shutil.copyfile(input_esp, output_dir / f"{input_esp.name}.bak")
        with open(dest_esm, "wb") as f:
            f.write(new_binary)
        print(f" ✓ Saved patched ESM to {dest_esm.name}")

    verify_path = write_step7_verify(output_dir, input_esp, dest_esm, args.lang)
    print(f" ✓ Step7 verify report: {verify_path.name}")
    print(f"\n[OK] Step 7 빌드 완료! 결과물 폴더: {output_dir}")
    print_ok(output_dir)
    return EXIT_SUCCESS

if __name__ == "__main__":
    sys.exit(main())
