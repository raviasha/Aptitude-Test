from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


@dataclass(frozen=True)
class RawBaselineRecord:
    """An immutable raw-Python candidate bound to its source associations."""

    record_id: str
    chapter: int
    source_hashes: dict[str, tuple[str, ...]]
    candidate: dict[str, object]
    baseline_sha256: str
    source_identity: dict[str, object]


@dataclass(frozen=True)
class AgentReviewJob:
    record_id: str
    baseline_sha256: str
    prompt_version: str
    prompt_sha256: str
    payload_sha256: str
    output_schema: str
    output_path: Path
    job_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", Path(self.output_path))


@dataclass(frozen=True)
class AgentReviewResult:
    record_id: str
    decision: str
    confidence: float
    checks: dict[str, str]
    reason_codes: tuple[str, ...]
    explanation: str
    reviewer: str
    baseline_sha256: str
    job_sha256: str
    result_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", _freeze_value(dict(self.checks)))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


@dataclass(frozen=True)
class RouteDecision:
    """The terminal deterministic route for one immutable Python baseline."""

    record_id: str
    decision: str
    candidate: dict[str, object]
    baseline_sha256: str
    review_result_sha256: str
    reason_codes: tuple[str, ...]
    route_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


@dataclass(frozen=True)
class VisionFallbackJob:
    """One full-record vision request bound only to current source evidence."""

    record_id: str
    question_number: int
    route_sha256: str
    baseline_sha256: str
    source_pdf_sha256: str
    source_dependency_fingerprint: str
    config_sha256: str
    schema_sha256: str
    prompt: str
    prompt_sha256: str
    sources: tuple[Mapping[str, object], ...]
    source_evidence_sha256s: tuple[str, ...]
    role_sha256s: Mapping[str, tuple[str, ...]]
    output_schema: str
    output_path: Path
    requires_quarantine: bool
    source_reasons: tuple[str, ...]
    job_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(_freeze_value(dict(source)) for source in self.sources))
        object.__setattr__(self, "source_evidence_sha256s", tuple(self.source_evidence_sha256s))
        object.__setattr__(self, "role_sha256s", _freeze_value(dict(self.role_sha256s)))
        object.__setattr__(self, "output_path", Path(self.output_path))
        object.__setattr__(self, "source_reasons", tuple(self.source_reasons))


@dataclass(frozen=True)
class VisionFallbackResult:
    """A terminal full-record vision result validated against one fallback job."""

    record_id: str
    decision: str
    question_text: str
    options: Mapping[str, str]
    correct_answer: str
    solution_steps: tuple[str, ...]
    representation: Mapping[str, object]
    media: Mapping[str, object]
    source_evidence_sha256s: tuple[str, ...]
    quarantine_reason: str
    reviewer: str
    route_sha256: str
    job_sha256: str
    result_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _freeze_value(dict(self.options)))
        object.__setattr__(self, "solution_steps", tuple(self.solution_steps))
        object.__setattr__(self, "representation", _freeze_value(dict(self.representation)))
        object.__setattr__(self, "media", _freeze_value(dict(self.media)))
        object.__setattr__(self, "source_evidence_sha256s", tuple(self.source_evidence_sha256s))
