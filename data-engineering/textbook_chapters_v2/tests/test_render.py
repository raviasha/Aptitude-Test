from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageChops, ImageDraw


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = DATA_ENGINEERING_ROOT.parent
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


class ServerShutdownTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows launcher process tree")
    def test_failed_tree_shutdown_raises_even_if_launcher_exits(self) -> None:
        from textbook_chapters_v2.render import _stop_server

        process = Mock(spec=subprocess.Popen)
        process.pid = 12345
        process.poll.return_value = None

        def failed_tree_shutdown(*args, **kwargs):
            # The launcher exits during taskkill, but a child could survive.
            process.poll.return_value = 0
            return subprocess.CompletedProcess(args[0], 1, "", "owned child termination failed")

        with patch("textbook_chapters_v2.render.subprocess.run", side_effect=failed_tree_shutdown):
            with self.assertRaisesRegex(RuntimeError, "owned child termination failed"):
                _stop_server(process)

    @unittest.skipUnless(os.name == "nt", "Windows launcher process tree")
    def test_shutdown_stops_the_owned_child_before_cleanup(self) -> None:
        from textbook_chapters_v2.render import _stop_server

        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            child_code = (
                "import socket,time,json,os; from pathlib import Path; "
                "sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(); "
                f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(),sock.getsockname()[1]])); "
                "time.sleep(60)"
            )
            parent_code = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); time.sleep(60)"
            )
            process = subprocess.Popen([sys.executable, "-c", parent_code])
            child_pid = None
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "owned child did not become ready")
                child_pid, port = json.loads(ready.read_text())
                _stop_server(process)
                with socket.socket() as probe:
                    probe.settimeout(0.25)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0,
                                        "owned child still accepts connections after shutdown")
            finally:
                for pid in (child_pid, process.pid):
                    if pid is not None:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       capture_output=True)
                process.wait(timeout=10)


class AppDataDirectoryTests(unittest.TestCase):
    def _imported_data_dir(
        self,
        *,
        configured: Path | None,
        frozen: bool = False,
        program_data: Path | None = None,
    ) -> Path:
        code = (
            "import json, sys; "
            + ("sys.frozen = True; " if frozen else "")
            + "import app; print(json.dumps(str(app.DATA_DIR)))"
        )
        environment = os.environ.copy()
        environment.pop("KSAT_DATA_DIR", None)
        if configured is not None:
            environment["KSAT_DATA_DIR"] = str(configured)
        if program_data is not None:
            environment["PROGRAMDATA"] = str(program_data)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=WORKSPACE_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return Path(json.loads(result.stdout.strip()))

    def test_explicit_data_directory_overrides_development_and_frozen_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            configured = root / "configured" / ".." / "validation-data"
            program_data = root / "program-data"

            self.assertEqual(
                self._imported_data_dir(configured=configured),
                configured.resolve(),
            )
            self.assertEqual(
                self._imported_data_dir(
                    configured=configured,
                    frozen=True,
                    program_data=program_data,
                ),
                configured.resolve(),
            )

    def test_unset_data_directory_preserves_development_and_frozen_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            program_data = Path(temporary_directory) / "program-data"

            self.assertEqual(
                self._imported_data_dir(configured=None),
                WORKSPACE_ROOT / "data",
            )
            self.assertEqual(
                self._imported_data_dir(
                    configured=None,
                    frozen=True,
                    program_data=program_data,
                ),
                program_data / "KSAT Coordinator",
            )


class RenderDataCleanupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows sharing violation")
    def test_persistent_lock_still_raises_at_deadline(self) -> None:
        from textbook_chapters_v2 import render

        original_directory = tempfile.TemporaryDirectory
        with original_directory() as parent:
            def owned_directory(**kwargs):
                return original_directory(dir=parent, **kwargs)

            handle = None
            try:
                with patch.object(render.tempfile, "TemporaryDirectory", side_effect=owned_directory), \
                     patch.object(render.time, "monotonic", side_effect=[0, 6]):
                    with self.assertRaises(PermissionError) as caught:
                        with render._render_data_directory() as directory:
                            handle = (Path(directory) / ".coordinator.lock").open("wb")
                self.assertEqual(caught.exception.winerror, 32)
                self.assertTrue(Path(directory).exists(), "Persistent locks must not be ignored")
            finally:
                if handle is not None:
                    handle.close()

    def test_non_sharing_cleanup_error_is_not_suppressed(self) -> None:
        from textbook_chapters_v2 import render

        with tempfile.TemporaryDirectory() as directory:
            temporary = Mock(name=directory)
            temporary.name = directory
            error = PermissionError("access denied, not a sharing violation")
            error.winerror = 5
            temporary.cleanup.side_effect = error
            with patch.object(render.tempfile, "TemporaryDirectory", return_value=temporary):
                with self.assertRaisesRegex(PermissionError, "access denied"):
                    with render._render_data_directory():
                        pass

    def test_capture_failure_is_not_promoted_to_success(self) -> None:
        from textbook_chapters_v2.render import _render_data_directory

        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            with _render_data_directory() as directory:
                raise RuntimeError("capture failed")
        self.assertFalse(Path(directory).exists())


class FieldCaptureTests(unittest.TestCase):
    def test_tall_fields_match_unobscured_pixels_at_actual_viewports(self) -> None:
        from playwright.sync_api import sync_playwright
        from textbook_chapters_v2.render import _capture_fields, _launch_browser

        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            browser, _ = _launch_browser(playwright)
            try:
                for width, height in ((1024, 768), (1600, 900)):
                    with self.subTest(viewport=(width, height)):
                        context = browser.new_context(viewport={"width": width, "height": height})
                        try:
                            page = context.new_page()
                            page.set_content("""
                                <style>
                                body { margin: 0; }
                                .question { width: 500px; margin: 100px 40px; }
                                .question-meta { position: sticky; top: 0; height: 64px;
                                    background: rgb(255,0,0); z-index: 10; }
                                h1, .feedback { margin: 0; height: 1100px;
                                    background: linear-gradient(#00ff00 0 80px, white 80px 1000px, #0000ff 1000px); }
                                button { display: block; width: 500px; height: 80px; }
                                .viewport-marker { height: 10vh; background: yellow; }
                                </style>
                                <div class="question">
                                  <div class="question-meta">Answer saved automatically</div>
                                  <h1>FIRST LINE<div class="viewport-marker"></div>Complete question</h1>
                                  <div class="answers"><button data-answer="A">Option A</button>
                                  <button data-answer="B">Option B</button></div>
                                  <div class="feedback">FIRST SOLUTION LINE<div class="viewport-marker"></div>Complete solution</div>
                                </div>
                            """)
                            page.evaluate("window.scrollTo(0, 0)")
                            selectors = {
                                "unanswered.card": ".question",
                                "submitted.card": ".question",
                                "unanswered.question": ".question > h1",
                                "unanswered.option-A": '[data-answer="A"]',
                                "unanswered.option-B": '[data-answer="B"]',
                                "submitted.solution": ".feedback",
                            }
                            bounds = {key: page.locator(selector).bounding_box() for key, selector in selectors.items()}
                            with Image.open(io.BytesIO(page.screenshot(full_page=True))) as source:
                                reference = source.convert("RGB")
                            before = page.evaluate("({width: innerWidth, height: innerHeight, html: document.documentElement.outerHTML})")
                            # Reproduce the production order: card capture precedes fields.
                            page.locator(".question").screenshot()
                            paths = {}
                            for state in ("unanswered", "submitted"):
                                prefix = Path(directory) / f"{width}-{state}"
                                card_path = prefix.with_name(prefix.name + "-card.png")
                                paths.update(_capture_fields(page, state, f"{width}x{height}", prefix, card_path=card_path))
                                paths[f"{state}.{width}x{height}.card"] = card_path
                            after = page.evaluate("({width: innerWidth, height: innerHeight, html: document.documentElement.outerHTML})")
                            self.assertEqual(after, before, "Capture must not modify viewport, DOM, or styles")
                            for key, box in bounds.items():
                                state, field = key.split(".", 1)
                                # Fixture coordinates are integers, independent of capture implementation.
                                expected = reference.crop((int(box["x"]), int(box["y"]),
                                    int(box["x"] + box["width"]), int(box["y"] + box["height"])))
                                with Image.open(paths[f"{state}.{width}x{height}.{field}"]) as actual:
                                    self.assertEqual(actual.size, expected.size, key)
                                    self.assertIsNone(ImageChops.difference(actual.convert("RGB"), expected).getbbox(),
                                        f"{key}: field pixels clipped or covered by sticky header")
                                    if field in ("question", "solution"):
                                        # Independent viewport-unit check: full-page capture must not
                                        # relayout 10vh using the tall document height.
                                        rgb = actual.convert("RGB")
                                        yellow_rows = sum(rgb.getpixel((300, y)) == (255, 255, 0)
                                                          for y in range(rgb.height))
                                        self.assertAlmostEqual(yellow_rows, height / 10, delta=1)
                        finally:
                            context.close()
            finally:
                browser.close()


class RealRendererTests(unittest.TestCase):
    viewports = ((1024, 768), (1600, 900))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _png(self, name: str, size: tuple[int, int], label: str) -> Path:
        path = self.root / name
        image = Image.new("RGB", size, "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 3, size[0] - 4, size[1] - 4), outline="#3159b8", width=5)
        draw.text((18, 18), label, fill="#17243f")
        image.save(path, format="PNG")
        return path

    def _package(self, question: dict[str, object], name: str = "bank.zip") -> Path:
        path = self.root / name
        manifest = {
            "format_version": 3,
            "bank_name": f"Validation {name}",
            "question_files": ["questions/data.jsonl"],
        }
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("questions/data.jsonl", json.dumps(question, ensure_ascii=False) + "\n")
        return path

    @staticmethod
    def _candidate(**changes: object) -> dict[str, object]:
        candidate: dict[str, object] = {
            "key": "ch01-q0334",
            "question_text": "The remainder when 7⁸⁴ is divided by 342 is",
            "category": "Quantitative Aptitude",
            "chapter": "Number System",
            "difficulty": "Medium",
            "options": {"A": "0", "B": "1", "C": "49", "D": "341"},
            "correct_answer": "B",
            "explanation": "Since 7³ = 343, the power is congruent to one.",
            "solution_steps": [
                "7⁸⁴ = (7³)²⁸ = 343²⁸.",
                "Therefore, the remainder is 1.",
            ],
        }
        candidate.update(changes)
        return candidate

    @staticmethod
    def _asset_item(path: Path, alt_text: str) -> tuple[dict[str, str], tuple[str, Path]]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        asset = f"assets/{digest}.png"
        return (
            {"asset": asset, "alt_text": alt_text, "sha256": digest},
            (asset, path),
        )

    @staticmethod
    def _assert_server_stopped(port: int) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            self_result = probe.connect_ex(("127.0.0.1", port))
        if self_result == 0:
            raise AssertionError(f"renderer server still accepts connections on port {port}")

    def test_text_candidate_captures_both_real_frontend_states_and_cleans_up(self) -> None:
        from textbook_chapters_v2.render import render_candidate

        output_dir = self.root / "text-render"
        artifacts = render_candidate(self._candidate(), {}, self.viewports, output_dir)

        self.assertEqual(len(artifacts.unanswered), 2)
        self.assertEqual(len(artifacts.submitted), 2)
        self.assertTrue(all(item.path.is_file() for item in artifacts.all_screenshots()))
        self.assertGreaterEqual(len(artifacts.field_screenshots), 12)
        self.assertEqual(artifacts.clipping_findings, [])
        self.assertRegex(artifacts.package_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(artifacts.imported_record_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(artifacts.import_evidence_path.is_file())
        self.assertEqual(
            artifacts.import_evidence_sha256,
            hashlib.sha256(artifacts.import_evidence_path.read_bytes()).hexdigest(),
        )
        imported = json.loads(artifacts.import_evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(imported["source_key"], "ch01-q0334")
        self.assertEqual(imported["record"]["question_text"], self._candidate()["question_text"])
        self.assertFalse(artifacts.temporary_data_dir.exists())
        self._assert_server_stopped(artifacts.server_port)

    @unittest.skipUnless(os.name == "nt", "Windows delayed handle release")
    def test_render_waits_for_delayed_owned_data_lock_release(self) -> None:
        from textbook_chapters_v2 import render

        original_directory = tempfile.TemporaryDirectory
        original_stop = render._stop_server
        owned_directories = []
        timers = []

        def owned_directory(*args, **kwargs):
            if kwargs.get("prefix") == "ksat-render-data-":
                kwargs["dir"] = self.root
                temporary = original_directory(*args, **kwargs)
                owned_directories.append(Path(temporary.name))
                return temporary
            return original_directory(*args, **kwargs)

        def stop_with_delayed_handle_release(process):
            original_stop(process)
            # Model a child handle still closing after successful taskkill.
            # This is a real Windows sharing violation, only in our owned directory.
            handle = (owned_directories[-1] / ".coordinator.lock").open("rb")
            timer = threading.Timer(0.5, handle.close)
            timers.append(timer)
            timer.start()

        try:
            with patch.object(render.tempfile, "TemporaryDirectory", side_effect=owned_directory), \
                 patch.object(render, "_stop_server", side_effect=stop_with_delayed_handle_release):
                artifacts = render.render_candidate(self._candidate(), {}, ((1024, 768),), self.root / "delayed-release")
            self.assertFalse(artifacts.temporary_data_dir.exists())
            self._assert_server_stopped(artifacts.server_port)
        finally:
            for timer in timers:
                timer.join()

    def test_media_candidate_decodes_tall_solution_without_vertical_clipping(self) -> None:
        from textbook_chapters_v2.render import render_candidate

        question_path = self._png("question.png", (720, 260), "Question field")
        option_path = self._png("option-d.png", (420, 120), "Option D")
        solution_path = self._png("solution-tall.png", (520, 1800), "Tall worked solution")
        question_item, question_asset = self._asset_item(question_path, "Rendered question")
        option_item, option_asset = self._asset_item(option_path, "Option D is 341")
        solution_item, solution_asset = self._asset_item(solution_path, "Complete worked solution")
        candidate = self._candidate(
            display_media={
                "question": question_item,
                "options": {"D": option_item},
                "solution": [solution_item],
            }
        )
        assets = dict((question_asset, option_asset, solution_asset))

        artifacts = render_candidate(candidate, assets, self.viewports, self.root / "media-render")

        self.assertEqual(len(artifacts.unanswered), 2)
        self.assertEqual(len(artifacts.submitted), 2)
        self.assertTrue(all(item.path.is_file() for item in artifacts.all_screenshots()))
        self.assertEqual(artifacts.clipping_findings, [])
        submitted_heights: list[int] = []
        for item in artifacts.submitted:
            with Image.open(item.path) as screenshot:
                submitted_heights.append(screenshot.height)
        self.assertTrue(
            all(
                screenshot_height > viewport_height
                for screenshot_height, (_, viewport_height) in zip(submitted_heights, self.viewports)
            )
        )
        self.assertTrue(
            artifacts.browser_runtime.startswith(("microsoft-edge:", "playwright-chromium:"))
        )
        self.assertFalse(artifacts.temporary_data_dir.exists())
        self._assert_server_stopped(artifacts.server_port)

    def test_branding_rails_leave_diagram_solutions_readable_at_small_desktop_width(self) -> None:
        from textbook_chapters_v2.render import render_candidate

        # A 650px source diagram must not shrink to ~250px behind branding rails.
        # Exercise the real imported bank, assessment DOM and production CSS.
        diagram = self._png("wide-diagram.png", (650, 450), "Diagram solution")
        media, asset = self._asset_item(diagram, "Complete diagram solution")
        candidate = self._candidate(display_media={"solution": [media]})
        artifacts = render_candidate(
            candidate, dict([asset]), ((1024, 768), (1600, 900), (390, 844)),
            self.root / "responsive-diagrams",
        )
        self.assertEqual(artifacts.clipping_findings, [])
        for viewport, minimum_width in (("1024x768", 480), ("1600x900", 480), ("390x844", 250)):
            with self.subTest(viewport=viewport):
                path = artifacts.field_screenshots[f"submitted.{viewport}.solution"]
                with Image.open(path).convert("RGB") as screenshot:
                    # Exact fixture-blue pixels locate the displayed image, excluding card padding.
                    mask = Image.eval(screenshot.getchannel("R"), lambda v: 255 if v == 49 else 0)
                    mask = ImageChops.multiply(mask, Image.eval(screenshot.getchannel("G"), lambda v: 255 if v == 89 else 0))
                    mask = ImageChops.multiply(mask, Image.eval(screenshot.getchannel("B"), lambda v: 255 if v == 184 else 0))
                    bounds = mask.getbbox()
                    self.assertIsNotNone(bounds, "Diagram border must be visible")
                    self.assertGreaterEqual(bounds[2] - bounds[0], minimum_width)
        self.assertFalse(artifacts.temporary_data_dir.exists())
        self._assert_server_stopped(artifacts.server_port)

    def test_imported_renderer_binds_screenshots_to_the_packaged_database_record(self) -> None:
        from textbook_chapters_v2.render import render_imported_question

        packaged = self._candidate(
            key="ch01-q0124",
            question_text="PACKAGED 112 × 5⁴",
            options={"A": "6700", "B": "70000", "C": "76500", "D": "77200"},
            correct_answer="B",
            solution_steps=["112 × 5⁴ = 70000"],
        )
        package = self._package(packaged)

        artifacts = render_imported_question(
            package, "ch01-q0124", self.root / "imported-render", ((1024, 768),)
        )

        evidence = json.loads(artifacts.import_evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["record"]["question_text"], "PACKAGED 112 × 5⁴")
        self.assertEqual(evidence["record"]["options"]["B"], "70000")
        self.assertEqual(evidence["record"]["correct_answer"], "B")
        self.assertEqual(artifacts.source_key, "ch01-q0124")
        self.assertEqual(artifacts.package_sha256, hashlib.sha256(package.read_bytes()).hexdigest())
        self.assertEqual(artifacts.imported_record_sha256, evidence["imported_record_sha256"])
        self.assertIn(artifacts.imported_record_sha256, artifacts.renderer_version)
        self.assertTrue(all(item.path.is_file() for item in artifacts.all_screenshots()))

    def test_imported_renderer_refuses_a_source_key_absent_from_the_package(self) -> None:
        from textbook_chapters_v2.render import render_imported_question

        package = self._package(self._candidate())

        with self.assertRaisesRegex(RuntimeError, "source key"):
            render_imported_question(
                package, "ch01-q0124", self.root / "missing-source-key", ((1024, 768),)
            )

        self.assertEqual(list((self.root / "missing-source-key").glob("*.png")), [])

    def test_browser_launcher_fails_closed_when_no_runtime_is_available(self) -> None:
        from textbook_chapters_v2.render import _launch_browser

        playwright = Mock()
        playwright.chromium.launch.side_effect = RuntimeError("runtime unavailable")
        with patch("textbook_chapters_v2.render._edge_executable", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "No usable Microsoft Edge or Playwright Chromium"):
                _launch_browser(playwright)


if __name__ == "__main__":
    unittest.main()
