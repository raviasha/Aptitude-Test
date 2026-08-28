from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.baseline import (
    _raw_config,
    _with_missing_raw_candidate,
    build_raw_baseline,
    build_raw_baseline_from_fixture,
)
from textbook_chapters import build as legacy_build
from python_vision_calibration import baseline as calibration_baseline


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

    def _associate_by_identity(
        self,
        *,
        raw_records: list[dict[str, object]],
        identity_regions: Mapping[int, Mapping[str, object]],
    ) -> dict[int, dict[str, object]]:
        associate = getattr(calibration_baseline, "_associate_raw_candidates_by_identity", None)
        self.assertIsNotNone(associate, "raw candidates need an identity-based association function")
        return associate(chapter=2, raw_records=raw_records, identity_regions=identity_regions)

    @staticmethod
    def _identity_regions() -> dict[int, dict[str, object]]:
        return {
            1: {"sha256": "1" * 64, "anchor_text": "Alpha orchard has eleven red apples."},
            2: {"sha256": "2" * 64, "anchor_text": "Beta workshop makes twenty blue gears."},
            3: {"sha256": "3" * 64, "anchor_text": "Gamma station receives thirty green trains."},
        }

    def test_equal_count_omission_and_duplicate_cannot_masquerade_as_complete_identity(self) -> None:
        raw_records = [
            {"key": "raw-1", "question_text": "Alpha orchard has eleven red apples.", "options": {}},
            {"key": "raw-1-copy", "question_text": "Alpha orchard has eleven red apples.", "options": {}},
            {"key": "raw-3", "question_text": "Gamma station receives thirty green trains.", "options": {}},
        ]

        with self.assertRaisesRegex(ValueError, r"unresolved raw/source identity.*chapter 2"):
            self._associate_by_identity(raw_records=raw_records, identity_regions=self._identity_regions())

    def test_reordered_explicit_identities_block_instead_of_falling_back_to_position(self) -> None:
        regions = self._identity_regions()
        raw_records = [
            {"key": "raw-2", "question_text": "Beta workshop makes twenty blue gears.", "source_identity": {"number": 2, "question_region_sha256": "2" * 64}},
            {"key": "raw-1", "question_text": "Alpha orchard has eleven red apples.", "source_identity": {"number": 1, "question_region_sha256": "1" * 64}},
            {"key": "raw-3", "question_text": "Gamma station receives thirty green trains.", "source_identity": {"number": 3, "question_region_sha256": "3" * 64}},
        ]

        with self.assertRaisesRegex(ValueError, r"reordered.*identity"):
            self._associate_by_identity(raw_records=raw_records, identity_regions=regions)

    def test_complete_explicit_hash_bound_identity_map_succeeds(self) -> None:
        regions = self._identity_regions()
        raw_records = [
            {"key": "raw-1", "question_text": "Alpha orchard has eleven red apples.", "source_identity": {"number": 1, "question_region_sha256": "1" * 64}},
            {"key": "raw-2", "question_text": "Beta workshop makes twenty blue gears.", "source_identity": {"number": 2, "question_region_sha256": "2" * 64}},
            {"key": "raw-3", "question_text": "Gamma station receives thirty green trains.", "source_identity": {"number": 3, "question_region_sha256": "3" * 64}},
        ]

        associated = self._associate_by_identity(raw_records=raw_records, identity_regions=regions)

        self.assertEqual([associated[number]["key"] for number in (1, 2, 3)], ["raw-1", "raw-2", "raw-3"])

    def test_explicit_number_and_hash_do_not_waive_candidate_anchor_validation(self) -> None:
        regions = self._identity_regions()
        raw_records = [
            {"key": "raw-1", "question_text": "Alpha orchard has eleven red apples.", "source_identity": {"number": 1, "question_region_sha256": "1" * 64}},
            {"key": "raw-2", "question_text": "Alpha orchard has eleven red apples.", "source_identity": {"number": 2, "question_region_sha256": "2" * 64}},
            {"key": "raw-3", "question_text": "Gamma station receives thirty green trains.", "source_identity": {"number": 3, "question_region_sha256": "3" * 64}},
        ]

        with self.assertRaisesRegex(ValueError, r"explicit identity.*anchor"):
            self._associate_by_identity(raw_records=raw_records, identity_regions=regions)

    def test_unique_deterministic_source_anchors_can_supply_missing_explicit_identity(self) -> None:
        raw_records = [
            {"key": "raw-1", "question_text": "Alpha orchard has eleven red apples.", "options": {}},
            {"key": "raw-2", "question_text": "Beta workshop makes twenty blue gears.", "options": {}},
            {"key": "raw-3", "question_text": "Gamma station receives thirty green trains.", "options": {}},
        ]

        associated = self._associate_by_identity(raw_records=raw_records, identity_regions=self._identity_regions())

        self.assertEqual([associated[number]["key"] for number in (1, 2, 3)], ["raw-1", "raw-2", "raw-3"])

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

    def test_missing_raw_candidate_with_source_provenance_is_a_hash_bound_placeholder(self) -> None:
        placeholder = _with_missing_raw_candidate(
            record_id="ch01-q0142",
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

    def test_real_chapter_one_q142_and_q374_omissions_block_unresolved_alignment(self) -> None:
        source_pdf = (
            PROJECT_ROOT
            / "data-engineering"
            / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf"
        )

        with self.assertRaisesRegex(
            ValueError,
            r"unresolved raw/source alignment.*chapter 1.*378 raw records.*380 printed records",
        ):
            build_raw_baseline(1, source_pdf, self.work_root)

        self.assertFalse((self.work_root / "baseline" / "chapter-001.jsonl").exists())

    def test_real_chapter_four_q94_omission_blocks_unresolved_alignment(self) -> None:
        source_pdf = (
            PROJECT_ROOT
            / "data-engineering"
            / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf"
        )

        with self.assertRaisesRegex(
            ValueError,
            r"unresolved raw/source alignment.*chapter 4.*542 raw records.*545 printed records",
        ):
            build_raw_baseline(4, source_pdf, self.work_root)

        self.assertFalse((self.work_root / "baseline" / "chapter-004.jsonl").exists())

    def test_real_chapters_two_and_three_have_hash_bound_identity_or_block_as_unprovable(self) -> None:
        source_pdf = (
            PROJECT_ROOT
            / "data-engineering"
            / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf"
        )
        expected_totals = {2: 130, 3: 206}

        for chapter, total in expected_totals.items():
            chapter_work = self.work_root / f"chapter-{chapter:03d}"
            try:
                records = build_raw_baseline(chapter, source_pdf, chapter_work)
            except ValueError as error:
                self.assertRegex(str(error), rf"unresolved raw/source identity.*chapter {chapter}")
                self.assertFalse((chapter_work / "baseline" / f"chapter-{chapter:03d}.jsonl").exists())
            else:
                self.assertEqual(len(records), total)
                identity_hashes = [record.source_hashes.get("question_identity", ()) for record in records]
                self.assertTrue(all(len(values) == 1 for values in identity_hashes))
                self.assertEqual(len({values[0] for values in identity_hashes}), total)

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
