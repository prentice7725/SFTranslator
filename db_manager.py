import sqlite3
import json
import logging
from pathlib import Path
from difflib import SequenceMatcher
from typing import Optional, Dict
from sqlite3 import Connection as SQLiteConnection

SCRIPT_DIR = Path(__file__).parent.resolve()
DB_FILE = SCRIPT_DIR / "translation_db.sqlite"

log = logging.getLogger("DB_Manager")

def get_connection():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = get_connection()
    c = conn.cursor()
    # 1. 용어집 테이블
    c.execute('''
    CREATE TABLE IF NOT EXISTS glossary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        english TEXT UNIQUE NOT NULL,
        korean TEXT NOT NULL
    )
    ''')
    
    # 2. 번역 메모리(TM) 테이블
    c.execute('''
    CREATE TABLE IF NOT EXISTS translation_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        english TEXT UNIQUE NOT NULL,
        korean TEXT NOT NULL,
        english_length INTEGER NOT NULL
    )
    ''')
    
    # 3. NPC 이름 매핑 테이블
    c.execute('''
    CREATE TABLE IF NOT EXISTS npc_names (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        form_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL
    )
    ''')
    
    # 4. 일본어(참조) 스트링 보관 테이블 [신규]
    c.execute('''
    CREATE TABLE IF NOT EXISTS reference_strings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mod_name TEXT NOT NULL,
        string_id INTEGER NOT NULL,
        lang TEXT NOT NULL,
        text TEXT NOT NULL,
        UNIQUE(mod_name, string_id, lang)
    )
    ''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_tm_length ON translation_memory(english_length)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tm_english ON translation_memory(english)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_npc_form_id ON npc_names(form_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ref_lookup ON reference_strings(mod_name, string_id, lang)')
    
    conn.commit()
    conn.close()

def migrate_from_json(glossary_path="glossary.json", tm_path="train_data_final.json", npc_path="vanilla_npcs.json"):
    log.info("JSON 데이터를 SQLite로 마이그레이션 시작합니다...")
    init_db()
    conn = get_connection()
    c = conn.cursor()
    
    # 1. Glossary migration
    g_path = SCRIPT_DIR / glossary_path
    if g_path.exists():
        log.info("용어집(Glossary) 마이그레이션 중...")
        with open(g_path, "r", encoding="utf-8") as f:
            glossary_data = json.load(f)
            
        for en, ko in glossary_data.items():
            en_clean = en.strip()
            if en_clean:
                try:
                    c.execute('INSERT OR IGNORE INTO glossary (english, korean) VALUES (?, ?)', (en_clean, ko.strip()))
                except Exception as e:
                    log.warning(f"Glossary Insert Error for {en}: {e}")
        conn.commit()
        log.info("용어집(Glossary) 마이그레이션 완료.")
        
    # 2. NPC Names migration
    n_path = SCRIPT_DIR / npc_path
    if n_path.exists():
        log.info("NPC 이름 데이터(NPC Names) 마이그레이션 중...")
        npc_data = {}
        try:
            with open(n_path, "r", encoding="utf-8") as f:
                npc_data = json.load(f)
        except UnicodeDecodeError:
            log.info("UTF-8 디코드 실패, CP1252 시도 중...")
            with open(n_path, "r", encoding="cp1252", errors="replace") as f:
                npc_data = json.load(f)
        except Exception as e:
            log.warning(f"NPC JSON 로드 실패: {e}")
        
        if npc_data:
            npc_list = []
            for fid, name in npc_data.items():
                npc_list.append((fid.upper(), name))
                
            c.executemany('INSERT OR IGNORE INTO npc_names (form_id, name) VALUES (?, ?)', npc_list)
            conn.commit()
            log.info(f"NPC 이름 마이그레이션 완료. {len(npc_list)}개 항목.")

    # 3. TM migration
    tm_path_obj = SCRIPT_DIR / tm_path
    if tm_path_obj.exists():
        log.info("번역 메모리(TM) 마이그레이션 중... 대용량 파일이므로 약간의 시간이 소요될 수 있습니다.")
        with open(tm_path_obj, "r", encoding="utf-8") as f:
            tm_data = json.load(f)
            
        batch_size = 10000
        batch = []
        for item in tm_data:
            en = item.get("input", "").strip()
            ko = item.get("output", "").strip()
            if en and ko:
                batch.append((en, ko, len(en)))
                
            if len(batch) >= batch_size:
                c.executemany('INSERT OR IGNORE INTO translation_memory (english, korean, english_length) VALUES (?, ?, ?)', batch)
                batch = []
                
        if batch:
            c.executemany('INSERT OR IGNORE INTO translation_memory (english, korean, english_length) VALUES (?, ?, ?)', batch)
            
        conn.commit()
        
        c.execute('SELECT COUNT(*) FROM translation_memory')
        count = c.fetchone()[0]
        log.info(f"TM 마이그레이션 완료. DB 기록 데이터 개수: {count}개.")
        
    conn.close()
    log.info("모든 마이그레이션 작업이 완료되었습니다.")

class DBRAG:
    """SQLite 기반의 RAG 매칭 및 데이터 조회"""
    def __init__(self):
        # 테이블이 없으면 생성(특히 새로 추가된 reference_strings)
        init_db()
        self.enabled = True
        self._conn: Optional[SQLiteConnection] = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    
    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()
    
    def get_npc_name(self, form_id: str) -> Optional[str]:
        """FormID(Hex string)로 NPC 이름을 조회합니다."""
        if not self.enabled or self._conn is None:
            return None
        try:
            c = self._conn.cursor()
            c.execute('SELECT name FROM npc_names WHERE form_id = ?', (form_id.upper(),))
            row = c.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def find_exact(self, src: str) -> Optional[str]:
        """[Step 3] RAG 100% 완전 일치 매칭만 반환합니다.
        유사도 기반 폴백 없음 — 정확히 동일한 영문 원문이 있을 때만 번역문을 반환합니다.
        """
        if not self.enabled or self._conn is None:
            return None
        clean = src.strip()
        if not clean:
            return None
        try:
            c = self._conn.cursor()
            c.execute(
                "SELECT korean FROM translation_memory WHERE english = ?",
                (clean,),
            )
            row = c.fetchone()
            return row[0] if row else None
        except Exception as e:
            log.warning(f"DBRAG.find_exact 오류: {e}")
            return None

    def get_reference_string(self, mod_name: str, string_id: int, lang: str = "ja") -> Optional[str]:
        """특정 모드의 특정 StringID에 해당하는 참조 언어(주로 일본어) 텍스트를 가져옵니다."""
        if not self.enabled or self._conn is None:
            return None
        try:
            c = self._conn.cursor()
            # mod_name은 파일명(확장자 제외)으로 관리함
            c.execute(
                'SELECT text FROM reference_strings WHERE mod_name = ? AND string_id = ? AND lang = ?',
                (mod_name.lower(), string_id, lang.lower())
            )
            row = c.fetchone()
            return row[0] if row else None
        except Exception as e:
            log.warning(f"DBRAG.get_reference_string 오류: {e}")
            return None

    def save_reference_string(self, mod_name: str, string_id: int, lang: str, text: str):
        """참조 언어 텍스트를 DB에 저장합니다."""
        if not self.enabled or self._conn is None:
            return
        try:
            c = self._conn.cursor()
            c.execute(
                'INSERT OR REPLACE INTO reference_strings (mod_name, string_id, lang, text) VALUES (?, ?, ?, ?)',
                (mod_name.lower(), string_id, lang.lower(), text)
            )
            self._conn.commit()
        except Exception as e:
            log.warning(f"DBRAG.save_reference_string 오류: {e}")

    def find_fuzzy(self, src: str) -> Optional[str]:
        if not self.enabled or self._conn is None:
            return None
        clean = src.strip()
        len_clean = len(clean)
        if len_clean == 0:
            return None
        
        try:
            c = self._conn.cursor()
            c.execute('SELECT english, korean FROM translation_memory WHERE english_length BETWEEN ? AND ?', (len_clean - 2, len_clean + 2))
            candidates = c.fetchall()
            
            if not candidates:
                return None
                
            unique = []
            seen = set()
            for en, ko in candidates:
                if en not in seen:
                    seen.add(en)
                    unique.append((en, ko))
            
            for en, ko in unique[:100]:
                if en == clean:
                    return ko
                    
            best_score = 0.0
            best_ko = None
            for en, ko in unique[:50]:
                score = SequenceMatcher(None, clean, en).ratio()
                if score > best_score and score >= 0.8:
                    best_score = score
                    best_ko = ko
                    
            return best_ko
        except Exception as e:
            log.warning(f"DBRAG.find_fuzzy 오류: {e}")
            return None

    def polish_with_rag(self, src: str) -> Optional[str]:
        """step2_translate_scene.py 등에서 사용하는 RAG 매칭 별칭 메서드입니다."""
        return self.find_fuzzy(src)

def load_glossary_db() -> Dict[str, str]:
    if not DB_FILE.exists():
        return {}
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT english, korean FROM glossary')
    rows = c.fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    migrate_from_json()
