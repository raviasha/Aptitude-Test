from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.audit import write_pilot_audit
from python_vision_calibration.merge import merge_final_candidates
from python_vision_calibration.tests import test_merge as merge_tests
from textbook_chapters_v2.models import PipelineBlocked
from textbook_chapters_v2.store import dependency_fingerprint


class PilotAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = merge_tests.MergeFinalCandidatesTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def __getattr__(self, name: str):
        fixture = self.__dict__.get("fixture")
        if fixture is None:
            raise AttributeError(name)
        return getattr(fixture, name)

    def write_audit(self, route, results, candidates, renders=()):
        return write_pilot_audit(
            (self.baseline,),
            (route,),
            results,
            candidates,
            renders,
            self.root,
            evidence=self.evidence,
            expected_record_ids=self.expected_ids,
        )

    def test_quarantine_is_excluded_but_audited(self) -> None:
        candidates = self.merge(self.vision_route, (self.quarantine_result,))
        audit = write_pilot_audit(
            (self.baseline,),
            (self.vision_route,),
            (self.quarantine_result,),
            candidates,
            (),
            self.root,
        )

        self.assertEqual(candidates, ())
        self.assertEqual(audit.records[0]["status"], "QUARANTINED")
        self.assertEqual(audit.records[0]["final_candidate"], None)
        self.assertEqual(audit.records[0]["vision_result"]["result_sha256"], self.quarantine_result.result_sha256)
        self.assertTrue(audit.path.is_file())

    def test_audit_is_one_canonical_hash_bound_object(self) -> None:
        candidates = self.merge(self.accept_route)
        audit = self.write_audit(self.accept_route, (), candidates)
        persisted = json.loads(audit.path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["record_count"], 1)
        self.assertEqual(persisted["expected_record_ids"], ["ch01-q0044"])
        self.assertEqual(persisted["records"][0]["record_sha256"], audit.records[0]["record_sha256"])
        self.assertEqual(persisted["audit_sha256"], audit.sha256)
        self.assertEqual(persisted["dependency_fingerprint"], audit.dependency_fingerprint)
        self.assertEqual(audit.records[0]["status"], "PENDING_RENDER")
        self.assertEqual(audit.records[0]["prompt"]["version"], "chapter1-agent-triage-v1")
        self.assertEqual(audit.records[0]["agent_result"]["result_sha256"], self.accept_route.review_result_sha256)

    def test_render_manifests_are_the_single_input_that_advances_status(self) -> None:
        candidates = self.merge(self.accept_route)
        pending = self.write_audit(self.accept_route, (), candidates)
        manifest = {
            "record_id": self.baseline.record_id,
            "candidate_sha256": candidates[0].sha256,
            "complete": True,
            "screenshot_hashes": {
                "unanswered-1024x768": "6" * 64,
                "submitted-1024x768": "7" * 64,
            },
            "findings": [],
            "renderer_fingerprint": "8" * 64,
        }

        rendered = self.write_audit(self.accept_route, (), candidates, (manifest,))

        self.assertEqual(pending.records[0]["status"], "PENDING_RENDER")
        self.assertEqual(rendered.records[0]["status"], "PYTHON_ACCEPTED")
        self.assertEqual(dict(rendered.records[0]["render_hashes"]), manifest["screenshot_hashes"])
        self.assertEqual(merge_tests.json_value(rendered.records[0]["render_manifest"]), manifest)
        self.assertNotEqual(rendered.dependency_fingerprint, pending.dependency_fingerprint)

    def test_hash_bearing_manifest_without_completion_remains_pending(self) -> None:
        candidates = self.merge(self.accept_route)
        incomplete = {
            "record_id": self.baseline.record_id,
            "candidate_sha256": candidates[0].sha256,
            "screenshot_hashes": {"unanswered-1024x768": "6" * 64},
            "findings": [],
        }

        audit = self.write_audit(self.accept_route, (), candidates, (incomplete,))

        self.assertEqual(audit.records[0]["status"], "PENDING_RENDER")

    def test_unchanged_dependencies_do_not_rewrite_existing_audit(self) -> None:
        candidates = self.merge(self.accept_route)
        first = self.write_audit(self.accept_route, (), candidates)
        sentinel = 1_600_000_000_000_000_000
        os.utime(first.path, ns=(sentinel, sentinel))

        second = self.write_audit(self.accept_route, (), candidates)

        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(second.path.stat().st_mtime_ns, sentinel)

    def test_existing_audit_must_recompute_its_declared_dependency_fingerprint(self) -> None:
        candidates = self.merge(self.accept_route)
        audit = self.write_audit(self.accept_route, (), candidates)
        forged = json.loads(audit.path.read_text(encoding="utf-8"))
        forged["dependency_fingerprint"] = "0" * 64
        forged["audit_sha256"] = dependency_fingerprint({
            key: value for key, value in forged.items() if key != "audit_sha256"
        })
        audit.path.write_text(json.dumps(forged), encoding="utf-8")

        with self.assertRaisesRegex(PipelineBlocked, "dependency fingerprint"):
            self.write_audit(self.accept_route, (), candidates)

    def test_stale_candidate_and_render_manifest_are_refused_before_rewrite(self) -> None:
        candidate = self.merge(self.accept_route)[0]
        stale_candidate = replace(candidate, sha256="9" * 64)
        with self.assertRaisesRegex(PipelineBlocked, "candidate"):
            self.write_audit(self.accept_route, (), (stale_candidate,))

        stale_render = {
            "record_id": self.baseline.record_id,
            "candidate_sha256": "9" * 64,
            "screenshot_hashes": {"unanswered": "6" * 64, "submitted": "7" * 64},
            "findings": [],
        }
        with self.assertRaisesRegex(PipelineBlocked, "render"):
            self.write_audit(self.accept_route, (), (candidate,), (stale_render,))

    def test_quarantine_cannot_become_acceptance_without_new_terminal_vision_result(self) -> None:
        quarantined = self.merge(self.vision_route, (self.quarantine_result,))
        self.write_audit(self.vision_route, (self.quarantine_result,), quarantined)
        accepted = self.merge(self.accept_route)

        with self.assertRaisesRegex(PipelineBlocked, "new terminal vision result"):
            self.write_audit(self.accept_route, (), accepted)

    def test_audit_rejects_missing_expected_records_and_extra_render_manifests(self) -> None:
        candidates = self.merge(self.accept_route)
        with self.assertRaisesRegex(PipelineBlocked, "missing"):
            write_pilot_audit(
                (self.baseline,),
                (),
                (),
                (),
                (),
                self.root,
                evidence=self.evidence,
                expected_record_ids=self.expected_ids,
            )
        extra = {
            "record_id": "ch01-q0045",
            "candidate_sha256": candidates[0].sha256,
            "screenshot_hashes": {"unanswered": "6" * 64, "submitted": "7" * 64},
            "findings": [],
        }
        with self.assertRaisesRegex(PipelineBlocked, "extra render"):
            self.write_audit(self.accept_route, (), candidates, (extra,))


if __name__ == "__main__":
    unittest.main()
