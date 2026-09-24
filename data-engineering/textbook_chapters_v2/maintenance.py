"""Selective rerun and accepted-baseline maintenance rules."""

from __future__ import annotations

import re
from typing import Any, Mapping


KNOWN_FAILURE_STAGES = frozenset({
    "source_extraction",
    "math_representation",
    "asset_association",
    "packaging",
    "import_update",
    "frontend_rendering",
})
_FAILURE_STAGES = {
    "source_extraction": ("extract", "build", "render", "verify", "package"),
    "math_representation": ("build", "render", "verify", "package"),
    "asset_association": ("prepare", "build", "render", "verify", "package"),
    "packaging": ("package",),
    "import_update": ("render", "verify"),
    "frontend_rendering": ("render", "verify"),
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _records(registry: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = registry.get("records")
    if isinstance(nested, Mapping):
        return nested
    return registry


def affected_records(registry: Mapping[str, Any], changed_tags: set[str]) -> set[str]:
    """Return every source key depending on any changed dependency tag."""
    if not isinstance(registry, Mapping):
        raise TypeError("registry must be a mapping")
    if not isinstance(changed_tags, set) or any(not isinstance(tag, str) or not tag for tag in changed_tags):
        raise TypeError("changed_tags must be a set of non-empty strings")
    result: set[str] = set()
    for source_key, record in _records(registry).items():
        if not isinstance(source_key, str) or not isinstance(record, Mapping):
            raise ValueError("registry records must map source keys to objects")
        tags = record.get("dependency_tags", ())
        if not isinstance(tags, (list, tuple, set)) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f"registry dependency tags are malformed for {source_key}")
        if changed_tags.intersection(tags):
            result.add(source_key)
    return result


def affected_records_for_registry_change(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    changed_tags: set[str],
) -> set[str]:
    """Select both removed and newly associated consumers of changed tags."""
    return affected_records(previous, changed_tags) | affected_records(current, changed_tags)


def stages_for_failure(stage: str) -> tuple[str, ...]:
    """Return the earliest required stage and every dependent downstream stage."""
    try:
        return _FAILURE_STAGES[stage]
    except KeyError as error:
        raise ValueError(f"Unknown maintenance failure stage: {stage}") from error


def baseline_acceptance(
    registry: Mapping[str, Any],
    source_key: str,
    *,
    archive_sha256: str,
    record_sha256: str,
) -> dict[str, str]:
    """Create explicit baseline provenance only for exact inventoried bytes."""
    if _HASH_RE.fullmatch(archive_sha256) is None:
        raise ValueError("baseline archive hash must be a lowercase SHA-256 value")
    if _HASH_RE.fullmatch(record_sha256) is None:
        raise ValueError("baseline record hash must be a lowercase SHA-256 value")
    records = _records(registry)
    record = records.get(source_key)
    if not isinstance(record, Mapping):
        raise ValueError(f"Source key is absent from the accepted baseline: {source_key}")
    if record.get("record_sha256") != record_sha256:
        raise ValueError(f"baseline record hash does not match the inventory for {source_key}")
    archive_name = record.get("archive")
    banks = registry.get("banks")
    if not isinstance(banks, list):
        raise ValueError("baseline inventory is missing bank evidence")
    bank = next((item for item in banks if isinstance(item, Mapping) and item.get("archive") == archive_name), None)
    if bank is None or bank.get("archive_sha256") != archive_sha256:
        raise ValueError(f"baseline archive hash does not match the inventory for {source_key}")
    return {
        "provenance": "baseline_accepted",
        "baseline_archive_sha256": archive_sha256,
        "baseline_record_sha256": record_sha256,
        "candidate_sha256": record_sha256,
    }


__all__ = [
    "KNOWN_FAILURE_STAGES",
    "affected_records",
    "affected_records_for_registry_change",
    "baseline_acceptance",
    "stages_for_failure",
]
