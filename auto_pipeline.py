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
    def __init__(self, input_esm, config_path="config.json", start_step=0):
        self.input_esm = Path(input_esm).resolve()
        self.config_path = Path(config_path).resolve()
        self.start_step = start_step
        self.mod_stem = self.input_esm.stem
        self.work_dir = self.input_esm.parent
        
        # [경로 규칙] 모든 중간 파일은 ESM과 동일한 폴더에 생성됨
        self.dump_json = self.work_dir / f"{self.mod_stem}_dump.json"
        self.priority_list = self.work_dir / "priority_list.json"
        self.tone_profiles = self.work_dir / f"{self.mod_stem}_tone_profiles.json"
        self.translated_json = self.work_dir / f"{self.mod_stem}_translated.json"
        self.output_xml = self.work_dir / f"{self.mod_stem}.xml"
        
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

    def run_command(self, cmd_list):
        """서브프로세스로 명령어를 실행합니다. 실패 시 파이프라인 전체를 중단합니다."""
        log.info(f"▶ 실행 중: {' '.join(cmd_list)}")
        try:
            # check=True를 사용하여 에러 발생 시 즉시 중단되도록 보장
            subprocess.run([sys.executable] + cmd_list, check=True, capture_output=False)
        except subprocess.CalledProcessError as e:
            log.error(f"❌ 명령어 실행 중 에러 발생 (코드 {e.returncode}). 작업을 중단합니다.")
            sys.exit(e.returncode)
        return True

    def execute(self):
        log.info(f"🚀 {self.mod_stem} [Step {self.start_step}-6] 파이프라인 시작")
        log.info(f"📍 작업 폴더: {self.work_dir}")
        log.info("-" * 60)

        # Step 0: XML 추출
        if self.start_step <= 0:
            log.info("[Step 0] 원본 XML 데이터 추출 중...")
            self.run_command(["step0_extract_xml.py", "-i", str(self.input_esm), "-o", str(self.work_dir)])

        # Step 1: 씬 데이터 추출
        if self.start_step <= 1:
            log.info("[Step 1] ESM 씬 데이터 및 오디오 리스트 추출 중...")
            cmd_step1 = ["step1_extract_scene.py", "-i", str(self.input_esm), "-o", str(self.dump_json)]
            if self.config.get("use_ja_ref"):
                cmd_step1.append("--use-ja-ref")
            self.run_command(cmd_step1)

        # 🔊 멀티모달 오디오 오디션 통합 로직 (start_step이 1.5 이하이고 분석 파일이 없을 때만 실행)
        if self.start_step <= 1.5 and self.config.get("auto_audio_analysis", True):
            if not self.tone_profiles.exists():
                if self.priority_list.exists():
                    log.info("[Audio] 멀티모달 오디오 분석 엔진 가동 (프로필 미발견)...")
                    global_audition_dir = "temp/audition"
                    
                    # 1. 음성 추출
                    self.run_command([
                        "extract_audio.py",
                        "-p", str(self.priority_list),
                        "-d", str(self.config.get("game_data_dir", "")),
                        "-o", global_audition_dir
                    ])
                    
                    # 2. 프로파일링
                    self.run_command([
                        "audition_profiler.py",
                        "-c", str(self.config_path),
                        "-p", str(self.priority_list),
                        "-a", global_audition_dir,
                        "-o", str(self.tone_profiles)
                    ])
                else:
                    log.warning("⚠️ priority_list.json이 없어 오디오 분석을 수행할 수 없습니다.")
            else:
                log.info(f"✨ 기존 어조 프로필 발견: {self.tone_profiles.name} (분석 건너뜀)")

        # Step 2: 씬 번역 (말투 가이드 주입)
        if self.start_step <= 2:
            log.info("[Step 2] 캐릭터 어조 가이드를 기반으로 씬 번역 중...")
            cmd_step2 = ["step2_translate_scene.py", "-i", str(self.dump_json), "-o", str(self.translated_json)]
            if self.tone_profiles.exists():
                cmd_step2.extend(["--tone-profiles", str(self.tone_profiles)])
            if self.config.get("use_ja_ref"):
                cmd_step2.append("--use-ja-ref")
            self.run_command(cmd_step2)

        # Step 3: XML 빌드
        if self.start_step <= 3:
            log.info("[Step 3] 번역된 데이터로 XML 빌드 중...")
            self.run_command(["step3_build_xml.py", "-i", str(self.input_esm), "-t", str(self.translated_json), "-o", str(self.output_xml)])

        # Step 4: XML 잔여 번역
        if self.start_step <= 4:
            log.info("[Step 4] 시스템 메시지 및 잔여 항목 번역 중...")
            cmd_step4 = ["step4_translate_xml.py", "-i", str(self.output_xml), "-o", str(self.output_xml)]
            if self.config.get("use_ja_ref"):
                cmd_step4.append("--use-ja-ref")
            self.run_command(cmd_step4)

        # Step 5: 리뷰 및 무결성 검사
        if self.start_step <= 5:
            log.info("[Step 5] 최종 번역 무결성 검사 중...")
            self.run_command(["step5_review_xml.py", "-i", str(self.output_xml)])

        # Step 6: 모드 업데이트
        if self.start_step <= 6:
            log.info("[Step 6] 최종 모드 업데이트 마무리 중...")
            self.run_command(["step6_mod_update.py", "-i", str(self.input_esm), "-x", str(self.output_xml)])

        log.info("-" * 60)
        log.info(f"✅ 축하합니다! 모든 작업이 완료되었습니다.")
        log.info(f"📦 최종 결과물: {self.output_xml}")

def main():
    parser = argparse.ArgumentParser(description="Starfield Mod [Step 0-6] 스마트 원클릭 자동 번역")
    parser.add_argument("-i", "--input", required=True, help="원본 ESM/ESP 파일 경로")
    parser.add_argument("-c", "--config", default="config.json", help="설정 파일 경로")
    parser.add_argument("-s", "--step", type=float, default=0, help="시작할 단계 (0~6)")
    args = parser.parse_args()

    try:
        pipeline = AutoPipeline(args.input, args.config, args.step)
        pipeline.execute()
    except Exception as e:
        log.error(f"❌ 파이프라인 작업 중 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
