"""Deterministic, non-promoting package gate for the Chapter 1 pilot."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CandidateRecord, PackageResult, PipelineBlocked
from textbook_chapters_v2.package import (
    _candidate_fingerprint,
    _question_entry,
)
from textbook_chapters_v2.store import canonical_json, dependency_fingerprint

from .agent_review import AGENT_REVIEW_PROMPT_VERSION
from .audit import PilotAuditSummary, _render_index, _validate_persisted


_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_HASHED_ASSET_PREFIX = "assets/"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class PublishedPackageGuard:
    """A byte-identity assertion for the read-only published package."""

    path: Path
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> "PublishedPackageGuard":
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise PipelineBlocked("The published Chapter 1 ZIP is missing.")
        return cls(resolved, _sha256_path(resolved))

    def verify(self) -> None:
        if not self.path.is_file() or _sha256_path(self.path) != self.sha256:
            raise PipelineBlocked("The published Chapter 1 ZIP changed during candidate packaging.")


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content, compresslevel=9)


def _canonical_jsonl(values: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json(value) + b"\n" for value in values)


def _record_id(candidate: CandidateRecord) -> str:
    return f"ch{candidate.chapter:02d}-q{candidate.question_number:04d}"


def _load_current_audit(audit: PilotAuditSummary) -> dict[str, Any]:
    if type(audit) is not PilotAuditSummary:
        raise PipelineBlocked("Pilot packaging requires an authoritative PilotAuditSummary.")
    try:
        payload = json.loads(audit.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PipelineBlocked("The authoritative pilot audit is missing or unreadable.") from error
    current = _validate_persisted(payload)
    if (
        audit.sha256 != current["audit_sha256"]
        or audit.dependency_fingerprint != current["dependency_fingerprint"]
        or canonical_json(list(audit.records)) != canonical_json(current["records"])
        or canonical_json(dict(audit.counts)) != canonical_json(current["counts"])
    ):
        raise PipelineBlocked("The supplied pilot audit summary is stale or forged.")
    return current


def _validate_release_gate(
    config: ChapterConfig,
    candidates: tuple[CandidateRecord, ...],
    audit: PilotAuditSummary,
) -> dict[str, Any]:
    payload = _load_current_audit(audit)
    expected_ids = [f"ch{config.chapter:02d}-q{number:04d}" for number in range(config.question_numbers[0], config.question_numbers[1] + 1)]
    if payload["chapter"] != 1 or payload["expected_record_ids"] != expected_ids:
        raise PipelineBlocked("Pilot audit inventory does not match the configured Chapter 1 range.")
    values = tuple(candidates)
    if not values:
        raise PipelineBlocked("Pilot package requires at least one accepted candidate.")
    if any(not isinstance(candidate, CandidateRecord) for candidate in values):
        raise TypeError("candidates must contain CandidateRecord values.")
    ids = [_record_id(candidate) for candidate in values]
    if len(set(ids)) != len(ids):
        raise PipelineBlocked("Pilot package refuses duplicate candidate entries.")
    if ids != sorted(ids):
        values = tuple(sorted(values, key=_record_id))
        ids = [_record_id(candidate) for candidate in values]
    if any(candidate.chapter != 1 or candidate.sha256 != _candidate_fingerprint(candidate) for candidate in values):
        raise PipelineBlocked("Pilot package candidate fingerprint or chapter is invalid.")
    records = payload["records"]
    if len(records) != len(expected_ids) or len({record.get("record_id") for record in records}) != len(records):
        raise PipelineBlocked("Pilot audit has missing or duplicate record entries.")
    by_id = {record["record_id"]: record for record in records}
    accepted_statuses = {"PYTHON_ACCEPTED", "VISION_ACCEPTED"}
    computed_counts = {
        "total_baselines": len(records),
        "python_accepts": sum(record["route"]["decision"] == "ACCEPT_PYTHON" for record in records),
        "vision_routes": sum(record["route"]["decision"] == "VISION_REQUIRED" for record in records),
        "vision_accepts": sum(
            isinstance(record.get("vision_result"), Mapping)
            and record["vision_result"].get("decision") == "VISION_ACCEPTED"
            for record in records
        ),
        "quarantined": sum(record["status"] == "QUARANTINED" for record in records),
        "included": sum(record["status"] in accepted_statuses for record in records),
        "pending_render": sum(record["status"] == "PENDING_RENDER" for record in records),
    }
    if payload["counts"] != computed_counts:
        raise PipelineBlocked("Pilot audit terminal counts are stale.")
    included = [record_id for record_id in expected_ids if by_id[record_id]["status"] in accepted_statuses]
    if ids != included:
        raise PipelineBlocked("Pilot package candidates are missing, extra, pending render, or quarantined against the audit.")
    if any(record["status"] not in accepted_statuses | {"QUARANTINED"} for record in records):
        raise PipelineBlocked("Pilot package requires every audit record to be terminal and rendered.")
    candidate_by_id = dict(zip(ids, values))
    manifests = []
    for record_id in ids:
        record = by_id[record_id]
        candidate = candidate_by_id[record_id]
        if record.get("final_candidate") != _json_value(candidate):
            raise PipelineBlocked(f"Pilot audit candidate {record_id} is stale.")
        manifest = record.get("render_manifest")
        if not isinstance(manifest, Mapping) or manifest.get("complete") is not True:
            raise PipelineBlocked(f"Pilot audit render is incomplete for {record_id}.")
        manifests.append(manifest)
    current_renders = _render_index(manifests, candidate_by_id)
    for record_id in ids:
        if current_renders[record_id] != by_id[record_id]["render_manifest"]:
            raise PipelineBlocked(f"Pilot audit render manifest is stale for {record_id}.")
    return payload


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {name: _json_value(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _referenced_assets(entries: Iterable[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for entry in entries:
        media = entry.get("display_media", {})
        if not isinstance(media, Mapping):
            raise PipelineBlocked("Package display media is malformed.")
        items: list[Any] = []
        if "question" in media:
            items.append(media["question"])
        options = media.get("options", {})
        if not isinstance(options, Mapping):
            raise PipelineBlocked("Package option display media is malformed.")
        items.extend(options.values())
        solution = media.get("solution", ())
        if not isinstance(solution, (tuple, list)):
            raise PipelineBlocked("Package solution display media is malformed.")
        items.extend(solution)
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {"asset", "alt_text", "sha256"}:
                raise PipelineBlocked("Package display-media entry is malformed.")
            asset = item["asset"]
            digest = item["sha256"]
            if not isinstance(asset, str) or asset != f"{_HASHED_ASSET_PREFIX}{digest}.png":
                raise PipelineBlocked("Package asset path is unsafe or not hash-addressed.")
            result.add(asset)
    return result


def _portable_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    records = []
    for record in payload["records"]:
        records.append({
            "record_id": record["record_id"],
            "status": record["status"],
            "record_sha256": record["record_sha256"],
            "candidate_sha256": None if record["final_candidate"] is None else record["final_candidate"]["sha256"],
            "agent_result_sha256": record["agent_result"]["result_sha256"],
            "route_sha256": record["route"]["route_sha256"],
            "vision_result_sha256": None if record["vision_result"] is None else record["vision_result"]["result_sha256"],
            "render_manifest_sha256": None if record["render_manifest"] is None else record["render_manifest"]["manifest_sha256"],
        })
    return {
        "schema_version": 1,
        "chapter": 1,
        "audit_sha256": payload["audit_sha256"],
        "audit_dependency_fingerprint": payload["dependency_fingerprint"],
        "counts": payload["counts"],
        "records": records,
    }


def _package_bindings(payload: Mapping[str, Any], assets: Mapping[str, bytes]) -> dict[str, Any]:
    included = [record for record in payload["records"] if record["status"] in {"PYTHON_ACCEPTED", "VISION_ACCEPTED"}]
    source_hashes = sorted({digest for record in payload["records"] for digest in record["baseline"]["source_hashes"]["source_pdf"]})
    extractor_hashes = sorted({digest for record in payload["records"] for digest in record["baseline"]["source_hashes"]["raw_extractor"]})
    prompts = {(record["prompt"]["version"], record["prompt"]["sha256"]) for record in payload["records"]}
    renderers = {record["render_manifest"]["renderer_fingerprint"] for record in included}
    applications = {record["render_manifest"]["application_fingerprint"] for record in included}
    browsers = {(record["render_manifest"]["browser_identity"], record["render_manifest"]["browser_fingerprint"]) for record in included}
    if len(source_hashes) != 1 or len(prompts) != 1 or len(renderers) != 1 or len(applications) != 1 or len(browsers) != 1:
        raise PipelineBlocked("Pilot package dependencies are inconsistent across audit records.")
    prompt_version, prompt_sha256 = next(iter(prompts))
    browser_identity, browser_fingerprint = next(iter(browsers))
    if prompt_version != AGENT_REVIEW_PROMPT_VERSION:
        raise PipelineBlocked("Pilot package prompt version is stale.")
    return {
        "source_pdf_sha256": source_hashes[0],
        "agent_prompt": {"version": prompt_version, "sha256": prompt_sha256},
        "extractor_sha256s": extractor_hashes,
        "audit_sha256": payload["audit_sha256"],
        "application_fingerprint": next(iter(applications)),
        "renderer_fingerprint": next(iter(renderers)),
        "browser": {"identity": browser_identity, "fingerprint": browser_fingerprint},
        "records": [
            {
                "record_id": record["record_id"],
                "candidate_sha256": record["final_candidate"]["sha256"],
                "render_manifest_sha256": record["render_manifest"]["manifest_sha256"],
            }
            for record in included
        ],
        "assets": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in sorted(assets.items())],
    }


def _validate_written_package(path: Path, manifest: Mapping[str, Any], count: int, assets: set[str]) -> None:
    try:
        import app
        with path.open("rb") as package:
            bank, questions, _, version = app.parse_question_package(package)
    except Exception as error:
        if isinstance(error, PipelineBlocked):
            raise
        raise PipelineBlocked("Pilot candidate failed application parser validation.") from error
    if bank != manifest["bank_name"] or version != 3 or len(questions) != count:
        raise PipelineBlocked("Pilot candidate failed application parser validation.")
    with zipfile.ZipFile(path) as archive:
        names = [member.filename for member in archive.infolist() if not member.is_dir()]
    required = {"manifest.json", "questions/ch01.jsonl", "metadata/agent-triage-audit.json", "metadata/lineage.json"}
    if len(names) != len(set(names)) or set(names) != required | assets:
        raise PipelineBlocked("Pilot candidate contains missing, duplicate, or unreferenced members.")


def build_pilot_candidate_package(
    config: ChapterConfig,
    candidates: Iterable[CandidateRecord],
    audit: PilotAuditSummary,
    output_path: Path,
) -> PackageResult:
    """Validate the pilot gate and publish one new candidate path exclusively."""
    if not isinstance(config, ChapterConfig) or config.chapter != 1:
        raise TypeError("config must be a Chapter 1 ChapterConfig value.")
    output = Path(output_path)
    if output.suffix.lower() != ".zip":
        raise ValueError("Pilot candidate output must end in .zip.")
    if output.exists():
        raise PipelineBlocked("Pilot packaging refuses to overwrite an existing candidate ZIP.")
    published_raw = config.extras.get("published_path")
    if not isinstance(published_raw, str) or not published_raw.strip():
        raise PipelineBlocked("Pilot config requires the published Chapter 1 ZIP path.")
    guard = PublishedPackageGuard.capture(Path(published_raw))
    guard.verify()
    supplied = tuple(candidates)
    if any(not isinstance(candidate, CandidateRecord) for candidate in supplied):
        raise TypeError("candidates must contain CandidateRecord values.")
    ordered = tuple(sorted(supplied, key=_record_id))
    audit_payload = _validate_release_gate(config, ordered, audit)
    assets: dict[str, bytes] = {}
    entries = [_question_entry(config, candidate, assets)[0] for candidate in ordered]
    referenced = _referenced_assets(entries)
    if set(assets) != referenced:
        raise PipelineBlocked("Pilot candidate has unreferenced or missing display assets.")
    for name, content in assets.items():
        digest = hashlib.sha256(content).hexdigest()
        if name != f"assets/{digest}.png":
            raise PipelineBlocked("Pilot candidate asset hash does not match its path.")
    bindings = _package_bindings(audit_payload, assets)
    configured_source = config.extras.get("source_pdf_sha256")
    if not isinstance(configured_source, str) or configured_source != bindings["source_pdf_sha256"]:
        raise PipelineBlocked("Pilot package source PDF hash does not match the authoritative audit.")
    source_path_raw = config.extras.get("source_pdf")
    source_path = Path(source_path_raw) if isinstance(source_path_raw, str) else Path()
    if not source_path_raw or not source_path.is_file() or _sha256_path(source_path) != configured_source:
        raise PipelineBlocked("Pilot package source PDF is missing or its current bytes changed.")
    manifest = {
        "format_version": 3,
        "artifact_kind": "agent-triage-manual-review-pilot",
        "manual_review_required": True,
        "bank_name": config.bank_name,
        "question_files": ["questions/ch01.jsonl"],
        "audit_file": "metadata/agent-triage-audit.json",
        "lineage_file": "metadata/lineage.json",
        **bindings,
    }
    lineage = {
        "schema_version": 1,
        "records": [
            {
                "record_id": _record_id(candidate),
                "question_number": candidate.question_number,
                "candidate_sha256": candidate.sha256,
                "source_fingerprint": candidate.source_fingerprint,
                "representation": {key: value for key, value in candidate.representation.items() if key != "media"},
                "display_assets": sorted(_referenced_assets((entry,))),
            }
            for candidate, entry in zip(ordered, entries)
        ],
    }
    members = {
        "manifest.json": canonical_json(manifest),
        "questions/ch01.jsonl": _canonical_jsonl(entries),
        "metadata/agent-triage-audit.json": canonical_json(_portable_audit(audit_payload)),
        "metadata/lineage.json": canonical_json(lineage),
        **assets,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
        temporary_path = Path(temporary_name)
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(members):
                _write_member(archive, name, members[name])
        _validate_written_package(temporary_path, manifest, len(entries), set(assets))
        guard.verify()
        try:
            os.link(temporary_path, output)
        except FileExistsError as error:
            raise PipelineBlocked("Pilot packaging refuses to overwrite an existing candidate ZIP.") from error
        try:
            guard.verify()
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        temporary_path.unlink()
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return PackageResult(
        path=output,
        sha256=_sha256_path(output),
        question_count=len(entries),
        rejected_count=audit_payload["counts"]["quarantined"],
        manifest=manifest,
    )


__all__ = ["PublishedPackageGuard", "build_pilot_candidate_package"]
