import json
import logging
import os
import hashlib
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import json_repair

log = logging.getLogger("Orchestrator")

FATAL_LLM_ERROR_KEYWORDS = (
    "iam_permission_denied",
    "permission 'aiplatform.endpoints.predict'",
    "permission denied",
    "defaultcredentialserror",
    "google_application_credentials",
)


def is_fatal_llm_error(exc) -> bool:
    err = str(exc).lower()
    return any(keyword in err for keyword in FATAL_LLM_ERROR_KEYWORDS)


class TranslationCache:
    """번역 결과물을 로컬 파일에 캐싱하여 중복 요청을 방지합니다."""
    def __init__(self, cache_file="translation_cache.json"):
        self.cache_file = cache_file
        self.cache = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception as e:
                log.warning(f"캐시 파일을 로드하지 못했습니다: {e}")

    def get_key(self, model_name, system_instruction, prompt):
        """요청의 고유 키를 생성합니다."""
        raw = f"{model_name}|{system_instruction}|{prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, model_name, system_instruction, prompt):
        key = self.get_key(model_name, system_instruction, prompt)
        return self.cache.get(key)

    def set(self, model_name, system_instruction, prompt, result):
        key = self.get_key(model_name, system_instruction, prompt)
        self.cache[key] = result
        self._save()

    def _save(self):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"캐시 저장 실패: {e}")

class TranslationOrchestrator:
    """
    다중 모델을 활용하여 번역 후보를 생성하고, 최종 감수 모델을 통해 최적의 번역본을 도출합니다.
    """
    def __init__(self, gen_backends, review_backend, glossary_text="", cache_enabled=True, work_dir=None):
        """
        gen_backends: 후보 생성을 담당할 백엔드 리스트 (각기 다른 페르소나 설정 권장)
        review_backend: 최종 감수 및 확정을 담당할 고성능 백엔드
        glossary_text: 준수해야 할 용어집 텍스트
        work_dir: 결과물을 저장할 모드별 작업 폴더 (None이면 현재 폴더)
        """
        self.gen_backends = gen_backends
        self.review_backend = review_backend
        self.glossary_text = glossary_text
        self.work_dir = Path(work_dir) if work_dir else Path(".")
        
        # 캐시 파일도 모드별 폴더에 생성하도록 변경
        cache_file = self.work_dir / "translation_cache.json"
        self.cache = TranslationCache(cache_file=str(cache_file)) if cache_enabled else None

    def cleanup(self):
        """작업이 성공적으로 완료되었을 때 임시 캐시 파일을 삭제합니다."""
        if self.cache and self.cache.cache_file:
            try:
                path = Path(self.cache.cache_file)
                if path.exists():
                    path.unlink()
                    log.info(f"임시 번역 캐시 삭제됨: {path.name}")
            except Exception as e:
                log.warning(f"캐시 삭제 실패: {e}")

    def generate_content(self, prompt, temperature=0.3, max_output_tokens=8192):
        """
        BaseLLMBackend 인터페이스 호환을 위한 래퍼 메서드
        """
        return self.translate_with_review(prompt)

    def _cached_generate(self, backend, prompt):
        """캐시를 확인하고 결과가 없으면 백엔드를 호출합니다."""
        if self.cache:
            cached = self.cache.get(backend.model_name, backend.system_instruction, prompt)
            if cached:
                return cached
        
        result = backend.generate_content(prompt)
        if result and self.cache:
            self.cache.set(backend.model_name, backend.system_instruction, prompt, result)
        return result

    def translate_with_review(self, prompt_body, context_info="", char_profile=""):
        """
        1단계: 여러 모델로부터 번역 후보 수집 (병렬 처리)
        2단계: 감수 모델이 후보들을 비교/검토하여 최종 결과 도출
        """
        candidates = []
        
        # 1. 후보 생성 (Parallel)
        with ThreadPoolExecutor(max_workers=len(self.gen_backends)) as executor:
            futures = {executor.submit(self._cached_generate, backend, prompt_body): i for i, backend in enumerate(self.gen_backends)}
            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        candidates.append(res)
                except Exception as e:
                    if is_fatal_llm_error(e):
                        log.error(f"치명적 LLM 인증/권한 오류 발생: {e}")
                        raise
                    log.error(f"후보 생성 중 오류 발생: {e}")

        if not candidates:
            log.warning("생성된 번역 후보가 없습니다. 기본 감수 모델로 단독 번역을 시도합니다.")
            return self._cached_generate(self.review_backend, prompt_body)

        # 2. 최종 감수 (Senior Editor)
        review_prompt = self._build_review_prompt(prompt_body, candidates, context_info, char_profile)
        final_result = self._cached_generate(self.review_backend, review_prompt)
        
        # 에디터의 판단 과정 로깅
        self._log_orchestration(prompt_body, candidates, final_result, context_info, char_profile)
        
        return final_result

    def _log_orchestration(self, original_prompt, candidates, final_result, context_info, char_profile):
        """에디터 모델이 참조한 후보들과 최종 결정을 파일로 저장합니다."""
        try:
            # 프로젝트 루트가 아닌, 모드별 작업 폴더(work_dir) 하위에 생성
            log_dir = self.work_dir / "debug_logs" / "orchestrator"
            log_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            # 파일명에 원문 해시를 추가해 고유성 확보
            prompt_hash = hashlib.md5(original_prompt.encode("utf-8")).hexdigest()[:6]
            log_file = log_dir / f"orch_{timestamp}_{prompt_hash}.md"
            
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"# AI Orchestration Log - {datetime.now().isoformat()}\n\n")
                f.write("## 1. Context & Profiles\n")
                f.write(f"### Character Profiles\n{char_profile}\n\n")
                f.write(f"### Context Info\n{context_info}\n\n")
                
                f.write("## 2. Translation Candidates\n")
                for i, cand in enumerate(candidates):
                    f.write(f"### Candidate {i+1}\n```json\n{cand}\n```\n\n")
                
                f.write("## 3. Final Decision (Senior Editor)\n")
                f.write(f"```json\n{final_result}\n```\n\n")
                
                f.write("---\n*이 로그는 수석 에디터 모델의 판단 과정을 참조하기 위해 자동 생성되었습니다.*")
                
        except Exception as e:
            log.warning(f"오케스트레이션 로그 저장 실패: {e}")

    def _build_review_prompt(self, original_prompt, candidates, context_info, char_profile):
        candidate_blocks = "\n\n".join([f"--- [후보 {i+1}] ---\n{c}" for i, c in enumerate(candidates)])
        
        instructions = f"""
당신은 AAA급 게임 로컬라이제이션 프로젝트를 총괄하는 **수석 에디터(Senior Localization Editor)**입니다.
아래 '원문 및 지시사항'과 여러 AI 번역 모델이 생성한 '번역 후보'들을 면밀히 검토하여, 단 하나의 완벽한 최종 번역본을 확정하십시오.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[9대 감수 기준 — 이 기준들을 종합하여 최종 결정을 내리십시오]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **자연스러움 (Naturalness)**
   - 한국어 원어민이 읽었을 때 어색한 표현, 어색한 어순은 없는가?
   - ⚠️ 텍스트 유형 구분 필수: **대사(Dialogue)**는 구어체 기준, **지문/내레이션(Narration)**은 문어체 기준을 각각 다르게 적용할 것.

2. **정확성 (Accuracy)**
   - 원문의 의미가 훼손되거나 오역된 부분은 없는가?
   - 의역이 필요한 경우, 원문 의도를 우선적으로 보존할 것.

3. **일관성 (Consistency)**
   - 제공된 캐릭터 프로필과 이전 문맥을 고려했을 때, 말투가 흔들리거나 혼재되지 않는가?
   - NPC 계층(군인, 상인, 귀족, 기술자 등)에 따른 경어/반말 구분이 적절한가?

4. **용어 준수 (Terminology)**
   - 제공된 용어집(Glossary)의 단어들이 정확히 사용되었는가?
   - ★ 용어집에 없는 신조어, 고유명사, 설정어가 새로 발견된 경우: JSON 출력 마지막에 `"glossary_suggestions": [{{"en": "...", "ko": "..."}}]` 키로 추가 제안할 것 (용어집 지속 발전을 위해).

5. **텍스트 길이 및 UI 제약 (UI Constraints)**
   - 원문 대비 번역문이 지나치게 길거나 짧지 않은가? (자막, UI 박스 줄 수 고려)
   - 버튼 라벨, 아이템명, HUD 텍스트 등 공간 제약이 큰 텍스트는 간결함을 최우선으로 할 것.

6. **문화적 현지화 (Cultural Localization)**
   - 영어권 관용구나 말장난이 직역되지 않고 한국어 맥락에 맞게 자연스럽게 의역되었는가?
   - 숫자, 단위, 날짜 표기가 한국어 표준(예: "10미터", "3층", "오전 10시")을 따르는가?

7. **캐릭터 보이스 및 감정 톤 (Emotional Voice)**
   - 캐릭터의 감정 상태(분노, 두려움, 유머, 슬픔 등)가 번역문에서도 살아있는가?
   - 원문의 어조(위협적, 친근한, 냉소적 등)가 한국어로 옮겨지면서 희석되지 않았는가?

8. **기술적 태그 및 변수 보존 (Tag Preservation)**
   - `<PlayerName>`, `[FACTION]`, `{{variable}}`, `<font color=...>` 등 게임 엔진 변수 태그가 원문과 완전히 동일하게 유지되었는가?
   - 줄바꿈(`\n`), 색상 태그 등 포맷 요소가 손상되거나 위치가 바뀌지 않았는가?

9. **반복성 및 연속성 (Consistency Across Content)**
   - 같은 아이템, 장소, 캐릭터명이 이 배치 내에서 동일하게 표기되었는가?
   - 이전 챕터나 DLC와의 용어 연속성이 유지되도록 용어집을 기준으로 판단할 것.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[캐릭터 및 문맥 정보]
{char_profile}
{context_info}

[용어집]
{self.glossary_text}

[생성된 번역 후보들]
{candidate_blocks}

[최종 임무]
후보들의 장점만 취합하거나, 필요하다면 위 9대 기준에 따라 직접 수정하여 최상의 번역 결과를 도출하십시오.
결과는 반드시 [후보 1]이 사용한 JSON 구조(Key-Value 매핑)를 완벽히 똑같이 유지하여 반환해야 합니다.
(절대 'BatchID'라는 문자열 자체를 key로 쓰지 마세요. 주어진 B0, B1 등의 고유 식별자를 그대로 사용하십시오.)
새로운 용어 제안이 있다면 `"glossary_suggestions"` 키를 추가할 수 있습니다.
어떠한 부가 설명 없이, 오직 완성된 JSON 데이터만 출력하십시오.
"""
        return instructions
