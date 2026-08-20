from __future__ import annotations

import importlib.util
import io
import json
import os
import re
from collections import Counter
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILD_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "build.py"
REVIEW_PATH = PROJECT_ROOT / "data-engineering" / "textbook_chapters" / "reviews" / "chapter-004.json"
SPEC = importlib.util.spec_from_file_location("textbook_chapters_build_ch04", BUILD_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load {BUILD_PATH}")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


@unittest.skipUnless(os.environ.get("APTITUDE_SOURCE_PDF"), "APTITUDE_SOURCE_PDF is required")
class Chapter04BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        cls.pdf_path = Path(os.environ["APTITUDE_SOURCE_PDF"])
        cls.output = Path(cls.temp_directory.name) / "chapter04.zip"
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
            {"source_records": 545, "published_questions": 291, "rejected_questions": 254, "stimuli": 0},
        )

    def test_every_answer_matches_the_reviewed_textbook_answer(self) -> None:
        _, questions, _, _ = self.package_records()
        answers = BUILD.parse_answer_key(self.pdf_path, [152, 153], 545, overrides={11: "B", 545: "D"})
        for question in questions:
            self.assertEqual(question["correct_answer"], answers[question["source_question_number"]])
            self.assertIn(question["correct_answer"], question["options"])

        by_number = {question["source_question_number"]: question for question in questions}
        self.assertEqual(by_number[545]["correct_answer"], "D")
        self.assertEqual(
            by_number[545]["answer_source"]["policy"],
            "textbook-numbered-solution-reviewed-override",
        )
        self.assertEqual(by_number[545]["answer_source"]["source_pages"], [153, 188])

    def test_every_solution_has_textbook_provenance_without_spill(self) -> None:
        _, questions, _, _ = self.package_records()
        for question in questions:
            self.assertTrue(question["solution_steps"])
            self.assertEqual(question["explanation"], question["solution_steps"][-1])
            self.assertEqual(question["option_explanations"], {})
            self.assertTrue(question["solution_source"]["source_pages"])
            self.assertIsNone(re.search(r"\bQuestions?\s+\d+", " ".join(question["solution_steps"]), re.I))

    def test_source_only_and_alignment_repairs_are_published(self) -> None:
        _, questions, rejected, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        rejected_numbers = {record["source_question_number"] for record in rejected}
        self.assertEqual(set(by_number) | rejected_numbers, set(range(1, 546)))
        self.assertFalse(set(by_number) & rejected_numbers)
        self.assertEqual(by_number[94]["correct_answer"], "C")
        self.assertEqual(by_number[544]["correct_answer"], "B")
        self.assertEqual(by_number[545]["solution_steps"][-1], "Therefore, 0.73 + 0.27 = 1.")
        self.assertTrue(by_number[137]["solution_steps"][-1].endswith("Rs. 68."))
        self.assertEqual(by_number[138]["solution_steps"][-1], "The amount received by each person is Rs. 436,563 / 69 = Rs. 6,327.")
        self.assertEqual(by_number[226]["solution_steps"], ["Among the given numbers, only 60489 is a multiple of 423."])
        self.assertEqual(by_number[227]["solution_steps"][-1], "Therefore, x = 770 x 72 / 35 = 1584.")
        self.assertEqual(by_number[423]["solution_steps"][-1], "Therefore, the required difference is 9.4 - 2.8 = 6.6.")
        self.assertEqual(by_number[543]["correct_answer"], "A")
        self.assertEqual(by_number[164]["question_text"], "If 2 = x + 1 / (1 + 1 / (3 + 1/4)), what is x?")
        self.assertEqual(by_number[164]["correct_answer"], "D")
        self.assertEqual(by_number[165]["question_text"], "If [2 + 1 / (3 4/5)] / [2 + 1 / (3 + 1 / (1 + 1/4))] = x, what is x?")
        self.assertEqual(by_number[165]["correct_answer"], "C")

    def test_shared_directions_and_solution_setups_are_self_contained(self) -> None:
        _, questions, _, _ = self.package_records()
        by_number = {question["source_question_number"]: question for question in questions}
        for number in [311, 312, 313, 451, 452, 462, 463, 464, 465, 466, 471, 472, 473, 474, 475, 505, 506, 511, 512, 513, 519, 520, 533]:
            self.assertIn("textbook", by_number[number]["question_text"].lower())
        self.assertIn("x + y + z = 35", " ".join(by_number[451]["solution_steps"]))
        self.assertIn("all three games is 2", " ".join(by_number[462]["solution_steps"]))
        self.assertIn("Hindi-and-English only", " ".join(by_number[471]["solution_steps"]))
        self.assertIn("5x = 40", " ".join(by_number[505]["solution_steps"]))
        self.assertIn("16x = 15y", " ".join(by_number[512]["solution_steps"]))
        self.assertIn("c = 23 and m = 1", " ".join(by_number[519]["solution_steps"]))

    def test_all_question_pages_have_question_first_vision_review(self) -> None:
        manifest, questions, rejected, lineage = self.package_records()
        self.assertEqual(manifest["stimuli"], [])
        self.assertEqual(len(rejected), 254)
        self.assertTrue(all(record["reason"] == "unresolved_pdf_layout_artifact" for record in rejected))
        self.assertEqual(lineage["vision_reviewed_question_pages"], list(range(116, 153)))
        self.assertTrue(
            all(question["image_association"]["status"] == "no_standalone_visual" for question in questions)
        )

    def test_package_matches_application_contract(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["format_version"], 2)
        self.assertEqual(manifest["chapter"], 4)
        self.assertEqual(len(questions), 291)
        self.assertEqual(manifest["total_questions"], len(questions))

    def test_every_published_record_is_readable_and_graded(self) -> None:
        manifest, questions, _, _ = self.package_records()
        self.assertEqual(manifest["difficulty_policy"], BUILD.DIFFICULTY_RUBRIC_VERSION)
        self.assertFalse(any(BUILD.unresolved_layout_issues(question) for question in questions))
        self.assertEqual(
            Counter(question["difficulty"] for question in questions),
            {"Easy": 96, "Medium": 161, "Hard": 34},
        )


if __name__ == "__main__":
    unittest.main()
