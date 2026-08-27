"""Resumable command-line orchestration for the vision-verified V2 pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .audit import AuditLedger, approval_dependency_fingerprint
from .candidates import assemble_candidate
from .config import ChapterConfig
from .models import (
    APPROVED_FOR_PUBLISH,
    BLOCKED,
    PENDING_EXTRACTION,
    PENDING_RENDER,
    PENDING_VISION,
    AuditRecord,
    CandidateRecord,
    CropBox,
    PipelineBlocked,
    RecordEvidence,
    RenderArtifacts,
    SourceCrop,
    VisionJob,
)
from .package import build_candidate_package
from .promote import promote_candidate
from .render import render_candidate
from .rules import POLICY_VERSION, validate_record
from .source import prepare_source_evidence
from .store import canonical_json
from .vision import (
    create_extraction_job,
    create_verification_job,
    ingest_extraction_result,
    ingest_verification_result,
)


SUCCESS_EXIT = 0
PENDING_VISION_EXIT = 20
BLOCKED_EXIT = 21
ERROR_EXIT = 22
EXTRACTOR_SCHEMA_VERSION = 1
VERIFIER_SCHEMA_VERSION = 1
DEFAULT_VIEWPORTS = ((1024, 768), (1600, 900))


def _atomic_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(canonical_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _atomic_jsonl(path: Path, payloads: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            for payload in payloads:
                temporary.write(canonical_json(payload) + b"\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _path_value(config: ChapterConfig, key: str, default: str | None = None) -> Path:
    raw = config.extras.get(key, default)
    if not isinstance(raw, str) or not raw.strip():
        raise PipelineBlocked(f"Chapter config requires a non-empty {key} path.")
    return Path(raw).resolve()


def _work_root(config: ChapterConfig) -> Path:
    return _path_value(config, "work_root", "tmp/textbook-v2")


def _chapter_root(config: ChapterConfig) -> Path:
    return _work_root(config) / f"chapter-{config.chapter:03d}"


def _candidate_path(config: ChapterConfig) -> Path:
    return _path_value(config, "candidate_path", f"tmp/textbook-v2/chapter-{config.chapter:03d}/candidate.zip")


def _published_path(config: ChapterConfig) -> Path:
    return _path_value(config, "published_path")


def _crop_payload(crop: SourceCrop) -> dict[str, Any]:
    return {
        "role": crop.role,
        "question_number": crop.question_number,
        "page_number": crop.page_number,
        "box": asdict(crop.box),
        "path": str(crop.path),
        "width": crop.width,
        "height": crop.height,
        "sha256": crop.sha256,
        "source_image_sha256": crop.source_image_sha256,
        "source_dpi": crop.source_dpi,
    }


def _crop_from_payload(raw: Mapping[str, Any]) -> SourceCrop:
    return SourceCrop(
        role=str(raw["role"]), question_number=int(raw["question_number"]), page_number=int(raw["page_number"]),
        box=CropBox(**raw["box"]), path=Path(raw["path"]), width=int(raw["width"]), height=int(raw["height"]),
        sha256=str(raw["sha256"]), source_image_sha256=str(raw["source_image_sha256"]), source_dpi=int(raw["source_dpi"]),
    )


def _evidence_payload(evidence: RecordEvidence) -> dict[str, Any]:
    return {
        "chapter": evidence.chapter, "question_number": evidence.question_number,
        "source_pdf": str(evidence.source_pdf), "source_pdf_sha256": evidence.source_pdf_sha256,
        "question_crops": [_crop_payload(item) for item in evidence.question_crops],
        "answer_key_crops": [_crop_payload(item) for item in evidence.answer_key_crops],
        "solution_crops": [_crop_payload(item) for item in evidence.solution_crops],
        "dependency_fingerprint": evidence.dependency_fingerprint,
    }


def _evidence_from_payload(raw: Mapping[str, Any]) -> RecordEvidence:
    return RecordEvidence(
        chapter=int(raw["chapter"]), question_number=int(raw["question_number"]), source_pdf=Path(raw["source_pdf"]),
        source_pdf_sha256=str(raw["source_pdf_sha256"]),
        question_crops=tuple(_crop_from_payload(item) for item in raw["question_crops"]),
        answer_key_crops=tuple(_crop_from_payload(item) for item in raw["answer_key_crops"]),
        solution_crops=tuple(_crop_from_payload(item) for item in raw["solution_crops"]),
        dependency_fingerprint=str(raw["dependency_fingerprint"]),
    )


def _candidate_payload(candidate: CandidateRecord) -> dict[str, Any]:
    return {
        "chapter": candidate.chapter, "question_number": candidate.question_number, "question_text": candidate.question_text,
        "options": dict(candidate.options), "correct_answer": candidate.correct_answer,
        "answer_key_answer": candidate.answer_key_answer, "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
        "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint, "solution_steps": list(candidate.solution_steps),
        "representation": dict(candidate.representation), "source_fingerprint": candidate.source_fingerprint,
        "sha256": candidate.sha256, "status": candidate.status,
    }


def _candidate_from_payload(raw: Mapping[str, Any]) -> CandidateRecord:
    return CandidateRecord(**raw)


def _job_payload(job: VisionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id, "stage": job.stage, "prompt": job.prompt,
        "sources": [dict(source) for source in job.sources], "output_schema": job.output_schema,
        "output_path": str(job.output_path) if job.output_path else None, "job_fingerprint": job.fingerprint,
    }


def _job_from_payload(raw: Mapping[str, Any]) -> VisionJob:
    return VisionJob(
        job_id=str(raw["job_id"]), stage=str(raw["stage"]), prompt=str(raw["prompt"]), sources=tuple(raw["sources"]),
        output_schema=str(raw["output_schema"]), output_path=Path(raw["output_path"]) if raw.get("output_path") else None,
        fingerprint=str(raw["job_fingerprint"]),
    )


def _render_payload(rendered: RenderArtifacts) -> dict[str, Any]:
    return {
        "question_screenshots": {key: str(value) for key, value in rendered.question_screenshots.items()},
        "solution_screenshots": {key: str(value) for key, value in rendered.solution_screenshots.items()},
        "screenshot_hashes": dict(rendered.screenshot_hashes), "findings": list(rendered.findings),
        "renderer_version": rendered.renderer_version,
    }


def _render_from_payload(raw: Mapping[str, Any]) -> RenderArtifacts:
    return RenderArtifacts(
        question_screenshots={key: Path(value) for key, value in raw["question_screenshots"].items()},
        solution_screenshots={key: Path(value) for key, value in raw["solution_screenshots"].items()},
        screenshot_hashes=raw["screenshot_hashes"], findings=tuple(raw["findings"]), renderer_version=str(raw["renderer_version"]),
    )


def _read_list(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        raise PipelineBlocked(f"Required stage artifact is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked(f"Required stage artifact is unreadable: {path}") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PipelineBlocked(f"Required stage artifact is malformed: {path}")
    return value


def _evidence(config: ChapterConfig) -> tuple[RecordEvidence, ...]:
    return tuple(_evidence_from_payload(item) for item in _read_list(_chapter_root(config) / "state" / "evidence.json"))


def _candidates(config: ChapterConfig) -> tuple[CandidateRecord, ...]:
    return tuple(_candidate_from_payload(item) for item in _read_list(_chapter_root(config) / "state" / "candidates.json"))


def _renders(config: ChapterConfig) -> dict[int, RenderArtifacts]:
    return {int(item["question_number"]): _render_from_payload(item["artifacts"])
            for item in _read_list(_chapter_root(config) / "state" / "renders.json")}


def _clear_cache(config: ChapterConfig, command: str) -> None:
    chapter = _chapter_root(config).resolve()
    directories = ["cache"] + (["state"] if command == "run" else [])
    for name in directories:
        disposable = (chapter / name).resolve()
        try:
            disposable.relative_to(chapter)
        except ValueError as error:
            raise PipelineBlocked("Configured cache path escapes the chapter work root.") from error
        if disposable.is_dir():
            shutil.rmtree(disposable)
    stage_files = {
        "prepare": (chapter / "state" / "evidence.json",),
        "ingest-extraction": (chapter / "state" / "candidates.json",),
        "render": (chapter / "state" / "renders.json",),
        "verify": (chapter / "verification-jobs.jsonl",),
    }
    for disposable in stage_files.get(command, ()):
        resolved = disposable.resolve()
        try:
            resolved.relative_to(chapter)
        except ValueError as error:
            raise PipelineBlocked("Configured cache path escapes the chapter work root.") from error
        resolved.unlink(missing_ok=True)


def _write_pending(stage: str, count: int, queue: Path) -> int:
    print(json.dumps({"status": "vision_pending", "stage": stage, "pending_jobs": count, "queue": str(queue)}))
    return PENDING_VISION_EXIT


def _prepare(config: ChapterConfig) -> int:
    records = tuple(prepare_source_evidence(config, _path_value(config, "source_pdf"), _chapter_root(config)))
    _atomic_json(_chapter_root(config) / "state" / "evidence.json", [_evidence_payload(item) for item in records])
    ledger = AuditLedger(_work_root(config), config.chapter)
    for evidence in records:
        try:
            current = ledger.record(evidence.question_number)
            incoming = replace(
                current,
                source_crop_hashes=tuple(crop.sha256 for crop in evidence.question_crops + evidence.answer_key_crops + evidence.solution_crops),
                policy_version=POLICY_VERSION,
                extractor_schema_version=EXTRACTOR_SCHEMA_VERSION,
            )
        except KeyError:
            incoming = AuditRecord(
                chapter=config.chapter, question_number=evidence.question_number, status=PENDING_EXTRACTION,
                source_crop_hashes=tuple(crop.sha256 for crop in evidence.question_crops + evidence.answer_key_crops + evidence.solution_crops),
                policy_version=POLICY_VERSION, extractor_schema_version=EXTRACTOR_SCHEMA_VERSION,
            )
        ledger.merge_record(incoming)
    print(json.dumps({"status": "prepared", "records": len(records)}))
    return SUCCESS_EXIT


def _extract(config: ChapterConfig, results_dir: Path | None = None) -> int:
    jobs: list[VisionJob] = []
    pending: list[VisionJob] = []
    default_results = results_dir or (_chapter_root(config) / "extraction-results")
    for evidence in _evidence(config):
        job_path = _chapter_root(config) / "extraction-jobs" / f"extract-ch{config.chapter:02d}-q{evidence.question_number:04d}.json"
        job = create_extraction_job(evidence, job_path)
        jobs.append(job)
        if not (default_results / f"{job.job_id}.json").is_file():
            pending.append(job)
    queue = _chapter_root(config) / "extraction-jobs.jsonl"
    _atomic_jsonl(queue, (_job_payload(job) for job in jobs))
    return _write_pending("extract", len(pending), queue) if pending else SUCCESS_EXIT


def _image_evidence(raw: dict[str, Any], evidence: RecordEvidence) -> dict[str, Any]:
    representation = raw.get("representation", {})
    option_modes = representation.get("options", {}) if isinstance(representation, dict) else {}
    media_crops: dict[str, Any] = {}
    alt_text: dict[str, Any] = {}
    if representation.get("question") == "image":
        media_crops["question"] = evidence.question_crops[0]
        alt_text["question"] = raw.get("question_text")
    image_options = {label: evidence.question_crops[0] for label, mode in option_modes.items() if mode == "image"}
    if image_options:
        media_crops["options"] = image_options
        alt_text["options"] = {label: raw["options"][label] for label in image_options}
    if representation.get("solution") == "image":
        media_crops["solution"] = evidence.solution_crops
        alt_text["solution"] = raw.get("solution_steps")
    return {**raw, "source_fingerprint": raw["job_fingerprint"], "media_crops": media_crops, "alt_text": alt_text}


def _ingest_extraction(config: ChapterConfig, results: Path) -> int:
    records: list[CandidateRecord] = []
    ledger = AuditLedger(_work_root(config), config.chapter)
    for evidence in _evidence(config):
        job_file = _chapter_root(config) / "extraction-jobs" / f"extract-ch{config.chapter:02d}-q{evidence.question_number:04d}.json"
        if not job_file.is_file():
            create_extraction_job(evidence, job_file)
        job = _job_from_payload(json.loads(job_file.read_text(encoding="utf-8")))
        result_path = results / f"{job.job_id}.json"
        if not result_path.is_file():
            return _write_pending("extract", 1, _chapter_root(config) / "extraction-jobs.jsonl")
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        findings = validate_record(raw)
        try:
            ingest_extraction_result(job, result_path)
            candidate = assemble_candidate(evidence, _image_evidence(raw, evidence), findings)
        except PipelineBlocked as error:
            current = ledger.record(evidence.question_number)
            quarantined = replace(
                current, status=BLOCKED, candidate_sha256="", asset_hashes=(), reviewer=str(raw.get("reviewer", "")),
                rejection_reason="", dependency_fingerprint="", field_verdicts={},
                findings=tuple([*(item.rule_id for item in findings), str(error)]),
            )
            ledger.merge_record(quarantined)
            if ledger.record(evidence.question_number).status != BLOCKED:
                ledger.merge_record(quarantined)
            raise
        records.append(candidate)
        current = ledger.record(evidence.question_number)
        ledger.merge_record(replace(
            current, status=PENDING_RENDER, candidate_sha256=candidate.sha256, asset_hashes=(),
            verifier_schema_version=VERIFIER_SCHEMA_VERSION, renderer_version="", application_asset_version="",
            reviewer="", rejection_reason="", dependency_fingerprint="", findings=tuple(item.rule_id for item in findings), field_verdicts={},
        ))
    _atomic_json(_chapter_root(config) / "state" / "candidates.json", [_candidate_payload(item) for item in records])
    print(json.dumps({"status": "extraction_ingested", "records": len(records)}))
    return SUCCESS_EXIT


def _build(config: ChapterConfig) -> int:
    records = _candidates(config)
    if not records:
        raise PipelineBlocked("Build requires extracted candidates.")
    print(json.dumps({"status": "built", "records": len(records)}))
    return SUCCESS_EXIT


def _viewports(config: ChapterConfig) -> tuple[tuple[int, int], ...]:
    raw = config.extras.get("validation_viewports", DEFAULT_VIEWPORTS)
    try:
        return tuple((int(item[0]), int(item[1])) for item in raw)
    except (TypeError, ValueError, IndexError) as error:
        raise PipelineBlocked("validation_viewports must contain width/height pairs.") from error


def _render(config: ChapterConfig) -> int:
    ledger = AuditLedger(_work_root(config), config.chapter)
    rendered_records: list[dict[str, Any]] = []
    for candidate in _candidates(config):
        rendered = render_candidate(candidate, {}, _viewports(config), _chapter_root(config) / "renders" / f"q{candidate.question_number:04d}")
        rendered_records.append({"question_number": candidate.question_number, "artifacts": _render_payload(rendered)})
        state_hashes = tuple(
            [f"unanswered.{key.removeprefix('question.')}:{value}" for key, value in rendered.screenshot_hashes.items() if key.startswith("question.")]
            + [f"submitted.{key.removeprefix('solution.')}:{value}" for key, value in rendered.screenshot_hashes.items() if key.startswith("solution.")]
        )
        current = ledger.record(candidate.question_number)
        ledger.merge_record(replace(
            current, status=BLOCKED if rendered.findings else PENDING_VISION, asset_hashes=state_hashes,
            renderer_version=rendered.renderer_version, application_asset_version=rendered.renderer_version,
            findings=tuple(rendered.findings), reviewer="", dependency_fingerprint="", field_verdicts={},
        ))
    _atomic_json(_chapter_root(config) / "state" / "renders.json", rendered_records)
    if any(item["artifacts"]["findings"] for item in rendered_records):
        raise PipelineBlocked("Mechanical application rendering findings remain quarantined.")
    print(json.dumps({"status": "rendered", "records": len(rendered_records)}))
    return SUCCESS_EXIT


def _verify(config: ChapterConfig, results_dir: Path | None = None) -> int:
    evidence = {item.question_number: item for item in _evidence(config)}
    renders = _renders(config)
    jobs: list[VisionJob] = []
    pending: list[VisionJob] = []
    default_results = results_dir or (_chapter_root(config) / "verification-results")
    for candidate in _candidates(config):
        source = evidence[candidate.question_number]
        job = create_verification_job(
            candidate, source.question_crops + source.answer_key_crops + source.solution_crops, renders[candidate.question_number]
        )
        jobs.append(job)
        if not (default_results / f"{job.job_id}.json").is_file():
            pending.append(job)
    queue = _chapter_root(config) / "verification-jobs.jsonl"
    _atomic_jsonl(queue, (_job_payload(job) for job in jobs))
    return _write_pending("verify", len(pending), queue) if pending else SUCCESS_EXIT


def _ingest_verification(config: ChapterConfig, results: Path) -> int:
    evidence = {item.question_number: item for item in _evidence(config)}
    renders = _renders(config)
    ledger = AuditLedger(_work_root(config), config.chapter)
    failed = 0
    for candidate in _candidates(config):
        source = evidence[candidate.question_number]
        job = create_verification_job(candidate, source.question_crops + source.answer_key_crops + source.solution_crops,
                                      renders[candidate.question_number])
        result_path = results / f"{job.job_id}.json"
        if not result_path.is_file():
            return _write_pending("verify", 1, _chapter_root(config) / "verification-jobs.jsonl")
        result = ingest_verification_result(job, result_path)
        current = ledger.record(candidate.question_number)
        passing = all(value == "pass" for value in result.verdicts.values()) and not current.findings
        updated = replace(
            current, status=APPROVED_FOR_PUBLISH if passing else BLOCKED, reviewer=result.reviewer,
            rejection_reason="", field_verdicts=result.verdicts,
            findings=() if passing else tuple(f"{key}: {value}" for key, value in result.differences.items()),
            dependency_fingerprint="",
        )
        if passing:
            updated = replace(updated, dependency_fingerprint=approval_dependency_fingerprint(updated))
        else:
            failed += 1
        ledger.merge_record(updated)
    if failed:
        raise PipelineBlocked(f"{failed} verification record(s) remain quarantined.")
    print(json.dumps({"status": "verification_ingested", "records": len(_candidates(config))}))
    return SUCCESS_EXIT


def _package(config: ChapterConfig) -> int:
    summary = AuditLedger(_work_root(config), config.chapter).validate_release_gate(config)
    result = build_candidate_package(config, _candidates(config), summary, _candidate_path(config))
    _atomic_json(_chapter_root(config) / "package-result.json", {
        "path": str(result.path), "sha256": result.sha256, "question_count": result.question_count,
        "rejected_count": result.rejected_count, "audit_sha256": summary["audit_sha256"],
    })
    _atomic_json(_candidate_path(config).with_suffix(".package.json"), {
        "candidate_sha256": result.sha256,
        "audit_sha256": summary["audit_sha256"],
    })
    print(json.dumps({"status": "packaged", "path": str(result.path), "sha256": result.sha256}))
    return SUCCESS_EXIT


def _promote(config: ChapterConfig) -> int:
    summary = AuditLedger(_work_root(config), config.chapter).validate_release_gate(config)
    receipt = promote_candidate(_candidate_path(config), _published_path(config), summary)
    print(json.dumps({"status": "promoted", "destination": str(receipt.destination), "sha256": receipt.candidate_sha256}))
    return SUCCESS_EXIT


def _run(config: ChapterConfig) -> int:
    if not (_chapter_root(config) / "state" / "evidence.json").is_file():
        _prepare(config)
    pending = _extract(config)
    if pending:
        return pending
    extraction_results = _chapter_root(config) / "extraction-results"
    _ingest_extraction(config, extraction_results)
    _build(config)
    _render(config)
    pending = _verify(config)
    if pending:
        return pending
    _ingest_verification(config, _chapter_root(config) / "verification-results")
    return _package(config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m textbook_chapters_v2")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "extract", "build", "render", "verify", "package", "promote", "run"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--force", action="store_true")
    for name in ("ingest-extraction", "ingest-verification"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--results", required=True, type=Path)
        command.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        config = ChapterConfig.load(arguments.config)
        if arguments.force:
            _clear_cache(config, arguments.command)
        handlers = {
            "prepare": lambda: _prepare(config), "extract": lambda: _extract(config),
            "ingest-extraction": lambda: _ingest_extraction(config, arguments.results.resolve()),
            "build": lambda: _build(config), "render": lambda: _render(config), "verify": lambda: _verify(config),
            "ingest-verification": lambda: _ingest_verification(config, arguments.results.resolve()),
            "package": lambda: _package(config), "promote": lambda: _promote(config), "run": lambda: _run(config),
        }
        return handlers[arguments.command]()
    except PipelineBlocked as error:
        print(json.dumps({"status": "blocked", "error": str(error)}), file=sys.stderr)
        return BLOCKED_EXIT
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return ERROR_EXIT


__all__ = ["BLOCKED_EXIT", "ERROR_EXIT", "PENDING_VISION_EXIT", "SUCCESS_EXIT", "main"]
