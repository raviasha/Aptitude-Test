from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.tests import test_merge as merge_tests
from textbook_chapters_v2.models import PipelineBlocked, RenderArtifacts


class RenderGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = merge_tests.MergeFinalCandidatesTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.candidate = self.fixture.merge(self.fixture.accept_route)[0]
        self.identity = "playwright-chromium:124.0.2"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_renderer(self, candidate, assets, viewports, output_dir):
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        questions = {}
        solutions = {}
        hashes = {}
        for width, height in viewports:
            viewport = f"{width}x{height}"
            for state, target in (("unanswered", questions), ("submitted", solutions)):
                path = output / f"{viewport}-{state}-card.png"
                path.write_bytes(f"{candidate.question_number}:{viewport}:{state}".encode("ascii"))
                target[viewport] = path
                prefix = "question" if state == "unanswered" else "solution"
                hashes[f"{prefix}.{viewport}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return RenderArtifacts(
            question_screenshots=questions,
            solution_screenshots=solutions,
            screenshot_hashes=hashes,
            findings=(),
            renderer_version="observed-renderer-v1",
        )

    def render(self, **changes):
        from python_vision_calibration.render_gate import render_all_candidates

        values = {
            "renderer": self.fake_renderer,
            "browser_identity_resolver": lambda: self.identity,
        }
        values.update(changes)
        return render_all_candidates((self.candidate,), {}, self.root / "work", **values)

    def test_real_adapter_contract_requests_both_states_at_exact_viewports(self) -> None:
        manifests = self.render()
        manifest = manifests[0]
        self.assertEqual(manifest["viewports"], [[1024, 768], [1600, 900]])
        self.assertEqual(set(manifest["screenshots"]), {"1024x768", "1600x900"})
        self.assertTrue(all(set(states) == {"unanswered", "submitted"} for states in manifest["screenshots"].values()))
        self.assertTrue(manifest["complete"])

    def test_complete_manifest_is_accepted_directly_by_authoritative_audit(self) -> None:
        from python_vision_calibration.audit import write_pilot_audit

        manifest = self.render()[0]
        audit = write_pilot_audit(
            (self.fixture.baseline,),
            (self.fixture.accept_route,),
            (),
            (self.candidate,),
            (manifest,),
            self.root / "audit-work",
            agent_jobs={self.fixture.agent_job.record_id: self.fixture.agent_job},
            agent_results={self.fixture.accept_review.record_id: self.fixture.accept_review},
            vision_jobs={},
            evidence=(),
            expected_record_ids=self.fixture.expected_ids,
        )
        self.assertEqual(audit.records[0]["status"], "PYTHON_ACCEPTED")

    def test_production_adapter_is_existing_v2_renderer(self) -> None:
        from python_vision_calibration.render_gate import render_all_candidates

        observed = []
        def boundary(candidate, assets, viewports, output_dir):
            observed.append(tuple(viewports))
            return self.fake_renderer(candidate, assets, viewports, output_dir)

        with patch("python_vision_calibration.render_gate.v2_render_candidate", side_effect=boundary):
            manifest = render_all_candidates(
                (self.candidate,), {}, self.root / "production-adapter",
                browser_identity_resolver=lambda: self.identity,
            )[0]
        self.assertTrue(manifest["complete"])
        self.assertEqual(observed, [((1024, 768), (1600, 900))])

    def test_current_manifest_is_reused_but_stale_runtime_fingerprints_are_not(self) -> None:
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return self.fake_renderer(*args, **kwargs)

        first = self.render(renderer=counted)
        second = self.render(renderer=counted)
        self.assertEqual(first, second)
        self.assertEqual(calls, 1)

        self.identity = "playwright-chromium:125.0.0"
        changed = self.render(renderer=counted)
        self.assertEqual(calls, 2)
        self.assertNotEqual(changed[0]["browser_fingerprint"], first[0]["browser_fingerprint"])
        self.assertNotEqual(changed[0]["renderer_fingerprint"], first[0]["renderer_fingerprint"])

    def test_application_fingerprint_ignores_only_the_non_visual_version_constant(self) -> None:
        from python_vision_calibration.render_gate import _current_fingerprints

        application = self.root / "application"
        static = application / "static"
        static.mkdir(parents=True)
        app_source = application / "app.py"
        app_source.write_text('APP_VERSION = "1.3.3"\n\ndef import_question():\n    return 1\n', encoding="utf-8")
        (application / "question_media.py").write_text("MEDIA_LIMIT = 1\n", encoding="utf-8")
        (static / "app.js").write_text("const screen = 'attempt';\n", encoding="utf-8")

        with patch("python_vision_calibration.render_gate.WORKSPACE_ROOT", application):
            first = _current_fingerprints(self.identity)[0]
            app_source.write_text(
                'APP_VERSION = "1.3.4"\n\ndef import_question():\n    return 1\n',
                encoding="utf-8",
            )
            version_only = _current_fingerprints(self.identity)[0]
            app_source.write_bytes(
                b'APP_VERSION = "1.3.4"\n\r\ndef import_question():\r\n    return 1\r\n'
            )
            version_line_ending_only = _current_fingerprints(self.identity)[0]
            app_source.write_text(
                'APP_VERSION = "1.3.4"\n\ndef import_question():\n    return 2\n',
                encoding="utf-8",
            )
            functional_change = _current_fingerprints(self.identity)[0]

        self.assertEqual(version_only, first)
        self.assertEqual(version_line_ending_only, first)
        self.assertNotEqual(functional_change, first)

    def test_missing_stale_extra_or_finding_bearing_screenshots_are_not_reused(self) -> None:
        manifest = self.render()[0]
        screenshot = Path(manifest["screenshots"]["1024x768"]["unanswered"]["path"])
        screenshot.write_bytes(b"stale")
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return self.fake_renderer(*args, **kwargs)

        refreshed = self.render(renderer=counted)[0]
        self.assertEqual(calls, 1)
        self.assertTrue(refreshed["complete"])

        for mutation in ("missing-state", "extra-state", "findings", "candidate"):
            current = self.render()[0]
            if mutation == "missing-state":
                del current["screenshots"]["1024x768"]["submitted"]
            elif mutation == "extra-state":
                current["screenshots"]["1024x768"]["other"] = dict(current["screenshots"]["1024x768"]["unanswered"])
            elif mutation == "findings":
                current["findings"] = ["horizontal-overflow"]
                current["complete"] = False
            else:
                current["candidate_sha256"] = "0" * 64
            from python_vision_calibration.render_gate import _manifest_is_current
            with self.subTest(mutation=mutation):
                self.assertFalse(_manifest_is_current(current, self.candidate))

    def test_forged_manifest_hashes_and_stale_paths_are_not_current(self) -> None:
        from python_vision_calibration.render_gate import _manifest_is_current

        manifest = self.render()[0]
        forged = dict(manifest)
        forged["dependency_fingerprint"] = "0" * 64
        self.assertFalse(_manifest_is_current(forged, self.candidate))
        forged = dict(manifest)
        forged["manifest_sha256"] = "0" * 64
        self.assertFalse(_manifest_is_current(forged, self.candidate))
        Path(manifest["screenshots"]["1600x900"]["submitted"]["path"]).unlink()
        self.assertFalse(_manifest_is_current(manifest, self.candidate))

    def test_partial_or_corrupt_render_persists_incomplete_resumable_manifest(self) -> None:
        def broken(candidate, assets, viewports, output_dir):
            output = Path(output_dir)
            output.mkdir(parents=True, exist_ok=True)
            path = output / "1024x768-unanswered-card.png"
            path.write_bytes(b"partial")
            raise RuntimeError("browser stopped")

        returned = self.render(renderer=broken)
        self.assertEqual(returned, ())
        manifest_path = self.root / "work" / "renders" / "ch01-q0044" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["findings"])
        self.assertTrue(manifest_path.is_file())

        from python_vision_calibration.audit import write_pilot_audit
        audit = write_pilot_audit(
            (self.fixture.baseline,), (self.fixture.accept_route,), (), (self.candidate,), returned,
            self.root / "partial-audit",
            agent_jobs={self.fixture.agent_job.record_id: self.fixture.agent_job},
            agent_results={self.fixture.accept_review.record_id: self.fixture.accept_review},
            vision_jobs={}, evidence=(), expected_record_ids=self.fixture.expected_ids,
        )
        self.assertEqual(audit.records[0]["status"], "PENDING_RENDER")

    def test_successful_render_with_findings_remains_audit_compatible_and_pending(self) -> None:
        def with_findings(candidate, assets, viewports, output_dir):
            rendered = self.fake_renderer(candidate, assets, viewports, output_dir)
            return RenderArtifacts(
                question_screenshots=rendered.question_screenshots,
                solution_screenshots=rendered.solution_screenshots,
                screenshot_hashes=rendered.screenshot_hashes,
                findings=("horizontal-overflow",),
                renderer_version=rendered.renderer_version,
            )

        returned = self.render(renderer=with_findings)
        self.assertEqual(len(returned), 1)
        self.assertFalse(returned[0]["complete"])
        self.assertEqual(returned[0]["findings"], ["horizontal-overflow"])
        from python_vision_calibration.audit import write_pilot_audit
        audit = write_pilot_audit(
            (self.fixture.baseline,), (self.fixture.accept_route,), (), (self.candidate,), returned,
            self.root / "findings-audit",
            agent_jobs={self.fixture.agent_job.record_id: self.fixture.agent_job},
            agent_results={self.fixture.accept_review.record_id: self.fixture.accept_review},
            vision_jobs={}, evidence=(), expected_record_ids=self.fixture.expected_ids,
        )
        self.assertEqual(audit.records[0]["status"], "PENDING_RENDER")

    def test_renderer_output_with_missing_or_extra_viewport_is_incomplete(self) -> None:
        for mutation in ("missing", "extra"):
            def malformed(candidate, assets, viewports, output_dir, mutation=mutation):
                rendered = self.fake_renderer(candidate, assets, viewports, output_dir)
                questions = dict(rendered.question_screenshots)
                if mutation == "missing":
                    questions.pop("1024x768")
                else:
                    questions["800x600"] = next(iter(questions.values()))
                return RenderArtifacts(
                    question_screenshots=questions,
                    solution_screenshots=rendered.solution_screenshots,
                    screenshot_hashes=rendered.screenshot_hashes,
                    findings=(),
                    renderer_version=rendered.renderer_version,
                )
            returned = self.render(renderer=malformed)
            with self.subTest(mutation=mutation):
                self.assertEqual(returned, ())


if __name__ == "__main__":
    unittest.main()
