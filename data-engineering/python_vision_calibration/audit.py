"""Canonical append-proof audit for the Chapter 1 agent-triage pilot."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textbook_chapters_v2.models import CandidateRecord, PipelineBlocked, RecordEvidence, freeze_value
from textbook_chapters_v2.store import canonical_json, dependency_fingerprint

from .agent_review import AGENT_REVIEW_PROMPT, AGENT_REVIEW_PROMPT_VERSION
from .merge import (
    _canonical_sha256,
    _expected_record_ids,
    _index_exact,
    _json_value,
    _record_number,
    _require_hash,
    _result_payload,
    _validate_baseline,
    _validate_route,
    merge_final_candidates,
)
from .models import RawBaselineRecord, RouteDecision, VisionFallbackResult


AUDIT_SCHEMA_VERSION = 1
AUDIT_FILENAME = "chapter-001-agent-triage.json"


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
        if normalized.get("candidate_sha256") != candidate.sha256:
            raise PipelineBlocked(f"Pilot audit render manifest is stale against candidate {record_id}.")
        hashes = normalized.get("screenshot_hashes", {})
        if not isinstance(hashes, dict):
            raise PipelineBlocked(f"Pilot audit render hashes are malformed for {record_id}.")
        for name, digest in hashes.items():
            if not isinstance(name, str) or not name:
                raise PipelineBlocked(f"Pilot audit render hash name is malformed for {record_id}.")
            _require_hash(digest, f"{record_id} render screenshot hash")
        findings = normalized.get("findings", [])
        if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
            raise PipelineBlocked(f"Pilot audit render findings are malformed for {record_id}.")
        if "complete" in normalized and not isinstance(normalized["complete"], bool):
            raise PipelineBlocked(f"Pilot audit render completion flag is malformed for {record_id}.")
        manifests[record_id] = normalized
    return manifests


def _render_is_complete(manifest: Mapping[str, Any] | None) -> bool:
    if manifest is None:
        return False
    hashes = manifest.get("screenshot_hashes")
    findings = manifest.get("findings")
    return bool(
        manifest.get("complete") is True
        and isinstance(hashes, Mapping)
        and hashes
        and findings == []
    )


def _validate_quarantines_without_prepared_evidence(
    baselines: tuple[RawBaselineRecord, ...],
    routes: tuple[RouteDecision, ...],
    results: tuple[VisionFallbackResult, ...],
    candidates: tuple[CandidateRecord, ...],
    expected_ids: tuple[str, ...],
) -> None:
    """Support the documented quarantine-only audit call without trusting candidate content."""
    baseline_by_id = _index_exact(
        baselines, RawBaselineRecord, "baseline", expected_ids, lambda item: item.record_id
    )
    route_by_id = _index_exact(routes, RouteDecision, "route", expected_ids, lambda item: item.record_id)
    result_by_id = _index_exact(
        results, VisionFallbackResult, "vision result", expected_ids, lambda item: item.record_id
    )
    if candidates:
        raise PipelineBlocked("Pilot audit needs prepared source evidence for supposedly accepted candidates.")
    for record_id in expected_ids:
        baseline = baseline_by_id[record_id]
        _validate_baseline(baseline)
        route = route_by_id[record_id]
        _validate_route(route, baseline)
        result = result_by_id[record_id]
        if route.decision != "VISION_REQUIRED" or result.decision != "QUARANTINE":
            raise PipelineBlocked("Pilot audit needs prepared source evidence for supposedly accepted records.")
        if result.route_sha256 != route.route_sha256:
            raise PipelineBlocked(f"{record_id} quarantine is stale against its route.")
        _require_hash(result.job_sha256, f"{record_id} vision job hash")
        _require_hash(result.result_sha256, f"{record_id} vision result hash")
        if result.result_sha256 != _canonical_sha256(_result_payload(result)):
            raise PipelineBlocked(f"{record_id} quarantine result hash is stale.")
        if not result.source_evidence_sha256s or any(
            not isinstance(item, str) for item in result.source_evidence_sha256s
        ):
            raise PipelineBlocked(f"{record_id} quarantine source evidence is missing.")
        for digest in result.source_evidence_sha256s:
            _require_hash(digest, f"{record_id} quarantine source evidence hash")
        if not result.quarantine_reason.strip():
            raise PipelineBlocked(f"{record_id} quarantine reason is missing.")


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
    route: RouteDecision,
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
    render_hashes = {} if manifest is None else dict(manifest.get("screenshot_hashes", {}))
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
        "agent_result": {"result_sha256": route.review_result_sha256},
        "route": _json_value(route),
        "vision_job": (
            None
            if result is None
            else {"job_sha256": result.job_sha256, "route_sha256": result.route_sha256}
        ),
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
    evidence_values = tuple(evidence or ())
    if evidence_values:
        expected_candidates = merge_final_candidates(
            baseline_values,
            route_values,
            result_values,
            evidence_values,
            expected_record_ids=expected_ids,
        )
    else:
        _validate_quarantines_without_prepared_evidence(
            baseline_values, route_values, result_values, candidate_values, expected_ids
        )
        expected_candidates = ()
    supplied_by_id = _candidate_index(candidate_values)
    expected_by_id = _candidate_index(expected_candidates)
    if supplied_by_id != expected_by_id:
        raise PipelineBlocked("Pilot audit final candidate set is missing, extra, or stale.")

    baseline_by_id = {record.record_id: record for record in baseline_values}
    route_by_id = {route.record_id: route for route in route_values}
    result_by_id = {result.record_id: result for result in result_values}
    evidence_by_id = {
        f"ch{item.chapter:02d}-q{item.question_number:04d}": item for item in evidence_values
    }
    render_by_id = _render_index(render_manifests, supplied_by_id)
    records = tuple(
        _record_payload(
            record_id,
            baseline_by_id[record_id],
            route_by_id[record_id],
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
