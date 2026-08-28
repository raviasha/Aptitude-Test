"""Resumable command-line orchestration for the Chapter 1 triage pilot."""

from __future__ import annotations

import json
import os
import sys
import hashlib
import contextlib
import io
import tempfile
from dataclasses import asdict, is_dataclass
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any, TextIO

from .agent_review import (
    AGENT_REVIEW_MIN_CONFIDENCE,
    AGENT_REVIEW_PROMPT,
    AGENT_REVIEW_PROMPT_VERSION,
    create_agent_review_queue,
    ingest_agent_review_result,
)
from .baseline import build_raw_baseline
from .models import (
    AgentReviewJob,
    AgentReviewResult,
    RawBaselineRecord,
    RouteDecision,
    VisionFallbackJob,
    VisionFallbackResult,
)
from .routing import ingest_agent_review_directory, resolve_agent_route
from .vision_fallback import create_vision_fallback_jobs, ingest_vision_fallback_results
from . import vision_fallback as _vision_protocol
from .merge import merge_final_candidates
from .audit import write_pilot_audit
from .render_gate import render_all_candidates
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import PipelineBlocked
from textbook_chapters_v2.source import prepare_source_evidence


def build_pilot_candidate_package(*args: Any, **kwargs: Any) -> Any:
    """Lazily import the dependency-heavy real package boundary."""
    from .pilot_package import build_pilot_candidate_package as build

    return build(*args, **kwargs)


COMMANDS = (
    "prepare",
    "ingest-agent",
    "prepare-vision",
    "ingest-vision",
    "finalize",
    "status",
    "run",
)
EXPECTED_RECORD_IDS = tuple(f"ch01-q{number:04d}" for number in range(1, 381))
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
APPROVED_SOURCE_SHA256 = "0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf"
APPROVED_PUBLISHED_SHA256 = "eb7b4174948b07148bdb81df23458d76495d00f6d8dbd72eed7ab084dc91c46b"
APPROVED_PATHS = {
    "source_pdf": "data-engineering/dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf",
    "v2_source_config": "data-engineering/textbook_chapters_v2/configs/chapter-001.json",
    "work_root": "tmp/python-vision-calibration/chapter-001-agent-triage",
    "candidate_path": "question-banks/candidates/agent-triage/ch01_number_system_candidate.zip",
    "published_path": "question-banks/ch01_number_system_complete.zip",
}
APPROVED_VIEWPORTS = ((1024, 768), (1600, 900))


class InvalidPilotInput(ValueError):
    """Raised when command syntax, configuration, or external JSON is invalid."""


@dataclass(frozen=True)
class PilotConfig:
    workspace_root: Path
    config_path: Path
    chapter: int
    source_pdf: Path
    source_pdf_sha256: str
    question_numbers: tuple[int, int]
    v2_source_config: Path
    work_root: Path
    candidate_path: Path
    published_path: Path
    viewports: tuple[tuple[int, int], ...]
    agent_prompt_version: str
    agent_accept_confidence: float


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise InvalidPilotInput(f"Configuration contains duplicate JSON key: {key}.")
        value[key] = item
    return value


def _is_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def _resolve_workspace_path(raw: Any, *, workspace_root: Path, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip() or Path(raw).is_absolute():
        raise InvalidPilotInput(f"{field} must be a non-empty workspace-relative path.")
    workspace = workspace_root.resolve()
    lexical = workspace / Path(raw)
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(workspace)
    except (OSError, ValueError) as error:
        raise InvalidPilotInput(f"{field} must stay within the workspace.") from error
    current = workspace
    for component in Path(raw).parts:
        if component in {"", "."}:
            continue
        if component == "..":
            raise InvalidPilotInput(f"{field} must not traverse outside its approved directory.")
        current = current / component
        if current.exists() and _is_reparse(current):
            raise InvalidPilotInput(f"{field} must not cross a symlink or reparse point.")
    return resolved


def _exact_pair(value: Any, field: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise InvalidPilotInput(f"{field} must contain exactly two integers.")
    return value[0], value[1]


def validate_pilot_config_payload(
    raw: Any,
    *,
    workspace_root: Path = WORKSPACE_ROOT,
    config_path: Path | None = None,
) -> PilotConfig:
    if not isinstance(raw, dict):
        raise InvalidPilotInput("Pilot configuration must be a JSON object.")
    required = {
        "chapter", "source_pdf", "source_pdf_sha256", "question_numbers", "v2_source_config",
        "work_root", "candidate_path", "published_path", "viewports", "agent_prompt_version",
        "agent_accept_confidence",
    }
    if set(raw) != required:
        raise InvalidPilotInput("Pilot configuration fields do not match the approved contract.")
    if raw["chapter"] != 1 or isinstance(raw["chapter"], bool):
        raise InvalidPilotInput("The pilot is restricted to Chapter 1.")
    if _exact_pair(raw["question_numbers"], "question_numbers") != (1, 380):
        raise InvalidPilotInput("The pilot inventory must be exactly questions 1 through 380.")
    if raw["source_pdf_sha256"] != APPROVED_SOURCE_SHA256:
        raise InvalidPilotInput("The source PDF hash differs from the approved Chapter 1 hash.")
    if raw["agent_prompt_version"] != AGENT_REVIEW_PROMPT_VERSION:
        raise InvalidPilotInput("The coding-agent prompt version has drifted.")
    confidence = raw["agent_accept_confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence != AGENT_REVIEW_MIN_CONFIDENCE:
        raise InvalidPilotInput("The coding-agent acceptance confidence must be exactly 0.95.")
    viewports_raw = raw["viewports"]
    if not isinstance(viewports_raw, list):
        raise InvalidPilotInput("viewports must be a list.")
    viewports = tuple(_exact_pair(item, "viewports") for item in viewports_raw)
    if viewports != APPROVED_VIEWPORTS:
        raise InvalidPilotInput("The pilot viewports differ from the reviewed render contract.")
    for field, approved in APPROVED_PATHS.items():
        if raw[field] != approved:
            raise InvalidPilotInput(f"{field} differs from the approved pilot path.")
    workspace = Path(workspace_root).resolve()
    paths = {
        field: _resolve_workspace_path(raw[field], workspace_root=workspace, field=field)
        for field in APPROVED_PATHS
    }
    candidate_parent = (workspace / "question-banks" / "candidates" / "agent-triage").resolve()
    try:
        paths["candidate_path"].relative_to(candidate_parent)
    except ValueError as error:
        raise InvalidPilotInput("candidate_path must stay below question-banks/candidates/agent-triage.") from error
    if os.path.normcase(str(paths["candidate_path"])) == os.path.normcase(str(paths["published_path"])):
        raise InvalidPilotInput("Candidate and published package paths must be distinct.")
    return PilotConfig(
        workspace_root=workspace,
        config_path=Path(config_path).resolve() if config_path else workspace / "data-engineering/python_vision_calibration/configs/chapter-001-agent-triage.json",
        chapter=1,
        source_pdf=paths["source_pdf"],
        source_pdf_sha256=APPROVED_SOURCE_SHA256,
        question_numbers=(1, 380),
        v2_source_config=paths["v2_source_config"],
        work_root=paths["work_root"],
        candidate_path=paths["candidate_path"],
        published_path=paths["published_path"],
        viewports=viewports,
        agent_prompt_version=AGENT_REVIEW_PROMPT_VERSION,
        agent_accept_confidence=float(confidence),
    )


def load_pilot_config(path: Path, *, workspace_root: Path = WORKSPACE_ROOT) -> PilotConfig:
    expected_path = Path(workspace_root).resolve() / "data-engineering/python_vision_calibration/configs/chapter-001-agent-triage.json"
    if Path(path).resolve(strict=False) != expected_path:
        raise InvalidPilotInput(f"The pilot must use its authoritative configuration path: {expected_path}")
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as source:
            raw = json.load(source, object_pairs_hook=_reject_duplicate_pairs)
    except InvalidPilotInput:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPilotInput(f"Invalid pilot configuration: {path}") from error
    return validate_pilot_config_payload(raw, workspace_root=workspace_root, config_path=Path(path))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise PipelineBlocked(f"Required protected artifact is missing or unreadable: {path}") from error
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Canonical JSON does not support {type(value).__name__} values.")


def canonical_json_bytes(value: Any, *, ensure_ascii: bool = True) -> bytes:
    return json.dumps(
        _json_value(value), ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_sha256(value: Any, *, ensure_ascii: bool = True) -> str:
    return hashlib.sha256(canonical_json_bytes(value, ensure_ascii=ensure_ascii)).hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if target.is_file() and target.read_bytes() == content:
            Path(temporary_name).unlink(missing_ok=True)
            temporary_name = None
            return
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as source:
            payload = json.load(source, object_pairs_hook=_reject_duplicate_pairs)
    except InvalidPilotInput:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPilotInput(f"Invalid canonical JSON artifact: {path}") from error
    if not isinstance(payload, dict):
        raise InvalidPilotInput(f"Canonical JSON artifact must be an object: {path}")
    return payload


def _read_jsonl(path: Path, *, ensure_ascii: bool = False) -> tuple[dict[str, Any], ...]:
    try:
        content = Path(path).read_bytes()
        lines = content.splitlines(keepends=True)
        if content and (not lines or not lines[-1].endswith(b"\n")):
            raise InvalidPilotInput(f"Canonical JSONL must end each record with a newline: {path}")
        values = []
        for line in lines:
            text = line[:-1].decode("utf-8")
            payload = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
            if not isinstance(payload, dict) or canonical_json_bytes(payload, ensure_ascii=ensure_ascii) + b"\n" != line:
                raise InvalidPilotInput(f"Noncanonical JSONL record in {path}.")
            values.append(payload)
        return tuple(values)
    except InvalidPilotInput:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPilotInput(f"Invalid canonical JSONL artifact: {path}") from error


def _baseline_payload(record: RawBaselineRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "chapter": record.chapter,
        "source_hashes": record.source_hashes,
        "source_identity": record.source_identity,
        "candidate": record.candidate,
        "baseline_sha256": record.baseline_sha256,
    }


def _validate_baselines(records: tuple[RawBaselineRecord, ...], config: PilotConfig) -> None:
    if tuple(record.record_id for record in records) != EXPECTED_RECORD_IDS:
        raise PipelineBlocked("The baseline inventory must be exactly ch01-q0001 through ch01-q0380.")
    for number, record in enumerate(records, start=1):
        if record.chapter != 1 or record.source_identity.get("number") != number:
            raise PipelineBlocked(f"Baseline identity is invalid for {record.record_id}.")
        if tuple(record.source_hashes.get("source_pdf", ())) != (config.source_pdf_sha256,):
            raise PipelineBlocked(f"Baseline source PDF binding is stale for {record.record_id}.")
        core = _baseline_payload(record)
        core.pop("baseline_sha256")
        if record.baseline_sha256 != canonical_sha256(core):
            raise PipelineBlocked(f"Baseline hash is stale for {record.record_id}.")


def _write_baselines(records: tuple[RawBaselineRecord, ...], path: Path) -> None:
    _atomic_bytes(path, b"".join(canonical_json_bytes(_baseline_payload(record)) + b"\n" for record in records))


def _load_baselines(config: PilotConfig) -> tuple[RawBaselineRecord, ...]:
    path = config.work_root / "baseline" / "chapter-001.jsonl"
    values = _read_jsonl(path)
    records: list[RawBaselineRecord] = []
    expected_fields = {"record_id", "chapter", "source_hashes", "source_identity", "candidate", "baseline_sha256"}
    for payload in values:
        if set(payload) != expected_fields or not isinstance(payload["source_hashes"], dict):
            raise InvalidPilotInput("Baseline artifact fields are malformed.")
        records.append(RawBaselineRecord(
            record_id=payload["record_id"],
            chapter=payload["chapter"],
            source_hashes={key: tuple(items) for key, items in payload["source_hashes"].items()},
            candidate=payload["candidate"],
            baseline_sha256=payload["baseline_sha256"],
            source_identity=payload["source_identity"],
        ))
    result = tuple(records)
    _validate_baselines(result, config)
    return result


def _job_expected(record: RawBaselineRecord, path: Path) -> tuple[AgentReviewJob, dict[str, Any]]:
    content_record = {
        "record_id": record.record_id,
        "chapter": record.chapter,
        "source_hashes": {key: list(values) for key, values in record.source_hashes.items()},
        "candidate": record.candidate,
        "baseline_sha256": record.baseline_sha256,
        "source_identity": record.source_identity,
    }
    prompt_hash = canonical_sha256(AGENT_REVIEW_PROMPT, ensure_ascii=False)
    payload_hash = canonical_sha256({
        "record": content_record,
        "prompt": AGENT_REVIEW_PROMPT,
        "prompt_version": AGENT_REVIEW_PROMPT_VERSION,
        "output_schema": "agent-review-result.schema.json",
    }, ensure_ascii=False)
    job_hash = canonical_sha256({
        "record_id": record.record_id,
        "baseline_sha256": record.baseline_sha256,
        "prompt_version": AGENT_REVIEW_PROMPT_VERSION,
        "prompt_sha256": prompt_hash,
        "payload_sha256": payload_hash,
        "output_schema": "agent-review-result.schema.json",
    }, ensure_ascii=False)
    job = AgentReviewJob(
        record_id=record.record_id,
        baseline_sha256=record.baseline_sha256,
        prompt_version=AGENT_REVIEW_PROMPT_VERSION,
        prompt_sha256=prompt_hash,
        payload_sha256=payload_hash,
        output_schema="agent-review-result.schema.json",
        output_path=path,
        job_sha256=job_hash,
    )
    document = {
        "record_id": job.record_id,
        "baseline_sha256": job.baseline_sha256,
        "prompt_version": job.prompt_version,
        "prompt_sha256": job.prompt_sha256,
        "payload_sha256": job.payload_sha256,
        "output_schema": job.output_schema,
        "output_path": str(job.output_path),
        "job_sha256": job.job_sha256,
        "prompt": AGENT_REVIEW_PROMPT,
        "record": content_record,
    }
    return job, document


def _load_agent_jobs(config: PilotConfig, baselines: tuple[RawBaselineRecord, ...]) -> tuple[AgentReviewJob, ...]:
    root = config.work_root / "agent-review"
    jobs_dir = root / "jobs"
    if not jobs_dir.is_dir():
        raise PipelineBlocked("The canonical agent-review job directory is missing.")
    observed = [path for path in jobs_dir.iterdir() if path.is_file()]
    expected_names = {f"{record_id}.json" for record_id in EXPECTED_RECORD_IDS}
    if {path.name for path in observed} != expected_names:
        raise InvalidPilotInput("Agent-review job inventory is missing, extra, duplicate, or noncanonical.")
    jobs: list[AgentReviewJob] = []
    index_rows = []
    for record in baselines:
        path = jobs_dir / f"{record.record_id}.json"
        expected_job, expected_document = _job_expected(record, path)
        payload = _read_json(path)
        if payload != expected_document or path.read_bytes() != canonical_json_bytes(expected_document, ensure_ascii=False):
            raise InvalidPilotInput(f"Agent-review job is stale or noncanonical: {record.record_id}.")
        jobs.append(expected_job)
        index_rows.append({
            "record_id": expected_job.record_id,
            "baseline_sha256": expected_job.baseline_sha256,
            "job_sha256": expected_job.job_sha256,
            "path": f"jobs/{expected_job.record_id}.json",
        })
    index_path = root / "agent-review-jobs.jsonl"
    if index_path.read_bytes() != b"".join(
        canonical_json_bytes(row, ensure_ascii=False) + b"\n" for row in index_rows
    ):
        raise InvalidPilotInput("Agent-review queue index is stale or noncanonical.")
    return tuple(jobs)


def _agent_result_inventory(directory: Path) -> dict[str, Path]:
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise InvalidPilotInput("Agent-review results path must be a directory.")
    expected = {f"{record_id}.json".casefold(): record_id for record_id in EXPECTED_RECORD_IDS}
    inventory: dict[str, list[Path]] = {}
    unexpected: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        record_id = expected.get(path.name.casefold())
        if record_id is None:
            if path.suffix.lower() == ".json":
                unexpected.append(path.name)
            continue
        inventory.setdefault(record_id, []).append(path)
    if unexpected:
        raise InvalidPilotInput(f"Unexpected agent-review result files: {', '.join(unexpected)}.")
    result: dict[str, Path] = {}
    for record_id, paths in inventory.items():
        if len(paths) != 1 or paths[0].name != f"{record_id}.json":
            raise InvalidPilotInput(f"Duplicate or noncanonical agent result for {record_id}.")
        result[record_id] = paths[0]
    return result


def _agent_progress(
    config: PilotConfig,
    baselines: tuple[RawBaselineRecord, ...],
    jobs: tuple[AgentReviewJob, ...],
    results_dir: Path,
    *,
    mutate: bool,
) -> tuple[dict[str, object], dict[str, AgentReviewResult], tuple[RouteDecision, ...]]:
    inventory = _agent_result_inventory(results_dir)
    jobs_by_id = {job.record_id: job for job in jobs}
    records_by_id = {record.record_id: record for record in baselines}
    results: dict[str, AgentReviewResult] = {}
    routes = []
    for record_id, path in sorted(inventory.items()):
        result = ingest_agent_review_result(jobs_by_id[record_id], path)
        results[record_id] = result
        routes.append(resolve_agent_route(records_by_id[record_id], result))
    if mutate:
        summary = ingest_agent_review_directory(baselines, jobs, results_dir, config.work_root)
    else:
        summary = {
            "total": len(baselines),
            "accepted_python": sum(route.decision == "ACCEPT_PYTHON" for route in routes),
            "vision_required": sum(route.decision == "VISION_REQUIRED" for route in routes),
            "pending": len(baselines) - len(results),
            "queue_path": str(config.work_root / "agent-review/agent-review-jobs.jsonl"),
            "results_path": str(results_dir),
        }
    return summary, results, tuple(sorted(routes, key=lambda route: route.record_id))


def _checkpoint(config: PilotConfig, stage: str, payload: Mapping[str, Any]) -> None:
    core = {
        "schema_version": 1,
        "stage": stage,
        "config_sha256": _sha256_path(config.config_path),
        "source_pdf_sha256": _sha256_path(config.source_pdf),
        "published_sha256": _sha256_path(config.published_path),
        **dict(payload),
    }
    document = {**core, "dependency_fingerprint": canonical_sha256(core)}
    _atomic_bytes(config.work_root / "state" / f"{stage}.json", canonical_json_bytes(document))


def _prepare(config: PilotConfig) -> tuple[tuple[RawBaselineRecord, ...], tuple[AgentReviewJob, ...]]:
    baseline_path = config.work_root / "baseline" / "chapter-001.jsonl"
    if baseline_path.is_file():
        baselines = _load_baselines(config)
    elif config.work_root.exists() and any(config.work_root.iterdir()):
        raise PipelineBlocked("The work root is non-empty without a canonical baseline.")
    else:
        baselines = tuple(build_raw_baseline(1, config.source_pdf, config.work_root))
        _validate_baselines(baselines, config)
        _write_baselines(baselines, baseline_path)
    queue_index = config.work_root / "agent-review" / "agent-review-jobs.jsonl"
    if queue_index.is_file():
        jobs = _load_agent_jobs(config, baselines)
    else:
        jobs = create_agent_review_queue(baselines, config.work_root / "agent-review")
        jobs = _load_agent_jobs(config, baselines)
    _checkpoint(config, "prepared", {
        "baseline_sha256": _sha256_path(baseline_path),
        "agent_queue_sha256": _sha256_path(queue_index),
        "record_count": len(baselines),
    })
    return baselines, jobs


def _agent_payload(command: str, config: PilotConfig, summary: Mapping[str, Any]) -> dict[str, object]:
    pending = int(summary["pending"])
    return {
        "command": command,
        "stage": "agent_review",
        "status": "pending_external" if pending else "complete",
        "pending_jobs": pending,
        "pending_agent_jobs": pending,
        "pending_vision_jobs": 0,
        "total_baselines": int(summary["total"]),
        "agent_terminal": int(summary["total"]) - pending,
        "accepted_python": int(summary["accepted_python"]),
        "vision_required": int(summary["vision_required"]),
        "vision_terminal": 0,
        "included": 0,
        "quarantined": 0,
        "paths": _artifact_paths(config),
    }


def _prepared_payload(command: str, config: PilotConfig) -> dict[str, object]:
    return {
        "command": command,
        "stage": "prepared",
        "status": "complete",
        "pending_jobs": 380,
        "pending_agent_jobs": 380,
        "pending_vision_jobs": 0,
        "total_baselines": 380,
        "agent_terminal": 0,
        "accepted_python": 0,
        "vision_required": 0,
        "vision_terminal": 0,
        "included": 0,
        "quarantined": 0,
        "paths": _artifact_paths(config),
    }


def _validated_results_path(config: PilotConfig, supplied: Path | None, stage: str) -> Path:
    expected = config.work_root / ("agent-review-results" if stage == "agent" else "vision-results")
    if supplied is None:
        return expected
    try:
        resolved = Path(supplied).resolve(strict=False)
        resolved.relative_to(config.workspace_root)
    except (OSError, ValueError) as error:
        raise InvalidPilotInput("Results directory must stay within the workspace.") from error
    if os.path.normcase(str(resolved)) != os.path.normcase(str(expected)):
        raise InvalidPilotInput(f"{stage} results must use the authoritative results directory: {expected}")
    if resolved.exists() and _is_reparse(resolved):
        raise InvalidPilotInput("Results directory must not be a symlink or reparse point.")
    return resolved


def prepare_source_and_vision_jobs(
    config: PilotConfig,
    v2_config: ChapterConfig,
    routes: tuple[RouteDecision, ...],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    evidence = tuple(prepare_source_evidence(
        v2_config,
        config.source_pdf,
        config.work_root / "vision" / "source-evidence",
    ))
    jobs = create_vision_fallback_jobs(routes, evidence, config.work_root)
    expected = {route.record_id for route in routes if route.decision == "VISION_REQUIRED"}
    if set(jobs) != expected:
        raise PipelineBlocked("Vision job inventory does not exactly match the terminal vision routes.")
    return evidence, jobs


def _package_config(config: PilotConfig, v2_config: ChapterConfig) -> ChapterConfig:
    extras = dict(v2_config.extras)
    extras.update({
        "source_pdf": str(config.source_pdf),
        "source_pdf_sha256": config.source_pdf_sha256,
        "published_path": str(config.published_path),
    })
    return ChapterConfig(
        chapter=v2_config.chapter,
        bank_name=v2_config.bank_name,
        question_pages=v2_config.question_pages,
        answer_pages=v2_config.answer_pages,
        solution_pages=v2_config.solution_pages,
        question_numbers=v2_config.question_numbers,
        intentional_exclusions=v2_config.intentional_exclusions,
        marker_overrides=v2_config.marker_overrides,
        layout_boundaries=v2_config.layout_boundaries,
        shared_contexts=v2_config.shared_contexts,
        extras=extras,
    )


def finalize_stage(
    *,
    config: PilotConfig,
    v2_config: ChapterConfig,
    baselines: tuple[RawBaselineRecord, ...],
    routes: tuple[RouteDecision, ...],
    agent_jobs: Mapping[str, AgentReviewJob],
    agent_results: Mapping[str, AgentReviewResult],
    evidence: tuple[Any, ...],
    vision_jobs: Mapping[str, Any],
    vision_results: tuple[Any, ...],
) -> Any:
    candidates = merge_final_candidates(
        baselines,
        routes,
        vision_results,
        evidence,
        vision_jobs=vision_jobs,
        expected_record_ids=EXPECTED_RECORD_IDS,
    )
    if not candidates:
        raise PipelineBlocked("The pilot cannot package zero accepted candidates.")
    authoritative = {
        "agent_jobs": agent_jobs,
        "agent_results": agent_results,
        "vision_jobs": vision_jobs,
        "evidence": evidence,
        "expected_record_ids": EXPECTED_RECORD_IDS,
    }
    write_pilot_audit(
        baselines, routes, vision_results, candidates, (), config.work_root, **authoritative
    )
    manifests = render_all_candidates(
        candidates,
        config.work_root / "vision" / "source-evidence",
        config.work_root,
    )
    audit = write_pilot_audit(
        baselines, routes, vision_results, candidates, manifests, config.work_root, **authoritative
    )
    if int(audit.counts.get("pending_render", 0)):
        raise PipelineBlocked("At least one accepted candidate is missing a complete finding-free render manifest.")
    return build_pilot_candidate_package(
        _package_config(config, v2_config), candidates, audit, config.candidate_path
    )


def _vision_progress(
    config: PilotConfig,
    routes: tuple[RouteDecision, ...],
    jobs: Mapping[str, Any],
    evidence: tuple[Any, ...],
    results_dir: Path,
) -> tuple[dict[str, object], tuple[Any, ...]]:
    summary = ingest_vision_fallback_results(routes, jobs, results_dir, config.work_root)
    inventory = _vision_protocol._result_inventory(results_dir, set(jobs))
    evidence_by_number = {item.question_number: item for item in evidence}
    results = tuple(
        _vision_protocol._ingest_result_against_current(
            jobs[record_id], path, evidence_by_number[jobs[record_id].question_number]
        )
        for record_id, path in sorted(inventory.items())
    )
    if len(results) != int(summary["total"]) - int(summary["pending"]):
        raise PipelineBlocked("Vision result summary does not match its validated terminal inventory.")
    return summary, results


def _load_vision_jobs(
    config: PilotConfig,
    routes: tuple[RouteDecision, ...],
) -> dict[str, VisionFallbackJob]:
    expected_ids = {route.record_id for route in routes if route.decision == "VISION_REQUIRED"}
    jobs_dir = config.work_root / "vision" / "jobs"
    if not jobs_dir.is_dir():
        raise PipelineBlocked("The canonical vision job directory is missing.")
    observed = [path for path in jobs_dir.iterdir() if path.is_file()]
    if {path.name for path in observed} != {f"{record_id}.json" for record_id in expected_ids}:
        raise InvalidPilotInput("Vision job inventory is missing, extra, duplicate, or noncanonical.")
    route_by_id = {route.record_id: route for route in routes}
    jobs: dict[str, VisionFallbackJob] = {}
    for record_id in sorted(expected_ids):
        path = jobs_dir / f"{record_id}.json"
        payload = _read_json(path)
        sources = tuple(payload.get("sources", ()))
        role_hashes = {
            role: tuple(source["sha256"] for source in sources if source.get("role") == role)
            for role in ("question", "answer_key", "solution")
        }
        try:
            job = VisionFallbackJob(
                record_id=payload["record_id"],
                question_number=payload["printed_question_number"],
                route_sha256=payload["route_sha256"],
                baseline_sha256=payload["baseline_sha256"],
                source_pdf_sha256=payload["source_pdf_sha256"],
                source_dependency_fingerprint=payload["source_dependency_fingerprint"],
                evidence_sha256=payload["evidence_sha256"],
                config_sha256=payload["config_sha256"],
                schema_sha256=payload["schema_sha256"],
                prompt=payload["prompt"],
                prompt_sha256=payload["prompt_sha256"],
                sources=sources,
                source_evidence_sha256s=tuple(payload["source_evidence_sha256s"]),
                role_sha256s=role_hashes,
                output_schema=payload["output_schema"],
                output_path=Path(payload["output_path"]),
                requires_quarantine=payload["requires_quarantine"],
                source_reasons=tuple(payload["source_reasons"]),
                job_sha256=payload["job_sha256"],
            )
        except (KeyError, TypeError) as error:
            raise InvalidPilotInput(f"Malformed vision job: {record_id}.") from error
        if job.record_id != record_id or job.route_sha256 != route_by_id[record_id].route_sha256:
            raise InvalidPilotInput(f"Vision job is stale against route {record_id}.")
        _vision_protocol._validate_job(job)
        expected_payload = _vision_protocol._job_payload(job)
        if payload != expected_payload or path.read_bytes() != canonical_json_bytes(expected_payload):
            raise InvalidPilotInput(f"Vision job is noncanonical: {record_id}.")
        jobs[record_id] = job
    index_path = config.work_root / "vision" / "vision-jobs.jsonl"
    expected_index = b"".join(
        canonical_json_bytes(_vision_protocol._job_payload(job)) + b"\n" for job in jobs.values()
    )
    if not index_path.is_file() or index_path.read_bytes() != expected_index:
        raise InvalidPilotInput("Vision job index is stale or noncanonical.")
    return jobs


def _load_vision_results_read_only(
    jobs: Mapping[str, VisionFallbackJob], results_dir: Path
) -> tuple[VisionFallbackResult, ...]:
    inventory = _vision_protocol._result_inventory(results_dir, set(jobs))
    results = []
    for record_id, path in sorted(inventory.items()):
        job = jobs[record_id]
        payload = _vision_protocol._read_result(path)
        _vision_protocol._validate_schema(payload, _vision_protocol._schema())
        if payload["record_id"] != record_id or payload["route_sha256"] != job.route_sha256 or payload["job_sha256"] != job.job_sha256:
            raise InvalidPilotInput(f"Vision result is stale against job {record_id}.")
        if payload["decision"] == "VISION_ACCEPTED":
            _vision_protocol._validate_accepted(job, payload)
        elif payload["decision"] == "QUARANTINE":
            _vision_protocol._validate_quarantine(job, payload)
        else:
            raise InvalidPilotInput(f"Vision result is nonterminal: {record_id}.")
        expected_hash = canonical_sha256(
            {key: value for key, value in payload.items() if key != "result_sha256"}
        )
        if payload["result_sha256"] != expected_hash:
            raise InvalidPilotInput(f"Vision result hash is stale: {record_id}.")
        results.append(VisionFallbackResult(
            record_id=payload["record_id"],
            decision=payload["decision"],
            question_text=payload["question_text"],
            options=payload["options"],
            correct_answer=payload["correct_answer"],
            solution_steps=tuple(payload["solution_steps"]),
            representation=payload["representation"],
            media=payload["media"],
            source_evidence_sha256s=tuple(payload["source_evidence_sha256s"]),
            quarantine_reason=payload["quarantine_reason"],
            reviewer=payload["reviewer"],
            route_sha256=payload["route_sha256"],
            job_sha256=payload["job_sha256"],
            result_sha256=payload["result_sha256"],
        ))
    return tuple(results)


def _vision_payload(
    command: str,
    config: PilotConfig,
    agent_summary: Mapping[str, Any],
    vision_summary: Mapping[str, Any],
) -> dict[str, object]:
    pending = int(vision_summary["pending"])
    return {
        "command": command,
        "stage": "vision_review",
        "status": "pending_external" if pending else "complete",
        "pending_jobs": pending,
        "pending_agent_jobs": 0,
        "pending_vision_jobs": pending,
        "total_baselines": 380,
        "agent_terminal": 380,
        "accepted_python": int(agent_summary["accepted_python"]),
        "vision_required": int(vision_summary["total"]),
        "vision_terminal": int(vision_summary["total"]) - pending,
        "vision_accepted": int(vision_summary["vision_accepted"]),
        "included": int(agent_summary["accepted_python"]) + int(vision_summary["vision_accepted"]),
        "quarantined": int(vision_summary["quarantined"]),
        "paths": _artifact_paths(config),
    }


def _execute_vision_command(
    command: str,
    config: PilotConfig,
    v2_config: ChapterConfig,
    baselines: tuple[RawBaselineRecord, ...],
    agent_jobs: tuple[AgentReviewJob, ...],
    agent_results: dict[str, AgentReviewResult],
    routes: tuple[RouteDecision, ...],
    agent_summary: Mapping[str, Any],
    results_dir: Path,
) -> tuple[int, dict[str, object]]:
    evidence, vision_jobs = prepare_source_and_vision_jobs(config, v2_config, routes)
    job_index = config.work_root / "vision" / "vision-jobs.jsonl"
    _checkpoint(config, "vision-prepared", {
        "route_hashes": {route.record_id: route.route_sha256 for route in routes},
        "vision_job_hashes": {
            record_id: job.job_sha256 for record_id, job in sorted(vision_jobs.items())
        },
        "vision_job_index_sha256": _sha256_path(job_index),
    })
    if command == "prepare-vision":
        summary, _ = _vision_progress(config, routes, vision_jobs, evidence, results_dir)
        payload = _vision_payload(command, config, agent_summary, summary)
        payload["status"] = "complete"
        return 0, payload
    vision_summary, vision_results = _vision_progress(
        config, routes, vision_jobs, evidence, results_dir
    )
    _checkpoint(config, "vision-review", {
        "vision_job_hashes": {
            record_id: job.job_sha256 for record_id, job in sorted(vision_jobs.items())
        },
        "vision_result_hashes": {
            result.record_id: result.result_sha256 for result in vision_results
        },
        "pending": int(vision_summary["pending"]),
    })
    payload = _vision_payload(command, config, agent_summary, vision_summary)
    if int(vision_summary["pending"]):
        return 20, payload
    if command == "ingest-vision":
        return 0, payload
    package = finalize_stage(
        config=config,
        v2_config=v2_config,
        baselines=baselines,
        routes=routes,
        agent_jobs={job.record_id: job for job in agent_jobs},
        agent_results=agent_results,
        evidence=evidence,
        vision_jobs=vision_jobs,
        vision_results=vision_results,
    )
    payload.update({
        "stage": "candidate_ready",
        "status": "complete",
        "pending_jobs": 0,
        "candidate_sha256": package.sha256,
        "candidate_question_count": package.question_count,
    })
    _checkpoint(config, "finalized", {
        "candidate_sha256": package.sha256,
        "candidate_question_count": package.question_count,
        "published_sha256": _sha256_path(config.published_path),
    })
    return 0, payload


def _validate_v2_source_config(config: PilotConfig) -> ChapterConfig:
    try:
        value = ChapterConfig.load(config.v2_source_config)
    except (OSError, ValueError) as error:
        raise InvalidPilotInput("The V2 Chapter 1 source configuration is invalid.") from error
    extras = value.extras
    expected = {
        "source_pdf": APPROVED_PATHS["source_pdf"],
        "source_pdf_sha256": APPROVED_SOURCE_SHA256,
        "published_path": APPROVED_PATHS["published_path"],
    }
    if (
        value.chapter != 1
        or value.question_numbers != (1, 380)
        or value.printed_question_count != 380
        or value.intentional_exclusions
        or any(extras.get(key) != expected_value for key, expected_value in expected.items())
        or tuple(tuple(item) for item in extras.get("validation_viewports", ())) != APPROVED_VIEWPORTS
    ):
        raise InvalidPilotInput("The V2 Chapter 1 source configuration has drifted from the reviewed contract.")
    return value


def _validate_protected_inputs(config: PilotConfig) -> ChapterConfig:
    v2_config = _validate_v2_source_config(config)
    if _sha256_path(config.source_pdf) != APPROVED_SOURCE_SHA256:
        raise PipelineBlocked("The Chapter 1 source PDF hash changed.")
    if _sha256_path(config.published_path) != APPROVED_PUBLISHED_SHA256:
        raise PipelineBlocked("The published Chapter 1 ZIP hash changed.")
    return v2_config


def _artifact_paths(config: PilotConfig) -> dict[str, str]:
    root = config.work_root
    return {
        "work_root": str(root),
        "baseline": str(root / "baseline" / "chapter-001.jsonl"),
        "agent_jobs": str(root / "agent-review" / "jobs"),
        "agent_results": str(root / "agent-review-results"),
        "routes": str(root / "routing" / "agent-routes.jsonl"),
        "vision_jobs": str(root / "vision" / "jobs"),
        "vision_results": str(root / "vision-results"),
        "audit": str(root / "audit" / "chapter-001-agent-triage.json"),
        "renders": str(root / "renders"),
        "candidate": str(config.candidate_path),
        "published": str(config.published_path),
    }


def _status(config: PilotConfig) -> dict[str, object]:
    baseline = config.work_root / "baseline" / "chapter-001.jsonl"
    if not config.work_root.exists():
        stage = "not_prepared"
    elif not baseline.is_file():
        raise PipelineBlocked("The pilot work root exists without its canonical Chapter 1 baseline.")
    else:
        baselines = _load_baselines(config)
        jobs = _load_agent_jobs(config, baselines)
        agent_summary, _, routes = _agent_progress(
            config,
            baselines,
            jobs,
            config.work_root / "agent-review-results",
            mutate=False,
        )
        agent_pending = int(agent_summary["pending"])
        base = {
            "command": "status",
            "status": "complete",
            "pending_jobs": agent_pending,
            "pending_agent_jobs": agent_pending,
            "pending_vision_jobs": 0,
            "total_baselines": 380,
            "agent_terminal": 380 - agent_pending,
            "accepted_python": int(agent_summary["accepted_python"]),
            "vision_required": int(agent_summary["vision_required"]),
            "vision_terminal": 0,
            "vision_accepted": 0,
            "included": int(agent_summary["accepted_python"]),
            "quarantined": 0,
            "paths": _artifact_paths(config),
        }
        if agent_pending:
            return {**base, "stage": "agent_review"}
        vision_index = config.work_root / "vision" / "vision-jobs.jsonl"
        if not vision_index.is_file():
            return {**base, "stage": "vision_preparation", "pending_jobs": 0}
        vision_jobs = _load_vision_jobs(config, routes)
        vision_results = _load_vision_results_read_only(
            vision_jobs, config.work_root / "vision-results"
        )
        vision_pending = len(vision_jobs) - len(vision_results)
        vision_accepted = sum(result.decision == "VISION_ACCEPTED" for result in vision_results)
        quarantined = sum(result.decision == "QUARANTINE" for result in vision_results)
        stage = "vision_review" if vision_pending else "ready_to_finalize"
        if config.candidate_path.is_file():
            stage = "candidate_ready"
        return {
            **base,
            "stage": stage,
            "pending_jobs": vision_pending,
            "pending_vision_jobs": vision_pending,
            "vision_required": len(vision_jobs),
            "vision_terminal": len(vision_results),
            "vision_accepted": vision_accepted,
            "included": int(agent_summary["accepted_python"]) + vision_accepted,
            "quarantined": quarantined,
        }
    return {
        "command": "status",
        "stage": stage,
        "status": "complete",
        "pending_jobs": 0,
        "pending_agent_jobs": 0,
        "pending_vision_jobs": 0,
        "total_baselines": 0,
        "agent_terminal": 0,
        "vision_required": 0,
        "vision_terminal": 0,
        "included": 0,
        "quarantined": 0,
        "paths": _artifact_paths(config),
    }


def _parse_arguments(arguments: list[str], workspace_root: Path) -> tuple[str, Path, Path | None]:
    if not arguments or arguments[0] not in COMMANDS:
        raise InvalidPilotInput(f"Unknown or missing command: {arguments[0] if arguments else '<none>'}")
    command = arguments[0]
    config_path = workspace_root / "data-engineering/python_vision_calibration/configs/chapter-001-agent-triage.json"
    results_path: Path | None = None
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--config", "--results"} or index + 1 >= len(arguments):
            raise InvalidPilotInput(f"Invalid command arguments near {option!r}.")
        value = Path(arguments[index + 1])
        if option == "--config":
            config_path = value if value.is_absolute() else workspace_root / value
        else:
            results_path = value if value.is_absolute() else workspace_root / value
        index += 2
    return command, config_path, results_path


def _base_error_payload(
    command: str, status: str, config: PilotConfig | None = None
) -> dict[str, object]:
    return {
        "command": command,
        "stage": "configuration",
        "status": status,
        "pending_jobs": 0,
        "pending_agent_jobs": 0,
        "pending_vision_jobs": 0,
        "paths": {} if config is None else _artifact_paths(config),
    }


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    workspace_root: Path | None = None,
) -> int:
    """Execute one pilot command and return its documented process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    diagnostics = sys.stderr if stderr is None else stderr
    command = arguments[0] if arguments else ""
    root = Path(workspace_root or WORKSPACE_ROOT).resolve()
    captured = io.StringIO()
    config: PilotConfig | None = None
    try:
        parsed_command, config_path, supplied_results = _parse_arguments(arguments, root)
        config = load_pilot_config(config_path, workspace_root=root)
        if supplied_results is not None and parsed_command not in {
            "ingest-agent", "ingest-vision", "finalize"
        }:
            raise InvalidPilotInput(f"{parsed_command} does not accept --results.")
        with contextlib.redirect_stdout(captured):
            v2_config = _validate_protected_inputs(config)
            if parsed_command == "status":
                payload = _status(config)
                code = 0
            elif parsed_command == "prepare":
                _prepare(config)
                payload = _prepared_payload(parsed_command, config)
                code = 0
            elif parsed_command in {"ingest-agent", "run", "prepare-vision", "ingest-vision", "finalize"}:
                if parsed_command == "run":
                    baselines, jobs = _prepare(config)
                else:
                    baselines = _load_baselines(config)
                    jobs = _load_agent_jobs(config, baselines)
                agent_supplied = supplied_results if parsed_command == "ingest-agent" else None
                results_dir = _validated_results_path(config, agent_supplied, "agent")
                summary, results, routes = _agent_progress(
                    config, baselines, jobs, results_dir, mutate=True
                )
                _checkpoint(config, "agent-review", {
                    "baseline_sha256": _sha256_path(config.work_root / "baseline/chapter-001.jsonl"),
                    "agent_queue_sha256": _sha256_path(config.work_root / "agent-review/agent-review-jobs.jsonl"),
                    "result_hashes": {record_id: value.result_sha256 for record_id, value in sorted(results.items())},
                    "route_hashes": {route.record_id: route.route_sha256 for route in routes},
                    "pending": int(summary["pending"]),
                })
                payload = _agent_payload(parsed_command, config, summary)
                if int(summary["pending"]):
                    code = 20
                elif parsed_command == "ingest-agent":
                    code = 0
                else:
                    vision_results_dir = _validated_results_path(
                        config,
                        supplied_results if parsed_command in {"ingest-vision", "finalize"} else None,
                        "vision",
                    )
                    code, payload = _execute_vision_command(
                        parsed_command,
                        config,
                        v2_config,
                        baselines,
                        jobs,
                        results,
                        routes,
                        summary,
                        vision_results_dir,
                    )
            else:
                raise InvalidPilotInput(f"Command implementation is not available yet: {parsed_command}.")
    except InvalidPilotInput as error:
        diagnostics.write(f"{error}\n")
        payload = _base_error_payload(command, "invalid", config)
        code = 22
    except (ValueError, TypeError) as error:
        diagnostics.write(f"{error}\n")
        payload = _base_error_payload(command, "invalid", config)
        code = 22
    except PipelineBlocked as error:
        diagnostics.write(f"{error}\n")
        payload = _base_error_payload(command, "blocked", config)
        code = 21
    except Exception as error:
        diagnostics.write(f"{type(error).__name__}: {error}\n")
        payload = _base_error_payload(command, "blocked", config)
        code = 21
    incidental = captured.getvalue()
    if incidental:
        diagnostics.write(incidental)
    _emit(output, payload)
    return code


__all__ = [
    "COMMANDS", "EXPECTED_RECORD_IDS", "InvalidPilotInput", "PilotConfig", "WORKSPACE_ROOT",
    "load_pilot_config", "main", "validate_pilot_config_payload",
]
