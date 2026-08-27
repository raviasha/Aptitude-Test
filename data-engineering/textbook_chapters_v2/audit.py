"""Atomic per-chapter audit gates and review-only rule feedback."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .config import ChapterConfig
from .models import (
    APPROVED_FOR_PUBLISH,
    REVIEWED_REJECTION,
    AuditRecord,
    FailureClassification,
    PipelineBlocked,
    RuleProposal,
    VerificationResult,
)
from .rules import POLICY_VERSION
from .store import canonical_json, dependency_fingerprint


AUDIT_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = {APPROVED_FOR_PUBLISH, REVIEWED_REJECTION}
_KNOWN_NONTERMINAL_STATUSES = {
    "pending_extraction",
    "candidate",
    "blocked",
    "pending_render",
    "pending_vision",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FIXTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REQUIRED_APPROVAL_VERDICTS = {"question", "answer_mapping", "solution", "readability", "clipping"}


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _approval_dependencies(record: AuditRecord) -> dict[str, Any]:
    return {
        "source_crop_hashes": sorted(record.source_crop_hashes),
        "candidate_sha256": record.candidate_sha256,
        "asset_hashes": sorted(record.asset_hashes),
        "policy_version": record.policy_version,
        "extractor_schema_version": record.extractor_schema_version,
        "verifier_schema_version": record.verifier_schema_version,
        "renderer_version": record.renderer_version,
        "application_asset_version": record.application_asset_version,
    }


def approval_dependency_fingerprint(record: AuditRecord) -> str:
    """Return the complete fingerprint required for an approval."""
    if not isinstance(record, AuditRecord):
        raise TypeError("record must be an AuditRecord value.")
    return dependency_fingerprint(_approval_dependencies(record))


def _record_payload(record: AuditRecord) -> dict[str, Any]:
    return {
        "chapter": record.chapter,
        "question_number": record.question_number,
        "status": record.status,
        "source_crop_hashes": list(record.source_crop_hashes),
        "candidate_sha256": record.candidate_sha256,
        "asset_hashes": list(record.asset_hashes),
        "policy_version": record.policy_version,
        "extractor_schema_version": record.extractor_schema_version,
        "verifier_schema_version": record.verifier_schema_version,
        "renderer_version": record.renderer_version,
        "application_asset_version": record.application_asset_version,
        "reviewer": record.reviewer,
        "rejection_reason": record.rejection_reason,
        "dependency_fingerprint": record.dependency_fingerprint,
        "findings": list(record.findings),
        "field_verdicts": dict(record.field_verdicts),
    }


def _record_from_payload(raw: Any) -> AuditRecord:
    if not isinstance(raw, dict):
        raise PipelineBlocked("Audit ledger record must be a JSON object.")
    allowed = {item.name for item in fields(AuditRecord)}
    if set(raw) - allowed:
        raise PipelineBlocked("Audit ledger record contains unknown fields.")
    try:
        return AuditRecord(**raw)
    except (TypeError, ValueError) as error:
        raise PipelineBlocked("Audit ledger record is malformed.") from error


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(canonical_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


class AuditSummary(Mapping[str, Any]):
    """Immutable, package-compatible result from the authoritative gate."""

    def __init__(
        self,
        *,
        chapter: int,
        total_records: int,
        approved_count: int,
        reviewed_rejections: tuple[Mapping[str, Any], ...],
        audit_sha256: str,
    ) -> None:
        normalized_rejections = tuple(MappingProxyType(dict(item)) for item in reviewed_rejections)
        self._data = MappingProxyType(
            {
                "chapter": chapter,
                "total_records": total_records,
                "approved_count": approved_count,
                "reviewed_rejection_count": len(normalized_rejections),
                "all_records_terminal": True,
                "all_records_approved_or_reviewed_rejection": True,
                "reviewed_rejections": normalized_rejections,
                "audit_sha256": audit_sha256,
            }
        )

    @property
    def chapter(self) -> int:
        return self._data["chapter"]

    @property
    def total_records(self) -> int:
        return self._data["total_records"]

    @property
    def approved_count(self) -> int:
        return self._data["approved_count"]

    @property
    def reviewed_rejection_count(self) -> int:
        return self._data["reviewed_rejection_count"]

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class AuditLedger:
    """One atomically persisted audit record collection for one chapter."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._records: dict[int, AuditRecord] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PipelineBlocked(f"Audit ledger is unreadable: {self.path}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != AUDIT_SCHEMA_VERSION:
            raise PipelineBlocked("Audit ledger schema version is missing or stale.")
        records = payload.get("records")
        if not isinstance(records, list):
            raise PipelineBlocked("Audit ledger records must be a JSON list.")
        loaded: dict[int, AuditRecord] = {}
        for raw in records:
            record = _record_from_payload(raw)
            if record.question_number in loaded:
                raise PipelineBlocked(f"Audit ledger contains duplicate question {record.question_number}.")
            loaded[record.question_number] = record
        declared_chapter = payload.get("chapter")
        chapters = {record.chapter for record in loaded.values()}
        if len(chapters) > 1 or (chapters and declared_chapter not in chapters):
            raise PipelineBlocked("Audit ledger contains inconsistent chapter records.")
        self._records = loaded

    def _persist(self) -> None:
        chapters = {record.chapter for record in self._records.values()}
        chapter = next(iter(chapters)) if len(chapters) == 1 else None
        _atomic_write(
            self.path,
            {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "chapter": chapter,
                "records": [_record_payload(self._records[number]) for number in sorted(self._records)],
            },
        )

    def record(self, question_number: int) -> AuditRecord:
        try:
            return self._records[question_number]
        except KeyError as error:
            raise KeyError(f"No audit record for question {question_number}.") from error

    @staticmethod
    def _invalidate_changed_approval(previous: AuditRecord, incoming: AuditRecord) -> AuditRecord:
        if previous.status not in _TERMINAL_STATUSES:
            return incoming
        previous_dependencies = _approval_dependencies(previous)
        incoming_dependencies = _approval_dependencies(incoming)
        changed = {key for key in previous_dependencies if previous_dependencies[key] != incoming_dependencies[key]}
        if not changed:
            return incoming

        extraction_dependencies = {"source_crop_hashes", "policy_version", "extractor_schema_version"}
        render_dependencies = {"candidate_sha256", "renderer_version", "application_asset_version"}
        if changed & extraction_dependencies:
            return replace(
                incoming,
                status="pending_extraction",
                candidate_sha256="",
                asset_hashes=(),
                reviewer="",
                rejection_reason="",
                dependency_fingerprint="",
                findings=(),
                field_verdicts={},
            )
        if changed & render_dependencies:
            return replace(
                incoming,
                status="pending_render",
                asset_hashes=(),
                reviewer="",
                rejection_reason="",
                dependency_fingerprint="",
                findings=(),
                field_verdicts={},
            )
        return replace(
            incoming,
            status="pending_vision",
            reviewer="",
            rejection_reason="",
            dependency_fingerprint="",
            findings=(),
            field_verdicts={},
        )

    def merge_record(self, record: AuditRecord) -> None:
        """Upsert one record and atomically replace the chapter ledger."""
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be an AuditRecord value.")
        if record.chapter <= 0 or record.question_number <= 0:
            raise ValueError("Audit records require positive chapter and question numbers.")
        if self._records and record.chapter not in {item.chapter for item in self._records.values()}:
            raise PipelineBlocked("One audit ledger cannot contain records from multiple chapters.")
        previous = self._records.get(record.question_number)
        merged = self._invalidate_changed_approval(previous, record) if previous is not None else record
        self._records[record.question_number] = merged
        self._persist()

    @staticmethod
    def _require_approved_evidence(record: AuditRecord) -> None:
        key = f"ch{record.chapter:02d}-q{record.question_number:04d}"
        if not _nonempty_string(record.reviewer):
            raise PipelineBlocked(f"{key} approved record is missing reviewer evidence.")
        if not record.source_crop_hashes or any(not _valid_hash(value) for value in record.source_crop_hashes):
            raise PipelineBlocked(f"{key} approved record is missing a valid source crop hash.")
        if not _valid_hash(record.candidate_sha256):
            raise PipelineBlocked(f"{key} approved record is missing a valid candidate hash.")
        if len(record.asset_hashes) < 2 or any(not _valid_hash(value) for value in record.asset_hashes):
            raise PipelineBlocked(f"{key} approved record needs screenshots for both rendering states.")
        if record.policy_version != POLICY_VERSION:
            raise PipelineBlocked(f"{key} approved record has a missing or stale policy version.")
        if record.extractor_schema_version <= 0:
            raise PipelineBlocked(f"{key} approved record is missing extractor schema version.")
        if record.verifier_schema_version <= 0:
            raise PipelineBlocked(f"{key} approved record is missing verifier schema version.")
        if not _nonempty_string(record.renderer_version):
            raise PipelineBlocked(f"{key} approved record is missing renderer version.")
        if not _nonempty_string(record.application_asset_version):
            raise PipelineBlocked(f"{key} approved record is missing application asset version.")
        verdicts = record.field_verdicts
        option_fields = {field for field in verdicts if field.startswith("options.") and len(field) > len("options.")}
        if (
            not _REQUIRED_APPROVAL_VERDICTS.issubset(verdicts)
            or not option_fields
            or any(value != "pass" for value in verdicts.values())
        ):
            raise PipelineBlocked(f"{key} approved record lacks complete passing field verdicts.")
        if record.findings:
            raise PipelineBlocked(f"{key} approved record still has quarantined findings.")
        if record.dependency_fingerprint != approval_dependency_fingerprint(record):
            raise PipelineBlocked(f"{key} approved record has a stale dependency fingerprint.")

    def validate_release_gate(self, config: ChapterConfig) -> AuditSummary:
        """Fail closed unless configured records appear exactly once and are terminal."""
        if not isinstance(config, ChapterConfig):
            raise TypeError("config must be a ChapterConfig value.")
        wrong_chapters = sorted(number for number, record in self._records.items() if record.chapter != config.chapter)
        if wrong_chapters:
            raise PipelineBlocked(f"Audit ledger chapter does not match config for questions {wrong_chapters}.")
        expected = set(range(config.question_numbers[0], config.question_numbers[1] + 1)) - set(config.intentional_exclusions)
        actual = set(self._records)
        missing = sorted(expected - actual)
        if missing:
            raise PipelineBlocked(f"Audit ledger is missing configured questions: {missing}.")
        extra = sorted(actual - expected)
        if extra:
            raise PipelineBlocked(f"Audit ledger contains excluded or unconfigured questions: {extra}.")

        approved = 0
        rejected: list[Mapping[str, Any]] = []
        for number in sorted(expected):
            record = self._records[number]
            if record.status in _KNOWN_NONTERMINAL_STATUSES:
                raise PipelineBlocked(f"Question {number} remains in forbidden status {record.status}.")
            if record.status == APPROVED_FOR_PUBLISH:
                self._require_approved_evidence(record)
                approved += 1
                continue
            if record.status == REVIEWED_REJECTION:
                if not _nonempty_string(record.reviewer) or not _nonempty_string(record.rejection_reason):
                    raise PipelineBlocked(f"Question {number} reviewed_rejection requires reviewer and rejection reason.")
                rejected.append(
                    {
                        "chapter": record.chapter,
                        "question_number": number,
                        "status": REVIEWED_REJECTION,
                        "reviewer": record.reviewer,
                        "rejection_reason": record.rejection_reason,
                    }
                )
                continue
            raise PipelineBlocked(f"Question {number} has unknown nonterminal status {record.status!r}.")

        total = len(expected)
        if approved + len(rejected) != total:
            raise PipelineBlocked("Audit terminal counts do not match the chapter configuration.")
        audit_hash = dependency_fingerprint(
            [_record_payload(self._records[number]) for number in sorted(expected)]
        )
        return AuditSummary(
            chapter=config.chapter,
            total_records=total,
            approved_count=approved,
            reviewed_rejections=tuple(rejected),
            audit_sha256=audit_hash,
        )


def classify_failure(result: VerificationResult) -> FailureClassification:
    """Classify literal verification failures without modifying policy code."""
    if not isinstance(result, VerificationResult):
        raise TypeError("result must be a VerificationResult value.")
    failed_fields = tuple(sorted(field for field, verdict in result.verdicts.items() if verdict == "fail"))
    if not failed_fields:
        raise ValueError("Verification result must contain at least one failed field.")
    missing_differences = [field for field in failed_fields if not str(result.differences.get(field, "")).strip()]
    if missing_differences:
        raise ValueError(f"Verification result lacks a concrete difference for failed fields {missing_differences}.")
    description = " | ".join(f"{field}: {result.differences[field]}" for field in failed_fields)
    normalized = description.casefold()

    if any(token in normalized for token in ("matrix", "multi-line", "multiline", "unique source layout", "one-off")):
        category = "unique_source_layout"
    elif any(field in {"readability", "clipping"} for field in failed_fields) or any(
        token in normalized for token in ("viewport", "rendered", "clipped", "overflow")
    ):
        category = "rendering"
    elif "answer_mapping" in failed_fields or any(token in normalized for token in ("answer key", "association", "belongs to")):
        category = "association"
    elif any(token in normalized for token in ("representation", "source crop", "wrong crop", "image mode", "media")):
        category = "representation"
    elif any(
        token in normalized
        for token in ("normalization", "superscript", "exponent", "fraction", "radical", "operator", "unicode", "whitespace")
    ):
        category = "normalization"
    else:
        category = "extraction"

    status = "record_specific_media" if category == "unique_source_layout" else "general_rule_candidate"
    return FailureClassification(
        category=category,
        status=status,
        description=description,
        evidence={
            "job_id": result.job_id,
            "job_fingerprint": result.job_fingerprint,
            "failed_fields": failed_fields,
            "differences": {field: result.differences[field] for field in failed_fields},
            "reviewer": result.reviewer,
        },
    )


def _hash_list(value: Any, name: str, *, minimum: int = 1) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        raise ValueError(f"Rule proposal requires complete {name} hash evidence.")
    normalized = [str(item) for item in value]
    if any(not _valid_hash(item) for item in normalized):
        raise ValueError(f"Rule proposal requires complete {name} hash evidence.")
    return sorted(normalized)


def create_rule_proposal(classification: FailureClassification, fixture: Mapping[str, Any]) -> RuleProposal:
    """Persist a review-only proposal; this never edits rules or policy versions."""
    if not isinstance(classification, FailureClassification):
        raise TypeError("classification must be a FailureClassification value.")
    if not isinstance(fixture, Mapping):
        raise TypeError("fixture must be a literal mapping.")
    fixture_id = fixture.get("id", fixture.get("fixture_id"))
    if not isinstance(fixture_id, str) or _SAFE_FIXTURE_ID_RE.fullmatch(fixture_id) is None:
        raise ValueError("Rule proposal fixture_id must be a safe lowercase identifier.")
    chapter = fixture.get("chapter")
    if isinstance(chapter, bool) or not isinstance(chapter, int) or chapter <= 0:
        raise ValueError("Rule proposal fixture requires a positive chapter.")
    corpus_fixture = (
        isinstance(fixture.get("candidate"), str)
        and bool(fixture["candidate"])
        and "expected_rule" in fixture
        and (fixture["expected_rule"] is None or isinstance(fixture["expected_rule"], str))
    )
    paired_fixture = (
        isinstance(fixture.get("input"), str)
        and bool(fixture["input"])
        and isinstance(fixture.get("expected"), str)
        and bool(fixture["expected"])
    )
    if not corpus_fixture and not paired_fixture:
        raise ValueError("Rule proposal requires literal candidate/rule or input/expected fixture values.")

    source_hashes = _hash_list(fixture.get("source_crop_hashes"), "source crop")
    candidate_hash = fixture.get("candidate_sha256")
    if not _valid_hash(candidate_hash):
        raise ValueError("Rule proposal requires complete candidate hash evidence.")
    render_hashes = _hash_list(fixture.get("render_hashes"), "render", minimum=2)

    work_root = Path(fixture.get("work_root", "work"))
    output = work_root / f"chapter-{chapter:03d}" / "rule-proposals" / f"{fixture_id}.json"
    proposed_fixture = {str(key): value for key, value in fixture.items() if key != "work_root"}
    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "pending_rule_review",
        "classification": {
            "category": classification.category,
            "status": classification.status,
            "description": classification.description,
            "evidence": dict(classification.evidence),
        },
        "required_fixture_id": fixture_id,
        "evidence": {
            "source_crop_hashes": source_hashes,
            "candidate_sha256": candidate_hash,
            "render_hashes": render_hashes,
        },
        "proposed_fixture": proposed_fixture,
        "resolution_requirements": {
            "tdd_code_change": True,
            "rules_file": "rules.py",
            "fixture_corpus": "fixtures/notation-regressions.json",
            "policy_version_increment": True,
        },
    }
    _atomic_write(output, payload)
    return RuleProposal(
        status="pending_rule_review",
        classification=classification,
        required_fixture_id=fixture_id,
        proposed_fixture=proposed_fixture,
        path=output,
    )


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditLedger",
    "AuditSummary",
    "approval_dependency_fingerprint",
    "classify_failure",
    "create_rule_proposal",
]
