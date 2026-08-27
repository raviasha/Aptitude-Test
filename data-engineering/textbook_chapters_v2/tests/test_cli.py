from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = DATA_ENGINEERING_ROOT.parent
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from textbook_chapters_v2.audit import AuditLedger, approval_dependency_fingerprint
from textbook_chapters_v2.cli import BLOCKED_EXIT, PENDING_VISION_EXIT, main
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import AuditRecord, CropBox, PipelineBlocked, RecordEvidence, RenderArtifacts, SourceCrop
from textbook_chapters_v2.promote import promote_candidate
from textbook_chapters_v2.store import canonical_json


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkflowCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.work_root = self.root / "work"
        self.published = self.root / "published" / "chapter.zip"
        self.candidate = self.root / "staging" / "candidate.zip"
        self.source_pdf = self.root / "source.pdf"
        self.source_pdf.write_bytes(b"controlled-test-pdf")
        self.config_path = self.root / "chapter-007.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "chapter": 7,
                    "bank_name": "CLI Contract",
                    "question_pages": [1, 1],
                    "answer_pages": [1, 1],
                    "solution_pages": [1, 1],
                    "question_numbers": [84, 84],
                    "source_pdf": str(self.source_pdf),
                    "work_root": str(self.work_root),
                    "candidate_path": str(self.candidate),
                    "published_path": str(self.published),
                }
            ),
            encoding="utf-8",
        )
        self.config = ChapterConfig.load(self.config_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _pending_record(self) -> AuditRecord:
        return AuditRecord(chapter=7, question_number=84, status="pending_extraction")

    def _approved_record(self, candidate_sha256: str = "d" * 64) -> AuditRecord:
        record = AuditRecord(
            chapter=7,
            question_number=84,
            status="approved_for_publish",
            source_crop_hashes=("a" * 64, "b" * 64, "c" * 64),
            candidate_sha256=candidate_sha256,
            asset_hashes=("unanswered.desktop:" + "e" * 64, "submitted.desktop:" + "f" * 64),
            policy_version=1,
            extractor_schema_version=1,
            verifier_schema_version=1,
            renderer_version="renderer-1",
            application_asset_version="application-1",
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
        return replace(record, dependency_fingerprint=approval_dependency_fingerprint(record))

    def _summary(self, candidate_sha256: str = "d" * 64):
        ledger = AuditLedger(self.work_root, 7)
        ledger.merge_record(self._approved_record(candidate_sha256))
        return ledger.validate_release_gate(self.config)

    def _prepared_evidence(self) -> list[RecordEvidence]:
        crop_path = self.root / "crop.png"
        crop_path.write_bytes(b"crop")
        crop_hash = _sha256(crop_path)
        crops = tuple(
            SourceCrop(role=role, question_number=84, page_number=1, box=CropBox(0, 0, 1, 1), path=crop_path,
                       width=1, height=1, sha256=crop_hash, source_image_sha256="b" * 64, source_dpi=180)
            for role in ("question", "answer_key", "solution")
        )
        return [RecordEvidence(chapter=7, question_number=84, source_pdf=self.source_pdf, source_pdf_sha256="a" * 64,
                               question_crops=(crops[0],), answer_key_crops=(crops[1],), solution_crops=(crops[2],),
                               dependency_fingerprint="c" * 64)]

    def _package(self, path: Path, summary, candidate_sha256: str = "d" * 64) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"format_version": 3, "bank_name": "CLI Contract", "question_files": ["questions/ch07.jsonl"]}
        question = {
            "key": "ch07-q0084",
            "question_text": "What is one plus one?",
            "category": "CLI Contract",
            "chapter": "7",
            "difficulty": "Easy",
            "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
            "correct_answer": "B",
            "solution_steps": ["1 + 1 = 2."],
        }
        lineage = {"records": [{"key": "ch07-q0084", "candidate_sha256": candidate_sha256}]}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("questions/ch07.jsonl", json.dumps(question) + "\n")
            archive.writestr("metadata/audit-summary.json", canonical_json(dict(summary)))
            archive.writestr("metadata/lineage.json", json.dumps(lineage))
        path.with_suffix(".package.json").write_text(
            json.dumps({"candidate_sha256": _sha256(path), "audit_sha256": summary["audit_sha256"]}),
            encoding="utf-8",
        )

    def test_prepare_then_run_stops_with_visible_pending_vision_queue(self) -> None:
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._prepared_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
            self.assertEqual(main(["run", "--config", str(self.config_path)]), PENDING_VISION_EXIT)

        queue = self.work_root / "chapter-007" / "extraction-jobs.jsonl"
        self.assertTrue(queue.is_file())
        self.assertFalse(self.published.exists())

    def test_force_run_recomputes_disposable_stage_state_without_touching_audit(self) -> None:
        evidence = self._prepared_evidence()
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence) as prepare:
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
            audit_before = (self.work_root / "chapter-007" / "audit-ledger.json").read_bytes()
            self.assertEqual(main(["run", "--config", str(self.config_path), "--force"]), PENDING_VISION_EXIT)

        self.assertEqual(prepare.call_count, 2)
        self.assertEqual((self.work_root / "chapter-007" / "audit-ledger.json").read_bytes(), audit_before)

    def test_semantic_extraction_disagreement_is_persisted_as_quarantine(self) -> None:
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._prepared_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        job = json.loads((self.work_root / "chapter-007" / "extraction-jobs" / "extract-ch07-q0084.json").read_text(encoding="utf-8"))
        answer_hash = next(item["sha256"] for item in job["sources"] if item["role"] == "answer_key")
        results = self.root / "results"
        results.mkdir()
        (results / "extract-ch07-q0084.json").write_text(json.dumps({
            "job_id": job["job_id"], "job_fingerprint": job["job_fingerprint"],
            "question_text": "Which answer is correct?", "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
            "correct_answer": "B", "answer_key": {"correct_answer": "D", "crop_sha256": answer_hash,
                                                     "job_fingerprint": job["job_fingerprint"]},
            "solution_steps": ["The textbook answer key says D."],
            "representation": {"question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                               "solution": "text"},
            "differences_from_legacy": [], "reviewer": "vision-extractor",
        }), encoding="utf-8")

        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(results)]), BLOCKED_EXIT)
        self.assertEqual(AuditLedger(self.work_root, 7).record(84).status, "blocked")

    def test_package_refuses_pending_verification_and_force_cannot_bypass_gate(self) -> None:
        ledger = AuditLedger(self.work_root, 7)
        ledger.merge_record(replace(self._pending_record(), status="pending_vision"))
        cache_marker = self.work_root / "chapter-007" / "cache" / "stale.json"
        cache_marker.parent.mkdir(parents=True)
        cache_marker.write_text("stale", encoding="utf-8")

        self.assertEqual(main(["package", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertTrue(cache_marker.exists())
        self.assertEqual(main(["package", "--config", str(self.config_path), "--force"]), BLOCKED_EXIT)
        self.assertFalse(cache_marker.exists())
        self.assertFalse(self.candidate.exists())

    def test_promote_refuses_lineage_hash_not_named_by_authoritative_audit(self) -> None:
        summary = self._summary()
        self._package(self.candidate, summary, candidate_sha256="9" * 64)

        with self.assertRaisesRegex(PipelineBlocked, "lineage"):
            promote_candidate(self.candidate, self.published, summary)
        self.assertFalse(self.published.exists())

    def test_promote_reparses_package_backs_up_previous_and_writes_receipt(self) -> None:
        summary = self._summary()
        self._package(self.candidate, summary)
        old_package = self.root / "old.zip"
        self._package(old_package, summary)
        with zipfile.ZipFile(old_package, "a") as archive:
            archive.writestr("old-marker.txt", "old")
        self.published.parent.mkdir(parents=True)
        self.published.write_bytes(old_package.read_bytes())
        prior_hash = _sha256(self.published)

        receipt = promote_candidate(self.candidate, self.published, summary)

        self.assertEqual(receipt.prior_sha256, prior_hash)
        self.assertEqual(receipt.candidate_sha256, _sha256(self.candidate))
        self.assertEqual(_sha256(self.published), receipt.candidate_sha256)
        receipt_path = self.published.with_suffix(".promotion.json")
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["prior_sha256"], prior_hash)
        self.assertEqual(payload["candidate_sha256"], receipt.candidate_sha256)
        rollback = Path(payload["rollback_path"])
        self.assertTrue(rollback.is_file())
        self.assertEqual(_sha256(rollback), prior_hash)

    def test_promote_command_reloads_current_ledger_and_force_cannot_use_stale_summary(self) -> None:
        summary = self._summary()
        self._package(self.candidate, summary)
        AuditLedger(self.work_root, 7).merge_record(replace(self._pending_record(), status="pending_vision"))

        self.assertEqual(main(["promote", "--config", str(self.config_path), "--force"]), BLOCKED_EXIT)
        self.assertFalse(self.published.exists())

    def test_promote_refuses_candidate_changed_after_package_seal(self) -> None:
        summary = self._summary()
        self._package(self.candidate, summary)
        with zipfile.ZipFile(self.candidate, "a") as archive:
            archive.writestr("unexpected.txt", "changed after packaging")

        with self.assertRaisesRegex(PipelineBlocked, "package seal"):
            promote_candidate(self.candidate, self.published, summary)
        self.assertFalse(self.published.exists())

    def test_promote_reports_corrupt_sealed_zip_as_a_blocked_gate(self) -> None:
        summary = self._summary()
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b"not-a-zip")
        self.candidate.with_suffix(".package.json").write_text(json.dumps({
            "candidate_sha256": _sha256(self.candidate), "audit_sha256": summary["audit_sha256"],
        }), encoding="utf-8")

        with self.assertRaisesRegex(PipelineBlocked, "package"):
            promote_candidate(self.candidate, self.published, summary)
        self.assertFalse(self.published.exists())

    def test_controlled_chapter_resumes_through_all_named_stages_and_promotes(self) -> None:
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._prepared_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        extraction_job = json.loads(
            (self.work_root / "chapter-007" / "extraction-jobs" / "extract-ch07-q0084.json").read_text(encoding="utf-8")
        )
        answer_hash = next(item["sha256"] for item in extraction_job["sources"] if item["role"] == "answer_key")
        extraction_results = self.work_root / "chapter-007" / "extraction-results"
        extraction_results.mkdir()
        (extraction_results / "extract-ch07-q0084.json").write_text(json.dumps({
            "job_id": extraction_job["job_id"], "job_fingerprint": extraction_job["job_fingerprint"],
            "question_text": "What is 1 + 1?", "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
            "correct_answer": "B", "answer_key": {"correct_answer": "B", "crop_sha256": answer_hash,
                                                     "job_fingerprint": extraction_job["job_fingerprint"]},
            "solution_steps": ["1 + 1 = 2."],
            "representation": {"question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                               "solution": "text"},
            "differences_from_legacy": [], "reviewer": "vision-extractor",
        }), encoding="utf-8")
        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(extraction_results)]), 0)
        self.assertEqual(main(["build", "--config", str(self.config_path)]), 0)

        unanswered = self.root / "unanswered.png"
        submitted = self.root / "submitted.png"
        unanswered.write_bytes(b"unanswered-render")
        submitted.write_bytes(b"submitted-render")
        rendered = RenderArtifacts(
            question_screenshots={"desktop": unanswered}, solution_screenshots={"desktop": submitted},
            screenshot_hashes={"question.desktop": _sha256(unanswered), "solution.desktop": _sha256(submitted)},
            renderer_version="controlled-real-app-renderer",
        )
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=rendered):
            self.assertEqual(main(["render", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        verification_job = json.loads(
            (self.work_root / "chapter-007" / "verification-jobs.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        verification_results = self.work_root / "chapter-007" / "verification-results"
        verification_results.mkdir()
        verdicts = {field: "pass" for field in (
            "question", "options.A", "options.B", "options.C", "options.D", "answer_mapping", "solution", "readability", "clipping"
        )}
        (verification_results / "verify-ch07-q0084.json").write_text(json.dumps({
            "job_id": verification_job["job_id"], "job_fingerprint": verification_job["job_fingerprint"],
            "verdicts": verdicts, "differences": {}, "reviewer": "independent-vision-verifier",
        }), encoding="utf-8")
        self.assertEqual(main(["ingest-verification", "--config", str(self.config_path), "--results", str(verification_results)]), 0)
        self.assertEqual(main(["package", "--config", str(self.config_path)]), 0)
        self.assertTrue(self.candidate.is_file())
        self.assertEqual(main(["promote", "--config", str(self.config_path)]), 0)
        self.assertEqual(_sha256(self.published), _sha256(self.candidate))


if __name__ == "__main__":
    unittest.main()
