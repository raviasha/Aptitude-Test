from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = DATA_ENGINEERING_ROOT.parent
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


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
