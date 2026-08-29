"""Deterministic, non-promoting format-v3 candidate package writer."""

from __future__ import annotations

import hashlib
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
from .models import CandidateRecord, PackageResult, PipelineBlocked, PIPELINE_VERSION
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
