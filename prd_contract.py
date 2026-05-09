from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

PROMPT_VERSION = "step2-v3"
TOOL_VERSION = "0.3.1"

TRANSLATE = "TRANSLATE"
COPY_AS_IS = "COPY_AS_IS"
REVIEW_ONLY = "REVIEW_ONLY"
SKIP_INTERNAL = "SKIP_INTERNAL"
LOCKED_TERM = "LOCKED_TERM"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def context_id(*parts: Any) -> str:
    clean = [str(part or "") for part in parts]
    return hashlib.sha256("|".join(clean).encode("utf-8")).hexdigest()[:16]


def make_stable_id(
    plugin_name: str,
    form_id: str,
    record_type: str,
    subrecord_path: str,
    field_index: int | str,
    text: str,
) -> str:
    raw = "|".join(
        [
            str(plugin_name or ""),
            str(form_id or ""),
            str(record_type or ""),
            str(subrecord_path or ""),
            str(field_index or 0),
            source_hash(text),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def classify_translation(source: str, record_type: str = "", subrecord_path: str = "") -> str:
    text = (source or "").strip()
    rec = (record_type or "").upper()
    path = (subrecord_path or "").upper()
    if not text:
        return SKIP_INTERNAL
    if re.fullmatch(r"[A-Z0-9_:\-.\\/]+", text) and not re.search(r"\s", text):
        return COPY_AS_IS
    if re.fullmatch(r"<[^>]+>|\{[^}]+\}|\[[^\]]+\]", text):
        return COPY_AS_IS
    if rec in {"SCPT", "PACK"} or any(key in path for key in ("EDID", "ALIAS", "ANAM", "VMAD")):
        return REVIEW_ONLY
    if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*|%[sdif]|\\[nrt]", text):
        return REVIEW_ONLY
    return TRANSLATE


def add_item_contract(
    item: dict[str, Any],
    *,
    plugin_name: str,
    record_type: str,
    subrecord_path: str,
    field_index: int = 0,
    quest_id: str = "",
    scene_id: str = "",
    topic_id: str = "",
    topic_info_id: str = "",
    choice_group_id: str = "",
    line_order: int = 0,
) -> dict[str, Any]:
    text = str(item.get("Text", "") or item.get("source", "") or "")
    form_id = str(item.get("FormID", "") or item.get("form_id", ""))
    item.setdefault("stable_id", make_stable_id(plugin_name, form_id, record_type, subrecord_path, field_index, text))
    item.setdefault("source_hash", source_hash(text))
    item.setdefault("context_id", context_id(quest_id, scene_id, topic_id, topic_info_id, choice_group_id, line_order))
    item.setdefault("record_type", record_type)
    item.setdefault("form_id", form_id)
    item.setdefault("subrecord_path", subrecord_path)
    item.setdefault("field_index", field_index)
    item.setdefault("quest_id", quest_id)
    if scene_id:
        item.setdefault("scene_id", scene_id)
    if topic_id:
        item.setdefault("topic_id", topic_id)
    if topic_info_id:
        item.setdefault("topic_info_id", topic_info_id)
    if choice_group_id:
        item.setdefault("choice_group_id", choice_group_id)
    item.setdefault("line_order", line_order)
    item.setdefault("tags", sorted(extract_preserved_tokens(text)))
    item.setdefault("translatable", classify_translation(text, record_type, subrecord_path) == TRANSLATE)
    item.setdefault("translation_class", classify_translation(text, record_type, subrecord_path))
    return item


def extract_preserved_tokens(text: str) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"<[^>]+>|\{[^}]+\}|\[[^\]]+\]", text))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # Cheap conservative estimator: ASCII words are ~4 chars/token, CJK/Korean
    # often runs denser. The max keeps mixed game strings from being undercounted.
    return max(1, int(len(text) / 3.2) + len(re.findall(r"[가-힣一-龥ぁ-んァ-ン]", text)) // 2)


def risk_flags(source: str, translation: str, item: dict[str, Any] | None = None) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    src = source or ""
    dst = (translation or "").strip()
    if not item or not item.get("stable_id"):
        flags.append({"severity": "fatal", "code": "missing_stable_id"})
    if not dst:
        flags.append({"severity": "fatal", "code": "empty_translation"})
    if dst and dst == src:
        flags.append({"severity": "quality", "code": "untranslated"})
    missing_tokens = extract_preserved_tokens(src) - extract_preserved_tokens(dst)
    if missing_tokens:
        flags.append({"severity": "fatal", "code": "token_loss"})
    if len(src) >= 20 and len(dst) > len(src) * 3.5:
        flags.append({"severity": "quality", "code": "too_long"})
    if item and str(item.get("Speaker", "")).lower().startswith("player"):
        flags.append({"severity": "hint", "code": "player_choice"})
    return flags


def env_overlay(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    env_map = {
        "GCP_PROJECT_ID": "gcp_project_id",
        "GCP_LOCATION": "gcp_location",
        "GCP_KEY_JSON": "gcp_key_json",
        "GOOGLE_APPLICATION_CREDENTIALS": "gcp_key_json",
        "GEMINI_API_KEY": "gemini_api_key",
        "OPENAI_API_KEY": "openai_api_key",
        "MIN1AI_API_KEY": "min1ai_api_key",
    }
    for env_name, key in env_map.items():
        if os.getenv(env_name):
            result[key] = os.getenv(env_name)
    return result
