import os
import json
import subprocess
import logging
from llm_backend import get_llm_backend, GeminiBackend

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AuditionProfiler")

def convert_wem_to_wav(wem_path, wav_path):
    """
    vgmstream-cli 를 사용하여 .wem 을 .wav 로 변환합니다. 
    """
    # 1. 사용자가 지정한 절대 경로 시도
    vgmstream_path = r"D:\vgmstream-win64\vgmstream-cli.exe"
    if os.path.exists(vgmstream_path):
        try:
            subprocess.run([vgmstream_path, "-o", wav_path, wem_path], check=True, capture_output=True)
            return True
        except:
            pass

    # 2. 시스템 PATH의 vgmstream-cli 시도
    try:
        subprocess.run(["vgmstream-cli", "-o", wav_path, wem_path], check=True, capture_output=True)
        return True
    except:
        pass
    
    # 3. ffmpeg 시도
    try:
        subprocess.run(["ffmpeg", "-y", "-i", wem_path, wav_path], check=True, capture_output=True)
        return True
    except:
        pass
    
    return False

def run_profiling(config_path, priority_list_path, audition_dir, output_profile_path):
    if not os.path.exists(config_path):
        log.error(f"Config not found: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    if not os.path.exists(priority_list_path):
        log.error(f"Priority list not found: {priority_list_path}")
        return

    with open(priority_list_path, "r", encoding="utf-8") as f:
        priority_list = json.load(f)

    # 4. 데이터 형식 보정 (List of Quests vs Dict of Speakers)
    if isinstance(priority_list, list):
        log.info("Detected Quest List format. Grouping speaker contexts...")
        grouped = {}
        def _collect(dialogues):
            for d in dialogues:
                spk = d.get("Speaker", "Unknown")
                path = d.get("AudioPath")
                txt = d.get("Text", "").strip()
                if spk and txt:
                    if spk not in grouped: grouped[spk] = []
                    # 중복 방지 (텍스트 위주)
                    if not any(x["Text"] == txt for x in grouped[spk]):
                        grouped[spk].append({"AudioPath": path, "Text": txt})
        
        for q in priority_list:
            for s in q.get("Scenes", []):
                for d in s.get("Dials", []):
                    _collect(d.get("Dialogues", []))
            for d in q.get("StandaloneDials", []):
                _collect(d.get("Dialogues", []))
        priority_list = grouped

    # 백엔드 초기화 (오디오 지원 여부 확인)
    backend = get_llm_backend(config, "system_prompt_step2")
    if not hasattr(backend, "generate_with_audio"):
        log.error(f"Selected backend ({type(backend).__name__}) does not support audio modality.")
        return

    tone_profiles = {}
    
    # 기존 프로파일 로드 (이어하기 지원)
    if os.path.exists(output_profile_path):
        try:
            with open(output_profile_path, "r", encoding="utf-8") as f:
                tone_profiles = json.load(f)
        except:
            pass

    for speaker, samples in priority_list.items():
        if speaker in tone_profiles:
            log.info(f"Speaker {speaker} already profiled. Skipping.")
            continue

        log.info(f"Profiling speaker: {speaker}")
        speaker_audition_dir = os.path.join(audition_dir, speaker)
        
        # 🌟 인물별 모든 대사 텍스트 취합 (문맥 파악용)
        all_texts = "\n".join([f"- {s['Text']}" for s in samples])
        
        # 샘플 중 하나만 사용하여 오디오 특징 파악 (변환 및 프로파일링)
        profile_text = ""
        for sample in samples:
            audio_path = sample.get("AudioPath")
            if not audio_path:
                continue

            wem_name = os.path.basename(audio_path)
            wem_path = os.path.join(speaker_audition_dir, wem_name)
            wav_path = wem_path.replace(".wem", ".wav")
            
            if not os.path.exists(wem_path):
                continue
            
            if not convert_wem_to_wav(wem_path, wav_path):
                log.warning(f"Failed to convert {wem_path}")
                continue
            
            # Gemini 종합 프로파일링 요청 (텍스트 문맥 + 오디오 감정)
            prompt = (
                f"당신은 전문적인 성우 디렉터이자 번역가입니다. 아래 제공된 [캐릭터의 대사 전집]과 [샘플 오디오]를 종합적으로 분석해 "
                f"이 캐릭터의 번역을 위한 '종합 가이드라인'을 작성해줘.\n\n"
                f"[캐릭터의 대사 전집 (문맥/관계 파악용)]:\n{all_texts}\n\n"
                f"[샘플 오디오 대사]: {sample['Text']}\n\n"
                f"분석 지시사항:\n"
                f"1. 오디오에서 느껴지는 성우의 톤, 감정, 사회적 지위, 성격적 특징을 파악하라.\n"
                f"2. 대사 전집을 통해 이 캐릭터가 플레이어(또는 상대방)에게 존댓말을 쓰는지, 혹은 어떤 특수한 말투(반말, 해요체 등)를 쓰는지 결정하라.\n"
                f"3. 위 두 가지를 결합해 향후 번역을 위한 시스템 프롬프트용 가이드(1~2문장)를 생성하라.\n\n"
                f"**응답 형식**: 반드시 아래와 같은 JSON 형식으로만 응답하고 다른 설명은 하지 마십시오.\n"
                f"{{\"text\": \"가이드라인 내용\"}}"
            )
            
            try:
                result = backend.generate_with_audio(prompt, wav_path)
                if result:
                    # JSON response 처리 (정규화 루틴)
                    def extract_text(data):
                        if isinstance(data, str):
                            try:
                                return extract_text(json.loads(data))
                            except:
                                return data
                        if isinstance(data, list) and len(data) > 0:
                            return extract_text(data[0])
                        if isinstance(data, dict):
                            # 우선순위 키 탐색
                            for key in ["text", "translation_guideline", "system_prompt", "guideline"]:
                                val = data.get(key)
                                if val:
                                    if isinstance(val, dict) and "text" in val:
                                        return val["text"]
                                    return extract_text(val)
                            # 키를 못 찾으면 딕셔너리 값 중 가장 긴 문자열 반환 시도
                            str_vals = [str(v) for v in data.values() if isinstance(v, str)]
                            if str_vals: return max(str_vals, key=len)
                        return str(data)

                    if "```" in result:
                        result = result.split("```")[-2].replace("json", "").strip()
                    
                    try:
                        raw_json = json.loads(result)
                        profile_text = extract_text(raw_json)
                    except:
                        profile_text = result.strip()
                    
                    log.info(f"Integrated Profile generated for {speaker}: {profile_text}")
                    break
            except Exception as e:
                log.error(f"Gemini error during profiling: {e}")
            finally:
                # 임시 wav 삭제
                if os.path.exists(wav_path):
                    os.remove(wav_path)
        
        # 🌟 오디오 분석 실패 시 텍스트 전집만으로 분석 (Fallback)
        if not profile_text:
            log.info(f"Audio files missing or failed for {speaker}. Falling back to Text-only profiling.")
            text_prompt = (
                f"당신은 전문적인 번역가입니다. 아래 제공된 [캐릭터의 대사 전집]을 분석해 "
                f"이 캐릭터의 말투와 인물 관계를 정의하는 '번역 가이드라인'을 작성해줘.\n\n"
                f"[대사 전집]:\n{all_texts}\n\n"
                f"분석 지시사항:\n"
                f"1. 이 캐릭터의 성격, 지위, 그리고 상대방(플레이어 등)에 대한 호칭과 종결어미(존대/반말 등)를 파악하라.\n"
                f"2. 향후 번역을 위한 시스템 프롬프트용 가이드(1~2문장)를 생성하라.\n\n"
                f"**응답 형식**: 반드시 아래와 같은 JSON 형식으로만 응답하고 다른 설명은 하지 마십시오.\n"
                f"{{\"text\": \"가이드라인 내용\"}}"
            )
            try:
                result = backend.generate_content(text_prompt) 
                if result:
                    if "```" in result:
                        result = result.split("```")[-2].replace("json", "").strip()
                    try:
                        raw_json = json.loads(result)
                        # 위에서 정의한 동일한 추출 로직 사용
                        def extract_text_simple(data):
                            if isinstance(data, list) and len(data) > 0: return extract_text_simple(data[0])
                            if isinstance(data, dict):
                                for k in ["text", "guideline", "system_prompt"]:
                                    if data.get(k): return data[k]
                                return str(data)
                            return str(data)
                        profile_text = extract_text_simple(raw_json)
                    except:
                        profile_text = result.strip()
                    log.info(f"Text-only Profile generated for {speaker}: {profile_text}")
            except Exception as e:
                log.error(f"Text-only profiling failed for {speaker}: {e}")

        if profile_text:
            tone_profiles[speaker] = profile_text
            # 중간 저장
            with open(output_profile_path, "w", encoding="utf-8") as f:
                json.dump(tone_profiles, f, ensure_ascii=False, indent=2)

    log.info(f"All profiling complete. Results saved to {output_profile_path}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("-p", "--priority-list", default="priority_list.json")
    parser.add_argument("-a", "--audition-dir", default="temp/audition")
    parser.add_argument("-o", "--output", default="tone_profiles.json")
    args = parser.parse_args()
    
    run_profiling(args.config, args.priority_list, args.audition_dir, args.output)
