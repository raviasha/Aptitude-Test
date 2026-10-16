from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from textbook_chapters_v2.audit import AuditLedger
from textbook_chapters_v2.cli import PENDING_VISION_EXIT, main
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import PipelineBlocked, SourceImage
from textbook_chapters_v2.source import prepare_source_evidence, sha256_path


class MissingOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"controlled source bytes")
        page = self.root / "page.png"
        Image.new("RGB", (100, 100), "white").save(page)
        self.page = SourceImage(page, 1, 180, sha256_path(page))
        # Substitute only PDF rasterization; real cropping, preparation, cache,
        # ledger, queue generation and release gates all run below.
        renderer = patch("textbook_chapters_v2.source.render_page", return_value=self.page)
        renderer.start()
        self.addCleanup(renderer.stop)
        self.work = self.root / "work"
        self.chapter = self.work / "chapter-419"
        self.config_path = self.root / "chapter.json"
        self.raw = {
            "chapter": 419, "bank_name": "Missing printed options",
            "question_pages": [1, 1], "answer_pages": [1, 1],
            "solution_pages": [1, 1], "question_numbers": [44, 46],
            "source_pdf": str(self.pdf), "work_root": str(self.work),
            "candidate_path": str(self.root / "candidate.zip"),
            "published_path": str(self.root / "published.zip"),
            "marker_overrides": {
                role: {str(number): {"segments": [{
                    "page": 1, "left": 0, "right": 100,
                    "top": (number - 44) * 30, "bottom": (number - 43) * 30,
                }]} for number in (44, 45, 46)}
                for role in ("question", "answer_key", "solution")
            },
        }
        self.issue = {
            "status": "missing_options", "reason": "missing_printed_options",
            "detail": "Q45 has no printed options; directions explicitly cover only Q40-44.",
            "requires_reviewed_rejection": True,
        }

    def _save_config(self) -> None:
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8")

    def test_missing_options_blocks_prepare_and_excludes_queue_without_rejection(self) -> None:
        self._save_config()
        self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        stale_job = self.chapter / "extraction-jobs/extract-ch419-q0045.json"
        self.assertTrue(stale_job.is_file())

        self.raw["known_source_issues"] = {"45": self.issue}
        self._save_config()
        self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        evidence_path = self.chapter / "state/evidence.json"
        first_evidence = evidence_path.read_bytes()
        evidence = json.loads(first_evidence)
        self.assertEqual([item["question_number"] for item in evidence], [44, 45, 46])
        missing = evidence[1]
        self.assertEqual(missing["source_status"], "missing_options")
        self.assertTrue(missing["requires_reviewed_rejection"])
        self.assertEqual(missing["source_reasons"], [
            "missing_printed_options: Q45 has no printed options; directions explicitly cover only Q40-44."
        ])
        for role in ("question", "answer_key", "solution"):
            self.assertEqual(len(missing[f"{role}_crops"]), 1)
            crop = missing[f"{role}_crops"][0]
            self.assertEqual(sha256_path(Path(crop["path"])), crop["sha256"])

        for _ in range(2):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
            self.assertEqual(evidence_path.read_bytes(), first_evidence)
            self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
            queue = [json.loads(line) for line in
                     (self.chapter / "extraction-jobs.jsonl").read_text().splitlines()]
            self.assertEqual([job["job_id"] for job in queue], [
                "extract-ch419-q0044", "extract-ch419-q0046"
            ])
            self.assertFalse(stale_job.exists())
            ledger = AuditLedger(self.work, 419)
            record = ledger.record(45)
            self.assertEqual(record.status, "blocked")
            self.assertEqual(record.findings, tuple(missing["source_reasons"]))
            self.assertEqual(record.reviewer, "")
            self.assertEqual(record.rejection_reason, "")
            self.assertEqual(record.candidate_sha256, "")
            self.assertEqual(record.asset_hashes, ())
            # Restrict the gate to Q45 so another pending record cannot mask it.
            blocked_only = ChapterConfig.from_dict({**self.raw, "question_numbers": [45, 45]})
            gate = AuditLedger(self.root / "gate", 419)
            gate.merge_record(record)
            with self.assertRaisesRegex(PipelineBlocked, "Question 45 remains in forbidden status blocked"):
                gate.validate_release_gate(blocked_only)
        self.assertEqual(list(self.chapter.glob("extraction-results/*.json")), [])
        self.assertFalse((self.root / "published.zip").exists())

    def test_missing_options_requires_reason_detail_and_explicit_review_gate(self) -> None:
        for field, value, message in (
            ("reason", "", "requires reason and detail"),
            ("detail", "", "requires reason and detail"),
            ("requires_reviewed_rejection", False, "must require reviewed rejection"),
            ("requires_reviewed_rejection", "true", "must require reviewed rejection"),
            ("status", "incomplete", "Unsupported source status"),
        ):
            with self.subTest(field=field, value=value):
                self.raw["known_source_issues"] = {"45": {**self.issue, field: value}}
                with self.assertRaisesRegex(ValueError, message):
                    prepare_source_evidence(ChapterConfig.from_dict(self.raw), self.pdf, self.work)


if __name__ == "__main__":
    unittest.main()
