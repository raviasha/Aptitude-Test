"""Immutable values passed between V2 pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


PIPELINE_VERSION = 1

pending_extraction = "pending_extraction"
candidate = "candidate"
blocked = "blocked"
pending_render = "pending_render"
pending_vision = "pending_vision"
approved_for_publish = "approved_for_publish"
reviewed_rejection = "reviewed_rejection"

PENDING_EXTRACTION = pending_extraction
CANDIDATE = candidate
BLOCKED = blocked
PENDING_RENDER = pending_render
PENDING_VISION = pending_vision
APPROVED_FOR_PUBLISH = approved_for_publish
REVIEWED_REJECTION = reviewed_rejection


class PipelineBlocked(RuntimeError):
    """Raised when a pipeline gate prevents unsafe progression."""


def freeze_value(value: Any) -> Any:
    """Recursively detach collection values before crossing a stage boundary."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_value(item) for item in value)
    return value


def frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return freeze_value(dict(value or {}))


@dataclass(frozen=True)
class CropBox:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class SourceImage:
    path: Path
    page_number: int
    dpi: int
    sha256: str


@dataclass(frozen=True)
class SourceCrop:
    role: str
    question_number: int
    page_number: int
    box: CropBox
    path: Path
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class RecordEvidence:
    chapter: int = 0
    question_number: int = 0
    source_pdf: Path | None = None
    source_pdf_sha256: str = ""
    question_crops: tuple[SourceCrop, ...] = ()
    answer_key_crops: tuple[SourceCrop, ...] = ()
    solution_crops: tuple[SourceCrop, ...] = ()
    dependency_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_crops", tuple(self.question_crops))
        object.__setattr__(self, "answer_key_crops", tuple(self.answer_key_crops))
        object.__setattr__(self, "solution_crops", tuple(self.solution_crops))


@dataclass(frozen=True)
class VisionJob:
    job_id: str = ""
    stage: str = ""
    prompt: str = ""
    sources: tuple[Mapping[str, Any], ...] = ()
    output_schema: str = ""
    output_path: Path | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(frozen_mapping(item) for item in self.sources))


@dataclass(frozen=True)
class CandidateRecord:
    chapter: int = 0
    question_number: int = 0
    question_text: str = ""
    options: Mapping[str, str] = field(default_factory=frozen_mapping)
    correct_answer: str = ""
    solution_steps: tuple[str, ...] = ()
    representation: Mapping[str, Any] = field(default_factory=frozen_mapping)
    source_fingerprint: str = ""
    sha256: str = ""
    status: str = candidate

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", frozen_mapping(self.options))
        object.__setattr__(self, "solution_steps", tuple(self.solution_steps))
        object.__setattr__(self, "representation", frozen_mapping(self.representation))


@dataclass(frozen=True)
class RenderArtifacts:
    question_screenshots: Mapping[str, Path] = field(default_factory=frozen_mapping)
    solution_screenshots: Mapping[str, Path] = field(default_factory=frozen_mapping)
    screenshot_hashes: Mapping[str, str] = field(default_factory=frozen_mapping)
    findings: tuple[str, ...] = ()
    renderer_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "question_screenshots", frozen_mapping(self.question_screenshots))
        object.__setattr__(self, "solution_screenshots", frozen_mapping(self.solution_screenshots))
        object.__setattr__(self, "screenshot_hashes", frozen_mapping(self.screenshot_hashes))
        object.__setattr__(self, "findings", tuple(self.findings))


@dataclass(frozen=True)
class VerificationResult:
    job_id: str = ""
    job_fingerprint: str = ""
    verdicts: Mapping[str, str] = field(default_factory=frozen_mapping)
    differences: Mapping[str, str] = field(default_factory=frozen_mapping)
    reviewer: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdicts", frozen_mapping(self.verdicts))
        object.__setattr__(self, "differences", frozen_mapping(self.differences))


@dataclass(frozen=True)
class AuditRecord:
    chapter: int = 0
    question_number: int = 0
    status: str = pending_extraction
    source_crop_hashes: tuple[str, ...] = ()
    candidate_sha256: str = ""
    asset_hashes: tuple[str, ...] = ()
    policy_version: int = 0
    extractor_schema_version: int = 0
    verifier_schema_version: int = 0
    renderer_version: str = ""
    application_asset_version: str = ""
    reviewer: str = ""
    rejection_reason: str = ""
    dependency_fingerprint: str = ""
    findings: tuple[str, ...] = ()
    field_verdicts: Mapping[str, str] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_crop_hashes", tuple(self.source_crop_hashes))
        object.__setattr__(self, "asset_hashes", tuple(self.asset_hashes))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "field_verdicts", frozen_mapping(self.field_verdicts))


@dataclass(frozen=True)
class PackageResult:
    path: Path | None = None
    sha256: str = ""
    question_count: int = 0
    rejected_count: int = 0
    manifest: Mapping[str, Any] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", frozen_mapping(self.manifest))


@dataclass(frozen=True)
class PromotionReceipt:
    source: Path | None = None
    destination: Path | None = None
    prior_sha256: str = ""
    candidate_sha256: str = ""
    audit_sha256: str = ""
    timestamp: str = ""


@dataclass(frozen=True)
class FailureClassification:
    category: str = ""
    status: str = ""
    description: str = ""
    evidence: Mapping[str, Any] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", frozen_mapping(self.evidence))


@dataclass(frozen=True)
class RuleProposal:
    status: str = "pending_rule_review"
    classification: FailureClassification | None = None
    required_fixture_id: str = ""
    proposed_fixture: Mapping[str, Any] = field(default_factory=frozen_mapping)
    path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposed_fixture", frozen_mapping(self.proposed_fixture))


@dataclass(frozen=True)
class ArtifactRef:
    path: Path
    stage: str
    key: str
    dependency_fingerprint: str
    payload_sha256: str
