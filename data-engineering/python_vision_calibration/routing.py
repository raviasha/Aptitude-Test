"""Final deterministic routing after strict one-record agent review ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from textbook_chapters_v2.models import PipelineBlocked

from .agent_review import ingest_agent_review_result
from .diagnostics import hard_warning_codes
from .models import AgentReviewJob, AgentReviewResult, RawBaselineRecord, RouteDecision


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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _route_hash_payload(
    *,
    record_id: str,
    decision: str,
    candidate: Mapping[str, object],
    baseline_sha256: str,
    review_result_sha256: str,
    reason_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "decision": decision,
        "candidate": candidate,
        "baseline_sha256": baseline_sha256,
        "review_result_sha256": review_result_sha256,
        "reason_codes": list(reason_codes),
    }


def resolve_agent_route(record: RawBaselineRecord, review: AgentReviewResult) -> RouteDecision:
    """Bind an already-validated review result to an immutable baseline route."""
    if not isinstance(record, RawBaselineRecord):
        raise TypeError("record must be a RawBaselineRecord value.")
    if not isinstance(review, AgentReviewResult):
        raise TypeError("review must be an AgentReviewResult value.")
    if record.chapter != 1:
        raise PipelineBlocked("Agent routing is limited to Chapter 1.")
    if review.record_id != record.record_id:
        raise ValueError("Agent review record_id does not match its baseline record.")
    if review.baseline_sha256 != record.baseline_sha256:
        raise ValueError("Agent review is stale against its baseline hash.")

    warnings = hard_warning_codes(record)
    candidate = deepcopy(record.candidate)
    if warnings:
        decision = "VISION_REQUIRED"
        reason_codes = tuple(sorted(("FORCED_BY_DIAGNOSTIC", *warnings)))
    elif review.decision == "ACCEPT_PYTHON":
        decision = "ACCEPT_PYTHON"
        reason_codes = ()
    else:
        decision = "VISION_REQUIRED"
        reason_codes = tuple(sorted(("AGENT_VISION_REQUIRED", *(f"AGENT_REVIEW:{code}" for code in review.reason_codes))))

    route_sha256 = _sha256(_route_hash_payload(
        record_id=record.record_id,
        decision=decision,
        candidate=candidate,
        baseline_sha256=record.baseline_sha256,
        review_result_sha256=review.result_sha256,
        reason_codes=reason_codes,
    ))
    return RouteDecision(
        record_id=record.record_id,
        decision=decision,
        candidate=candidate,
        baseline_sha256=record.baseline_sha256,
        review_result_sha256=review.result_sha256,
        reason_codes=reason_codes,
        route_sha256=route_sha256,
    )


def _atomic_write_jsonl(path: Path, values: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            for value in values:
                temporary.write(_canonical_bytes(value) + b"\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def _validate_records_and_jobs(
    records: Iterable[RawBaselineRecord], jobs: Iterable[AgentReviewJob]
) -> tuple[tuple[RawBaselineRecord, ...], dict[str, AgentReviewJob]]:
    record_values = tuple(records)
    job_values = tuple(jobs)
    if any(not isinstance(record, RawBaselineRecord) for record in record_values):
        raise TypeError("records must contain RawBaselineRecord values.")
    if any(not isinstance(job, AgentReviewJob) for job in job_values):
        raise TypeError("jobs must contain AgentReviewJob values.")
    if any(record.chapter != 1 for record in record_values):
        raise PipelineBlocked("Agent routing is limited to Chapter 1.")

    record_ids = [record.record_id for record in record_values]
    job_ids = [job.record_id for job in job_values]
    if len(set(record_ids)) != len(record_ids):
        raise PipelineBlocked("Agent routing refuses duplicate baseline record IDs.")
    if len(set(job_ids)) != len(job_ids):
        raise PipelineBlocked("Agent routing refuses duplicate agent-review jobs.")
    if set(record_ids) != set(job_ids):
        raise PipelineBlocked("Agent routing requires exactly one job per baseline record.")

    jobs_by_id = {job.record_id: job for job in job_values}
    for record in record_values:
        if jobs_by_id[record.record_id].baseline_sha256 != record.baseline_sha256:
            raise PipelineBlocked("Agent routing job is stale against its baseline record.")
    return tuple(sorted(record_values, key=lambda record: record.record_id)), jobs_by_id


def _queue_path(jobs: Mapping[str, AgentReviewJob], work_root: Path) -> Path:
    roots = {job.output_path.parent.parent for job in jobs.values()}
    if len(roots) == 1:
        return next(iter(roots)) / "agent-review-jobs.jsonl"
    return work_root / "agent-review" / "agent-review-jobs.jsonl"


def _result_inventory(directory: Path, expected_ids: set[str]) -> dict[str, Path]:
    """Require exactly one canonical `<record_id>.json` result file per observed ID."""
    if not directory.exists():
        return {}

    expected_filenames = {f"{record_id}.json".casefold(): record_id for record_id in expected_ids}
    observed: dict[str, list[Path]] = {}
    unexpected: list[str] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        record_id = expected_filenames.get(path.name.casefold())
        if record_id is not None:
            observed.setdefault(record_id, []).append(path)
        elif path.suffix.lower() == ".json":
            unexpected.append(path.name)
    if unexpected:
        raise ValueError(f"Agent review results contain unexpected result files: {', '.join(unexpected)}.")

    inventory: dict[str, Path] = {}
    for record_id, paths in sorted(observed.items()):
        if len(paths) != 1:
            names = ", ".join(path.name for path in paths)
            raise ValueError(f"Agent review results contain duplicate evidence for {record_id}: {names}.")
        path = paths[0]
        canonical_name = f"{record_id}.json"
        if path.name != canonical_name:
            raise ValueError(
                f"Agent review result filename is noncanonical for {record_id}: "
                f"expected {canonical_name}, found {path.name}."
            )
        inventory[record_id] = path
    return inventory


def ingest_agent_review_directory(
    records: Iterable[RawBaselineRecord],
    jobs: Iterable[AgentReviewJob],
    results_dir: Path,
    work_root: Path,
) -> dict[str, object]:
    """Ingest current one-record results and persist routes only at a complete boundary."""
    ordered_records, jobs_by_id = _validate_records_and_jobs(records, jobs)
    directory = Path(results_dir)
    root = Path(work_root)
    if directory.exists() and not directory.is_dir():
        raise ValueError("Agent review results path must be a directory.")

    inventory = _result_inventory(directory, set(jobs_by_id))

    routes: list[RouteDecision] = []
    pending = 0
    for record in ordered_records:
        result_path = inventory.get(record.record_id)
        if result_path is None:
            pending += 1
            continue
        review = ingest_agent_review_result(jobs_by_id[record.record_id], result_path)
        routes.append(resolve_agent_route(record, review))

    accepted_python = sum(route.decision == "ACCEPT_PYTHON" for route in routes)
    vision_required = sum(route.decision == "VISION_REQUIRED" for route in routes)
    routes_path = root / "routing" / "agent-routes.jsonl"
    if pending == 0:
        _atomic_write_jsonl(routes_path, (_json_value(route) for route in routes))

    return {
        "total": len(ordered_records),
        "accepted_python": accepted_python,
        "vision_required": vision_required,
        "pending": pending,
        "queue_path": str(_queue_path(jobs_by_id, root)),
        "results_path": str(directory),
    }
