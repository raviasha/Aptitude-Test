from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from textbook_chapters_v2.config import ChapterConfig


ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "data-engineering" / "textbook_chapters_v2" / "configs" / "chapter-001.json"
EXPECTED_SOURCE_SHA256 = "0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf"


class Chapter001PreflightTests(unittest.TestCase):
    def test_configuration_pins_the_reviewed_chapter_and_source(self) -> None:
        config = ChapterConfig.load(CONFIG_PATH)

        self.assertEqual(config.chapter, 1)
        self.assertEqual(config.chapter_name, "Number System")
        self.assertEqual(config.printed_question_count, 380)
        self.assertEqual(config.question_numbers, (1, 380))
        self.assertEqual(config.question_pages, (23, 40))
        self.assertEqual(config.answer_pages, (40, 41))
        self.assertEqual(config.solution_pages, (42, 59))
        self.assertEqual(config.extras["source_pdf_sha256"], EXPECTED_SOURCE_SHA256)

        source_pdf = ROOT / config.extras["source_pdf"]
        self.assertTrue(source_pdf.is_file(), f"Chapter 1 source PDF is missing: {source_pdf}")
        self.assertEqual(hashlib.sha256(source_pdf.read_bytes()).hexdigest(), EXPECTED_SOURCE_SHA256)

    def test_every_printed_number_has_question_and_answer_evidence_and_publishable_records_have_solution_evidence(self) -> None:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        markers = raw["marker_overrides"]
        expected = {str(number) for number in range(1, 381)}

        self.assertEqual(set(markers["question"]), expected)
        self.assertEqual(set(markers["answer_key"]), expected)
        self.assertEqual(set(markers["solution"]), expected)
        self.assertEqual(
            {number for number, spec in markers["solution"].items() if spec.get("missing")},
            {"365"},
        )
        self.assertTrue(markers["solution"]["366"]["segments"])
        known_source_issues = {str(number) for number in raw["known_source_issues"]}
        for number in expected - known_source_issues:
            self.assertTrue(markers["solution"][number].get("segments"), number)

    def test_known_regressions_remain_bound_to_record_specific_source_crops(self) -> None:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for number in (44, 128, 173, 334):
            key = str(number)
            self.assertTrue(raw["marker_overrides"]["question"][key]["segments"])
            self.assertTrue(raw["marker_overrides"]["answer_key"][key]["segments"])
            self.assertTrue(raw["marker_overrides"]["solution"][key]["segments"])


if __name__ == "__main__":
    unittest.main()
