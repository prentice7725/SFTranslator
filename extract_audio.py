import os
import json
import struct
import zlib
import shutil
from pathlib import Path

class BA2Extractor:
    def __init__(self, ba2_path):
        self.ba2_path = ba2_path
        self.file_map = {} # path -> entry
        self._index_ready = False

    def _initialize_index(self):
        if self._index_ready:
            return
        
        try:
            with open(self.ba2_path, "rb") as f:
                header = f.read(32)
                if header[0:4] != b"BTDX":
                    return
                
                version = struct.unpack("<I", header[4:8])[0]
                file_count = struct.unpack("<I", header[12:16])[0]
                str_offset = struct.unpack("<Q", header[16:24])[0]
                
                entry_offset = 32 if version >= 2 else 24
                
                # Filenames loading
                f.seek(str_offset)
                filenames = []
                for _ in range(file_count):
                    name_len = struct.unpack("<H", f.read(2))[0]
                    name = f.read(name_len).decode("utf-8", errors="ignore").lower().replace("/", "\\")
                    filenames.append(name)
                
                # Entries loading
                f.seek(entry_offset)
                # Starfield (BTDX Version 2) GNMT/DX10 entries are 36 bytes.
                # Fallout 4 (BTDX Version 1) General: 36, Texture: 24.
                ENTRY_SIZE = 36
                if version == 1:
                    # Check type for FO4
                    header_type = header[8:12]
                    if header_type == b"DX10":
                        ENTRY_SIZE = 24
                
                for i in range(file_count):
                    entry_raw = f.read(ENTRY_SIZE)
                    if len(entry_raw) < ENTRY_SIZE:
                        break
                    # Starfield GNMT entry: 
                    # offset is at byte 16 (8 bytes)
                    # packed_size at 24 (4 bytes)
                    # unpacked_size at 28 (4 bytes)
                    offset = struct.unpack("<Q", entry_raw[16:24])[0]
                    packed = struct.unpack("<I", entry_raw[24:28])[0]
                    unpacked = struct.unpack("<I", entry_raw[28:32])[0]
                    
                    if i < len(filenames):
                        self.file_map[filenames[i]] = {
                            "offset": offset,
                            "packed": packed,
                            "unpacked": unpacked
                        }
            self._index_ready = True
            print(f"  [DEBUG] Indexed {len(self.file_map)} files from {os.path.basename(self.ba2_path)} (Version: {version}, Size: {ENTRY_SIZE})")
            # 처음 3개 파일 경로 출력하여 구조 확인
            if self.file_map:
                samples = list(self.file_map.keys())[:3]
                print(f"     -> Sample paths: {samples}")
        except Exception as e:
            print(f"Error indexing {self.ba2_path}: {e}")

    def extract(self, target_path, output_path):
        self._initialize_index()
        target = target_path.lower().replace("/", "\\")
        
        if target not in self.file_map:
            return False
        
        entry = self.file_map[target]
        try:
            with open(self.ba2_path, "rb") as f:
                f.seek(entry["offset"])
                if entry["packed"] == 0:
                    data = f.read(entry["unpacked"])
                else:
                    compressed = f.read(entry["packed"])
                    # RAW zlib (no header) use wbits=-15
                    try:
                        data = zlib.decompress(compressed, wbits=-15)
                    except zlib.error:
                        # Fallback to auto
                        data = zlib.decompress(compressed, wbits=15)
                
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as out:
                    out.write(data)
                return True
        except Exception as e:
            print(f"Error extracting {target} from {self.ba2_path}: {e}")
            return False

    def find_by_basename(self, basename):
        """파일명(또는 ID)만으로 내부 경로를 찾아 반환 (더욱 유연하게)"""
        self._initialize_index()
        # 확장자를 뗀 순수 ID 추출 (예: 0000e974.wem -> 0000e974)
        stem = os.path.splitext(basename)[0].lower()
        for path in self.file_map.keys():
            if stem in path and path.endswith(".wem"):
                return path
        return None

def run_extraction(priority_list_path, data_dir, output_dir):
    if not os.path.exists(priority_list_path):
        print(f"Priority list not found: {priority_list_path}")
        return

    with open(priority_list_path, "r", encoding="utf-8") as f:
        priority_list = json.load(f)

    # BA2 Extractors cache
    extractors = {}
    
    # 데이터 폴더 내의 모든 BA2 파일 목록 가져오기
    try:
        ba2_files = [f for f in os.listdir(data_dir) if f.lower().endswith(".ba2")]
        ba2_full_paths = [os.path.join(data_dir, f) for f in ba2_files]
        
        # Priority List가 있는 폴더(모드 작업 폴더)에서도 BA2 탐색 (스마트 폴백)
        priority_dir = os.path.dirname(priority_list_path)
        if priority_dir.lower() != data_dir.lower():
            mod_ba2_files = [f for f in os.listdir(priority_dir) if f.lower().endswith(".ba2")]
            for f in mod_ba2_files:
                full_p = os.path.join(priority_dir, f)
                if full_p not in ba2_full_paths:
                    ba2_full_paths.append(full_p)
                    print(f"  🔍 Found additional archive in project dir: {f}")
                    
    except Exception as e:
        print(f"  ❌ Error scanning directories: {e}")
        return

    # 3. 바닐라 및 확장 아카이브 자동 탐색
    voice_archives = []
    print(f"  🔍 Scanning for voice archives...")
    for full_path in ba2_full_paths:
        f = os.path.basename(full_path)
        f_lower = f.lower()
        # 모든 보이스 팩(Numbered, Patch, Locale, Mod Voices) 탐색
        if f_lower.startswith("starfield - voices") or "voices" in f_lower:
            print(f"     -> Adding archive: {f}")
            voice_archives.append(full_path)
    
    if not voice_archives:
        print(f"  ⚠️ Warning: No 'Starfield - Voices' archives found in {data_dir}")
        print(f"     Please ensure 'Game Data Dir' points to the actual Starfield/Data folder.")
    
    # [DEBUG] Print all target archives
    print(f"  🔍 Total archives to scan: {len(ba2_full_paths)}")

    for speaker, samples in priority_list.items():
        print(f"Processing speaker: {speaker}")
        speaker_dir = os.path.join(output_dir, speaker)
        os.makedirs(speaker_dir, exist_ok=True)
        
        for sample in samples:
            rel_path = sample["AudioPath"]
            dest_name = os.path.basename(rel_path)
            dest_path = os.path.join(speaker_dir, dest_name)
            
            extracted = False
            
            # 1. Loose Files check (Game Data & Project Dir)
            loose_paths = [
                os.path.join(data_dir, rel_path),
                os.path.join(priority_dir, rel_path)
            ]
            for lp in loose_paths:
                if os.path.exists(lp):
                    shutil.copy(lp, dest_path)
                    extracted = True
                    break
            
            # 2. Mod/Creation Archive check (Includes official Creations like SFBGS)
            if not extracted:
                parts = rel_path.split("\\")
                if len(parts) > 2:
                    master_esm = parts[2] # sound\voice\[Master_File]
                    mod_stem = os.path.splitext(master_esm)[0].lower()
                    
                    # Search through all gathered ba2 files for anything matching the mod stem
                    for ba2_path in ba2_full_paths:
                        ba2_name = os.path.basename(ba2_path)
                        if mod_stem in ba2_name.lower():
                            if ba2_path not in extractors:
                                extractors[ba2_path] = BA2Extractor(ba2_path)
                            
                            # Try to extract from this archive (Exact Match)
                            if extractors[ba2_path].extract(rel_path, dest_path):
                                extracted = True
                                break
                            
                            # Try without .esm extension in path (Fuzzy folder match)
                            fuzzy_rel = rel_path.replace(".esm\\", "\\").replace(".esp\\", "\\")
                            if fuzzy_rel != rel_path:
                                if extractors[ba2_path].extract(fuzzy_rel, dest_path):
                                    extracted = True
                                    print(f"     [INFO] Found via fuzzy master folder: {fuzzy_rel}")
                                    break
            
            # 3. Vanilla Archives check (Iterate all found voice BA2s)
            if not extracted:
                for ba2_path in voice_archives:
                    if ba2_path not in extractors:
                        extractors[ba2_path] = BA2Extractor(ba2_path)
                    if extractors[ba2_path].extract(rel_path, dest_path):
                        extracted = True
                        break

            # 4. Final Fallback: Search by Basename in ALL available archives
            if not extracted:
                basename = os.path.basename(rel_path)
                for ba2_path in ba2_full_paths:
                    if ba2_path not in extractors:
                        extractors[ba2_path] = BA2Extractor(ba2_path)
                    ext = extractors[ba2_path]
                    found_path = ext.find_by_basename(basename)
                    if found_path:
                        if ext.extract(found_path, dest_path):
                            extracted = True
                            print(f"     [INFO] Found via basename fallback: {found_path} in {os.path.basename(ba2_path)}")
                            break

            if extracted:
                print(f"  ✓ Extracted: {dest_name}")
            else:
                print(f"  ⚠️ Failed: {rel_path}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--priority-list", default="priority_list.json")
    parser.add_argument("-d", "--data-dir", required=True, help="Starfield Data directory")
    parser.add_argument("-o", "--output-dir", default="temp/audition")
    args = parser.parse_args()
    
    run_extraction(args.priority_list, args.data_dir, args.output_dir)
