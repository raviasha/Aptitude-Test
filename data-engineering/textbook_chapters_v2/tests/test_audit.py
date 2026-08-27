from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.audit import AuditLedger
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import AuditRecord, PipelineBlocked


def _literal_fingerprint(record: AuditRecord) -> str:
    dependencies = {
        "source_crop_hashes": sorted(record.source_crop_hashes),
        "candidate_sha256": record.candidate_sha256,
        "asset_hashes": sorted(record.asset_hashes),
        "policy_version": record.policy_version,
        "extractor_schema_version": record.extractor_schema_version,
        "verifier_schema_version": record.verifier_schema_version,
        "renderer_version": record.renderer_version,
        "application_asset_version": record.application_asset_version,
    }
    encoded = json.dumps([dependencies], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuditLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config = ChapterConfig.from_dict(
            {
                "chapter": 7,
                "bank_name": "Audit Gate",
                "question_pages": [1, 1],
                "answer_pages": [2, 2],
                "solution_pages": [3, 3],
                "question_numbers": [84, 84],
            }
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _approved(self, question_number: int = 84, **changes: object) -> AuditRecord:
        record = AuditRecord(
            chapter=7,
            question_number=question_number,
            status="approved_for_publish",
            source_crop_hashes=("a" * 64, "b" * 64, "c" * 64),
            candidate_sha256="d" * 64,
            asset_hashes=("unanswered.desktop:" + "e" * 64, "submitted.desktop:" + "f" * 64),
            policy_version=1,
            extractor_schema_version=1,
            verifier_schema_version=1,
            renderer_version="playwright-1.58.0",
            application_asset_version="ksat-ui-1.3.0",
            reviewer="independent-vision-reviewer",
            field_verdicts={
                "question": "pass",
                "options.A": "pass",
                "options.B": "pass",
                "options.C": "pass",
                "options.D": "pass",
                "answer_mapping": "pass",
                "solution": "pass",
                "readability": "pass",
                "clipping": "pass",
            },
        )
        record = replace(record, dependency_fingerprint=_literal_fingerprint(record))
        changed = replace(record, **changes)
        if changes and "dependency_fingerprint" not in changes:
            changed = replace(changed, dependency_fingerprint=_literal_fingerprint(changed))
        return changed

    def _ledger_with(self, record: AuditRecord, name: str = "chapter-007.json") -> AuditLedger:
        ledger = AuditLedger(self.root / name / "work", 7)
        ledger.merge_record(record)
        return ledger

    def test_every_nonterminal_or_quarantined_status_blocks_release(self) -> None:
        for index, status in enumerate(("pending_extraction", "candidate", "blocked", "pending_render", "pending_vision")):
            ledger = self._ledger_with(replace(self._approved(), status=status), f"forbidden-{index}.json")
            with self.subTest(status=status), self.assertRaisesRegex(PipelineBlocked, status):
                ledger.validate_release_gate(self.config)

    def test_approved_record_requires_every_piece_of_release_evidence(self) -> None:
        missing_cases = (
            ("reviewer", {"reviewer": ""}),
            ("source crop", {"source_crop_hashes": ()}),
            ("candidate", {"candidate_sha256": ""}),
            ("both rendering states", {"asset_hashes": ("unanswered.desktop:" + "e" * 64,)}),
            ("both rendering states", {"asset_hashes": ("e" * 64, "f" * 64)}),
            (
                "distinct",
                {"asset_hashes": ("unanswered.desktop:" + "e" * 64, "submitted.desktop:" + "e" * 64)},
            ),
            ("field verdicts", {"field_verdicts": {}}),
            ("policy version", {"policy_version": 0}),
            ("extractor schema version", {"extractor_schema_version": 0}),
            ("verifier schema version", {"verifier_schema_version": 0}),
            ("renderer version", {"renderer_version": ""}),
            ("application asset version", {"application_asset_version": ""}),
        )
        for index, (message, changes) in enumerate(missing_cases):
            ledger = self._ledger_with(self._approved(**changes), f"missing-{index}.json")
            with self.subTest(message=message), self.assertRaisesRegex(PipelineBlocked, message):
                ledger.validate_release_gate(self.config)

        stale = self._ledger_with(self._approved(dependency_fingerprint="0" * 64), "stale.json")
        with self.assertRaisesRegex(PipelineBlocked, "dependency fingerprint"):
            stale.validate_release_gate(self.config)

    def test_approved_record_requires_literal_complete_passing_field_verdicts(self) -> None:
        verdict_cases = (
            {"question": "pass", "answer_mapping": "pass", "solution": "pass", "readability": "pass", "clipping": "pass"},
            {
                "question": "pass", "options.A": "pass", "answer_mapping": "pass",
                "solution": "pass", "readability": "pass", "clipping": "pass",
            },
            {
                "question": "pass", "options.A": "fail", "answer_mapping": "pass",
                "solution": "pass", "readability": "pass", "clipping": "pass",
            },
        )
        for index, verdicts in enumerate(verdict_cases):
            ledger = self._ledger_with(self._approved(field_verdicts=verdicts), f"verdict-{index}.json")
            with self.subTest(verdicts=verdicts), self.assertRaisesRegex(PipelineBlocked, "field verdicts"):
                ledger.validate_release_gate(self.config)

    def test_reviewed_rejection_requires_reviewer_and_reason(self) -> None:
        for index, (reviewer, reason) in enumerate((("", "not a source question"), ("source-reviewer", ""))):
            ledger = self._ledger_with(
                AuditRecord(
                    chapter=7,
                    question_number=84,
                    status="reviewed_rejection",
                    reviewer=reviewer,
                    rejection_reason=reason,
                ),
                f"rejection-{index}.json",
            )
            with self.subTest(reviewer=reviewer, reason=reason), self.assertRaisesRegex(PipelineBlocked, "reviewed_rejection"):
                ledger.validate_release_gate(self.config)

    def test_terminal_records_produce_config_matched_package_compatible_counts(self) -> None:
        config = replace(self.config, question_numbers=(84, 86))
        ledger = self._ledger_with(self._approved(84))
        ledger.merge_record(
            AuditRecord(
                chapter=7,
                question_number=85,
                status="reviewed_rejection",
                reviewer="source-reviewer",
                rejection_reason="Answer key is absent from the configured source pages.",
            )
        )
        ledger.merge_record(self._approved(86))

        summary = ledger.validate_release_gate(config)

        self.assertEqual(summary.total_records, 3)
        self.assertEqual(summary.approved_count, 2)
        self.assertEqual(summary.reviewed_rejection_count, 1)
        self.assertTrue(summary["all_records_terminal"])
        self.assertTrue(summary["all_records_approved_or_reviewed_rejection"])
        self.assertEqual(summary["reviewed_rejections"][0]["question_number"], 85)

    def test_missing_extra_or_wrong_chapter_record_blocks_exact_coverage(self) -> None:
        missing = AuditLedger(self.root / "missing" / "work", 7)
        with self.assertRaisesRegex(PipelineBlocked, "missing.*84"):
            missing.validate_release_gate(self.config)

        extra = self._ledger_with(self._approved(84), "extra-record.json")
        extra.merge_record(self._approved(86))
        with self.assertRaisesRegex(PipelineBlocked, "excluded or unconfigured.*86"):
            extra.validate_release_gate(self.config)

        wrong = AuditLedger(self.root / "wrong" / "work", 8)
        wrong.merge_record(replace(self._approved(84), chapter=8))
        with self.assertRaisesRegex(PipelineBlocked, "chapter"):
            wrong.validate_release_gate(self.config)

    def test_merge_is_atomic_persistent_and_keeps_one_record_per_question(self) -> None:
        work_root = self.root / "atomic" / "work"
        path = work_root / "chapter-007" / "audit-ledger.json"
        ledger = AuditLedger(work_root, 7)
        ledger.merge_record(replace(self._approved(84), status="pending_vision"))
        ledger.merge_record(self._approved(84))

        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 1)
        self.assertEqual(len(persisted["records"]), 1)
        self.assertEqual(persisted["records"][0]["status"], "approved_for_publish")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        self.assertEqual(AuditLedger(work_root, 7).record(84), self._approved(84))

    def test_ledger_path_is_fixed_beneath_its_declared_chapter_work_root(self) -> None:
        work_root = self.root / "scoped" / "work"
        ledger = AuditLedger(work_root, 7)
        ledger.merge_record(self._approved())

        self.assertEqual(ledger.path, work_root.resolve() / "chapter-007" / "audit-ledger.json")
        self.assertTrue(ledger.path.is_file())
        with self.assertRaisesRegex((TypeError, ValueError), "chapter"):
            AuditLedger(self.root / "arbitrary-ledger.json")

    def test_dependency_changes_reset_only_that_record_to_earliest_affected_state(self) -> None:
        mutations = (
            ("source_crop_hashes", ("1" * 64,), "pending_extraction"),
            ("candidate_sha256", "2" * 64, "pending_render"),
            (
                "asset_hashes",
                ("unanswered.desktop:" + "3" * 64, "submitted.desktop:" + "4" * 64),
                "pending_vision",
            ),
            ("policy_version", 2, "pending_extraction"),
            ("extractor_schema_version", 2, "pending_extraction"),
            ("verifier_schema_version", 2, "pending_vision"),
            ("renderer_version", "playwright-2", "pending_render"),
            ("application_asset_version", "ksat-ui-2", "pending_render"),
        )
        for index, (field, value, expected_status) in enumerate(mutations):
            work_root = self.root / f"invalidate-{index}" / "work"
            path = work_root / "chapter-007" / "audit-ledger.json"
            ledger = AuditLedger(work_root, 7)
            ledger.merge_record(self._approved(84))
            ledger.merge_record(self._approved(85))
            changed = replace(self._approved(84), **{field: value})
            changed = replace(changed, dependency_fingerprint=_literal_fingerprint(changed))

            ledger.merge_record(changed)

            with self.subTest(field=field):
                self.assertEqual(ledger.record(84).status, expected_status)
                self.assertEqual(ledger.record(85).status, "approved_for_publish")
                persisted = json.loads(path.read_text(encoding="utf-8"))
                statuses = {item["question_number"]: item["status"] for item in persisted["records"]}
                self.assertEqual(statuses, {84: expected_status, 85: "approved_for_publish"})


if __name__ == "__main__":
    unittest.main()
