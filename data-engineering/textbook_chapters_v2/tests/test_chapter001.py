from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CropBox
from textbook_chapters_v2.source import crop_region, render_page


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

    def test_every_cross_boundary_record_has_human_reviewed_source_image_anchors(self) -> None:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        reviews = raw["boundary_reviews"]
        expected = {
            *(f"question:{number}" for number in (11, 22, 123, 136, 165, 219, 278, 309, 318)),
            *(f"solution:{number}" for number in (26, 40, 52, 90, 170, 200, 214, 226, 234, 294, 304, 315, 325, 337, 352, 360, 377, 378)),
            "solution:44",
            "solution:45",
            "solution:46",
            *(f"question:{number}" for number in (137, 138, 139, 140)),
        }

        self.assertEqual(set(reviews), expected)
        for key, review in reviews.items():
            with self.subTest(key=key):
                self.assertTrue(review["first_visible_content"].strip())
                self.assertTrue(review["last_visible_content"].strip())
                self.assertTrue(review["crop_sha256s"])
                role, number = key.split(":")
                self.assertEqual(
                    len(review["crop_sha256s"]),
                    len(raw["marker_overrides"][role][number]["segments"]),
                )

    def test_boundary_reviews_pin_actual_source_crop_bytes_and_boxes(self) -> None:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        source_pdf = ROOT / raw["source_pdf"]
        rendered: dict[int, object] = {}
        work = Path(tempfile.mkdtemp(prefix="ksat-boundary-crops-"))
        try:
            for key, review in raw["boundary_reviews"].items():
                role, number = key.split(":")
                configured = raw["marker_overrides"][role][number]["segments"]
                actual_hashes = []
                for index, segment in enumerate(configured):
                    page_number = segment["page"]
                    if page_number not in rendered:
                        rendered[page_number] = render_page(
                            source_pdf,
                            page_number,
                            raw["source_dpi"],
                            work / f"page-{page_number:03d}.png",
                        )
                    crop = crop_region(
                        rendered[page_number],
                        CropBox(segment["left"], segment["top"], segment["right"], segment["bottom"]),
                        work / f"{role}-q{int(number):04d}-s{index:02d}.png",
                    )
                    actual_hashes.append(crop.sha256)
                self.assertEqual(actual_hashes, review["crop_sha256s"], key)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
