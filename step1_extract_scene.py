import argparse
import json
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

HEADER_TES4 = b"TES4"
HEADER_GRUP = b"GRUP"


@dataclass
class FieldData:
    f_type: bytes
    f_data: bytes


# -----------------------------------------------------------------------
# Guard functions (from xmlExportv2)
# -----------------------------------------------------------------------
def proc1_gmst_data(fields: List[FieldData], _idx: int) -> bool:
    for fd in fields:
        if fd.f_type == b"EDID" and len(fd.f_data) > 0:
            return fd.f_data[0] == 115
    return False


def proc2_perk_epfd(fields: List[FieldData], current_idx: int) -> bool:
    for z, fd in enumerate(fields):
        if fd.f_type == b"EPFT" and len(fd.f_data) == 1 and fd.f_data[0] == 7:
            for j in range(z + 1, min(len(fields), z + 4)):
                if j == current_idx:
                    return True
    return False


def proc4_perk_epf2(fields: List[FieldData], current_idx: int) -> bool:
    for z, fd in enumerate(fields):
        if fd.f_type == b"EPFT" and len(fd.f_data) == 1 and fd.f_data[0] == 4:
            for j in range(z + 1, min(len(fields), z + 3)):
                if j == current_idx:
                    return True
    return False


def proc5_door_cnam(fields: List[FieldData], current_idx: int) -> bool:
    start = current_idx - 1
    stop = max(-1, current_idx - 4)
    for j in range(start, stop, -1):
        if fields[j].f_type == b"BFCB":
            return False
        if fields[j].f_type == b"BFCE":
            break
    return True


def proc_all(_fields, _idx) -> bool:
    return True


# -----------------------------------------------------------------------
# REC_DEFS
# -----------------------------------------------------------------------
REC_DEFS = [
    (b"DNAM", b"MGEF", 0, False, proc_all),
    (b"NAM1", b"INFO", 2, False, proc_all),
    (b"SHRT", b"NPC_", 0, False, proc_all),
    (b"CNAM", b"QUST", 1, False, proc_all),
    (b"CNAM", b"BOOK", 1, False, proc_all),
    (b"TNAM", b"WOOP", 0, False, proc_all),
    (b"NNAM", b"QUST", 0, False, proc_all),
    (b"NNAM", b"MESG", 0, False, proc_all),
    (b"ITXT", b"MESG", 0, False, proc_all),
    (b"RDMP", b"REGN", 0, False, proc_all),
    (b"RNAM", b"ACTI", 0, False, proc_all),
    (b"RNAM", b"FLOR", 0, False, proc_all),
    (b"RNAM", b"INFO", 0, False, proc_all),
    (b"BPTN", b"BPTD", 0, False, proc_all),
    (b"MNAM", b"FACT", 0, False, proc_all),
    (b"FNAM", b"FACT", 0, False, proc_all),
    (b"DESC", b"LSCR", 0, False, proc_all),
    (b"ONAM", b"AMMO", 0, False, proc_all),
    (b"ONAM", b"LVLI", 0, False, proc_all),
    (b"ANAM", b"AVIF", 0, False, proc_all),
    (b"WNAM", b"INNR", 0, False, proc_all),
    (b"FMRN", b"RACE", 0, False, proc_all),
    (b"BTXT", b"TERM", 0, False, proc_all),
    (b"ITXT", b"TERM", 0, False, proc_all),
    (b"RNAM", b"TERM", 0, False, proc_all),
    (b"UNAM", b"TERM", 0, False, proc_all),
    (b"WNAM", b"TERM", 0, False, proc_all),
    (b"DNAM", b"ALCH", 0, False, proc_all),
    (b"ONAM", b"DOOR", 0, False, proc_all),
    (b"TTGP", b"RACE", 0, False, proc_all),
    (b"MPPN", b"RACE", 0, False, proc_all),
    (b"NAM0", b"TERM", 0, False, proc_all),
    (b"SNAM", b"RACE", 0, False, proc_all),
    (b"NNAM", b"ENTM", 0, False, proc_all),
    (b"HNAM", b"COBJ", 0, False, proc_all),
    (b"SNAM", b"CNCY", 0, False, proc_all),
    (b"ONAM", b"LVLN", 0, False, proc_all),
    (b"NNAM", b"COEN", 0, False, proc_all),
    (b"LSST", b"LSCR", 0, False, proc_all),
    (b"DATA", b"GMST", 0, False, proc1_gmst_data),
    (b"EPFD", b"PERK", 0, False, proc2_perk_epfd),
    (b"EPF2", b"PERK", 0, False, proc4_perk_epf2),
    (b"CNAM", b"DOOR", 0, False, proc5_door_cnam),
    (b"BTXT", b"TMLM", 0, False, proc_all),
    (b"UNAM", b"TMLM", 0, False, proc_all),
    (b"ITXT", b"TMLM", 0, False, proc_all),
    (b"INAM", b"TMLM", 0, False, proc_all),
    (b"ISTX", b"TMLM", 0, False, proc_all),
    (b"LNAM", b"NPC_", 0, False, proc_all),
    (b"HULL", b"GBFM", 0, False, proc_all),
    (b"QMDP", b"QUST", 0, False, proc_all),
    (b"QMDT", b"QUST", 0, False, proc_all),
    (b"QMDS", b"QUST", 0, False, proc_all),
    (b"ENAM", b"BOOK", 0, False, proc_all),
    (b"FNAM", b"BOOK", 0, False, proc_all),
    (b"WABB", b"WEAP", 0, False, proc_all),
    (b"UNAM", b"REFR", 0, False, proc_all),
    (b"FDSL", b"RACE", 0, False, proc_all),
    (b"NNAM", b"IRES", 0, False, proc_all),
    (b"NNAM", b"MISC", 0, False, proc_all),
    (b"QMSU", b"QUST", 0, False, proc_all),
    (b"VOVS", b"GPOF", 0, False, proc_all),
    (b"RESN", b"GPOF", 0, False, proc_all),
    (b"NNAM", b"GPOF", 0, False, proc_all),
    (b"DNAM", b"GPOF", 0, False, proc_all),
    (b"NNAM", b"GPOG", 0, False, proc_all),
    (b"FULL", b"IMAD", 0, True, proc_all),
    (b"FULL", None, 0, False, proc_all),
    (b"DESC", None, 1, False, proc_all),
    (b"ATTX", None, 0, False, proc_all),
]


def is_translatable(
    rec_type: bytes, f_type: bytes, fields: List[FieldData], current_idx: int
) -> Tuple[bool, str]:
    if rec_type == HEADER_TES4:
        if f_type in (b"CNAM", b"SNAM"):
            return True, "0"
        return False, "0"

    for def_field, def_rec, def_list, ignored, proc_fn in REC_DEFS:
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
# StringsLoader (xmlExportv2.py 에서 이식)
# -----------------------------------------------------------------------
class StringsLoader:
    def __init__(self):
        # 언어별 테이블 저장: self.tables[lang][table_idx] = {id: text}
        self.tables: Dict[str, List[dict]] = {}

    def load(self, strings_dir: str, mod_stem: str, langs: List[str] = ["en"]) -> bool:
        exts = [".strings", ".dlstrings", ".ilstrings"]
        table_types = ["strings", "dlstrings", "ilstrings"]
        found_any_global = False

        for lang in langs:
            lang = lang.lower()
            if lang not in self.tables:
                self.tables[lang] = [{}, {}, {}]
            
            found_any_lang = False
            for i, ext in enumerate(exts):
                path = os.path.join(strings_dir, f"{mod_stem}_{lang}{ext}")
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
            f"{mod_stem}.ba2",
        ]
        for candidate in candidates:
            path = os.path.join(directory, candidate)
            if os.path.exists(path):
                return path
        return None

    def _load_from_ba2(self, ba2_path: str, mod_stem: str, lang: str = "en") -> bool:
        try:
            with open(ba2_path, "rb") as f:
                raw = f.read()

            # 헤더 최소 크기 검사
            if len(raw) < 24:
                return False
            if raw[0:4] != b"BTDX":
                return False

            version = struct.unpack("<I", raw[4:8])[0]
            file_count = struct.unpack("<I", raw[12:16])[0]
            str_offset = struct.unpack("<Q", raw[16:24])[0]  # 8바이트로 정확히 읽기

            # 스타필드 BA2 버전에 따른 시작 지점(entry_offset) 설정
            if version == 1:
                entry_offset = 24
            elif version == 2 or version == 3:
                entry_offset = 32
            else:
                entry_offset = 24  # Fallback

            filenames = []
            pos = str_offset
            for _ in range(file_count):
                if pos + 2 > len(raw):
                    break
                name_len = struct.unpack("<H", raw[pos : pos + 2])[0]
                pos += 2
                name = raw[pos : pos + name_len].decode("utf-8", errors="ignore")
                pos += name_len
                filenames.append(name)

            table_map = {"strings": 0, "dlstrings": 1, "ilstrings": 2}
            found_any = False
            ENTRY_SIZE = 36
            entries = []

            for i in range(file_count):
                if entry_offset + ENTRY_SIZE > len(raw):
                    break
                entries.append(
                    {
                        # 🔥수정됨: 엉뚱한 곳을 짚던 오프셋 주소를 정규 규격에 맞게 올바르게 수정
                        "data_offset": struct.unpack(
                            "<Q", raw[entry_offset + 16 : entry_offset + 24]
                        )[0],
                        "packed_size": struct.unpack(
                            "<I", raw[entry_offset + 24 : entry_offset + 28]
                        )[0],
                        "unpacked_size": struct.unpack(
                            "<I", raw[entry_offset + 28 : entry_offset + 32]
                        )[0],
                    }
                )
                entry_offset += ENTRY_SIZE

            mod_stem_lower = mod_stem.lower()
            for i, entry in enumerate(entries):
                filename = filenames[i] if i < len(filenames) else ""
                fname_only = filename.lower().replace("\\", "/").split("/")[-1]
                for ext_name, table_idx in table_map.items():
                    pattern = f"{mod_stem_lower}_{lang}.{ext_name}"
                    if fname_only == pattern:
                        offset = entry["data_offset"]
                        packed = entry["packed_size"]
                        unpacked = entry["unpacked_size"]

                        if packed == 0:
                            data = raw[offset : offset + unpacked]
                        else:
                            compressed = raw[offset : offset + packed]
                            data = None
                            for wbits in [-15, 15, 47]:
                                try:
                                    data = zlib.decompress(compressed, wbits=wbits)
                                    break
                                except zlib.error:
                                    continue

                        if data:
                            self.tables[lang][table_idx] = self._parse_bytes(data, ext_name)
                            found_any = True
                        break
            return found_any
        except Exception as e:
            print(f"  ✗ BA2 load failed ({lang}): {e}")
            return False

    def _parse_file(self, path: str, file_type: str = "strings") -> dict:
        with open(path, "rb") as f:
            return self._parse_bytes(f.read(), file_type)

    def _parse_bytes(self, raw: bytes, file_type: str = "strings") -> dict:
        result = {}
        if len(raw) < 8:
            return result

        count = struct.unpack("<I", raw[0:4])[0]
        data_size = struct.unpack("<I", raw[4:8])[0]
        dir_size = count * 8
        data_base = 8 + dir_size

        for k in range(count):
            off = 8 + k * 8
            if off + 8 > len(raw):
                break

            sid = struct.unpack("<I", raw[off : off + 4])[0]
            offset = struct.unpack("<I", raw[off + 4 : off + 8])[0]
            abs_ = data_base + offset

            if abs_ >= len(raw):
                continue

            if file_type == "strings":
                end = raw.find(b"\x00", abs_)
                if end == -1:
                    end = len(raw)
                byte_str = raw[abs_:end]
            else:
                # 🔥수정됨: .dlstrings 와 .ilstrings 처리 (길이 값이 이상할 때를 대비한 자동 폴백 추가)
                if abs_ + 4 <= len(raw):
                    str_len = struct.unpack("<I", raw[abs_ : abs_ + 4])[0]
                    # 터무니없는 길이거나 파일 크기를 초과하면 일반 문자열(null-terminated)로 취급
                    if 0 < str_len < 200000 and (abs_ + 4 + str_len <= len(raw)):
                        byte_str = raw[abs_ + 4 : abs_ + 4 + str_len].strip(b"\x00")
                    else:
                        end = raw.find(b"\x00", abs_)
                        if end == -1:
                            end = len(raw)
                        byte_str = raw[abs_:end]
                else:
                    byte_str = b""

            try:
                text = byte_str.decode("utf-8")
            except UnicodeDecodeError:
                text = byte_str.decode("cp1252", errors="replace")

            result[sid] = text

        return result

    def lookup(self, string_id: int, list_id: int, lang: str = "en") -> Optional[str]:
        lang = lang.lower()
        if lang not in self.tables:
            return None
        if list_id < 0 or list_id > 2:
            list_id = 0
        return self.tables[lang][list_id].get(string_id)


def decode_string(data: bytes) -> str:
    if not data:
        return ""
    end = len(data)
    while end > 0 and data[end - 1] == 0:
        end -= 1
    data = data[:end]
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except:
            return ""


# -----------------------------------------------------------------------
# ESM Analyzer
# -----------------------------------------------------------------------
class StarfieldSceneExtractor:
    def get_npc_name(self, fid: int) -> str:
        if fid in (7, 0x14):
            return "Player"
        if fid in self.npcs:
            return self.npcs[fid]["full"] if isinstance(self.npcs[fid], dict) else self.npcs[fid]

        hex_fid = f"{fid:08X}"
        if hex_fid in self.vanilla_npcs:
            v_npc = self.vanilla_npcs[hex_fid]
            if isinstance(v_npc, dict):
                return v_npc.get("full", v_npc.get("edid", f"NPC_{hex_fid}"))
            return v_npc

        return f"NPC_{hex_fid}"

    def get_npc_edid(self, fid: int) -> str:
        if fid in (7, 0x14):
            return "player"
        if fid in self.npcs and isinstance(self.npcs[fid], dict):
            return self.npcs[fid]["edid"]
        
        hex_fid = f"{fid:08X}"
        if hex_fid in self.vanilla_npcs:
            v_npc = self.vanilla_npcs[hex_fid]
            if isinstance(v_npc, dict):
                return v_npc.get("edid", "")
        return ""

    def __init__(self, file_path: str, strings_loader: StringsLoader, use_ja_ref: bool = False, lang: str = "en"):
        self.file_path = file_path
        self.mod_stem = os.path.splitext(os.path.basename(file_path))[0]
        self.strings_loader = strings_loader
        self.use_ja_ref = use_ja_ref
        self.lang = lang
        self.is_localized = False

        from db_manager import DBRAG
        self.db_rag = DBRAG()

        self.npcs = {}  # form_id -> {'full': name, 'edid': edid}
        self.quests = {}  # form_id -> {"aliases": {alias_id: alias_name}, "alias_npcs": {id: fid}, "edid": edid}
        self.infos = {}  # form_id -> INFO data dict
        self.dials = {}  # form_id -> DIAL data dict
        self.scenes_actors = {}  # form_id -> {alias_id: npc_form_id}
        self.scene_dial_to_alias = {}  # form_id(DIAL) -> alias_id
        self.scenes = {}  # 🔥 추가: 씬 타임라인 순서를 저장할 딕셔너리
        self.masters = [] # 🔥 추가: 마스터 파일 목록 (MAST)
        self.vtyps = {}   # 🔥 추가: Voice Type 목록
        self.current_dial = None

        # 바닐라 NPC 데이터 로드 (추가된 부분)
        self.vanilla_npcs = {}
        vanilla_json_path = "vanilla_npcs.json"
        if os.path.exists(vanilla_json_path):
            with open(vanilla_json_path, "rb") as f:
                raw_data = f.read()
            try:
                text_data = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text_data = raw_data.decode("cp1252")
                except UnicodeDecodeError:
                    text_data = raw_data.decode("utf-8", errors="ignore")
            try:
                self.vanilla_npcs = json.loads(text_data)
                print(f"Loaded {len(self.vanilla_npcs)} vanilla NPCs.")
            except json.JSONDecodeError as e:
                print(f"Failed to decode vanilla_npcs.json: {e}")

    def parse(self):
        import mmap

        print(f"Loading {os.path.basename(self.file_path)} into memory (mmap)...")
        with open(self.file_path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            self._read_chunk(mm)
            mm.close()
        
        # Ensure Starfield.esm is at index 0 if it's not the file itself
        base_name = os.path.basename(self.file_path).lower()
        if base_name != "starfield.esm":
            # Remove any existing Starfield.esm to avoid dups and move it to front
            other_masters = [m for m in self.masters if m.lower() != "starfield.esm"]
            self.masters = ["Starfield.esm"] + other_masters
            
        print(f"Finalized masters: {self.masters}")

    def _read_chunk(self, f, end_offset: int = -1):
        # struct.unpack 컴파일 (수백만 번 호출되는 녀석들 속도 극대화)
        unpack_I = struct.Struct("<I").unpack

        while True:
            if end_offset != -1 and f.tell() >= end_offset:
                break
            type_bytes = f.read(4)
            if not type_bytes or len(type_bytes) < 4:
                if end_offset == -1:
                    break
                raise EOFError()

            size_bytes = f.read(4)
            data_size = unpack_I(size_bytes)[0]

            if type_bytes == HEADER_GRUP:
                label_bytes = f.read(4)
                group_type = unpack_I(f.read(4))[0]
                f.seek(8, 1)  # f.read(8) 대신 seek(8, 1)로 커서만 이동 (속도업)

                if group_type == 7:  # Children of DIAL
                    self.current_dial = unpack_I(label_bytes)[0]

                group_end = f.tell() + (data_size - 24)
                self._read_chunk(f, group_end)
            else:
                self._read_record(f, type_bytes, data_size)

    # 자주 확인하는 타겟 레코드를 상수로 선언
    TARGET_RECORDS = {b"TES4", b"NPC_", b"QUST", b"INFO", b"DIAL", b"SCEN", b"VTYP"}

    def _read_record(self, f, rec_type: bytes, data_size: int):
        # 처리 대상이 아닌 레코드는 16바이트 헤더 잔여물과 데이터를 즉시 건너뜁니다
        if rec_type not in self.TARGET_RECORDS:
            f.seek(16 + data_size, 1)  # 1 = os.SEEK_CUR
            return

        flags_bytes = f.read(4)
        formid_bytes = f.read(4)
        f.seek(8, 1)  # 커서만 이동

        flags = struct.unpack("<I", flags_bytes)[0]
        form_id = struct.unpack("<I", formid_bytes)[0]

        if rec_type == HEADER_TES4:
            self.is_localized = bool(flags & 0x80)

        is_compressed = bool(flags & 0x00040000)

        if is_compressed:
            if data_size < 4:
                f.seek(data_size, 1)
                return
            struct.unpack("<I", f.read(4))[0]
            compressed = f.read(data_size - 4)
            try:
                record_data = zlib.decompress(compressed)
            except zlib.error:
                record_data = b""
        else:
            record_data = f.read(data_size)

        # 이미 위에서 타겟만 걸러냈으므로 바로 파싱으로 던짐
        rec_str = rec_type.decode("ascii")  # 출력용/비교용으로 이제서야 딱 한 번 디코드
        fields = self._parse_fields(record_data)
        self._process_record(rec_str, form_id, fields)

    def _parse_fields(self, data: bytes) -> List[FieldData]:
        raw_fields = []
        offset = 0
        length = len(data)
        next_field_size = 0
        mv = memoryview(data)

        while offset < length:
            if offset + 6 > length:
                break
            f_type = mv[offset : offset + 4].tobytes()
            f_size = int.from_bytes(mv[offset + 4 : offset + 6], "little")
            offset += 6

            actual_size = next_field_size if next_field_size else f_size
            next_field_size = 0

            if offset + actual_size > length:
                break
            f_data = mv[offset : offset + actual_size].tobytes()
            offset += actual_size

            if f_type == b"XXXX":
                if len(f_data) >= 4:
                    next_field_size = int.from_bytes(f_data, "little")
                continue

            raw_fields.append(FieldData(f_type, f_data))

        return raw_fields

    def _get_localized_string(
        self, f_data: bytes, list_id: str = "0", record_form_id: int = 0
    ) -> Tuple[int, str]:
        if not self.is_localized:
            text = decode_string(f_data)
            # 🌟 [CRITICAL FIX] 로컬라이징되지 않은 모드는 StringID가 없으므로 
            # 레코드의 FormID를 가상 ID로 사용하여 번역 오매칭(ID 0 중복) 방지
            return record_form_id, text

        if len(f_data) == 4:
            s_id = struct.unpack("<I", f_data)[0]
            text = self.strings_loader.lookup(s_id, int(list_id), self.lang) or ""
            
            # 일본어 참조 모드일 경우 DB에 저장
            if self.use_ja_ref and s_id > 0:
                ja_text = self.strings_loader.lookup(s_id, int(list_id), "ja")
                if ja_text:
                    self.db_rag.save_reference_string(self.mod_stem, s_id, "ja", ja_text)
            
            return s_id, text

        return 0, decode_string(f_data)

    def _process_record(self, rec_str: str, form_id: int, fields: List[FieldData]):
        rec_type_bytes = rec_str.encode("ascii")

        if rec_str == "TES4":
            for fd in fields:
                if fd.f_type == b"MAST":
                    m_name = fd.f_data.decode("utf-8", errors="ignore").strip("\x00")
                    if m_name not in self.masters:
                        self.masters.append(m_name)

        elif rec_str == "VTYP":
            for fd in fields:
                if fd.f_type == b"EDID":
                    edid = fd.f_data.decode("utf-8", errors="ignore").strip("\x00")
                    self.vtyps[form_id] = edid

        elif rec_str == "NPC_":
            name = f"Unknown NPC _{form_id:08X}"
            edid = ""
            vtyp = None
            for idx, fd in enumerate(fields):
                if fd.f_type == b"EDID":
                    edid = fd.f_data.decode("utf-8", errors="ignore").strip("\x00")
                elif fd.f_type == b"VTCK" and len(fd.f_data) >= 4:
                    vtyp = struct.unpack("<I", fd.f_data[:4])[0]
                    
                is_str, list_id = is_translatable(
                    rec_type_bytes, fd.f_type, fields, idx
                )
                if fd.f_type == b"FULL" and is_str:
                    _, nm = self._get_localized_string(fd.f_data, list_id)
                    if nm:
                        name = nm
            self.npcs[form_id] = {"full": name, "edid": edid, "vtyp": vtyp}

        elif rec_str == "QUST":
            aliases = {}
            alias_npcs = {}
            current_alias_id = -1
            current_alias_name = ""
            edid = ""
            for idx, fd in enumerate(fields):
                if fd.f_type == b"EDID":
                    edid = fd.f_data.decode("utf-8", errors="ignore").strip("\x00")
                elif fd.f_type in (b"ALST", b"ALLS") and len(fd.f_data) >= 4:
                    current_alias_id = struct.unpack("<I", fd.f_data[:4])[0]
                elif fd.f_type == b"ALID":
                    current_alias_name = fd.f_data.decode(
                        "utf-8", errors="ignore"
                    ).strip("\x00")
                    aliases[current_alias_id] = current_alias_name
                elif fd.f_type in (b"ALFR", b"ALUA") and len(fd.f_data) >= 4:
                    npc_fid = struct.unpack("<I", fd.f_data[:4])[0]
                    alias_npcs[current_alias_id] = npc_fid
            self.quests[form_id] = {"aliases": aliases, "alias_npcs": alias_npcs, "edid": edid}

        elif rec_str == "DIAL":
            topic_name = ""
            conditions = []
            alias_conds = []
            qnam = None
            for idx, fd in enumerate(fields):
                is_str, list_id = is_translatable(
                    rec_type_bytes, fd.f_type, fields, idx
                )
                if fd.f_type == b"FULL" and is_str:
                    _, nm = self._get_localized_string(fd.f_data, list_id)
                    topic_name = nm
                # INFO와 DIAL의 CTDA 조건문 파싱 위치에 공통으로 적용
                elif fd.f_type == b"CTDA" and len(fd.f_data) >= 24:
                    try:
                        func = struct.unpack("<H", fd.f_data[8:10])[0]
                        p1 = struct.unpack("<I", fd.f_data[12:16])[0]

                        if func == 72 and p1 not in (0, 0xFFFFFFFF):  # GetIsID
                            conditions.append(p1)
                        elif func == 566 and p1 != 0xFFFFFFFF:  # GetIsAlias
                            alias_conds.append(p1)
                    except:
                        pass
                elif fd.f_type == b"QNAM" and len(fd.f_data) >= 4:
                    qnam = struct.unpack("<I", fd.f_data[:4])[0]

            self.dials[form_id] = {
                "name": topic_name,
                "conditions": conditions,
                "aliases": alias_conds,
                "quest": qnam,
            }

        elif rec_str == "SCEN":
            scene_actors = {}
            current_alias_id = None
            curr_action_alias = None
            ordered_dials = []  # 🔥 씬 타임라인 순서 기록
            edid = ""

            for fd in fields:
                if fd.f_type == b"EDID":
                    edid = fd.f_data.decode("utf-8", errors="ignore").strip("\x00")
                elif fd.f_type == b"HNAM" and len(fd.f_data) >= 4:
                    current_alias_id = struct.unpack("<I", fd.f_data[:4])[0]
                elif (
                    fd.f_type == b"LNAM"
                    and current_alias_id is not None
                    and len(fd.f_data) >= 4
                ):
                    npc_fid = struct.unpack("<I", fd.f_data[:4])[0]
                    scene_actors[current_alias_id] = npc_fid
                    current_alias_id = None
                elif fd.f_type == b"ALID" and len(fd.f_data) >= 4:
                    curr_action_alias = struct.unpack("<I", fd.f_data[:4])[0]
                # 🔥 수정: 화자(ALID)가 생략된 플레이어 액션도 놓치지 않기 위해,
                # 4바이트짜리 DATA 필드는 모조리 잠재적 DIAL로 간주하여 타임라인에 담아둡니다.
                if fd.f_type == b"DATA" and len(fd.f_data) >= 4:
                    try:
                        dial_fid = struct.unpack("<I", fd.f_data[:4])[0]
                        if not ordered_dials or ordered_dials[-1] != dial_fid:
                            ordered_dials.append(dial_fid)

                        # 삭제된 기존 코드 복원: SCEN 대화와 화자를 연결해주는 핵심 포인터
                        if curr_action_alias is not None and dial_fid != 0:
                            self.scene_dial_to_alias[dial_fid] = curr_action_alias
                    except:
                        pass

            self.scenes_actors[form_id] = scene_actors
            self.scenes[form_id] = {"dials": ordered_dials, "edid": edid}  # 🔥 SCEN 딕셔너리에 대화 순서와 EDID 저장

        elif rec_str == "INFO":
            info_data = {
                "PNAM": None,
                "Speaker": None,
                "IsPrompt": False,
                "Conditions": [],
                "AliasConditions": [],
                "VoiceTypes": [],  # 🔥 추가: 목소리 타입 조건
                "Factions": [],  # 🔥 추가: 팩션 조건
                "AudioID": form_id,  # 🔥 스타필드 규칙: INFO FormID가 곧 보이스 파일명
                "ParentDial": self.current_dial,
                "Texts": [],
                "LinkedTopics": [],  # 🔥 추가: 분기점(선택지) DIAL의 FormID를 담을 리스트
                "FormID": form_id,
            }
            # Starfield INFO 내부에서 여러 NAM1 (응답 내용) 이 올 수 있습니다.
            current_trda_id = None

            for idx, fd in enumerate(fields):
                if fd.f_type == b"PNAM" and len(fd.f_data) >= 4:
                    try:
                        info_data["PNAM"] = struct.unpack("<I", fd.f_data[:4])[0]
                    except:
                        pass
                elif fd.f_type == b"RNAM":
                    info_data["IsPrompt"] = True
                elif fd.f_type == b"TRDA" and len(fd.f_data) >= 8:
                    try:
                        current_trda_id = struct.unpack("<I", fd.f_data[4:8])[0]
                    except:
                        pass
                elif fd.f_type == b"VNAM" and len(fd.f_data) >= 4:
                    # VNAM이 명시적으로 존재한다면 이를 우선시 (폴백용)
                    try:
                        info_data["AudioID"] = struct.unpack("<I", fd.f_data[:4])[0]
                    except:
                        pass
                elif fd.f_type == b"ANAM" and len(fd.f_data) >= 4:
                    try:
                        info_data["Speaker"] = struct.unpack("<I", fd.f_data[:4])[0]
                    except:
                        pass

                # 🔥 수정: Starfield는 TCLT뿐만 아니라 TPIC를 사용하여 연결된 선택지를 기록합니다.
                elif fd.f_type in (b"TCLT", b"TPIC"):
                    for i in range(0, len(fd.f_data), 4):
                        if i + 4 <= len(fd.f_data):
                            try:
                                linked_id = struct.unpack("<I", fd.f_data[i : i + 4])[0]
                                info_data["LinkedTopics"].append(linked_id)
                            except:
                                pass

                # INFO와 DIAL의 CTDA 조건문 파싱 위치에 공통으로 적용
                elif fd.f_type == b"CTDA" and len(fd.f_data) >= 24:
                    try:
                        func = struct.unpack("<H", fd.f_data[8:10])[0]
                        p1 = struct.unpack("<I", fd.f_data[12:16])[0]

                        if func == 72 and p1 not in (0, 0xFFFFFFFF):  # GetIsID
                            info_data["Conditions"].append(p1)
                        elif func == 566 and p1 != 0xFFFFFFFF:  # GetIsAlias
                            info_data["AliasConditions"].append(p1)
                        # 🔥 추가: 지나가는 시민, 경비병, 방송 화자 잡기
                        elif func == 73 and p1 != 0xFFFFFFFF:  # GetIsVoiceType
                            info_data["VoiceTypes"].append(p1)
                        elif func == 74 and p1 != 0xFFFFFFFF:  # GetInFaction
                            info_data["Factions"].append(p1)
                    except:
                        pass

                # 강제로 스트링 판별 시도 (대사 본문 및 플레이어 프롬프트 텍스트)
                if fd.f_type in (b"NAM1", b"RNAM"):
                    try:
                        is_str, list_id = is_translatable(
                            rec_type_bytes, fd.f_type, fields, idx
                        )
                        if is_str:
                            s_id, text = self._get_localized_string(fd.f_data, list_id, form_id)
                        else:
                            # 번역이 불가능하더라도 데이터가 있다면 자체 파싱
                            if len(fd.f_data) == 4 and self.is_localized:
                                s_id = struct.unpack("<I", fd.f_data)[0]
                                list_to_check = (
                                    2 if fd.f_type == b"NAM1" else 0
                                )  # RNAM은 대체로 Strings(0)
                                text = (
                                    self.strings_loader.lookup(s_id, list_to_check)
                                    or ""
                                )
                            else:
                                s_id = 0
                                text = decode_string(fd.f_data)

                        if text or s_id > 0:
                            # TRDA가 있는 경우 이를 우선시하고, 없는 경우 기본 AudioID(VNAM 또는 INFO FID) 사용
                            line_audio_id = current_trda_id if current_trda_id else info_data["AudioID"]
                            info_data["Texts"].append({
                                "StringID": s_id, 
                                "Text": text, 
                                "AudioID": line_audio_id
                            })
                            current_trda_id = None # 사용 후 초기화
                    except:
                        pass

            # 텍스트가 있거나, 다음 대화로 넘어가는 연결점(TCLT)이 있는 경우 수집 (라우팅 노드)
            if info_data["Texts"] or info_data["LinkedTopics"]:
                self.infos[form_id] = info_data

    def build_quest_batches(self) -> dict:
        """
        추출된 INFO 레코드들을 소속 DIAL 단위로 묶고, PNAM을 기반으로 엔진 정렬 순서에 맞게 연결 리스트로 재구축합니다.
        가장 실용적인 번역 컨텍스트를 제공합니다.
        """

        # 1. INFO별 화자 1차 식별
        for current_id, info in self.infos.items():
            spk_id = info["Speaker"]
            is_prompt = info.get("IsPrompt", False)
            info_conds = info.get("Conditions", [])
            info_aliases = info.get("AliasConditions", [])

            parent_dial = info.get("ParentDial")
            dial_dict = self.dials.get(parent_dial, {}) if parent_dial else {}
            dial_conds = dial_dict.get("conditions", [])
            dial_aliases = dial_dict.get("aliases", [])

            speaker_name = "Unknown"
            speaker_fid = None

            if spk_id and spk_id != 0:
                speaker_fid = spk_id
                speaker_name = self.get_npc_name(spk_id)
            elif is_prompt:
                speaker_fid = 7 # Player
                speaker_name = "Player"
            else:
                found_cond_spk = False

                # 1. INFO GetIsID
                for cond_id in info_conds:
                    speaker_fid = cond_id
                    speaker_name = self.get_npc_name(cond_id)
                    found_cond_spk = True
                    break

                # 2. DIAL GetIsID
                if not found_cond_spk:
                    for cond_id in dial_conds:
                        speaker_fid = cond_id
                        speaker_name = self.get_npc_name(cond_id)
                        found_cond_spk = True
                        break

                # 3. SCEN->DIAL (Action) GetIsAlias
                if not found_cond_spk and parent_dial in self.scene_dial_to_alias:
                    scen_alias_id = self.scene_dial_to_alias[parent_dial]
                    # Find which SCEN provides this NPC
                    for s_actors in self.scenes_actors.values():
                        if scen_alias_id in s_actors:
                            npc_fid = s_actors[scen_alias_id]
                            speaker_name = self.get_npc_name(npc_fid)
                            speaker_fid = npc_fid
                            found_cond_spk = True
                            break
                    if not found_cond_spk:
                        quest_id = dial_dict.get("quest")
                        if quest_id and quest_id in self.quests:
                            q = self.quests[quest_id]
                            npc_fid = q.get("alias_npcs", {}).get(scen_alias_id)
                            if npc_fid:
                                speaker_name = self.get_npc_name(npc_fid)
                                speaker_fid = npc_fid
                                found_cond_spk = True
                            if not found_cond_spk:
                                aname = q.get("aliases", {}).get(scen_alias_id)
                                if aname:
                                    speaker_name = aname
                                    found_cond_spk = True
                    if not found_cond_spk:
                        speaker_name = f"Alias_{scen_alias_id}"
                        found_cond_spk = True

                # 4. INFO GetIsAlias
                if not found_cond_spk and info_aliases:
                    for alias_id in info_aliases:
                        if found_cond_spk:
                            break
                        quest_id = dial_dict.get("quest")
                        if quest_id and quest_id in self.quests:
                            q = self.quests[quest_id]
                            npc_fid = q.get("alias_npcs", {}).get(alias_id)
                            if npc_fid:
                                speaker_name = self.get_npc_name(npc_fid)
                                speaker_fid = npc_fid
                                found_cond_spk = True
                            if not found_cond_spk:
                                aname = q.get("aliases", {}).get(alias_id)
                                if aname:
                                    speaker_name = aname
                                    found_cond_spk = True
                        if not found_cond_spk:
                            for s_actors in self.scenes_actors.values():
                                if alias_id in s_actors:
                                    npc_fid = s_actors[alias_id]
                                    speaker_name = self.get_npc_name(npc_fid)
                                    speaker_fid = npc_fid
                                    found_cond_spk = True
                                    break
                        if not found_cond_spk:
                            if alias_id != 0:  # skip empty 0 padding matches
                                speaker_name = f"Alias_{alias_id}"
                                found_cond_spk = True

                # 5. DIAL GetIsAlias
                if not found_cond_spk and dial_aliases:
                    for alias_id in dial_aliases:
                        if found_cond_spk:
                            break
                        quest_id = dial_dict.get("quest")
                        if quest_id and quest_id in self.quests:
                            q = self.quests[quest_id]
                            npc_fid = q.get("alias_npcs", {}).get(alias_id)
                            if npc_fid:
                                speaker_name = self.get_npc_name(npc_fid)
                                speaker_fid = npc_fid
                                found_cond_spk = True
                            if not found_cond_spk:
                                aname = q.get("aliases", {}).get(alias_id)
                                if aname:
                                    speaker_name = aname
                                    found_cond_spk = True
                        if not found_cond_spk:
                            for s_actors in self.scenes_actors.values():
                                if alias_id in s_actors:
                                    npc_fid = s_actors[alias_id]
                                    speaker_name = self.get_npc_name(npc_fid)
                                    speaker_fid = npc_fid
                                    found_cond_spk = True
                                    break
                        if not found_cond_spk:
                            if alias_id != 0:
                                speaker_name = f"Alias_{alias_id}"
                                found_cond_spk = True

                # 6. INFO GetIsVoiceType (목소리 타입으로 화자 유추)
                if not found_cond_spk and info.get("VoiceTypes"):
                    vtyp_id = info["VoiceTypes"][0]
                    speaker_name = f"VoiceType_{vtyp_id:08X}"
                    found_cond_spk = True

                # 7. INFO GetInFaction (소속 팩션으로 화자 유추)
                if not found_cond_spk and info.get("Factions"):
                    fact_id = info["Factions"][0]
                    speaker_name = f"Faction_{fact_id:08X}"
                    speaker_fid = fact_id
                    found_cond_spk = True

            info["_resolved_speaker"] = speaker_name or "Unknown"
            info["_resolved_speaker_fid"] = speaker_fid

        # 2. DIAL별 최다 화자 상속 휴리스틱
        dial_speakers = {}
        for fid, info in self.infos.items():
            pd = info.get("ParentDial")
            spk = info.get("_resolved_speaker")
            if (
                pd
                and spk
                and spk not in ("Unknown", "Player")
                and not spk.startswith("Alias_")
            ):
                dial_speakers.setdefault(pd, []).append(spk)

        for fid, info in self.infos.items():
            curr_spk = info.get("_resolved_speaker", "Unknown")
            if not curr_spk or curr_spk == "Unknown" or curr_spk.startswith("Alias_"):
                pd = info.get("ParentDial")
                if pd in dial_speakers and dial_speakers[pd]:
                    from collections import Counter

                    most_common = Counter(dial_speakers[pd]).most_common(1)[0][0]
                    info["_resolved_speaker"] = most_common
                else:
                    # Player로 무조건 퉁치지 말고, DIAL에 다른 NPC 화자가 전혀 없을 때만 추론
                    info["_resolved_speaker"] = "Unknown"

        quest_groups = {}
        def get_quest_group(qid: int):
            if qid not in quest_groups:
                q_edid = ""
                if qid in self.quests:
                    q_edid = self.quests[qid].get("edid", "")
                
                quest_groups[qid] = {
                    "QuestID": f"{qid:08X}" if qid else "00000000",
                    "QuestName": q_edid,
                    "Scenes": [],
                    "StandaloneDials": []
                }
                if not q_edid:
                    del quest_groups[qid]["QuestName"]
            return quest_groups[qid]

        processed_dials = set()

        # 2. SCEN(Scene) 기반 메인 타임라인 구축
        for scen_id, scen_data in self.scenes.items():
            ordered_dials = scen_data.get("dials", [])
            scen_edid = scen_data.get("edid", "")
            
            scen_quest_id = None
            scene_dials_list = []

            for dial_fid in ordered_dials:
                if dial_fid in processed_dials:
                    continue
                processed_dials.add(dial_fid)

                dial_dict = self.dials.get(dial_fid, {})
                if not scen_quest_id and dial_dict.get("quest"):
                    scen_quest_id = dial_dict.get("quest")

                dial_infos = [
                    info for fid, info in self.infos.items()
                    if info.get("ParentDial") == dial_fid
                ]
                if not dial_infos:
                    continue

                dialogues = []
                for info in dial_infos:
                    texts_data = info.get("Texts", [])
                    if not texts_data:
                        continue

                    speaker_name = info.get("_resolved_speaker", "Unknown")
                    speaker_fid = info.get("_resolved_speaker_fid")
                    
                    audio_speaker = "unknown"
                    if speaker_fid:
                        # 1. Voice Type (VTCK) 기반 폴더명 우선 (스타필드 모드 표준)
                        vtyp_id = self.npcs.get(speaker_fid, {}).get("vtyp")
                        if vtyp_id in self.vtyps:
                             audio_speaker = self.vtyps[vtyp_id].lower()
                        else:
                             edid = self.get_npc_edid(speaker_fid)
                             audio_speaker = edid.lower() if edid else speaker_name.replace(" ", "").lower()
                    else:
                        audio_speaker = speaker_name.replace(" ", "").lower()

                    for txt in texts_data:
                        dialogue_entry = {
                            "FormID": f"{info['FormID']:08X}",
                            "StringID": f"{txt['StringID']:08X}" if txt.get('StringID') else "00000000",
                            "Speaker": speaker_name,
                            "Text": txt.get("Text", "")
                        }
                        target_audio_id = txt.get("AudioID") or info.get("AudioID")
                        if target_audio_id:
                            prefix = target_audio_id >> 24
                            audio_id = target_audio_id & 0x00FFFFFF 
                            
                            if prefix < len(self.masters):
                                owner_master = self.masters[prefix].lower()
                                wem_id = f"{audio_id:08X}"
                            elif prefix == len(self.masters) or prefix == 0xFD:
                                owner_master = os.path.basename(self.file_path).lower()
                                wem_id = f"{audio_id:08X}"
                            else:
                                owner_master = "starfield.esm"
                                wem_id = f"{audio_id:08X}"
                            
                            # [DEBUG LOG]
                            if "_debug_logged" not in info: # redundant logs prevent
                                # [DEBUG] FormID: {info['FormID']:08X} (Prefix: {prefix:02X}) -> Owner: {owner_master}
                                info["_debug_logged"] = True

                            dialogue_entry["AudioPath"] = f"sound\\voice\\{owner_master}\\{audio_speaker}\\{wem_id.lower()}.wem"
                        dialogues.append(dialogue_entry)

                if dialogues:
                    scene_dials_list.append({
                        "DialID": f"{dial_fid:08X}",
                        "Dialogues": dialogues
                    })

            if scene_dials_list:
                qg = get_quest_group(scen_quest_id or 0)
                scen_dict = {
                    "SceneID": f"{scen_id:08X}",
                    "Dials": scene_dials_list
                }
                if scen_edid:
                    scen_dict["SceneName"] = scen_edid
                qg["Scenes"].append(scen_dict)

        # 3. Standalone Dials
        for dial_id, dial in self.dials.items():
            if dial_id in processed_dials:
                continue

            quest_id = dial.get("quest")
            dial_infos = [
                info for fid, info in self.infos.items()
                if info.get("ParentDial") == dial_id
            ]
            if not dial_infos:
                continue

            has_npc_speaker = any(
                info.get("_resolved_speaker", "Unknown") not in ("Unknown", "Player")
                for info in dial_infos
            )

            dialogues = []
            for info in dial_infos:
                texts_data = info.get("Texts", [])
                if not texts_data:
                    continue

                speaker_name = info.get("_resolved_speaker", "Unknown")
                speaker_fid = info.get("_resolved_speaker_fid")
                if speaker_name == "Unknown" and not has_npc_speaker:
                    speaker_name = "Player (Inferred)"
                    speaker_fid = 7

                audio_speaker = "unknown"
                if speaker_fid:
                    # 1. Voice Type (VTCK) 기반 폴더명 우선 (스타필드 모드 표준)
                    vtyp_id = self.npcs.get(speaker_fid, {}).get("vtyp")
                    if vtyp_id in self.vtyps:
                         audio_speaker = self.vtyps[vtyp_id].lower()
                    else:
                         edid = self.get_npc_edid(speaker_fid)
                         audio_speaker = edid.lower() if edid else speaker_name.replace(" ", "").lower()
                else:
                    audio_speaker = speaker_name.replace(" ", "").lower()

                for txt in texts_data:
                    dialogue_entry = {
                        "FormID": f"{info['FormID']:08X}",
                        "StringID": f"{txt['StringID']:08X}" if txt.get('StringID') else "00000000",
                        "Speaker": speaker_name,
                        "Text": txt.get("Text", "")
                    }
                    
                    target_audio_id = txt.get("AudioID") or info.get("AudioID")
                    if target_audio_id:
                        prefix = target_audio_id >> 24
                        audio_id = target_audio_id & 0x00FFFFFF 
                        
                        if prefix < len(self.masters):
                            owner_master = self.masters[prefix].lower()
                            wem_id = f"{audio_id:08X}"
                        elif prefix == len(self.masters) or prefix == 0xFD:
                            owner_master = os.path.basename(self.file_path).lower()
                            wem_id = f"{audio_id:08X}"
                        else:
                            owner_master = "starfield.esm"
                            wem_id = f"{audio_id:08X}"
                            
                        dialogue_entry["AudioPath"] = f"sound\\voice\\{owner_master}\\{audio_speaker}\\{wem_id.lower()}.wem"
                    dialogues.append(dialogue_entry)

            if dialogues:
                qg = get_quest_group(quest_id or 0)
                dial_dict = {
                    "DialID": f"{dial_id:08X}",
                    "Dialogues": dialogues
                }
                dial_name = dial.get("name", "")
                if dial_name:
                    dial_dict["DialName"] = dial_name
                qg["StandaloneDials"].append(dial_dict)

        return list(quest_groups.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("--strings-dir", default=None)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--use-ja-ref", action="store_true", help="일본어 원문 참조 모드 활성 (공식 DLC/모드 등 일본어 존재 시)")
    parser.add_argument("-o", "--output", default="dump.json")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    mod_stem = os.path.splitext(os.path.basename(input_path))[0]
    out_file = os.path.abspath(args.output)
    strings_dir = args.strings_dir if args.strings_dir else os.path.dirname(input_path)

    langs = [args.lang]
    if args.use_ja_ref and "ja" not in langs:
        langs.append("ja")

    loader = StringsLoader()
    loader.load(strings_dir, mod_stem, langs)

    print(f"Parsing ESM records for {mod_stem}...")
    extractor = StarfieldSceneExtractor(input_path, loader, use_ja_ref=args.use_ja_ref, lang=args.lang)
    extractor.parse()

    print(
        f"Loaded {len(extractor.npcs)} NPCs, {len(extractor.dials)} Topics, {len(extractor.infos)} Dialogue Responses."
    )
    # 🔥 오디오 데이터 보유 현황 통계
    audio_info_count = sum(1 for info in extractor.infos.values() if info.get("AudioID"))
    print(f"  (Audio-Ready INFOs: {audio_info_count} / {len(extractor.infos)})")

    batches = extractor.build_quest_batches()
    print(f"Built {len(batches)} quest batches.")

    # 🔥 1. 화자별 샘플링 시스템 (priority_list.json 생성) - AudioPath가 필요하므로 먼저 실행
    print("Generating priority_list.json for audio extraction...")
    speaker_samples = {}
    for batch in batches:
        processed_items = []
        if "Scenes" in batch:
            for scene in batch["Scenes"]:
                for dial in scene["Dials"]:
                    processed_items.extend(dial["Dialogues"])
        if "StandaloneDials" in batch:
            for dial in batch["StandaloneDials"]:
                processed_items.extend(dial["Dialogues"])
        
        for item in processed_items:
            spk = item["Speaker"]
            if spk in ("Player", "Unknown") or "AudioPath" not in item:
                continue
            if spk not in speaker_samples:
                speaker_samples[spk] = []
            speaker_samples[spk].append({
                "Text": item["Text"],
                "AudioPath": item["AudioPath"]
            })

    priority_list = {}
    import random
    for spk, samples in speaker_samples.items():
        if len(samples) <= 1:
            continue
        count = min(len(samples), 10)
        priority_list[spk] = random.sample(samples, count)

    priority_file = os.path.join(os.path.dirname(out_file), "priority_list.json")
    with open(priority_file, "w", encoding="utf-8") as f:
        json.dump(priority_list, f, ensure_ascii=False, indent=2)
    print(f"  ✓ Priority list created ({len(priority_list)} speakers): {priority_file}")

    # 🔥 2. dump.json 저장 전 AudioPath 제거 (번역 데이터 경량화)
    for q in batches:
        for s in q.get("Scenes", []):
            for d in s.get("Dials", []):
                for dial in d.get("Dialogues", []):
                    dial.pop("AudioPath", None)
        for d in q.get("StandaloneDials", []):
            for dial in d.get("Dialogues", []):
                dial.pop("AudioPath", None)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(batches, f, ensure_ascii=False, indent=2)
    print(f"✅ Extracted scene data saved: {out_file}")


if __name__ == "__main__":
    main()
