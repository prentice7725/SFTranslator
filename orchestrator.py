import json
import logging
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json_repair

log = logging.getLogger("Orchestrator")

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
    def __init__(self, gen_backends, review_backend, glossary_text="", cache_enabled=True):
        """
        gen_backends: 후보 생성을 담당할 백엔드 리스트 (각기 다른 페르소나 설정 권장)
        review_backend: 최종 감수 및 확정을 담당할 고성능 백엔드
        glossary_text: 준수해야 할 용어집 텍스트
        """
        self.gen_backends = gen_backends
        self.review_backend = review_backend
        self.glossary_text = glossary_text
        self.cache = TranslationCache() if cache_enabled else None

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
                    log.error(f"후보 생성 중 오류 발생: {e}")

        if not candidates:
            log.warning("생성된 번역 후보가 없습니다. 기본 감수 모델로 단독 번역을 시도합니다.")
            return self._cached_generate(self.review_backend, prompt_body)

        # 2. 최종 감수 (Senior Editor)
        review_prompt = self._build_review_prompt(prompt_body, candidates, context_info, char_profile)
        final_result = self._cached_generate(self.review_backend, review_prompt)
        
        return final_result

    def _build_review_prompt(self, original_prompt, candidates, context_info, char_profile):
        candidate_blocks = "\n\n".join([f"--- [후보 {i+1}] ---\n{c}" for i, c in enumerate(candidates)])
        
        instructions = f"""
당신은 게임 로컬라이제이션 팀의 **수석 에디터(Senior Editor)**입니다.
아래 제공된 '원문 및 지시사항'과 여러 AI 모델이 생성한 '번역 후보'들을 검토하여, 가장 완벽한 최종 번역본을 JSON 형태로 확정하십시오.

[검토 및 감수 가이드라인]
1. **자연스러움**: 한국어 원어민이 읽었을 때 어색함이 없는가? (구어체, 대사 톤 중심)
2. **정확성**: 원문의 의미를 훼손하거나 오역한 부분은 없는가?
3. **일관성**: 캐릭터 프로필과 이전 문맥을 고려했을 때 말투가 일관적인가?
4. **용어 준수**: 용어집(Glossary)에 명시된 단어들이 정확히 쓰였는가?

[캐릭터 및 문맥 정보]
{char_profile}
{context_info}

[용어집]
{self.glossary_text}

[생성된 번역 후보들]
{candidate_blocks}

[최종 임무]
후보들의 장점만 취합하거나, 필요하다면 직접 수정하여 최상의 번역 결과를 도출하십시오. 
결과는 반드시 원본 요청과 동일한 JSON 구조(예: {{ "BatchID": "최종번역문" }} 또는 [{{ "id": 0, "result": "..." }}])여야 합니다.
부가적인 설명 없이 오직 JSON 데이터만 출력하십시오.
"""
        return instructions
