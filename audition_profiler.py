import os
import json
import subprocess
import logging
import math
import statistics
import wave
from llm_backend import get_llm_backend, GeminiBackend

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("AuditionProfiler")


def _mean(values):
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return sum(clean) / len(clean) if clean else None


def _stdev(values):
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return statistics.stdev(clean) if len(clean) >= 2 else 0.0 if clean else None


def _safe_round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def extract_basic_wav_features(wav_path):
    """표준 라이브러리만으로 duration/RMS를 계산하는 최소 폴백."""
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            channels = max(1, wf.getnchannels())
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            frame_count = wf.getnframes()
        duration = frame_count / framerate if framerate else 0.0
        if sample_width != 2 or not frames:
            return {"duration_sec": _safe_round(duration), "rms": None}

        import array

        samples = array.array("h")
        samples.frombytes(frames)
        if channels > 1:
            samples = array.array("h", samples[::channels])
        scale = 32768.0
        rms = math.sqrt(sum((sample / scale) ** 2 for sample in samples) / max(1, len(samples)))
        return {"duration_sec": _safe_round(duration), "rms": _safe_round(rms, 5)}
    except Exception as exc:
        log.debug(f"기본 WAV 특징 추출 실패: {exc}")
        return {}


def extract_acoustic_features(wav_path, transcript=""):
    """
    librosa/parselmouth가 있으면 정량 음향 특징을 추출하고,
    없으면 wave 기반 duration/RMS만 반환합니다.
    """
    features = extract_basic_wav_features(wav_path)

    try:
        import numpy as np
        import librosa

        y, sr = librosa.load(wav_path, sr=None, mono=True)
        duration = librosa.get_duration(y=y, sr=sr)
        rms = librosa.feature.rms(y=y)[0]
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")

        features.update({
            "duration_sec": _safe_round(duration),
            "rms": _safe_round(float(np.mean(rms)), 5) if len(rms) else features.get("rms"),
            "rms_std": _safe_round(float(np.std(rms)), 5) if len(rms) else None,
            "spectral_centroid_hz": _safe_round(float(np.mean(centroid)), 1) if len(centroid) else None,
            "spectral_centroid_std": _safe_round(float(np.std(centroid)), 1) if len(centroid) else None,
            "onset_rate_per_sec": _safe_round(len(onsets) / duration if duration else None, 2),
        })
    except Exception as exc:
        log.debug(f"librosa 특징 추출 스킵: {exc}")

    try:
        import parselmouth

        sound = parselmouth.Sound(wav_path)
        pitch = sound.to_pitch()
        pitch_values = pitch.selected_array["frequency"]
        voiced_pitch = [float(v) for v in pitch_values if v and math.isfinite(float(v))]
        features.update({
            "f0_mean_hz": _safe_round(_mean(voiced_pitch), 1),
            "f0_std_hz": _safe_round(_stdev(voiced_pitch), 1),
            "f0_min_hz": _safe_round(min(voiced_pitch), 1) if voiced_pitch else None,
            "f0_max_hz": _safe_round(max(voiced_pitch), 1) if voiced_pitch else None,
            "voiced_ratio": _safe_round(len(voiced_pitch) / len(pitch_values), 3) if len(pitch_values) else None,
        })
    except Exception as exc:
        log.debug(f"parselmouth 특징 추출 스킵: {exc}")

    if transcript:
        token_count = len([tok for tok in transcript.replace("\n", " ").split(" ") if tok.strip()])
        duration = features.get("duration_sec") or 0
        features["text_tokens"] = token_count
        features["approx_tokens_per_sec"] = _safe_round(token_count / duration if duration else None, 2)

    return features


def summarize_acoustic_features(sample_features):
    if not sample_features:
        return {}
    numeric_keys = sorted({
        key
        for sample in sample_features
        for key, value in sample.items()
        if isinstance(value, (int, float)) and value is not None
    })
    summary = {"sample_count": len(sample_features)}
    for key in numeric_keys:
        values = [sample.get(key) for sample in sample_features if sample.get(key) is not None]
        summary[f"{key}_mean"] = _safe_round(_mean(values), 3)
        summary[f"{key}_std"] = _safe_round(_stdev(values), 3)
    return summary


def describe_acoustic_summary(summary):
    if not summary:
        return "정량 음향 특징을 추출하지 못했습니다."
    lines = [f"- 샘플 수: {summary.get('sample_count', 0)}"]
    label_map = {
        "f0_mean_hz_mean": "F0 평균(Hz)",
        "f0_std_hz_mean": "F0 변동성(Hz)",
        "voiced_ratio_mean": "유성음 비율",
        "rms_mean": "RMS 에너지",
        "rms_std_mean": "에너지 변동성",
        "spectral_centroid_hz_mean": "스펙트럼 중심(Hz)",
        "onset_rate_per_sec_mean": "초당 onset",
        "approx_tokens_per_sec_mean": "초당 텍스트 토큰",
        "duration_sec_mean": "평균 길이(초)",
    }
    for key, label in label_map.items():
        value = summary.get(key)
        if value is not None:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def parse_profile_json(raw_text):
    if not raw_text:
        return None
    text = str(raw_text).strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[-2].replace("json", "", 1).strip()
    if "{" in text and "}" in text:
        text = text[text.find("{"):text.rfind("}") + 1]
    try:
        return json.loads(text)
    except Exception:
        try:
            from json_repair import repair_json

            return json.loads(repair_json(text))
        except Exception:
            return None


def infer_demographic_hints(speaker, all_texts, acoustic_summary):
    """이름/정량 음향 기반의 약한 성별/연령 힌트. 최종 판정은 LLM/오디오가 한다."""
    hints = []
    lower_name = (speaker or "").lower()
    generic_name = (
        not speaker
        or speaker.lower() in {"unknown", "player"}
        or speaker.startswith("Alias_")
        or speaker.startswith("NPC_")
    )

    male_names = {
        "calvin", "alan", "alexander", "barrett", "benjamin", "damien", "frank",
        "george", "henry", "james", "john", "lin", "marcus", "michael", "robert",
        "sam", "sebastian", "vladimir", "walter",
    }
    female_names = {
        "andreja", "calla", "cora", "emily", "eva", "isabelle", "jane", "jessamine",
        "julia", "laylah", "lydia", "lyria", "maria", "mary", "sarah", "sophia",
        "suzie",
    }

    first_token = lower_name.replace(".", " ").split(" ")[0] if lower_name else ""
    if not generic_name:
        if any(prefix in lower_name for prefix in ["mr ", "mr."]):
            hints.append("이름 호칭상 male 가능성이 있음(약한 힌트).")
        elif any(prefix in lower_name for prefix in ["ms ", "ms.", "mrs ", "mrs."]):
            hints.append("이름 호칭상 female 가능성이 있음(약한 힌트).")
        elif first_token in male_names:
            hints.append(f"이름 '{speaker}'은 일반적으로 male 이름으로 쓰임(약한 힌트).")
        elif first_token in female_names:
            hints.append(f"이름 '{speaker}'은 일반적으로 female 이름으로 쓰임(약한 힌트).")

    f0 = acoustic_summary.get("f0_mean_hz_mean") if acoustic_summary else None
    voiced_ratio = acoustic_summary.get("voiced_ratio_mean") if acoustic_summary else None
    if f0 is None:
        hints.append("F0/pitch를 얻지 못했으므로 정량 음향만으로 gender 판정은 제한적임.")
    elif voiced_ratio is not None and voiced_ratio < 0.25:
        hints.append(f"F0 평균 {f0}Hz가 있으나 유성음 비율 {voiced_ratio}가 낮아 신뢰도 제한.")
    elif f0 < 155:
        hints.append(f"F0 평균 {f0}Hz: adult male 음역 가능성이 상대적으로 높음.")
    elif f0 > 175:
        hints.append(f"F0 평균 {f0}Hz: adult female 음역 가능성이 상대적으로 높음.")
    else:
        hints.append(f"F0 평균 {f0}Hz: male/female 경계역이라 단독 판정 금지.")

    speech_rate = acoustic_summary.get("approx_tokens_per_sec_mean") if acoustic_summary else None
    if speech_rate is not None:
        hints.append(f"발화 속도 {speech_rate} tokens/sec는 스타일 참고용이며 age 판정 근거로는 약함.")

    if "kid" in all_texts.lower() or "child" in all_texts.lower():
        hints.append("대사에 child/kid 단서가 있으나 자기지칭인지 타인지 확인 필요.")

    return "\n".join(f"- {hint}" for hint in hints)


def build_profile_prompt(speaker, all_texts, acoustic_report, acoustic_samples, acoustic_summary):
    acoustic_block = ""
    if acoustic_summary:
        acoustic_block = (
            "\n\n[정량 음향 분석]\n"
            f"{acoustic_report}\n\n"
            "[샘플별 원시 특징]\n"
            f"{json.dumps(acoustic_samples, ensure_ascii=False, indent=2)}\n"
        )

    demographic_hints = infer_demographic_hints(speaker, all_texts, acoustic_summary)
    return (
        f"이 화자의 샘플 대사 전집과 정량 음향 특징을 분석하여 번역용 어투 프로파일을 추출하라.\n\n"
        f"화자명: {speaker}\n"
        f"대사 전집:\n{all_texts}\n"
        f"{acoustic_block}\n\n"
        f"[성별/연령 추론 힌트]\n{demographic_hints or '- 별도 힌트 없음'}\n\n"
        "규칙:\n"
        "- gender/age_group은 번역 톤 보조용 추정값이다. 명백히 불가능한 경우만 unknown을 사용한다.\n"
        "- 이름 힌트는 약한 근거이며, 음성/대사와 충돌하면 음성/대사를 우선한다.\n"
        "- age_group은 목소리와 말투상 체감 연령대이며 생물학적 정확도를 주장하지 않는다.\n"
        "- confidence는 전체 프로파일 신뢰도, gender_confidence/age_confidence는 해당 필드만의 신뢰도다.\n\n"
        "응답은 반드시 아래 JSON 구조와 타입을 유지하라. 설명 없이 JSON만 반환.\n"
        "{\n"
        "  \"tone\": \"professional_informative (string)\",\n"
        "  \"gender\": \"male|female|androgynous|unknown\",\n"
        "  \"gender_confidence\": 0.65,\n"
        "  \"age_group\": \"young_adult|adult|middle_aged|elderly|unknown\",\n"
        "  \"age_confidence\": 0.55,\n"
        "  \"demographic_basis\": \"brief basis string\",\n"
        "  \"speech_style\": \"professional (string)\",\n"
        "  \"honorific_level\": \"존댓말|반말\",\n"
        "  \"acoustic_features\": {\"f0_mean_hz\": 180.0, \"rms\": 0.02, \"speech_rate\": 4.5},\n"
        "  \"confidence\": 0.75,\n"
        "  \"sample_lines\": [\"sample1\", \"sample2\"]\n"
        "}"
    )


def profile_demographics_with_audio(backend, wav_path, speaker, sample_text, all_texts, acoustic_report):
    if not hasattr(backend, "generate_with_audio"):
        return None
    prompt = (
        "첨부 오디오는 게임 대사 한 줄이다. 성별/연령대만 보강 판정하라.\n"
        "텍스트 내용만으로 직업/상황을 과잉 추론하지 말고, 목소리와 발화 느낌을 우선하라.\n\n"
        f"화자명: {speaker}\n"
        f"오디오 샘플 대사: {sample_text}\n"
        f"대사 전집 참고:\n{all_texts}\n\n"
        f"정량 음향 참고:\n{acoustic_report or '없음'}\n\n"
        "JSON만 반환:\n"
        "{\n"
        "  \"gender\": \"male|female|androgynous|unknown\",\n"
        "  \"gender_confidence\": 0.0,\n"
        "  \"age_group\": \"young_adult|adult|middle_aged|elderly|unknown\",\n"
        "  \"age_confidence\": 0.0,\n"
        "  \"demographic_basis\": \"brief vocal basis\"\n"
        "}"
    )
    try:
        return parse_profile_json(backend.generate_with_audio(prompt, wav_path))
    except Exception as exc:
        log.debug(f"오디오 성별/연령 보강 실패: {exc}")
        return None


def needs_demographic_audio_fallback(acoustic_samples):
    if not acoustic_samples:
        return True
    usable_f0 = [
        sample.get("f0_mean_hz")
        for sample in acoustic_samples
        if sample.get("f0_mean_hz") is not None and sample.get("voiced_ratio", 1.0) >= 0.25
    ]
    return not usable_f0


def merge_demographic_profile(profile, demographic):
    if not isinstance(profile, dict) or not isinstance(demographic, dict):
        return profile
    for key, conf_key in [("gender", "gender_confidence"), ("age_group", "age_confidence")]:
        new_value = demographic.get(key)
        if not new_value or new_value == "unknown":
            continue
        old_value = profile.get(key)
        old_conf = float(profile.get(conf_key) or 0)
        new_conf = float(demographic.get(conf_key) or 0)
        if old_value in (None, "", "unknown") or new_conf >= old_conf:
            profile[key] = new_value
            profile[conf_key] = new_conf
    if demographic.get("demographic_basis"):
        profile["demographic_basis"] = demographic["demographic_basis"]
    profile["demographic_audio_fallback_used"] = True
    return profile


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

    audio_cfg = config.get("audio_profile", {})
    max_audio_samples = int(audio_cfg.get("max_samples", config.get("audio_profile_max_samples", 5)))
    use_audio_modality = bool(audio_cfg.get("use_audio_modality", config.get("audio_profile_use_audio_modality", False)))
    demographic_audio_fallback = bool(audio_cfg.get("demographic_audio_fallback", True))

    # 백엔드 초기화
    backend = get_llm_backend(config, "system_prompt_step2", role="audio_profile")
    if mode == "audio" and use_audio_modality and not hasattr(backend, "generate_with_audio"):
        log.error(f"Selected backend ({type(backend).__name__}) does not support audio modality.")
        return
    if mode == "audio" and demographic_audio_fallback and not hasattr(backend, "generate_with_audio"):
        log.info(f"Selected backend ({type(backend).__name__}) has no audio modality. Demographic fallback will use text hints only.")

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

        acoustic_samples = []
        acoustic_summary = {}
        acoustic_report = ""
        demographic_audio_result = None

        if mode == "audio":
            speaker_audition_dir = os.path.join(audition_dir, speaker)
            converted_count = 0
            for sample in samples:
                if converted_count >= max_audio_samples:
                    break
                audio_path = sample.get("AudioPath")
                if not audio_path:
                    continue

                wem_name = os.path.basename(audio_path)
                wem_path = os.path.join(speaker_audition_dir, wem_name)
                wav_path = wem_path.replace(".wem", ".wav")

                if not os.path.exists(wem_path):
                    continue
                if not convert_wem_to_wav(wem_path, wav_path):
                    continue

                try:
                    features = extract_acoustic_features(wav_path, sample.get("Text", ""))
                    if features:
                        features["text"] = sample.get("Text", "")
                        features["audio_file"] = wem_name
                        acoustic_samples.append(features)
                        converted_count += 1

                    if use_audio_modality and hasattr(backend, "generate_with_audio") and not profile_json_result:
                        prompt = (
                            f"이 화자의 샘플 대사 전집과 음성을 분석하라.\n"
                            f"대사 전집: {all_texts}\n\n"
                            f"오디오 기반 샘플 대사: {sample['Text']}\n"
                            f"응답은 반드시 아래 JSON 구조와 타입을 유지하라.\n"
                            f"{{\n"
                            f"  \"tone\": \"friendly_casual (string)\",\n"
                            f"  \"gender\": \"male|female|androgynous|unknown\",\n"
                            f"  \"gender_confidence\": 0.75,\n"
                            f"  \"age_group\": \"young_adult|adult|middle_aged|elderly|unknown\",\n"
                            f"  \"age_confidence\": 0.65,\n"
                            f"  \"demographic_basis\": \"brief vocal basis\",\n"
                            f"  \"speech_style\": \"informal (string)\",\n"
                            f"  \"honorific_level\": \"반말 (string)\",\n"
                            f"  \"sample_lines\": [\"샘플1\", \"샘플2\"]\n"
                            f"}}"
                        )
                        profile_json_result = backend.generate_with_audio(prompt, wav_path)
                    elif (
                        demographic_audio_fallback
                        and hasattr(backend, "generate_with_audio")
                        and not demographic_audio_result
                        and needs_demographic_audio_fallback(acoustic_samples)
                    ):
                        demographic_audio_result = profile_demographics_with_audio(
                            backend,
                            wav_path,
                            speaker,
                            sample.get("Text", ""),
                            all_texts,
                            describe_acoustic_summary(summarize_acoustic_features(acoustic_samples)),
                        )
                except Exception as e:
                    log.error(f"Audio feature extraction failed for {speaker}: {e}")
                finally:
                    if os.path.exists(wav_path): os.remove(wav_path)

            acoustic_summary = summarize_acoustic_features(acoustic_samples)
            acoustic_report = describe_acoustic_summary(acoustic_summary)
            if acoustic_samples:
                log.info(f"{speaker}: 정량 음향 샘플 {len(acoustic_samples)}개 추출 완료")
                
        # Quantitative acoustic interpretation or string fallback.
        if not profile_json_result:
            prompt = build_profile_prompt(speaker, all_texts, acoustic_report, acoustic_samples, acoustic_summary)
            try:
                profile_json_result = backend.generate_content(prompt)
            except Exception as e:
                log.error(f"Text-only profiling failed for {speaker}: {e}")
                
        if profile_json_result:
            parsed_profile = parse_profile_json(profile_json_result)
            try:
                if not parsed_profile:
                    raise ValueError("profile JSON parse failed")
                if demographic_audio_result:
                    parsed_profile = merge_demographic_profile(parsed_profile, demographic_audio_result)
                if acoustic_summary:
                    parsed_profile.setdefault("acoustic_features", acoustic_summary)
                    parsed_profile.setdefault("acoustic_samples", acoustic_samples)
                tone_profiles["speakers"][speaker] = parsed_profile
            except Exception:
                fallback_profile = {"tone": profile_json_result.strip()}
                if acoustic_summary:
                    fallback_profile["acoustic_features"] = acoustic_summary
                    fallback_profile["acoustic_samples"] = acoustic_samples
                tone_profiles["speakers"][speaker] = fallback_profile
                
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
