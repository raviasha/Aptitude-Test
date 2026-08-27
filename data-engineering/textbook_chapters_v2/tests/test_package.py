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
from textbook_chapters_v2.candidates import assemble_candidate
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CropBox, PipelineBlocked, RecordEvidence, SourceCrop
from textbook_chapters_v2.package import build_candidate_package


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
        extraction: dict[str, object] = {
            "question_text": "The remainder when 7⁸⁴ is divided by 342 is",
            "options": {"A": "0", "B": "1", "C": "49", "D": "341"},
            "correct_answer": "B",
            "answer_key": {"correct_answer": "B", "crop_sha256": self.answer_crop.sha256, "job_fingerprint": "d" * 64},
            "solution_steps": ["7⁸⁴ = (7³)²⁸ = 343²⁸.", "Therefore, the remainder is 1."],
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            },
            "source_fingerprint": "d" * 64,
        }
        extraction.update(changes)
        return extraction

    def _approved_audit(self, **changes: object) -> dict[str, object]:
        audit: dict[str, object] = {
            "all_records_terminal": True,
            "all_records_approved_or_reviewed_rejection": True,
            "reviewed_rejections": [],
        }
        audit.update(changes)
        return audit

    def test_assembly_keeps_known_math_text_and_answer_key_is_independent(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])

        self.assertEqual(candidate.question_text, "The remainder when 7⁸⁴ is divided by 342 is")
        self.assertEqual(candidate.correct_answer, "B")
        self.assertIn("7⁸⁴", " ".join(candidate.solution_steps))

        disagreeing = self._extraction(answer_key={"correct_answer": "D", "crop_sha256": self.answer_crop.sha256, "job_fingerprint": "d" * 64})
        with self.assertRaisesRegex(PipelineBlocked, "answer-key"):
            assemble_candidate(self.evidence, disagreeing, [])

        wrong_crop = self._extraction(answer_key={"correct_answer": "B", "crop_sha256": "0" * 64, "job_fingerprint": "d" * 64})
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

        build_candidate_package(self.config, [candidate], self._approved_audit(), path)

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

        with self.assertRaisesRegex(PipelineBlocked, "terminal"):
            build_candidate_package(
                self.config,
                [candidate],
                self._approved_audit(all_records_terminal=False),
                path,
            )

        build_candidate_package(self.config, [candidate], self._approved_audit(), path)
        original = path.read_bytes()
        with self.assertRaisesRegex(PipelineBlocked, "existing"):
            build_candidate_package(self.config, [candidate], self._approved_audit(), path)
        self.assertEqual(path.read_bytes(), original)

    def test_build_refuses_a_pending_candidate_even_when_summary_claims_terminal(self) -> None:
        pending = replace(assemble_candidate(self.evidence, self._extraction(), []), status="pending_vision")

        with self.assertRaisesRegex(PipelineBlocked, "pending"):
            build_candidate_package(self.config, [pending], self._approved_audit(), self.root / "pending.zip")

    def test_build_requires_terminal_reviewed_rejection_metadata(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        invalid_rejections = (
            {"status": "pending_vision", "reviewer": "reviewer", "rejection_reason": "reason"},
            {"status": "reviewed_rejection", "reviewer": "", "rejection_reason": "reason"},
            {"status": "reviewed_rejection", "reviewer": "reviewer", "rejection_reason": ""},
        )

        for index, rejection in enumerate(invalid_rejections):
            with self.subTest(rejection=rejection):
                audit = self._approved_audit(reviewed_rejections=[rejection])
                with self.assertRaisesRegex(PipelineBlocked, "reviewed rejection"):
                    build_candidate_package(self.config, [candidate], audit, self.root / f"bad-rejection-{index}.zip")

    def test_build_revalidates_complete_representation_and_media_for_direct_candidate(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        bypassed = replace(candidate, representation={"question": "image", "options": {"A": "text", "B": "text", "C": "text", "D": "text"}, "solution": "text", "media": {}})

        with self.assertRaisesRegex(PipelineBlocked, "display media"):
            build_candidate_package(self.config, [bypassed], self._approved_audit(), self.root / "bypassed.zip")

        without_answer_key = replace(candidate, answer_key_crop_sha256="")
        with self.assertRaisesRegex(PipelineBlocked, "answer-key"):
            build_candidate_package(self.config, [without_answer_key], self._approved_audit(), self.root / "missing-answer-key.zip")

    def test_build_does_not_clobber_destination_created_after_validation(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        path = self.root / "raced.zip"
        original_validate = package_module._validate_written_package

        def create_competing_destination(*args: object, **kwargs: object) -> None:
            original_validate(*args, **kwargs)
            path.write_bytes(b"published package")

        with patch.object(package_module, "_validate_written_package", side_effect=create_competing_destination):
            with self.assertRaisesRegex(PipelineBlocked, "existing"):
                build_candidate_package(self.config, [candidate], self._approved_audit(), path)
        self.assertEqual(path.read_bytes(), b"published package")

    def test_unchanged_builds_are_byte_deterministic_and_parse_as_v3(self) -> None:
        candidate = assemble_candidate(self.evidence, self._extraction(), [])
        first = self.root / "first.zip"
        second = self.root / "second.zip"

        first_result = build_candidate_package(self.config, [candidate], self._approved_audit(), first)
        second_result = build_candidate_package(self.config, [candidate], self._approved_audit(), second)

        self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), hashlib.sha256(second.read_bytes()).hexdigest())
        self.assertEqual(first_result.sha256, second_result.sha256)
        self.assertEqual(first_result.manifest["format_version"], 3)
        self.assertEqual(first_result.question_count, 1)
        with first.open("rb") as package:
            _, questions, _, version = app.parse_question_package(package)
        self.assertEqual((len(questions), version), (1, 3))


if __name__ == "__main__":
    unittest.main()
