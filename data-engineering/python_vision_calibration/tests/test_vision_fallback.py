from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.models import RouteDecision, VisionFallbackJob, VisionFallbackResult
from python_vision_calibration.vision_fallback import (
    create_vision_fallback_job,
    create_vision_fallback_jobs,
    ingest_vision_fallback_result,
    ingest_vision_fallback_results,
)
from textbook_chapters_v2.models import CropBox, RecordEvidence, SourceCrop


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class VisionFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.crop_root = self.root / "crops"
        self.crop_root.mkdir()
        self.evidence = (self.record_evidence(44),)
        self.accept_route = self.route("ACCEPT_PYTHON")
        self.vision_route = self.route("VISION_REQUIRED")
        self.job = create_vision_fallback_job(
            self.vision_route,
            self.evidence[0],
            self.root / "vision" / "jobs" / "ch01-q0044.json",
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def route(self, decision: str, *, record_id: str = "ch01-q0044") -> RouteDecision:
        candidate = {
            "question_text": "UNTRUSTED PYTHON CANDIDATE MUST NOT CROSS THE BOUNDARY",
            "options": {"A": "old A", "B": "old B", "C": "old C", "D": "old D"},
            "correct_answer": "A",
            "solution_steps": ["old solution"],
        }
        reason_codes = ("AMBIGUOUS_NOTATION",) if decision == "VISION_REQUIRED" else ()
        route_sha256 = canonical_sha256({
            "record_id": record_id,
            "decision": decision,
            "candidate": candidate,
            "baseline_sha256": "1" * 64,
            "review_result_sha256": "2" * 64,
            "reason_codes": list(reason_codes),
        })
        return RouteDecision(
            record_id=record_id,
            decision=decision,
            candidate=candidate,
            baseline_sha256="1" * 64,
            review_result_sha256="2" * 64,
            reason_codes=reason_codes,
            route_sha256=route_sha256,
        )

    def crop(self, role: str, number: int, suffix: str) -> SourceCrop:
        path = self.crop_root / f"q{number:04d}-{suffix}.png"
        path.write_bytes(f"{role}:{number}:{suffix}".encode("ascii"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return SourceCrop(
            role=role,
            question_number=number,
            page_number=25,
            box=CropBox(10, 20, 100, 120),
            path=path,
            width=90,
            height=100,
            sha256=digest,
            source_image_sha256="4" * 64,
            source_dpi=180,
        )

    def record_evidence(self, number: int, **overrides: object) -> RecordEvidence:
        values: dict[str, object] = {
            "chapter": 1,
            "question_number": number,
            "source_pdf": PROJECT_ROOT / "data-engineering" / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf",
            "source_pdf_sha256": "0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf",
            "question_crops": (self.crop("question", number, "question"),),
            "answer_key_crops": (self.crop("answer_key", number, "answer"),),
            "solution_crops": (self.crop("solution", number, "solution"),),
            "dependency_fingerprint": "5" * 64,
        }
        values.update(overrides)
        return RecordEvidence(**values)

    def write(self, payload: dict[str, object], name: str = "ch01-q0044.json") -> Path:
        path = self.root / "results" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(payload), encoding="utf-8")
        return path

    def with_result_hash(self, payload: dict[str, object]) -> dict[str, object]:
        payload = dict(payload)
        payload["result_sha256"] = canonical_sha256(payload)
        return payload

    def valid_vision_result(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_id": self.job.record_id,
            "decision": "VISION_ACCEPTED",
            "question_text": "What is 2 + 2?",
            "options": {"A": "3", "B": "4", "C": "5", "D": "6"},
            "correct_answer": "B",
            "solution_steps": ["Adding 2 and 2 gives 4."],
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text"},
                "solution": "text",
            },
            "media": {
                "question": [],
                "options": {"A": [], "B": [], "C": [], "D": []},
                "solution": [],
            },
            "source_evidence_sha256s": list(self.job.source_evidence_sha256s),
            "quarantine_reason": "",
            "reviewer": "vision-test-reviewer",
            "route_sha256": self.job.route_sha256,
            "job_sha256": self.job.job_sha256,
        }
        return self.with_result_hash(payload)

    def valid_quarantine_result(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_id": self.job.record_id,
            "decision": "QUARANTINE",
            "question_text": "",
            "options": {},
            "correct_answer": "",
            "solution_steps": [],
            "representation": {},
            "media": {},
            "source_evidence_sha256s": [self.job.source_evidence_sha256s[0]],
            "quarantine_reason": "The printed source does not support a complete solution.",
            "reviewer": "vision-test-reviewer",
            "route_sha256": self.job.route_sha256,
            "job_sha256": self.job.job_sha256,
        }
        return self.with_result_hash(payload)

    def test_only_vision_routes_emit_jobs(self) -> None:
        jobs = create_vision_fallback_jobs((self.accept_route, self.vision_route), self.evidence, self.root)

        self.assertEqual(tuple(jobs), ("ch01-q0044",))

    def test_job_does_not_supply_a_proposed_correction(self) -> None:
        job = next(iter(create_vision_fallback_jobs((self.vision_route,), self.evidence, self.root).values()))
        payload = json.loads(job.output_path.read_text(encoding="utf-8"))

        self.assertNotIn("candidate", payload)
        self.assertNotIn("suggested", payload)
        self.assertNotIn("UNTRUSTED PYTHON CANDIDATE", canonical_json(payload))
        self.assertIn("complete question, all options, printed answer, and full solution", payload["prompt"])
        self.assertIn("untrusted textbook data", payload["prompt"])
        self.assertIn("printed question number", payload["prompt"])

    def test_vision_accept_requires_every_record_field(self) -> None:
        payload = self.valid_vision_result()
        del payload["options"]["D"]  # type: ignore[index]
        payload = self.with_result_hash({key: value for key, value in payload.items() if key != "result_sha256"})

        with self.assertRaisesRegex(ValueError, "options"):
            ingest_vision_fallback_result(self.job, self.write(payload))

    def test_quarantine_requires_source_reason_and_evidence_hash(self) -> None:
        payload = self.valid_quarantine_result()
        payload["source_evidence_sha256s"] = []
        payload = self.with_result_hash({key: value for key, value in payload.items() if key != "result_sha256"})

        with self.assertRaisesRegex(ValueError, "evidence"):
            ingest_vision_fallback_result(self.job, self.write(payload))

    def test_valid_accepted_result_is_hash_bound_and_immutable(self) -> None:
        result = ingest_vision_fallback_result(self.job, self.write(self.valid_vision_result()))

        self.assertIsInstance(result, VisionFallbackResult)
        self.assertEqual(result.decision, "VISION_ACCEPTED")
        self.assertEqual(result.options["B"], "4")
        with self.assertRaises(TypeError):
            result.options["B"] = "mutated"  # type: ignore[index]

    def test_result_rejects_stale_route_job_source_and_result_hashes(self) -> None:
        cases = {
            "route": ("route_sha256", "0" * 64),
            "job": ("job_sha256", "0" * 64),
            "source": ("source_evidence_sha256s", ["0" * 64]),
            "result": ("result_sha256", "0" * 64),
        }
        for expected, (field, value) in cases.items():
            with self.subTest(field=field):
                payload = self.valid_vision_result()
                payload[field] = value
                if field != "result_sha256":
                    payload = self.with_result_hash({key: item for key, item in payload.items() if key != "result_sha256"})
                with self.assertRaisesRegex(ValueError, expected):
                    ingest_vision_fallback_result(self.job, self.write(payload))

    def test_image_representation_requires_role_appropriate_current_media(self) -> None:
        payload = self.valid_vision_result()
        payload["representation"]["question"] = "image"  # type: ignore[index]
        payload = self.with_result_hash({key: value for key, value in payload.items() if key != "result_sha256"})
        with self.assertRaisesRegex(ValueError, "media.question"):
            ingest_vision_fallback_result(self.job, self.write(payload))

        payload = self.valid_vision_result()
        payload["representation"]["question"] = "image"  # type: ignore[index]
        payload["media"]["question"] = [self.job.role_sha256s["question"][0]]  # type: ignore[index]
        payload = self.with_result_hash({key: value for key, value in payload.items() if key != "result_sha256"})
        accepted = ingest_vision_fallback_result(self.job, self.write(payload))
        self.assertEqual(accepted.media["question"], (self.job.role_sha256s["question"][0],))

    def test_incomplete_or_ambiguous_source_can_only_quarantine(self) -> None:
        incomplete = self.record_evidence(
            44,
            solution_crops=(),
            source_status="reviewed_missing",
            source_reasons=("The textbook prints no complete solution.",),
            requires_reviewed_rejection=True,
        )
        job = create_vision_fallback_job(self.vision_route, incomplete, self.root / "incomplete-job.json")
        payload = self.valid_vision_result()
        payload.update({
            "source_evidence_sha256s": list(job.source_evidence_sha256s),
            "route_sha256": job.route_sha256,
            "job_sha256": job.job_sha256,
        })
        payload = self.with_result_hash({key: value for key, value in payload.items() if key != "result_sha256"})

        with self.assertRaisesRegex(ValueError, "quarantine"):
            ingest_vision_fallback_result(job, self.write(payload, "incomplete.json"))

    def test_jobs_reject_missing_duplicate_or_mismatched_source_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing"):
            create_vision_fallback_jobs((self.vision_route,), (), self.root)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            create_vision_fallback_jobs((self.vision_route,), self.evidence * 2, self.root)
        with self.assertRaisesRegex(ValueError, "question number"):
            create_vision_fallback_job(self.vision_route, self.record_evidence(45), self.root / "mismatch.json")

    def test_job_rejects_crop_whose_declared_hash_does_not_match_bytes(self) -> None:
        stale_crop = replace(self.evidence[0].question_crops[0], sha256="0" * 64)
        evidence = replace(self.evidence[0], question_crops=(stale_crop,))

        with self.assertRaisesRegex(ValueError, "crop hash"):
            create_vision_fallback_job(self.vision_route, evidence, self.root / "stale.json")

    def test_one_shot_route_iterable_cannot_hide_an_invalid_route_value(self) -> None:
        routes = (item for item in (self.vision_route, object()))

        with self.assertRaisesRegex(TypeError, "RouteDecision"):
            create_vision_fallback_jobs(routes, self.evidence, self.root)

    def test_ingestion_revalidates_full_job_source_metadata_and_crop_bytes(self) -> None:
        changed_sources = [dict(source) for source in self.job.sources]
        changed_sources[0]["page_number"] = 999
        stale_metadata_job = replace(self.job, sources=tuple(changed_sources))
        with self.assertRaisesRegex(ValueError, "job hash"):
            ingest_vision_fallback_result(stale_metadata_job, self.write(self.valid_vision_result()))

        self.evidence[0].question_crops[0].path.write_bytes(b"changed after job creation")
        with self.assertRaisesRegex(ValueError, "source crop hash"):
            ingest_vision_fallback_result(self.job, self.write(self.valid_vision_result()))

    def test_directory_ingestion_is_resumable_and_writes_only_terminal_results(self) -> None:
        later_route = self.route("VISION_REQUIRED", record_id="ch01-q0045")
        later_evidence = self.record_evidence(45)
        jobs = create_vision_fallback_jobs((self.vision_route, later_route), (*self.evidence, later_evidence), self.root)
        results_dir = self.root / "vision-results"
        results_dir.mkdir()
        first_payload = self.valid_vision_result()
        (results_dir / "ch01-q0044.json").write_text(canonical_json(first_payload), encoding="utf-8")

        pending = ingest_vision_fallback_results((self.vision_route, later_route), jobs, results_dir, self.root)
        self.assertEqual(pending["pending"], 1)
        self.assertFalse((self.root / "vision" / "vision-results.jsonl").exists())

        later_job = jobs["ch01-q0045"]
        later_payload = self.valid_quarantine_result()
        later_payload.update({
            "record_id": later_job.record_id,
            "source_evidence_sha256s": [later_job.source_evidence_sha256s[0]],
            "route_sha256": later_job.route_sha256,
            "job_sha256": later_job.job_sha256,
        })
        later_payload = self.with_result_hash({key: value for key, value in later_payload.items() if key != "result_sha256"})
        (results_dir / "ch01-q0045.json").write_text(canonical_json(later_payload), encoding="utf-8")

        complete = ingest_vision_fallback_results((self.vision_route, later_route), jobs, results_dir, self.root)
        output = self.root / "vision" / "vision-results.jsonl"
        self.assertEqual(complete["vision_accepted"], 1)
        self.assertEqual(complete["quarantined"], 1)
        self.assertEqual(complete["pending"], 0)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)

        (results_dir / "ch01-q0045.json").unlink()
        resumed = ingest_vision_fallback_results((self.vision_route, later_route), jobs, results_dir, self.root)
        self.assertEqual(resumed["pending"], 1)
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)

    def test_directory_ingestion_blocks_unexpected_and_noncanonical_results(self) -> None:
        jobs = {self.job.record_id: self.job}
        results_dir = self.root / "directory-results"
        results_dir.mkdir()
        (results_dir / "unexpected.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            ingest_vision_fallback_results((self.vision_route,), jobs, results_dir, self.root)

        (results_dir / "unexpected.json").unlink()
        (results_dir / "ch01-q0044.JSON").write_text(canonical_json(self.valid_vision_result()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            ingest_vision_fallback_results((self.vision_route,), jobs, results_dir, self.root)


if __name__ == "__main__":
    unittest.main()
