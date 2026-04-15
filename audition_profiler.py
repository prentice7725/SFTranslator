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

def run_profiling(config_path, priority_list_path, audition_dir, output_profile_path, mode="audio", input_json_path=None):
    if not os.path.exists(config_path):
        log.error(f"Config not found: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 1. 입력 데이터 로드 로직
    priority_list = {}
    if mode == "string" and input_json_path and os.path.exists(input_json_path):
        log.info(f"Using string mode with input: {input_json_path}")
        with open(input_json_path, "r", encoding="utf-8") as f:
            dump_data = json.load(f)
            
        for scene in dump_data.get("scenes", []):
            for line in scene.get("lines", []):
                spk = line.get("speaker_name", "Unknown")
                txt = line.get("english", "").strip()
                if not spk or not txt: continue
                if spk not in priority_list:
                    priority_list[spk] = []
                if len(priority_list[spk]) < 20 and not any(x.get("Text") == txt for x in priority_list[spk]):
                    priority_list[spk].append({"Text": txt})
    else:
        if not os.path.exists(priority_list_path):
            log.error(f"Priority list not found: {priority_list_path}")
            return
    
        with open(priority_list_path, "r", encoding="utf-8") as f:
            priority_list = json.load(f)
    
        # 데이터 형식 보정 (List of Quests vs Dict of Speakers)
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

    # 백엔드 초기화
    backend = get_llm_backend(config, "system_prompt_step2", role="audio_profile")
    if not hasattr(backend, "generate_with_audio"):
        log.error(f"Selected backend ({type(backend).__name__}) does not support audio modality.")
        return

    tone_profiles = {"speakers": {}}
    
    # 기존 프로파일 로드 (이어하기 지원)
    if os.path.exists(output_profile_path):
        try:
            with open(output_profile_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if "speakers" in loaded:
                    tone_profiles = loaded
                else:
                    tone_profiles["speakers"] = loaded # 구버전 호환
        except:
            pass

    for speaker, samples in priority_list.items():
        if speaker in tone_profiles["speakers"]:
            log.info(f"Speaker {speaker} already profiled. Skipping.")
            continue

        log.info(f"Profiling speaker: {speaker}")
        
        # 인물별 모든 대사 텍스트 취합 (문맥 파악용)
        all_texts = "\n".join([f"- {s['Text']}" for s in samples][:20]) # 최대 20개
        profile_json_result = None

        if mode == "audio":
            speaker_audition_dir = os.path.join(audition_dir, speaker)
            if not hasattr(backend, "generate_with_audio"):
                log.warning("Backend does not support audio, fallback to text mode.")
            else:
                for sample in samples:
                    audio_path = sample.get("AudioPath")
                    if not audio_path: continue
        
                    wem_name = os.path.basename(audio_path)
                    wem_path = os.path.join(speaker_audition_dir, wem_name)
                    wav_path = wem_path.replace(".wem", ".wav")
                    
                    if not os.path.exists(wem_path): continue
                    if not convert_wem_to_wav(wem_path, wav_path): continue
                    
                    prompt = (
                        f"이 화자의 샘플 대사 전집과 음성을 분석하라.\n"
                        f"대사 전집: {all_texts}\n\n"
                        f"오디오 기반 샘플 대사: {sample['Text']}\n"
                        f"응답은 반드시 아래 JSON 구조와 타입을 유지하라.\n"
                        f"{{\n"
                        f"  \"tone\": \"friendly_casual (string)\",\n"
                        f"  \"gender\": \"female (string)\",\n"
                        f"  \"age_group\": \"young_adult (string)\",\n"
                        f"  \"speech_style\": \"informal (string)\",\n"
                        f"  \"honorific_level\": \"반말 (string)\",\n"
                        f"  \"sample_lines\": [\"샘플1\", \"샘플2\"]\n"
                        f"}}"
                    )
                    try:
                        res = backend.generate_with_audio(prompt, wav_path)
                        if res:
                            profile_json_result = res
                            break
                    except Exception as e:
                        log.error(f"Gemini error during audio profiling: {e}")
                    finally:
                        if os.path.exists(wav_path): os.remove(wav_path)
                
        # String fallback or string mode
        if not profile_json_result:
            prompt = (
                f"이 화자의 샘플 대사 전집을 분석하여 톤과 특징을 추출하라.\n\n"
                f"대사 전집: {all_texts}\n\n"
                f"응답은 반드시 아래 JSON 구조와 타입을 유지하라. 설명 없이 JSON만 반환.\n"
                f"{{\n"
                f"  \"tone\": \"friendly_casual (string)\",\n"
                f"  \"gender\": \"female (string)\",\n"
                f"  \"age_group\": \"young_adult (string)\",\n"
                f"  \"speech_style\": \"informal (string)\",\n"
                f"  \"honorific_level\": \"반말 (string)\",\n"
                f"  \"sample_lines\": [\"샘플1\", \"샘플2\"]\n"
                f"}}"
            )
            try:
                profile_json_result = backend.generate_content(prompt)
            except Exception as e:
                log.error(f"Text-only profiling failed for {speaker}: {e}")
                
        if profile_json_result:
            import re
            if "```" in profile_json_result:
                profile_json_result = profile_json_result.split("```")[-2].replace("json", "").strip()
            # fallback find {}
            elif "{" in profile_json_result:
                profile_json_result = profile_json_result[profile_json_result.find("{"):profile_json_result.rfind("}")+1]
                
            try:
                parsed_profile = json.loads(profile_json_result)
                tone_profiles["speakers"][speaker] = parsed_profile
            except:
                tone_profiles["speakers"][speaker] = {"tone": profile_json_result.strip()}
                
            # 중간 저장
            with open(output_profile_path, "w", encoding="utf-8") as f:
                json.dump(tone_profiles, f, ensure_ascii=False, indent=2)

    log.info(f"All profiling complete. Results saved to {output_profile_path}")

if __name__ == "__main__":
    import argparse
    from pipeline_runner import print_ok
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("-p", "--priority-list", default="priority_list.json")
    parser.add_argument("-a", "--audition-dir", default="temp/audition")
    parser.add_argument("-o", "--output", default="tone_profiles.json")
    parser.add_argument("--mode", choices=["audio", "string"], default="audio")
    parser.add_argument("--input-json", default=None)
    args = parser.parse_args()
    
    run_profiling(args.config, args.priority_list, args.audition_dir, args.output, args.mode, args.input_json)
    print_ok(args.output)
