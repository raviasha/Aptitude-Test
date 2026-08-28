from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RawBaselineRecord:
    """An immutable raw-Python candidate bound to its source associations."""

    record_id: str
    chapter: int
    source_hashes: dict[str, tuple[str, ...]]
    candidate: dict[str, object]
    baseline_sha256: str
    source_identity: dict[str, object]
