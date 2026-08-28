from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.merge import merge_final_candidates
from python_vision_calibration.models import RawBaselineRecord, RouteDecision, VisionFallbackResult
from textbook_chapters_v2.models import CropBox, PipelineBlocked, RecordEvidence, SourceCrop
from textbook_chapters_v2.store import dependency_fingerprint


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


class MergeFinalCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.baseline = self.make_baseline()
        self.accept_route = self.make_route("ACCEPT_PYTHON")
        self.vision_route = self.make_route("VISION_REQUIRED")
        self.evidence = (self.make_evidence(),)
        self.vision_result = self.make_vision_result("VISION_ACCEPTED")
        self.quarantine_result = self.make_vision_result("QUARANTINE")
        self.expected_ids = {self.baseline.record_id}

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def make_baseline(self, **candidate_changes: object) -> RawBaselineRecord:
        source_hashes = {
            "source_pdf": ("a" * 64,),
            "raw_extractor": ("b" * 64,),
            "config": ("c" * 64,),
            "question": ("d" * 64,),
            "answer": ("e" * 64,),
            "solution": ("f" * 64,),
            "question_identity": ("0" * 64,),
        }
        source_identity = {
            "number": 44,
            "question_region_sha256": "0" * 64,
            "pdf_sha256": "a" * 64,
            "page": 25,
            "box": [20.0, 100.0, 314.0, 160.0],
        }
        candidate: dict[str, object] = {
            "question_text": "What is 2 + 2?  ",
            "options": {"A": "3", "B": "4", "C": "5", "D": "6"},
            "correct_answer": "B",
            "solution_steps": ["Add the two numbers.", "2 + 2 = 4."],
            "baseline_failures": [],
        }
        candidate.update(candidate_changes)
        payload = {
            "record_id": "ch01-q0044",
            "chapter": 1,
            "source_hashes": source_hashes,
            "source_identity": source_identity,
            "candidate": candidate,
        }
        return RawBaselineRecord(
            record_id="ch01-q0044",
            chapter=1,
            source_hashes=source_hashes,
            candidate=candidate,
            baseline_sha256=canonical_sha256(payload),
            source_identity=source_identity,
        )

    def make_route(self, decision: str, baseline: RawBaselineRecord | None = None) -> RouteDecision:
        baseline = baseline or self.baseline
        reason_codes = ("AMBIGUOUS_NOTATION",) if decision == "VISION_REQUIRED" else ()
        payload = {
            "record_id": baseline.record_id,
            "decision": decision,
            "candidate": baseline.candidate,
            "baseline_sha256": baseline.baseline_sha256,
            "review_result_sha256": "2" * 64,
            "reason_codes": list(reason_codes),
        }
        return RouteDecision(
            record_id=baseline.record_id,
            decision=decision,
            candidate=dict(baseline.candidate),
            baseline_sha256=baseline.baseline_sha256,
            review_result_sha256="2" * 64,
            reason_codes=reason_codes,
            route_sha256=canonical_sha256(payload),
        )

    def make_crop(self, role: str) -> SourceCrop:
        path = self.root / f"{role}.png"
        path.write_bytes(f"{role}:44".encode("ascii"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return SourceCrop(
            role=role,
            question_number=44,
            page_number=25,
            box=CropBox(10, 20, 100, 120),
            path=path,
            width=90,
            height=100,
            sha256=digest,
            source_image_sha256="4" * 64,
            source_dpi=180,
        )

    def make_evidence(self) -> RecordEvidence:
        return RecordEvidence(
            chapter=1,
            question_number=44,
            source_pdf=self.root / "source.pdf",
            source_pdf_sha256="a" * 64,
            question_crops=(self.make_crop("question"),),
            answer_key_crops=(self.make_crop("answer_key"),),
            solution_crops=(self.make_crop("solution"),),
            dependency_fingerprint="5" * 64,
        )

    def make_vision_result(
        self,
        decision: str,
        *,
        representation: dict[str, object] | None = None,
        media: dict[str, object] | None = None,
    ) -> VisionFallbackResult:
        if decision == "VISION_ACCEPTED":
            question_text = "Vision: what is 3 + 4?"
            options = {"A": "5", "B": "6", "C": "7", "D": "8"}
            correct_answer = "C"
            solution_steps = ("Vision reads 3 + 4.", "The sum is 7.")
            representation = representation or {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            }
            media = media or {
                "question": [],
                "options": {"A": [], "B": [], "C": [], "D": []},
                "solution": [],
            }
            source_hashes = tuple(
                crop.sha256
                for crops in (
                    self.evidence[0].question_crops,
                    self.evidence[0].answer_key_crops,
                    self.evidence[0].solution_crops,
                )
                for crop in crops
            )
            quarantine_reason = ""
        else:
            question_text = ""
            options = {}
            correct_answer = ""
            solution_steps = ()
            representation = {}
            media = {}
            source_hashes = (self.evidence[0].question_crops[0].sha256,)
            quarantine_reason = "The source does not support a complete record."
        payload = {
            "record_id": self.baseline.record_id,
            "decision": decision,
            "question_text": question_text,
            "options": options,
            "correct_answer": correct_answer,
            "solution_steps": list(solution_steps),
            "representation": representation,
            "media": media,
            "source_evidence_sha256s": list(source_hashes),
            "quarantine_reason": quarantine_reason,
            "reviewer": "vision-test-reviewer",
            "route_sha256": self.vision_route.route_sha256,
            "job_sha256": "3" * 64,
        }
        return VisionFallbackResult(
            record_id=self.baseline.record_id,
            decision=decision,
            question_text=question_text,
            options=options,
            correct_answer=correct_answer,
            solution_steps=solution_steps,
            representation=representation,
            media=media,
            source_evidence_sha256s=source_hashes,
            quarantine_reason=quarantine_reason,
            reviewer="vision-test-reviewer",
            route_sha256=self.vision_route.route_sha256,
            job_sha256="3" * 64,
            result_sha256=canonical_sha256(payload),
        )

    def merge(self, route: RouteDecision, results: tuple[VisionFallbackResult, ...] = ()):
        return merge_final_candidates(
            (self.baseline,),
            (route,),
            results,
            self.evidence,
            expected_record_ids=self.expected_ids,
        )

    def test_python_accept_is_byte_for_byte_baseline_content(self) -> None:
        candidate = self.merge(self.accept_route)[0]

        self.assertEqual(candidate.question_text, self.baseline.candidate["question_text"])
        self.assertEqual(dict(candidate.options), self.baseline.candidate["options"])
        self.assertEqual(list(candidate.solution_steps), self.baseline.candidate["solution_steps"])
        self.assertEqual(
            dict(candidate.representation),
            {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
                "media": {},
            },
        )

    def test_python_candidate_hash_uses_exact_packaging_fields(self) -> None:
        candidate = self.merge(self.accept_route)[0]
        source_fingerprint = dependency_fingerprint({
            "baseline_sha256": self.baseline.baseline_sha256,
            "review_result_sha256": self.accept_route.review_result_sha256,
        })
        expected_payload = {
            "chapter": 1,
            "question_number": 44,
            "question_text": self.baseline.candidate["question_text"],
            "options": self.baseline.candidate["options"],
            "correct_answer": "B",
            "answer_key_answer": "B",
            "answer_key_crop_sha256": "e" * 64,
            "answer_key_job_fingerprint": source_fingerprint,
            "solution_steps": self.baseline.candidate["solution_steps"],
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
                "media": {},
            },
            "source_fingerprint": source_fingerprint,
        }

        self.assertEqual(candidate.sha256, dependency_fingerprint(expected_payload))

    def test_vision_accept_replaces_every_semantic_and_representation_field(self) -> None:
        question_hash = self.evidence[0].question_crops[0].sha256
        result = self.make_vision_result(
            "VISION_ACCEPTED",
            representation={
                "question": "image",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            },
            media={
                "question": [question_hash],
                "options": {"A": [], "B": [], "C": [], "D": []},
                "solution": [],
            },
        )
        candidate = self.merge(self.vision_route, (result,))[0]

        self.assertEqual(candidate.question_text, result.question_text)
        self.assertEqual(dict(candidate.options), dict(result.options))
        self.assertEqual(candidate.correct_answer, result.correct_answer)
        self.assertEqual(candidate.solution_steps, result.solution_steps)
        self.assertEqual(candidate.representation["question"], "image")
        self.assertEqual(candidate.representation["media"]["question"]["source_sha256"], question_hash)
        self.assertEqual(candidate.representation["media"]["question"]["alt_text"], result.question_text)

    def test_quarantine_is_excluded(self) -> None:
        self.assertEqual(self.merge(self.vision_route, (self.quarantine_result,)), ())

    def test_python_accept_rejects_baseline_failure_without_normalizing_content(self) -> None:
        failed = self.make_baseline(baseline_failures=["lost_superscript"])
        route = self.make_route("ACCEPT_PYTHON", failed)

        with self.assertRaisesRegex(PipelineBlocked, "baseline failure"):
            merge_final_candidates(
                (failed,), (route,), (), self.evidence, expected_record_ids={failed.record_id}
            )

    def test_missing_duplicate_extra_and_cross_chapter_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(PipelineBlocked, "missing"):
            merge_final_candidates(
                (self.baseline,), (), (), self.evidence, expected_record_ids=self.expected_ids
            )
        with self.assertRaisesRegex(PipelineBlocked, "duplicate"):
            merge_final_candidates(
                (self.baseline, self.baseline),
                (self.accept_route,),
                (),
                self.evidence,
                expected_record_ids=self.expected_ids,
            )
        with self.assertRaisesRegex(PipelineBlocked, "extra vision result"):
            merge_final_candidates(
                (self.baseline,),
                (self.accept_route,),
                (self.vision_result,),
                self.evidence,
                expected_record_ids=self.expected_ids,
            )
        chapter_two = replace(self.baseline, record_id="ch02-q0044", chapter=2)
        with self.assertRaisesRegex(PipelineBlocked, "Chapter 1"):
            merge_final_candidates(
                (chapter_two,), (), (), (), expected_record_ids={chapter_two.record_id}
            )

    def test_route_candidate_and_vision_evidence_cannot_mix_provenance(self) -> None:
        changed_candidate = {**self.accept_route.candidate, "question_text": "mixed"}
        mixed_payload = {
            "record_id": self.accept_route.record_id,
            "decision": self.accept_route.decision,
            "candidate": changed_candidate,
            "baseline_sha256": self.accept_route.baseline_sha256,
            "review_result_sha256": self.accept_route.review_result_sha256,
            "reason_codes": list(self.accept_route.reason_codes),
        }
        mixed_route = replace(
            self.accept_route,
            candidate=changed_candidate,
            route_sha256=canonical_sha256(mixed_payload),
        )
        with self.assertRaisesRegex(PipelineBlocked, "mixed provenance"):
            self.merge(mixed_route)

        stale_result = replace(
            self.vision_result,
            source_evidence_sha256s=("9" * 64,),
        )
        stale_payload = {
            "record_id": stale_result.record_id,
            "decision": stale_result.decision,
            "question_text": stale_result.question_text,
            "options": dict(stale_result.options),
            "correct_answer": stale_result.correct_answer,
            "solution_steps": list(stale_result.solution_steps),
            "representation": json_value(stale_result.representation),
            "media": json_value(stale_result.media),
            "source_evidence_sha256s": list(stale_result.source_evidence_sha256s),
            "quarantine_reason": stale_result.quarantine_reason,
            "reviewer": stale_result.reviewer,
            "route_sha256": stale_result.route_sha256,
            "job_sha256": stale_result.job_sha256,
        }
        stale_result = replace(stale_result, result_sha256=canonical_sha256(stale_payload))
        with self.assertRaisesRegex(PipelineBlocked, "current source evidence"):
            self.merge(self.vision_route, (stale_result,))


if __name__ == "__main__":
    unittest.main()
