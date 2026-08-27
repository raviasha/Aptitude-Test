"""Resumable command-line orchestration for the vision-verified V2 pipeline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image

from .audit import AuditLedger, AuditSummary, approval_dependency_fingerprint
from .candidates import assemble_candidate
from .config import ChapterConfig
from .models import (
    APPROVED_FOR_PUBLISH,
    BLOCKED,
    PENDING_EXTRACTION,
    PENDING_RENDER,
    PENDING_VISION,
    REVIEWED_REJECTION,
    AuditRecord,
    CandidateRecord,
    CropBox,
    PipelineBlocked,
    RecordEvidence,
    RenderArtifacts,
    SourceCrop,
    VisionJob,
)
from .package import build_candidate_package
from .promote import promote_candidate
from .render import RenderArtifacts as BrowserRenderArtifacts, _launch_browser, render_candidate
from .rules import POLICY_VERSION, validate_record
from .source import prepare_source_evidence
from .store import canonical_json, dependency_fingerprint
from .vision import (
    create_extraction_job,
    create_verification_job,
    extraction_job_fingerprint,
    ingest_extraction_result,
    ingest_verification_result,
)


SUCCESS_EXIT = 0
PENDING_VISION_EXIT = 20
BLOCKED_EXIT = 21
ERROR_EXIT = 22
EXTRACTOR_SCHEMA_VERSION = 1
VERIFIER_SCHEMA_VERSION = 1
DEFAULT_VIEWPORTS = ((1024, 768), (1600, 900))
APPLICATION_ROOT = Path(__file__).resolve().parents[2]
RENDER_CONTRACT_VERSION = "ksat-v2-real-frontend-contract-2"
_RENDERER_CONTRACT_FILES = (
    "data-engineering/textbook_chapters_v2/cli.py",
    "data-engineering/textbook_chapters_v2/render.py",
    "data-engineering/textbook_chapters_v2/models.py",
    "data-engineering/textbook_chapters_v2/candidates.py",
    "data-engineering/textbook_chapters_v2/package.py",
    "data-engineering/textbook_chapters_v2/vision.py",
    "data-engineering/textbook_chapters_v2/rules.py",
    "data-engineering/textbook_chapters_v2/schemas/extraction-result.schema.json",
    "data-engineering/textbook_chapters_v2/schemas/verification-result.schema.json",
)


def _atomic_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
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


def _atomic_jsonl(path: Path, payloads: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            for payload in payloads:
                temporary.write(canonical_json(payload) + b"\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_png(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            temporary_name = temporary.name
            image.save(temporary, format="PNG", optimize=False, compress_level=9)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _path_value(config: ChapterConfig, key: str, default: str | None = None) -> Path:
    raw = config.extras.get(key, default)
    if not isinstance(raw, str) or not raw.strip():
        raise PipelineBlocked(f"Chapter config requires a non-empty {key} path.")
    return Path(raw).resolve()


def _work_root(config: ChapterConfig) -> Path:
    return _path_value(config, "work_root", "tmp/textbook-v2")


def _chapter_root(config: ChapterConfig) -> Path:
    return _work_root(config) / f"chapter-{config.chapter:03d}"


def _candidate_path(config: ChapterConfig) -> Path:
    return _path_value(config, "candidate_path", f"tmp/textbook-v2/chapter-{config.chapter:03d}/candidate.zip")


def _published_path(config: ChapterConfig) -> Path:
    return _path_value(config, "published_path")


def _current_browser_identity() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise PipelineBlocked("Playwright is required to fingerprint the selected validation browser runtime.") from error
    try:
        with sync_playwright() as playwright:
            browser, selected = _launch_browser(playwright)
            try:
                version = " ".join(str(browser.version).split()).casefold()
                engine = "microsoft-edge" if selected.startswith("Microsoft Edge") else "playwright-chromium"
            finally:
                browser.close()
    except Exception as error:
        if isinstance(error, PipelineBlocked):
            raise
        raise PipelineBlocked(f"Cannot fingerprint the selected validation browser runtime: {error}") from error
    if not version:
        raise PipelineBlocked("Selected validation browser did not report a version identity.")
    return f"{engine}:{version}"


def _application_renderer_manifest(config: ChapterConfig) -> dict[str, Any]:
    root = APPLICATION_ROOT
    static_root = root / "static"
    if not (root / "app.py").is_file() or not static_root.is_dir():
        raise PipelineBlocked(f"Application render contract is missing beneath {root}.")
    startup_paths = (root / "app.py", root / "question_media.py", root / "chapter_repairs.py")
    missing_startup = [str(path) for path in startup_paths if not path.is_file()]
    if missing_startup:
        raise PipelineBlocked(f"Validation server startup/import files are missing: {missing_startup}.")
    application_paths = (*startup_paths, *sorted(path for path in static_root.rglob("*") if path.is_file()))
    renderer_paths = tuple(root / relative for relative in _RENDERER_CONTRACT_FILES)
    missing = [str(path) for path in renderer_paths if not path.is_file()]
    if missing:
        raise PipelineBlocked(f"Renderer contract files are missing: {missing}.")

    def entries(paths: Iterable[Path]) -> list[dict[str, str]]:
        return [
            {"path": path.relative_to(root).as_posix(), "sha256": _sha256_path(path)}
            for path in paths
        ]

    application_assets = entries(application_paths)
    renderer_contract = entries(renderer_paths)
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        playwright_version = "unavailable"
    runtime_policy = {
        "python_abi": f"{sys.version_info.major}.{sys.version_info.minor}",
        "playwright_version": playwright_version,
        "browser_selection": ["system-edge", "playwright-chromium"],
        "selected_browser_identity": _current_browser_identity(),
        "headless": True,
        "validation_viewports": [list(viewport) for viewport in _viewports(config)],
        "package_format_version": 3,
        "render_contract_version": RENDER_CONTRACT_VERSION,
    }
    application_fingerprint = dependency_fingerprint("ksat-application-assets", application_assets)
    renderer_fingerprint = dependency_fingerprint("ksat-renderer", renderer_contract, runtime_policy)
    return {
        "schema_version": 1,
        "application_assets": application_assets,
        "renderer_contract": renderer_contract,
        "runtime_policy": runtime_policy,
        "application_fingerprint": application_fingerprint,
        "renderer_fingerprint": renderer_fingerprint,
        "manifest_fingerprint": dependency_fingerprint(application_fingerprint, renderer_fingerprint),
    }


def _evidence_input_fingerprint(config: ChapterConfig) -> str:
    source = _path_value(config, "source_pdf")
    if not source.is_file():
        raise PipelineBlocked(f"Source PDF is missing: {source}")
    return dependency_fingerprint("source-evidence-input", config, _sha256_path(source))


def _evidence_is_current(config: ChapterConfig) -> bool:
    state = _chapter_root(config) / "state"
    cache_path = state / "evidence-cache.json"
    evidence_path = state / "evidence.json"
    field_media_path = state / "field-media.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            not isinstance(cache, dict)
            or cache.get("input_fingerprint") != _evidence_input_fingerprint(config)
            or cache.get("evidence_sha256") != _sha256_path(evidence_path)
            or cache.get("field_media_sha256") != _sha256_path(field_media_path)
        ):
            return False
        records = _evidence(config)
    except (OSError, ValueError, TypeError, PipelineBlocked):
        return False
    for evidence in records:
        if evidence.source_pdf is None or not evidence.source_pdf.is_file() or _sha256_path(evidence.source_pdf) != evidence.source_pdf_sha256:
            return False
        for crop in evidence.question_crops + evidence.answer_key_crops + evidence.solution_crops:
            if not crop.path.is_file() or _sha256_path(crop.path) != crop.sha256:
                return False
    return True


def _crop_payload(crop: SourceCrop) -> dict[str, Any]:
    return {
        "role": crop.role,
        "question_number": crop.question_number,
        "page_number": crop.page_number,
        "box": asdict(crop.box),
        "path": str(crop.path),
        "width": crop.width,
        "height": crop.height,
        "sha256": crop.sha256,
        "source_image_sha256": crop.source_image_sha256,
        "source_dpi": crop.source_dpi,
    }


def _crop_from_payload(raw: Mapping[str, Any]) -> SourceCrop:
    return SourceCrop(
        role=str(raw["role"]), question_number=int(raw["question_number"]), page_number=int(raw["page_number"]),
        box=CropBox(**raw["box"]), path=Path(raw["path"]), width=int(raw["width"]), height=int(raw["height"]),
        sha256=str(raw["sha256"]), source_image_sha256=str(raw["source_image_sha256"]), source_dpi=int(raw["source_dpi"]),
    )


def _field_specs(config: ChapterConfig, question_number: int) -> Mapping[str, Any]:
    configured = config.extras.get("field_media", {})
    if not isinstance(configured, Mapping):
        raise PipelineBlocked("field_media must be a mapping keyed by question number.")
    raw = configured.get(str(question_number), configured.get(question_number, {}))
    if not isinstance(raw, Mapping):
        raise PipelineBlocked(f"field_media for question {question_number} must be an object.")
    if set(raw) - {"question", "options", "solution"}:
        raise PipelineBlocked(f"field_media for question {question_number} contains an unknown field.")
    return raw


def _selected_segment(
    evidence: RecordEvidence,
    raw: Any,
    expected_role: str,
    field: str,
    output_dir: Path,
    ordinal: int,
) -> SourceCrop:
    if not isinstance(raw, Mapping) or raw.get("role") != expected_role:
        raise PipelineBlocked(f"Explicit field evidence for {field} must name role {expected_role}.")
    candidates = evidence.question_crops if expected_role == "question" else evidence.solution_crops
    index = raw.get("source_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(candidates):
        raise PipelineBlocked(f"Explicit field evidence for {field} names an invalid source_index.")
    source = candidates[index]
    if not source.path.is_file() or _sha256_path(source.path) != source.sha256:
        raise PipelineBlocked(f"Explicit field evidence for {field} has stale source bytes.")
    box_raw = raw.get("box")
    if box_raw is None:
        return source
    if not isinstance(box_raw, (list, tuple)) or len(box_raw) != 4 or any(
        isinstance(value, bool) or not isinstance(value, int) for value in box_raw
    ):
        raise PipelineBlocked(f"Explicit field evidence for {field} needs a four-integer relative box.")
    left, top, right, bottom = box_raw
    with Image.open(source.path) as image:
        image.load()
        if left < 0 or top < 0 or right > image.width or bottom > image.height or left >= right or top >= bottom:
            raise PipelineBlocked(f"Explicit field evidence for {field} has an out-of-bounds box.")
        cropped = image.crop((left, top, right, bottom))
    output = output_dir / f"{field.replace('.', '-')}-{ordinal:02d}.png"
    _write_png(output, cropped)
    return SourceCrop(
        role=expected_role,
        question_number=evidence.question_number,
        page_number=source.page_number,
        box=CropBox(source.box.left + left, source.box.top + top, source.box.left + right, source.box.top + bottom),
        path=output,
        width=right - left,
        height=bottom - top,
        sha256=_sha256_path(output),
        source_image_sha256=source.source_image_sha256,
        source_dpi=source.source_dpi,
    )


def _combined_segment(
    evidence: RecordEvidence,
    segments: tuple[SourceCrop, ...],
    expected_role: str,
    field: str,
    output_dir: Path,
) -> SourceCrop:
    if len(segments) == 1:
        return segments[0]
    loaded: list[Image.Image] = []
    try:
        for segment in segments:
            with Image.open(segment.path) as image:
                image.load()
                loaded.append(image.convert("RGB"))
        width = max(image.width for image in loaded)
        height = sum(image.height for image in loaded)
        combined = Image.new("RGB", (width, height), "white")
        top = 0
        for image in loaded:
            combined.paste(image, (0, top))
            top += image.height
        output = output_dir / f"{field.replace('.', '-')}-combined.png"
        _write_png(output, combined)
    finally:
        for image in loaded:
            image.close()
    return SourceCrop(
        role=expected_role,
        question_number=evidence.question_number,
        page_number=segments[0].page_number,
        box=CropBox(0, 0, width, height),
        path=output,
        width=width,
        height=height,
        sha256=_sha256_path(output),
        source_image_sha256=dependency_fingerprint("field-composite", [item.sha256 for item in segments]),
        source_dpi=segments[0].source_dpi,
    )


def _prepare_field_media(config: ChapterConfig, evidence: RecordEvidence, chapter_root: Path) -> tuple[RecordEvidence, dict[str, Any]]:
    configured = _field_specs(config, evidence.question_number)
    output_dir = chapter_root / "field-media" / f"q{evidence.question_number:04d}"
    manifest: dict[str, Any] = {}
    extra_question: list[SourceCrop] = []
    extra_solution: list[SourceCrop] = []

    def prepare_field(field: str, raw_specs: Any, role: str, *, combine: bool) -> list[SourceCrop]:
        if not isinstance(raw_specs, (list, tuple)) or not raw_specs:
            raise PipelineBlocked(f"Explicit field evidence for {field} must contain one or more source segments.")
        selected = tuple(
            _selected_segment(evidence, raw, role, field, output_dir, index)
            for index, raw in enumerate(raw_specs)
        )
        prepared = [_combined_segment(evidence, selected, role, field, output_dir)] if combine else list(selected)
        destination = extra_question if role == "question" else extra_solution
        for crop in prepared:
            if all(existing.sha256 != crop.sha256 for existing in (evidence.question_crops + evidence.solution_crops + tuple(destination))):
                destination.append(crop)
        manifest[field] = {
            "crop_sha256s": [crop.sha256 for crop in prepared],
            "component_sha256s": [crop.sha256 for crop in selected],
        }
        return prepared

    if "question" in configured:
        prepare_field("question", configured["question"], "question", combine=True)
    options = configured.get("options", {})
    if not isinstance(options, Mapping):
        raise PipelineBlocked(f"field_media options for question {evidence.question_number} must be an object.")
    for label, raw_specs in sorted(options.items()):
        if label not in {"A", "B", "C", "D", "E"}:
            raise PipelineBlocked(f"field_media contains an invalid option label {label!r}.")
        prepare_field(f"options.{label}", raw_specs, "question", combine=True)
    if "solution" in configured:
        prepare_field("solution", configured["solution"], "solution", combine=False)

    mapping_fingerprint = dependency_fingerprint(manifest)
    augmented = replace(
        evidence,
        question_crops=evidence.question_crops + tuple(extra_question),
        solution_crops=evidence.solution_crops + tuple(extra_solution),
        dependency_fingerprint=dependency_fingerprint(
            evidence.dependency_fingerprint, mapping_fingerprint, _evidence_input_fingerprint(config)
        ),
    )
    return augmented, manifest


def _evidence_payload(evidence: RecordEvidence) -> dict[str, Any]:
    return {
        "chapter": evidence.chapter, "question_number": evidence.question_number,
        "source_pdf": str(evidence.source_pdf), "source_pdf_sha256": evidence.source_pdf_sha256,
        "question_crops": [_crop_payload(item) for item in evidence.question_crops],
        "answer_key_crops": [_crop_payload(item) for item in evidence.answer_key_crops],
        "solution_crops": [_crop_payload(item) for item in evidence.solution_crops],
        "dependency_fingerprint": evidence.dependency_fingerprint,
    }


def _evidence_from_payload(raw: Mapping[str, Any]) -> RecordEvidence:
    return RecordEvidence(
        chapter=int(raw["chapter"]), question_number=int(raw["question_number"]), source_pdf=Path(raw["source_pdf"]),
        source_pdf_sha256=str(raw["source_pdf_sha256"]),
        question_crops=tuple(_crop_from_payload(item) for item in raw["question_crops"]),
        answer_key_crops=tuple(_crop_from_payload(item) for item in raw["answer_key_crops"]),
        solution_crops=tuple(_crop_from_payload(item) for item in raw["solution_crops"]),
        dependency_fingerprint=str(raw["dependency_fingerprint"]),
    )


def _candidate_payload(candidate: CandidateRecord) -> dict[str, Any]:
    return {
        "chapter": candidate.chapter, "question_number": candidate.question_number, "question_text": candidate.question_text,
        "options": dict(candidate.options), "correct_answer": candidate.correct_answer,
        "answer_key_answer": candidate.answer_key_answer, "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
        "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint, "solution_steps": list(candidate.solution_steps),
        "representation": dict(candidate.representation), "source_fingerprint": candidate.source_fingerprint,
        "sha256": candidate.sha256, "status": candidate.status,
    }


def _candidate_from_payload(raw: Mapping[str, Any]) -> CandidateRecord:
    return CandidateRecord(**raw)


def _job_payload(job: VisionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id, "stage": job.stage, "prompt": job.prompt,
        "sources": [dict(source) for source in job.sources], "output_schema": job.output_schema,
        "output_path": str(job.output_path) if job.output_path else None, "job_fingerprint": job.fingerprint,
    }


def _job_from_payload(raw: Mapping[str, Any]) -> VisionJob:
    return VisionJob(
        job_id=str(raw["job_id"]), stage=str(raw["stage"]), prompt=str(raw["prompt"]), sources=tuple(raw["sources"]),
        output_schema=str(raw["output_schema"]), output_path=Path(raw["output_path"]) if raw.get("output_path") else None,
        fingerprint=str(raw["job_fingerprint"]),
    )


def _result_is_current(path: Path, job: VisionJob) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("job_id") == job.job_id and payload.get("job_fingerprint") == job.fingerprint


def _render_payload(rendered: RenderArtifacts) -> dict[str, Any]:
    return {
        "question_screenshots": {key: str(value) for key, value in rendered.question_screenshots.items()},
        "solution_screenshots": {key: str(value) for key, value in rendered.solution_screenshots.items()},
        "field_screenshots": {
            key: str(value) for key, value in getattr(rendered, "field_screenshots", {}).items()
        },
        "screenshot_hashes": dict(rendered.screenshot_hashes), "findings": list(rendered.findings),
        "renderer_version": rendered.renderer_version,
    }


def _render_from_payload(raw: Mapping[str, Any]) -> BrowserRenderArtifacts:
    return BrowserRenderArtifacts(
        question_screenshots={key: Path(value) for key, value in raw["question_screenshots"].items()},
        solution_screenshots={key: Path(value) for key, value in raw["solution_screenshots"].items()},
        field_screenshots={key: Path(value) for key, value in raw.get("field_screenshots", {}).items()},
        screenshot_hashes=raw["screenshot_hashes"], findings=tuple(raw["findings"]), renderer_version=str(raw["renderer_version"]),
    )


def _read_list(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        raise PipelineBlocked(f"Required stage artifact is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked(f"Required stage artifact is unreadable: {path}") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PipelineBlocked(f"Required stage artifact is malformed: {path}")
    return value


def _evidence(config: ChapterConfig) -> tuple[RecordEvidence, ...]:
    return tuple(_evidence_from_payload(item) for item in _read_list(_chapter_root(config) / "state" / "evidence.json"))


def _candidates(config: ChapterConfig) -> tuple[CandidateRecord, ...]:
    return tuple(_candidate_from_payload(item) for item in _read_list(_chapter_root(config) / "state" / "candidates.json"))


def _candidate_content_fingerprint(candidate: CandidateRecord) -> str:
    return dependency_fingerprint({
        "chapter": candidate.chapter, "question_number": candidate.question_number,
        "question_text": candidate.question_text, "options": dict(candidate.options),
        "correct_answer": candidate.correct_answer, "answer_key_answer": candidate.answer_key_answer,
        "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
        "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint,
        "solution_steps": list(candidate.solution_steps), "representation": dict(candidate.representation),
        "source_fingerprint": candidate.source_fingerprint,
    })


def _candidate_cache_is_current(config: ChapterConfig) -> bool:
    if not _evidence_is_current(config):
        return False
    try:
        evidence = {item.question_number: item for item in _evidence(config)}
        candidates = _candidates(config)
        ledger = AuditLedger(_work_root(config), config.chapter)
    except (OSError, ValueError, TypeError, PipelineBlocked):
        return False
    rejected: set[int] = set()
    for number in evidence:
        try:
            if ledger.record(number).status == REVIEWED_REJECTION:
                rejected.add(number)
        except KeyError:
            pass
    expected = set(evidence) - rejected
    if {item.question_number for item in candidates} != expected:
        return False
    return all(
        candidate.source_fingerprint == extraction_job_fingerprint(evidence[candidate.question_number])
        and candidate.sha256 == _candidate_content_fingerprint(candidate)
        for candidate in candidates
    )


def _render_dependency_fingerprint(
    candidates: Iterable[CandidateRecord],
    manifest: Mapping[str, Any],
    rendered_records: Iterable[Mapping[str, Any]],
) -> str:
    normalized_render_hashes: list[dict[str, Any]] = []
    for item in sorted(rendered_records, key=lambda value: int(value["question_number"])):
        artifacts = item.get("artifacts")
        hashes = artifacts.get("screenshot_hashes") if isinstance(artifacts, Mapping) else None
        if not isinstance(hashes, Mapping) or not hashes:
            raise PipelineBlocked("Render state needs exact full-card and field screenshot hashes.")
        normalized_render_hashes.append({
            "question_number": int(item["question_number"]),
            "screenshot_hashes": {str(key): str(value) for key, value in sorted(hashes.items())},
        })
    return dependency_fingerprint(
        "render-state",
        [(candidate.question_number, candidate.sha256) for candidate in sorted(candidates, key=lambda item: item.question_number)],
        manifest,
        normalized_render_hashes,
    )


def _render_state(config: ChapterConfig) -> Mapping[str, Any]:
    path = _chapter_root(config) / "state" / "renders.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked(f"Required render state is unreadable: {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
        raise PipelineBlocked(f"Required render state is malformed: {path}")
    return raw


def _renders(config: ChapterConfig) -> dict[int, RenderArtifacts]:
    return {
        int(item["question_number"]): _render_from_payload(item["artifacts"])
        for item in _render_state(config)["records"]
    }


def _render_cache_is_current(config: ChapterConfig) -> bool:
    if not _candidate_cache_is_current(config):
        return False
    try:
        candidates = _candidates(config)
        current_manifest = _application_renderer_manifest(config)
        state = _render_state(config)
        if state.get("application_renderer_manifest") != current_manifest:
            return False
        if state.get("dependency_fingerprint") != _render_dependency_fingerprint(
            candidates, current_manifest, state["records"]
        ):
            return False
        renders = _renders(config)
    except (OSError, ValueError, TypeError, KeyError, PipelineBlocked):
        return False
    if set(renders) != {candidate.question_number for candidate in candidates}:
        return False
    for rendered in renders.values():
        declared_paths = {
            **{f"question.{key}": value for key, value in rendered.question_screenshots.items()},
            **{f"solution.{key}": value for key, value in rendered.solution_screenshots.items()},
            **{f"field.{key}": value for key, value in getattr(rendered, "field_screenshots", {}).items()},
        }
        if set(declared_paths) != set(rendered.screenshot_hashes):
            return False
        if any(
            not path.is_file() or _sha256_path(path) != rendered.screenshot_hashes[key]
            for key, path in declared_paths.items()
        ):
            return False
    return True


def _render_asset_hashes(rendered: RenderArtifacts) -> tuple[str, ...]:
    hashes = rendered.screenshot_hashes
    return tuple(
        [
            f"unanswered.{key.removeprefix('question.')}:" + hashes[key]
            for key in sorted(hashes)
            if key.startswith("question.")
        ]
        + [
            f"submitted.{key.removeprefix('solution.')}:" + hashes[key]
            for key in sorted(hashes)
            if key.startswith("solution.")
        ]
        + [hashes[key] for key in sorted(hashes) if key.startswith("field.")]
    )


def _verification_job(
    candidate: CandidateRecord,
    source_crops: Iterable[SourceCrop],
    rendered: RenderArtifacts,
) -> VisionJob:
    base = create_verification_job(candidate, source_crops, rendered)
    field_sources: list[dict[str, Any]] = []
    for key, path in sorted(getattr(rendered, "field_screenshots", {}).items()):
        expected_key = f"field.{key}"
        declared = rendered.screenshot_hashes.get(expected_key)
        if not isinstance(declared, str) or len(declared) != 64 or not path.is_file() or _sha256_path(path) != declared:
            raise PipelineBlocked(f"Field render screenshot evidence is missing or stale: {key}")
        parts = key.split(".", 2)
        if len(parts) != 3 or parts[0] not in {"unanswered", "submitted"}:
            raise PipelineBlocked(f"Field render screenshot key is malformed: {key}")
        field_sources.append({
            "kind": "field_render", "state": parts[0], "viewport": parts[1], "field": parts[2],
            "path": str(path), "sha256": declared,
        })
    if not field_sources:
        raise PipelineBlocked("Verification requires field-bounded application screenshots.")
    sources = base.sources + tuple(field_sources)
    return replace(
        base,
        sources=sources,
        fingerprint=dependency_fingerprint(base.fingerprint, "field-renders", field_sources),
    )


def _clear_cache(config: ChapterConfig, command: str) -> None:
    chapter = _chapter_root(config).resolve()
    directories = ["cache"] + (["state"] if command == "run" else [])
    for name in directories:
        disposable = (chapter / name).resolve()
        try:
            disposable.relative_to(chapter)
        except ValueError as error:
            raise PipelineBlocked("Configured cache path escapes the chapter work root.") from error
        if disposable.is_dir():
            shutil.rmtree(disposable)
    stage_files = {
        "prepare": (chapter / "state" / "evidence.json",),
        "ingest-extraction": (chapter / "state" / "candidates.json",),
        "render": (chapter / "state" / "renders.json",),
        "verify": (chapter / "verification-jobs.jsonl",),
    }
    for disposable in stage_files.get(command, ()):
        resolved = disposable.resolve()
        try:
            resolved.relative_to(chapter)
        except ValueError as error:
            raise PipelineBlocked("Configured cache path escapes the chapter work root.") from error
        resolved.unlink(missing_ok=True)


def _write_pending(stage: str, count: int, queue: Path) -> int:
    print(json.dumps({"status": "vision_pending", "stage": stage, "pending_jobs": count, "queue": str(queue)}))
    return PENDING_VISION_EXIT


def _prepare(config: ChapterConfig) -> int:
    input_fingerprint = _evidence_input_fingerprint(config)
    base_records = tuple(prepare_source_evidence(config, _path_value(config, "source_pdf"), _chapter_root(config)))
    prepared = tuple(_prepare_field_media(config, evidence, _chapter_root(config)) for evidence in base_records)
    records = tuple(item[0] for item in prepared)
    state = _chapter_root(config) / "state"
    evidence_path = state / "evidence.json"
    field_media_path = state / "field-media.json"
    _atomic_json(evidence_path, [_evidence_payload(item) for item in records])
    _atomic_json(
        field_media_path,
        {str(evidence.question_number): manifest for evidence, manifest in prepared},
    )
    _atomic_json(
        state / "evidence-cache.json",
        {
            "input_fingerprint": input_fingerprint,
            "evidence_sha256": _sha256_path(evidence_path),
            "field_media_sha256": _sha256_path(field_media_path),
        },
    )
    ledger = AuditLedger(_work_root(config), config.chapter)
    for evidence in records:
        try:
            current = ledger.record(evidence.question_number)
            incoming = replace(
                current,
                source_crop_hashes=tuple(crop.sha256 for crop in evidence.question_crops + evidence.answer_key_crops + evidence.solution_crops)
                + (evidence.dependency_fingerprint,),
                policy_version=POLICY_VERSION,
                extractor_schema_version=EXTRACTOR_SCHEMA_VERSION,
            )
        except KeyError:
            incoming = AuditRecord(
                chapter=config.chapter, question_number=evidence.question_number, status=PENDING_EXTRACTION,
                source_crop_hashes=tuple(crop.sha256 for crop in evidence.question_crops + evidence.answer_key_crops + evidence.solution_crops)
                + (evidence.dependency_fingerprint,),
                policy_version=POLICY_VERSION, extractor_schema_version=EXTRACTOR_SCHEMA_VERSION,
            )
        ledger.merge_record(incoming)
    print(json.dumps({"status": "prepared", "records": len(records)}))
    return SUCCESS_EXIT


def _extract(config: ChapterConfig, results_dir: Path | None = None) -> int:
    if not _evidence_is_current(config):
        _prepare(config)
    jobs: list[VisionJob] = []
    pending: list[VisionJob] = []
    default_results = results_dir or (_chapter_root(config) / "extraction-results")
    for evidence in _evidence(config):
        job_path = _chapter_root(config) / "extraction-jobs" / f"extract-ch{config.chapter:02d}-q{evidence.question_number:04d}.json"
        job = create_extraction_job(evidence, job_path)
        jobs.append(job)
        if not _result_is_current(default_results / f"{job.job_id}.json", job):
            pending.append(job)
    queue = _chapter_root(config) / "extraction-jobs.jsonl"
    _atomic_jsonl(queue, (_job_payload(job) for job in jobs))
    return _write_pending("extract", len(pending), queue) if pending else SUCCESS_EXIT


def _image_evidence(config: ChapterConfig, raw: dict[str, Any], evidence: RecordEvidence) -> dict[str, Any]:
    representation = raw.get("representation", {})
    option_modes = representation.get("options", {}) if isinstance(representation, dict) else {}
    manifest_path = _chapter_root(config) / "state" / "field-media.json"
    try:
        chapter_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("Explicit source-backed field media evidence is missing.") from error
    record_manifest = chapter_manifest.get(str(evidence.question_number), {}) if isinstance(chapter_manifest, dict) else {}
    if not isinstance(record_manifest, dict):
        raise PipelineBlocked("Explicit source-backed field media evidence is malformed.")
    authorized = {
        crop.sha256: crop
        for crop in evidence.question_crops + evidence.solution_crops
    }

    def field_crops(field: str) -> tuple[SourceCrop, ...]:
        item = record_manifest.get(field)
        hashes = item.get("crop_sha256s") if isinstance(item, dict) else None
        if not isinstance(hashes, list) or not hashes or any(value not in authorized for value in hashes):
            raise PipelineBlocked(f"Image representation for {field} lacks explicit record-authorized field crop evidence.")
        return tuple(authorized[value] for value in hashes)

    media_crops: dict[str, Any] = {}
    alt_text: dict[str, Any] = {}
    if representation.get("question") == "image":
        question = field_crops("question")
        if len(question) != 1:
            raise PipelineBlocked("Question image field evidence must be combined into one complete display artifact.")
        media_crops["question"] = question[0]
        alt_text["question"] = raw.get("question_text")
    image_options = {label: field_crops(f"options.{label}") for label, mode in option_modes.items() if mode == "image"}
    if image_options:
        if any(len(crops) != 1 for crops in image_options.values()):
            raise PipelineBlocked("Option image field evidence must be combined into one option-only display artifact.")
        media_crops["options"] = {label: crops[0] for label, crops in image_options.items()}
        alt_text["options"] = {label: raw["options"][label] for label in image_options}
    if representation.get("solution") == "image":
        solution = field_crops("solution")
        media_crops["solution"] = solution
        semantic_solution = " ".join(str(step).strip() for step in raw.get("solution_steps", ()) if str(step).strip())
        alt_text["solution"] = [semantic_solution for _ in solution]
    return {**raw, "source_fingerprint": raw["job_fingerprint"], "media_crops": media_crops, "alt_text": alt_text}


def _ingest_extraction(config: ChapterConfig, results: Path) -> int:
    if not _evidence_is_current(config):
        _prepare(config)
        return _extract(config, results)
    records: list[CandidateRecord] = []
    ledger = AuditLedger(_work_root(config), config.chapter)
    for evidence in _evidence(config):
        try:
            if ledger.record(evidence.question_number).status == REVIEWED_REJECTION:
                continue
        except KeyError:
            pass
        job_file = _chapter_root(config) / "extraction-jobs" / f"extract-ch{config.chapter:02d}-q{evidence.question_number:04d}.json"
        job = create_extraction_job(evidence, job_file)
        result_path = results / f"{job.job_id}.json"
        if not _result_is_current(result_path, job):
            return _write_pending("extract", 1, _chapter_root(config) / "extraction-jobs.jsonl")
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        findings = validate_record(raw)
        try:
            ingest_extraction_result(job, result_path)
            candidate = assemble_candidate(evidence, _image_evidence(config, raw, evidence), findings)
        except PipelineBlocked as error:
            current = ledger.record(evidence.question_number)
            quarantined = replace(
                current, status=BLOCKED, candidate_sha256="", asset_hashes=(), reviewer=str(raw.get("reviewer", "")),
                rejection_reason="", dependency_fingerprint="", field_verdicts={},
                findings=tuple([*(item.rule_id for item in findings), str(error)]),
            )
            ledger.merge_record(quarantined)
            if ledger.record(evidence.question_number).status != BLOCKED:
                ledger.merge_record(quarantined)
            if records:
                _atomic_json(
                    _chapter_root(config) / "state" / "candidates.json",
                    [_candidate_payload(item) for item in records],
                )
            raise
        records.append(candidate)
        current = ledger.record(evidence.question_number)
        ledger.merge_record(replace(
            current, status=PENDING_RENDER, candidate_sha256=candidate.sha256, asset_hashes=(),
            verifier_schema_version=VERIFIER_SCHEMA_VERSION, renderer_version="", application_asset_version="",
            reviewer="", rejection_reason="", dependency_fingerprint="", findings=tuple(item.rule_id for item in findings), field_verdicts={},
        ))
    _atomic_json(_chapter_root(config) / "state" / "candidates.json", [_candidate_payload(item) for item in records])
    print(json.dumps({"status": "extraction_ingested", "records": len(records)}))
    return SUCCESS_EXIT


def _build(config: ChapterConfig) -> int:
    if not _candidate_cache_is_current(config):
        resumed = _ingest_extraction(config, _chapter_root(config) / "extraction-results")
        if resumed != SUCCESS_EXIT:
            return resumed
    records = _candidates(config)
    if not records:
        raise PipelineBlocked("Build requires extracted candidates.")
    print(json.dumps({"status": "built", "records": len(records)}))
    return SUCCESS_EXIT


def _viewports(config: ChapterConfig) -> tuple[tuple[int, int], ...]:
    raw = config.extras.get("validation_viewports", DEFAULT_VIEWPORTS)
    try:
        return tuple((int(item[0]), int(item[1])) for item in raw)
    except (TypeError, ValueError, IndexError) as error:
        raise PipelineBlocked("validation_viewports must contain width/height pairs.") from error


def _render(config: ChapterConfig) -> int:
    if not _candidate_cache_is_current(config):
        resumed = _ingest_extraction(config, _chapter_root(config) / "extraction-results")
        if resumed != SUCCESS_EXIT:
            return resumed
    ledger = AuditLedger(_work_root(config), config.chapter)
    candidates = _candidates(config)
    manifest = _application_renderer_manifest(config)
    rendered_records: list[dict[str, Any]] = []
    for candidate in candidates:
        rendered = render_candidate(candidate, {}, _viewports(config), _chapter_root(config) / "renders" / f"q{candidate.question_number:04d}")
        rendered = replace(rendered, renderer_version=str(manifest["manifest_fingerprint"]))
        rendered_records.append({"question_number": candidate.question_number, "artifacts": _render_payload(rendered)})
        state_hashes = _render_asset_hashes(rendered)
        current = ledger.record(candidate.question_number)
        ledger.merge_record(replace(
            current, status=BLOCKED if rendered.findings else PENDING_VISION, asset_hashes=state_hashes,
            renderer_version=str(manifest["renderer_fingerprint"]),
            application_asset_version=str(manifest["application_fingerprint"]),
            findings=tuple(rendered.findings), reviewer="", dependency_fingerprint="", field_verdicts={},
        ))
    _atomic_json(_chapter_root(config) / "state" / "application-renderer-manifest.json", manifest)
    _atomic_json(_chapter_root(config) / "state" / "renders.json", {
        "application_renderer_manifest": manifest,
        "dependency_fingerprint": _render_dependency_fingerprint(candidates, manifest, rendered_records),
        "records": rendered_records,
    })
    if any(item["artifacts"]["findings"] for item in rendered_records):
        raise PipelineBlocked("Mechanical application rendering findings remain quarantined.")
    print(json.dumps({"status": "rendered", "records": len(rendered_records)}))
    return SUCCESS_EXIT


def _verify(config: ChapterConfig, results_dir: Path | None = None) -> int:
    if not _candidate_cache_is_current(config):
        resumed = _ingest_extraction(config, _chapter_root(config) / "extraction-results")
        if resumed != SUCCESS_EXIT:
            return resumed
    if not _render_cache_is_current(config):
        rendered = _render(config)
        if rendered != SUCCESS_EXIT:
            return rendered
    evidence = {item.question_number: item for item in _evidence(config)}
    renders = _renders(config)
    jobs: list[VisionJob] = []
    pending: list[VisionJob] = []
    default_results = results_dir or (_chapter_root(config) / "verification-results")
    for candidate in _candidates(config):
        source = evidence[candidate.question_number]
        job = _verification_job(
            candidate, source.question_crops + source.answer_key_crops + source.solution_crops, renders[candidate.question_number]
        )
        jobs.append(job)
        if not _result_is_current(default_results / f"{job.job_id}.json", job):
            pending.append(job)
    queue = _chapter_root(config) / "verification-jobs.jsonl"
    _atomic_jsonl(queue, (_job_payload(job) for job in jobs))
    return _write_pending("verify", len(pending), queue) if pending else SUCCESS_EXIT


def _ingest_verification(config: ChapterConfig, results: Path) -> int:
    if not _candidate_cache_is_current(config):
        resumed = _ingest_extraction(config, _chapter_root(config) / "extraction-results")
        if resumed != SUCCESS_EXIT:
            return resumed
    if not _render_cache_is_current(config):
        rendered = _render(config)
        if rendered != SUCCESS_EXIT:
            return rendered
        return _verify(config, results)
    evidence = {item.question_number: item for item in _evidence(config)}
    renders = _renders(config)
    ledger = AuditLedger(_work_root(config), config.chapter)
    failed = 0
    for candidate in _candidates(config):
        source = evidence[candidate.question_number]
        job = _verification_job(candidate, source.question_crops + source.answer_key_crops + source.solution_crops,
                                renders[candidate.question_number])
        result_path = results / f"{job.job_id}.json"
        if not _result_is_current(result_path, job):
            return _write_pending("verify", 1, _chapter_root(config) / "verification-jobs.jsonl")
        result = ingest_verification_result(job, result_path)
        current = ledger.record(candidate.question_number)
        passing = all(value == "pass" for value in result.verdicts.values()) and not current.findings
        updated = replace(
            current, status=APPROVED_FOR_PUBLISH if passing else BLOCKED, reviewer=result.reviewer,
            rejection_reason="", field_verdicts=result.verdicts,
            findings=() if passing else tuple(f"{key}: {value}" for key, value in result.differences.items()),
            dependency_fingerprint="",
        )
        if passing:
            updated = replace(updated, dependency_fingerprint=approval_dependency_fingerprint(updated))
        else:
            failed += 1
        ledger.merge_record(updated)
    if failed:
        raise PipelineBlocked(f"{failed} verification record(s) remain quarantined.")
    print(json.dumps({"status": "verification_ingested", "records": len(_candidates(config))}))
    return SUCCESS_EXIT


def _release_summary(config: ChapterConfig) -> AuditSummary:
    if not _evidence_is_current(config) or not _candidate_cache_is_current(config):
        raise PipelineBlocked("Release gate requires current source, evidence, extraction, and candidate fingerprints.")
    if not _render_cache_is_current(config):
        raise PipelineBlocked("Release gate requires a fresh application render and independent vision verification.")
    manifest = _application_renderer_manifest(config)
    renders = _renders(config)
    ledger = AuditLedger(_work_root(config), config.chapter)
    included_numbers = set(range(config.question_numbers[0], config.question_numbers[1] + 1)) - set(config.intentional_exclusions)
    for number in sorted(included_numbers):
        try:
            record = ledger.record(number)
        except KeyError:
            continue
        if record.status == APPROVED_FOR_PUBLISH and (
            record.renderer_version != manifest["renderer_fingerprint"]
            or record.application_asset_version != manifest["application_fingerprint"]
        ):
            raise PipelineBlocked(
                f"Question {number} approval is stale for the current application/renderer fingerprint; rerender and reverify."
            )
        if record.status == APPROVED_FOR_PUBLISH and record.asset_hashes != _render_asset_hashes(renders[number]):
            raise PipelineBlocked(
                f"Question {number} approval does not match the current exact full-card and field render evidence; reverify."
            )
    return ledger.validate_release_gate(config)


def _record_reviewed_rejection(
    config: ChapterConfig, question_number: int, reviewer: str, reason: str
) -> int:
    reviewer = reviewer.strip()
    reason = reason.strip()
    included = set(range(config.question_numbers[0], config.question_numbers[1] + 1)) - set(config.intentional_exclusions)
    if question_number not in included:
        raise PipelineBlocked(f"Question {question_number} is not an included record in this chapter config.")
    if not reviewer:
        raise PipelineBlocked("A reviewed rejection requires a non-empty reviewer identity.")
    if len(reason) < 12:
        raise PipelineBlocked("A reviewed rejection requires a specific reason of at least 12 characters.")
    ledger = AuditLedger(_work_root(config), config.chapter)
    try:
        current = ledger.record(question_number)
    except KeyError as error:
        raise PipelineBlocked(f"Question {question_number} has no pipeline audit record to reject.") from error
    if current.status == REVIEWED_REJECTION:
        if current.reviewer == reviewer and current.rejection_reason == reason:
            print(json.dumps({"status": REVIEWED_REJECTION, "question_number": question_number}))
            return SUCCESS_EXIT
        raise PipelineBlocked("A reviewed rejection is already recorded; do not silently replace its review evidence.")
    if current.status != BLOCKED:
        raise PipelineBlocked(
            f"Question {question_number} must first be quarantined by a field or vision gate; current status is {current.status}."
        )
    ledger.merge_record(replace(
        current,
        status=REVIEWED_REJECTION,
        reviewer=reviewer,
        rejection_reason=reason,
        dependency_fingerprint="",
        field_verdicts={},
    ))
    print(json.dumps({"status": REVIEWED_REJECTION, "question_number": question_number}))
    return SUCCESS_EXIT


def _package(config: ChapterConfig) -> int:
    summary = _release_summary(config)
    approved_numbers = {int(record["question_number"]) for record in summary["approved_records"]}
    approved_candidates = tuple(
        candidate for candidate in _candidates(config) if candidate.question_number in approved_numbers
    )
    result = build_candidate_package(config, approved_candidates, summary, _candidate_path(config))
    _atomic_json(_chapter_root(config) / "package-result.json", {
        "path": str(result.path), "sha256": result.sha256, "question_count": result.question_count,
        "rejected_count": result.rejected_count, "audit_sha256": summary["audit_sha256"],
    })
    _atomic_json(_candidate_path(config).with_suffix(".package.json"), {
        "candidate_sha256": result.sha256,
        "audit_sha256": summary["audit_sha256"],
    })
    print(json.dumps({"status": "packaged", "path": str(result.path), "sha256": result.sha256}))
    return SUCCESS_EXIT


def _promote(config: ChapterConfig) -> int:
    summary = _release_summary(config)
    receipt = promote_candidate(_candidate_path(config), _published_path(config), summary)
    print(json.dumps({"status": "promoted", "destination": str(receipt.destination), "sha256": receipt.candidate_sha256}))
    return SUCCESS_EXIT


def _run(config: ChapterConfig) -> int:
    if not _evidence_is_current(config):
        _prepare(config)
    pending = _extract(config)
    if pending:
        return pending
    extraction_results = _chapter_root(config) / "extraction-results"
    _ingest_extraction(config, extraction_results)
    _build(config)
    _render(config)
    pending = _verify(config)
    if pending:
        return pending
    _ingest_verification(config, _chapter_root(config) / "verification-results")
    return _package(config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m textbook_chapters_v2")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "extract", "build", "render", "verify", "package", "promote", "run"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--force", action="store_true")
    for name in ("ingest-extraction", "ingest-verification"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--results", required=True, type=Path)
        command.add_argument("--force", action="store_true")
    reject = subcommands.add_parser("reject")
    reject.add_argument("--config", required=True, type=Path)
    reject.add_argument("--question", required=True, type=int)
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        config = ChapterConfig.load(arguments.config)
        if getattr(arguments, "force", False):
            _clear_cache(config, arguments.command)
        handlers = {
            "prepare": lambda: _prepare(config), "extract": lambda: _extract(config),
            "ingest-extraction": lambda: _ingest_extraction(config, arguments.results.resolve()),
            "build": lambda: _build(config), "render": lambda: _render(config), "verify": lambda: _verify(config),
            "ingest-verification": lambda: _ingest_verification(config, arguments.results.resolve()),
            "reject": lambda: _record_reviewed_rejection(
                config, arguments.question, arguments.reviewer, arguments.reason
            ),
            "package": lambda: _package(config), "promote": lambda: _promote(config), "run": lambda: _run(config),
        }
        return handlers[arguments.command]()
    except PipelineBlocked as error:
        print(json.dumps({"status": "blocked", "error": str(error)}), file=sys.stderr)
        return BLOCKED_EXIT
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return ERROR_EXIT


__all__ = ["BLOCKED_EXIT", "ERROR_EXIT", "PENDING_VISION_EXIT", "SUCCESS_EXIT", "main"]
