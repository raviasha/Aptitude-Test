"""Resumable full-card rendering through the real KSAT application."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from textbook_chapters_v2.models import CandidateRecord, PipelineBlocked
from textbook_chapters_v2.render import WORKSPACE_ROOT, _launch_browser, render_candidate as v2_render_candidate
from textbook_chapters_v2.store import canonical_json, dependency_fingerprint


VIEWPORTS = ((1024, 768), (1600, 900))
STATES = ("unanswered", "submitted")
_MANIFEST_FIELDS = {
    "record_id", "candidate_sha256", "complete", "renderer_fingerprint",
    "application_fingerprint", "browser_fingerprint", "browser_identity",
    "viewports", "screenshots", "findings", "dependency_fingerprint", "manifest_sha256",
}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_id(candidate: CandidateRecord) -> str:
    if not isinstance(candidate, CandidateRecord):
        raise TypeError("candidates must contain CandidateRecord values.")
    if candidate.chapter != 1 or candidate.question_number <= 0:
        raise PipelineBlocked("Render gate accepts Chapter 1 candidates only.")
    return f"ch{candidate.chapter:02d}-q{candidate.question_number:04d}"


def _normalized_browser_identity(value: Any) -> str:
    identity = " ".join(str(value).split()).casefold()
    if not identity.startswith(("microsoft-edge:", "playwright-chromium:")) or not identity.split(":", 1)[1]:
        raise PipelineBlocked("Render gate requires an exact browser engine and version identity.")
    return identity


def _resolve_browser_identity() -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise PipelineBlocked("Python Playwright is required for application rendering.") from error
    with sync_playwright() as playwright:
        browser, selected = _launch_browser(playwright)
        try:
            version = " ".join(str(browser.version).split()).casefold()
            engine = "microsoft-edge" if selected.startswith("Microsoft Edge") else "playwright-chromium"
            return _normalized_browser_identity(f"{engine}:{version}")
        finally:
            browser.close()


def _file_inventory(paths: Iterable[Path], root: Path) -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256_path(path)}
        for path in sorted({Path(path).resolve() for path in paths}, key=lambda item: item.as_posix())
    ]


def _application_file_inventory(paths: Iterable[Path], root: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for path in sorted({Path(path).resolve() for path in paths}, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if relative == "app.py":
            normalized_carriage_return = b"\r" if content.count(b"\r\n") > content.count(b"\n") // 2 else b""
            content, replacements = re.subn(
                rb'(?m)^APP_VERSION = "[^"\r\n]+"\r?$',
                b'APP_VERSION = "1.3.3"' + normalized_carriage_return,
                content,
            )
            if replacements != 1:
                raise PipelineBlocked("KSAT application version declaration is missing or ambiguous.")
        inventory.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    return inventory


def _current_fingerprints(browser_identity: str) -> tuple[str, str, str]:
    root = WORKSPACE_ROOT.resolve()
    startup = tuple(
        path for path in (root / "app.py", root / "question_media.py", root / "chapter_repairs.py")
        if path.is_file()
    )
    static = root / "static"
    application_paths = (*startup, *sorted(path for path in static.rglob("*") if path.is_file()))
    if not (root / "app.py").is_file() or not (root / "question_media.py").is_file() or not static.is_dir():
        raise PipelineBlocked("KSAT application render contract is incomplete.")
    application = dependency_fingerprint(
        "ksat-application-assets", _application_file_inventory(application_paths, root)
    )
    render_module = Path(__file__).resolve().parents[1] / "textbook_chapters_v2" / "render.py"
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        playwright_version = "unavailable"
    browser = dependency_fingerprint("browser-runtime", browser_identity)
    renderer = dependency_fingerprint(
        "agent-triage-renderer-v1",
        _sha256_path(render_module),
        f"{sys.version_info.major}.{sys.version_info.minor}",
        playwright_version,
        [list(item) for item in VIEWPORTS],
        browser_identity,
    )
    return application, renderer, browser


def _with_hashes(core: Mapping[str, Any]) -> dict[str, Any]:
    dependency = dependency_fingerprint(dict(core))
    with_dependency = {**dict(core), "dependency_fingerprint": dependency}
    return {**with_dependency, "manifest_sha256": dependency_fingerprint(with_dependency)}


def _manifest_is_current(
    raw: Any,
    candidate: CandidateRecord,
    *,
    application_fingerprint: str | None = None,
    renderer_fingerprint: str | None = None,
    browser_identity: str | None = None,
    browser_fingerprint: str | None = None,
) -> bool:
    if not _manifest_is_audit_compatible(
        raw,
        candidate,
        application_fingerprint=application_fingerprint,
        renderer_fingerprint=renderer_fingerprint,
        browser_identity=browser_identity,
        browser_fingerprint=browser_fingerprint,
    ):
        return False
    return raw["complete"] is True and raw["findings"] == []


def _manifest_is_audit_compatible(
    raw: Any,
    candidate: CandidateRecord,
    *,
    application_fingerprint: str | None = None,
    renderer_fingerprint: str | None = None,
    browser_identity: str | None = None,
    browser_fingerprint: str | None = None,
) -> bool:
    """Return whether Task 4 can consume this exact current manifest safely."""
    try:
        if not isinstance(raw, Mapping) or set(raw) != _MANIFEST_FIELDS:
            return False
        manifest = dict(raw)
        if manifest["record_id"] != _record_id(candidate) or manifest["candidate_sha256"] != candidate.sha256:
            return False
        if not isinstance(manifest["complete"], bool):
            return False
        if not isinstance(manifest["findings"], list) or any(not isinstance(item, str) for item in manifest["findings"]):
            return False
        if application_fingerprint is not None and manifest["application_fingerprint"] != application_fingerprint:
            return False
        if renderer_fingerprint is not None and manifest["renderer_fingerprint"] != renderer_fingerprint:
            return False
        if browser_identity is not None and manifest["browser_identity"] != browser_identity:
            return False
        if browser_fingerprint is not None and manifest["browser_fingerprint"] != browser_fingerprint:
            return False
        if manifest["viewports"] != [list(item) for item in VIEWPORTS]:
            return False
        screenshots = manifest["screenshots"]
        labels = {f"{width}x{height}" for width, height in VIEWPORTS}
        if not isinstance(screenshots, Mapping) or set(screenshots) != labels:
            return False
        for label in labels:
            states = screenshots[label]
            if not isinstance(states, Mapping) or set(states) != set(STATES):
                return False
            for state in STATES:
                item = states[state]
                if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
                    return False
                path = Path(item["path"])
                if not path.is_file() or _sha256_path(path) != item["sha256"]:
                    return False
        core = {key: value for key, value in manifest.items() if key not in {"dependency_fingerprint", "manifest_sha256"}}
        if manifest["dependency_fingerprint"] != dependency_fingerprint(core):
            return False
        return manifest["manifest_sha256"] == dependency_fingerprint({**core, "dependency_fingerprint": manifest["dependency_fingerprint"]})
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as temporary:
            name = temporary.name
            temporary.write(canonical_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def _read_manifest(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def render_all_candidates(
    candidates: Iterable[CandidateRecord],
    assets: Mapping[str, Any] | Path,
    work_root: Path,
    *,
    renderer: Callable[..., Any] | None = None,
    browser_identity_resolver: Callable[[], str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Render candidates sequentially and return current resumable manifests."""
    values = tuple(candidates)
    ids = [_record_id(candidate) for candidate in values]
    if len(set(ids)) != len(ids):
        raise PipelineBlocked("Render gate refuses duplicate candidate record IDs.")
    identity = _normalized_browser_identity((browser_identity_resolver or _resolve_browser_identity)())
    application, renderer_hash, browser_hash = _current_fingerprints(identity)
    adapter = renderer or v2_render_candidate
    manifests: list[dict[str, Any]] = []
    for candidate, record_id in sorted(zip(values, ids), key=lambda item: item[1]):
        record_root = Path(work_root) / "renders" / record_id
        manifest_path = record_root / "manifest.json"
        existing = _read_manifest(manifest_path)
        if _manifest_is_current(
            existing,
            candidate,
            application_fingerprint=application,
            renderer_fingerprint=renderer_hash,
            browser_identity=identity,
            browser_fingerprint=browser_hash,
        ):
            manifests.append(dict(existing))
            continue
        screenshots: dict[str, dict[str, dict[str, str]]] = {}
        findings: list[str] = []
        try:
            rendered = adapter(candidate, assets, VIEWPORTS, record_root / "screenshots")
            observed_identity = getattr(rendered, "browser_runtime", "") or identity
            if _normalized_browser_identity(observed_identity) != identity:
                raise PipelineBlocked("Rendered browser identity differs from the current runtime identity.")
            findings.extend(str(item) for item in rendered.findings)
            expected_labels = {f"{width}x{height}" for width, height in VIEWPORTS}
            if set(rendered.question_screenshots) != expected_labels or set(rendered.solution_screenshots) != expected_labels:
                raise PipelineBlocked("Renderer returned a missing or extra viewport/state inventory.")
            for width, height in VIEWPORTS:
                label = f"{width}x{height}"
                screenshots[label] = {}
                for state, paths in (("unanswered", rendered.question_screenshots), ("submitted", rendered.solution_screenshots)):
                    path = Path(paths[label])
                    screenshots[label][state] = {"path": str(path), "sha256": _sha256_path(path)}
        except Exception as error:
            findings.append(f"RENDER_ERROR:{type(error).__name__}:{error}")
        core = {
            "record_id": record_id,
            "candidate_sha256": candidate.sha256,
            "complete": not findings and set(screenshots) == {"1024x768", "1600x900"}
                and all(set(states) == set(STATES) for states in screenshots.values()),
            "renderer_fingerprint": renderer_hash,
            "application_fingerprint": application,
            "browser_fingerprint": browser_hash,
            "browser_identity": identity,
            "viewports": [list(item) for item in VIEWPORTS],
            "screenshots": screenshots,
            "findings": findings,
        }
        manifest = _with_hashes(core)
        _atomic_json(manifest_path, manifest)
        if _manifest_is_audit_compatible(
            manifest,
            candidate,
            application_fingerprint=application,
            renderer_fingerprint=renderer_hash,
            browser_identity=identity,
            browser_fingerprint=browser_hash,
        ):
            manifests.append(manifest)
    return tuple(manifests)


__all__ = ["render_all_candidates"]
