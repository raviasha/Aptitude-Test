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

from python_vision_calibration.agent_review import create_agent_review_job
from python_vision_calibration.diagnostics import hard_warning_codes
from python_vision_calibration.models import AgentReviewResult, RawBaselineRecord
from python_vision_calibration.routing import ingest_agent_review_directory, resolve_agent_route


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.clean_record = self.record()
        self.warning_record = self.record(question_text="The remainder when 7 84 is divided by 342 is")
        self.accept_result = self.review_result(self.clean_record)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def record(self, **candidate_overrides: object) -> RawBaselineRecord:
        candidate: dict[str, object] = {
            "question_text": "What is 2 + 2?",
            "options": {"A": "3", "B": "4", "C": "5", "D": "6"},
            "correct_answer": "B",
            "solution_steps": ["2 + 2 = 4."],
            "baseline_failures": [],
        }
        candidate.update(candidate_overrides)
        return RawBaselineRecord(
            record_id="ch01-q0044",
            chapter=1,
            source_hashes={
                "source_pdf": ("a" * 64,),
                "raw_extractor": ("b" * 64,),
                "config": ("c" * 64,),
                "question": ("d" * 64,),
                "answer": ("e" * 64,),
                "solution": ("f" * 64,),
                "question_identity": ("0" * 64,),
            },
            candidate=candidate,
            baseline_sha256="d" * 64,
            source_identity={
                "number": 44,
                "question_region_sha256": "0" * 64,
                "pdf_sha256": "a" * 64,
                "page": 25,
                "box": [20.0, 100.0, 314.0, 160.0],
            },
        )

    def review_result(self, record: RawBaselineRecord, *, decision: str = "ACCEPT_PYTHON") -> AgentReviewResult:
        return AgentReviewResult(
            record_id=record.record_id,
            decision=decision,
            confidence=0.99 if decision == "ACCEPT_PYTHON" else 0.60,
            checks={
                "rendering": "PASS" if decision == "ACCEPT_PYTHON" else "SUSPECT",
                "structure": "PASS",
                "logic": "PASS",
                "cross_field_consistency": "PASS",
            },
            reason_codes=() if decision == "ACCEPT_PYTHON" else ("AMBIGUOUS_NOTATION",),
            explanation="The result is internally coherent." if decision == "ACCEPT_PYTHON" else "Notation is ambiguous.",
            reviewer="codex-test-reviewer",
            baseline_sha256=record.baseline_sha256,
            job_sha256="b" * 64,
            result_sha256="c" * 64,
        )

    def valid_result_payload(self, job: object, *, decision: str = "ACCEPT_PYTHON") -> dict[str, object]:
        payload: dict[str, object] = {
            "record_id": job.record_id,
            "decision": decision,
            "confidence": 0.99 if decision == "ACCEPT_PYTHON" else 0.60,
            "checks": {
                "rendering": "PASS" if decision == "ACCEPT_PYTHON" else "SUSPECT",
                "structure": "PASS",
                "logic": "PASS",
                "cross_field_consistency": "PASS",
            },
            "reason_codes": [] if decision == "ACCEPT_PYTHON" else ["AMBIGUOUS_NOTATION"],
            "explanation": "The result is internally coherent." if decision == "ACCEPT_PYTHON" else "Notation is ambiguous.",
            "reviewer": "codex-test-reviewer",
            "baseline_sha256": job.baseline_sha256,
            "job_sha256": job.job_sha256,
        }
        payload["result_sha256"] = canonical_sha256(payload)
        return payload

    def test_lost_power_and_empty_solution_force_vision(self) -> None:
        detached = self.record(question_text="The remainder when 7 84 is divided by 342 is")
        empty_solution = self.record(solution_steps=[], baseline_failures=["missing_solution_steps"])

        self.assertIn("AMBIGUOUS_DETACHED_DIGITS", hard_warning_codes(detached))
        self.assertIn("MISSING_SOLUTION", hard_warning_codes(empty_solution))

    def test_literal_fixture_patterns_are_routing_signals_without_rewriting(self) -> None:
        cases = (
            ("question_text", "(80) 2 - (65) 2 + 81 = ?", "AMBIGUOUS_DETACHED_DIGITS"),
            ("options", {"A": "3", "B": "4", "C": "5", "D": "2 1 x"}, "AMBIGUOUS_NOTATION"),
            ("options", {"A": "3", "B": "4", "C": "5", "D": "28700 ab 252 ba 24 12 12 ×"}, "SUSPICIOUS_TOKEN_CHAIN"),
        )
        for field, value, warning in cases:
            with self.subTest(warning=warning):
                record = self.record(**{field: value})
                self.assertIn(warning, hard_warning_codes(record))
                self.assertEqual(record.candidate[field], value)

    def test_literal_notation_patterns_do_not_expand_outside_their_fixture_fields(self) -> None:
        record = self.record(category="The remainder when 7 84 is divided by 342 is")

        self.assertNotIn("AMBIGUOUS_DETACHED_DIGITS", hard_warning_codes(record))

    def test_hard_warning_overrides_agent_accept(self) -> None:
        route = resolve_agent_route(self.warning_record, self.review_result(self.warning_record))

        self.assertEqual(route.decision, "VISION_REQUIRED")
        self.assertIn("FORCED_BY_DIAGNOSTIC", route.reason_codes)

    def test_clean_accept_preserves_candidate_exactly_and_detaches_it(self) -> None:
        before = canonical_json(self.clean_record.candidate)
        route = resolve_agent_route(self.clean_record, self.accept_result)

        self.assertEqual(route.decision, "ACCEPT_PYTHON")
        self.assertEqual(canonical_json(route.candidate), before)
        route.candidate["question_text"] = "mutated after routing"
        self.assertEqual(self.clean_record.candidate["question_text"], "What is 2 + 2?")
        self.assertNotEqual(route.route_sha256, canonical_sha256({
            "record_id": route.record_id,
            "decision": route.decision,
            "candidate": route.candidate,
            "baseline_sha256": route.baseline_sha256,
            "review_result_sha256": route.review_result_sha256,
            "reason_codes": list(route.reason_codes),
        }))

    def test_invalid_structure_unicode_and_stale_review_cannot_accept(self) -> None:
        malformed = self.record(options={"A": "3"}, correct_answer="B", question_text="Bad\ufffd text")
        warnings = hard_warning_codes(malformed)
        self.assertIn("INVALID_UNICODE", warnings)
        self.assertIn("ANSWER_LABEL_NOT_IN_OPTIONS", warnings)

        stale_review = replace(self.accept_result, baseline_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "baseline"):
            resolve_agent_route(self.clean_record, stale_review)

    def test_directory_ingestion_is_resumable_and_writes_only_complete_routes(self) -> None:
        later = replace(self.clean_record, record_id="ch01-q0045")
        jobs_root = self.root / "agent-review"
        first_job = create_agent_review_job(self.clean_record, jobs_root / "jobs" / "ch01-q0044.json")
        later_job = create_agent_review_job(later, jobs_root / "jobs" / "ch01-q0045.json")
        results_dir = self.root / "agent-review-results"
        results_dir.mkdir()
        (results_dir / "ch01-q0044.json").write_text(
            canonical_json(self.valid_result_payload(first_job)), encoding="utf-8"
        )

        pending = ingest_agent_review_directory(
            (self.clean_record, later), (first_job, later_job), results_dir, self.root
        )
        self.assertEqual(pending["pending"], 1)
        self.assertFalse((self.root / "routing" / "agent-routes.jsonl").exists())

        (results_dir / "ch01-q0045.json").write_text(
            canonical_json(self.valid_result_payload(later_job)), encoding="utf-8"
        )
        complete = ingest_agent_review_directory(
            (self.clean_record, later), (first_job, later_job), results_dir, self.root
        )
        routes_path = self.root / "routing" / "agent-routes.jsonl"
        self.assertEqual(complete["accepted_python"], 2)
        self.assertEqual(complete["pending"], 0)
        self.assertEqual(len(routes_path.read_text(encoding="utf-8").splitlines()), 2)

        (results_dir / "ch01-q0045.json").unlink()
        resumed = ingest_agent_review_directory(
            (self.clean_record, later), (first_job, later_job), results_dir, self.root
        )
        self.assertEqual(resumed["pending"], 1)
        self.assertEqual(len(routes_path.read_text(encoding="utf-8").splitlines()), 2)

    def test_directory_ingestion_blocks_unexpected_and_stale_results(self) -> None:
        job = create_agent_review_job(self.clean_record, self.root / "agent-review" / "jobs" / "ch01-q0044.json")
        results_dir = self.root / "agent-review-results"
        results_dir.mkdir()
        (results_dir / "unexpected.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "unexpected"):
            ingest_agent_review_directory((self.clean_record,), (job,), results_dir, self.root)

        (results_dir / "unexpected.json").unlink()
        stale = self.valid_result_payload(job)
        stale["baseline_sha256"] = "0" * 64
        stale["result_sha256"] = canonical_sha256({key: value for key, value in stale.items() if key != "result_sha256"})
        (results_dir / "ch01-q0044.json").write_text(canonical_json(stale), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "stale"):
            ingest_agent_review_directory((self.clean_record,), (job,), results_dir, self.root)


if __name__ == "__main__":
    unittest.main()
