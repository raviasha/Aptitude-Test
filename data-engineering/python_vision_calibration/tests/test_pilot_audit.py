from __future__ import annotations

import hashlib
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

from python_vision_calibration.audit import portable_pilot_audit, write_pilot_audit
from python_vision_calibration.merge import merge_final_candidates
from python_vision_calibration.tests import test_merge as merge_tests
from textbook_chapters_v2.models import CropBox, PipelineBlocked
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
        review = self.accept_review if route.decision == "ACCEPT_PYTHON" else self.vision_review
        vision_jobs = (
            {self.vision_job.record_id: self.vision_job}
            if route.decision == "VISION_REQUIRED"
            else {}
        )
        return write_pilot_audit(
            (self.baseline,),
            (route,),
            results,
            candidates,
            renders,
            self.root,
            agent_jobs={self.agent_job.record_id: self.agent_job},
            agent_results={review.record_id: review},
            vision_jobs=vision_jobs,
            evidence=self.evidence if route.decision == "VISION_REQUIRED" else (),
            expected_record_ids=self.expected_ids,
        )

    def with_manifest_hashes(self, manifest: dict[str, object]) -> dict[str, object]:
        core = {
            key: value
            for key, value in manifest.items()
            if key not in {"dependency_fingerprint", "manifest_sha256"}
        }
        dependency = dependency_fingerprint(core)
        with_dependency = {**core, "dependency_fingerprint": dependency}
        return {**with_dependency, "manifest_sha256": dependency_fingerprint(with_dependency)}

    def make_render_manifest(self, candidate, *, complete: bool = True) -> dict[str, object]:
        screenshots: dict[str, dict[str, dict[str, str]]] = {}
        for width, height in ((1024, 768), (1600, 900)):
            viewport = f"{width}x{height}"
            screenshots[viewport] = {}
            for state in ("unanswered", "submitted"):
                path = self.root / "renders" / f"{viewport}-{state}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{viewport}:{state}".encode("ascii"))
                screenshots[viewport][state] = {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
        return self.with_manifest_hashes({
            "record_id": self.baseline.record_id,
            "candidate_sha256": candidate.sha256,
            "complete": complete,
            "renderer_fingerprint": "6" * 64,
            "application_fingerprint": "7" * 64,
            "browser_fingerprint": "8" * 64,
            "browser_identity": "playwright-chromium:124.0.2",
            "viewports": [[1024, 768], [1600, 900]],
            "screenshots": screenshots,
            "findings": [],
        })

    def test_quarantine_is_excluded_but_audited(self) -> None:
        candidates = self.merge(self.vision_route, (self.quarantine_result,))
        audit = write_pilot_audit(
            (self.baseline,),
            (self.vision_route,),
            (self.quarantine_result,),
            candidates,
            (),
            self.root,
            agent_jobs={self.agent_job.record_id: self.agent_job},
            agent_results={self.vision_review.record_id: self.vision_review},
            vision_jobs={self.vision_job.record_id: self.vision_job},
        )

        self.assertEqual(candidates, ())
        self.assertEqual(audit.records[0]["status"], "QUARANTINED")
        self.assertEqual(audit.records[0]["final_candidate"], None)
        self.assertEqual(audit.records[0]["vision_result"]["result_sha256"], self.quarantine_result.result_sha256)
        self.assertEqual(audit.records[0]["vision_job"]["job_sha256"], self.vision_job.job_sha256)
        self.assertEqual(audit.records[0]["agent_result"]["confidence"], self.vision_review.confidence)
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
        self.assertEqual(audit.records[0]["agent_job"]["job_sha256"], self.agent_job.job_sha256)
        self.assertEqual(audit.records[0]["agent_result"]["checks"]["logic"], "PASS")

    def test_portable_audit_is_compact_path_free_and_hash_bound_to_full_authority(self) -> None:
        candidates = self.merge(self.accept_route)
        manifest = self.make_render_manifest(candidates[0])
        audit = self.write_audit(self.accept_route, (), candidates, (manifest,))
        full = json.loads(audit.path.read_text(encoding="utf-8"))

        portable = portable_pilot_audit(full)
        portable_bytes = json.dumps(
            portable, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")

        self.assertNotIn(str(self.root).encode("ascii"), portable_bytes)
        self.assertLess(len(portable_bytes), len(audit.path.read_bytes()) // 3)
        self.assertEqual(portable["full_audit_sha256"], audit.sha256)
        self.assertEqual(portable["full_audit_dependency_fingerprint"], audit.dependency_fingerprint)
        self.assertEqual(portable["records"][0]["record_id"], self.baseline.record_id)
        self.assertEqual(portable["records"][0]["agent"]["confidence"], self.accept_review.confidence)
        self.assertEqual(portable["records"][0]["render_manifest_sha256"], manifest["manifest_sha256"])
        core = {key: value for key, value in portable.items() if key != "dependency_fingerprint"}
        self.assertEqual(portable["dependency_fingerprint"], dependency_fingerprint(core))

    def test_python_only_audit_does_not_require_source_image_evidence(self) -> None:
        candidates = merge_final_candidates(
            (self.baseline,), (self.accept_route,), (), (),
            vision_jobs={}, expected_record_ids=self.expected_ids,
        )
        audit = write_pilot_audit(
            (self.baseline,), (self.accept_route,), (), candidates, (), self.root,
            agent_jobs={self.agent_job.record_id: self.agent_job},
            agent_results={self.accept_review.record_id: self.accept_review},
            vision_jobs={}, evidence=(), expected_record_ids=self.expected_ids,
        )
        self.assertEqual(audit.counts["python_accepts"], 1)
        self.assertEqual(audit.counts["vision_routes"], 0)
        self.assertEqual(
            tuple(audit.records[0]["source_evidence"]["baseline_source_hashes"]["source_pdf"]),
            tuple(self.baseline.source_hashes["source_pdf"]),
        )

    def test_render_manifests_are_the_single_input_that_advances_status(self) -> None:
        candidates = self.merge(self.accept_route)
        pending = self.write_audit(self.accept_route, (), candidates)
        manifest = self.make_render_manifest(candidates[0])

        rendered = self.write_audit(self.accept_route, (), candidates, (manifest,))

        self.assertEqual(pending.records[0]["status"], "PENDING_RENDER")
        self.assertEqual(rendered.records[0]["status"], "PYTHON_ACCEPTED")
        self.assertEqual(len(rendered.records[0]["render_hashes"]), 4)
        self.assertEqual(merge_tests.json_value(rendered.records[0]["render_manifest"]), manifest)
        self.assertNotEqual(rendered.dependency_fingerprint, pending.dependency_fingerprint)

    def test_hash_bearing_manifest_without_completion_remains_pending(self) -> None:
        candidates = self.merge(self.accept_route)
        incomplete = self.make_render_manifest(candidates[0], complete=False)

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

        stale_render = self.make_render_manifest(candidate)
        stale_render["candidate_sha256"] = "9" * 64
        stale_render = self.with_manifest_hashes(stale_render)
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
                agent_jobs={self.agent_job.record_id: self.agent_job},
                agent_results={self.accept_review.record_id: self.accept_review},
                vision_jobs={},
                evidence=self.evidence,
                expected_record_ids=self.expected_ids,
            )
        extra = {
            "record_id": "ch01-q0045",
            "candidate_sha256": candidates[0].sha256,
            "complete": True,
            "screenshots": {},
            "findings": [],
        }
        with self.assertRaisesRegex(PipelineBlocked, "extra render"):
            self.write_audit(self.accept_route, (), candidates, (extra,))

    def test_audit_rejects_missing_or_stale_full_review_dependencies(self) -> None:
        candidates = self.merge(self.accept_route)
        with self.assertRaisesRegex(PipelineBlocked, "agent job"):
            write_pilot_audit(
                (self.baseline,),
                (self.accept_route,),
                (),
                candidates,
                (),
                self.root,
                agent_jobs={},
                agent_results={self.accept_review.record_id: self.accept_review},
                vision_jobs={},
                evidence=self.evidence,
                expected_record_ids=self.expected_ids,
            )

        stale_review = replace(self.accept_review, confidence=0.50)
        with self.assertRaisesRegex(PipelineBlocked, "agent result"):
            write_pilot_audit(
                (self.baseline,),
                (self.accept_route,),
                (),
                candidates,
                (),
                self.root,
                agent_jobs={self.agent_job.record_id: self.agent_job},
                agent_results={stale_review.record_id: stale_review},
                vision_jobs={},
                evidence=self.evidence,
                expected_record_ids=self.expected_ids,
            )

    def test_render_manifest_requires_exact_current_inventory_and_hash_binding(self) -> None:
        candidates = self.merge(self.accept_route)
        manifest = self.make_render_manifest(candidates[0])
        one_viewport = dict(manifest)
        one_viewport["viewports"] = [[1024, 768]]
        one_viewport = self.with_manifest_hashes(one_viewport)
        with self.assertRaisesRegex(PipelineBlocked, "viewport"):
            self.write_audit(self.accept_route, (), candidates, (one_viewport,))

        screenshot_path = Path(manifest["screenshots"]["1024x768"]["unanswered"]["path"])
        screenshot_path.write_bytes(b"changed after manifest")
        with self.assertRaisesRegex(PipelineBlocked, "screenshot"):
            self.write_audit(self.accept_route, (), candidates, (manifest,))

    def test_audit_rejects_substituted_vision_evidence_with_unchanged_crop_bytes(self) -> None:
        candidates = self.merge(self.vision_route, (self.vision_result,))
        original = self.evidence[0].question_crops[0]
        substituted_crop = replace(
            original,
            page_number=999,
            box=CropBox(11, 21, 101, 121),
            source_image_sha256="9" * 64,
            source_dpi=181,
        )
        substituted = self.with_evidence_fingerprint(
            replace(self.evidence[0], question_crops=(substituted_crop,))
        )

        with self.assertRaisesRegex(PipelineBlocked, "vision job|current source evidence"):
            write_pilot_audit(
                (self.baseline,),
                (self.vision_route,),
                (self.vision_result,),
                candidates,
                (),
                self.root,
                agent_jobs={self.agent_job.record_id: self.agent_job},
                agent_results={self.vision_review.record_id: self.vision_review},
                vision_jobs={self.vision_job.record_id: self.vision_job},
                evidence=(substituted,),
                expected_record_ids=self.expected_ids,
            )

    def test_audit_rejects_acceptance_from_a_requires_quarantine_job(self) -> None:
        restricted_evidence, restricted_job = self.make_restricted_evidence_and_job()
        accepted = self.with_result_hash(
            replace(self.vision_result, job_sha256=restricted_job.job_sha256)
        )

        with self.assertRaisesRegex(PipelineBlocked, "requires quarantine"):
            write_pilot_audit(
                (self.baseline,),
                (self.vision_route,),
                (accepted,),
                (),
                (),
                self.root,
                agent_jobs={self.agent_job.record_id: self.agent_job},
                agent_results={self.vision_review.record_id: self.vision_review},
                vision_jobs={restricted_job.record_id: restricted_job},
                evidence=(restricted_evidence,),
                expected_record_ids=self.expected_ids,
            )

    def test_evidence_less_audit_rejects_self_rehashed_noncanonical_vision_jobs(self) -> None:
        substituted_prompt = "A substituted evidence-less quarantine prompt."
        forged_jobs = (
            self.with_job_hash(
                replace(self.vision_job, source_dependency_fingerprint="9" * 64)
            ),
            self.with_job_hash(
                replace(
                    self.vision_job,
                    prompt=substituted_prompt,
                    prompt_sha256=hashlib.sha256(substituted_prompt.encode("utf-8")).hexdigest(),
                )
            ),
        )
        for forged_job in forged_jobs:
            with self.subTest(job_sha256=forged_job.job_sha256):
                forged_result = self.with_result_hash(
                    replace(self.quarantine_result, job_sha256=forged_job.job_sha256)
                )
                with self.assertRaisesRegex(PipelineBlocked, "vision job"):
                    write_pilot_audit(
                        (self.baseline,),
                        (self.vision_route,),
                        (forged_result,),
                        (),
                        (),
                        self.root,
                        agent_jobs={self.agent_job.record_id: self.agent_job},
                        agent_results={self.vision_review.record_id: self.vision_review},
                        vision_jobs={forged_job.record_id: forged_job},
                        expected_record_ids=self.expected_ids,
                    )

    def test_evidence_less_audit_rejects_quarantine_candidate_content(self) -> None:
        malformed = self.with_result_hash(replace(self.quarantine_result, question_text="forbidden content"))

        with self.assertRaisesRegex(PipelineBlocked, "quarantine.*candidate|candidate.*quarantine"):
            write_pilot_audit(
                (self.baseline,),
                (self.vision_route,),
                (malformed,),
                (),
                (),
                self.root,
                agent_jobs={self.agent_job.record_id: self.agent_job},
                agent_results={self.vision_review.record_id: self.vision_review},
                vision_jobs={self.vision_job.record_id: self.vision_job},
                expected_record_ids=self.expected_ids,
            )


if __name__ == "__main__":
    unittest.main()
