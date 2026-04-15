import argparse
import json
import shutil
import platform
import os
import sys
from datetime import datetime
from pathlib import Path
from enum import Enum
import sys
from datetime import datetime
from pathlib import Path

from pipeline_runner import (
    EXIT_ARGUMENT_ERROR,
    EXIT_INPUT_MISSING,
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    PipelineManifest,
    STEP_SPECS,
    build_job_paths,
    build_step_command,
    get_step_sequence,
    load_config,
    print_ok,
    require_file,
    run_subprocess,
)


class PipelineStage(Enum):
    INIT = "init"
    STEP1 = "step1"
    BRANCH_DETECT = "branch_detect"
    SELECT_TONE_METHOD = "select_tone_method"
    AUDIO_EXTRACT = "audio_extract"
    TONE_PROFILE = "tone_profile"
    STEP2 = "step2"
    REVIEW_STEP2 = "review_step2"
    STEP3 = "step3"
    STEP4 = "step4"
    REVIEW_XML = "review_xml"
    DONE = "done"

def detect_branch(step1_dump_json: Path) -> str:
    """
    Returns:
        "scene"      - dialogue/quest 있음 -> scene 번역 경로
        "direct_xml" - 내용 없음 -> XML 직행 경로
    """
    if not step1_dump_json.exists():
        return "direct_xml"
    
    try:
        data = json.loads(step1_dump_json.read_text(encoding="utf-8"))
        if data.get("scenes") and len(data["scenes"]) > 0:
            return "scene"
    except Exception:
        pass
    return "direct_xml"

class AutoPipeline:
    def __init__(self, input_esp: str, config_path: str, from_step: str = "step0", include_step6: bool = False, resume: bool = False, work_dir: str | None = None, branch: str | None = None, tone_method: str | None = None, skip_review_step2: bool = False):
        self.paths = build_job_paths(input_esp, work_dir)
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = load_config(self.config_path)
        self.from_step = from_step
        self.include_step6 = include_step6
        self.resume = resume
        self.branch_override = branch
        self.tone_method_override = tone_method
        self.skip_review_step2 = skip_review_step2
        job_id = f"{self.paths.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.manifest = PipelineManifest(self.paths.manifest, job_id=job_id)

    def _step_output_exists(self, step_name: str) -> bool:
        spec = STEP_SPECS[step_name]
        for output_name in spec.outputs:
            output_path = getattr(self.paths, output_name)
            if not Path(output_path).exists():
                return False
        return True

    def _should_skip(self, step_name: str) -> bool:
        if not self.resume:
            return False
        # Resume only skips a step when both the manifest and the expected
        # outputs agree that the step really finished.
        return self.manifest.get_status(step_name) == "done" and self._step_output_exists(step_name)

    def _copy_final_output(self) -> None:
        # The pipeline always publishes one stable "final" file even though
        # the last concrete producer may be Step 5 or Step 6.
        source = self.paths.step6_refined if self.include_step6 and self.paths.step6_refined.exists() else self.paths.step5_reviewed
        if not source.exists():
            return
        self.paths.final_xml.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, self.paths.final_xml)

    def _run_step(self, step_name: str) -> int:
        if step_name == "audio_extract":
            if not self.config.get("auto_audio_analysis", True):
                self.manifest.update_step(step_name, status="skipped", reason="auto_audio_analysis disabled")
                print(f"[SKIP] {step_name}: auto_audio_analysis disabled")
                return EXIT_SUCCESS
            data_dir = self.config.get("game_data_dir")
            if not data_dir:
                self.manifest.update_step(step_name, status="skipped", reason="game_data_dir missing in config")
                print(f"[SKIP] {step_name}: game_data_dir missing in config")
                return EXIT_SUCCESS
            command = build_step_command(
                step_name,
                priority_list=self.paths.step1_priority,
                data_dir=data_dir,
                output_dir=self.paths.step3_audio_dir,
            )
        elif step_name == "audio_profile":
            if not self.config.get("auto_audio_analysis", True):
                self.manifest.update_step(step_name, status="skipped", reason="auto_audio_analysis disabled")
                print(f"[SKIP] {step_name}: auto_audio_analysis disabled")
                return EXIT_SUCCESS
            command = build_step_command(
                step_name,
                config=self.config_path,
                priority_list=self.paths.step1_priority,
                audition_dir=self.paths.step3_audio_dir,
                output=self.paths.audio_tone_profile,
            )
        elif step_name == "step0":
            command = build_step_command(step_name, input_esp=self.paths.input_esp, output_xml=self.paths.step0_xml)
        elif step_name == "step1":
            command = build_step_command(
                step_name,
                input_esp=self.paths.input_esp,
                output_json=self.paths.step1_dump,
                output_priority=self.paths.step1_priority,
                use_ja_ref=self.config.get("use_ja_ref", False),
            )
        elif step_name == "step2":
            input_json = self.paths.step1_dump
            out_json = self.paths.step2_translated
            command = build_step_command(
                step_name,
                input_json=input_json,
                output_json=out_json,
                profile_json=self.paths.step2_profile,
                tone_profile=self.paths.audio_tone_profile if self.paths.audio_tone_profile.exists() else None,
                config=self.config_path,
                use_ja_ref=self.config.get("use_ja_ref", False),
            )
        elif step_name == "review_step2":
            if self.skip_review_step2:
                self.manifest.update_step(step_name, status="skipped", reason="skip_review_step2 flag")
                print(f"[SKIP] {step_name}")
                return EXIT_SUCCESS
            command = build_step_command(
                "step5",
                mode="step2",
                input_json=self.paths.step2_translated,
                output_json=self.paths.step2_reviewed,
                tone_profile=self.paths.audio_tone_profile if self.paths.audio_tone_profile.exists() else None,
                scan_output=self.paths.step2_scan,
                config=self.config,
                config_path=self.config_path
            )
        elif step_name == "step3":
            # branch에 따라 step3 direct_build 여부 결정
            branch_val = self.manifest.data.get("branch_type", "direct_xml")
            direct_build = (branch_val == "direct_xml")
            input_j = self.paths.step2_reviewed if self.paths.step2_reviewed.exists() else self.paths.step2_translated
            
            command = build_step_command(
                "step3",
                input_esp=self.paths.input_esp,
                base_xml=self.paths.step0_xml,
                input_json=input_j if not direct_build else None,
                output_xml=self.paths.step3_merged,
                direct_build=direct_build
            )
        elif step_name == "step4":
            command = build_step_command(
                step_name,
                input_xml=self.paths.step3_merged,
                output_xml=self.paths.step4_translated,
                use_ja_ref=self.config.get("use_ja_ref", False),
            )
        elif step_name == "step5":
            command = build_step_command(
                step_name,
                input_xml=self.paths.step4_translated,
                output_xml=self.paths.step5_reviewed,
                scan_output=self.paths.step5_scan,
            )
        elif step_name == "step6":
            command = build_step_command(
                step_name,
                mode="refine",
                input_file=self.paths.step5_reviewed,
                output_xml=self.paths.step6_refined,
                profile_json=self.paths.step2_profile,
            )
        else:
            raise ValueError(f"Unsupported step: {step_name}")

        # Manifest updates happen around the subprocess boundary so resume can
        # reason about the exact last known state without inspecting logs.
        self.manifest.update_step(step_name, status="running", command=[str(part) for part in command])
        print(f"[RUN] {step_name}: {' '.join(str(part) for part in command)}")
        return_code = run_subprocess(command, cwd=self.paths.work_dir)
        if return_code == 0:
            self.manifest.update_step(step_name, status="done")
        else:
            self.manifest.update_step(step_name, status="failed", return_code=return_code)
        return return_code

    def execute(self) -> int:
        require_file(self.paths.input_esp, "input ESM")
        self.paths.work_dir.mkdir(parents=True, exist_ok=True)

        # 1) 기초 단계 (step0, step1)
        base_steps = ["step0", "step1"]
        
        started = False
        for step_name in base_steps:
            if step_name == self.from_step:
                started = True
            if not started:
                continue
            if self._should_skip(step_name):
                self.manifest.update_step(step_name, status="skipped", reason="resume")
                print(f"[SKIP] {step_name}: already completed")
                continue
            
            # 여기서 branch_detect를 step1 직후에 실행
            return_code = self._run_step(step_name)
            if return_code != 0:
                return return_code
                
            if step_name == "step1":
                branch = self.branch_override or detect_branch(self.paths.step1_dump)
                self.manifest.update_step("branch_detect", status="done")
                self.manifest.data["branch_type"] = branch
                
                tone_method = self.tone_method_override or self.config.get("pipeline", {}).get("tone_profile_method", "audio")
                self.manifest.data["tone_profile_method"] = tone_method
                self.manifest._save()
                print(f"[*] 분기 판정 완료: branch_type={branch}, tone_method={tone_method}")

        # 2) branch 결과에 따른 후속 단계 결정
        branch_type = self.manifest.data.get("branch_type", "direct_xml")
        tone_method = self.manifest.data.get("tone_profile_method", "audio")
        
        subsequent_steps = []
        if branch_type == "scene":
            if tone_method == "audio":
                subsequent_steps.extend(["audio_extract", "audio_profile"])
            elif tone_method == "string":
                subsequent_steps.append("audio_profile")
            
            subsequent_steps.extend(["step2", "review_step2", "step3"])
        else:
            subsequent_steps.append("step3")

        subsequent_steps.extend(["step4", "step5"])
        if self.include_step6:
            subsequent_steps.append("step6")

        for step_name in subsequent_steps:
            # from_step이 여기에 있을 경우 시작 시점 처리
            if not started:
                if step_name == self.from_step:
                    started = True
                else:
                    continue
            
            if self._should_skip(step_name):
                self.manifest.update_step(step_name, status="skipped", reason="resume")
                print(f"[SKIP] {step_name}: already completed")
                continue
            
            # audio_profile: string일 경우 내부적으로 모드 처리
            if step_name == "audio_profile" and tone_method == "string":
                # build_step_command에서 --mode string, --input-json 주입을 위해 _run_step 오버라이드
                # 여기서는 직접 하드코딩
                command = build_step_command(
                    "audio_profile",
                    config=self.config,
                    config_path=self.config_path,
                    mode="string",
                    input_json=self.paths.step1_dump,
                    output=self.paths.audio_tone_profile
                )
                self.manifest.update_step(step_name, status="running", command=[str(part) for part in command])
                print(f"[RUN] {step_name}: {' '.join(str(part) for part in command)}")
                return_code = run_subprocess(command, cwd=self.paths.work_dir)
                if return_code == 0:
                    self.manifest.update_step(step_name, status="done")
                else:
                    self.manifest.update_step(step_name, status="failed", return_code=return_code)
                    return return_code
            else:
                return_code = self._run_step(step_name)
                if return_code != 0:
                    return return_code

        self._copy_final_output()
        if self.paths.final_xml.exists():
            print_ok(self.paths.final_xml)
        return EXIT_SUCCESS


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Thin orchestrator for the standardized Starfield translation pipeline.")
    parser.add_argument("--input-esp", dest="input_esp", required=False, help="Input ESM/ESP path")
    parser.add_argument("-i", "--input", dest="legacy_input", required=False, help="Legacy alias for input ESM/ESP path")
    parser.add_argument("--config", default="config.json", help="Config JSON path")
    parser.add_argument("--from-step", default="step0", choices=get_step_sequence(include_step6=True, include_audio=True), help="Step to start from")
    parser.add_argument("-s", "--step", dest="legacy_step", default=None, help="Legacy start step alias")
    parser.add_argument("--include-step6", action="store_true", help="Include Step 6 refine pass")
    parser.add_argument("--resume", action="store_true", help="Resume from manifest and skip completed steps")
    parser.add_argument("--work-dir", default=None, help="Optional working directory for pipeline outputs")
    parser.add_argument("--branch", choices=["scene", "direct_xml"], help="Branch type override (scene | direct_xml)")
    parser.add_argument("--tone-method", choices=["audio", "string"], help="Tone profile method override (audio | string)")
    parser.add_argument("--skip-review-step2", action="store_true", help="Skip the JSON review step for speed")
    args = parser.parse_args()
    args.input_esp = args.input_esp or args.legacy_input
    if args.legacy_step is not None:
        step_alias_map = {
            "0": "step0",
            "1": "step1",
            "1.5": "audio_extract",
            "2": "step2",
            "3": "step3",
            "4": "step4",
            "5": "step5",
            "6": "step6",
        }
        args.from_step = step_alias_map.get(str(args.legacy_step), args.from_step)
    return args


def main() -> int:
    args = _parse_args()
    if not args.input_esp:
        print("Error: --input-esp is required.", file=sys.stderr)
        return EXIT_ARGUMENT_ERROR
    try:
        pipeline = AutoPipeline(
            input_esp=args.input_esp,
            config_path=args.config,
            from_step=args.from_step,
            include_step6=args.include_step6,
            resume=args.resume,
            work_dir=args.work_dir,
            branch=args.branch,
            tone_method=args.tone_method,
            skip_review_step2=args.skip_review_step2
        )
        return pipeline.execute()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INPUT_MISSING
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
