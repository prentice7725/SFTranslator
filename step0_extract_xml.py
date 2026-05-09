"""
step0_extract_xml.py

이 스크립트는 베데스다 확장자 파일(ESM/ESP 등)을 파싱하여 xTranslator와 호환되는
SSTXMLRessources 형식의 XML 파일로 번역 대상 텍스트를 추출하는 역할을 수행합니다.
압축된 BA2 아카이브 및 다국어 지원 스트링 파일(.strings, .dlstrings, .ilstrings)로부터 
원본 텍스트를 읽어들이고, 이를 구조화하여 XML로 저장합니다.
"""
import struct
import os
import re
import sys
import zlib
import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, BinaryIO
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from pipeline_runner import (
    EXIT_ARGUMENT_ERROR,
    EXIT_INPUT_MISSING,
    EXIT_INTERNAL_ERROR,
    EXIT_OUTPUT_FAILURE,
    EXIT_SUCCESS,
    ensure_parent,
    print_ok,
    require_file,
)
from prd_contract import classify_translation, context_id, make_stable_id, source_hash

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HEADER_TES4 = b'TES4'
HEADER_GRUP = b'GRUP'

# -----------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------
@dataclass
class FieldData:
    """단일 데이터 필드(바이트 단위의 타입과 데이터)를 보관하는 데이터 클래스"""
    f_type: bytes
    f_data: bytes

@dataclass
class StringEntry:
    """추출된 번역 대상 문자열 세부정보와 식별용 메타데이터를 저장하는 구조체"""
    id: int
    form_id: int
    edid: str
    rec_type: str
    field_type: str
    source_text: str
    dest_text: str
    status: str = "Translated"
    list_id: str = "0"
    field_index: int = 0
    field_index_max: int = 0
    string_id: int = 0

# -----------------------------------------------------------------------
# Guard functions
# -----------------------------------------------------------------------
def proc1_gmst_data(fields: List[FieldData], _idx: int) -> bool:
    for fd in fields:
        if fd.f_type == b'EDID' and len(fd.f_data) > 0:
            return fd.f_data[0] == 115
    return False

def proc2_perk_epfd(fields: List[FieldData], current_idx: int) -> bool:
    for z, fd in enumerate(fields):
        if fd.f_type == b'EPFT' and len(fd.f_data) == 1 and fd.f_data[0] == 7:
            for j in range(z + 1, min(len(fields), z + 4)):
                if j == current_idx:
                    return True
    return False

def proc4_perk_epf2(fields: List[FieldData], current_idx: int) -> bool:
    for z, fd in enumerate(fields):
        if fd.f_type == b'EPFT' and len(fd.f_data) == 1 and fd.f_data[0] == 4:
            for j in range(z + 1, min(len(fields), z + 3)):
                if j == current_idx:
                    return True
    return False

def proc5_door_cnam(fields: List[FieldData], current_idx: int) -> bool:
    start = current_idx - 1
    stop  = max(-1, current_idx - 4)
    for j in range(start, stop, -1):
        if fields[j].f_type == b'BFCB':
            return False
        if fields[j].f_type == b'BFCE':
            break
    return True

def proc_all(_fields, _idx) -> bool:
    return True

# -----------------------------------------------------------------------
# REC_DEFS
# -----------------------------------------------------------------------
REC_DEFS = [
    (b'DNAM', b'MGEF', 0, False, proc_all),
    (b'NAM1', b'INFO', 2, False, proc_all),
    (b'SHRT', b'NPC_', 0, False, proc_all),
    (b'CNAM', b'QUST', 1, False, proc_all),
    (b'CNAM', b'BOOK', 1, False, proc_all),
    (b'TNAM', b'WOOP', 0, False, proc_all),
    (b'NNAM', b'QUST', 0, False, proc_all),
    (b'NNAM', b'MESG', 0, False, proc_all),
    (b'ITXT', b'MESG', 0, False, proc_all),
    (b'RDMP', b'REGN', 0, False, proc_all),
    (b'RNAM', b'ACTI', 0, False, proc_all),
    (b'RNAM', b'FLOR', 0, False, proc_all),
    (b'RNAM', b'INFO', 0, False, proc_all),
    (b'BPTN', b'BPTD', 0, False, proc_all),
    (b'MNAM', b'FACT', 0, False, proc_all),
    (b'FNAM', b'FACT', 0, False, proc_all),
    (b'DESC', b'LSCR', 0, False, proc_all),
    (b'ONAM', b'AMMO', 0, False, proc_all),
    (b'ONAM', b'LVLI', 0, False, proc_all),
    (b'ANAM', b'AVIF', 0, False, proc_all),
    (b'WNAM', b'INNR', 0, False, proc_all),
    (b'FMRN', b'RACE', 0, False, proc_all),
    (b'BTXT', b'TERM', 0, False, proc_all),
    (b'ITXT', b'TERM', 0, False, proc_all),
    (b'RNAM', b'TERM', 0, False, proc_all),
    (b'UNAM', b'TERM', 0, False, proc_all),
    (b'WNAM', b'TERM', 0, False, proc_all),
    (b'DNAM', b'ALCH', 0, False, proc_all),
    (b'ONAM', b'DOOR', 0, False, proc_all),
    (b'TTGP', b'RACE', 0, False, proc_all),
    (b'MPPN', b'RACE', 0, False, proc_all),
    (b'NAM0', b'TERM', 0, False, proc_all),
    (b'SNAM', b'RACE', 0, False, proc_all),
    (b'NNAM', b'ENTM', 0, False, proc_all),
    (b'HNAM', b'COBJ', 0, False, proc_all),
    (b'SNAM', b'CNCY', 0, False, proc_all),
    (b'ONAM', b'LVLN', 0, False, proc_all),
    (b'NNAM', b'COEN', 0, False, proc_all),
    (b'LSST', b'LSCR', 0, False, proc_all),
    (b'DATA', b'GMST', 0, False, proc1_gmst_data),
    (b'EPFD', b'PERK', 0, False, proc2_perk_epfd),
    (b'EPF2', b'PERK', 0, False, proc4_perk_epf2),
    (b'CNAM', b'DOOR', 0, False, proc5_door_cnam),
    (b'BTXT', b'TMLM', 0, False, proc_all),
    (b'UNAM', b'TMLM', 0, False, proc_all),
    (b'ITXT', b'TMLM', 0, False, proc_all),
    (b'INAM', b'TMLM', 0, False, proc_all),
    (b'ISTX', b'TMLM', 0, False, proc_all),
    (b'LNAM', b'NPC_', 0, False, proc_all),
    (b'HULL', b'GBFM', 0, False, proc_all),
    (b'QMDP', b'QUST', 0, False, proc_all),
    (b'QMDT', b'QUST', 0, False, proc_all),
    (b'QMDS', b'QUST', 0, False, proc_all),
    (b'ENAM', b'BOOK', 0, False, proc_all),
    (b'FNAM', b'BOOK', 0, False, proc_all),
    (b'WABB', b'WEAP', 0, False, proc_all),
    (b'UNAM', b'REFR', 0, False, proc_all),
    (b'FDSL', b'RACE', 0, False, proc_all),
    (b'NNAM', b'IRES', 0, False, proc_all),
    (b'NNAM', b'MISC', 0, False, proc_all),
    (b'QMSU', b'QUST', 0, False, proc_all),
    (b'VOVS', b'GPOF', 0, False, proc_all),
    (b'RESN', b'GPOF', 0, False, proc_all),
    (b'NNAM', b'GPOF', 0, False, proc_all),
    (b'DNAM', b'GPOF', 0, False, proc_all),
    (b'NNAM', b'GPOG', 0, False, proc_all),
    (b'FULL', b'IMAD', 0, True,  proc_all),
    (b'FULL', None,   0, False, proc_all),
    (b'DESC', None,   1, False, proc_all),
    (b'ATTX', None,   0, False, proc_all),
]

def is_translatable(rec_type: bytes, f_type: bytes,
                    fields: List[FieldData], current_idx: int
                    ) -> Tuple[bool, str]:
    """
    입력된 레코드와 필드가 실제 번역이 필요한 항목인지 검사합니다.
    미리 정의된 REC_DEFS 테이블과 가드(Guard) 함수를 기반으로 판별합니다.
    반환: (번역 가능 여부, 사용될 스트링 리스트 ID)
    """
    if rec_type == HEADER_TES4:
        if f_type in (b'CNAM', b'SNAM'):
            return True, "0"
        return False, "0"

    for (def_field, def_rec, def_list, ignored, proc_fn) in REC_DEFS:
        if f_type != def_field:
            continue
        if def_rec is None or def_rec == rec_type:
            if not proc_fn(fields, current_idx):
                continue
            if ignored:
                return False, "0"
            return True, str(def_list)

    return False, "0"

# -----------------------------------------------------------------------
# StringsLoader
# -----------------------------------------------------------------------
class StringsLoader:
    """
    다국어(Localized) 문맥에서 .strings, .dlstrings, .ilstrings 파일 혹은 
    압축된 BA2 아카이브로부터 문자열 값을 메모리에 읽어들이는 클래스입니다.
    """
    def __init__(self):
        # 언어별 테이블 저장: self.tables[lang][table_idx] = {id: text}
        self.tables: Dict[str, List[dict]] = {}

    def load(self, strings_dir: str, mod_stem: str, langs: List[str] = ['en']) -> bool:
        exts = ['.strings', '.dlstrings', '.ilstrings']
        table_types = ['strings', 'dlstrings', 'ilstrings']
        found_any_global = False

        for lang in langs:
            lang = lang.lower()
            if lang not in self.tables:
                self.tables[lang] = [{}, {}, {}]
            
            found_any_lang = False
            for i, ext in enumerate(exts):
                path = os.path.join(strings_dir, f'{mod_stem}_{lang}{ext}')
                if os.path.exists(path):
                    self.tables[lang][i] = self._parse_file(path, table_types[i])
                    found_any_lang = True
                    found_any_global = True
                    print(f"  ✓ Strings loaded ({lang}): {os.path.basename(path)}")

            if not found_any_lang:
                ba2_path = self._find_ba2(strings_dir, mod_stem)
                if ba2_path:
                    print(f"  Strings for {lang} not found → trying BA2: {os.path.basename(ba2_path)}")
                    if self._load_from_ba2(ba2_path, mod_stem, lang):
                        found_any_global = True

        return found_any_global

    def _find_ba2(self, directory: str, mod_stem: str) -> Optional[str]:
        candidates = [
            f"{mod_stem} - Localization.ba2",
            f"{mod_stem}-Localization.ba2",
            f"{mod_stem} - Main.ba2",
            f"{mod_stem} - Sounds.ba2",
            f"{mod_stem} - Voices_en.ba2",
            f"{mod_stem} - Voices_ko.ba2",
            f"{mod_stem}.ba2",
        ]
        for candidate in candidates:
            path = os.path.join(directory, candidate)
            if os.path.exists(path):
                return path
        return None

    def _load_from_ba2(self, ba2_path: str, mod_stem: str, lang: str = 'en') -> bool:
        try:
            with open(ba2_path, 'rb') as f:
                raw = f.read()
            if raw[0:4] != b'BTDX':
                print("  ✗ Not a valid BA2 file")
                return False

            version = struct.unpack('<I', raw[4:8])[0]
            header_size = 32 if version >= 2 else 24
            file_count = struct.unpack('<I', raw[12:16])[0]
            str_offset = struct.unpack('<Q', raw[16:24])[0] # uint64

            # Filenames read
            filenames = []
            pos = str_offset
            for _ in range(file_count):
                if pos + 2 > len(raw): break
                name_len = struct.unpack('<H', raw[pos:pos+2])[0]
                pos += 2
                name = raw[pos:pos+name_len].decode('utf-8', errors='ignore')
                pos += name_len
                filenames.append(name)

            table_map = {'strings': 0, 'dlstrings': 1, 'ilstrings': 2}
            found_any = False
            ENTRY_SIZE = 36
            entry_offset = header_size

            entries = []
            for i in range(file_count):
                if entry_offset + ENTRY_SIZE > len(raw):
                    break
                # Starfield (V2) & FO4 GNRL Entry
                # 0: name_hash(4), 4: ext(4), 8: dir(4), 12: flags(4), 16: offset(8), 24: packed(4), 28: unpacked(4), 32: unknown(4)
                data_offset   = struct.unpack('<Q', raw[entry_offset+16:entry_offset+24])[0]
                packed_size   = struct.unpack('<I', raw[entry_offset+24:entry_offset+28])[0]
                unpacked_size = struct.unpack('<I', raw[entry_offset+28:entry_offset+32])[0]
                entries.append({
                    'data_offset': data_offset,
                    'packed_size': packed_size,
                    'unpacked_size': unpacked_size,
                })
                entry_offset += ENTRY_SIZE

            mod_stem_lower = mod_stem.lower()

            for i, entry in enumerate(entries):
                filename = filenames[i] if i < len(filenames) else ''
                fname_only = filename.lower().replace('\\', '/').split('/')[-1]

                for ext_name, table_idx in table_map.items():
                    pattern = f'{mod_stem_lower}_{lang}.{ext_name}'
                    if fname_only == pattern:
                        offset   = entry['data_offset']
                        packed   = entry['packed_size']
                        unpacked = entry['unpacked_size']
                        print(f"  Matched: {filename} | offset={offset} packed={packed} unpacked={unpacked}")
                        if packed == 0:
                            data = raw[offset:offset + unpacked]
                        else:
                            compressed = raw[offset:offset + packed]
                            data = None
                            for wbits in [-15, 15, 47]:
                                try:
                                    data = zlib.decompress(compressed, wbits=wbits)
                                    break
                                except zlib.error:
                                    continue
                            if data is None:
                                print(f"  ✗ Decompress failed for {filename}")
                                continue
                        print(f"  ✓ Extracted: {filename} ({len(data):,} bytes)")
                        if data:
                            self.tables[lang][table_idx] = self._parse_bytes(data, ext_name)
                            found_any = True
                        break

            return found_any

        except Exception as e:
            print(f"  ✗ BA2 load failed ({lang}): {e}")
            import traceback
            traceback.print_exc()
            return False

    def _parse_file(self, path: str, file_type: str = 'strings') -> dict:
        with open(path, 'rb') as f:
            return self._parse_bytes(f.read(), file_type)

    def _parse_bytes(self, raw: bytes, file_type: str = 'strings') -> dict:
        result = {}
        if len(raw) < 8:
            return result

        count = struct.unpack('<I', raw[0:4])[0]
        data_size = struct.unpack('<I', raw[4:8])[0]
        dir_size = count * 8
        data_base = 8 + dir_size

        for k in range(count):
            off = 8 + k * 8
            if off + 8 > len(raw):
                break
            
            sid = struct.unpack('<I', raw[off:off+4])[0]
            
            if file_type == 'strings':
                offset = struct.unpack('<I', raw[off+4:off+8])[0]
                abs_ = data_base + offset
                end = raw.find(b'\x00', abs_)
                if end == -1:
                    end = len(raw)
                byte_str = raw[abs_:end]
                
            else: 
                offset = struct.unpack('<I', raw[off+4:off+8])[0]
                abs_ = data_base + offset
                if abs_ + 4 > len(raw):
                    break
                str_len = struct.unpack('<I', raw[abs_:abs_+4])[0]
                if abs_ + 4 + str_len > len(raw):
                    str_len = len(raw) - (abs_ + 4)
                byte_str = raw[abs_+4 : abs_+4+str_len].strip(b'\x00')
            
            try:
                text = byte_str.decode('utf-8')
            except UnicodeDecodeError:
                text = byte_str.decode('cp1252', errors='replace')
                
            result[sid] = text
            
        return result

    def lookup(self, string_id: int, list_id: int, lang: str = 'en') -> Optional[str]:
        lang = lang.lower()
        if lang not in self.tables:
            return None
        if list_id < 0 or list_id > 2:
            list_id = 0
        return self.tables[lang][list_id].get(string_id)

# -----------------------------------------------------------------------
# String decoder
# -----------------------------------------------------------------------
def decode_string(data: bytes) -> str:
    """
    바이트 배열(bytes)을 전달받아 널 문자(null-terminated)를 제거한 뒤,
    UTF-8 혹은 CP1252(유럽어/서구권 인코딩)로 디코딩하여 파이썬 문자열(str)로 변환합니다.
    """
    if not data:
        return ""
    end = len(data)
    while end > 0 and data[end - 1] == 0:
        end -= 1
    data = data[:end]
    if not data:
        return ""
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return data.decode('cp1252')
        except Exception:
            return ""

# -----------------------------------------------------------------------
# EspParser
# -----------------------------------------------------------------------
class EspParser:
    """
    바이너리 형태의 ESP/ESM 플러그인 데이터를 순차적으로 파싱하고, 
    그룹(Group)과 레코드(Record) 계층을 탐색하여 번역 텍스트를 추출하는 파서(Parser) 클래스.
    """
    def __init__(self, file_path: str, strings_loader: Optional[StringsLoader] = None, use_ja_ref: bool = False, lang: str = 'en'):
        self.file_path = file_path
        self.mod_stem = os.path.splitext(os.path.basename(file_path))[0]
        self.entries: List[StringEntry] = []
        self._id_counter = 0
        self.is_localized = False
        self.strings_loader = strings_loader
        self.use_ja_ref = use_ja_ref
        self.lang = lang

        from db_manager import DBRAG
        self.db_rag = DBRAG()

    def parse(self):
        with open(self.file_path, 'rb') as f:
            self._read_chunk(f)

    def _read_chunk(self, f: BinaryIO, end_offset: int = -1):
        while True:
            if end_offset != -1 and f.tell() >= end_offset:
                break
            type_bytes = f.read(4)
            if not type_bytes or len(type_bytes) < 4:
                if end_offset == -1:
                    break
                raise EOFError("Unexpected EOF reading type")
            size_bytes = f.read(4)
            if len(size_bytes) < 4:
                raise EOFError("Unexpected EOF reading size")
            data_size = struct.unpack('<I', size_bytes)[0]
            if type_bytes == HEADER_GRUP:
                self._read_grup(f, data_size)
            else:
                self._read_record(f, type_bytes, data_size)

    def _read_grup(self, f: BinaryIO, size: int):
        f.read(16)
        group_end = f.tell() + (size - 24)
        self._read_chunk(f, group_end)

    def _read_record(self, f: BinaryIO, rec_type: bytes, data_size: int):
        flags_bytes  = f.read(4)
        formid_bytes = f.read(4)
        f.read(8)
        flags   = struct.unpack('<I', flags_bytes)[0]
        form_id = struct.unpack('<I', formid_bytes)[0]

        if rec_type == HEADER_TES4:
            self.is_localized = bool(flags & 0x80)

        is_compressed = bool(flags & 0x00040000)

        if is_compressed:
            if data_size < 4:
                f.read(data_size)
                return
            _decomp_size = struct.unpack('<I', f.read(4))[0]
            compressed   = f.read(data_size - 4)
            try:
                record_data = zlib.decompress(compressed)
            except zlib.error:
                record_data = b''
        else:
            record_data = f.read(data_size)

        if self.is_localized and rec_type == HEADER_TES4:
            return

        self._parse_record_fields(rec_type, form_id, record_data)

    def _parse_record_fields(self, rec_type: bytes, form_id: int, data: bytes):
        rec_str = rec_type.decode('ascii', errors='replace')
        raw_fields: List[FieldData] = []
        offset = 0
        length = len(data)
        next_field_size = 0

        while offset < length:
            if offset + 6 > length:
                break
            f_type = data[offset:offset+4]
            f_size = struct.unpack('<H', data[offset+4:offset+6])[0]
            offset += 6
            actual_size = next_field_size if next_field_size else f_size
            next_field_size = 0
            if offset + actual_size > length:
                break
            f_data = data[offset:offset + actual_size]
            offset += actual_size
            if f_type == b'XXXX':
                if len(f_data) >= 4:
                    next_field_size = struct.unpack('<I', f_data)[0]
                continue
            raw_fields.append(FieldData(f_type, f_data))

        edid_str = ""
        for fd in raw_fields:
            if fd.f_type == b'EDID':
                edid_str = decode_string(fd.f_data)
                break

        string_entries: List[StringEntry] = []
        seen_string_count: dict = {}

        for idx, fd in enumerate(raw_fields):
            is_str, list_id = is_translatable(rec_type, fd.f_type, raw_fields, idx)
            if not is_str:
                continue

            ft_str = fd.f_type.decode('ascii', errors='replace')

            if self.is_localized:
                if len(fd.f_data) != 4:
                    text = decode_string(fd.f_data)
                    if not text:
                        continue
                    string_id = 0
                else:
                    string_id = struct.unpack('<I', fd.f_data)[0]
                    if string_id == 0:
                        continue
                    if self.strings_loader:
                        actual = self.strings_loader.lookup(string_id, int(list_id), self.lang)
                        if actual is None:
                            text = f'<StringNotFound_PlaceHolder {string_id:06X}>'
                        elif actual == '':
                            continue
                        else:
                            text = actual

                        # 일본어 참조 모드일 경우 DB에 저장
                        if self.use_ja_ref and string_id > 0:
                            ja_text = self.strings_loader.lookup(string_id, int(list_id), "ja")
                            if ja_text:
                                self.db_rag.save_reference_string(self.mod_stem, string_id, "ja", ja_text)
                    else:
                        text = f'<StringNotFound_PlaceHolder {string_id:06X}>'
            else:
                text = decode_string(fd.f_data)
                if not text:
                    continue
                string_id = 0

            prev_count = seen_string_count.get(fd.f_type, 0)
            seen_string_count[fd.f_type] = prev_count + 1

            self._id_counter += 1
            entry = StringEntry(
                id=self._id_counter,
                form_id=form_id,
                edid=edid_str,
                rec_type=rec_str,
                field_type=ft_str,
                source_text=text,
                dest_text=text,
                list_id=list_id,
                field_index=prev_count,
                field_index_max=0,
                string_id=string_id,
            )
            string_entries.append(entry)

        from collections import defaultdict
        type_entries: dict = defaultdict(list)
        for e in string_entries:
            type_entries[e.field_type].append(e)

        for entries_of_type in type_entries.values():
            if len(entries_of_type) > 1:
                max_idx = len(entries_of_type) - 1
                for e in entries_of_type:
                    e.field_index_max = max_idx

        self.entries.extend(string_entries)

# -----------------------------------------------------------------------
# 헤더 전용 문자열 필터링 - 새로 추가!
# -----------------------------------------------------------------------
def has_meaningful_content(entries: List[StringEntry]) -> bool:
    """
    TES4:CNAM/SNAM (헤더 작성자/설명) 이외의 실제 번역 대상 문자열이 있는지 확인
    """
    for entry in entries:
        # TES4 헤더가 아닌 실제 레코드가 하나라도 있으면 OK
        if entry.rec_type != "TES4":
            return True
    return False

# -----------------------------------------------------------------------
# XML helpers
# -----------------------------------------------------------------------
def sanitize_xml_chars(text: str) -> str:
    if not text:
        return text
    return "".join(c for c in text if (
        0x20 <= ord(c) <= 0xD7FF or
        c in ('\t', '\n', '\r') or
        0xE000 <= ord(c) <= 0xFFFD or
        0x10000 <= ord(c) <= 0x10FFFF
    ))

def write_xml(entries: List[StringEntry], output_path: str, addon_name: str):
    """
    추출된 StringEntry 요소들을 바탕으로, xTranslator와 호환되는
    SSTXMLRessources 형식의 XML 문서를 생성하고 디스크에 기록합니다.
    """
    root = ET.Element("SSTXMLRessources")
    plugin_stem = os.path.splitext(addon_name)[0]
    params = ET.SubElement(root, "Params")
    ET.SubElement(params, "Addon").text = addon_name
    ET.SubElement(params, "Source").text = "en"
    ET.SubElement(params, "Dest").text = "ko"
    ET.SubElement(params, "Version").text = "2"
    content = ET.SubElement(root, "Content")

    for entry in entries:
        s_node = ET.SubElement(content, "String")
        s_node.set("List", entry.list_id)
        sid = make_stable_id(
            plugin_stem,
            f"{entry.form_id:08X}",
            entry.rec_type,
            entry.field_type,
            entry.field_index,
            entry.source_text,
        )
        s_node.set("stable_id", sid)
        s_node.set("source_hash", source_hash(entry.source_text))
        s_node.set("context_id", context_id(f"{entry.form_id:08X}", entry.rec_type, entry.field_type, entry.field_index))
        s_node.set("translation_class", classify_translation(entry.source_text, entry.rec_type, entry.field_type))
        if entry.string_id > 0:
            s_node.set("sID", f'{entry.string_id:06X}')
        edid_text = entry.edid if entry.edid else f"[{entry.form_id:08X}]"
        ET.SubElement(s_node, "EDID").text = sanitize_xml_chars(edid_text)
        rec_node = ET.SubElement(s_node, "REC")
        rec_node.text = f"{entry.rec_type}:{entry.field_type}"
        if entry.field_index_max > 0:
            rec_node.set("id",    str(entry.field_index))
            rec_node.set("idMax", str(entry.field_index_max))
        ET.SubElement(s_node, "Source").text = sanitize_xml_chars(entry.source_text)
        ET.SubElement(s_node, "Dest").text   = sanitize_xml_chars(entry.dest_text)

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

# -----------------------------------------------------------------------
# 로그 기록용 dataclass
# -----------------------------------------------------------------------
@dataclass
class ProcessResult:
    input_path: str
    status: str           # "OK" | "NO_STRINGS" | "HEADER_ONLY" | "ERROR"
    string_count: int
    xml_path: str
    error_msg: str

# -----------------------------------------------------------------------
# 로그 파일 저장
# -----------------------------------------------------------------------
def write_log(results: List[ProcessResult], log_path: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total   = len(results)
    ok      = sum(1 for r in results if r.status == "OK")
    no_str  = sum(1 for r in results if r.status == "NO_STRINGS")
    header  = sum(1 for r in results if r.status == "HEADER_ONLY")
    errors  = sum(1 for r in results if r.status == "ERROR")

    lines = []
    lines.append("=" * 70)
    lines.append(f"  ESM Processing Log  |  {now_str}")
    lines.append("=" * 70)
    lines.append(f"  Total: {total}  |  OK: {ok}  |  Header only: {header}  |  No strings: {no_str}  |  Error: {errors}")
    lines.append("=" * 70)
    lines.append("")

    for r in results:
        if r.status == "OK":
            marker = "[OK      ]"
        elif r.status == "HEADER_ONLY":
            marker = "[HDR ONLY]"
        elif r.status == "NO_STRINGS":
            marker = "[NO STR  ]"
        else:
            marker = "[ERROR   ]"

        lines.append(f"{marker}  {r.input_path}")

        if r.status == "OK":
            lines.append(f"           Strings : {r.string_count}")
            lines.append(f"           XML     : {r.xml_path}")
        elif r.status == "HEADER_ONLY":
            lines.append(f"           Only TES4:CNAM/SNAM header found. XML not created.")
        elif r.status == "NO_STRINGS":
            lines.append(f"           No translatable strings found. XML not created.")
        elif r.status == "ERROR":
            lines.append(f"           Error   : {r.error_msg}")

        lines.append("")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n✓ Log saved: {log_path}")

# -----------------------------------------------------------------------
# 개별 ESM 처리
# -----------------------------------------------------------------------
def process_single_esm(input_path: str,
                       output_dir: Optional[str],
                       strings_dir: Optional[str],
                       lang: str,
                       use_ja_ref: bool = False) -> ProcessResult:
    """
    단일 ESM/ESP 파일에 대한 분석/추출, 다국어 처리, XML 생성 및
    상태 리포트까지의 전체 흐름(파이프라인)을 처리합니다.
    """
    input_path = os.path.abspath(input_path)
    input_dir  = os.path.dirname(input_path)
    mod_stem   = os.path.splitext(os.path.basename(input_path))[0]

    if not os.path.exists(input_path):
        return ProcessResult(input_path, "ERROR", 0, "", "File not found")

    out_dir = os.path.abspath(output_dir) if output_dir else input_dir
    if not os.path.exists(out_dir):
        return ProcessResult(input_path, "ERROR", 0, "",
                             f"Output directory not found: {out_dir}")

    output_file = os.path.join(out_dir, f"{mod_stem}.xml")

    loader = StringsLoader()
    search_dir = strings_dir if strings_dir else input_dir
    
    langs = [lang]
    if use_ja_ref and "ja" not in langs:
        langs.append("ja")

    found = loader.load(search_dir, mod_stem, langs)

    if not found and not strings_dir:
        strings_subdir = os.path.join(input_dir, 'Strings')
        if os.path.isdir(strings_subdir):
            found = loader.load(strings_subdir, mod_stem, langs)

    strings_loader = loader if found else None

    print(f"  Parsing {input_path} ...")
    esp = EspParser(input_path, strings_loader=strings_loader, use_ja_ref=use_ja_ref, lang=lang)
    try:
        esp.parse()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ProcessResult(input_path, "ERROR", 0, "", str(e))

    localized_note = " [LOCALIZED]" if esp.is_localized else ""
    count = len(esp.entries)
    print("  Found {} translatable strings.{}".format(count, localized_note))

    if count == 0:
        print("  ✗ No translatable strings. XML not created.")
        return ProcessResult(input_path, "NO_STRINGS", 0, "", "")

    # 새로 추가: 헤더만 있는지 체크
    if not has_meaningful_content(esp.entries):
        print(f"  ✗ Only TES4 header fields (CNAM/SNAM). XML not created.")
        return ProcessResult(input_path, "HEADER_ONLY", count, "", "")

    print(f"  Exporting → {output_file}")
    write_xml(esp.entries, output_file, os.path.basename(input_path))
    print(f"  ✓ Done.")
    return ProcessResult(input_path, "OK", count, output_file, "")

# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------
def main():
    """
    통합 스크립트의 진입점으로, 커맨드라인 파라미터를 파싱하고
    단일 파일 혹은 디렉토리 모드에 맞추어 작업을 분배/실행한 뒤 총괄 로그를 남깁니다.
    """
    parser = argparse.ArgumentParser(
        description="Parse ESM/ESP and export xTranslator-compatible XML.")
    parser.add_argument("--input-esp", dest="input_esp", default=None,
                        help="Standardized single-file input ESM/ESP path.")
    parser.add_argument("--output-xml", dest="output_xml", default=None,
                        help="Standardized single-file output XML path.")
    parser.add_argument("-i", "--input",
                        required=False,
                        help="Path to input ESM/ESP file OR directory (recursive).")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Output directory for XML files. Default: same folder as each ESM.")
    parser.add_argument("--strings-dir", default=None,
                        help="Directory containing .strings files (optional).")
    parser.add_argument("--lang", default="en",
                        help="Language code for .strings filenames (default: en).")
    parser.add_argument("--use-ja-ref", action="store_true",
                        help="일본어 원문 참조 모드 활성 (공식 DLC/모드 등 일본어 존재 시)")
    parser.add_argument("--log-file", default=None,
                        help="Path for the processing log TXT file. "
                             "Default: processed_log_[datetime].txt in the output/current directory.")
    args = parser.parse_args()

    if args.input_esp:
        args.input = args.input_esp

    if not args.input:
        print("Error: input ESM/ESP path is required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR

    if args.output_xml:
        try:
            input_path = require_file(args.input, "input")
            output_xml = ensure_parent(args.output_xml)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_INPUT_MISSING

        try:
            result = process_single_esm(
                str(input_path),
                str(output_xml.parent),
                args.strings_dir,
                args.lang,
                use_ja_ref=args.use_ja_ref,
            )
            if result.status != "OK" or not result.xml_path:
                print(f"Error: XML export failed ({result.status}).", file=sys.stderr)
                return EXIT_OUTPUT_FAILURE
            generated_xml = os.path.abspath(result.xml_path)
            if os.path.abspath(str(output_xml)) != generated_xml:
                os.replace(generated_xml, str(output_xml))
            print_ok(output_xml)
            return EXIT_SUCCESS
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

    in_path = os.path.abspath(args.input)
    results: List[ProcessResult] = []

    if os.path.isfile(in_path):
        print(f"[Single file mode]")
        result = process_single_esm(in_path, args.output_dir, args.strings_dir, args.lang, use_ja_ref=args.use_ja_ref)
        results.append(result)

    elif os.path.isdir(in_path):
        esm_files = []
        for root, dirs, files in os.walk(in_path):
            for name in sorted(files):
                if name.lower().endswith(".esm"):
                    esm_files.append(os.path.join(root, name))

        total = len(esm_files)
        if total == 0:
            print(f"No .esm files found in: {in_path}")
            return EXIT_INPUT_MISSING

        print(f"[Directory mode] Found {total} ESM file(s) in: {in_path}")
        for i, esm_p in enumerate(esm_files):
            print(f"\n({i+1}/{total}) Processing: {esm_p}")
            res = process_single_esm(esm_p, args.output_dir, args.strings_dir, args.lang, use_ja_ref=args.use_ja_ref)
            results.append(res)
        for idx, esm_path in enumerate(esm_files, 1):
            print(f"\n[{idx}/{total}] {os.path.basename(esm_path)}")
            print("-" * 60)
            result = process_single_esm(esm_path, args.output_dir, args.strings_dir, args.lang)
            results.append(result)

    else:
        print(f"Error: '{in_path}' is neither a file nor a directory.")
        return EXIT_INPUT_MISSING

    if args.log_file:
        log_path = os.path.abspath(args.log_file)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_name  = f"processed_log_{timestamp}.txt"
        log_dir   = os.path.abspath(args.output_dir) if args.output_dir else os.getcwd()
        log_path  = os.path.join(log_dir, log_name)

    write_log(results, log_path)

    ok      = sum(1 for r in results if r.status == "OK")
    header  = sum(1 for r in results if r.status == "HEADER_ONLY")
    no_str  = sum(1 for r in results if r.status == "NO_STRINGS")
    errors  = sum(1 for r in results if r.status == "ERROR")
    print(f"\n{'=' * 60}")
    print(f"  Result  →  OK: {ok}  |  Header only: {header}  |  No strings: {no_str}  |  Error: {errors}")
    print(f"{'=' * 60}")

    return EXIT_SUCCESS if errors == 0 else EXIT_INTERNAL_ERROR

if __name__ == "__main__":
    sys.exit(main())
