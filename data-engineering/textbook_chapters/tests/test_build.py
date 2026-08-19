from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILD_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "build.py"
SPEC = importlib.util.spec_from_file_location("textbook_chapters_build", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_PATH}")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


@unittest.skipUnless(os.environ.get("APTITUDE_SOURCE_PDF"), "APTITUDE_SOURCE_PDF is required")
class Chapter01BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.pdf_path = Path(os.environ["APTITUDE_SOURCE_PDF"])
        cls.output = Path(cls.temp_directory.name) / "chapter01.zip"
        cls.summary = BUILD.build_package(
            pdf_path=cls.pdf_path,
            source_bank_path=BUILD.DEFAULT_SOURCE_BANK,
            review_path=BUILD.DEFAULT_REVIEW,
            output_path=cls.output,
        )
        cls.package_bytes = cls.output.read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()

    def package_records(self) -> tuple[dict, list[dict], list[dict], dict]:
        with zipfile.ZipFile(io.BytesIO(self.package_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            questions = [
                json.loads(line)
                for line in archive.read(manifest["question_files"][0]).decode("utf-8").splitlines()
                if line.strip()
            ]
            rejected = [
                json.loads(line)
                for line in archive.read("metadata/rejected-questions.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
            lineage = json.loads(archive.read("metadata/lineage.json"))
        return manifest, questions, rejected, lineage

    def test_audited_totals(self) -> None:
        self.assertEqual(
            self.summary,
            {"source_records": 380, "published_questions": 361, "rejected_questions": 19, "stimuli": 0},
        )

    def test_every_answer_matches_the_textbook_key(self) -> None:
        _, questions, _, _ = self.package_records()
        answers = BUILD.parse_answer_key(self.pdf_path, [40, 41], 380)
        for question in questions:
            self.assertEqual(question["correct_answer"], answers[question["source_question_number"]])
            self.assertIn(question["correct_answer"], question["options"])

    def test_every_solution_has_numbered_textbook_provenance(self) -> None:
        _, questions, _, _ = self.package_records()
        for question in questions:
            self.assertTrue(question["solution_steps"])
            self.assertEqual(question["explanation"], question["solution_steps"][-1])
            self.assertEqual(question["option_explanations"], {})
            self.assertTrue(question["solution_source"]["source_pages"])
            self.assertEqual(
                question["solution_source"]["source_question_number"],
                question["source_question_number"],
            )

    def test_source_omissions_are_recovered(self) -> None:
        _, questions, _, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        self.assertEqual(by_number[142]["correct_answer"], "C")
        self.assertEqual(by_number[142]["question_text"], "If a + b + c = 0, then (a + b)(b + c)(c + a) equals")
        self.assertEqual(by_number[374]["correct_answer"], "D")
        self.assertIn("Statement II", by_number[374]["question_text"])

    def test_vision_repairs_are_present(self) -> None:
        _, questions, _, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        self.assertIn("π", by_number[13]["question_text"])
        self.assertIn("√2", by_number[14]["question_text"])
        self.assertEqual(by_number[137]["correct_answer"], "A")
        self.assertEqual(by_number[137]["solution_steps"][-1], "768 + 232 = 1000.")
        self.assertEqual(by_number[209]["solution_steps"][-1], "Hence, x = 4.")
        self.assertIn("1! + 2!", by_number[344]["question_text"])

    def test_mismatched_or_unresolved_records_are_explicitly_rejected(self) -> None:
        _, questions, rejected, _ = self.package_records()
        published_numbers = {question["source_question_number"] for question in questions}
        rejected_by_number = {record["source_question_number"]: record for record in rejected}
        expected_mismatches = {368, 369, 370, 371, 377, 378, 380}
        self.assertTrue(expected_mismatches.isdisjoint(published_numbers))
        for number in expected_mismatches:
            self.assertEqual(rejected_by_number[number]["reason"], "textbook_solution_mismatch")
        self.assertEqual(rejected_by_number[365]["reason"], "textbook_solution_missing")
        self.assertEqual(rejected_by_number[366]["reason"], "textbook_solution_incomplete")

    def test_question_first_image_review_is_recorded(self) -> None:
        manifest, questions, _, lineage = self.package_records()
        self.assertEqual(manifest["stimuli"], [])
        self.assertEqual(lineage["vision_reviewed_question_pages"], list(range(23, 41)))
        self.assertTrue(
            all(question["image_association"]["status"] == "no_standalone_visual" for question in questions)
        )

    def test_package_matches_application_contract(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["format_version"], 2)
        self.assertTrue(manifest["bank_name"])
        self.assertEqual(len(questions), 361)
        self.assertEqual(manifest["total_questions"], len(questions))

    def test_pipeline_is_not_an_application_dependency(self) -> None:
        app_source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        app_requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("textbook_chapters", app_source)
        self.assertNotIn("pdfplumber", app_requirements)
        self.assertNotIn("pypdf", app_requirements.lower())


if __name__ == "__main__":
    unittest.main()
