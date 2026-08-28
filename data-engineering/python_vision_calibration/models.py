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
