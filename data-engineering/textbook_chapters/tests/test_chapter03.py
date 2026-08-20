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
REVIEW_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "reviews" / "chapter-003.json"
SPEC = importlib.util.spec_from_file_location("textbook_chapters_build_ch03", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_PATH}")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


@unittest.skipUnless(os.environ.get("APTITUDE_SOURCE_PDF"), "APTITUDE_SOURCE_PDF is required")
class Chapter03BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.pdf_path = Path(os.environ["APTITUDE_SOURCE_PDF"])
        cls.output = Path(cls.temp_directory.name) / "chapter03.zip"
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
            {"source_records": 206, "published_questions": 90, "rejected_questions": 116, "stimuli": 0},
        )

    def test_every_answer_matches_the_textbook_key(self) -> None:
        _, questions, _, _ = self.package_records()
        answers = BUILD.parse_answer_key(self.pdf_path, [94], 206)
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

    def test_reviewed_alignment_repairs_are_published(self) -> None:
        _, questions, _, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        self.assertEqual(by_number[8]["solution_steps"][-1], "0.1 = 10 x 0.01, so 0.1 is 10 times 0.01.")
        self.assertEqual(by_number[68]["correct_answer"], "A")
        self.assertIn("368.39 divided by 17", by_number[68]["question_text"])
        self.assertEqual(by_number[194]["correct_answer"], "B")
        self.assertEqual(by_number[194]["solution_steps"][-1], "This is 35 + 24 x 16 = 419, whose closest listed value is 420.")
        self.assertEqual(by_number[205]["solution_steps"][-1], "Thus, 30% of 333 = 99.9.")
        self.assertEqual(by_number[206]["correct_answer"], "B")
        self.assertEqual(by_number[206]["solution_steps"][-1], "Therefore, 15/2 - 3 = 9/2 = 4 1/2.")

    def test_all_question_pages_have_question_first_vision_review(self) -> None:
        manifest, questions, rejected, lineage = self.package_records()
        self.assertEqual(manifest["stimuli"], [])
        self.assertEqual(len(rejected), 116)
        self.assertTrue(all(record["reason"] == "unresolved_pdf_layout_artifact" for record in rejected))
        self.assertEqual(lineage["vision_reviewed_question_pages"], list(range(83, 94)))
        self.assertTrue(
            all(question["image_association"]["status"] == "no_standalone_visual" for question in questions)
        )

    def test_package_matches_application_contract(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["format_version"], 2)
        self.assertEqual(manifest["chapter"], 3)
        self.assertEqual(len(questions), 90)
        self.assertEqual(manifest["total_questions"], len(questions))

    def test_every_published_record_is_readable_and_graded(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["difficulty_policy"], BUILD.DIFFICULTY_RUBRIC_VERSION)
        self.assertFalse(any(BUILD.unresolved_layout_issues(question) for question in questions))
        self.assertEqual(
            Counter(question["difficulty"] for question in questions),
            {"Easy": 79, "Medium": 10, "Hard": 1},
        )


if __name__ == "__main__":
    unittest.main()
