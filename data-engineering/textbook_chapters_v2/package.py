"""Deterministic, non-promoting format-v3 candidate package writer."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

import app

from .audit import AuditSummary
from .config import ChapterConfig
from .models import CandidateRecord, PackageResult, PipelineBlocked, PIPELINE_VERSION, RecordEvidence
from .rules import POLICY_VERSION
from .store import canonical_json, dependency_fingerprint


_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_jsonl(values: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json(value) + b"\n" for value in values)


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content, compresslevel=9)


def package_content_digest(path: Path) -> str:
    """Hash sorted ZIP member names and bytes, excluding container metadata."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist() if not item.is_dir()}
    except (OSError, zipfile.BadZipFile) as error:
        raise PipelineBlocked(f"Question package is unreadable: {path}") from error
    digest = hashlib.sha256()
    for name in sorted(members):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(members[name]).to_bytes(8, "big"))
        digest.update(members[name])
    return digest.hexdigest()


def build_shared_visual_overlay_package(
    config: ChapterConfig,
    baseline_path: Path,
    evidence: Iterable[RecordEvidence],
    field_media: Mapping[str, Any],
    validation: Mapping[str, Any],
    output_path: Path,
) -> PackageResult:
    """Preserve accepted baseline records and add only reviewed shared source visuals."""
    baseline = Path(baseline_path)
    output = Path(output_path)
    if not baseline.is_file() or baseline.resolve() == output.resolve():
        raise PipelineBlocked("Shared-visual packaging needs a separate accepted baseline ZIP.")
    if output.exists():
        raise PipelineBlocked("Shared-visual packaging refuses to overwrite an existing ZIP.")
    chapter_validation = validation.get("chapters", {}).get(str(config.chapter), {})
    if chapter_validation.get("errors") != [] or chapter_validation.get("association_count") is None:
        raise PipelineBlocked("Shared-visual packaging needs a current zero-error association audit.")
    approved_groups = {
        item.get("context_id"): item
        for item in chapter_validation.get("groups", [])
        if isinstance(item, Mapping) and item.get("vision_verdict") == "pass"
    }
    configured_groups = config.shared_contexts.get("question", {})
    if set(approved_groups) != set(configured_groups):
        raise PipelineBlocked("Shared-visual vision coverage does not match configured context groups.")
    evidence_values = tuple(evidence)
    by_number = {item.question_number: item for item in evidence_values}
    if len(by_number) != len(evidence_values):
        raise PipelineBlocked("Source evidence contains duplicate question numbers.")
    try:
        with zipfile.ZipFile(baseline) as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist() if not item.is_dir()}
    except (OSError, zipfile.BadZipFile) as error:
        raise PipelineBlocked("Accepted baseline ZIP is unreadable.") from error
    try:
        manifest = json.loads(members["manifest.json"])
        question_file = manifest["question_files"][0]
        questions = [json.loads(line) for line in members[question_file].decode("utf-8").splitlines() if line]
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError, IndexError) as error:
        raise PipelineBlocked("Accepted baseline ZIP has malformed questions or manifest.") from error
    assets: dict[str, bytes] = {}
    changed_keys: list[str] = []
    for question in questions:
        key = question.get("key")
        if not isinstance(key, str) or not key.startswith(f"ch{config.chapter:02d}-q"):
            raise PipelineBlocked("Accepted baseline question identity does not match the configured chapter.")
        number = int(key.rsplit("q", 1)[1])
        record = by_number.get(number)
        raw_media = field_media.get(str(number))
        question_media = raw_media.get("question") if isinstance(raw_media, Mapping) else None
        hashes = question_media.get("crop_sha256s") if isinstance(question_media, Mapping) else None
        alt_text = question_media.get("alt_text") if isinstance(question_media, Mapping) else None
        if not isinstance(hashes, list) or len(hashes) != 1 or not isinstance(alt_text, str) or not alt_text.strip():
            raise PipelineBlocked(f"{key} lacks one reviewed shared visual and meaningful alternative text.")
        if record is None:
            raise PipelineBlocked(f"{key} lacks current source evidence.")
        crop = {item.sha256: item for item in record.question_crops}.get(hashes[0])
        if crop is None or not crop.path.is_file():
            raise PipelineBlocked(f"{key} shared visual is absent from current source evidence.")
        content = crop.path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != crop.sha256:
            raise PipelineBlocked(f"{key} shared visual bytes changed after review.")
        asset_name = f"assets/{digest}.png"
        assets.setdefault(asset_name, content)
        question["display_media"] = {
            **(question.get("display_media") if isinstance(question.get("display_media"), dict) else {}),
            "question": {"asset": asset_name, "alt_text": alt_text.strip(), "sha256": digest, "placement": "context"},
        }
        changed_keys.append(key)
    rejection_member = manifest.get("rejected_questions_file")
    rejected = []
    if isinstance(rejection_member, str) and rejection_member in members:
        rejected = [json.loads(line) for line in members[rejection_member].decode("utf-8").splitlines() if line]
    configured_count = config.question_numbers[1] - config.question_numbers[0] + 1
    if len(questions) + len(rejected) != configured_count:
        raise PipelineBlocked("Baseline questions and explicit omissions do not cover the configured chapter.")
    if len(rejected) > int(configured_count * 0.05):
        raise PipelineBlocked("Explicit omissions exceed the five-percent chapter cap.")
    maintenance = {
        "chapter": config.chapter,
        "baseline_archive_sha256": _sha256_path(baseline),
        "baseline_content_digest": package_content_digest(baseline),
        "changed_question_keys": changed_keys,
        "omissions": rejected,
        "root_cause": "genuine spatial source visuals were absent from the accepted text-only package",
        "fix": "attach reviewed shared textbook crops as display_media.question while preserving baseline text",
        "validation": chapter_validation,
    }
    members[question_file] = _canonical_jsonl(questions)
    members["metadata/fidelity-maintenance.json"] = canonical_json(maintenance)
    members.update(assets)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
        temporary_path = Path(temporary_name)
        with zipfile.ZipFile(temporary_path, "w") as archive:
            for name in sorted(members):
                _write_member(archive, name, members[name])
        with temporary_path.open("rb") as source:
            app.parse_question_package(source)
        try:
            os.link(temporary_path, output)
        except FileExistsError as error:
            raise PipelineBlocked("Shared-visual packaging refuses to overwrite an existing ZIP.") from error
        temporary_path.unlink()
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return PackageResult(path=output, sha256=_sha256_path(output), question_count=len(questions), rejected_count=len(rejected), manifest=manifest)


def _require_audit_summary(
    audit: Any, config: ChapterConfig, candidates: tuple[CandidateRecord, ...]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(audit) is not AuditSummary:
        raise PipelineBlocked("Candidate packaging requires an authoritative AuditSummary from AuditLedger.")
    return AuditSummary._verified_package_payload(audit, config, candidates)


def _candidate_media(candidate: CandidateRecord) -> Mapping[str, Any]:
    media = candidate.representation.get("media", {})
    if not isinstance(media, Mapping):
        raise PipelineBlocked("Candidate display media must be a mapping.")
    return media


def _candidate_fingerprint(candidate: CandidateRecord) -> str:
    payload = {
        "chapter": candidate.chapter,
        "question_number": candidate.question_number,
        "question_text": candidate.question_text,
        "options": dict(candidate.options),
        "correct_answer": candidate.correct_answer,
        "answer_key_answer": candidate.answer_key_answer,
        "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
        "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint,
        "solution_steps": list(candidate.solution_steps),
        "representation": dict(candidate.representation),
        "source_fingerprint": candidate.source_fingerprint,
    }
    return dependency_fingerprint(payload)


def _validate_candidate_representation(candidate: CandidateRecord) -> Mapping[str, Any]:
    if (
        candidate.answer_key_answer != candidate.correct_answer
        or not isinstance(candidate.answer_key_crop_sha256, str)
        or len(candidate.answer_key_crop_sha256) != 64
        or candidate.answer_key_job_fingerprint != candidate.source_fingerprint
    ):
        raise PipelineBlocked("Candidate answer-key provenance is incomplete or disagrees with the candidate answer.")
    representation = candidate.representation
    if not isinstance(representation, Mapping) or set(representation) != {"question", "options", "solution", "media"}:
        raise PipelineBlocked("Candidate needs complete question, option, solution, and media representation decisions.")
    options = representation.get("options")
    if not isinstance(options, Mapping) or set(options) != set(candidate.options):
        raise PipelineBlocked("Candidate needs a representation decision for every option.")
    if representation.get("question") not in {"text", "image"} or representation.get("solution") not in {"text", "image"}:
        raise PipelineBlocked("Candidate representation decision is invalid.")
    if any(mode not in {"text", "image"} for mode in options.values()):
        raise PipelineBlocked("Candidate option representation decision is invalid.")
    media = _candidate_media(candidate)
    if set(media) - {"question", "options", "solution"}:
        raise PipelineBlocked("Candidate display media contains an unknown field.")
    if (representation["question"] == "image") != ("question" in media):
        raise PipelineBlocked("Candidate question image representation needs matching display media.")
    if (representation["solution"] == "image") != ("solution" in media):
        raise PipelineBlocked("Candidate solution image representation needs matching display media.")
    image_options = {label for label, mode in options.items() if mode == "image"}
    media_options = media.get("options", {})
    if not isinstance(media_options, Mapping) or set(media_options) != image_options:
        raise PipelineBlocked("Candidate option image representation needs matching display media.")

    question_media = media.get("question")
    question_digest = question_media.get("source_sha256") if isinstance(question_media, Mapping) else None
    option_digest_labels: dict[str, str] = {}
    for label, item in media_options.items():
        if not isinstance(item, Mapping):
            raise PipelineBlocked("Candidate option display media is invalid.")
        digest = item.get("source_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise PipelineBlocked("Candidate option display media hash is invalid.")
        if digest == question_digest:
            raise PipelineBlocked("A full-question crop cannot be reused as an option image.")
        if digest in option_digest_labels:
            raise PipelineBlocked("Distinct options cannot reuse the same display-media crop.")
        option_digest_labels[digest] = str(label)
    return media


def _media_item(raw: Any, assets: dict[str, bytes]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PipelineBlocked("Candidate media item is invalid.")
    source_path = Path(str(raw.get("source_path", "")))
    if not source_path.is_file():
        raise PipelineBlocked("Candidate media source crop is missing.")
    content = source_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if raw.get("source_sha256") != digest:
        raise PipelineBlocked("Candidate media source crop hash does not match.")
    if content[:8] != b"\x89PNG\r\n\x1a\n":
        raise PipelineBlocked("Candidate display media source must be PNG.")
    alt_text = str(raw.get("alt_text", "")).strip()
    if not alt_text:
        raise PipelineBlocked("Candidate display media requires verified alt text.")
    asset = f"assets/{digest}.png"
    existing = assets.setdefault(asset, content)
    if existing != content:
        raise PipelineBlocked("Content-hashed candidate media asset collision.")
    return {"asset": asset, "alt_text": alt_text, "sha256": digest}


def _question_entry(config: ChapterConfig, candidate: CandidateRecord, assets: dict[str, bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    if candidate.status not in {"candidate", "approved_for_publish"}:
        raise PipelineBlocked(f"Candidate ch{candidate.chapter:02d}-q{candidate.question_number:04d} is pending or quarantined.")
    if candidate.chapter != config.chapter:
        raise PipelineBlocked("Candidate chapter does not match package configuration.")
    labels = tuple(sorted(candidate.options))
    if labels not in (("A", "B", "C", "D"), ("A", "B", "C", "D", "E")) or candidate.correct_answer not in candidate.options:
        raise PipelineBlocked("Candidate has invalid option or answer mapping.")
    if not candidate.question_text or not candidate.solution_steps:
        raise PipelineBlocked("Candidate requires semantic question text and solution steps.")
    entry: dict[str, Any] = {
        "key": f"ch{candidate.chapter:02d}-q{candidate.question_number:04d}",
        "question_text": candidate.question_text,
        "category": config.bank_name,
        "chapter": str(candidate.chapter),
        "difficulty": "Medium",
        "options": dict(candidate.options),
        "correct_answer": candidate.correct_answer,
        "explanation": "",
        "solution_steps": list(candidate.solution_steps),
    }
    media = _validate_candidate_representation(candidate)
    display_media: dict[str, Any] = {}
    lineage_media: dict[str, Any] = {}
    if "question" in media:
        display_media["question"] = _media_item(media["question"], assets)
        lineage_media["question"] = dict(media["question"])
    if "options" in media:
        options = media["options"]
        if not isinstance(options, Mapping) or set(options) - set(candidate.options):
            raise PipelineBlocked("Candidate option display media must match an option.")
        display_media["options"] = {label: _media_item(options[label], assets) for label in sorted(options)}
        lineage_media["options"] = {label: dict(options[label]) for label in sorted(options)}
    if "solution" in media:
        solution = media["solution"]
        if not isinstance(solution, (list, tuple)) or not solution:
            raise PipelineBlocked("Candidate solution display media must be a non-empty list.")
        display_media["solution"] = [_media_item(item, assets) for item in solution]
        lineage_media["solution"] = [dict(item) for item in solution]
    if display_media:
        entry["display_media"] = display_media
    lineage = {
        "key": entry["key"],
        "candidate_sha256": candidate.sha256 or dependency_fingerprint(candidate),
        "source_fingerprint": candidate.source_fingerprint,
        "representation": {key: value for key, value in candidate.representation.items() if key != "media"},
        "media": lineage_media,
    }
    return entry, lineage


def _validate_written_package(path: Path, manifest: Mapping[str, Any], expected_questions: int, expected_assets: set[str]) -> None:
    with path.open("rb") as package_file:
        bank_name, questions, _, version = app.parse_question_package(package_file)
    if bank_name != manifest["bank_name"] or version != 3 or len(questions) != expected_questions:
        raise PipelineBlocked("Candidate package failed post-write parser validation.")
    with zipfile.ZipFile(path) as archive:
        names = {member.filename for member in archive.infolist() if not member.is_dir()}
    if expected_assets - names:
        raise PipelineBlocked("Candidate package parser validation found missing media assets.")


def build_candidate_package(
    config: ChapterConfig, candidates: Iterable[CandidateRecord], audit: AuditSummary, output_path: Path
) -> PackageResult:
    """Write one new candidate ZIP; existing files are never overwritten or promoted."""
    if not isinstance(config, ChapterConfig):
        raise TypeError("config must be a ChapterConfig value.")
    output = Path(output_path)
    if output.suffix.lower() != ".zip":
        raise ValueError("Candidate output_path must end in .zip.")
    records = tuple(candidates)
    if not records or any(not isinstance(candidate, CandidateRecord) for candidate in records):
        raise PipelineBlocked("Candidate packaging needs one or more CandidateRecord values.")
    ordered = tuple(sorted(records, key=lambda item: (item.chapter, item.question_number)))
    if len({(item.chapter, item.question_number) for item in ordered}) != len(ordered):
        raise PipelineBlocked("Candidate packaging refuses duplicate question numbers.")
    if any(candidate.sha256 != _candidate_fingerprint(candidate) for candidate in ordered):
        raise PipelineBlocked("Package candidate fingerprint is missing or stale.")
    audit_summary, rejections = _require_audit_summary(audit, config, ordered)
    assets: dict[str, bytes] = {}
    entries_and_lineage = [_question_entry(config, candidate, assets) for candidate in ordered]
    entries = [item[0] for item in entries_and_lineage]
    question_file = f"questions/ch{config.chapter:02d}.jsonl"
    manifest = {
        "format_version": 3,
        "pipeline_version": PIPELINE_VERSION,
        "policy_version": POLICY_VERSION,
        "bank_name": config.bank_name,
        "question_files": [question_file],
        "lineage_file": "metadata/lineage.json",
        "audit_summary_file": "metadata/audit-summary.json",
        "rejected_questions_file": "metadata/rejected-questions.jsonl",
    }
    members: dict[str, bytes] = {
        "manifest.json": canonical_json(manifest),
        question_file: _canonical_jsonl(entries),
        "metadata/lineage.json": canonical_json({"records": [item[1] for item in entries_and_lineage]}),
        "metadata/audit-summary.json": canonical_json(audit_summary),
        "metadata/rejected-questions.jsonl": _canonical_jsonl(sorted(rejections, key=canonical_json)),
        **assets,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
        temporary_path = Path(temporary_name)
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(members):
                _write_member(archive, name, members[name])
        _validate_written_package(temporary_path, manifest, len(entries), set(assets))
        try:
            os.link(temporary_path, output)
        except FileExistsError as error:
            raise PipelineBlocked("Candidate packaging refuses to overwrite an existing ZIP.") from error
        temporary_path.unlink()
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return PackageResult(
        path=output,
        sha256=_sha256_path(output),
        question_count=len(entries),
        rejected_count=len(rejections),
        manifest=manifest,
    )
