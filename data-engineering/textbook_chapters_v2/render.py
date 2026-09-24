"""Capture validation evidence from the real KSAT browser renderer."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
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
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .models import CandidateRecord, RenderArtifacts as PipelineRenderArtifacts
from .store import canonical_json


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
    package_sha256: str = ""
    imported_record_sha256: str = ""
    source_key: str = ""
    import_evidence_path: Path = Path()
    import_evidence_sha256: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "field_screenshots",
            MappingProxyType({str(key): Path(value) for key, value in self.field_screenshots.items()}),
        )
        object.__setattr__(self, "temporary_data_dir", Path(self.temporary_data_dir))
        object.__setattr__(self, "import_evidence_path", Path(self.import_evidence_path))

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


def _render_mapping(
    candidate: CandidateRecord | Mapping[str, Any],
    assets: Mapping[str, Any] | Path,
    viewports: Sequence[tuple[int, int]],
    output_dir: Path,
) -> RenderArtifacts:
    """Render an already imported mapping through the real KSAT frontend."""
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


_IMPORT_PACKAGE_SCRIPT = r"""
import hashlib
import io
import json
import sys
from pathlib import Path

import app


package_path = Path(sys.argv[1]).resolve()
source_key = sys.argv[2]
evidence_path = Path(sys.argv[3]).resolve()
app.ensure_schema()
package_bytes = package_path.read_bytes()
bank_name, questions, stimuli, format_version = app.parse_question_package(io.BytesIO(package_bytes))
saved = app.save_question_package(bank_name, questions, stimuli, package_path.name, format_version)
with app.db() as connection:
    rows = connection.execute(
        "SELECT q.*, b.bank_name FROM questions q JOIN question_banks b ON b.bank_id = q.bank_id WHERE q.source_key = ?",
        (source_key,),
    ).fetchall()
if len(rows) != 1:
    raise RuntimeError(f"Package import resolved {len(rows)} records for source key {source_key!r}; expected exactly one.")
row = rows[0]
stored_media = json.loads(row["display_media_json"] or "{}")
asset_dir = app.question_assets_dir() / str(row["bank_id"])


def media_item(item):
    return {
        "source_path": str(asset_dir / str(item["asset_filename"])),
        "alt_text": str(item["alt_text"]),
        "sha256": str(item["sha256"]),
    }


display_media = {}
if "question" in stored_media:
    display_media["question"] = media_item(stored_media["question"])
if "options" in stored_media:
    display_media["options"] = {key: media_item(value) for key, value in stored_media["options"].items()}
if "solution" in stored_media:
    display_media["solution"] = [media_item(value) for value in stored_media["solution"]]
record = {
    "key": source_key,
    "question_text": app.display_question_text(row["question_text"], source_key),
    "question_html": app.clean_display_text(row["question_html"] or ""),
    "category": row["category"],
    "chapter": row["chapter"],
    "difficulty": row["difficulty"],
    "options": app.question_options(row),
    "correct_answer": row["correct_answer"],
    "explanation": app.clean_display_text(row["explanation"] or ""),
    "solution_steps": app.display_solution_steps(row["question_text"], row["solution_steps"], source_key),
    "display_media": display_media,
}
encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
evidence_path.write_text(
    json.dumps(
        {
            "bank_id": saved["bank_id"],
            "bank_name": saved["bank_name"],
            "format_version": saved["format_version"],
            "source_key": source_key,
            "record": record,
            "imported_record_sha256": hashlib.sha256(encoded).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n",
    encoding="utf-8",
)
"""


def _import_packaged_record(package_path: Path, source_key: str, data_dir: Path) -> dict[str, Any]:
    evidence_path = data_dir / "import-evidence.json"
    environment = os.environ.copy()
    environment["KSAT_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PACKAGE_SCRIPT, str(package_path), source_key, str(evidence_path)],
        cwd=WORKSPACE_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"KSAT package import failed for source key {source_key}: {detail}")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("KSAT package import did not produce readable evidence.") from error
    if not isinstance(evidence, dict) or evidence.get("source_key") != source_key or not isinstance(evidence.get("record"), dict):
        raise RuntimeError("KSAT package import evidence does not match the requested source key.")
    expected_hash = hashlib.sha256(canonical_json(evidence["record"])).hexdigest()
    if evidence.get("imported_record_sha256") != expected_hash:
        raise RuntimeError("KSAT package import evidence has a stale imported-record hash.")
    return evidence


def render_imported_question(
    package_path: Path,
    source_key: str,
    output_dir: Path,
    viewports: tuple[tuple[int, int], ...],
) -> RenderArtifacts:
    """Import a package in isolation, resolve one persisted record, and render it in KSAT."""
    package = Path(package_path).resolve()
    if not package.is_file():
        raise FileNotFoundError(f"Question-bank package does not exist: {package}")
    if not isinstance(source_key, str) or not source_key:
        raise ValueError("source_key must be a non-empty string")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    package_sha256 = hashlib.sha256(package.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ksat-import-validation-") as temporary_data:
        evidence = _import_packaged_record(package, source_key, Path(temporary_data))
        rendered = _render_mapping(evidence["record"], {}, viewports, output)
    evidence_payload = {
        **evidence,
        "package_path": str(package),
        "package_sha256": package_sha256,
        "renderer_version": rendered.renderer_version,
        "screenshot_hashes": dict(rendered.screenshot_hashes),
    }
    evidence_path = output / "import-evidence.json"
    evidence_path.write_bytes(canonical_json(evidence_payload) + b"\n")
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    imported_hash = str(evidence["imported_record_sha256"])
    return RenderArtifacts(
        question_screenshots=rendered.question_screenshots,
        solution_screenshots=rendered.solution_screenshots,
        screenshot_hashes=rendered.screenshot_hashes,
        findings=rendered.findings,
        renderer_version=f"{rendered.renderer_version}:package={package_sha256}:imported={imported_hash}",
        field_screenshots=rendered.field_screenshots,
        browser_runtime=rendered.browser_runtime,
        server_port=rendered.server_port,
        temporary_data_dir=rendered.temporary_data_dir,
        package_sha256=package_sha256,
        imported_record_sha256=imported_hash,
        source_key=source_key,
        import_evidence_path=evidence_path,
        import_evidence_sha256=evidence_sha256,
    )


def _validation_media_item(
    raw: Any,
    assets: Mapping[str, Any] | Path,
    archive_assets: dict[str, bytes],
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ValueError("Validation display-media entries must be mappings.")
    content = _asset_bytes(raw, assets)
    _png_dimensions(content)
    digest = hashlib.sha256(content).hexdigest()
    declared = raw.get("sha256") or raw.get("source_sha256")
    if declared is not None and declared != digest:
        raise ValueError("Validation display-media SHA-256 does not match its local PNG.")
    alt_text = raw.get("alt_text")
    if not isinstance(alt_text, str) or not alt_text.strip():
        raise ValueError("Validation display media requires non-empty verified alt text.")
    member = f"assets/{digest}.png"
    archive_assets[member] = content
    return {"asset": member, "alt_text": alt_text.strip(), "sha256": digest}


def _validation_display_media(
    raw: Any,
    assets: Mapping[str, Any] | Path,
    archive_assets: dict[str, bytes],
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or set(raw) - {"question", "options", "solution"}:
        raise ValueError("Candidate display_media must contain only question, options, and solution fields.")
    result: dict[str, Any] = {}
    if "question" in raw:
        result["question"] = _validation_media_item(raw["question"], assets, archive_assets)
    if "options" in raw:
        options = raw["options"]
        if not isinstance(options, Mapping):
            raise ValueError("Candidate option display media must be a mapping.")
        result["options"] = {
            str(label): _validation_media_item(value, assets, archive_assets)
            for label, value in sorted(options.items())
        }
    if "solution" in raw:
        solution = raw["solution"]
        if not isinstance(solution, (list, tuple)) or not solution:
            raise ValueError("Candidate solution display media must be a non-empty list.")
        result["solution"] = [
            _validation_media_item(value, assets, archive_assets) for value in solution
        ]
    return result


def _write_zip_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    member.compress_type = zipfile.ZIP_DEFLATED
    member.external_attr = 0o100644 << 16
    archive.writestr(member, content)


def _write_validation_package(
    candidate: CandidateRecord | Mapping[str, Any],
    assets: Mapping[str, Any] | Path,
    destination: Path,
) -> str:
    record = _candidate_mapping(candidate)
    source_key = record.get("key")
    if not isinstance(source_key, str) or not source_key:
        raise ValueError("Candidate key must be a non-empty source identity.")
    archive_assets: dict[str, bytes] = {}
    package_record = {key: value for key, value in record.items() if key != "display_media"}
    media = _validation_display_media(record.get("display_media"), assets, archive_assets)
    if media:
        package_record["display_media"] = media
    manifest = {
        "format_version": 3,
        "bank_name": f"KSAT render validation: {source_key}",
        "question_files": ["questions/data.jsonl"],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        _write_zip_member(archive, "manifest.json", canonical_json(manifest))
        _write_zip_member(archive, "questions/data.jsonl", canonical_json(package_record) + b"\n")
        for name, content in sorted(archive_assets.items()):
            _write_zip_member(archive, name, content)
    return source_key


def render_candidate(
    candidate: CandidateRecord | Mapping[str, Any],
    assets: Mapping[str, Any] | Path,
    viewports: Sequence[tuple[int, int]],
    output_dir: Path,
) -> RenderArtifacts:
    """Package, import, resolve, and render one candidate through the real KSAT path."""
    with tempfile.TemporaryDirectory(prefix="ksat-validation-package-") as temporary:
        package = Path(temporary) / "candidate.zip"
        source_key = _write_validation_package(candidate, assets, package)
        return render_imported_question(package, source_key, Path(output_dir), tuple(viewports))


__all__ = [
    "CHROMIUM_INSTALL_COMMAND",
    "RenderArtifacts",
    "ScreenshotArtifact",
    "render_candidate",
    "render_imported_question",
]
