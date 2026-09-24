"""Baseline bank inventory and rendering-category sidecar registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json


REGISTRY_SCHEMA_VERSION = 1
CATEGORY_RULE_VERSION = 1
_SOURCE_KEY = re.compile(r"^ch(?P<chapter>\d{2})-q(?P<question>\d{4})$")
_VISUAL_WORDS = re.compile(r"\b(?:graph|chart|diagram|figure|table)\b", re.IGNORECASE)
_FRACTION = re.compile(r"(?<!\w)\d+\s*/\s*\d+(?!\w)")
_BASIC_MATH = re.compile(r"(?:\d|[A-Za-z)])\s*(?:[=+−–-]|[×÷≤≥≠±])\s*(?:\d|[A-Za-z(])")
_SUPER_SUBSCRIPT = frozenset("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_field(text: str, spatial: bool = False) -> dict[str, Any]:
    """Return a deterministic rendering proposal while preserving literal text."""
    if not isinstance(text, str):
        raise TypeError("display field text must be a string")
    if not isinstance(spatial, bool):
        raise TypeError("spatial must be a boolean")

    tags: list[str]
    if spatial:
        category, reason, tags = "source_visual", "field has reviewed spatial source evidence", ["asset:source-visual"]
    elif "\u0305" in text or "\u0304" in text or "¯" in text:
        category, reason, tags = "structured_math", "text contains an overline or recurring-number mark", ["math:recurring"]
    elif _FRACTION.search(text) or "√" in text:
        category, reason, tags = "structured_math", "text contains a fraction or radical requiring controlled layout", ["math:structured"]
    elif "\n" in text and "|" in text:
        category, reason, tags = "structured_table", "text contains row and column separators", ["layout:table"]
    elif _VISUAL_WORDS.search(text):
        category, reason, tags = "review_needed", "visual wording requires source association before choosing a renderer", ["review:visual-reference"]
    elif any(character in _SUPER_SUBSCRIPT for character in text) or any(character in text for character in "×÷≤≥≠±∑∏∞") or _BASIC_MATH.search(text):
        category, reason, tags = "unicode_math", "linear mathematical notation is representable with verified Unicode", ["math:unicode"]
    else:
        category, reason, tags = "plain_text", "no specialized rendering feature was detected", ["text:plain"]

    return {
        "category": category,
        "rule_version": CATEGORY_RULE_VERSION,
        "dependency_tags": [f"category:{category}:v{CATEGORY_RULE_VERSION}", *tags],
        "reason": reason,
        "text": text,
        "text_sha256": _sha256_bytes(text.encode("utf-8")),
    }


def _media_fields(record: Mapping[str, Any]) -> set[str]:
    raw = record.get("display_media")
    if not isinstance(raw, Mapping):
        return set()
    result: set[str] = set()
    if raw.get("question"):
        result.add("question")
    options = raw.get("options")
    if isinstance(options, Mapping):
        result.update(f"option.{label}" for label, value in options.items() if value)
    if raw.get("solution"):
        result.add("solution")
    return result


def _record_fields(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    spatial_fields = _media_fields(record)
    fields: dict[str, dict[str, Any]] = {}
    question = record.get("question_text", "")
    if not isinstance(question, str):
        raise ValueError("question_text must be a string")
    fields["question"] = classify_field(question, "question" in spatial_fields)

    options = record.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("options must be an object")
    for label in sorted(options):
        value = options[label]
        if not isinstance(label, str) or not isinstance(value, str):
            raise ValueError("option labels and values must be strings")
        field = f"option.{label}"
        fields[field] = classify_field(value, field in spatial_fields)

    steps = record.get("solution_steps", [])
    if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
        raise ValueError("solution_steps must be a list of strings")
    solution = "\n".join(steps)
    fields["solution"] = classify_field(solution, "solution" in spatial_fields)
    return fields


def _read_json_object(archive: zipfile.ZipFile, member: str) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(member).decode("utf-8"))
    except KeyError as error:
        raise ValueError(f"Package is missing {member}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{member} must contain a JSON object")
    return value


def inventory_banks(bank_dir: Path) -> dict[str, Any]:
    """Inventory accepted text-only banks without modifying their archives."""
    root = Path(bank_dir)
    if not root.is_dir():
        raise ValueError(f"Bank directory does not exist: {root}")
    paths = sorted(root.glob("*_all_vision_text_only.zip"), key=lambda item: item.name.casefold())
    records: dict[str, Any] = {}
    banks: list[dict[str, Any]] = []

    for path in paths:
        archive_hash = _sha256_path(path)
        with zipfile.ZipFile(path, "r") as archive:
            member_hashes = {
                name: _sha256_bytes(archive.read(name))
                for name in sorted(archive.namelist())
                if not name.endswith("/")
            }
            manifest = _read_json_object(archive, "manifest.json")
            question_files = manifest.get("question_files")
            if not isinstance(question_files, list) or not question_files:
                raise ValueError(f"{path.name} manifest must list question files")
            bank_keys: list[str] = []
            chapters: set[int] = set()
            for member in question_files:
                if not isinstance(member, str):
                    raise ValueError(f"{path.name} question file names must be strings")
                for line_number, line in enumerate(archive.read(member).decode("utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        raise ValueError(f"{path.name}:{member}:{line_number} must be a JSON object")
                    source_key = raw.get("key")
                    match = _SOURCE_KEY.fullmatch(source_key) if isinstance(source_key, str) else None
                    if match is None:
                        raise ValueError(f"Invalid source key in {path.name}:{member}:{line_number}")
                    if source_key in records:
                        raise ValueError(f"Duplicate source key: {source_key}")
                    chapter = int(match.group("chapter"))
                    declared_chapter = raw.get("chapter")
                    if declared_chapter is not None and str(declared_chapter).lstrip("0") != str(chapter):
                        raise ValueError(f"Source key chapter does not match record chapter: {source_key}")
                    chapters.add(chapter)
                    bank_keys.append(source_key)
                    fields = _record_fields(raw)
                    records[source_key] = {
                        "archive": path.name,
                        "chapter": chapter,
                        "question_number": int(match.group("question")),
                        "record_sha256": _sha256_bytes(canonical_json(raw)),
                        "fields": fields,
                        "dependency_tags": sorted({
                            tag
                            for field in fields.values()
                            for tag in field["dependency_tags"]
                        }),
                    }
            banks.append({
                "archive": path.name,
                "archive_sha256": archive_hash,
                "bank_name": str(manifest.get("bank_name", path.stem)),
                "chapters": sorted(chapters),
                "member_hashes": member_hashes,
                "question_count": len(bank_keys),
                "source_keys": sorted(bank_keys),
            })

    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "category_rule_version": CATEGORY_RULE_VERSION,
        "bank_count": len(banks),
        "question_count": len(records),
        "banks": banks,
        "records": {key: records[key] for key in sorted(records)},
    }


def write_inventory(path: Path, inventory: Mapping[str, Any]) -> None:
    """Atomically write canonical sidecar JSON outside package archives."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(inventory))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


__all__ = ["CATEGORY_RULE_VERSION", "REGISTRY_SCHEMA_VERSION", "classify_field", "inventory_banks", "write_inventory"]
