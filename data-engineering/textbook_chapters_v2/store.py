"""Content-addressed, root-contained JSON artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import ArtifactRef


SCHEMA_VERSION = 1


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Canonical JSON object keys must be strings.")
            normalized[key] = _json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json(item))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Canonical JSON does not support {type(value).__name__} values.")


def canonical_json(value: Any) -> bytes:
    """Return UTF-8 JSON with stable ordering and no optional whitespace."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def dependency_fingerprint(*values: Any) -> str:
    """Hash the ordered dependency tuple without ambiguity between values."""
    return hashlib.sha256(canonical_json(values)).hexdigest()


def _safe_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"{name} must be a non-empty filename component.")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.name != value or "/" in value or "\\" in value:
        raise ValueError(f"{name} must not be an absolute or separated path.")
    return value


class ArtifactStore:
    """Stores stage artifacts under one caller-owned work root."""

    def __init__(self, work_root: Path) -> None:
        self.root = Path(work_root).resolve()

    def _path(self, stage: str, key: str) -> Path:
        safe_stage = _safe_component(stage, "stage")
        safe_key = _safe_component(key, "key")
        path = self.root / safe_stage / f"{safe_key}.json"
        try:
            path.resolve().relative_to(self.root)
        except ValueError as error:
            raise ValueError("artifact path escapes the configured work root.") from error
        return path

    def write_json(
        self,
        stage: str,
        key: str,
        payload: Mapping[str, Any],
        dependencies: Any,
    ) -> ArtifactRef:
        path = self._path(stage, key)
        if not isinstance(payload, Mapping):
            raise TypeError("Artifact payload must be a JSON object.")
        normalized_payload = json.loads(canonical_json(payload).decode("utf-8"))
        dependency_hash = dependency_fingerprint(dependencies)
        payload_hash = dependency_fingerprint(normalized_payload)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "stage": stage,
            "key": key,
            "dependency_fingerprint": dependency_hash,
            "payload_sha256": payload_hash,
            "payload": normalized_payload,
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{key}.", suffix=".tmp", delete=False
            ) as temporary_file:
                temporary_name = temporary_file.name
                temporary_file.write(canonical_json(envelope))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
            raise
        return ArtifactRef(path, stage, key, dependency_hash, payload_hash)

    def read_if_current(self, stage: str, key: str, dependencies: Any) -> dict[str, Any] | None:
        path = self._path(stage, key)
        if not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            return None
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("stage") != stage
            or envelope.get("key") != key
            or envelope.get("dependency_fingerprint") != dependency_fingerprint(dependencies)
            or envelope.get("payload_sha256") != dependency_fingerprint(payload)
        ):
            return None
        return json.loads(canonical_json(payload).decode("utf-8"))
