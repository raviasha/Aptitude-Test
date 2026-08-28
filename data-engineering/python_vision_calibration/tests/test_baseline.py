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

from python_vision_calibration.baseline import (
    _candidate_from_question_region,
    _raw_config,
    _with_missing_raw_candidate,
    build_raw_baseline,
    build_raw_baseline_from_fixture,
)
from textbook_chapters import build as legacy_build


class RawBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"original textbook bytes")
        self.work_root = self.root / "calibration-work"
        self.source_pdf_sha256 = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.source_identity = {
            "number": 44,
            "question_region_sha256": "4" * 64,
            "pdf_sha256": self.source_pdf_sha256,
            "page": 25,
            "box": [20.0, 100.0, 314.0, 160.0],
        }
        self.raw_records = (
            {
                "record_id": "ch01-q0044",
                "source_identity": self.source_identity,
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

    def test_question_candidate_is_created_from_the_hash_bound_pdf_region(self) -> None:
        record = _candidate_from_question_region(
            chapter=2,
            chapter_name="H.C.F. and L.C.M. of Numbers",
            identity={
                "number": 7,
                "question_region_sha256": "7" * 64,
                "pdf_sha256": "f" * 64,
                "page": 65,
                "box": [20.0, 111.25, 314.0, 166.5],
            },
            region_text=(
                "7. Find the H.C.F. of 12 and 18.\n"
                "(a) 2 (b) 3\n(c) 6 (d) 9"
            ),
        )

        self.assertEqual(record["record_id"], "ch02-q0007")
        self.assertEqual(record["question_text"], "Find the H.C.F. of 12 and 18.")
        self.assertEqual(record["options"], {"A": "2", "B": "3", "C": "6", "D": "9"})
        self.assertEqual(record["source_identity"]["question_region_sha256"], "7" * 64)
        self.assertEqual(record["source_identity"]["box"], [20.0, 111.25, 314.0, 166.5])

    def test_garbled_region_parse_is_preserved_as_a_hash_bound_failure(self) -> None:
        record = _candidate_from_question_region(
            chapter=3,
            chapter_name="Decimal Fractions",
            identity={
                "number": 28,
                "question_region_sha256": "8" * 64,
                "pdf_sha256": "e" * 64,
                "page": 84,
                "box": [20.0, 200.0, 314.0, 240.0],
            },
            region_text="28. 1 2 3 �� broken source extraction",
        )

        self.assertEqual(record["record_id"], "ch03-q0028")
        self.assertIn("missing_options", record["baseline_failures"])
        self.assertIn("replacement_character", record["baseline_failures"])
        self.assertEqual(record["source_identity"]["number"], 28)

    def test_column_gutter_glyphs_are_not_silently_treated_as_clean_text(self) -> None:
        record = _candidate_from_question_region(
            chapter=2,
            chapter_name="H.C.F. and L.C.M. of Numbers",
            identity={
                "number": 44,
                "question_region_sha256": "9" * 64,
                "pdf_sha256": "d" * 64,
                "page": 66,
                "box": [298.0, 270.0, 592.0, 326.0],
            },
            region_text="44. Find the L.C.M.\ne\n(a) 10 (b) 12\nC\n(c) 15 (d) 20",
        )

        self.assertIn("isolated_gutter_glyph", record["baseline_failures"])

    def test_fixture_requires_explicit_record_id_and_source_identity(self) -> None:
        no_id = dict(self.raw_records[0])
        no_id.pop("record_id")
        with self.assertRaisesRegex(ValueError, "explicit record_id"):
            build_raw_baseline_from_fixture(
                chapter=1,
                source_pdf=self.pdf,
                work_root=self.work_root,
                raw_records=(no_id,),
                legacy_review={},
            )

        no_identity = dict(self.raw_records[0])
        no_identity.pop("source_identity")
        with self.assertRaisesRegex(ValueError, "explicit source_identity"):
            build_raw_baseline_from_fixture(
                chapter=1,
                source_pdf=self.pdf,
                work_root=self.work_root,
                raw_records=(no_identity,),
                legacy_review={},
            )

    def test_unlabeled_reordered_legacy_rows_cannot_change_region_candidate(self) -> None:
        records = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={
                "legacy_rows": [
                    {"question_text": "wrong second row"},
                    {"question_text": "wrong first row"},
                ]
            },
        )

        self.assertEqual(records[0].candidate["question_text"], "raw extracted question")
        self.assertEqual(records[0].source_identity, self.source_identity)

    def test_baseline_hash_changes_when_source_bytes_change(self) -> None:
        first = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=self.raw_records,
            legacy_review={},
        )
        self.pdf.write_bytes(self.pdf.read_bytes() + b"changed")
        changed_record = dict(self.raw_records[0])
        changed_identity = dict(self.source_identity)
        changed_identity["pdf_sha256"] = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        changed_record["source_identity"] = changed_identity
        second = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=(changed_record,),
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
        self.assertEqual(line["source_identity"], self.source_identity)
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
        raw = {
            "key": "raw-pdf-region-q64",
            "question_text": "Which of the following has no extracted solution?",
            "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
            "correct_answer": "A",
            "solution_steps": [],
        }
        identity = {
            "number": 64,
            "question_region_sha256": "6" * 64,
            "pdf_sha256": self.source_pdf_sha256,
            "page": 67,
            "box": [20.0, 100.0, 314.0, 160.0],
        }
        record = {**raw, "record_id": "ch02-q0064", "source_identity": identity, "source_association": {
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

    def test_missing_raw_candidate_with_source_provenance_is_a_hash_bound_placeholder(self) -> None:
        placeholder = _with_missing_raw_candidate(
            record_id="ch01-q0142",
            source_identity={
                "number": 142,
                "question_region_sha256": "a" * 64,
                "pdf_sha256": self.source_pdf_sha256,
                "page": 29,
                "box": [20.0, 200.0, 314.0, 260.0],
            },
            source_association={
                "question": ["q142-question"],
                "answer": ["q142-answer"],
                "solution": ["q142-solution"],
            },
        )

        records = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=(placeholder,),
            legacy_review={},
        )

        self.assertEqual(records[0].candidate["question_text"], "")
        self.assertIn("missing_raw_candidate", records[0].candidate["baseline_failures"])
        self.assertEqual(records[0].source_hashes["question"], ("q142-question",))
        self.assertNotEqual(records[0].baseline_sha256, "")

    def test_real_chapters_one_through_four_are_source_extracted_or_explicitly_blocked(self) -> None:
        source_pdf = (
            PROJECT_ROOT
            / "data-engineering"
            / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf"
        )
        expected_totals = {1: 380, 2: 130, 3: 206, 4: 545}

        for chapter, total in expected_totals.items():
            chapter_work = self.work_root / f"chapter-{chapter:03d}"
            try:
                records = build_raw_baseline(chapter, source_pdf, chapter_work)
            except ValueError as error:
                self.assertRegex(str(error), rf"source-association blocker.*chapter {chapter}")
                self.assertFalse((chapter_work / "baseline" / f"chapter-{chapter:03d}.jsonl").exists())
            else:
                self.assertEqual(len(records), total)
                identity_hashes = [record.source_identity["question_region_sha256"] for record in records]
                self.assertEqual(len(set(identity_hashes)), total)
                self.assertTrue(all(record.source_identity["number"] == number for number, record in enumerate(records, 1)))

    def test_malformed_nonempty_raw_fields_are_explicit_baseline_failures(self) -> None:
        raw = dict(self.raw_records[0])
        raw.update({
            "question_text": " ",
            "options": {"A": "good", "Q": "", "C": 4},
            "correct_answer": "F",
            "solution_steps": [" ", 42],
        })

        record = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=(raw,),
            legacy_review={},
        )[0]

        self.assertIn("blank_question_text", record.candidate["baseline_failures"])
        self.assertIn("invalid_option_labels", record.candidate["baseline_failures"])
        self.assertIn("invalid_option_text", record.candidate["baseline_failures"])
        self.assertIn("invalid_correct_answer", record.candidate["baseline_failures"])
        self.assertIn("invalid_solution_step", record.candidate["baseline_failures"])

    def test_non_string_question_and_options_are_explicit_baseline_failures(self) -> None:
        raw = dict(self.raw_records[0])
        raw.update({"question_text": 7, "options": 12})

        record = build_raw_baseline_from_fixture(
            chapter=1,
            source_pdf=self.pdf,
            work_root=self.work_root,
            raw_records=(raw,),
            legacy_review={},
        )[0]

        self.assertIn("invalid_question_text", record.candidate["baseline_failures"])
        self.assertIn("invalid_options", record.candidate["baseline_failures"])


if __name__ == "__main__":
    unittest.main()
