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

from PIL import Image

DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = DATA_ENGINEERING_ROOT.parent
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from textbook_chapters_v2.audit import AuditLedger, approval_dependency_fingerprint
from textbook_chapters_v2.cli import (
    BLOCKED_EXIT,
    PENDING_VISION_EXIT,
    _application_renderer_manifest,
    _candidates,
    _path_value,
    _render_dependency_fingerprint,
    main,
)
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import AuditRecord, CandidateRecord, CropBox, PipelineBlocked, RecordEvidence, RenderArtifacts, SourceCrop
from textbook_chapters_v2.promote import promote_candidate
from textbook_chapters_v2.render import RenderArtifacts as BrowserRenderArtifacts
from textbook_chapters_v2.store import canonical_json, dependency_fingerprint


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
        self.application_root = self.root / "application"
        application_files = (
            "app.py", "question_media.py", "chapter_repairs.py",
            "static/index.html", "static/app.js", "static/styles.css",
            "static/branding.css", "static/math.css",
            "data-engineering/textbook_chapters_v2/cli.py",
            "data-engineering/textbook_chapters_v2/render.py",
            "data-engineering/textbook_chapters_v2/models.py",
            "data-engineering/textbook_chapters_v2/candidates.py",
            "data-engineering/textbook_chapters_v2/package.py",
            "data-engineering/textbook_chapters_v2/vision.py",
            "data-engineering/textbook_chapters_v2/rules.py",
            "data-engineering/textbook_chapters_v2/schemas/extraction-result.schema.json",
            "data-engineering/textbook_chapters_v2/schemas/verification-result.schema.json",
        )
        for relative in application_files:
            path = self.application_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"controlled {relative}\n", encoding="utf-8")
        self.application_root_patch = patch("textbook_chapters_v2.cli.APPLICATION_ROOT", self.application_root)
        self.application_root_patch.start()
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
        self.application_root_patch.stop()
        self.temporary_directory.cleanup()

    def _pending_record(self) -> AuditRecord:
        return AuditRecord(chapter=7, question_number=84, status="pending_extraction")

    def test_configured_work_paths_expand_the_user_home_before_resolving(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["work_root"] = "~/.ksat/textbook-v2"
        config = ChapterConfig.from_dict(raw)

        self.assertEqual(_path_value(config, "work_root"), (Path.home() / ".ksat" / "textbook-v2").resolve())

    def _approved_record(self, candidate_sha256: str = "d" * 64) -> AuditRecord:
        manifest = _application_renderer_manifest(self.config, ("playwright-chromium:124.0.2",))
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
            renderer_version=manifest["renderer_fingerprint"],
            application_asset_version=manifest["application_fingerprint"],
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
        return [RecordEvidence(chapter=7, question_number=84, source_pdf=self.source_pdf, source_pdf_sha256=_sha256(self.source_pdf),
                               question_crops=(crops[0],), answer_key_crops=(crops[1],), solution_crops=(crops[2],),
                               dependency_fingerprint="c" * 64)]

    def _png_crop(self, name: str, role: str, size: tuple[int, int], color: str, index: int) -> SourceCrop:
        path = self.root / f"{name}.png"
        Image.new("RGB", size, color).save(path)
        return SourceCrop(
            role=role, question_number=84, page_number=index + 1, box=CropBox(0, 0, size[0], size[1]), path=path,
            width=size[0], height=size[1], sha256=_sha256(path), source_image_sha256=f"{index + 1:064x}", source_dpi=180,
        )

    def _media_evidence(self) -> list[RecordEvidence]:
        question = (
            self._png_crop("question-part-1", "question", (10, 10), "red", 0),
            self._png_crop("question-part-2", "question", (10, 15), "blue", 1),
            self._png_crop("option-d-only", "question", (7, 6), "green", 2),
        )
        answer = (self._png_crop("answer", "answer_key", (5, 5), "white", 3),)
        solution = (
            self._png_crop("solution-part-1", "solution", (12, 8), "black", 4),
            self._png_crop("solution-part-2", "solution", (12, 9), "gray", 5),
        )
        return [RecordEvidence(
            chapter=7, question_number=84, source_pdf=self.source_pdf, source_pdf_sha256=_sha256(self.source_pdf),
            question_crops=question, answer_key_crops=answer, solution_crops=solution, dependency_fingerprint="c" * 64,
        )]

    def _set_field_media(self, field_media: dict[str, object]) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["field_media"] = {"84": field_media}
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.config = ChapterConfig.load(self.config_path)

    def _write_extraction_result(
        self, representation: dict[str, object], results: Path | None = None
    ) -> Path:
        job = json.loads((self.work_root / "chapter-007" / "extraction-jobs" / "extract-ch07-q0084.json").read_text(encoding="utf-8"))
        answer_hash = next(item["sha256"] for item in job["sources"] if item["role"] == "answer_key")
        results = results or (self.root / "media-results")
        results.mkdir(exist_ok=True)
        (results / "extract-ch07-q0084.json").write_text(json.dumps({
            "job_id": job["job_id"], "job_fingerprint": job["job_fingerprint"],
            "question_text": "Question text", "options": {"A": "1", "B": "2", "C": "3", "D": "option D"},
            "correct_answer": "D", "answer_key": {"correct_answer": "D", "crop_sha256": answer_hash,
                                                     "job_fingerprint": job["job_fingerprint"]},
            "solution_steps": ["One semantic solution step covering the complete source solution."],
            "representation": representation, "differences_from_legacy": [], "reviewer": "vision-extractor",
        }), encoding="utf-8")
        return results

    def _evidence_for_question(
        self,
        question_number: int,
        *,
        source_status: str = "complete",
        source_reasons: tuple[str, ...] = (),
        requires_reviewed_rejection: bool = False,
        dependency_fingerprint: str | None = None,
    ) -> RecordEvidence:
        original = self._prepared_evidence()[0]

        def renumber(crops: tuple[SourceCrop, ...]) -> tuple[SourceCrop, ...]:
            return tuple(replace(crop, question_number=question_number) for crop in crops)

        return replace(
            original,
            question_number=question_number,
            question_crops=renumber(original.question_crops),
            answer_key_crops=renumber(original.answer_key_crops),
            solution_crops=renumber(original.solution_crops),
            source_status=source_status,
            source_reasons=source_reasons,
            requires_reviewed_rejection=requires_reviewed_rejection,
            dependency_fingerprint=dependency_fingerprint or f"{question_number:064x}",
        )

    def _three_record_source_evidence(self) -> list[RecordEvidence]:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["question_numbers"] = [84, 86]
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.config = ChapterConfig.load(self.config_path)
        return [
            self._evidence_for_question(84),
            self._evidence_for_question(
                85,
                source_status="missing_solution",
                source_reasons=("textbook_solution_missing: No numbered solution is printed.",),
                requires_reviewed_rejection=True,
            ),
            self._evidence_for_question(
                86,
                source_status="incomplete_solution",
                source_reasons=("textbook_solution_incomplete: Only an unnumbered continuation is visible.",),
                requires_reviewed_rejection=True,
            ),
        ]

    def _prepare_media_extraction(self, field_media: dict[str, object] | None = None) -> Path:
        if field_media is not None:
            self._set_field_media(field_media)
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._media_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        return self.work_root / "chapter-007"

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

    def _text_candidate(self, question_number: int, source_fingerprint: str = "b" * 64) -> CandidateRecord:
        payload = {
            "chapter": 7, "question_number": question_number, "question_text": f"Question {question_number}",
            "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "correct_answer": "B",
            "answer_key_answer": "B", "answer_key_crop_sha256": "a" * 64,
            "answer_key_job_fingerprint": source_fingerprint, "solution_steps": ["The answer is 2."],
            "representation": {
                "question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text", "media": {},
            },
            "source_fingerprint": source_fingerprint,
        }
        return CandidateRecord(**payload, sha256=dependency_fingerprint(payload))

    def _candidate_state(self, candidate: CandidateRecord) -> dict[str, object]:
        return {
            "chapter": candidate.chapter, "question_number": candidate.question_number,
            "question_text": candidate.question_text, "options": dict(candidate.options),
            "correct_answer": candidate.correct_answer, "answer_key_answer": candidate.answer_key_answer,
            "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
            "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint,
            "solution_steps": list(candidate.solution_steps), "representation": dict(candidate.representation),
            "source_fingerprint": candidate.source_fingerprint, "sha256": candidate.sha256, "status": candidate.status,
        }

    def test_prepare_then_run_stops_with_visible_pending_vision_queue(self) -> None:
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._prepared_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
            self.assertEqual(main(["run", "--config", str(self.config_path)]), PENDING_VISION_EXIT)

        queue = self.work_root / "chapter-007" / "extraction-jobs.jsonl"
        self.assertTrue(queue.is_file())
        self.assertFalse(self.published.exists())

    def test_prepare_quarantines_source_issue_and_persists_reasoned_evidence(self) -> None:
        source_issue = replace(
            self._prepared_evidence()[0],
            source_status="missing_solution",
            source_reasons=("textbook_solution_missing: No numbered solution is printed.",),
            requires_reviewed_rejection=True,
        )
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=[source_issue]):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)

        evidence_payload = json.loads(
            (self.work_root / "chapter-007" / "state" / "evidence.json").read_text(encoding="utf-8")
        )[0]
        record = AuditLedger(self.work_root, 7).record(84)
        self.assertEqual(evidence_payload["source_status"], "missing_solution")
        self.assertEqual(evidence_payload["source_reasons"], list(source_issue.source_reasons))
        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.findings, source_issue.source_reasons)

    def test_unchanged_prepare_preserves_reviewed_rejections_and_run_queues_only_actionable_records(self) -> None:
        evidence = self._three_record_source_evidence()
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        self.assertEqual(
            main([
                "reject", "--config", str(self.config_path), "--question", "85",
                "--reviewer", "source-reviewer", "--reason", "The numbered textbook solution is absent.",
            ]),
            0,
        )
        self.assertEqual(
            main([
                "reject", "--config", str(self.config_path), "--question", "86",
                "--reviewer", "source-reviewer", "--reason", "The textbook solution begins before the visible continuation.",
            ]),
            0,
        )

        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["run", "--config", str(self.config_path)]), PENDING_VISION_EXIT)

        ledger = AuditLedger(self.work_root, 7)
        for number, reason in (
            (85, "The numbered textbook solution is absent."),
            (86, "The textbook solution begins before the visible continuation."),
        ):
            record = ledger.record(number)
            self.assertEqual(record.status, "reviewed_rejection")
            self.assertEqual(record.reviewer, "source-reviewer")
            self.assertEqual(record.rejection_reason, reason)
        queue = [
            json.loads(line)
            for line in (self.work_root / "chapter-007" / "extraction-jobs.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual([item["job_id"] for item in queue], ["extract-ch07-q0084"])
        job_files = sorted(path.name for path in (self.work_root / "chapter-007" / "extraction-jobs").glob("*.json"))
        self.assertEqual(job_files, ["extract-ch07-q0084.json"])

    def test_changed_source_evidence_reopens_reviewed_rejection_as_source_blocked(self) -> None:
        evidence = [self._evidence_for_question(
            84,
            source_status="missing_solution",
            source_reasons=("textbook_solution_missing: No numbered solution is printed.",),
            requires_reviewed_rejection=True,
        )]
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main([
            "reject", "--config", str(self.config_path), "--question", "84",
            "--reviewer", "source-reviewer", "--reason", "The numbered textbook solution is absent.",
        ]), 0)

        changed = [replace(
            evidence[0],
            source_reasons=("textbook_solution_missing: The reviewed source evidence changed.",),
            dependency_fingerprint="f" * 64,
        )]
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=changed):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)

        record = AuditLedger(self.work_root, 7).record(84)
        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.reviewer, "")
        self.assertEqual(record.rejection_reason, "")
        self.assertEqual(record.findings, changed[0].source_reasons)

    def test_run_advances_after_every_actionable_extraction_result_is_current(self) -> None:
        evidence = self._three_record_source_evidence()
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        for number, reason in (
            (85, "The numbered textbook solution is absent."),
            (86, "The textbook solution begins before the visible continuation."),
        ):
            self.assertEqual(main([
                "reject", "--config", str(self.config_path), "--question", str(number),
                "--reviewer", "source-reviewer", "--reason", reason,
            ]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        self._write_extraction_result(
            {"question": "text", "options": {label: "text" for label in "ABCD"}, "solution": "text"},
            self.work_root / "chapter-007" / "extraction-results",
        )

        with (
            patch("textbook_chapters_v2.cli._build", return_value=0),
            patch("textbook_chapters_v2.cli._render", return_value=0),
            patch("textbook_chapters_v2.cli._verify", return_value=PENDING_VISION_EXIT),
        ):
            self.assertEqual(main(["run", "--config", str(self.config_path)]), PENDING_VISION_EXIT)

        candidates = json.loads(
            (self.work_root / "chapter-007" / "state" / "candidates.json").read_text(encoding="utf-8")
        )
        self.assertEqual([item["question_number"] for item in candidates], [84])

    def test_manifest_hashes_validation_imports_and_exact_rendered_browser_identity(self) -> None:
        with patch(
            "textbook_chapters_v2.render._launch_browser",
            return_value=(object(), "Microsoft Edge (detached probe)"),
        ) as detached_probe:
            first = _application_renderer_manifest(self.config, ("playwright-chromium:124.0.2",))
        detached_probe.assert_not_called()
        application_paths = {item["path"] for item in first["application_assets"]}
        self.assertIn("question_media.py", application_paths)
        self.assertIn("chapter_repairs.py", application_paths)
        self.assertNotIn(str(self.application_root), json.dumps(first))

        (self.application_root / "question_media.py").write_text("changed question media import\n", encoding="utf-8")
        changed_import = _application_renderer_manifest(self.config, ("playwright-chromium:124.0.2",))
        self.assertNotEqual(changed_import["application_fingerprint"], first["application_fingerprint"])

        changed_browser = _application_renderer_manifest(self.config, ("microsoft-edge:123.0.1",))
        self.assertNotEqual(changed_browser["renderer_fingerprint"], changed_import["renderer_fingerprint"])
        self.assertEqual(
            changed_browser["runtime_evidence"]["selected_browser_identity"], "microsoft-edge:123.0.1"
        )
        with self.assertRaisesRegex(PipelineBlocked, "one consistent actual browser runtime"):
            _application_renderer_manifest(
                self.config, ("microsoft-edge:123.0.1", "playwright-chromium:124.0.2")
            )

    def test_changed_source_or_config_reprepares_and_invalidates_existing_extraction_result(self) -> None:
        evidence = self._prepared_evidence()
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=evidence):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        job_path = self.work_root / "chapter-007" / "extraction-jobs" / "extract-ch07-q0084.json"
        old_fingerprint = json.loads(job_path.read_text(encoding="utf-8"))["job_fingerprint"]
        results = self.work_root / "chapter-007" / "extraction-results"
        results.mkdir()
        (results / "extract-ch07-q0084.json").write_text(json.dumps({
            "job_id": "extract-ch07-q0084", "job_fingerprint": old_fingerprint,
        }), encoding="utf-8")

        self.source_pdf.write_bytes(b"replaced-textbook-pdf")
        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_config["bank_name"] = "Changed CLI Contract"
        self.config_path.write_text(json.dumps(raw_config), encoding="utf-8")
        changed = replace(evidence[0], source_pdf_sha256=_sha256(self.source_pdf), dependency_fingerprint="8" * 64)
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=[changed]) as prepare:
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main([
            "ingest-extraction", "--config", str(self.config_path), "--results", str(results),
        ]), PENDING_VISION_EXIT)

        self.assertEqual(prepare.call_count, 1)
        self.assertNotEqual(json.loads(job_path.read_text(encoding="utf-8"))["job_fingerprint"], old_fingerprint)

    def test_stale_extraction_result_fingerprint_remains_vision_pending(self) -> None:
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=self._prepared_evidence()):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        results = self.work_root / "chapter-007" / "extraction-results"
        results.mkdir()
        (results / "extract-ch07-q0084.json").write_text(json.dumps({
            "job_id": "extract-ch07-q0084", "job_fingerprint": "0" * 64,
        }), encoding="utf-8")

        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)

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

    def test_image_option_without_explicit_field_crop_is_quarantined(self) -> None:
        self._prepare_media_extraction()
        results = self._write_extraction_result({
            "question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "image"},
            "solution": "text",
        })

        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(results)]), BLOCKED_EXIT)
        self.assertEqual(AuditLedger(self.work_root, 7).record(84).status, "blocked")

    def test_explicit_option_media_uses_only_the_option_field_crop(self) -> None:
        self._prepare_media_extraction({
            "options": {"D": [{"role": "question", "source_index": 2}]},
        })
        results = self._write_extraction_result({
            "question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "image"},
            "solution": "text",
        })

        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(results)]), 0)
        candidate = json.loads((self.work_root / "chapter-007" / "state" / "candidates.json").read_text(encoding="utf-8"))[0]
        option_path = Path(candidate["representation"]["media"]["options"]["D"]["source_path"])
        with Image.open(option_path) as image:
            self.assertEqual(image.size, (7, 6))
        self.assertEqual(_sha256(option_path), _sha256(self.root / "option-d-only.png"))

    def test_multi_crop_question_media_combines_every_explicit_segment(self) -> None:
        self._prepare_media_extraction({
            "question": [{"role": "question", "source_index": 0}, {"role": "question", "source_index": 1}],
        })
        results = self._write_extraction_result({
            "question": "image", "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
            "solution": "text",
        })

        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(results)]), 0)
        candidate = json.loads((self.work_root / "chapter-007" / "state" / "candidates.json").read_text(encoding="utf-8"))[0]
        question_path = Path(candidate["representation"]["media"]["question"]["source_path"])
        with Image.open(question_path) as image:
            self.assertEqual(image.size, (10, 25))
            self.assertEqual(image.getpixel((2, 2)), (255, 0, 0))
            self.assertEqual(image.getpixel((2, 20)), (0, 0, 255))

    def test_solution_media_segments_do_not_depend_on_solution_step_count(self) -> None:
        self._prepare_media_extraction({
            "solution": [{"role": "solution", "source_index": 0}, {"role": "solution", "source_index": 1}],
        })
        results = self._write_extraction_result({
            "question": "text", "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
            "solution": "image",
        })

        self.assertEqual(main(["ingest-extraction", "--config", str(self.config_path), "--results", str(results)]), 0)
        candidate = json.loads((self.work_root / "chapter-007" / "state" / "candidates.json").read_text(encoding="utf-8"))[0]
        media = candidate["representation"]["media"]["solution"]
        self.assertEqual(len(media), 2)
        sizes = []
        for item in media:
            with Image.open(item["source_path"]) as image:
                sizes.append(image.size)
        self.assertEqual(sizes, [(12, 8), (12, 9)])

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

    def test_package_filters_reviewed_rejection_candidate_from_published_questions(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["question_numbers"] = [84, 85]
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.config = ChapterConfig.load(self.config_path)
        first = self._prepared_evidence()[0]
        second = replace(
            first,
            question_number=85,
            question_crops=tuple(replace(crop, question_number=85) for crop in first.question_crops),
            answer_key_crops=tuple(replace(crop, question_number=85) for crop in first.answer_key_crops),
            solution_crops=tuple(replace(crop, question_number=85) for crop in first.solution_crops),
            dependency_fingerprint="d" * 64,
        )
        with patch("textbook_chapters_v2.cli.prepare_source_evidence", return_value=[first, second]):
            self.assertEqual(main(["prepare", "--config", str(self.config_path)]), 0)
        self.assertEqual(main(["extract", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        jobs = {
            int(path.stem.rsplit("q", 1)[1]): json.loads(path.read_text(encoding="utf-8"))
            for path in (self.work_root / "chapter-007" / "extraction-jobs").glob("*.json")
        }
        results = self.root / "mixed-results"
        results.mkdir()
        for number, job in jobs.items():
            answer_hash = next(source["sha256"] for source in job["sources"] if source["role"] == "answer_key")
            (results / f"extract-ch07-q{number:04d}.json").write_text(json.dumps({
                "job_id": job["job_id"], "job_fingerprint": job["job_fingerprint"],
                "question_text": f"Question {number}", "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "B",
                "answer_key": {
                    "correct_answer": "B" if number == 84 else "D", "crop_sha256": answer_hash,
                    "job_fingerprint": job["job_fingerprint"],
                },
                "solution_steps": ["The textbook solution is reviewed."],
                "representation": {
                    "question": "text", "options": {label: "text" for label in "ABCD"}, "solution": "text",
                },
                "differences_from_legacy": [], "reviewer": "vision-extractor",
            }), encoding="utf-8")
        self.assertEqual(main([
            "ingest-extraction", "--config", str(self.config_path), "--results", str(results),
        ]), BLOCKED_EXIT)
        ledger = AuditLedger(self.work_root, 7)
        self.assertEqual(main([
            "reject", "--config", str(self.config_path), "--question", "84",
            "--reviewer", "source-reviewer", "--reason", "Cannot bypass an approved record.",
        ]), BLOCKED_EXIT)
        self.assertEqual(main([
            "reject", "--config", str(self.config_path), "--question", "85",
            "--reviewer", "source-reviewer",
            "--reason", "The source layout is not safely representable after field-level review.",
        ]), 0)
        self.assertEqual(main([
            "ingest-extraction", "--config", str(self.config_path), "--results", str(results),
        ]), 0)
        approved_sha256 = json.loads(
            (self.work_root / "chapter-007" / "state" / "candidates.json").read_text(encoding="utf-8")
        )[0]["sha256"]
        unanswered = self.root / "mixed-unanswered.png"
        submitted = self.root / "mixed-submitted.png"
        field = self.root / "mixed-field.png"
        unanswered.write_bytes(b"mixed-unanswered")
        submitted.write_bytes(b"mixed-submitted")
        field.write_bytes(b"mixed-field")
        rendered = BrowserRenderArtifacts(
            question_screenshots={"desktop": unanswered}, solution_screenshots={"desktop": submitted},
            screenshot_hashes={
                "question.desktop": _sha256(unanswered), "solution.desktop": _sha256(submitted),
                "field.unanswered.desktop.question": _sha256(field),
            },
            renderer_version="controlled-mixed-renderer",
            field_screenshots={"unanswered.desktop.question": field},
            browser_runtime="playwright-chromium:124.0.2",
        )
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=rendered):
            self.assertEqual(main(["render", "--config", str(self.config_path)]), 0)
        ledger = AuditLedger(self.work_root, 7)
        approved_record = replace(
            ledger.record(84),
            status="approved_for_publish",
            candidate_sha256=approved_sha256,
            reviewer="independent-vision-reviewer",
            findings=(),
            field_verdicts={
                "question": "pass", "options.A": "pass", "options.B": "pass", "options.C": "pass",
                "options.D": "pass", "answer_mapping": "pass", "solution": "pass",
                "readability": "pass", "clipping": "pass",
            },
        )
        ledger.merge_record(replace(
            approved_record, dependency_fingerprint=approval_dependency_fingerprint(approved_record)
        ))

        self.assertEqual(main(["package", "--config", str(self.config_path)]), 0)
        with zipfile.ZipFile(self.candidate) as archive:
            questions = [json.loads(line) for line in archive.read("questions/ch07.jsonl").decode("utf-8").splitlines()]
            rejection = json.loads(archive.read("metadata/rejected-questions.jsonl").decode("utf-8"))
        self.assertEqual([item["key"] for item in questions], ["ch07-q0084"])
        self.assertEqual(rejection["question_number"], 85)

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
        candidate_state = self.work_root / "chapter-007" / "state" / "candidates.json"
        candidate_payload = json.loads(candidate_state.read_text(encoding="utf-8"))
        expected_source_fingerprint = candidate_payload[0]["source_fingerprint"]
        candidate_payload[0]["source_fingerprint"] = "0" * 64
        candidate_state.write_text(json.dumps(candidate_payload), encoding="utf-8")
        self.assertEqual(main(["build", "--config", str(self.config_path)]), 0)
        self.assertEqual(
            json.loads(candidate_state.read_text(encoding="utf-8"))[0]["source_fingerprint"],
            expected_source_fingerprint,
        )

        unanswered = self.root / "unanswered.png"
        submitted = self.root / "submitted.png"
        question_field = self.root / "question-field.png"
        unanswered.write_bytes(b"unanswered-render")
        submitted.write_bytes(b"submitted-render")
        question_field.write_bytes(b"question-field-render")
        rendered = BrowserRenderArtifacts(
            question_screenshots={"desktop": unanswered}, solution_screenshots={"desktop": submitted},
            screenshot_hashes={
                "question.desktop": _sha256(unanswered), "solution.desktop": _sha256(submitted),
                "field.unanswered.desktop.question": _sha256(question_field),
            },
            renderer_version="controlled-real-app-renderer",
            field_screenshots={"unanswered.desktop.question": question_field},
            browser_runtime="playwright-chromium:124.0.2",
        )
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=rendered):
            self.assertEqual(main(["render", "--config", str(self.config_path)]), 0)
        render_state = json.loads(
            (self.work_root / "chapter-007" / "state" / "renders.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            render_state["application_renderer_manifest"]["runtime_evidence"]["selected_browser_identity"],
            "playwright-chromium:124.0.2",
        )
        self.assertEqual(render_state["records"][0]["artifacts"]["browser_runtime"], "playwright-chromium:124.0.2")
        self.assertEqual(
            render_state["records"][0]["artifacts"]["observed_renderer_version"],
            "controlled-real-app-renderer",
        )
        self.assertIn(_sha256(question_field), AuditLedger(self.work_root, 7).record(84).asset_hashes)
        self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        verification_job = json.loads(
            (self.work_root / "chapter-007" / "verification-jobs.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        field_sources = [source for source in verification_job["sources"] if source["kind"] == "field_render"]
        self.assertEqual(field_sources[0]["sha256"], _sha256(question_field))
        styles = self.application_root / "static" / "styles.css"
        styles.write_text("controlled styles changed once\n", encoding="utf-8")
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=rendered) as rerender:
            self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        self.assertEqual(rerender.call_count, 1)
        changed_verification_job = json.loads(
            (self.work_root / "chapter-007" / "verification-jobs.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertNotEqual(changed_verification_job["job_fingerprint"], verification_job["job_fingerprint"])
        verification_job = changed_verification_job
        question_field.write_bytes(b"question-field-render-refreshed")
        refreshed_render = replace(rendered, screenshot_hashes={
            "question.desktop": _sha256(unanswered), "solution.desktop": _sha256(submitted),
            "field.unanswered.desktop.question": _sha256(question_field),
        })
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=refreshed_render) as rerender:
            self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        self.assertEqual(rerender.call_count, 1)
        refreshed_verification_job = json.loads(
            (self.work_root / "chapter-007" / "verification-jobs.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertNotEqual(refreshed_verification_job["job_fingerprint"], verification_job["job_fingerprint"])
        verification_job = refreshed_verification_job
        verification_results = self.work_root / "chapter-007" / "verification-results"
        verification_results.mkdir()
        (verification_results / "verify-ch07-q0084.json").write_text(json.dumps({
            "job_id": verification_job["job_id"], "job_fingerprint": "0" * 64,
        }), encoding="utf-8")
        self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        verdicts = {field: "pass" for field in (
            "question", "options.A", "options.B", "options.C", "options.D", "answer_mapping", "solution", "readability", "clipping"
        )}
        (verification_results / "verify-ch07-q0084.json").write_text(json.dumps({
            "job_id": verification_job["job_id"], "job_fingerprint": verification_job["job_fingerprint"],
            "verdicts": verdicts, "differences": {}, "reviewer": "independent-vision-verifier",
        }), encoding="utf-8")
        self.assertEqual(main(["ingest-verification", "--config", str(self.config_path), "--results", str(verification_results)]), 0)
        (self.application_root / "static" / "branding.css").write_text(
            "controlled branding changed after approval\n", encoding="utf-8"
        )
        unanswered.write_bytes(b"unanswered-render-after-approval")
        question_field.write_bytes(b"question-field-render-after-approval")
        post_approval_render = replace(refreshed_render, browser_runtime="microsoft-edge:125.0.1", screenshot_hashes={
            "question.desktop": _sha256(unanswered), "solution.desktop": _sha256(submitted),
            "field.unanswered.desktop.question": _sha256(question_field),
        })
        with patch("textbook_chapters_v2.cli.render_candidate", return_value=post_approval_render):
            self.assertEqual(main(["render", "--config", str(self.config_path)]), 0)
        rerendered_record = AuditLedger(self.work_root, 7).record(84)
        self.assertEqual(rerendered_record.status, "pending_vision")
        self.assertIn(_sha256(question_field), rerendered_record.asset_hashes)
        self.assertEqual(main(["package", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(main(["verify", "--config", str(self.config_path)]), PENDING_VISION_EXIT)
        verification_job = json.loads(
            (self.work_root / "chapter-007" / "verification-jobs.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        (verification_results / "verify-ch07-q0084.json").write_text(json.dumps({
            "job_id": verification_job["job_id"], "job_fingerprint": verification_job["job_fingerprint"],
            "verdicts": verdicts, "differences": {}, "reviewer": "independent-vision-verifier",
        }), encoding="utf-8")
        self.assertEqual(main([
            "ingest-verification", "--config", str(self.config_path), "--results", str(verification_results),
        ]), 0)
        self.assertEqual(AuditLedger(self.work_root, 7).record(84).status, "approved_for_publish")
        render_state_path = self.work_root / "chapter-007" / "state" / "renders.json"
        original_render_state = render_state_path.read_bytes()
        original_unanswered = unanswered.read_bytes()
        mutated_state = json.loads(original_render_state)
        unanswered.write_bytes(b"regenerated-unanswered-without-reverification")
        mutated_state["records"][0]["artifacts"]["screenshot_hashes"]["question.desktop"] = _sha256(unanswered)
        mutated_state["dependency_fingerprint"] = _render_dependency_fingerprint(
            _candidates(self.config), mutated_state["application_renderer_manifest"], mutated_state["records"]
        )
        render_state_path.write_bytes(canonical_json(mutated_state))
        self.assertEqual(main(["package", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertFalse(self.candidate.exists())
        unanswered.write_bytes(original_unanswered)
        render_state_path.write_bytes(original_render_state)
        styles.write_text("controlled styles changed twice\n", encoding="utf-8")
        self.assertEqual(main(["package", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertFalse(self.candidate.exists())
        styles.write_text("controlled styles changed once\n", encoding="utf-8")
        self.assertEqual(main(["package", "--config", str(self.config_path)]), 0)
        self.assertTrue(self.candidate.is_file())
        packaged_render_state = render_state_path.read_bytes()
        runtime_changed_state = json.loads(packaged_render_state)
        changed_runtime_manifest = _application_renderer_manifest(
            self.config, ("playwright-chromium:126.0.3",)
        )
        runtime_changed_state["application_renderer_manifest"] = changed_runtime_manifest
        runtime_changed_state["records"][0]["artifacts"]["browser_runtime"] = "playwright-chromium:126.0.3"
        runtime_changed_state["records"][0]["artifacts"]["renderer_version"] = changed_runtime_manifest[
            "manifest_fingerprint"
        ]
        runtime_changed_state["dependency_fingerprint"] = _render_dependency_fingerprint(
            _candidates(self.config), changed_runtime_manifest, runtime_changed_state["records"]
        )
        render_state_path.write_bytes(canonical_json(runtime_changed_state))
        self.assertEqual(main(["promote", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertFalse(self.published.exists())
        render_state_path.write_bytes(packaged_render_state)
        renderer_contract = self.application_root / "data-engineering" / "textbook_chapters_v2" / "render.py"
        original_renderer_contract = renderer_contract.read_text(encoding="utf-8")
        renderer_contract.write_text("controlled renderer contract changed\n", encoding="utf-8")
        self.assertEqual(main(["promote", "--config", str(self.config_path)]), BLOCKED_EXIT)
        self.assertFalse(self.published.exists())
        renderer_contract.write_text(original_renderer_contract, encoding="utf-8")
        self.assertEqual(main(["promote", "--config", str(self.config_path)]), 0)
        self.assertEqual(_sha256(self.published), _sha256(self.candidate))


if __name__ == "__main__":
    unittest.main()
