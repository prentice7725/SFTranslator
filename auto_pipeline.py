import os
import sys
import json
import argparse
import subprocess
import logging
from pathlib import Path

# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("AutoPipeline")

class AutoPipeline:
    def __init__(self, input_esm, config_path="config.json"):
        self.input_esm = Path(input_esm).resolve()
        self.config_path = Path(config_path).resolve()
        self.mod_stem = self.input_esm.stem
        self.work_dir = self.input_esm.parent
        
        # 파일 경로 정의
        self.dump_json = self.work_dir / f"{self.mod_stem}_dump.json"
        self.translated_json = self.work_dir / f"{self.mod_stem}_translated.json"
        self.output_xml = self.work_dir / f"{self.mod_stem}_final.xml"
        self.priority_list = self.work_dir / "priority_list.json"
        self.priority_info = None
        
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
            
        # 세션 크레딧 초기화
        session_file = Path("session_credits.json")
        if session_file.exists():
            session_file.unlink()
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump({"total": 0}, f)

    def run_command(self, cmd_list):
        """서브프로세스로 명령어를 실행합니다."""
        log.info(f"실행 중: {' '.join(cmd_list)}")
        result = subprocess.run([sys.executable] + cmd_list, capture_output=False, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"명령어 실행 실패 (코드 {result.returncode}): {' '.join(cmd_list)}")
        return True

    def execute(self):
        log.info(f"🚀 {self.mod_stem} 자동화 번역 파이프라인 시작...")
        
        # 활성 모델 정보 로깅 (사용자 요청 반영)
        main_model = self.config.get("model_name", "Unknown")
        log.info(f"📍 활성 메인 모델: [{main_model}]")
        
        if self.config.get("orchestrator", {}).get("enabled"):
            models = [m.get("model") for m in self.config["orchestrator"].get("generation_models", [])]
            review_model = self.config["orchestrator"].get("review_model", {}).get("model")
            log.info(f"🧬 오케스트레이터 활성화됨: 후보군({', '.join(models)}) ➔ 최종감수({review_model})")
        else:
            log.info("🔌 오케스트레이터 비활성화됨 (단일 모델 모드)")
        log.info("-" * 60)

        # 1단계: Step 1 (추출)
        log.info("[Step 1] ESM 데이터 추출 및 대화 분석 중...")
        cmd_step1 = [
            "step1_extract_scene.py",
            "-i", str(self.input_esm),
            "-o", str(self.dump_json)
        ]
        if self.config.get("use_ja_ref"):
            cmd_step1.append("--use-ja-ref")
        self.run_command(cmd_step1)

        # 대화 존재 여부 확인
        has_dialogue = False
        if self.dump_json.exists():
            with open(self.dump_json, "r", encoding="utf-8") as f:
                batches = json.load(f)
                if batches and len(batches) > 0:
                    has_dialogue = True

        if has_dialogue:
            log.info("[+] 대화 데이터 발견. Path B (Complex Mode)로 진행합니다.")
            
            # 2단계: 음성 분석 및 프로파일 생성 (Audio Profiling)
            if self.config.get("auto_audio_analysis"):
                log.info("[Audio] 음성 데이터 추출 및 캐릭터 프로파일링 시작...")
                # 음성 추출 (mod 이름과 연동)
                cmd_audio = [
                    "extract_audio.py",
                    "-p", str(self.dump_json), # Step 1에서 나온 dump가 오디오 경로를 포함함
                    "-d", self.config.get("game_data_dir", ""),
                    "-o", str(self.work_dir / "audio_samples")
                ]
                try:
                    self.run_command(cmd_audio)
                    
                    # 프로파일링 (어조 분석)
                    cmd_profile = [
                        "audition_profiler.py",
                        "-c", str(self.config_path),
                        "-p", str(self.dump_json),
                        "-a", str(self.work_dir / "audio_samples"),
                        "-o", str(self.work_dir / "tone_profiles.json")
                    ]
                    self.run_command(cmd_profile)
                    self.priority_info = self.work_dir / "tone_profiles.json"
                except Exception as e:
                    log.warning(f"⚠️ 음성 분석 중 오류 발생 (건너뜀): {e}")

            # 3단계: Step 2 (씬 번역)
            log.info("[Step 2] 씬 번역 및 전용 오케스트레이터 가동...")
            cmd_step2 = [
                "step2_translate_scene.py",
                "-i", str(self.dump_json),
                "-o", str(self.translated_json)
            ]
            if self.priority_info and self.priority_info.exists():
                cmd_step2.extend(["--tone-profiles", str(self.priority_info)])
            if self.config.get("use_ja_ref"):
                cmd_step2.append("--use_ja_ref")
            self.run_command(cmd_step2)
        else:
            log.info("[-] 대화 데이터 없음. Path A (Simple Mode)로 진행합니다.")

        # 3단계: Step 3 (XML 빌드)
        log.info("[Step 3] XML 파일 생성 및 번역 데이터 병합 중...")
        cmd_step3 = [
            "step3_build_xml.py",
            "-i", str(self.input_esm),
            "-o", str(self.output_xml)
        ]
        if self.translated_json.exists():
            cmd_step3.extend(["-t", str(self.translated_json)])
        self.run_command(cmd_step3)

        # 4단계: Step 4 (XML 잔여 번역)
        log.info("[Step 4] 시스템 메시지 및 아이템 이름 등 잔여 데이터 번역 중...")
        # Step 4는 입력을 그대로 출력으로 쓰는 경우가 많으므로 경로 주의
        cmd_step4 = [
            "step4_translate_xml.py",
            "-i", str(self.output_xml),
            "-o", str(self.output_xml)  # 덮어쓰기 방식으로 최종본 완성
        ]
        if self.config.get("use_ja_ref"):
            cmd_step4.append("--use-ja-ref")
        self.run_command(cmd_step4)

        # 6단계: Step 5 (최종 무결성 검사)
        log.info("[Step 5] 최종 번역 무결성 및 태그 검사 중...")
        cmd_step5 = [
            "step5_review_xml.py",
            "-i", str(self.output_xml)
        ]
        self.run_command(cmd_step5)

        log.info(f"✅ 모든 작업이 완료되었습니다! 최종 결과물: {self.output_xml}")

        # 최종 누적 크레딧 리포트
        session_file = Path("session_credits.json")
        if session_file.exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    total = json.load(f).get("total", 0)
                if total > 0:
                    log.info("=" * 60)
                    log.info(f" 💰 [1min.ai 비용 결산] 이번 자동화 파이프라인에서 총 {total} 크레딧을 사용했습니다.")
                    log.info("=" * 60)
            except:
                pass

def main():
    parser = argparse.ArgumentParser(description="Starfield Mod 원클릭 자동 번역 파이프라인")
    parser.add_argument("-i", "--input", required=True, help="원본 ESM/ESP 파일 경로")
    parser.add_argument("-c", "--config", default="config.json", help="설정 파일 경로")
    args = parser.parse_args()

    try:
        pipeline = AutoPipeline(args.input, args.config)
        pipeline.execute()
    except Exception as e:
        log.error(f"❌ 파이프라인 작업 중 치명적 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
