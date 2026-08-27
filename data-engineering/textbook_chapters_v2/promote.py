"""Guarded, auditable promotion of a fully approved candidate package."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import app

from .audit import AuditSummary
from .models import PipelineBlocked, PromotionReceipt
from .store import canonical_json


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_member(archive: zipfile.ZipFile, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(archive.read(name).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineBlocked(f"Candidate package lacks valid {name} promotion evidence.") from error
    if not isinstance(value, dict):
        raise PipelineBlocked(f"Candidate package {name} promotion evidence must be an object.")
    return value


def _validate_audit_binding(candidate: Path, audit: AuditSummary) -> None:
    if type(audit) is not AuditSummary:
        raise PipelineBlocked("Promotion requires an authoritative AuditSummary from AuditLedger.")
    ledger_path = Path(audit._ledger_path)
    if not ledger_path.is_file() or _sha256_path(ledger_path) != audit["audit_sha256"]:
        raise PipelineBlocked("Promotion audit summary is stale against the current ledger.")
    seal_path = candidate.with_suffix(".package.json")
    try:
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("Candidate package seal is missing or unreadable.") from error
    if (
        not isinstance(seal, dict)
        or seal.get("candidate_sha256") != _sha256_path(candidate)
        or seal.get("audit_sha256") != audit["audit_sha256"]
    ):
        raise PipelineBlocked("Candidate package seal is stale or does not match the authoritative audit.")
    try:
        with zipfile.ZipFile(candidate) as archive:
            embedded = _read_json_member(archive, "metadata/audit-summary.json")
            lineage = _read_json_member(archive, "metadata/lineage.json")
    except zipfile.BadZipFile as error:
        raise PipelineBlocked("Candidate package is not a valid ZIP archive.") from error
    if canonical_json(embedded) != canonical_json(dict(audit)):
        raise PipelineBlocked("Candidate package audit summary does not match the authoritative current audit.")
    records = lineage.get("records")
    if not isinstance(records, list):
        raise PipelineBlocked("Candidate package lineage records are missing.")
    actual: dict[int, str] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise PipelineBlocked("Candidate package lineage record is malformed.")
        key = raw.get("key")
        digest = raw.get("candidate_sha256")
        if not isinstance(key, str) or "-q" not in key or not isinstance(digest, str):
            raise PipelineBlocked("Candidate package lineage record is malformed.")
        try:
            number = int(key.rsplit("-q", 1)[1])
        except ValueError as error:
            raise PipelineBlocked("Candidate package lineage record is malformed.") from error
        if number in actual:
            raise PipelineBlocked("Candidate package lineage contains duplicate records.")
        actual[number] = digest
    approved = {
        int(raw["question_number"]): str(raw["candidate_sha256"])
        for raw in audit["approved_records"]
    }
    if actual != approved:
        raise PipelineBlocked("Candidate package lineage hash is not named by the authoritative audit summary.")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(canonical_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def promote_candidate(candidate_path: Path, published_path: Path, audit_summary: AuditSummary) -> PromotionReceipt:
    """Atomically replace a published ZIP only when its V2 evidence is current."""
    candidate = Path(candidate_path).resolve()
    published = Path(published_path).resolve()
    if not candidate.is_file() or candidate.suffix.lower() != ".zip":
        raise PipelineBlocked(f"Candidate ZIP is missing: {candidate}")
    if candidate == published:
        raise PipelineBlocked("Candidate and published paths must be different.")
    _validate_audit_binding(candidate, audit_summary)
    try:
        with candidate.open("rb") as source:
            app.parse_question_package(source)
    except Exception as error:
        if isinstance(error, PipelineBlocked):
            raise
        raise PipelineBlocked("Candidate package failed application parser validation.") from error

    candidate_hash = _sha256_path(candidate)
    prior_hash = _sha256_path(published) if published.is_file() else ""
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    filename_stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    published.parent.mkdir(parents=True, exist_ok=True)
    rollback = published.with_name(f"{published.stem}.rollback-{filename_stamp}{published.suffix}") if prior_hash else None
    receipt_path = published.with_suffix(".promotion.json")

    temporary_name: str | None = None
    try:
        if rollback is not None:
            shutil.copy2(published, rollback)
            if _sha256_path(rollback) != prior_hash:
                raise PipelineBlocked("Rollback artifact hash does not match the prior published package.")
        with tempfile.NamedTemporaryFile("wb", dir=published.parent, prefix=f".{published.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            with candidate.open("rb") as source:
                shutil.copyfileobj(source, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path = Path(temporary_name)
        if _sha256_path(temporary_path) != candidate_hash:
            raise PipelineBlocked("Temporary promotion copy does not match the candidate hash.")
        os.replace(temporary_path, published)
        temporary_name = None
        receipt_payload = {
            "source": str(candidate),
            "destination": str(published),
            "prior_sha256": prior_hash,
            "candidate_sha256": candidate_hash,
            "audit_sha256": audit_summary["audit_sha256"],
            "timestamp": timestamp,
            "rollback_path": str(rollback) if rollback is not None else "",
        }
        _atomic_json(receipt_path, receipt_payload)
    except BaseException:
        if rollback is not None and rollback.is_file():
            shutil.copy2(rollback, published)
        elif not prior_hash:
            published.unlink(missing_ok=True)
        raise
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

    return PromotionReceipt(
        source=candidate,
        destination=published,
        prior_sha256=prior_hash,
        candidate_sha256=candidate_hash,
        audit_sha256=str(audit_summary["audit_sha256"]),
        timestamp=timestamp,
    )


__all__ = ["promote_candidate"]
