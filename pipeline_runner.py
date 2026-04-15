from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

EXIT_SUCCESS = 0
EXIT_ARGUMENT_ERROR = 2
EXIT_INPUT_MISSING = 3
EXIT_OUTPUT_FAILURE = 4
EXIT_INTERNAL_ERROR = 5


@dataclass(frozen=True)
class PipelinePaths:
    input_esp: Path
    work_dir: Path
    stem: str
    step0_xml: Path
    step1_dump: Path
    step1_priority: Path
    audio_tone_profile: Path
    step3_audio_dir: Path
    step2_translated: Path
    step2_profile: Path
    step2_reviewed: Path
    step2_scan: Path
    step3_merged: Path
    step4_translated: Path
    step5_reviewed: Path
    step5_scan: Path
    step6_refined: Path
    final_xml: Path
    manifest: Path


@dataclass(frozen=True)
class StepSpec:
    name: str
    script: str
    required_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    optional: bool = False


STEP_SPECS: dict[str, StepSpec] = {
    "step0": StepSpec("step0", "step0_extract_xml.py", ("input_esp",), ("step0_xml",)),
    "step1": StepSpec("step1", "step1_extract_scene.py", ("input_esp",), ("step1_dump", "step1_priority")),
    "audio_extract": StepSpec("audio_extract", "extract_audio.py", ("step1_priority",), ("step3_audio_dir",), optional=True),
    "audio_profile": StepSpec("audio_profile", "audition_profiler.py", ("step1_priority",), ("audio_tone_profile",), optional=True),
    "step2": StepSpec("step2", "step2_translate_scene.py", ("step1_dump",), ("step2_translated", "step2_profile")),
    "step3": StepSpec("step3", "step3_build_xml.py", ("step0_xml", "step2_translated"), ("step3_merged",)),
    "step4": StepSpec("step4", "step4_translate_xml.py", ("step3_merged",), ("step4_translated",)),
    "step5": StepSpec("step5", "step5_review_xml.py", ("step4_translated",), ("step5_reviewed", "step5_scan")),
    "step6": StepSpec("step6", "step6_mod_update.py", ("step5_reviewed",), ("step6_refined",), optional=True),
}


def build_job_paths(input_esp: str | Path, work_dir: str | Path | None = None) -> PipelinePaths:
    input_path = Path(input_esp).expanduser().resolve()
    job_dir = Path(work_dir).expanduser().resolve() if work_dir else input_path.parent
    stem = input_path.stem
    # Every execution path should derive filenames from the same stem so
    # auto/manual/GUI runs can hand outputs to each other without guessing.
    return PipelinePaths(
        input_esp=input_path,
        work_dir=job_dir,
        stem=stem,
        step0_xml=job_dir / f"{stem}.step0.extracted.xml",
        step1_dump=job_dir / f"{stem}.step1.dump.json",
        step1_priority=job_dir / f"{stem}.step1.priority.json",
        audio_tone_profile=job_dir / f"{stem}.audio.tone_profiles.json",
        step3_audio_dir=job_dir / "temp" / "audition",
        step2_translated=job_dir / f"{stem}.step2.translated.json",
        step2_profile=job_dir / f"{stem}.step2.profile.json",
        step2_reviewed=job_dir / f"{stem}.step2.reviewed.json",
        step2_scan=job_dir / f"{stem}.step2.scan.json",
        step3_merged=job_dir / f"{stem}.step3.merged.xml",
        step4_translated=job_dir / f"{stem}.step4.translated.xml",
        step5_reviewed=job_dir / f"{stem}.step5.reviewed.xml",
        step5_scan=job_dir / f"{stem}.step5.scan.json",
        step6_refined=job_dir / f"{stem}.step6.refined.xml",
        final_xml=job_dir / f"{stem}.final.xml",
        manifest=job_dir / f"{stem}.pipeline_manifest.json",
    )


def normalize_step_name(step_name: str) -> str:
    key = step_name.strip().lower().replace("-", "_")
    aliases = {
        "step1": "step1",
        "step2": "step2",
        "step3": "step3",
        "step4": "step4",
        "step5": "step5",
        "step6": "step6",
        "audioextract": "audio_extract",
        "audio_extract": "audio_extract",
        "audioprofile": "audio_profile",
        "audio_profile": "audio_profile",
        "autopipeline": "auto_pipeline",
        "auto_pipeline": "auto_pipeline",
        "step0": "step0",
    }
    if key not in aliases:
        raise ValueError(f"Unknown step name: {step_name}")
    return aliases[key]


def _derive_profile_json(output_json: str | Path) -> Path:
    out_path = Path(output_json).expanduser().resolve()
    return out_path.with_name(out_path.stem + "_profile.json")


def _stringify_command(parts: list[Any]) -> list[str]:
    return [str(part) for part in parts if part not in (None, "")]


def build_step_command(step_name: str, config: dict | None = None, **kwargs: Any) -> list[str]:
    if isinstance(config, str):
        kwargs["config_path"] = config
        kwargs["config"] = config
        config = None

    normalized = normalize_step_name(step_name)
    parts = []
    
    # Base command assembly

    # This is the single CLI contract boundary for GUI and auto pipeline.
    input_esp = kwargs.get("input_esp") or kwargs.get("input")
    paths = build_job_paths(input_esp) if input_esp else None

    if normalized == "auto_pipeline":
        config_path = kwargs.get("config") or kwargs.get("config_path") or "config.json"
        parts: list[Any] = ["auto_pipeline.py", "--input-esp", input_esp, "--config", config_path]
        if kwargs.get("resume"):
            parts.append("--resume")
        if kwargs.get("include_step6"):
            parts.append("--include-step6")
        if kwargs.get("from_step") is not None:
            parts.extend(["--from-step", kwargs["from_step"]])
        if kwargs.get("work_dir"):
            parts.extend(["--work-dir", kwargs["work_dir"]])
        return _stringify_command(parts)

    if normalized == "step0":
        output_xml = kwargs.get("output_xml") or kwargs.get("output") or (str(paths.step0_xml) if paths else None)
        parts = ["step0_extract_xml.py", "--input-esp", input_esp, "--output-xml", output_xml]
        if kwargs.get("strings_dir"):
            parts.extend(["--strings-dir", kwargs["strings_dir"]])
        if kwargs.get("lang"):
            parts.extend(["--lang", kwargs["lang"]])
        if kwargs.get("use_ja_ref"):
            parts.append("--use-ja-ref")
        return _stringify_command(parts)

    if normalized == "step1":
        output_json = kwargs.get("output_json") or kwargs.get("output") or (str(paths.step1_dump) if paths else None)
        output_priority = kwargs.get("output_priority") or (str(paths.step1_priority) if paths else None)
        parts = ["step1_extract_scene.py", "--input-esp", input_esp, "--output-json", output_json]
        if output_priority:
            parts.extend(["--output-priority", output_priority])
        if kwargs.get("strings_dir") or kwargs.get("strings"):
            parts.extend(["--strings-dir", kwargs.get("strings_dir") or kwargs.get("strings")])
        if kwargs.get("lang"):
            parts.extend(["--lang", kwargs["lang"]])
        if kwargs.get("use_ja_ref"):
            parts.append("--use-ja-ref")
        return _stringify_command(parts)

    if normalized == "audio_extract":
        parts = [
            "extract_audio.py",
            "--priority-list",
            kwargs.get("priority_list"),
            "--data-dir",
            kwargs.get("data_dir"),
            "--output-dir",
            kwargs.get("output_dir"),
        ]
        return _stringify_command(parts)

    if normalized == "audio_profile":
        parts = [
            "audition_profiler.py",
            "--priority-list",
            kwargs.get("priority_list"),
            "--audition-dir",
            kwargs.get("audition_dir"),
            "--output",
            kwargs.get("output"),
        ]
        if kwargs.get("mode"):
            parts.extend(["--mode", kwargs["mode"]])
        if kwargs.get("input_json"):
            parts.extend(["--input-json", kwargs["input_json"]])
        
        if config and config.get("pipeline", {}).get("auto_apply_cli"):
            parts.extend(["--config", kwargs.get("config_path", "config.json")])
        elif kwargs.get("config_path"):
            parts.extend(["--config", kwargs.get("config_path")])
        
        return _stringify_command(parts)

    if normalized == "step2":
        input_json = kwargs.get("input_json") or kwargs.get("input")
        output_json = kwargs.get("output_json") or kwargs.get("output")
        profile_json = kwargs.get("profile_json") or _derive_profile_json(output_json)
        parts = [
            "step2_translate_scene.py",
            "--input-json",
            input_json,
            "--output-json",
            output_json,
            "--profile-json",
            profile_json,
            "--config",
            kwargs.get("config", "config.json"),
        ]
        tone_profile = kwargs.get("tone_profile") or kwargs.get("tone_profiles")
        if tone_profile:
            parts.extend(["--tone-profile", tone_profile])
        if kwargs.get("profile_only"):
            parts.append("--profile-only")
        if kwargs.get("use_ja_ref"):
            parts.append("--use-ja-ref")
        return _stringify_command(parts)

    if normalized == "step3":
        parts = ["step3_build_xml.py"]
        
        if kwargs.get("direct_build"):
            parts.append("--direct-build")
        
        base_xml = kwargs.get("base_xml") or kwargs.get("merge_xml")
        input_json = kwargs.get("input_json") or kwargs.get("trans")
        input_esp = kwargs.get("input_esp") or kwargs.get("esm")
        output_xml = kwargs.get("output_xml") or kwargs.get("out") or kwargs.get("output")
        
        if input_esp:
            parts.extend(["--input-esp", input_esp])
        if base_xml:
            parts.extend(["--base-xml", base_xml])
        if input_json and not kwargs.get("direct_build"):
            parts.extend(["--input-json", input_json])
        if output_xml:
            parts.extend(["--output-xml", output_xml])
        if kwargs.get("strings_dir"):
            parts.extend(["--strings-dir", kwargs["strings_dir"]])
        if kwargs.get("lang"):
            parts.extend(["--lang", kwargs["lang"]])
        return _stringify_command(parts)

    if normalized == "step4":
        parts = [
            "step4_translate_xml.py",
            "--input-xml",
            kwargs.get("input_xml") or kwargs.get("input"),
            "--output-xml",
            kwargs.get("output_xml") or kwargs.get("output"),
        ]
        if kwargs.get("mod_name"):
            parts.extend(["--mod-name", kwargs["mod_name"]])
        if kwargs.get("use_ja_ref"):
            parts.append("--use-ja-ref")
        return _stringify_command(parts)

    if normalized == "step5":
        parts = ["step5_review_xml.py"]
        
        if kwargs.get("mode"):
            parts.extend(["--mode", kwargs["mode"]])
            if kwargs["mode"] == "step2":
                parts.extend(["--input-json", kwargs.get("input_json")])
                if kwargs.get("output_json"):
                    parts.extend(["--output-json", kwargs["output_json"]])
                if kwargs.get("tone_profile"):
                    parts.extend(["--tone-profile", kwargs["tone_profile"]])
                if kwargs.get("scan_output"):
                    parts.extend(["--scan-output", kwargs["scan_output"]])
                return _stringify_command(parts)
                
        input_xml = kwargs.get("input_xml") or kwargs.get("input")
        output_xml = kwargs.get("output_xml") or kwargs.get("output")
        parts.extend(["--input-xml", input_xml])
        if output_xml:
            parts.extend(["--output-xml", output_xml])
        if kwargs.get("scan_output"):
            parts.extend(["--scan-output", kwargs["scan_output"]])
        if kwargs.get("scan_only"):
            parts.append("--scan-only")
        if kwargs.get("indices"):
            parts.extend(["--translate-indices", kwargs["indices"]])
        return _stringify_command(parts)

    if normalized == "step6":
        parts = [
            "step6_mod_update.py",
            "--mode",
            kwargs.get("mode"),
            "--input-file",
            kwargs.get("input_file") or kwargs.get("input"),
            "--output-xml",
            kwargs.get("output_xml") or kwargs.get("output"),
        ]
        if kwargs.get("profile_json") or kwargs.get("profile"):
            parts.extend(["--profile-json", kwargs.get("profile_json") or kwargs.get("profile")])
        if kwargs.get("reference"):
            parts.extend(["--reference", kwargs["reference"]])
        return _stringify_command(parts)

    raise ValueError(f"Unsupported step name: {step_name}")


def load_config(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_parent(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def require_file(path: str | Path, label: str = "input") -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"{label} file not found: {resolved}")
    return resolved


def print_ok(output_path: str | Path) -> None:
    print(f"[OK] output={Path(output_path).expanduser().resolve()}")


def get_step_sequence(include_step6: bool = False, include_audio: bool = True) -> list[str]:
    steps = ["step0", "step1"]
    if include_audio:
        steps.extend(["audio_extract", "audio_profile"])
    steps.extend(["step2", "step3", "step4", "step5"])
    if include_step6:
        steps.append("step6")
    return steps


class PipelineManifest:
    def __init__(self, manifest_path: str | Path, job_id: str):
        self.path = Path(manifest_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            "job_id": job_id,
            "updated_at": None,
            "steps": {},
        }
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    # Resume should preserve prior state, but new required keys
                    # still need sane defaults when older manifests are loaded.
                    self.data.update(loaded)
                    self.data.setdefault("steps", {})
            except Exception:
                pass

    def _save(self) -> None:
        self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)

    def get_status(self, step_name: str) -> str | None:
        step = self.data.get("steps", {}).get(step_name, {})
        if isinstance(step, dict):
            return step.get("status")
        return None

    def update_step(self, step_name: str, **fields: Any) -> None:
        step_info = self.data.setdefault("steps", {}).setdefault(step_name, {})
        step_info.update(fields)
        self._save()


def run_subprocess(command: list[str], cwd: str | Path | None = None) -> int:
    completed = subprocess.run([sys.executable] + command, cwd=str(cwd) if cwd else None, check=False)
    return completed.returncode
