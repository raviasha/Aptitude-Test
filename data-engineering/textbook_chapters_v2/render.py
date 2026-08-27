"""Capture validation evidence from the real KSAT browser renderer."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .models import CandidateRecord, RenderArtifacts as PipelineRenderArtifacts


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CHROMIUM_INSTALL_COMMAND = "python -m playwright install chromium"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ScreenshotArtifact:
    path: Path
    state: str
    viewport: str
    field: str = "card"


@dataclass(frozen=True)
class RenderArtifacts(PipelineRenderArtifacts):
    """Pipeline-compatible render evidence with state and field conveniences."""

    field_screenshots: Mapping[str, Path] = field(default_factory=dict)
    browser_runtime: str = ""
    server_port: int = 0
    temporary_data_dir: Path = Path()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "field_screenshots",
            MappingProxyType({str(key): Path(value) for key, value in self.field_screenshots.items()}),
        )
        object.__setattr__(self, "temporary_data_dir", Path(self.temporary_data_dir))

    @property
    def unanswered(self) -> tuple[ScreenshotArtifact, ...]:
        return tuple(
            ScreenshotArtifact(path=Path(path), state="unanswered", viewport=viewport)
            for viewport, path in self.question_screenshots.items()
        )

    @property
    def submitted(self) -> tuple[ScreenshotArtifact, ...]:
        return tuple(
            ScreenshotArtifact(path=Path(path), state="submitted", viewport=viewport)
            for viewport, path in self.solution_screenshots.items()
        )

    @property
    def clipping_findings(self) -> list[str]:
        return list(self.findings)

    def all_screenshots(self) -> tuple[ScreenshotArtifact, ...]:
        fields = tuple(
            ScreenshotArtifact(
                path=Path(path),
                state=key.split(".", 1)[0],
                viewport=key.split(".", 2)[1],
                field=key.split(".", 2)[2],
            )
            for key, path in self.field_screenshots.items()
        )
        return self.unanswered + self.submitted + fields


def _viewport_label(viewport: tuple[int, int]) -> str:
    return f"{viewport[0]}x{viewport[1]}"


def _validated_viewports(viewports: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for value in viewports:
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 2
            or isinstance(value[0], bool)
            or isinstance(value[1], bool)
            or not isinstance(value[0], int)
            or not isinstance(value[1], int)
            or value[0] <= 0
            or value[1] <= 0
        ):
            raise ValueError("Each viewport must contain positive integer width and height values.")
        viewport = (value[0], value[1])
        if viewport in result:
            raise ValueError(f"Duplicate validation viewport: {_viewport_label(viewport)}")
        result.append(viewport)
    if not result:
        raise ValueError("At least one validation viewport is required.")
    return tuple(result)


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != _PNG_SIGNATURE or content[12:16] != b"IHDR":
        raise ValueError("Validation display media must be a PNG image.")
    width, height = struct.unpack(">II", content[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("Validation display media has invalid PNG dimensions.")
    return width, height


def _asset_bytes(raw: Mapping[str, Any], assets: Mapping[str, Any] | Path) -> bytes:
    source_path = raw.get("source_path")
    if isinstance(source_path, (str, os.PathLike)) and str(source_path):
        path = Path(source_path)
        if not path.is_file():
            raise FileNotFoundError(f"Validation media source does not exist: {path}")
        return path.read_bytes()
    asset = raw.get("asset")
    if not isinstance(asset, str) or not asset:
        raise ValueError("Validation display media needs a local source_path or asset key.")
    if isinstance(assets, Path):
        value: Any = assets / Path(asset)
    else:
        if asset not in assets:
            raise FileNotFoundError(f"Validation media asset is missing: {asset}")
        value = assets[asset]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(f"Validation media asset does not exist: {path}")
    return path.read_bytes()


def _attempt_media_item(raw: Any, assets: Mapping[str, Any] | Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("Validation display-media entries must be mappings.")
    content = _asset_bytes(raw, assets)
    digest = hashlib.sha256(content).hexdigest()
    declared = raw.get("sha256") or raw.get("source_sha256")
    if declared is not None and declared != digest:
        raise ValueError("Validation display-media SHA-256 does not match its local PNG.")
    alt_text = raw.get("alt_text")
    if not isinstance(alt_text, str) or not alt_text.strip():
        raise ValueError("Validation display media requires non-empty verified alt text.")
    width, height = _png_dimensions(content)
    return {
        "url": "data:image/png;base64," + base64.b64encode(content).decode("ascii"),
        "alt_text": alt_text.strip(),
        "width": width,
        "height": height,
    }


def _attempt_display_media(raw: Any, assets: Mapping[str, Any] | Path) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Candidate display_media must be a mapping.")
    if set(raw) - {"question", "options", "solution"}:
        raise ValueError("Candidate display_media contains an unknown field.")
    result: dict[str, Any] = {}
    if "question" in raw:
        result["question"] = _attempt_media_item(raw["question"], assets)
    if "options" in raw:
        options = raw["options"]
        if not isinstance(options, Mapping):
            raise ValueError("Candidate option display media must be a mapping.")
        result["options"] = {
            str(label): _attempt_media_item(value, assets)
            for label, value in sorted(options.items())
        }
    if "solution" in raw:
        solution = raw["solution"]
        if not isinstance(solution, (list, tuple)) or not solution:
            raise ValueError("Candidate solution display media must be a non-empty list.")
        result["solution"] = [_attempt_media_item(value, assets) for value in solution]
    return result


def _candidate_mapping(candidate: CandidateRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(candidate, CandidateRecord):
        return {
            "key": f"ch{candidate.chapter:02d}-q{candidate.question_number:04d}",
            "question_text": candidate.question_text,
            "category": "Quantitative Aptitude",
            "chapter": str(candidate.chapter),
            "difficulty": "Medium",
            "options": dict(candidate.options),
            "correct_answer": candidate.correct_answer,
            "explanation": "",
            "solution_steps": list(candidate.solution_steps),
            "display_media": candidate.representation.get("media", {}),
        }
    if not isinstance(candidate, Mapping):
        raise TypeError("candidate must be a CandidateRecord or parsed candidate mapping.")
    return {str(key): value for key, value in candidate.items()}


def _attempt_payload(
    candidate: CandidateRecord | Mapping[str, Any],
    assets: Mapping[str, Any] | Path,
    *,
    submitted: bool,
) -> dict[str, Any]:
    record = _candidate_mapping(candidate)
    options = record.get("options")
    if not isinstance(options, Mapping) or not options:
        raise ValueError("Candidate options must be a non-empty mapping.")
    correct_answer = record.get("correct_answer")
    if not isinstance(correct_answer, str) or correct_answer not in options:
        raise ValueError("Candidate correct_answer must identify an option.")
    question_text = record.get("question_text")
    if not isinstance(question_text, str) or not question_text.strip():
        raise ValueError("Candidate question_text must be non-empty.")
    steps = record.get("solution_steps")
    if not isinstance(steps, (list, tuple)) or not steps or any(not isinstance(step, str) or not step.strip() for step in steps):
        raise ValueError("Candidate solution_steps must contain non-empty strings.")
    display_media = _attempt_display_media(record.get("display_media"), assets)
    question_media = {key: display_media[key] for key in ("question", "options") if key in display_media}
    feedback: dict[str, Any] | None = None
    if submitted:
        feedback = {
            "correct": True,
            "correct_answer": correct_answer,
            "explanation": str(record.get("explanation", "")),
            "solution_steps": [str(step) for step in steps],
        }
        if "solution" in display_media:
            feedback["display_media"] = {"solution": display_media["solution"]}
    question: dict[str, Any] = {
        "question_id": 1,
        "question_text": question_text,
        "category": str(record.get("category", "Quantitative Aptitude")),
        "chapter": str(record.get("chapter", "Validation")),
        "difficulty": str(record.get("difficulty", "Medium")),
        "options": {str(label): str(value) for label, value in options.items()},
        "selected_answer": correct_answer if submitted else None,
    }
    if record.get("question_html"):
        question["question_html"] = str(record["question_html"])
    if question_media:
        question["display_media"] = question_media
    if feedback is not None:
        question["feedback"] = feedback
    return {
        "attempt_id": "local-render-validation",
        "status": "submitted" if submitted else "in_progress",
        "test_name": "Local render validation",
        "mode": "student_practice",
        "feedback_allowed": True,
        "proctored": False,
        "total_questions": 1,
        "questions": [question],
    }


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(process: subprocess.Popen[Any], url: str, log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            raise RuntimeError(f"KSAT validation server exited during startup.\n{log}".rstrip())
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise RuntimeError(f"KSAT validation server did not become ready at {url}.")


def _stop_server(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _edge_executable() -> Path | None:
    candidates: list[Path] = []
    located = shutil.which("msedge")
    if located:
        candidates.append(Path(located))
    if os.name == "nt":
        for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.getenv(environment_name)
            if root:
                candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return next((path for path in candidates if path.is_file()), None)


def _launch_browser(playwright: Any) -> tuple[Any, str]:
    edge = _edge_executable()
    edge_error: Exception | None = None
    if edge is not None:
        try:
            return playwright.chromium.launch(executable_path=str(edge), headless=True), f"Microsoft Edge ({edge})"
        except Exception as error:  # Playwright uses one cross-runtime Error type.
            edge_error = error
    try:
        return playwright.chromium.launch(headless=True), "Playwright Chromium"
    except Exception as chromium_error:
        details = f" Installed Edge failed to launch: {edge_error}" if edge_error is not None else ""
        raise RuntimeError(
            "No usable Microsoft Edge or Playwright Chromium runtime was found. "
            f"Install Chromium with: {CHROMIUM_INSTALL_COMMAND}.{details} "
            f"Chromium launch failed: {chromium_error}"
        ) from chromium_error


_WAIT_FOR_VISUALS = """
async () => {
  if (document.fonts?.ready) await document.fonts.ready;
  const images = [...document.images];
  await Promise.allSettled(images.map(image => image.decode()));
}
"""


_MECHANICAL_FINDINGS = """
() => {
  const findings = [];
  const visible = element => {
    if (!element || element.closest('.sr-only')) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const name = element => {
    if (element.matches('.question')) return 'question-card';
    if (element.matches('.feedback')) return 'feedback';
    if (element.matches('.answers')) return 'answers';
    if (element.matches('.question-meta')) return 'question-meta';
    if (element.matches('.question footer')) return 'question-footer';
    if (element.matches('.institution-rail-left')) return 'left-brand-rail';
    if (element.matches('.institution-rail-right')) return 'right-brand-rail';
    if (element.matches('main.assessment')) return 'assessment';
    return element.tagName.toLowerCase() + (element.className ? '.' + String(element.className).trim().replaceAll(' ', '.') : '');
  };
  const root = document.documentElement;
  if (root.scrollWidth > root.clientWidth + 1) {
    findings.push(`horizontal-overflow: document ${root.scrollWidth}>${root.clientWidth}`);
  }
  document.querySelectorAll('img').forEach(image => {
    if (!image.complete || image.naturalWidth === 0 || image.naturalHeight === 0) {
      findings.push(`missing-image: ${image.alt || image.getAttribute('src') || '(unnamed)'}`);
    }
  });
  document.querySelectorAll('.question, .question *').forEach(element => {
    if (!visible(element)) return;
    const style = getComputedStyle(element);
    if ((style.overflowX === 'hidden' || style.overflowX === 'clip') && element.scrollWidth > element.clientWidth + 1) {
      findings.push(`horizontal-clipping: ${name(element)} ${element.scrollWidth}>${element.clientWidth}`);
    }
    if ((style.overflowY === 'hidden' || style.overflowY === 'clip') && element.scrollHeight > element.clientHeight + 1) {
      findings.push(`vertical-clipping: ${name(element)} ${element.scrollHeight}>${element.clientHeight}`);
    }
  });
  const card = document.querySelector('.question');
  if (card) {
    const bounds = card.getBoundingClientRect();
    card.querySelectorAll('*').forEach(element => {
      if (!visible(element)) return;
      const rect = element.getBoundingClientRect();
      if (rect.left < bounds.left - 1 || rect.right > bounds.right + 1 || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1) {
        findings.push(`outside-question-card: ${name(element)}`);
      }
    });
  }
  const intersecting = (left, right) => {
    const a = left.getBoundingClientRect(), b = right.getBoundingClientRect();
    return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
      && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
  };
  const groups = [
    ['.institution-rail-left', 'main.assessment', '.institution-rail-right'],
    ['main.assessment > aside', 'main.assessment > .question'],
    ['.question-meta', '.question > h1, .question > .visual-question, .question > .question-media', '.answers', '.feedback', '.question footer'],
  ];
  groups.forEach(selectors => {
    const elements = selectors.map(selector => document.querySelector(selector)).filter(visible);
    elements.forEach((left, index) => elements.slice(index + 1).forEach(right => {
      if (intersecting(left, right)) findings.push(`intersection: ${name(left)} with ${name(right)}`);
    }));
  });
  return [...new Set(findings)];
}
"""


def _capture_fields(page: Any, state: str, viewport: str, prefix: Path) -> dict[str, Path]:
    screenshots: dict[str, Path] = {}
    if state == "unanswered":
        prompt = page.locator(
            ".question > h1, .question > .visual-question, .question > .question-media"
        ).first
        prompt_path = prefix.with_name(prefix.name + "-question.png")
        prompt.screenshot(path=str(prompt_path))
        screenshots[f"{state}.{viewport}.question"] = prompt_path
        answers = page.locator(".answers button")
        for index in range(answers.count()):
            label = answers.nth(index).get_attribute("data-answer") or str(index + 1)
            option_path = prefix.with_name(prefix.name + f"-option-{label}.png")
            answers.nth(index).screenshot(path=str(option_path))
            screenshots[f"{state}.{viewport}.option-{label}"] = option_path
    else:
        feedback_path = prefix.with_name(prefix.name + "-solution.png")
        page.locator(".feedback").screenshot(path=str(feedback_path))
        screenshots[f"{state}.{viewport}.solution"] = feedback_path
    return screenshots


def _frontend_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative in ("static/app.js", "static/styles.css", "static/branding.css", "static/math.css"):
        digest.update((WORKSPACE_ROOT / relative).read_bytes())
    return digest.hexdigest()


def _screenshot_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}


def render_candidate(
    candidate: CandidateRecord | Mapping[str, Any],
    assets: Mapping[str, Any] | Path,
    viewports: Sequence[tuple[int, int]],
    output_dir: Path,
) -> RenderArtifacts:
    """Render both validation states through the real FastAPI-served KSAT frontend."""
    validated_viewports = _validated_viewports(viewports)
    if not isinstance(assets, (Mapping, Path)):
        raise TypeError("assets must be a mapping or local asset directory Path.")
    unanswered_payload = _attempt_payload(candidate, assets, submitted=False)
    submitted_payload = _attempt_payload(candidate, assets, submitted=True)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    port = _free_local_port()
    url = f"http://127.0.0.1:{port}/"
    temporary_data_path = Path()
    process: subprocess.Popen[Any] | None = None
    browser_runtime = ""
    question_screenshots: dict[str, Path] = {}
    solution_screenshots: dict[str, Path] = {}
    field_screenshots: dict[str, Path] = {}
    findings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ksat-render-data-") as temporary_data:
        temporary_data_path = Path(temporary_data)
        log_path = temporary_data_path / "uvicorn.log"
        environment = os.environ.copy()
        environment["KSAT_DATA_DIR"] = str(temporary_data_path)
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with log_path.open("w", encoding="utf-8") as server_log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=WORKSPACE_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
            try:
                _wait_for_server(process, url, log_path)
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as error:
                    raise RuntimeError(
                        "Python Playwright is unavailable. Install data-engineering/requirements.txt, then run "
                        f"{CHROMIUM_INSTALL_COMMAND} if Chromium is needed."
                    ) from error
                with sync_playwright() as playwright:
                    browser, selected_browser = _launch_browser(playwright)
                    try:
                        version = " ".join(str(browser.version).split()).casefold()
                        if not version:
                            raise RuntimeError("Selected screenshot browser did not report its actual version identity.")
                        engine = "microsoft-edge" if selected_browser.startswith("Microsoft Edge") else "playwright-chromium"
                        browser_runtime = f"{engine}:{version}"
                        for width, height in validated_viewports:
                            viewport = _viewport_label((width, height))
                            context = browser.new_context(viewport={"width": width, "height": height})
                            try:
                                page = context.new_page()
                                page.goto(url, wait_until="domcontentloaded")
                                page.wait_for_function("typeof window.renderAttemptForValidation === 'function'")
                                for state, payload in (("unanswered", unanswered_payload), ("submitted", submitted_payload)):
                                    page.evaluate(
                                        "payload => window.renderAttemptForValidation(payload, 0)",
                                        payload,
                                    )
                                    page.evaluate(_WAIT_FOR_VISUALS)
                                    for finding in page.evaluate(_MECHANICAL_FINDINGS):
                                        findings.append(f"{state}.{viewport}: {finding}")
                                    prefix = output / f"{viewport}-{state}"
                                    card_path = prefix.with_name(prefix.name + "-card.png")
                                    page.locator(".question").screenshot(path=str(card_path))
                                    if state == "unanswered":
                                        question_screenshots[viewport] = card_path
                                    else:
                                        solution_screenshots[viewport] = card_path
                                    field_screenshots.update(_capture_fields(page, state, viewport, prefix))
                            finally:
                                context.close()
                    finally:
                        browser.close()
            finally:
                _stop_server(process)

    screenshot_paths = {
        **{f"question.{key}": value for key, value in question_screenshots.items()},
        **{f"solution.{key}": value for key, value in solution_screenshots.items()},
        **{f"field.{key}": value for key, value in field_screenshots.items()},
    }
    playwright_version = importlib.metadata.version("playwright")
    return RenderArtifacts(
        question_screenshots=question_screenshots,
        solution_screenshots=solution_screenshots,
        screenshot_hashes=_screenshot_hashes(screenshot_paths),
        findings=tuple(dict.fromkeys(findings)),
        renderer_version=f"ksat-real-frontend-v1:{_frontend_fingerprint()}:{playwright_version}:{browser_runtime}",
        field_screenshots=field_screenshots,
        browser_runtime=browser_runtime,
        server_port=port,
        temporary_data_dir=temporary_data_path,
    )


__all__ = [
    "CHROMIUM_INSTALL_COMMAND",
    "RenderArtifacts",
    "ScreenshotArtifact",
    "render_candidate",
]
