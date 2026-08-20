from __future__ import annotations

import importlib.util
import io
import json
import os
from collections import Counter
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILD_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "build.py"
REVIEW_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "reviews" / "chapter-002.json"
SPEC = importlib.util.spec_from_file_location("textbook_chapters_build_ch02", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_PATH}")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


@unittest.skipUnless(os.environ.get("APTITUDE_SOURCE_PDF"), "APTITUDE_SOURCE_PDF is required")
class Chapter02BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.pdf_path = Path(os.environ["APTITUDE_SOURCE_PDF"])
        cls.output = Path(cls.temp_directory.name) / "chapter02.zip"
        cls.summary = BUILD.build_package(
            pdf_path=cls.pdf_path,
            source_bank_path=BUILD.DEFAULT_SOURCE_BANK,
            review_path=REVIEW_PATH,
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
            {"source_records": 130, "published_questions": 119, "rejected_questions": 11, "stimuli": 0},
        )

    def test_every_answer_matches_the_textbook_key(self) -> None:
        _, questions, _, _ = self.package_records()
        answers = BUILD.parse_answer_key(self.pdf_path, [71], 130)
        for question in questions:
            self.assertEqual(question["correct_answer"], answers[question["source_question_number"]])
            self.assertIn(question["correct_answer"], question["options"])

    def test_every_solution_has_textbook_provenance(self) -> None:
        _, questions, _, _ = self.package_records()
        for question in questions:
            self.assertTrue(question["solution_steps"])
            self.assertEqual(question["explanation"], question["solution_steps"][-1])
            self.assertEqual(question["option_explanations"], {})
            self.assertTrue(question["solution_source"]["source_pages"])

    def test_reviewed_repairs_are_published(self) -> None:
        _, questions, _, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        self.assertEqual(by_number[8]["solution_steps"][-1], "Dividing numerator and denominator by 38,896,717 gives 3/11.")
        self.assertEqual(by_number[64]["correct_answer"], "A")
        self.assertEqual(by_number[64]["solution_steps"][-1], "Since one number lies between 200 and 300, the numbers are 273 and 357.")
        self.assertEqual(by_number[74]["solution_steps"][-1], "The largest square-tile side is H.C.F.(378, 525) = 21 cm.")
        self.assertEqual(by_number[91]["solution_steps"][-1], "Therefore, the greatest divisible four-digit number is 9999 − 399 = 9600.")
        self.assertIn("ratio of two numbers is 3:4", by_number[128]["question_text"])

    def test_all_question_pages_have_question_first_vision_review(self) -> None:
        manifest, questions, rejected, lineage = self.package_records()
        self.assertEqual(manifest["stimuli"], [])
        self.assertEqual(len(rejected), 11)
        self.assertTrue(all(record["reason"] == "unresolved_pdf_layout_artifact" for record in rejected))
        self.assertEqual(lineage["vision_reviewed_question_pages"], list(range(64, 71)))
        self.assertTrue(
            all(question["image_association"]["status"] == "no_standalone_visual" for question in questions)
        )

    def test_package_matches_application_contract(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["format_version"], 2)
        self.assertEqual(manifest["chapter"], 2)
        self.assertEqual(len(questions), 119)
        self.assertEqual(manifest["total_questions"], len(questions))

    def test_every_published_record_is_readable_and_graded(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["difficulty_policy"], BUILD.DIFFICULTY_RUBRIC_VERSION)
        self.assertFalse(any(BUILD.unresolved_layout_issues(question) for question in questions))
        self.assertEqual(
            Counter(question["difficulty"] for question in questions),
            {"Easy": 76, "Medium": 39, "Hard": 4},
        )


if __name__ == "__main__":
    unittest.main()
