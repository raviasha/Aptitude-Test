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

import app
import textbook_chapters_v2.package as package_module
from textbook_chapters_v2.audit import AuditLedger, AuditSummary, approval_dependency_fingerprint
from textbook_chapters_v2.candidates import assemble_candidate
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import AuditRecord, CandidateRecord, CropBox, PipelineBlocked, RecordEvidence, SourceCrop
from textbook_chapters_v2.package import build_candidate_package
from textbook_chapters_v2.store import dependency_fingerprint
from textbook_chapters_v2.vision import extraction_job_fingerprint


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class CandidatePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.question_crop = self._crop("question")
        self.answer_crop = self._crop("answer_key")
        self.solution_crop = self._crop("solution")
        self.evidence = RecordEvidence(
            chapter=1,
            question_number=334,
            source_pdf=self.root / "chapter-1.pdf",
            source_pdf_sha256="a" * 64,
            question_crops=(self.question_crop,),
            answer_key_crops=(self.answer_crop,),
            solution_crops=(self.solution_crop,),
            dependency_fingerprint="b" * 64,
        )
        self.config = ChapterConfig.from_dict(
            {
                "chapter": 1,
                "bank_name": "Candidate Number System",
                "question_pages": [1, 1],
                "answer_pages": [1, 1],
                "solution_pages": [1, 1],
                "question_numbers": [334, 334],
            }
        )
        self.audit_index = 0

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _crop(self, role: str) -> SourceCrop:
        path = self.root / f"{role}.png"
        path.write_bytes(_PNG)
        return SourceCrop(
            role=role,
            question_number=334,
            page_number=38,
            box=CropBox(1, 2, 30, 40),
            path=path,
            width=1,
            height=1,
            sha256=hashlib.sha256(_PNG).hexdigest(),
            source_image_sha256="c" * 64,
            source_dpi=240,
        )

    def _extraction(self, **changes: object) -> dict[str, object]:
        fingerprint = extraction_job_fingerprint(self.evidence)
        extraction: dict[str, object] = {
            "question_text": "The remainder when 7⁸⁴ is divided by 342 is",
            "options": {"A": "0", "B": "1", "C": "49", "D": "341"},
            "correct_answer": "B",
            "answer_key": {"correct_answer": "B", "crop_sha256": self.answer_crop.sha256, "job_fingerprint": fingerprint},
            "solution_steps": ["7⁸⁴ = (7³)²⁸ = 343²⁸.", "Therefore, the remainder is 1."],
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            },
            "source_fingerprint": fingerprint,
        }
        extraction.update(changes)
        return extraction

    def _audit_record(
        self,
        candidate: CandidateRecord,
        question_number: int = 334,
        verdict_option_labels: tuple[str, ...] | None = None,
    ) -> AuditRecord:
        option_labels = verdict_option_labels or tuple(candidate.options)
        record = AuditRecord(
            chapter=1,
            question_number=question_number,
            status="approved_for_publish",
            source_crop_hashes=(self.question_crop.sha256, self.answer_crop.sha256, self.solution_crop.sha256),
            candidate_sha256=candidate.sha256,
            asset_hashes=("unanswered.desktop:" + "e" * 64, "submitted.desktop:" + "f" * 64),
            policy_version=1,
            extractor_schema_version=1,
            verifier_schema_version=1,
            renderer_version="playwright-1.58.0",
            application_asset_version="ksat-ui-1.3.0",
            reviewer="independent-vision-reviewer",
            field_verdicts={
                "question": "pass",
                **{f"options.{label}": "pass" for label in option_labels},
                "answer_mapping": "pass",
                "solution": "pass",
                "readability": "pass",
                "clipping": "pass",
            },
        )
        return replace(record, dependency_fingerprint=approval_dependency_fingerprint(record))

    def _with_current_candidate_hash(self, candidate: CandidateRecord) -> CandidateRecord:
        payload = {
            "chapter": candidate.chapter,
            "question_number": candidate.question_number,
            "question_text": candidate.question_text,
            "options": dict(candidate.options),
            "correct_answer": candidate.correct_answer,
            "answer_key_answer": candidate.answer_key_answer,
            "answer_key_crop_sha256": candidate.answer_key_crop_sha256,
            "answer_key_job_fingerprint": candidate.answer_key_job_fingerprint,
            "solution_steps": list(candidate.solution_steps),
            "representation": dict(candidate.representation),
            "source_fingerprint": candidate.source_fingerprint,
        }
        return replace(candidate, sha256=dependency_fingerprint(payload))

    def _approved_audit(self, candidate: CandidateRecord, config: ChapterConfig | None = None) -> AuditSummary:
        self.audit_index += 1
        ledger = AuditLedger(self.root / f"audit-{self.audit_index}" / "work", 1)
        ledger.merge_record(self._audit_record(candidate))
        return ledger.validate_release_gate(config or self.config)

    def test_assembly_keeps_known_math_text_and_answer_key_is_independent(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])

        self.assertEqual(candidate.question_text, "The remainder when 7⁸⁴ is divided by 342 is")
        self.assertEqual(candidate.correct_answer, "B")
        self.assertIn("7⁸⁴", " ".join(candidate.solution_steps))

        fingerprint = extraction_job_fingerprint(self.evidence)
        disagreeing = self._extraction(answer_key={"correct_answer": "D", "crop_sha256": self.answer_crop.sha256, "job_fingerprint": fingerprint})
        with self.assertRaisesRegex(PipelineBlocked, "answer-key"):
            assemble_candidate(self.evidence, disagreeing, [])

        wrong_crop = self._extraction(answer_key={"correct_answer": "B", "crop_sha256": "0" * 64, "job_fingerprint": fingerprint})
        with self.assertRaisesRegex(PipelineBlocked, "answer-key crop"):
            assemble_candidate(self.evidence, wrong_crop, [])

    def test_assembly_requires_each_field_decision_and_blocks_quarantine(self) -> None:
        incomplete = self._extraction(
            representation={
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text"},
                "solution": "text",
            }
        )
        with self.assertRaisesRegex(PipelineBlocked, "option D"):
            assemble_candidate(self.evidence, incomplete, [])

        quarantined = self._extraction(
            representation={
                "question": "quarantine",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            }
        )
        with self.assertRaisesRegex(PipelineBlocked, "quarantine"):
            assemble_candidate(self.evidence, quarantined, [])

    def test_image_mode_copies_the_verified_crop_and_keeps_semantic_fallback(self) -> None:
        extraction = self._extraction(
            representation={
                "question": "image",
                "options": {"A": "text", "B": "text", "C": "text", "D": "image"},
                "solution": "image",
            },
            media_crops={
                "question": self.question_crop,
                "options": {"D": self.question_crop},
                "solution": [self.solution_crop],
            },
            alt_text={
                "question": "The remainder when seven to the power eighty-four is divided by 342",
                "options": {"D": "341"},
                "solution": ["The solution proves the remainder is one."],
            },
        )
        candidate = assemble_candidate(self.evidence, extraction, [])
        path = self.root / "candidate.zip"

        build_candidate_package(self.config, [candidate], self._approved_audit(candidate), path)

        with zipfile.ZipFile(path) as archive:
            question = json.loads(archive.read("questions/ch01.jsonl").decode("utf-8"))
            media = question["display_media"]
            question_asset = media["question"]["asset"]
            self.assertEqual(archive.read(question_asset), _PNG)
            self.assertEqual(question["question_text"], extraction["question_text"])
            self.assertEqual(media["options"]["D"]["alt_text"], "341")
            self.assertEqual(archive.read(media["solution"][0]["asset"]), _PNG)
            self.assertEqual(question_asset, f"assets/{hashlib.sha256(_PNG).hexdigest()}.png")

        with path.open("rb") as package:
            _, parsed, _, format_version = app.parse_question_package(package)
        self.assertEqual(format_version, 3)
        self.assertEqual(parsed[0]["options"]["D"], "341")

    def test_assembly_rejects_an_image_crop_not_authorized_by_this_record_evidence(self) -> None:
        foreign_crop = replace(self.question_crop, question_number=335)
        extraction = self._extraction(
            representation={
                "question": "image",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            },
            media_crops={"question": foreign_crop},
            alt_text={"question": "Verified question crop."},
        )

        with self.assertRaisesRegex(PipelineBlocked, "authorized"):
            assemble_candidate(self.evidence, extraction, [])

    def test_build_requires_terminal_audit_and_never_overwrites_an_existing_zip(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        path = self.root / "candidate.zip"

        with self.assertRaisesRegex(PipelineBlocked, "authoritative AuditSummary"):
            build_candidate_package(
                self.config,
                [candidate],
                {
                    "all_records_terminal": True,
                    "all_records_approved_or_reviewed_rejection": True,
                    "reviewed_rejections": [],
                    "audit_sha256": "0" * 64,
                },
                path,
            )

        build_candidate_package(self.config, [candidate], self._approved_audit(candidate), path)
        original = path.read_bytes()
        with self.assertRaisesRegex(PipelineBlocked, "existing"):
            build_candidate_package(self.config, [candidate], self._approved_audit(candidate), path)
        self.assertEqual(path.read_bytes(), original)

    def test_build_refuses_a_pending_candidate_even_when_summary_claims_terminal(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        pending = replace(candidate, status="pending_vision")

        with self.assertRaisesRegex(PipelineBlocked, "pending"):
            build_candidate_package(self.config, [pending], self._approved_audit(candidate), self.root / "pending.zip")

    def test_build_embeds_only_a_genuine_reviewed_rejection_from_the_ledger(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        config = replace(self.config, question_numbers=(334, 335))
        ledger = AuditLedger(self.root / "rejection-audit" / "work", 1)
        ledger.merge_record(self._audit_record(candidate))
        ledger.merge_record(
            AuditRecord(
                chapter=1,
                question_number=335,
                status="reviewed_rejection",
                reviewer="source-reviewer",
                rejection_reason="The source answer key is absent.",
            )
        )
        summary = ledger.validate_release_gate(config)
        output = self.root / "with-rejection.zip"

        result = build_candidate_package(config, [candidate], summary, output)

        self.assertEqual(result.rejected_count, 1)
        with zipfile.ZipFile(output) as archive:
            rejection = json.loads(archive.read("metadata/rejected-questions.jsonl"))
        self.assertEqual(rejection["question_number"], 335)

    def test_audit_summary_cannot_be_publicly_constructed_or_reused_after_ledger_changes(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        with self.assertRaisesRegex(TypeError, "AuditLedger"):
            AuditSummary(
                chapter=1,
                total_records=1,
                approved_count=1,
                reviewed_rejections=(),
                audit_sha256="0" * 64,
            )

        ledger = AuditLedger(self.root / "stale-summary" / "work", 1)
        approved = self._audit_record(candidate)
        ledger.merge_record(approved)
        summary = ledger.validate_release_gate(self.config)
        ledger.merge_record(replace(approved, status="pending_vision"))

        with self.assertRaisesRegex(PipelineBlocked, "audit_sha256"):
            build_candidate_package(self.config, [candidate], summary, self.root / "stale-summary.zip")

    def test_package_rejects_an_audit_summary_subclass_with_overridden_verification(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])

        class ForgedAuditSummary(AuditSummary):
            def __init__(self) -> None:
                pass

            def _verified_package_payload(self, config: object, candidates: object) -> tuple[dict[str, object], list[dict[str, object]]]:
                return (
                    {
                        "all_records_terminal": True,
                        "all_records_approved_or_reviewed_rejection": True,
                        "reviewed_rejections": [],
                        "audit_sha256": "0" * 64,
                    },
                    [],
                )

        with self.assertRaisesRegex(PipelineBlocked, "authoritative AuditSummary"):
            build_candidate_package(
                self.config,
                [candidate],
                ForgedAuditSummary(),
                self.root / "forged-subclass.zip",
            )

    def test_package_binds_audit_option_verdicts_to_actual_candidate_labels(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        five_option = self._with_current_candidate_hash(
            replace(
                candidate,
                options={**dict(candidate.options), "E": "343"},
                representation={
                    **dict(candidate.representation),
                    "options": {**dict(candidate.representation["options"]), "E": "text"},
                },
            )
        )
        cases = (
            (five_option, ("A", "B", "C", "D"), "five-missing-e"),
            (candidate, ("A", "B", "C", "D", "E"), "four-extra-e"),
        )

        for index, (actual_candidate, verdict_labels, name) in enumerate(cases):
            ledger = AuditLedger(self.root / f"option-binding-{index}" / "work", 1)
            ledger.merge_record(self._audit_record(actual_candidate, verdict_option_labels=verdict_labels))
            summary = ledger.validate_release_gate(self.config)

            with self.subTest(name=name), self.assertRaisesRegex(PipelineBlocked, "option verdict"):
                build_candidate_package(
                    self.config,
                    [actual_candidate],
                    summary,
                    self.root / f"{name}.zip",
                )

    def test_build_revalidates_complete_representation_and_media_for_direct_candidate(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        bypassed = self._with_current_candidate_hash(
            replace(candidate, representation={"question": "image", "options": {"A": "text", "B": "text", "C": "text", "D": "text"}, "solution": "text", "media": {}})
        )

        with self.assertRaisesRegex(PipelineBlocked, "display media"):
            build_candidate_package(self.config, [bypassed], self._approved_audit(bypassed), self.root / "bypassed.zip")

        without_answer_key = self._with_current_candidate_hash(replace(candidate, answer_key_crop_sha256=""))
        with self.assertRaisesRegex(PipelineBlocked, "answer-key"):
            build_candidate_package(
                self.config,
                [without_answer_key],
                self._approved_audit(without_answer_key),
                self.root / "missing-answer-key.zip",
            )

    def test_build_recomputes_candidate_fingerprint_instead_of_trusting_its_sha256_field(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        summary = self._approved_audit(candidate)
        tampered = replace(candidate, question_text="A different question with the old approved hash")

        with self.assertRaisesRegex(PipelineBlocked, "candidate fingerprint"):
            build_candidate_package(self.config, [tampered], summary, self.root / "tampered-candidate.zip")

    def test_build_does_not_clobber_destination_created_after_validation(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        path = self.root / "raced.zip"
        original_validate = package_module._validate_written_package

        def create_competing_destination(*args: object, **kwargs: object) -> None:
            original_validate(*args, **kwargs)
            path.write_bytes(b"published package")

        with patch.object(package_module, "_validate_written_package", side_effect=create_competing_destination):
            with self.assertRaisesRegex(PipelineBlocked, "existing"):
                build_candidate_package(self.config, [candidate], self._approved_audit(candidate), path)
        self.assertEqual(path.read_bytes(), b"published package")

    def test_unchanged_builds_are_byte_deterministic_and_parse_as_v3(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        first = self.root / "first.zip"
        second = self.root / "second.zip"

        first_result = build_candidate_package(self.config, [candidate], self._approved_audit(candidate), first)
        second_result = build_candidate_package(self.config, [candidate], self._approved_audit(candidate), second)

        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
        self.assertEqual(first_result.sha256, second_result.sha256)
        self.assertEqual(first_result.manifest["format_version"], 3)
        self.assertEqual(first_result.question_count, 1)
        with first.open("rb") as package:
            _, questions, _, version = app.parse_question_package(package)
        self.assertEqual((len(questions), version), (1, 3))


if __name__ == "__main__":
    unittest.main()
