"""Atomic merge of Python baselines and terminal full-record vision results."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from textbook_chapters_v2.models import CandidateRecord, PipelineBlocked, RecordEvidence, SourceCrop
from textbook_chapters_v2.store import dependency_fingerprint

from .models import RawBaselineRecord, RouteDecision, VisionFallbackJob, VisionFallbackResult
from .vision_fallback import (
    _canonical_bytes as _vision_canonical_bytes,
    _evidence_sha256 as _vision_evidence_sha256,
    _job_payload as _vision_job_document,
    _prompt as _vision_prompt,
    _source_payload as _vision_source_payload,
    _source_reasons as _vision_source_reasons,
    _validate_job as _validate_vision_job_object,
)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^ch01-q([0-9]{4})$")
_OPTION_LABEL_SETS = (tuple("ABCD"), tuple("ABCDE"))
_BASELINE_SOURCE_FIELDS = {
    "source_pdf", "raw_extractor", "config", "question", "answer", "solution", "question_identity"
}
_VISION_SOURCE_FIELDS = {
    "kind",
    "role",
    "printed_question_number",
    "path",
    "sha256",
    "page_number",
    "box",
    "width",
    "height",
    "source_image_sha256",
    "source_dpi",
}
_T = TypeVar("_T")


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Canonical JSON does not support {type(value).__name__} values.")


def _canonical_sha256(value: Any, *, ensure_ascii: bool = True) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=ensure_ascii,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise PipelineBlocked(f"{field} must be a lowercase SHA-256 hash.")
    return value


def _record_number(record_id: Any) -> int:
    match = _RECORD_ID.fullmatch(record_id) if isinstance(record_id, str) else None
    if match is None:
        raise PipelineBlocked("Atomic merge is limited to canonical Chapter 1 record IDs.")
    number = int(match.group(1))
    if number < 1:
        raise PipelineBlocked("Atomic merge record IDs require positive printed question numbers.")
    return number


def _expected_record_ids(
    baselines: tuple[RawBaselineRecord, ...], expected_record_ids: Iterable[str] | None
) -> tuple[str, ...]:
    if expected_record_ids is None:
        values = tuple(record.record_id for record in baselines)
    else:
        if isinstance(expected_record_ids, (str, bytes)):
            raise TypeError("expected_record_ids must be an iterable of record IDs.")
        values = tuple(expected_record_ids)
    if any(not isinstance(record_id, str) for record_id in values):
        raise TypeError("expected_record_ids must contain strings.")
    if len(set(values)) != len(values):
        raise PipelineBlocked("Atomic merge expected record IDs contain duplicates.")
    for record_id in values:
        _record_number(record_id)
    if not values:
        raise PipelineBlocked("Atomic merge requires at least one expected record ID.")
    return tuple(sorted(values))


def _index_exact(
    values: Iterable[_T],
    expected_type: type,
    name: str,
    expected_ids: tuple[str, ...],
    record_id,
) -> dict[str, _T]:
    items = tuple(values)
    if any(not isinstance(item, expected_type) for item in items):
        raise TypeError(f"{name} must contain {expected_type.__name__} values.")
    ids = [record_id(item) for item in items]
    if len(set(ids)) != len(ids):
        raise PipelineBlocked(f"Atomic merge refuses duplicate {name} record IDs.")
    observed = set(ids)
    expected = set(expected_ids)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing:
        raise PipelineBlocked(f"Atomic merge is missing {name} records: {', '.join(missing)}.")
    if extra:
        raise PipelineBlocked(f"Atomic merge has extra {name} records: {', '.join(extra)}.")
    return dict(zip(ids, items))


def _baseline_payload(record: RawBaselineRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "chapter": record.chapter,
        "source_hashes": record.source_hashes,
        "source_identity": record.source_identity,
        "candidate": record.candidate,
    }


def _validate_baseline(record: RawBaselineRecord) -> int:
    number = _record_number(record.record_id)
    if record.chapter != 1:
        raise PipelineBlocked("Atomic merge is limited to Chapter 1 baselines.")
    if not isinstance(record.candidate, Mapping):
        raise PipelineBlocked(f"{record.record_id} baseline candidate must be a mapping.")
    if not isinstance(record.source_hashes, Mapping) or set(record.source_hashes) != _BASELINE_SOURCE_FIELDS:
        raise PipelineBlocked(f"{record.record_id} baseline source hashes are missing or extra.")
    for role, hashes in record.source_hashes.items():
        if not isinstance(hashes, (list, tuple)) or not hashes:
            raise PipelineBlocked(f"{record.record_id} baseline {role} evidence is missing.")
        for value in hashes:
            _require_hash(value, f"{record.record_id} baseline {role} evidence")
    if not isinstance(record.source_identity, Mapping):
        raise PipelineBlocked(f"{record.record_id} baseline source identity is missing.")
    if record.source_identity.get("number") != number:
        raise PipelineBlocked(f"{record.record_id} baseline source identity is mixed provenance.")
    if record.source_identity.get("pdf_sha256") not in record.source_hashes["source_pdf"]:
        raise PipelineBlocked(f"{record.record_id} baseline PDF identity is mixed provenance.")
    _require_hash(record.baseline_sha256, f"{record.record_id} baseline hash")
    if record.baseline_sha256 != _canonical_sha256(_baseline_payload(record), ensure_ascii=False):
        raise PipelineBlocked(f"{record.record_id} baseline hash is stale.")
    return number


def _route_payload(route: RouteDecision) -> dict[str, object]:
    return {
        "record_id": route.record_id,
        "decision": route.decision,
        "candidate": route.candidate,
        "baseline_sha256": route.baseline_sha256,
        "review_result_sha256": route.review_result_sha256,
        "reason_codes": list(route.reason_codes),
    }


def _validate_route(route: RouteDecision, baseline: RawBaselineRecord) -> None:
    if route.decision not in {"ACCEPT_PYTHON", "VISION_REQUIRED"}:
        raise PipelineBlocked(f"{route.record_id} route is not terminal.")
    _require_hash(route.review_result_sha256, f"{route.record_id} review result hash")
    _require_hash(route.route_sha256, f"{route.record_id} route hash")
    if route.baseline_sha256 != baseline.baseline_sha256:
        raise PipelineBlocked(f"{route.record_id} route is stale against its baseline.")
    if _json_value(route.candidate) != _json_value(baseline.candidate):
        raise PipelineBlocked(f"{route.record_id} route candidate has mixed provenance.")
    if route.route_sha256 != _canonical_sha256(_route_payload(route)):
        raise PipelineBlocked(f"{route.record_id} route hash is stale.")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordered_crops(evidence: RecordEvidence) -> tuple[SourceCrop, ...]:
    return (*evidence.question_crops, *evidence.answer_key_crops, *evidence.solution_crops)


def _source_dependency_payload(evidence: RecordEvidence) -> dict[str, object]:
    crops = _ordered_crops(evidence)
    dpis = {crop.source_dpi for crop in crops}
    if len(dpis) != 1:
        raise PipelineBlocked(
            f"ch01-q{evidence.question_number:04d} current source evidence must use one positive DPI."
        )
    dpi = next(iter(dpis))
    if not isinstance(dpi, int) or isinstance(dpi, bool) or dpi < 1:
        raise PipelineBlocked(
            f"ch01-q{evidence.question_number:04d} current source evidence has an invalid DPI."
        )
    crop_provenance = tuple(
        {
            "role": crop.role,
            "page_number": crop.page_number,
            "box": [crop.box.left, crop.box.top, crop.box.right, crop.box.bottom],
            "source_image_sha256": crop.source_image_sha256,
            "source_dpi": crop.source_dpi,
            "crop_sha256": crop.sha256,
            **({"context_id": crop.context_id} if crop.context_id else {}),
        }
        for crop in crops
    )
    return {
        "chapter": evidence.chapter,
        "question_number": evidence.question_number,
        "source_pdf_sha256": evidence.source_pdf_sha256,
        "dpi": dpi,
        "crop_provenance": crop_provenance,
        "source_status": evidence.source_status,
        "source_reasons": evidence.source_reasons,
        "requires_reviewed_rejection": evidence.requires_reviewed_rejection,
        "boundary_review": evidence.boundary_review,
    }


def _validate_evidence(
    evidence: RecordEvidence,
    baseline: RawBaselineRecord,
    number: int,
    pdf_hashes: dict[Path, str],
) -> None:
    if evidence.chapter != 1 or evidence.question_number != number:
        raise PipelineBlocked(f"{baseline.record_id} current source evidence is cross-chapter or misassociated.")
    _require_hash(evidence.source_pdf_sha256, f"{baseline.record_id} source PDF hash")
    if evidence.source_pdf_sha256 not in baseline.source_hashes["source_pdf"]:
        raise PipelineBlocked(f"{baseline.record_id} current source evidence has mixed provenance.")
    _require_hash(evidence.dependency_fingerprint, f"{baseline.record_id} source dependency fingerprint")
    if evidence.source_pdf is None:
        raise PipelineBlocked(f"{baseline.record_id} current source evidence is missing its PDF path.")
    source_pdf = Path(evidence.source_pdf).resolve()
    if not source_pdf.is_file():
        raise PipelineBlocked(f"{baseline.record_id} current source PDF is missing.")
    current_pdf_hash = pdf_hashes.get(source_pdf)
    if current_pdf_hash is None:
        current_pdf_hash = _sha256_path(source_pdf)
        pdf_hashes[source_pdf] = current_pdf_hash
    if current_pdf_hash != evidence.source_pdf_sha256:
        raise PipelineBlocked(f"{baseline.record_id} current source PDF bytes are stale.")
    if not isinstance(evidence.source_status, str) or not evidence.source_status:
        raise PipelineBlocked(f"{baseline.record_id} current source status is malformed.")
    if any(not isinstance(reason, str) or not reason.strip() for reason in evidence.source_reasons):
        raise PipelineBlocked(f"{baseline.record_id} current source reasons are malformed.")
    if not isinstance(evidence.requires_reviewed_rejection, bool) or not isinstance(evidence.boundary_review, Mapping):
        raise PipelineBlocked(f"{baseline.record_id} current source policy is malformed.")
    role_groups = (
        ("question", evidence.question_crops),
        ("answer_key", evidence.answer_key_crops),
        ("solution", evidence.solution_crops),
    )
    hashes: list[str] = []
    for role, crops in role_groups:
        for crop in crops:
            if not isinstance(crop, SourceCrop):
                raise TypeError("RecordEvidence crops must contain SourceCrop values.")
            if crop.role != role or crop.question_number != number:
                raise PipelineBlocked(f"{baseline.record_id} current source crop has mixed provenance.")
            if (
                not isinstance(crop.page_number, int)
                or isinstance(crop.page_number, bool)
                or crop.page_number < 1
                or not isinstance(crop.width, int)
                or isinstance(crop.width, bool)
                or crop.width < 1
                or not isinstance(crop.height, int)
                or isinstance(crop.height, bool)
                or crop.height < 1
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    for value in (crop.box.left, crop.box.top, crop.box.right, crop.box.bottom)
                )
                or crop.box.left >= crop.box.right
                or crop.box.top >= crop.box.bottom
                or not isinstance(crop.context_id, str)
            ):
                raise PipelineBlocked(f"{baseline.record_id} current source crop metadata is malformed.")
            _require_hash(crop.sha256, f"{baseline.record_id} {role} crop hash")
            _require_hash(crop.source_image_sha256, f"{baseline.record_id} {role} source image hash")
            if not crop.path.is_file() or _sha256_path(crop.path) != crop.sha256:
                raise PipelineBlocked(f"{baseline.record_id} current source evidence crop is missing or stale.")
            hashes.append(crop.sha256)
    if len(set(hashes)) != len(hashes):
        raise PipelineBlocked(f"{baseline.record_id} current source evidence contains duplicate crop hashes.")
    expected_dependency = dependency_fingerprint(_source_dependency_payload(evidence))
    if evidence.dependency_fingerprint != expected_dependency:
        raise PipelineBlocked(f"{baseline.record_id} current source dependency fingerprint is stale.")


def _vision_job_index(
    vision_jobs: Mapping[str, VisionFallbackJob] | None, expected_ids: tuple[str, ...]
) -> dict[str, VisionFallbackJob]:
    if vision_jobs is None:
        values: Mapping[str, VisionFallbackJob] = {}
    elif not isinstance(vision_jobs, Mapping):
        raise TypeError("vision_jobs must be a mapping keyed by record ID.")
    else:
        values = vision_jobs
    jobs: list[VisionFallbackJob] = []
    for key, job in values.items():
        if not isinstance(key, str) or not isinstance(job, VisionFallbackJob):
            raise TypeError("vision_jobs must map record IDs to VisionFallbackJob values.")
        if key != job.record_id:
            raise PipelineBlocked(f"Vision job mapping key is stale for {key}.")
        jobs.append(job)
    return _index_exact(jobs, VisionFallbackJob, "vision job", expected_ids, lambda item: item.record_id)


def _validate_vision_job_canonical(job: VisionFallbackJob) -> None:
    """Validate all job fields that do not require a prepared RecordEvidence value."""
    try:
        _validate_vision_job_object(job)
        for field, value in (
            ("route hash", job.route_sha256),
            ("baseline hash", job.baseline_sha256),
            ("source PDF hash", job.source_pdf_sha256),
            ("source dependency fingerprint", job.source_dependency_fingerprint),
            ("evidence hash", job.evidence_sha256),
            ("config hash", job.config_sha256),
            ("schema hash", job.schema_sha256),
            ("prompt hash", job.prompt_sha256),
            ("job hash", job.job_sha256),
        ):
            _require_hash(value, f"{job.record_id} vision job {field}")
        if not isinstance(job.requires_quarantine, bool):
            raise ValueError("requires_quarantine must be boolean")
        if any(not isinstance(reason, str) or not reason.strip() for reason in job.source_reasons):
            raise ValueError("source reasons must be non-empty strings")
        if job.source_reasons and not job.requires_quarantine:
            raise ValueError("source reasons require quarantine")
        expected_prompt = _vision_prompt(job.record_id, job.question_number, tuple(job.source_reasons))
        if job.prompt != expected_prompt:
            raise ValueError("prompt is not canonical for the job source reasons")
        if not job.sources or not job.source_evidence_sha256s:
            raise ValueError("source inventory is empty")
        for source in job.sources:
            if not isinstance(source, Mapping):
                raise ValueError("source entry is not a mapping")
            keys = set(source)
            if keys not in (_VISION_SOURCE_FIELDS, _VISION_SOURCE_FIELDS | {"context_id"}):
                raise ValueError("source entry has missing or extra metadata")
            for field in ("page_number", "width", "height", "source_dpi"):
                value = source[field]
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError(f"source {field} must be a positive integer")
            box = source["box"]
            if not isinstance(box, Mapping) or set(box) != {"left", "top", "right", "bottom"}:
                raise ValueError("source box metadata is malformed")
            coordinates = tuple(box[field] for field in ("left", "top", "right", "bottom"))
            if any(not isinstance(value, int) or isinstance(value, bool) for value in coordinates):
                raise ValueError("source box coordinates must be integers")
            if box["left"] >= box["right"] or box["top"] >= box["bottom"]:
                raise ValueError("source box coordinates are empty or inverted")
            if "context_id" in source and (
                not isinstance(source["context_id"], str) or not source["context_id"].strip()
            ):
                raise ValueError("source context_id must be a non-empty string")
        if not job.output_path.is_file():
            raise ValueError("canonical job artifact is missing")
        if job.output_path.read_bytes() != _vision_canonical_bytes(_vision_job_document(job)):
            raise ValueError("canonical job artifact is stale")
    except (AttributeError, KeyError, OSError, PipelineBlocked, RuntimeError, TypeError, ValueError) as error:
        raise PipelineBlocked(f"{job.record_id} authoritative vision job is invalid: {error}") from error


def _validate_vision_job(
    job: VisionFallbackJob,
    route: RouteDecision,
    baseline: RawBaselineRecord,
    evidence: RecordEvidence,
    result: VisionFallbackResult,
) -> None:
    _validate_vision_job_canonical(job)
    try:
        sources, role_hashes = _vision_source_payload(evidence, evidence.question_number)
        source_reasons = _vision_source_reasons(evidence, role_hashes)
    except (TypeError, ValueError, RuntimeError, OSError) as error:
        raise PipelineBlocked(f"{job.record_id} authoritative vision job is invalid: {error}") from error
    expected_quarantine = bool(
        evidence.requires_reviewed_rejection
        or evidence.source_status != "complete"
        or source_reasons
    )
    if (
        job.question_number != evidence.question_number
        or job.route_sha256 != route.route_sha256
        or job.baseline_sha256 != baseline.baseline_sha256
        or job.source_pdf_sha256 != evidence.source_pdf_sha256
        or job.source_dependency_fingerprint != evidence.dependency_fingerprint
        or job.evidence_sha256 != _vision_evidence_sha256(evidence)
        or _json_value(job.sources) != _json_value(sources)
        or tuple(job.source_evidence_sha256s) != tuple(crop.sha256 for crop in _ordered_crops(evidence))
        or _json_value(job.role_sha256s) != _json_value(role_hashes)
        or job.requires_quarantine != expected_quarantine
        or tuple(job.source_reasons) != tuple(source_reasons)
        or job.prompt != _vision_prompt(job.record_id, job.question_number, source_reasons)
    ):
        raise PipelineBlocked(f"{job.record_id} vision job is stale against current source evidence.")
    if result.job_sha256 != job.job_sha256:
        raise PipelineBlocked(f"{job.record_id} vision result is stale against its authoritative vision job.")
    if job.requires_quarantine and result.decision != "QUARANTINE":
        raise PipelineBlocked(f"{job.record_id} authoritative vision job requires quarantine.")


def _options(value: Any, record_id: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PipelineBlocked(f"{record_id} accepted record needs options A-D or A-E.")
    labels = tuple(sorted(value))
    if labels not in _OPTION_LABEL_SETS:
        raise PipelineBlocked(f"{record_id} accepted record needs exactly contiguous options A-D or A-E.")
    options = dict(value)
    if any(not isinstance(options[label], str) or not options[label].strip() for label in labels):
        raise PipelineBlocked(f"{record_id} accepted record has an empty or non-text option.")
    return options


def _accepted_semantics(
    record_id: str,
    question_text: Any,
    options_value: Any,
    correct_answer: Any,
    solution_steps_value: Any,
) -> tuple[str, dict[str, str], str, tuple[str, ...]]:
    if not isinstance(question_text, str) or not question_text.strip():
        raise PipelineBlocked(f"{record_id} accepted record needs non-empty question text.")
    options = _options(options_value, record_id)
    if not isinstance(correct_answer, str) or correct_answer not in options:
        raise PipelineBlocked(f"{record_id} accepted record answer must name an existing option.")
    if not isinstance(solution_steps_value, (list, tuple)) or not solution_steps_value:
        raise PipelineBlocked(f"{record_id} accepted record needs at least one solution step.")
    steps = tuple(solution_steps_value)
    if any(not isinstance(step, str) or not step.strip() for step in steps):
        raise PipelineBlocked(f"{record_id} accepted record has an empty or non-text solution step.")
    return question_text, options, correct_answer, steps


def _result_payload(result: VisionFallbackResult) -> dict[str, object]:
    return {
        "record_id": result.record_id,
        "decision": result.decision,
        "question_text": result.question_text,
        "options": dict(result.options),
        "correct_answer": result.correct_answer,
        "solution_steps": list(result.solution_steps),
        "representation": dict(result.representation),
        "media": dict(result.media),
        "source_evidence_sha256s": list(result.source_evidence_sha256s),
        "quarantine_reason": result.quarantine_reason,
        "reviewer": result.reviewer,
        "route_sha256": result.route_sha256,
        "job_sha256": result.job_sha256,
    }


def _validate_quarantine_shape(result: VisionFallbackResult) -> None:
    if (
        result.question_text != ""
        or dict(result.options) != {}
        or result.correct_answer != ""
        or result.solution_steps != ()
        or dict(result.representation) != {}
        or dict(result.media) != {}
    ):
        raise PipelineBlocked(f"{result.record_id} quarantine contains candidate content.")
    if not isinstance(result.quarantine_reason, str) or not result.quarantine_reason.strip():
        raise PipelineBlocked(f"{result.record_id} quarantine reason is missing.")


def _validate_result(
    result: VisionFallbackResult, route: RouteDecision, evidence: RecordEvidence
) -> None:
    if result.decision not in {"VISION_ACCEPTED", "QUARANTINE"}:
        raise PipelineBlocked(f"{result.record_id} vision result is not terminal.")
    if result.route_sha256 != route.route_sha256:
        raise PipelineBlocked(f"{result.record_id} vision result is stale against its route.")
    if not isinstance(result.reviewer, str) or not result.reviewer.strip():
        raise PipelineBlocked(f"{result.record_id} vision result reviewer is missing.")
    _require_hash(result.job_sha256, f"{result.record_id} vision job hash")
    _require_hash(result.result_sha256, f"{result.record_id} vision result hash")
    if result.result_sha256 != _canonical_sha256(_result_payload(result)):
        raise PipelineBlocked(f"{result.record_id} vision result hash is stale.")
    current_hashes = tuple(crop.sha256 for crop in _ordered_crops(evidence))
    if result.decision == "VISION_ACCEPTED":
        if result.source_evidence_sha256s != current_hashes:
            raise PipelineBlocked(f"{result.record_id} vision result does not match current source evidence.")
        _accepted_semantics(
            result.record_id,
            result.question_text,
            result.options,
            result.correct_answer,
            result.solution_steps,
        )
        if result.quarantine_reason != "":
            raise PipelineBlocked(f"{result.record_id} accepted vision result has a quarantine reason.")
    else:
        _validate_quarantine_shape(result)
        if (
            not result.source_evidence_sha256s
            or len(set(result.source_evidence_sha256s)) != len(result.source_evidence_sha256s)
            or any(
            item not in current_hashes for item in result.source_evidence_sha256s
            )
        ):
            raise PipelineBlocked(f"{result.record_id} quarantine does not match current source evidence.")


def _media_item(crop: SourceCrop, alt_text: str) -> dict[str, object]:
    return {
        "source_path": str(crop.path),
        "source_sha256": crop.sha256,
        "alt_text": alt_text,
        "page_number": crop.page_number,
        "box": {
            "left": crop.box.left,
            "top": crop.box.top,
            "right": crop.box.right,
            "bottom": crop.box.bottom,
        },
        "source_image_sha256": crop.source_image_sha256,
        "source_dpi": crop.source_dpi,
    }


def _vision_representation(
    result: VisionFallbackResult, evidence: RecordEvidence, options: Mapping[str, str]
) -> dict[str, object]:
    raw = result.representation
    labels = tuple(sorted(options))
    if not isinstance(raw, Mapping) or set(raw) != {"question", "options", "solution"}:
        raise PipelineBlocked(f"{result.record_id} vision representation is incomplete.")
    option_modes = raw.get("options")
    if not isinstance(option_modes, Mapping) or tuple(sorted(option_modes)) != labels:
        raise PipelineBlocked(f"{result.record_id} vision option representations are incomplete.")
    modes = {"question": raw.get("question"), "options": dict(option_modes), "solution": raw.get("solution")}
    if modes["question"] not in {"text", "image"} or modes["solution"] not in {"text", "image"} or any(
        mode not in {"text", "image"} for mode in modes["options"].values()
    ):
        raise PipelineBlocked(f"{result.record_id} vision representation contains an invalid mode.")
    media = result.media
    if not isinstance(media, Mapping) or set(media) != {"question", "options", "solution"}:
        raise PipelineBlocked(f"{result.record_id} vision media declarations are incomplete.")
    media_options = media.get("options")
    if not isinstance(media_options, Mapping) or tuple(sorted(media_options)) != labels:
        raise PipelineBlocked(f"{result.record_id} vision option media declarations are incomplete.")
    crop_by_hash = {crop.sha256: crop for crop in _ordered_crops(evidence)}

    def references(value: Any, mode: str, field: str) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise PipelineBlocked(f"{result.record_id} {field} media must be a hash list.")
        hashes = tuple(value)
        if (mode == "text" and hashes) or (mode == "image" and not hashes):
            raise PipelineBlocked(f"{result.record_id} {field} media disagrees with its representation.")
        if len(set(hashes)) != len(hashes) or any(item not in crop_by_hash for item in hashes):
            raise PipelineBlocked(f"{result.record_id} {field} media is stale against current source evidence.")
        return hashes

    question_hashes = references(media["question"], modes["question"], "question")
    option_hashes = {
        label: references(media_options[label], modes["options"][label], f"option {label}") for label in labels
    }
    solution_hashes = references(media["solution"], modes["solution"], "solution")
    candidate_media: dict[str, object] = {}
    if question_hashes:
        if len(question_hashes) != 1 or crop_by_hash[question_hashes[0]].role != "question":
            raise PipelineBlocked(f"{result.record_id} question image needs one current question crop.")
        candidate_media["question"] = _media_item(crop_by_hash[question_hashes[0]], result.question_text)
    rendered_options: dict[str, object] = {}
    for label, hashes in option_hashes.items():
        if hashes:
            if len(hashes) != 1 or crop_by_hash[hashes[0]].role != "question":
                raise PipelineBlocked(f"{result.record_id} option {label} image needs one current question crop.")
            rendered_options[label] = _media_item(crop_by_hash[hashes[0]], options[label])
    if rendered_options:
        candidate_media["options"] = rendered_options
    if solution_hashes:
        if any(crop_by_hash[item].role != "solution" for item in solution_hashes):
            raise PipelineBlocked(f"{result.record_id} solution images need current solution crops.")
        alt_text = " ".join(result.solution_steps)
        candidate_media["solution"] = [_media_item(crop_by_hash[item], alt_text) for item in solution_hashes]
    return {**modes, "media": candidate_media}


def _candidate(
    *,
    chapter: int,
    question_number: int,
    question_text: str,
    options: dict[str, str],
    correct_answer: str,
    answer_evidence_sha256: str,
    solution_steps: tuple[str, ...],
    representation: Mapping[str, object],
    source_fingerprint: str,
) -> CandidateRecord:
    payload = {
        "chapter": chapter,
        "question_number": question_number,
        "question_text": question_text,
        "options": options,
        "correct_answer": correct_answer,
        "answer_key_answer": correct_answer,
        "answer_key_crop_sha256": answer_evidence_sha256,
        "answer_key_job_fingerprint": source_fingerprint,
        "solution_steps": list(solution_steps),
        "representation": dict(representation),
        "source_fingerprint": source_fingerprint,
    }
    return CandidateRecord(**payload, sha256=dependency_fingerprint(payload))


def _python_candidate(
    baseline: RawBaselineRecord, route: RouteDecision, question_number: int
) -> CandidateRecord:
    failures = baseline.candidate.get("baseline_failures", ())
    if not isinstance(failures, (list, tuple)) or failures:
        raise PipelineBlocked(f"{baseline.record_id} cannot accept Python with a baseline failure.")
    question_text, options, correct_answer, steps = _accepted_semantics(
        baseline.record_id,
        baseline.candidate.get("question_text"),
        baseline.candidate.get("options"),
        baseline.candidate.get("correct_answer"),
        baseline.candidate.get("solution_steps"),
    )
    answer_hashes = tuple(baseline.source_hashes["answer"])
    answer_evidence = answer_hashes[0] if len(answer_hashes) == 1 else dependency_fingerprint({
        "answer_source_sha256s": list(answer_hashes)
    })
    source_fingerprint = dependency_fingerprint({
        "baseline_sha256": baseline.baseline_sha256,
        "review_result_sha256": route.review_result_sha256,
    })
    representation = {
        "question": "text",
        "options": {label: "text" for label in options},
        "solution": "text",
        "media": {},
    }
    return _candidate(
        chapter=1,
        question_number=question_number,
        question_text=question_text,
        options=options,
        correct_answer=correct_answer,
        answer_evidence_sha256=answer_evidence,
        solution_steps=steps,
        representation=representation,
        source_fingerprint=source_fingerprint,
    )


def _vision_candidate(
    result: VisionFallbackResult, evidence: RecordEvidence, question_number: int
) -> CandidateRecord:
    question_text, options, correct_answer, steps = _accepted_semantics(
        result.record_id,
        result.question_text,
        result.options,
        result.correct_answer,
        result.solution_steps,
    )
    if len(evidence.answer_key_crops) != 1:
        raise PipelineBlocked(f"{result.record_id} needs exactly one current answer-key evidence crop.")
    return _candidate(
        chapter=1,
        question_number=question_number,
        question_text=question_text,
        options=options,
        correct_answer=correct_answer,
        answer_evidence_sha256=evidence.answer_key_crops[0].sha256,
        solution_steps=steps,
        representation=_vision_representation(result, evidence, options),
        source_fingerprint=evidence.dependency_fingerprint,
    )


def merge_final_candidates(
    baselines: Iterable[RawBaselineRecord],
    routes: Iterable[RouteDecision],
    vision_results: Iterable[VisionFallbackResult],
    evidence: Iterable[RecordEvidence],
    *,
    vision_jobs: Mapping[str, VisionFallbackJob] | None = None,
    expected_record_ids: Iterable[str] | None = None,
) -> tuple[CandidateRecord, ...]:
    """Return complete immutable candidates, excluding terminal quarantines."""
    baseline_values = tuple(baselines)
    if any(not isinstance(record, RawBaselineRecord) for record in baseline_values):
        raise TypeError("baselines must contain RawBaselineRecord values.")
    expected_ids = _expected_record_ids(baseline_values, expected_record_ids)
    baseline_by_id = _index_exact(
        baseline_values, RawBaselineRecord, "baseline", expected_ids, lambda item: item.record_id
    )
    route_by_id = _index_exact(routes, RouteDecision, "route", expected_ids, lambda item: item.record_id)
    numbers: dict[str, int] = {}
    for record_id in expected_ids:
        baseline = baseline_by_id[record_id]
        number = _validate_baseline(baseline)
        numbers[record_id] = number
        _validate_route(route_by_id[record_id], baseline)

    vision_ids = tuple(sorted(record_id for record_id in expected_ids if route_by_id[record_id].decision == "VISION_REQUIRED"))
    evidence_by_id = _index_exact(
        evidence,
        RecordEvidence,
        "source evidence",
        vision_ids,
        lambda item: f"ch{item.chapter:02d}-q{item.question_number:04d}",
    )
    pdf_hashes: dict[Path, str] = {}
    for record_id in vision_ids:
        _validate_evidence(
            evidence_by_id[record_id], baseline_by_id[record_id], numbers[record_id], pdf_hashes
        )
    result_by_id = _index_exact(
        vision_results,
        VisionFallbackResult,
        "vision result",
        vision_ids,
        lambda item: item.record_id,
    )
    job_by_id = _vision_job_index(vision_jobs, vision_ids)

    candidates: list[CandidateRecord] = []
    for record_id in expected_ids:
        route = route_by_id[record_id]
        if route.decision == "ACCEPT_PYTHON":
            candidates.append(_python_candidate(baseline_by_id[record_id], route, numbers[record_id]))
            continue
        result = result_by_id[record_id]
        _validate_vision_job(
            job_by_id[record_id],
            route,
            baseline_by_id[record_id],
            evidence_by_id[record_id],
            result,
        )
        _validate_result(result, route, evidence_by_id[record_id])
        if result.decision == "VISION_ACCEPTED":
            candidates.append(_vision_candidate(result, evidence_by_id[record_id], numbers[record_id]))
    return tuple(candidates)
