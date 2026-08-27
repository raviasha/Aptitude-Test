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

from python_vision_calibration.baseline import build_raw_baseline_from_fixture


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


if __name__ == "__main__":
    unittest.main()
