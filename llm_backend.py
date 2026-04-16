"""
llm_backend.py

이 모듈은 번역 툴 내에서 대형 언어 모델(LLM) API 통신을 추상화하여 담당합니다.
다양한 프로바이더(GCP Vertex AI, Google Gemini API, OpenAI, Local LLM 등)를 지원하며,
요청 실패 시 자동으로 지수 백오프(Exponential Backoff) 방식의 재시도(Retry)를 수행합니다.
또한 실시간 토큰 사용량과 예상 비용을 추적하는 기능을 제공합니다.
"""
import os
import time
import logging
import json
import random
from typing import Dict, Any, List, Optional, Union
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

log = logging.getLogger("LLMBackend")

ROLE_KEY_MAP = {
    "audio_profile": "models.audio_profile",
    "translation":   "models.translation",
    "review":        "models.review",
}

def get_model_for_role(config: dict, role: str) -> str:
    """role = 'audio_profile' | 'translation' | 'review'"""
    if "models" in config and role in config["models"]:
        return config["models"][role]
    # 폴백: 기존 model_name 사용
    return config.get("model_name", "gemini-2.5-flash")

class CostTracker:
    """
    LLM API 호출 시 소모된 입력(Input) 및 출력(Output) 토큰 수를 누적하고,
    사전 정의된 단가표를 바탕으로 예상되는 총 소모 비용(USD)을 계산합니다.
    """
    def __init__(self, model_name):
        self.pricing = {
            "gemini-2.0-flash":     {"input": 0.10,  "output": 0.40},
            "gemini-2.5-flash":     {"input": 0.075, "output": 0.30},
            "gemini-3-flash-preview": {"input": 0.075, "output": 0.30},
            "gemini-2.5-pro":       {"input": 1.25,  "output": 5.00},
            "gpt-4o":               {"input": 2.50,  "output": 10.00},
            "gpt-4o-mini":          {"input": 0.15,  "output": 0.60},
        }
        self.model = model_name
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def add_usage(self, input_tokens, output_tokens):
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    @property
    def estimated_cost(self):
        p = self.pricing.get(self.model)
        if not p:
            return 0.0
        return (self.total_input_tokens / 1_000_000 * p["input"] +
                self.total_output_tokens / 1_000_000 * p["output"])

    def __str__(self):
        return (f"토큰: {self.total_input_tokens:,}in / {self.total_output_tokens:,}out | "
                f"예상 비용: ${self.estimated_cost:.4f}")

class BaseLLMBackend:
    """
    LLM 프로바이더들의 공통된 행동 양식(인터페이스)을 정의하는 베이스 클래스입니다.
    모든 백엔드는 generate_content 메서드를 통해 일관된 텍스트 생성을 보장해야 합니다.
    """
    def __init__(self, model_name, system_instruction, max_retries=3, retry_base_wait=60):
        self.model_name = model_name
        self.system_instruction = system_instruction
        self.max_retries = max_retries
        self.retry_base_wait = retry_base_wait
        self.cost_tracker = CostTracker(model_name)
    
    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192) -> str | None:
        raise NotImplementedError

class VertexBackend(BaseLLMBackend):
    _RETRY_KEYWORDS = ('429', 'resourceexhausted', 'quota', 'internalservererror', '500', 'serviceunavailable', '503', 'deadline', 'timeout')
    
    def __init__(self, project_id, location, model_name, system_instruction, max_retries=3, retry_base_wait=60):
        super().__init__(model_name, system_instruction, max_retries, retry_base_wait)
        import vertexai
        from vertexai.generative_models import GenerativeModel, SafetySetting, HarmCategory, HarmBlockThreshold
        # 런타임 패치: SDK의 리전 검증 목록에 'global' 강제 추가 (필요시)
        try:
            from vertexai import constants
            if hasattr(constants, 'SUPPORTED_REGIONS') and location not in constants.SUPPORTED_REGIONS:
                new_regions = set(constants.SUPPORTED_REGIONS)
                new_regions.add(location)
                constants.SUPPORTED_REGIONS = frozenset(new_regions)
        except:
            pass
            
        # 방법 2 적용: api_endpoint를 직접 지정하여 global 리전 접속 보장
        endpoint = "global-aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        vertexai.init(project=project_id, location=location, api_endpoint=endpoint)
        self.safety_settings = [
            SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_NONE),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_NONE),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_NONE),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_NONE),
        ]
        # 모델 생성 (vertexai.init에서 설정한 전역 설정을 자동으로 상속)
        self.model = GenerativeModel(
            self.model_name, 
            system_instruction=self.system_instruction
        )
        
    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192, response_mime_type: str = "application/json") -> str | None:
        generation_config = {
            "temperature": temperature, 
            "response_mime_type": response_mime_type,
            "max_output_tokens": max_output_tokens
        }
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.model.generate_content(prompt, generation_config=generation_config, safety_settings=self.safety_settings)
                if not response.candidates: return None
                if hasattr(response, 'usage_metadata') and response.usage_metadata:
                    self.cost_tracker.add_usage(response.usage_metadata.prompt_token_count or 0, response.usage_metadata.candidates_token_count or 0)
                
                try:
                    text = response.text
                    return text
                except ValueError as ve:
                    reason = getattr(response.candidates[0], "finish_reason", "UNKNOWN")
                    reason_name = getattr(reason, "name", str(reason))
                    if "MAX_TOKENS" in reason_name or "max_tokens" in str(ve).lower():
                        log.warning(f"MAX_TOKENS 에러: 텍스트/결과가 너무 깁니다. 분할 재시도를 유도하기 위해 예외를 발생시킵니다.")
                        raise ValueError("MAX_TOKENS")
                    elif "SAFETY" in reason_name or "candidate content has no parts" in str(ve).lower():
                        log.warning(f"콘텐츠 차단됨 (시도 {attempt}) - 안전 필터 원인: {reason_name}")
                        raise ValueError("SAFETY_BLOCK_RETRY")
                    else:
                        raise ve
                        
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                if "credentials" in err_str or "defaultcredentialserror" in err_str:
                    log.error(f"치명적 인증 오류 발생: {e}")
                    raise e
                    
                retry_cond = (any(kw in err_str for kw in self._RETRY_KEYWORDS) or "safety_block_retry" in err_str)
                if retry_cond:
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    log.warning(f"일시적 오류 또는 검열 차단 (시도 {attempt}), {wait}초 대기: {str(e)[:100]}")
                    time.sleep(wait)
                    continue
                else:
                    raise e
                    
        if last_exc: 
            log.error("최대 재시도 횟수 초과. 스킵합니다.")
            return None
        return None

    def generate_with_audio(self, prompt: str, audio_path: str, temperature=0.3) -> str | None:
        """
        오디오 파일과 텍스트 프롬프트를 함께 Vertex AI LLM에 전달하여 분석 결과를 얻습니다.
        """
        from vertexai.generative_models import Part
        
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with open(audio_path, "rb") as f:
                    audio_data = f.read()
                
                audio_part = Part.from_data(data=audio_data, mime_type="audio/wav")
                
                generation_config = {
                    "temperature": temperature,
                    "response_mime_type": "application/json"
                }
                
                response = self.model.generate_content(
                    [audio_part, prompt],
                    generation_config=generation_config,
                    safety_settings=self.safety_settings
                )
                
                if not response.candidates: return None
                return response.text
                
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                if any(kw in err_str for kw in self._RETRY_KEYWORDS):
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    time.sleep(wait)
                    continue
                else:
                    raise e
        return None

class GeminiBackend(BaseLLMBackend):
    _RETRY_KEYWORDS = ('429', 'resourceexhausted', 'quota', 'internalservererror', '500', 'serviceunavailable', '503', 'deadline', 'timeout')

    def __init__(self, api_key, model_name, system_instruction, max_retries=3, retry_base_wait=60):
        super().__init__(model_name, system_instruction, max_retries, retry_base_wait)
        import google.generativeai as genai
        from google.generativeai.types import HarmCategory as GenaiHarmCategory, HarmBlockThreshold as GenaiHarmBlockThreshold
        genai.configure(api_key=api_key)
        self.safety_settings = {
            GenaiHarmCategory.HARM_CATEGORY_HATE_SPEECH: GenaiHarmBlockThreshold.BLOCK_NONE,
            GenaiHarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: GenaiHarmBlockThreshold.BLOCK_NONE,
            GenaiHarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: GenaiHarmBlockThreshold.BLOCK_NONE,
            GenaiHarmCategory.HARM_CATEGORY_HARASSMENT: GenaiHarmBlockThreshold.BLOCK_NONE,
        }
        self.model = genai.GenerativeModel(self.model_name, system_instruction=self.system_instruction)

    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192) -> str | None:
        import google.generativeai as genai
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens
        )
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.model.generate_content(prompt, generation_config=generation_config, safety_settings=self.safety_settings)
                if not response.candidates: return None
                if hasattr(response, 'usage_metadata') and response.usage_metadata:
                    self.cost_tracker.add_usage(response.usage_metadata.prompt_token_count or 0, response.usage_metadata.candidates_token_count or 0)
                
                try:
                    text = response.text
                    return text
                except ValueError as ve:
                    reason = getattr(response.candidates[0], "finish_reason", "UNKNOWN")
                    reason_name = getattr(reason, "name", str(reason))
                    if "MAX_TOKENS" in reason_name or "max_tokens" in str(ve).lower():
                        log.warning(f"MAX_TOKENS 에러: 텍스트/결과가 너무 깁니다. 분할 재시도를 유도하기 위해 예외를 발생시킵니다.")
                        raise ValueError("MAX_TOKENS")
                    elif "SAFETY" in reason_name or "candidate content has no parts" in str(ve).lower():
                        log.warning(f"콘텐츠 차단됨 (시도 {attempt}) - 안전 필터 원인: {reason_name}")
                        raise ValueError("SAFETY_BLOCK_RETRY")
                    else:
                        raise ve
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                retry_cond = (any(kw in err_str for kw in self._RETRY_KEYWORDS) or "safety_block_retry" in err_str)
                if retry_cond:
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    log.warning(f"일시적 오류 또는 검열 차단 (시도 {attempt}), {wait}초 대기: {str(e)[:100]}")
                    time.sleep(wait)
                    continue
                else:
                    raise e
        
        if last_exc: 
            log.error("최대 재시도 횟수 초과. 스킵합니다.")
            return None
        return None

    def generate_with_audio(self, prompt: str, audio_path: str, temperature=0.3) -> str | None:
        """
        오디오 파일과 텍스트 프롬프트를 함께 LLM에 전달하여 분석 결과를 얻습니다.
        (Gemini 1.5 Pro/Flash 전용)
        """
        import google.generativeai as genai
        
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                # 오디오 파일 업로드 (임시)
                print(f"Uploading audio for profiling: {audio_path}")
                audio_file = genai.upload_file(path=audio_path)
                
                generation_config = genai.types.GenerationConfig(
                    temperature=temperature,
                    response_mime_type="application/json"
                )
                
                response = self.model.generate_content(
                    [audio_file, prompt],
                    generation_config=generation_config,
                    safety_settings=self.safety_settings
                )
                
                # 업로드된 파일 삭제 (권장 사항)
                genai.delete_file(audio_file.name)

                if not response.candidates: return None
                return response.text
                
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                if any(kw in err_str for kw in self._RETRY_KEYWORDS):
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    time.sleep(wait)
                    continue
                else:
                    raise e
        return None

class OpenAIBackend(BaseLLMBackend):
    _RETRY_KEYWORDS = ('429', 'rate_limit_exceeded', '500', '503', 'timeout')

    def __init__(self, api_key, model_name, system_instruction, max_retries=3, retry_base_wait=60):
        super().__init__(model_name, system_instruction, max_retries, retry_base_wait)
        import openai
        self.client = openai.OpenAI(api_key=api_key)
        
    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192) -> str | None:
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self.system_instruction},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=temperature,
                    max_tokens=max_output_tokens
                )
                if not response.choices: return None
                
                content = response.choices[0].message.content
                if response.usage:
                    self.cost_tracker.add_usage(response.usage.prompt_tokens, response.usage.completion_tokens)
                
                return content
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                retry_cond = any(kw in err_str for kw in self._RETRY_KEYWORDS)
                if retry_cond:
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    log.warning(f"일시적 오류 (시도 {attempt}), {wait}초 대기: {str(e)[:100]}")
                    time.sleep(wait)
                    continue
                else:
                    raise e
        
        if last_exc: 
            log.error("최대 재시도 횟수 초과. 스킵합니다.")
            return None
        return None

class LocalLLMBackend(BaseLLMBackend):
    _RETRY_KEYWORDS = ('429', 'rate_limit_exceeded', '500', '503', 'timeout', 'connection error')

    def __init__(self, base_url, api_key, model_name, system_instruction, max_retries=3, retry_base_wait=60):
        super().__init__(model_name, system_instruction, max_retries, retry_base_wait)
        import openai
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key or "sk-localllm")
        
    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192) -> str | None:
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self.system_instruction},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=temperature,
                    max_tokens=max_output_tokens
                )
                if not response.choices: return None
                
                content = response.choices[0].message.content
                if hasattr(response, 'usage') and response.usage:
                    try:
                        self.cost_tracker.add_usage(response.usage.prompt_tokens, response.usage.completion_tokens)
                    except Exception as e:
                        log.debug(f"로컬 LLM 토큰 사용량 파싱 스킵 (무시 가능): {e}")
                
                return content
            except Exception as e:
                err_str = str(e).lower()
                last_exc = e
                retry_cond = any(kw in err_str for kw in self._RETRY_KEYWORDS)
                if retry_cond:
                    wait = self.retry_base_wait * (2 ** (attempt - 1))
                    log.warning(f"일시적 오류 (시도 {attempt}), {wait}초 대기: {str(e)[:100]}")
                    time.sleep(wait)
                    continue
                else:
                    raise e
        
        if last_exc: 
            log.error("최대 재시도 횟수 초과. 스킵합니다.")
            return None
        return None

import requests

class Min1AIBackend(BaseLLMBackend):
    _RETRY_KEYWORDS = ('429', 'rate_limit_exceeded', '500', '503', 'timeout', 'internal_error')
    total_used_credit = 0  # 세션 내 누적 크레딧 추적

    def __init__(self, api_key, model_name, system_instruction, max_retries=5, retry_base_wait=60):
        super().__init__(model_name, system_instruction, max_retries, retry_base_wait)
        self.api_key = api_key
        self.base_url = "https://api.1min.ai/api/chat-with-ai"
        self.headers = {
            "Content-Type": "application/json",
            "API-KEY": self.api_key
        }

    def _extract_text_response(self, data: Dict[str, Any]) -> str:
        """1min.ai Unified API 응답 구조에서 텍스트 내용을 추출합니다."""
        # Unified API 구조: data["aiRecord"]["aiRecordDetail"]["resultObject"] -> List[str]
        ai_record = data.get("aiRecord", {})
        detail = ai_record.get("aiRecordDetail", {})
        result_obj = detail.get("resultObject", [])

        if isinstance(result_obj, list) and len(result_obj) > 0:
            return result_obj[0]
            
        # 심의(검열) 걸리는 경우 resultObject가 dict 형태로 반환됨
        if isinstance(result_obj, dict) and "code" in result_obj:
            error_msg = f"1min.ai API Error [{result_obj.get('code')}]: {result_obj.get('message')}"
            raise RuntimeError(error_msg)
        
        # 폴백: 하위 호환성 유지 (혹시라도 responseObject가 올 경우)
        response_obj = detail.get("responseObject", {})
        if isinstance(response_obj, str):
            return response_obj
        if isinstance(response_obj, dict):
            return response_obj.get("content") or response_obj.get("text") or ""

        if "result" in data and data["result"]:
            return data["result"]
            
        return ""

    def generate_content(self, prompt: str, temperature=0.3, max_output_tokens=8192) -> str | None:
        # 사용자 제안 반영: system 지침을 별도 필드로 분리하여 500 에러 방지 시도
        payload = {
            "type": "UNIFY_CHAT_WITH_AI",
            "model": self.model_name,
            "promptObject": {
                "prompt": prompt,
                "system": self.system_instruction,
                "settings": {
                    "historySettings": {"isMixed": False},
                    "webSearchSettings": {"webSearch": False}
                }
            }
        }
        
        # URL에 non-streaming 명시
        target_url = f"{self.base_url}?isStreaming=false"
        
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(target_url, headers=self.headers, json=payload, timeout=120)
                
                if response.status_code == 429:
                    wait = min(2 ** attempt + random.random() * 5, 30)
                    log.warning(f"1min.ai Rate Limit (429) 감지. {wait:.1f}초 대기 후 재시도...")
                    time.sleep(wait)
                    continue
                    
                response.raise_for_status()
                data = response.json()
                
                # 크레딧 정보 추출 (사용자 요청 반영: usedCredit, creditLimit 등 상세 정보 로깅)
                metadata = data.get("metadata", {})
                batch_credit = metadata.get("credit", 0)
                
                ai_record = data.get("aiRecord", {})
                team_user = ai_record.get("teamUser", {})
                remaining_credit = team_user.get("creditLimit", 0) - team_user.get("usedCredit", 0)

                Min1AIBackend.total_used_credit += batch_credit
                log.info(f"[1min.ai] Model: {self.model_name} | 소모: {batch_credit} | 남은 잔액: {remaining_credit} | 세션 누적: {Min1AIBackend.total_used_credit}")

                # 프로세스 간 크레딧 공유를 위해 파일에 기록
                try:
                    session_file = "session_credits.json"
                    current_total = 0
                    if os.path.exists(session_file):
                        with open(session_file, "r", encoding="utf-8") as f:
                            current_total = json.load(f).get("total", 0)
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump({"total": current_total + batch_credit}, f)
                except:
                    pass

                # 최종 텍스트 추출
                content = self._extract_text_response(data)
                
                if not content:
                    log.warning(f"1min.ai 응답 내용이 비어 있습니다. (시도 {attempt})")
                    log.error(f"🔍 1min.ai 원시 응답(Raw Response): {json.dumps(data, ensure_ascii=False)}")
                    raise ValueError("EMPTY_RESPONSE")
                
                return content
                
            except Exception as e:
                # 400/500 에러 시 어떤 모델에서 문제가 생겼는지 명시
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        err_body = e.response.json()
                        log.error(f"1min.ai API 상세 에러 (HTTP {e.response.status_code}) [Model: {self.model_name}]: {err_body}")
                    except:
                        log.error(f"1min.ai API 상세 에러 (HTTP {e.response.status_code}) [Model: {self.model_name}]: {e.response.text}")
                
                # 재시도 조건 판별
                err_str = str(e).lower()
                last_exc = e
                retry_cond = any(kw in err_str for kw in self._RETRY_KEYWORDS) or (hasattr(e, 'response') and e.response is not None and e.response.status_code in [500, 502, 503, 504])
                
                if retry_cond:
                    # 500 에러는 짧게(10~15초), 429/기타는 지수 백오프 유지
                    is_500 = hasattr(e, 'response') and e.response is not None and e.response.status_code == 500
                    if is_500:
                        wait = 10 + (random.random() * 5)
                    else:
                        wait = self.retry_base_wait * (2 ** (attempt - 1)) + (random.random() * 2)
                    
                    log.warning(f"1min.ai 일시적 오류 (시도 {attempt}) [Model: {self.model_name}], {wait:.1f}초 대기 후 재시도: {str(e)[:100]}")
                    time.sleep(wait)
                    continue
                else:
                    raise e
                    
        if last_exc: 
            log.error("1min.ai 최대 재시도 횟수 초과. 스킵합니다.")
            return None
        return None

    def _upload_file(self, file_path: str) -> Dict[str, Any] | None:
        """1min.ai 서버에 파일을 업로드하고 메타데이터를 반환합니다."""
        upload_url = "https://api.1min.ai/api/files/upload?isFullData=true"
        try:
            with open(file_path, "rb") as f:
                files = {"file": f}
                response = requests.post(upload_url, headers={"API-KEY": self.api_key}, files=files)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            log.error(f"1min.ai file upload failed: {e}")
            return None

    def generate_with_audio(self, prompt: str, audio_path: str) -> str | None:
        """오디오 파일을 첨부하여 1min.ai에 분석 요청을 보냅니다."""
        # 1. 파일 업로드
        file_info = self._upload_file(audio_path)
        if not file_info:
            return None
        
        # 2. 업로드된 파일 정보를 포함하여 채팅 요청
        payload = {
            "type": "UNIFY_CHAT_WITH_AI",
            "model": self.model_name,
            "promptObject": {
                "prompt": prompt,
                "system": self.system_instruction,
                "settings": {
                    "historySettings": {"isMixed": False},
                    "webSearchSettings": {"webSearch": False}
                },
                "attachments": {
                    "files": [
                        {
                            "id": file_info.get("uuid"),
                            "name": file_info.get("name"),
                            "type": file_info.get("type"),
                            "url": file_info.get("url")
                        }
                    ],
                    "images": []
                }
            }
        }
        
        target_url = f"{self.base_url}?isStreaming=false"
        try:
            response = requests.post(target_url, headers=self.headers, json=payload, timeout=180)
            response.raise_for_status()
            data = response.json()
            return self._extract_text_response(data)
        except Exception as e:
            log.error(f"1min.ai generate_with_audio failed: {e}")
            return None

def _deobfuscate(text: str) -> str:
    if not text: return text
    try:
        import base64
        decoded = base64.b64decode(text.encode('utf-8')).decode('utf-8')
        key = "STARFIELD"
        deobfuscated = "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
        return deobfuscated
    except:
        return text

def get_llm_backend(config_dict, step_prompt_key, role=None, max_retries=3, retry_base_wait=60):
    # 신규 'provider' 필드 우선, 없으면 기존 'api_provider' 사용
    provider = config_dict.get("provider") or config_dict.get("api_provider", "vertexai")
    
    # [보강] vertexai_gemini 등의 별칭도 vertexai 공식 명칭으로 매핑
    if str(provider).lower() in ["vertexai_gemini", "vertex_ai", "google", "vertexai"]:
        provider = "vertexai"

    # role이 주어지면 해당 역할에 맞는 모델 선택, 아니면 기본 model_name 사용
    if role:
        model_name = get_model_for_role(config_dict, role)
    else:
        model_name = config_dict.get("model_name", "gemini-2.5-flash")
    
    system_instruction = config_dict.get(step_prompt_key, "")
    
    if provider == "vertexai":
        project_id = os.getenv("GCP_PROJECT_ID") or config_dict.get("gcp_project_id", "")
        location = os.getenv("GCP_LOCATION") or config_dict.get("gcp_location", "asia-northeast1")
        
        key_json_path = os.getenv("GCP_KEY_JSON") or config_dict.get("gcp_key_json", "")
        if key_json_path and os.path.exists(key_json_path):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_json_path
            
        return VertexBackend(project_id, location, model_name, system_instruction, max_retries, retry_base_wait)
    elif provider == "gemini":
        env_key = os.getenv("GEMINI_API_KEY")
        api_key = _deobfuscate(env_key) if env_key else _deobfuscate(config_dict.get("gemini_api_key", ""))
        return GeminiBackend(api_key, model_name, system_instruction, max_retries, retry_base_wait)
    elif provider == "openai":
        env_key = os.getenv("OPENAI_API_KEY")
        api_key = _deobfuscate(env_key) if env_key else _deobfuscate(config_dict.get("openai_api_key", ""))
        return OpenAIBackend(api_key, model_name, system_instruction, max_retries, retry_base_wait)
    elif provider == "localllm":
        base_url = os.getenv("LOCALL_LLM_BASE_URL") or config_dict.get("localllm_base_url", "http://localhost:11434/v1")
        env_key = os.getenv("LOCALL_LLM_API_KEY")
        api_key = _deobfuscate(env_key) if env_key else _deobfuscate(config_dict.get("localllm_api_key", ""))
        return LocalLLMBackend(base_url, api_key, model_name, system_instruction, max_retries, retry_base_wait)
    elif provider == "1minai":
        env_key = os.getenv("MIN1AI_API_KEY")
        api_key = _deobfuscate(env_key) if env_key else _deobfuscate(config_dict.get("1minai_api_key", ""))
        return Min1AIBackend(api_key, model_name, system_instruction, max_retries, retry_base_wait)
    else:
        raise ValueError(f"Unknown API provider: {provider}")

