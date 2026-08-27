from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.baseline import _raw_config, build_raw_baseline_from_fixture
from textbook_chapters import build as legacy_build


class RawBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"original textbook bytes")
        self.work_root = self.root / "calibration-work"
        self.raw_records = (
            {
                "record_id": "ch01-q0044",
                "question_text": "raw extracted question",
                "options": {"A": "one", "B": "two"},
                "correct_answer": "A",
                "solution_steps": ["raw extracted solution"],
                "source_association": {
                    "question": ["question-crop-sha"],
                    "answer": ["answer-crop-sha"],
                    "solution": ["solution-crop-sha"],
                },
            },
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_raw_baseline_ignores_legacy_question_override(self) -> None:
        records = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={"questions": {"44": {"question_text": "reviewed text"}}},
        )

        self.assertEqual(records[0].candidate["question_text"], "raw extracted question")
        self.assertNotEqual(records[0].candidate["question_text"], "reviewed text")

    def test_baseline_hash_changes_when_source_bytes_change(self) -> None:
        first = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={},
        )
        self.pdf.write_bytes(self.pdf.read_bytes() + b"changed")
        second = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={},
        )

        self.assertNotEqual(first[0].baseline_sha256, second[0].baseline_sha256)

    def test_baseline_is_canonical_and_atomically_replaces_prior_output(self) -> None:
        records = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={},
        )

        output = self.work_root / "baseline" / "chapter-001.jsonl"
        line = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(line["baseline_sha256"], records[0].baseline_sha256)
        self.assertEqual(line["source_hashes"]["source_pdf"], [hashlib.sha256(self.pdf.read_bytes()).hexdigest()])
        self.assertIn("raw_extractor", line["source_hashes"])
        self.assertIn("config", line["source_hashes"])
        self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_missing_raw_source_association_is_rejected(self) -> None:
        raw = dict(self.raw_records[0])
        raw["source_association"] = {"question": ["q"], "answer": ["a"]}

        with self.assertRaisesRegex(ValueError, "solution association"):
            build_raw_baseline_from_fixture(
                chapter=1,
                source_pdf=self.pdf,
                work_root=self.work_root,
                raw_records=(raw,),
                legacy_review={},
            )

    def test_real_chapter_two_q64_empty_solution_is_preserved_for_vision(self) -> None:
        raw = legacy_build.source_questions(
            PROJECT_ROOT / "question-banks" / "quantitative_aptitude_complete_extended.json",
            "HCF and LCM",
        )[63]
        self.assertEqual(raw["solution_steps"], [])
        record = {**raw, "record_id": "ch02-q0064", "source_association": {
            "question": ["q64-question"],
            "answer": ["q64-answer"],
            "solution": ["q64-solution"],
        }}

        records = build_raw_baseline_from_fixture(
            chapter=2,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=(record,),
            legacy_review={"questions": {"64": {"solution_steps": ["reviewed repair"]}}},
        )

        self.assertEqual(records[0].candidate["solution_steps"], [])
        self.assertIn("missing_solution_steps", records[0].candidate["baseline_failures"])

    def test_review_exceptions_cannot_influence_raw_association_config(self) -> None:
        raw = {
            "chapter": 2,
            "chapter_name": "HCF and LCM",
            "printed_question_count": 130,
            "question_pages": [64, 70],
            "answer_pages": [71],
            "solution_pages": [71, 77],
        }
        reviewed = {
            **raw,
            "source_only_question_numbers": [64],
            "allowed_missing_solution_markers": [65],
            "question_marker_overrides": {"64": {"page": 999, "x0": 1, "top": 1}},
            "solution_marker_overrides": {"64": {"page": 999, "x0": 1, "top": 1}},
            "answer_key_overrides": {"64": "D"},
            "questions": {"64": {"question_text": "reviewed repair"}},
            "rejections": {"64": {"reason": "reviewed"}},
        }

        self.assertEqual(_raw_config(raw), _raw_config(reviewed))
        self.assertNotIn("source_only_question_numbers", _raw_config(reviewed))
        self.assertNotIn("allowed_missing_solution_markers", _raw_config(reviewed))


if __name__ == "__main__":
    unittest.main()
