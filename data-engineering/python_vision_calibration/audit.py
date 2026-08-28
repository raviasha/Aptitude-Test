"""Canonical append-proof audit for the Chapter 1 agent-triage pilot."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textbook_chapters_v2.models import CandidateRecord, PipelineBlocked, RecordEvidence, freeze_value
from textbook_chapters_v2.store import canonical_json, dependency_fingerprint

from .agent_review import (
    AGENT_REVIEW_CHECKS,
    AGENT_REVIEW_MIN_CONFIDENCE,
    AGENT_REVIEW_PROMPT,
    AGENT_REVIEW_PROMPT_VERSION,
    _job_content as _agent_job_content,
    _job_document as _agent_job_document,
    _record_payload as _agent_record_payload,
    _schema as _agent_result_schema,
    _validate_schema as _validate_agent_result_schema,
)
from .merge import (
    _canonical_sha256,
    _expected_record_ids,
    _index_exact,
    _json_value,
    _record_number,
    _require_hash,
    _result_payload,
    _sha256_path,
    _validate_quarantine_shape,
    _validate_baseline,
    _validate_route,
    _validate_vision_job_canonical,
    _vision_job_index,
    merge_final_candidates,
)
from .models import (
    AgentReviewJob,
    AgentReviewResult,
    RawBaselineRecord,
    RouteDecision,
    VisionFallbackJob,
    VisionFallbackResult,
)
from .routing import resolve_agent_route


AUDIT_SCHEMA_VERSION = 1
AUDIT_FILENAME = "chapter-001-agent-triage.json"
_RENDER_VIEWPORTS = ((1024, 768), (1600, 900))
_RENDER_STATES = ("unanswered", "submitted")
_RENDER_FIELDS = {
    "record_id",
    "candidate_sha256",
    "complete",
    "renderer_fingerprint",
    "application_fingerprint",
    "browser_fingerprint",
    "browser_identity",
    "viewports",
    "screenshots",
    "findings",
    "dependency_fingerprint",
    "manifest_sha256",
}


@dataclass(frozen=True)
class PilotAuditSummary:
    """Immutable verified view of one persisted pilot audit."""

    path: Path
    records: tuple[Mapping[str, Any], ...]
    counts: Mapping[str, int]
    dependency_fingerprint: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "records", tuple(freeze_value(record) for record in self.records))
        object.__setattr__(self, "counts", freeze_value(dict(self.counts)))


def _candidate_id(candidate: CandidateRecord) -> str:
    if candidate.chapter != 1:
        raise PipelineBlocked("Pilot audit candidates must belong to Chapter 1.")
    return f"ch{candidate.chapter:02d}-q{candidate.question_number:04d}"


def _candidate_index(candidates: Iterable[CandidateRecord]) -> dict[str, CandidateRecord]:
    values = tuple(candidates)
    if any(not isinstance(candidate, CandidateRecord) for candidate in values):
        raise TypeError("candidates must contain CandidateRecord values.")
    ids = [_candidate_id(candidate) for candidate in values]
    if len(set(ids)) != len(ids):
        raise PipelineBlocked("Pilot audit refuses duplicate final candidate record IDs.")
    return dict(zip(ids, values))


def _typed_mapping(
    values: Mapping[str, Any] | None,
    expected_type: type,
    name: str,
    expected_ids: tuple[str, ...],
) -> dict[str, Any]:
    if values is None:
        supplied: Mapping[str, Any] = {}
    elif not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping keyed by record ID.")
    else:
        supplied = values
    items: list[Any] = []
    for key, item in supplied.items():
        if not isinstance(key, str) or not isinstance(item, expected_type):
            raise TypeError(f"{name} must map record IDs to {expected_type.__name__} values.")
        if key != item.record_id:
            raise PipelineBlocked(f"Pilot audit {name} mapping key is stale for {key}.")
        items.append(item)
    return _index_exact(items, expected_type, name, expected_ids, lambda item: item.record_id)


def _agent_result_payload(result: AgentReviewResult) -> dict[str, Any]:
    return {
        "record_id": result.record_id,
        "decision": result.decision,
        "confidence": result.confidence,
        "checks": dict(result.checks),
        "reason_codes": list(result.reason_codes),
        "explanation": result.explanation,
        "reviewer": result.reviewer,
        "baseline_sha256": result.baseline_sha256,
        "job_sha256": result.job_sha256,
    }


def _validate_agent_dependencies(
    baseline_by_id: Mapping[str, RawBaselineRecord],
    route_by_id: Mapping[str, RouteDecision],
    agent_jobs: Mapping[str, AgentReviewJob] | None,
    agent_results: Mapping[str, AgentReviewResult] | None,
    expected_ids: tuple[str, ...],
) -> tuple[dict[str, AgentReviewJob], dict[str, AgentReviewResult]]:
    job_by_id = _typed_mapping(agent_jobs, AgentReviewJob, "agent job", expected_ids)
    result_by_id = _typed_mapping(agent_results, AgentReviewResult, "agent result", expected_ids)
    prompt_sha256 = _canonical_sha256(AGENT_REVIEW_PROMPT, ensure_ascii=False)
    for record_id in expected_ids:
        baseline = baseline_by_id[record_id]
        route = route_by_id[record_id]
        job = job_by_id[record_id]
        result = result_by_id[record_id]
        record_payload = _agent_record_payload(baseline)
        job_content = _agent_job_content(record_payload)
        payload_sha256 = _canonical_sha256(job_content, ensure_ascii=False)
        expected_job_hash = _canonical_sha256({
            "record_id": record_id,
            "baseline_sha256": baseline.baseline_sha256,
            "prompt_version": AGENT_REVIEW_PROMPT_VERSION,
            "prompt_sha256": prompt_sha256,
            "payload_sha256": payload_sha256,
            "output_schema": job.output_schema,
        }, ensure_ascii=False)
        if (
            job.baseline_sha256 != baseline.baseline_sha256
            or job.prompt_version != AGENT_REVIEW_PROMPT_VERSION
            or job.prompt_sha256 != prompt_sha256
            or job.payload_sha256 != payload_sha256
            or job.output_schema != "agent-review-result.schema.json"
            or job.job_sha256 != expected_job_hash
        ):
            raise PipelineBlocked(f"{record_id} authoritative agent job is missing or stale.")
        if not job.output_path.is_file():
            raise PipelineBlocked(f"{record_id} authoritative agent job file is missing.")
        try:
            persisted_job = json.loads(job.output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PipelineBlocked(f"{record_id} authoritative agent job file is invalid.") from error
        if persisted_job != _json_value(_agent_job_document(job, record_payload)):
            raise PipelineBlocked(f"{record_id} authoritative agent job file is stale.")

        full_result_payload = {**_agent_result_payload(result), "result_sha256": result.result_sha256}
        try:
            _validate_agent_result_schema(full_result_payload, _agent_result_schema())
        except (TypeError, ValueError, RuntimeError) as error:
            raise PipelineBlocked(f"{record_id} authoritative agent result is malformed: {error}") from error
        if (
            result.baseline_sha256 != baseline.baseline_sha256
            or result.job_sha256 != job.job_sha256
            or route.review_result_sha256 != result.result_sha256
            or not isinstance(result.confidence, float)
            or not math.isfinite(result.confidence)
            or not 0.0 <= result.confidence <= 1.0
            or set(result.checks) != set(AGENT_REVIEW_CHECKS)
            or any(value not in {"PASS", "SUSPECT"} for value in result.checks.values())
            or not result.reviewer.strip()
        ):
            raise PipelineBlocked(f"{record_id} authoritative agent result is missing or stale.")
        if result.decision == "ACCEPT_PYTHON":
            if (
                result.confidence < AGENT_REVIEW_MIN_CONFIDENCE
                or set(result.checks.values()) != {"PASS"}
                or result.reason_codes
            ):
                raise PipelineBlocked(f"{record_id} authoritative agent result cannot accept Python.")
        elif result.decision == "VISION_REQUIRED":
            if not result.reason_codes or not result.explanation.strip():
                raise PipelineBlocked(f"{record_id} authoritative agent result lacks its vision reason.")
        else:
            raise PipelineBlocked(f"{record_id} authoritative agent result decision is invalid.")
        if result.result_sha256 != _canonical_sha256(
            _agent_result_payload(result), ensure_ascii=False
        ):
            raise PipelineBlocked(f"{record_id} authoritative agent result hash is stale.")
        if _json_value(route) != _json_value(resolve_agent_route(baseline, result)):
            raise PipelineBlocked(f"{record_id} route contradicts its authoritative agent result.")
    return job_by_id, result_by_id


def _manifest_values(render_manifests: Any) -> tuple[tuple[str | None, Any], ...]:
    if render_manifests is None:
        return ()
    if isinstance(render_manifests, Mapping):
        if "record_id" in render_manifests:
            return ((None, render_manifests),)
        return tuple((str(key), value) for key, value in render_manifests.items())
    if isinstance(render_manifests, (str, bytes)):
        raise TypeError("render_manifests must contain manifest objects.")
    return tuple((None, value) for value in render_manifests)


def _render_index(render_manifests: Any, candidate_by_id: Mapping[str, CandidateRecord]) -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for declared_id, raw in _manifest_values(render_manifests):
        normalized = _json_value(raw)
        if not isinstance(normalized, dict):
            raise TypeError("render_manifests must contain mapping or dataclass values.")
        record_id = normalized.get("record_id")
        if not isinstance(record_id, str):
            raise PipelineBlocked("Pilot audit render manifest is missing record_id.")
        _record_number(record_id)
        if declared_id is not None and declared_id != record_id:
            raise PipelineBlocked(f"Pilot audit render mapping key is stale for {record_id}.")
        if record_id in manifests:
            raise PipelineBlocked(f"Pilot audit refuses duplicate render manifests for {record_id}.")
        candidate = candidate_by_id.get(record_id)
        if candidate is None:
            raise PipelineBlocked(f"Pilot audit has an extra render manifest for {record_id}.")
        if set(normalized) != _RENDER_FIELDS:
            raise PipelineBlocked("Pilot audit render manifest has missing or extra fields.")
        if normalized.get("candidate_sha256") != candidate.sha256:
            raise PipelineBlocked(f"Pilot audit render manifest is stale against candidate {record_id}.")
        if not isinstance(normalized.get("complete"), bool):
            raise PipelineBlocked(f"Pilot audit render completion flag is malformed for {record_id}.")
        for field in ("renderer_fingerprint", "application_fingerprint", "browser_fingerprint"):
            _require_hash(normalized.get(field), f"{record_id} render {field}")
        if not isinstance(normalized.get("browser_identity"), str) or not normalized["browser_identity"].strip():
            raise PipelineBlocked(f"Pilot audit browser identity is malformed for {record_id}.")
        if normalized.get("viewports") != [list(viewport) for viewport in _RENDER_VIEWPORTS]:
            raise PipelineBlocked(f"Pilot audit render viewport inventory is invalid for {record_id}.")
        screenshots = normalized.get("screenshots")
        expected_viewports = {f"{width}x{height}" for width, height in _RENDER_VIEWPORTS}
        if not isinstance(screenshots, dict) or set(screenshots) != expected_viewports:
            raise PipelineBlocked(f"Pilot audit render screenshot viewport inventory is invalid for {record_id}.")
        for viewport in sorted(expected_viewports):
            states = screenshots[viewport]
            if not isinstance(states, dict) or set(states) != set(_RENDER_STATES):
                raise PipelineBlocked(f"Pilot audit render screenshot state inventory is invalid for {record_id}.")
            for state in _RENDER_STATES:
                screenshot = states[state]
                if not isinstance(screenshot, dict) or set(screenshot) != {"path", "sha256"}:
                    raise PipelineBlocked(f"Pilot audit render screenshot entry is malformed for {record_id}.")
                path_value = screenshot.get("path")
                if not isinstance(path_value, str) or not path_value:
                    raise PipelineBlocked(f"Pilot audit render screenshot path is malformed for {record_id}.")
                screenshot_path = Path(path_value)
                if not screenshot_path.is_file():
                    raise PipelineBlocked(f"Pilot audit render screenshot is missing for {record_id}.")
                digest = _require_hash(screenshot.get("sha256"), f"{record_id} render screenshot hash")
                if _sha256_path(screenshot_path) != digest:
                    raise PipelineBlocked(f"Pilot audit render screenshot hash is stale for {record_id}.")
        findings = normalized.get("findings", [])
        if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
            raise PipelineBlocked(f"Pilot audit render findings are malformed for {record_id}.")
        core = {
            key: value
            for key, value in normalized.items()
            if key not in {"dependency_fingerprint", "manifest_sha256"}
        }
        expected_dependency = dependency_fingerprint(core)
        if normalized.get("dependency_fingerprint") != expected_dependency:
            raise PipelineBlocked(f"Pilot audit render dependency fingerprint is stale for {record_id}.")
        with_dependency = {**core, "dependency_fingerprint": expected_dependency}
        if normalized.get("manifest_sha256") != dependency_fingerprint(with_dependency):
            raise PipelineBlocked(f"Pilot audit render manifest hash is stale for {record_id}.")
        manifests[record_id] = normalized
    return manifests


def _render_is_complete(manifest: Mapping[str, Any] | None) -> bool:
    if manifest is None:
        return False
    findings = manifest.get("findings")
    return bool(
        manifest.get("complete") is True
        and findings == []
    )


def _validate_quarantines_without_prepared_evidence(
    baselines: tuple[RawBaselineRecord, ...],
    routes: tuple[RouteDecision, ...],
    results: tuple[VisionFallbackResult, ...],
    candidates: tuple[CandidateRecord, ...],
    vision_jobs: Mapping[str, VisionFallbackJob] | None,
    expected_ids: tuple[str, ...],
) -> dict[str, VisionFallbackJob]:
    """Support the documented quarantine-only audit call without trusting candidate content."""
    baseline_by_id = _index_exact(
        baselines, RawBaselineRecord, "baseline", expected_ids, lambda item: item.record_id
    )
    route_by_id = _index_exact(routes, RouteDecision, "route", expected_ids, lambda item: item.record_id)
    result_by_id = _index_exact(
        results, VisionFallbackResult, "vision result", expected_ids, lambda item: item.record_id
    )
    job_by_id = _vision_job_index(vision_jobs, expected_ids)
    if candidates:
        raise PipelineBlocked("Pilot audit needs prepared source evidence for supposedly accepted candidates.")
    for record_id in expected_ids:
        baseline = baseline_by_id[record_id]
        _validate_baseline(baseline)
        route = route_by_id[record_id]
        _validate_route(route, baseline)
        result = result_by_id[record_id]
        job = job_by_id[record_id]
        if route.decision != "VISION_REQUIRED" or result.decision != "QUARANTINE":
            raise PipelineBlocked("Pilot audit needs prepared source evidence for supposedly accepted records.")
        _validate_vision_job_canonical(job)
        if (
            job.route_sha256 != route.route_sha256
            or job.baseline_sha256 != baseline.baseline_sha256
            or result.route_sha256 != route.route_sha256
            or result.job_sha256 != job.job_sha256
        ):
            raise PipelineBlocked(f"{record_id} quarantine is stale against its route.")
        _require_hash(result.job_sha256, f"{record_id} vision job hash")
        _require_hash(result.result_sha256, f"{record_id} vision result hash")
        if result.result_sha256 != _canonical_sha256(_result_payload(result)):
            raise PipelineBlocked(f"{record_id} quarantine result hash is stale.")
        if not isinstance(result.reviewer, str) or not result.reviewer.strip():
            raise PipelineBlocked(f"{record_id} quarantine reviewer is missing.")
        if (
            not result.source_evidence_sha256s
            or len(set(result.source_evidence_sha256s)) != len(result.source_evidence_sha256s)
            or any(
                not isinstance(item, str) or item not in job.source_evidence_sha256s
                for item in result.source_evidence_sha256s
            )
        ):
            raise PipelineBlocked(f"{record_id} quarantine source evidence is missing.")
        for digest in result.source_evidence_sha256s:
            _require_hash(digest, f"{record_id} quarantine source evidence hash")
        if not result.quarantine_reason.strip():
            raise PipelineBlocked(f"{record_id} quarantine reason is missing.")
        _validate_quarantine_shape(result)
    return job_by_id


def _record_source_evidence(
    record_id: str,
    baseline: RawBaselineRecord,
    result: VisionFallbackResult | None,
    evidence_by_id: Mapping[str, RecordEvidence],
) -> Any:
    current = evidence_by_id.get(record_id)
    if current is not None:
        return _json_value(current)
    if result is not None:
        return {"source_evidence_sha256s": list(result.source_evidence_sha256s)}
    return {"baseline_source_hashes": _json_value(baseline.source_hashes)}


def _record_payload(
    record_id: str,
    baseline: RawBaselineRecord,
    agent_job: AgentReviewJob,
    agent_result: AgentReviewResult,
    route: RouteDecision,
    vision_job: VisionFallbackJob | None,
    result: VisionFallbackResult | None,
    candidate: CandidateRecord | None,
    evidence_by_id: Mapping[str, RecordEvidence],
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if result is not None and result.decision == "QUARANTINE":
        status = "QUARANTINED"
    elif not _render_is_complete(manifest):
        status = "PENDING_RENDER"
    elif route.decision == "ACCEPT_PYTHON":
        status = "PYTHON_ACCEPTED"
    else:
        status = "VISION_ACCEPTED"
    render_hashes = {}
    if manifest is not None:
        for viewport, states in manifest["screenshots"].items():
            for state, screenshot in states.items():
                render_hashes[f"{viewport}-{state}"] = screenshot["sha256"]
    prompt_sha256 = _canonical_sha256(AGENT_REVIEW_PROMPT)
    core = {
        "record_id": record_id,
        "chapter": 1,
        "question_number": _record_number(record_id),
        "status": status,
        "baseline": _json_value(baseline),
        "prompt": {
            "version": AGENT_REVIEW_PROMPT_VERSION,
            "sha256": prompt_sha256,
            "text": AGENT_REVIEW_PROMPT,
        },
        "agent_job": _json_value(agent_job),
        "agent_result": _json_value(agent_result),
        "route": _json_value(route),
        "vision_job": None if vision_job is None else _json_value(vision_job),
        "vision_result": None if result is None else _json_value(result),
        "final_candidate": None if candidate is None else _json_value(candidate),
        "source_evidence": _record_source_evidence(record_id, baseline, result, evidence_by_id),
        "render_hashes": render_hashes,
        "render_manifest": None if manifest is None else dict(manifest),
    }
    record_dependency = dependency_fingerprint(core)
    with_dependency = {**core, "dependency_fingerprint": record_dependency}
    return {**with_dependency, "record_sha256": dependency_fingerprint(with_dependency)}


def _audit_payload(
    expected_ids: tuple[str, ...], records: tuple[dict[str, Any], ...], counts: Mapping[str, int]
) -> dict[str, Any]:
    dependency = dependency_fingerprint({
        "expected_record_ids": list(expected_ids),
        "record_dependency_fingerprints": [record["dependency_fingerprint"] for record in records],
    })
    base = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "chapter": 1,
        "record_count": len(records),
        "expected_record_ids": list(expected_ids),
        "counts": dict(counts),
        "dependency_fingerprint": dependency,
        "records": list(records),
    }
    return {**base, "audit_sha256": dependency_fingerprint(base)}


def _validate_persisted(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PipelineBlocked("Existing pilot audit is not a JSON object.")
    required = {
        "schema_version", "chapter", "record_count", "expected_record_ids", "counts",
        "dependency_fingerprint", "records", "audit_sha256",
    }
    if set(payload) != required or payload.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise PipelineBlocked("Existing pilot audit has an unsupported or malformed schema.")
    records = payload.get("records")
    if not isinstance(records, list) or payload.get("record_count") != len(records):
        raise PipelineBlocked("Existing pilot audit has a stale record count.")
    for record in records:
        if (
            not isinstance(record, dict)
            or "record_sha256" not in record
            or "dependency_fingerprint" not in record
        ):
            raise PipelineBlocked("Existing pilot audit contains a malformed record.")
        expected_dependency = dependency_fingerprint({
            key: value
            for key, value in record.items()
            if key not in {"dependency_fingerprint", "record_sha256"}
        })
        if record["dependency_fingerprint"] != expected_dependency:
            raise PipelineBlocked("Existing pilot audit contains a stale record dependency fingerprint.")
        expected = dependency_fingerprint({key: value for key, value in record.items() if key != "record_sha256"})
        if record["record_sha256"] != expected:
            raise PipelineBlocked("Existing pilot audit contains a stale record hash.")
    expected_dependency = dependency_fingerprint({
        "expected_record_ids": payload["expected_record_ids"],
        "record_dependency_fingerprints": [record["dependency_fingerprint"] for record in records],
    })
    if payload.get("dependency_fingerprint") != expected_dependency:
        raise PipelineBlocked("Existing pilot audit has a stale dependency fingerprint.")
    expected_audit = dependency_fingerprint({key: value for key, value in payload.items() if key != "audit_sha256"})
    if payload.get("audit_sha256") != expected_audit:
        raise PipelineBlocked("Existing pilot audit hash is stale.")
    return payload


def portable_pilot_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, path-free repository/package audit bound to the full audit."""
    current = _validate_persisted(dict(payload))
    records: list[dict[str, Any]] = []
    for record in current["records"]:
        agent = record["agent_result"]
        route = record["route"]
        vision_job = record["vision_job"]
        vision_result = record["vision_result"]
        candidate = record["final_candidate"]
        render = record["render_manifest"]
        baseline_sources = record["baseline"]["source_hashes"]
        records.append({
            "record_id": record["record_id"],
            "status": record["status"],
            "record_sha256": record["record_sha256"],
            "baseline_sha256": record["baseline"]["baseline_sha256"],
            "baseline_source_hashes": baseline_sources,
            "agent": {
                "decision": agent["decision"],
                "confidence": agent["confidence"],
                "reason_codes": agent["reason_codes"],
                "result_sha256": agent["result_sha256"],
            },
            "route": {
                "decision": route["decision"],
                "reason_codes": route["reason_codes"],
                "route_sha256": route["route_sha256"],
            },
            "vision": None if vision_job is None else {
                "job_sha256": vision_job["job_sha256"],
                "evidence_sha256": vision_job["evidence_sha256"],
                "source_dependency_fingerprint": vision_job["source_dependency_fingerprint"],
                "source_evidence_sha256s": vision_job["source_evidence_sha256s"],
                "decision": None if vision_result is None else vision_result["decision"],
                "result_sha256": None if vision_result is None else vision_result["result_sha256"],
            },
            "candidate_sha256": None if candidate is None else candidate["sha256"],
            "render_manifest_sha256": None if render is None else render["manifest_sha256"],
        })
    core = {
        "schema_version": 2,
        "chapter": 1,
        "record_count": current["record_count"],
        "expected_record_ids": current["expected_record_ids"],
        "counts": current["counts"],
        "full_audit_sha256": current["audit_sha256"],
        "full_audit_dependency_fingerprint": current["dependency_fingerprint"],
        "records": records,
    }
    return {**core, "dependency_fingerprint": dependency_fingerprint(core)}


def _read_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise PipelineBlocked("Pilot audit path exists but is not a file.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineBlocked("Existing pilot audit is unreadable or invalid JSON.") from error
    return _validate_persisted(payload)


def _protect_quarantine_transitions(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    prior_records = previous.get("records", [])
    current_records = current.get("records", [])
    if not isinstance(prior_records, list) or not isinstance(current_records, list):
        raise PipelineBlocked("Pilot audit transition records are malformed.")
    current_by_id = {record["record_id"]: record for record in current_records}
    for prior in prior_records:
        if prior.get("status") != "QUARANTINED":
            continue
        record_id = prior.get("record_id")
        replacement = current_by_id.get(record_id)
        if replacement is None or replacement.get("status") == "QUARANTINED":
            continue
        old_result = prior.get("vision_result")
        new_result = replacement.get("vision_result")
        old_hash = old_result.get("result_sha256") if isinstance(old_result, Mapping) else None
        if (
            not isinstance(new_result, Mapping)
            or new_result.get("decision") != "VISION_ACCEPTED"
            or new_result.get("result_sha256") == old_hash
        ):
            raise PipelineBlocked(
                f"Pilot audit cannot accept {record_id} after quarantine without a new terminal vision result."
            )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise


def _summary(path: Path, payload: Mapping[str, Any]) -> PilotAuditSummary:
    return PilotAuditSummary(
        path=path,
        records=tuple(payload["records"]),
        counts=payload["counts"],
        dependency_fingerprint=payload["dependency_fingerprint"],
        sha256=payload["audit_sha256"],
    )


def write_pilot_audit(
    baselines: Iterable[RawBaselineRecord],
    routes: Iterable[RouteDecision],
    vision_results: Iterable[VisionFallbackResult],
    candidates: Iterable[CandidateRecord],
    render_manifests: Any,
    work_root: Path,
    *,
    agent_jobs: Mapping[str, AgentReviewJob] | None = None,
    agent_results: Mapping[str, AgentReviewResult] | None = None,
    vision_jobs: Mapping[str, VisionFallbackJob] | None = None,
    evidence: Iterable[RecordEvidence] | None = None,
    expected_record_ids: Iterable[str] | None = None,
) -> PilotAuditSummary:
    """Validate and atomically replace the sole authoritative pilot audit."""
    baseline_values = tuple(baselines)
    route_values = tuple(routes)
    result_values = tuple(vision_results)
    candidate_values = tuple(candidates)
    if any(not isinstance(record, RawBaselineRecord) for record in baseline_values):
        raise TypeError("baselines must contain RawBaselineRecord values.")
    expected_ids = _expected_record_ids(baseline_values, expected_record_ids)
    baseline_by_id = _index_exact(
        baseline_values, RawBaselineRecord, "baseline", expected_ids, lambda item: item.record_id
    )
    route_by_id = _index_exact(
        route_values, RouteDecision, "route", expected_ids, lambda item: item.record_id
    )
    for record_id in expected_ids:
        _validate_baseline(baseline_by_id[record_id])
        _validate_route(route_by_id[record_id], baseline_by_id[record_id])
    agent_job_by_id, agent_result_by_id = _validate_agent_dependencies(
        baseline_by_id, route_by_id, agent_jobs, agent_results, expected_ids
    )
    vision_ids = tuple(
        record_id for record_id in expected_ids if route_by_id[record_id].decision == "VISION_REQUIRED"
    )
    evidence_values = tuple(evidence or ())
    if evidence_values or not vision_ids:
        expected_candidates = merge_final_candidates(
            baseline_values,
            route_values,
            result_values,
            evidence_values,
            vision_jobs=vision_jobs,
            expected_record_ids=expected_ids,
        )
        vision_job_by_id = _vision_job_index(vision_jobs, vision_ids)
    else:
        vision_job_by_id = _validate_quarantines_without_prepared_evidence(
            baseline_values,
            route_values,
            result_values,
            candidate_values,
            vision_jobs,
            expected_ids,
        )
        expected_candidates = ()
    supplied_by_id = _candidate_index(candidate_values)
    expected_by_id = _candidate_index(expected_candidates)
    if supplied_by_id != expected_by_id:
        raise PipelineBlocked("Pilot audit final candidate set is missing, extra, or stale.")

    result_by_id = _index_exact(
        result_values,
        VisionFallbackResult,
        "vision result",
        vision_ids,
        lambda item: item.record_id,
    )
    evidence_by_id = {
        f"ch{item.chapter:02d}-q{item.question_number:04d}": item for item in evidence_values
    }
    render_by_id = _render_index(render_manifests, supplied_by_id)
    records = tuple(
        _record_payload(
            record_id,
            baseline_by_id[record_id],
            agent_job_by_id[record_id],
            agent_result_by_id[record_id],
            route_by_id[record_id],
            vision_job_by_id.get(record_id),
            result_by_id.get(record_id),
            supplied_by_id.get(record_id),
            evidence_by_id,
            render_by_id.get(record_id),
        )
        for record_id in expected_ids
    )
    statuses = [record["status"] for record in records]
    counts = {
        "total_baselines": len(expected_ids),
        "python_accepts": sum(route.decision == "ACCEPT_PYTHON" for route in route_values),
        "vision_routes": sum(route.decision == "VISION_REQUIRED" for route in route_values),
        "vision_accepts": sum(result.decision == "VISION_ACCEPTED" for result in result_values),
        "quarantined": statuses.count("QUARANTINED"),
        "included": len(candidate_values),
        "pending_render": statuses.count("PENDING_RENDER"),
    }
    payload = _audit_payload(expected_ids, records, counts)
    path = Path(work_root) / "audit" / AUDIT_FILENAME
    existing = _read_existing(path)
    if existing is not None:
        if existing["dependency_fingerprint"] == payload["dependency_fingerprint"]:
            if canonical_json(existing) != canonical_json(payload):
                raise PipelineBlocked("Pilot audit changed without a declared dependency fingerprint change.")
            return _summary(path, existing)
        _protect_quarantine_transitions(existing, payload)
    _atomic_write(path, canonical_json(payload))
    return _summary(path, payload)
