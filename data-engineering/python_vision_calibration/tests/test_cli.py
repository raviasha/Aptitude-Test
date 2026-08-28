from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from python_vision_calibration import cli
from python_vision_calibration.models import RawBaselineRecord
from python_vision_calibration.models import RouteDecision
from textbook_chapters_v2.models import CandidateRecord, PackageResult, PipelineBlocked


class CliContractTests(unittest.TestCase):
    CONFIG = Path(__file__).resolve().parents[1] / "configs" / "chapter-001-agent-triage.json"

    def test_main_rejects_an_unknown_command_with_one_json_object(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = cli.main(["unknown"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, 22)
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["command"], "unknown")
        self.assertEqual(payload["stage"], "configuration")
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(payload["pending_jobs"], 0)
        self.assertTrue(stderr.getvalue().strip())

    def test_production_inventory_is_exactly_chapter_one_questions_1_to_380(self) -> None:
        self.assertEqual(len(cli.EXPECTED_RECORD_IDS), 380)
        self.assertEqual(cli.EXPECTED_RECORD_IDS[0], "ch01-q0001")
        self.assertEqual(cli.EXPECTED_RECORD_IDS[-1], "ch01-q0380")

    def test_approved_config_resolves_all_paths_from_workspace_not_cwd(self) -> None:
        config = cli.load_pilot_config(self.CONFIG)

        self.assertEqual(config.chapter, 1)
        self.assertEqual(config.question_numbers, (1, 380))
        self.assertEqual(config.viewports, ((1024, 768), (1600, 900)))
        self.assertEqual(config.agent_prompt_version, "chapter1-agent-triage-v1")
        self.assertEqual(config.agent_accept_confidence, 0.95)
        for path in (
            config.source_pdf,
            config.v2_source_config,
            config.work_root,
            config.candidate_path,
            config.published_path,
        ):
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_relative_to(config.workspace_root))
        self.assertEqual(
            config.candidate_path.relative_to(config.workspace_root).as_posix(),
            "question-banks/candidates/agent-triage/ch01_number_system_candidate.zip",
        )

    def test_config_rejects_any_declared_pilot_drift_before_mutation(self) -> None:
        original = json.loads(self.CONFIG.read_text(encoding="utf-8"))
        mutations = {
            "chapter": 2,
            "question_numbers": [1, 379],
            "viewports": [[1024, 768]],
            "agent_prompt_version": "changed",
            "agent_accept_confidence": 0.94,
            "candidate_path": original["published_path"],
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                payload = dict(original)
                payload[field] = value
                with self.assertRaises(cli.InvalidPilotInput):
                    cli.validate_pilot_config_payload(payload, workspace_root=cli.WORKSPACE_ROOT)


class CliWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        source_workspace = cli.WORKSPACE_ROOT
        for relative in (
            cli.APPROVED_PATHS["source_pdf"],
            cli.APPROVED_PATHS["published_path"],
        ):
            source = source_workspace / relative
            destination = self.workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, destination)
            except OSError:
                shutil.copyfile(source, destination)
        v2_relative = cli.APPROVED_PATHS["v2_source_config"]
        v2_destination = self.workspace / v2_relative
        v2_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_workspace / v2_relative, v2_destination)
        self.config = self.workspace / "data-engineering/python_vision_calibration/configs/chapter-001-agent-triage.json"
        self.config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CliContractTests.CONFIG, self.config)
        self.work_root = self.workspace / cli.APPROVED_PATHS["work_root"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, command: str, config: Path | None = None) -> tuple[int, dict[str, object], str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = cli.main(
            [command, "--config", str(config or self.config)],
            stdout=stdout,
            stderr=stderr,
            workspace_root=self.workspace,
        )
        return code, json.loads(stdout.getvalue()), stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def synthetic_baselines() -> tuple[RawBaselineRecord, ...]:
        records = []
        source_hash = cli.APPROVED_SOURCE_SHA256
        for number, record_id in enumerate(cli.EXPECTED_RECORD_IDS, start=1):
            identity = {
                "number": number,
                "question_region_sha256": f"{number:064x}",
                "pdf_sha256": source_hash,
                "page": 23,
                "box": [1.0, 2.0, 3.0, 4.0],
            }
            source_hashes = {
                "source_pdf": (source_hash,),
                "raw_extractor": ("1" * 64,),
                "config": ("2" * 64,),
                "question": (f"{number:064x}",),
                "answer": (f"{number + 400:064x}",),
                "solution": (f"{number + 800:064x}",),
                "question_identity": (f"{number:064x}",),
            }
            candidate = {
                "question_text": f"Question {number}?",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "A",
                "solution_steps": ["The displayed answer is 1."],
            }
            fingerprint = cli.canonical_sha256({
                "record_id": record_id,
                "chapter": 1,
                "source_hashes": source_hashes,
                "source_identity": identity,
                "candidate": candidate,
            })
            records.append(RawBaselineRecord(
                record_id=record_id,
                chapter=1,
                source_hashes=source_hashes,
                candidate=candidate,
                baseline_sha256=fingerprint,
                source_identity=identity,
            ))
        return tuple(records)

    def invoke_with_synthetic_extract(self, command: str) -> tuple[int, dict[str, object], str, str]:
        with mock.patch.object(cli, "build_raw_baseline", return_value=self.synthetic_baselines()):
            return self.invoke(command)

    def seed_agent_result(self, record_id: str, *, decision: str = "ACCEPT_PYTHON") -> Path:
        job_path = self.work_root / "agent-review/jobs" / f"{record_id}.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        result = {
            "record_id": record_id,
            "decision": decision,
            "confidence": 0.99,
            "checks": {
                "rendering": "PASS" if decision == "ACCEPT_PYTHON" else "SUSPECT",
                "structure": "PASS",
                "logic": "PASS",
                "cross_field_consistency": "PASS",
            },
            "reason_codes": [] if decision == "ACCEPT_PYTHON" else ["AMBIGUOUS_NOTATION"],
            "explanation": "All displayed fields are coherent." if decision == "ACCEPT_PYTHON" else "Notation is ambiguous.",
            "reviewer": "test-coding-agent",
            "baseline_sha256": job["baseline_sha256"],
            "job_sha256": job["job_sha256"],
        }
        result["result_sha256"] = cli.canonical_sha256(result, ensure_ascii=False)
        path = self.work_root / "agent-review-results" / f"{record_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return path

    def test_status_on_clean_work_root_is_strictly_read_only(self) -> None:
        before = sorted(path.relative_to(self.workspace).as_posix() for path in self.workspace.rglob("*"))

        code, payload, raw, diagnostics = self.invoke("status")

        after = sorted(path.relative_to(self.workspace).as_posix() for path in self.workspace.rglob("*"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["stage"], "not_prepared")
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["pending_jobs"], 0)
        self.assertEqual(raw.count("\n"), 1)
        self.assertEqual(diagnostics, "")
        self.assertEqual(after, before)
        self.assertFalse(self.work_root.exists())

    def test_results_option_is_rejected_for_commands_without_external_ingestion(self) -> None:
        for command in ("status", "prepare", "prepare-vision", "run"):
            with self.subTest(command=command):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = cli.main(
                    [command, "--config", str(self.config), "--results", str(self.work_root / "unused")],
                    stdout=stdout,
                    stderr=stderr,
                    workspace_root=self.workspace,
                )
                self.assertEqual(code, 22)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "invalid")
                self.assertTrue(stderr.getvalue().strip())
                self.assertFalse(self.work_root.exists())

    def test_duplicate_cli_options_are_invalid_before_mutation(self) -> None:
        cases = (
            ["status", "--config", str(self.config), "--config", str(self.config)],
            [
                "ingest-agent", "--config", str(self.config),
                "--results", str(self.work_root / "agent-review-results"),
                "--results", str(self.work_root / "agent-review-results"),
            ],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = cli.main(
                    arguments, stdout=stdout, stderr=stderr, workspace_root=self.workspace
                )
                self.assertEqual(code, 22)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "invalid")
                self.assertFalse(self.work_root.exists())

    def test_changed_source_or_published_bytes_block_without_creating_work(self) -> None:
        for relative in (cli.APPROVED_PATHS["source_pdf"], cli.APPROVED_PATHS["published_path"]):
            with self.subTest(relative=relative):
                path = self.workspace / relative
                original = path.read_bytes()
                path.unlink()
                path.write_bytes(b"changed")
                try:
                    code, payload, raw, diagnostics = self.invoke("prepare")
                    self.assertEqual(code, 21)
                    self.assertEqual(payload["status"], "blocked")
                    self.assertEqual(raw.count("\n"), 1)
                    self.assertTrue(diagnostics.strip())
                    self.assertFalse(self.work_root.exists())
                finally:
                    path.write_bytes(original)

    def test_v2_source_config_drift_is_invalid_and_does_not_mutate(self) -> None:
        v2_path = self.workspace / cli.APPROVED_PATHS["v2_source_config"]
        raw = json.loads(v2_path.read_text(encoding="utf-8"))
        raw["question_numbers"] = [1, 379]
        v2_path.write_text(json.dumps(raw), encoding="utf-8")

        code, payload, _, diagnostics = self.invoke("prepare")

        self.assertEqual(code, 22)
        self.assertEqual(payload["status"], "invalid")
        self.assertTrue(diagnostics.strip())
        self.assertFalse(self.work_root.exists())

    def test_symlink_escape_is_rejected_before_mutation(self) -> None:
        outside = self.workspace.parent / f"{self.workspace.name}-outside"
        outside.mkdir(exist_ok=True)
        work_parent = self.workspace / "tmp/python-vision-calibration"
        work_parent.mkdir(parents=True, exist_ok=True)
        link = work_parent / "chapter-001-agent-triage"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows account cannot create symlinks.")
        try:
            code, payload, _, _ = self.invoke("status")
            self.assertEqual(code, 22)
            self.assertEqual(payload["status"], "invalid")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            link.unlink(missing_ok=True)
            outside.rmdir()

    def test_clean_run_stops_at_agent_boundary_with_380_jobs(self) -> None:
        code, payload, raw, diagnostics = self.invoke_with_synthetic_extract("run")

        self.assertEqual(code, 20)
        self.assertEqual(payload["stage"], "agent_review")
        self.assertEqual(payload["status"], "pending_external")
        self.assertEqual(payload["pending_jobs"], 380)
        self.assertEqual(payload["total_baselines"], 380)
        self.assertEqual(len(list((self.work_root / "agent-review/jobs").glob("*.json"))), 380)
        self.assertEqual(raw.count("\n"), 1)
        self.assertEqual(diagnostics, "")

    def test_one_current_agent_result_resumes_at_379_without_rewriting_it(self) -> None:
        self.invoke_with_synthetic_extract("run")
        result_path = self.seed_agent_result("ch01-q0001")
        before = result_path.read_bytes(), result_path.stat().st_mtime_ns

        code, payload, _, _ = self.invoke_with_synthetic_extract("run")

        self.assertEqual(code, 20)
        self.assertEqual(payload["pending_jobs"], 379)
        self.assertEqual(payload["agent_terminal"], 1)
        self.assertEqual((result_path.read_bytes(), result_path.stat().st_mtime_ns), before)

    def test_extra_malformed_or_case_variant_agent_results_fail_closed(self) -> None:
        self.invoke_with_synthetic_extract("run")
        cases = {
            "extra": ("other.json", "{}"),
            "malformed": ("ch01-q0001.json", "{"),
            "case": ("CH01-Q0001.json", "{}"),
        }
        for label, (filename, content) in cases.items():
            with self.subTest(label=label):
                directory = self.work_root / "agent-review-results"
                if directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(parents=True)
                (directory / filename).write_text(content, encoding="utf-8")
                code, payload, _, diagnostics = self.invoke_with_synthetic_extract("ingest-agent")
                self.assertEqual(code, 22)
                self.assertEqual(payload["status"], "invalid")
                self.assertTrue(diagnostics.strip())

    def test_agent_result_inventory_rejects_non_json_files_and_directories(self) -> None:
        self.invoke_with_synthetic_extract("run")
        directory = self.work_root / "agent-review-results"
        for label, create in (
            ("temporary-file", lambda: (directory / "ch01-q0001.json.tmp").write_text("{}")),
            ("nested-directory", lambda: (directory / "nested").mkdir()),
        ):
            with self.subTest(label=label):
                if directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(parents=True)
                create()
                code, payload, _, diagnostics = self.invoke_with_synthetic_extract("ingest-agent")
                self.assertEqual(code, 22)
                self.assertEqual(payload["status"], "invalid")
                self.assertTrue(diagnostics.strip())

    def test_prepare_vision_uses_all_routes_but_emits_only_exact_vision_routes(self) -> None:
        routes = tuple(
            RouteDecision(
                record_id=record_id,
                decision="VISION_REQUIRED" if number in {2, 7, 19} else "ACCEPT_PYTHON",
                candidate={},
                baseline_sha256="1" * 64,
                review_result_sha256="2" * 64,
                reason_codes=(),
                route_sha256=f"{number:064x}",
            )
            for number, record_id in enumerate(cli.EXPECTED_RECORD_IDS, start=1)
        )
        evidence = tuple(object() for _ in range(3))
        expected_jobs = {record_id: object() for record_id in ("ch01-q0002", "ch01-q0007", "ch01-q0019")}
        v2_config = object()
        with (
            mock.patch.object(
                cli, "prepare_vision_fallback_jobs", return_value=(evidence, expected_jobs)
            ) as prepare,
        ):
            observed_evidence, jobs = cli.prepare_source_and_vision_jobs(
                self._pilot_config(), v2_config, routes
            )

        self.assertIs(observed_evidence, evidence)
        self.assertEqual(jobs, expected_jobs)
        prepare.assert_called_once_with(routes, self.work_root)

    def test_cli_vision_ingest_reuses_the_single_persisted_evidence_pass(self) -> None:
        config = self._pilot_config()
        summary = {"total": 0, "vision_accepted": 0, "quarantined": 0, "pending": 0}
        with (
            mock.patch.object(cli, "ingest_vision_fallback_results", return_value=summary) as ingest,
            mock.patch.object(cli._vision_protocol, "_result_inventory", return_value={}),
        ):
            observed, results = cli._vision_progress(
                config, (), {}, (), config.work_root / "vision-results"
            )
        self.assertEqual(observed, summary)
        self.assertEqual(results, ())
        ingest.assert_called_once_with(
            (), {}, config.work_root / "vision-results", config.work_root,
            refresh_source=False,
        )

    def _pilot_config(self):
        return cli.load_pilot_config(self.config, workspace_root=self.workspace)

    def test_finalize_calls_audit_before_and_after_render_with_authoritative_mappings(self) -> None:
        config = self._pilot_config()
        baselines = self.synthetic_baselines()
        routes = tuple(
            RouteDecision(
                record_id=record.record_id,
                decision="ACCEPT_PYTHON",
                candidate=record.candidate,
                baseline_sha256=record.baseline_sha256,
                review_result_sha256="2" * 64,
                reason_codes=(),
                route_sha256=f"{number:064x}",
            )
            for number, record in enumerate(baselines, start=1)
        )
        candidates = tuple(
            CandidateRecord(chapter=1, question_number=number, sha256=f"{number:064x}")
            for number in range(1, 381)
        )
        manifests = tuple({"record_id": record_id} for record_id in cli.EXPECTED_RECORD_IDS)
        pre_audit = mock.Mock(counts={"pending_render": 380})
        post_audit = mock.Mock(counts={"pending_render": 0, "quarantined": 0})
        package = PackageResult(path=config.candidate_path, sha256="f" * 64, question_count=380)
        agent_jobs = {record_id: object() for record_id in cli.EXPECTED_RECORD_IDS}
        agent_results = {record_id: object() for record_id in cli.EXPECTED_RECORD_IDS}
        with (
            mock.patch.object(cli, "merge_final_candidates", return_value=candidates) as merge,
            mock.patch.object(cli, "write_pilot_audit", side_effect=[pre_audit, post_audit]) as audit,
            mock.patch.object(cli, "render_all_candidates", return_value=manifests) as render,
            mock.patch.object(cli, "build_pilot_candidate_package", return_value=package) as build,
            mock.patch.object(cli, "write_completion_evidence") as completion,
        ):
            result = cli.finalize_stage(
                config=config,
                v2_config=cli._validate_v2_source_config(config),
                baselines=baselines,
                routes=routes,
                agent_jobs=agent_jobs,
                agent_results=agent_results,
                evidence=(),
                vision_jobs={},
                vision_results=(),
            )

        self.assertEqual(result, package)
        self.assertEqual(audit.call_count, 2)
        self.assertEqual(audit.call_args_list[0].args[4], ())
        self.assertEqual(audit.call_args_list[1].args[4], manifests)
        self.assertEqual(audit.call_args_list[1].kwargs["expected_record_ids"], cli.EXPECTED_RECORD_IDS)
        self.assertIs(audit.call_args_list[1].kwargs["agent_jobs"], agent_jobs)
        self.assertIs(audit.call_args_list[1].kwargs["agent_results"], agent_results)
        merge.assert_called_once()
        render.assert_called_once()
        build.assert_called_once_with(mock.ANY, candidates, post_audit, config.candidate_path)
        completion.assert_called_once_with(config, post_audit, package)

    def test_finalize_blocks_without_package_when_post_render_audit_is_pending(self) -> None:
        config = self._pilot_config()
        baselines = self.synthetic_baselines()
        candidates = (CandidateRecord(chapter=1, question_number=1, sha256="1" * 64),)
        with (
            mock.patch.object(cli, "merge_final_candidates", return_value=candidates),
            mock.patch.object(cli, "write_pilot_audit", side_effect=[
                mock.Mock(counts={"pending_render": 1}),
                mock.Mock(counts={"pending_render": 1, "quarantined": 379}),
            ]),
            mock.patch.object(cli, "render_all_candidates", return_value=()),
            mock.patch.object(cli, "build_pilot_candidate_package") as build,
        ):
            with self.assertRaises(PipelineBlocked):
                cli.finalize_stage(
                    config=config,
                    v2_config=cli._validate_v2_source_config(config),
                    baselines=baselines,
                    routes=tuple(mock.Mock() for _ in range(380)),
                    agent_jobs={record_id: object() for record_id in cli.EXPECTED_RECORD_IDS},
                    agent_results={record_id: object() for record_id in cli.EXPECTED_RECORD_IDS},
                    evidence=(),
                    vision_jobs={},
                    vision_results=(),
                )
        build.assert_not_called()
        for path in cli._completion_paths(config):
            self.assertFalse(path.exists())

    def test_completion_evidence_uses_planned_paths_canonical_bytes_and_idempotent_reuse(self) -> None:
        config = self._pilot_config()
        audit_path = self.work_root / "audit/chapter-001-agent-triage.json"
        audit_path.parent.mkdir(parents=True)
        audit_payload = {"audit_sha256": "a" * 64, "records": [], "counts": {}}
        audit_path.write_bytes(cli.canonical_json_bytes(audit_payload))
        audit = mock.Mock(
            path=audit_path,
            sha256="a" * 64,
            dependency_fingerprint="b" * 64,
            counts={
                "total_baselines": 380, "python_accepts": 377, "vision_routes": 3,
                "vision_accepts": 2, "quarantined": 1, "included": 379,
                "pending_render": 0,
            },
        )
        package = PackageResult(
            path=config.candidate_path,
            sha256="c" * 64,
            question_count=379,
            manifest={
                "source_pdf_sha256": cli.APPROVED_SOURCE_SHA256,
                "agent_prompt": {"version": cli.AGENT_REVIEW_PROMPT_VERSION, "sha256": "d" * 64},
                "application_fingerprint": "e" * 64,
                "renderer_fingerprint": "f" * 64,
                "browser": {"identity": "test-browser", "fingerprint": "1" * 64},
                "records": [{"record_id": "ch01-q0001", "candidate_sha256": "2" * 64, "render_manifest_sha256": "3" * 64}],
            },
        )

        first = cli.write_completion_evidence(config, audit, package)
        audit_copy = self.workspace / "data-engineering/python_vision_calibration/audits/chapter-001-agent-triage.json"
        summary_path = self.workspace / "data-engineering/python_vision_calibration/reports/chapter-001-agent-triage-summary.json"
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (audit_copy, summary_path)
        }
        second = cli.write_completion_evidence(config, audit, package)

        self.assertEqual(first, second)
        self.assertEqual(audit_copy.read_bytes(), audit_path.read_bytes())
        self.assertEqual(summary_path.read_bytes(), cli.canonical_json_bytes(first))
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before
        )
        self.assertEqual(first["total_baselines"], 380)
        self.assertEqual(first["agent_terminal"], 380)
        self.assertEqual(first["vision_terminal"], 3)
        self.assertEqual(first["included"] + first["quarantined"], 380)
        self.assertEqual(first["published_sha256_before"], cli.APPROVED_PUBLISHED_SHA256)
        self.assertEqual(first["published_sha256_after"], cli.APPROVED_PUBLISHED_SHA256)
        self.assertEqual(first["candidate_sha256"], package.sha256)
        self.assertEqual(first["audit_sha256"], cli._sha256_path(audit_copy))
        self.assertEqual(
            first["dependency_fingerprint"],
            cli.canonical_sha256({key: value for key, value in first.items() if key != "dependency_fingerprint"}),
        )

    def test_completed_candidate_authentication_uses_current_audit_and_never_republishes(self) -> None:
        config = self._pilot_config()
        config.candidate_path.parent.mkdir(parents=True)
        config.candidate_path.write_bytes(b"existing candidate")
        candidates = (mock.Mock(),)
        audit = mock.Mock()
        package = PackageResult(
            path=config.candidate_path, sha256="a" * 64, question_count=380, manifest={}
        )
        summary = {"candidate_sha256": package.sha256}
        with (
            mock.patch.object(cli, "merge_final_candidates", return_value=candidates) as merge,
            mock.patch.object(cli, "validate_current_audit", return_value=audit) as validate_audit,
            mock.patch.object(
                cli, "authenticate_pilot_candidate_package", return_value=package
            ) as authenticate,
            mock.patch.object(cli, "validate_completion_evidence", return_value=summary),
            mock.patch.object(cli, "build_pilot_candidate_package") as publish,
        ):
            observed = cli.authenticate_completed_pilot(
                config=config,
                v2_config=cli._validate_v2_source_config(config),
                baselines=self.synthetic_baselines(),
                routes=tuple(mock.Mock() for _ in range(380)),
                agent_jobs={record_id: mock.Mock() for record_id in cli.EXPECTED_RECORD_IDS},
                agent_results={record_id: mock.Mock() for record_id in cli.EXPECTED_RECORD_IDS},
                evidence=(),
                vision_jobs={},
                vision_results=(),
            )
        self.assertEqual(observed, (package, audit, summary))
        merge.assert_called_once()
        validate_audit.assert_called_once()
        authenticate.assert_called_once()
        publish.assert_not_called()

    def test_finalize_reuses_authenticated_candidate_and_recovers_missing_evidence(self) -> None:
        config = self._pilot_config()
        index = config.work_root / "vision/vision-jobs.jsonl"
        index.parent.mkdir(parents=True)
        index.write_bytes(b"")
        (config.work_root / "vision/jobs").mkdir()
        package = PackageResult(
            path=config.candidate_path, sha256="a" * 64, question_count=380, manifest={}
        )
        audit = mock.Mock()
        agent_summary = {"accepted_python": 380, "vision_required": 0}
        vision_summary = {"total": 0, "pending": 0, "vision_accepted": 0, "quarantined": 0}
        with (
            mock.patch.object(cli, "prepare_source_and_vision_jobs", return_value=((), {})),
            mock.patch.object(cli, "_load_vision_jobs", return_value={}) as load_jobs,
            mock.patch.object(
                cli._vision_protocol, "load_persisted_vision_evidence", return_value=()
            ) as load_evidence,
            mock.patch.object(cli, "_checkpoint"),
            mock.patch.object(cli, "_vision_progress", return_value=(vision_summary, ())),
            mock.patch.object(
                cli, "authenticate_completed_pilot", return_value=(package, audit, None)
            ) as authenticate,
            mock.patch.object(cli, "write_completion_evidence", return_value={}) as recover,
            mock.patch.object(cli, "finalize_stage") as finalize,
        ):
            code, payload = cli._execute_vision_command(
                "finalize", config, cli._validate_v2_source_config(config),
                self.synthetic_baselines(), tuple(mock.Mock() for _ in range(380)),
                {record_id: mock.Mock() for record_id in cli.EXPECTED_RECORD_IDS},
                tuple(mock.Mock() for _ in range(380)), agent_summary,
                config.work_root / "vision-results",
            )
        self.assertEqual(code, 0)
        self.assertEqual(payload["stage"], "candidate_ready")
        self.assertEqual(payload["candidate_sha256"], package.sha256)
        authenticate.assert_called_once()
        load_jobs.assert_called_once()
        load_evidence.assert_called_once()
        recover.assert_called_once_with(config, audit, package)
        finalize.assert_not_called()

    def test_status_authenticates_completed_candidate_and_reports_missing_evidence_as_recovery(self) -> None:
        config = self._pilot_config()
        baseline_path = config.work_root / "baseline/chapter-001.jsonl"
        baseline_path.parent.mkdir(parents=True)
        baseline_path.write_bytes(b"placeholder")
        vision_index = config.work_root / "vision/vision-jobs.jsonl"
        vision_index.parent.mkdir(parents=True)
        vision_index.write_bytes(b"")
        config.candidate_path.parent.mkdir(parents=True)
        config.candidate_path.write_bytes(b"candidate")
        baselines = self.synthetic_baselines()
        jobs = tuple(mock.Mock(record_id=record_id) for record_id in cli.EXPECTED_RECORD_IDS)
        results = {record_id: mock.Mock() for record_id in cli.EXPECTED_RECORD_IDS}
        routes = tuple(mock.Mock() for _ in cli.EXPECTED_RECORD_IDS)
        package = PackageResult(
            path=config.candidate_path, sha256="a" * 64, question_count=380, manifest={}
        )
        with (
            mock.patch.object(cli, "_load_baselines", return_value=baselines),
            mock.patch.object(cli, "_load_agent_jobs", return_value=jobs),
            mock.patch.object(cli, "_agent_progress", return_value=(
                {"pending": 0, "accepted_python": 380, "vision_required": 0}, results, routes
            )),
            mock.patch.object(cli, "_load_vision_jobs", return_value={}),
            mock.patch.object(cli._vision_protocol, "load_persisted_vision_evidence", return_value=()),
            mock.patch.object(cli, "_load_vision_results_read_only", return_value=()),
            mock.patch.object(
                cli, "authenticate_completed_pilot",
                return_value=(package, mock.Mock(), {"candidate_sha256": package.sha256}),
            ),
        ):
            ready = cli._status(config)
        self.assertEqual(ready["stage"], "candidate_ready")
        self.assertEqual(ready["candidate_sha256"], package.sha256)

        with (
            mock.patch.object(cli, "_load_baselines", return_value=baselines),
            mock.patch.object(cli, "_load_agent_jobs", return_value=jobs),
            mock.patch.object(cli, "_agent_progress", return_value=(
                {"pending": 0, "accepted_python": 380, "vision_required": 0}, results, routes
            )),
            mock.patch.object(cli, "_load_vision_jobs", return_value={}),
            mock.patch.object(cli._vision_protocol, "load_persisted_vision_evidence", return_value=()),
            mock.patch.object(cli, "_load_vision_results_read_only", return_value=()),
            mock.patch.object(
                cli, "authenticate_completed_pilot", return_value=(package, mock.Mock(), None)
            ),
        ):
            recovery = cli._status(config)
        self.assertEqual(recovery["stage"], "candidate_recovery")
        self.assertEqual(recovery["status"], "recovery_needed")

    def test_all_380_agent_results_produce_exactly_the_three_requested_vision_routes(self) -> None:
        self.invoke_with_synthetic_extract("run")
        vision_ids = {"ch01-q0002", "ch01-q0007", "ch01-q0019"}
        for record_id in cli.EXPECTED_RECORD_IDS:
            self.seed_agent_result(
                record_id,
                decision="VISION_REQUIRED" if record_id in vision_ids else "ACCEPT_PYTHON",
            )

        code, payload, _, diagnostics = self.invoke("ingest-agent")

        self.assertEqual(code, 0)
        self.assertEqual(payload["agent_terminal"], 380)
        self.assertEqual(payload["vision_required"], 3)
        self.assertEqual(diagnostics, "")
        rows = [
            json.loads(line)
            for line in (self.work_root / "routing/agent-routes.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(rows), 380)
        self.assertEqual(
            {row["record_id"] for row in rows if row["decision"] == "VISION_REQUIRED"},
            vision_ids,
        )

    def test_main_rejects_malformed_extra_and_case_variant_vision_results(self) -> None:
        self.invoke_with_synthetic_extract("run")
        for record_id in cli.EXPECTED_RECORD_IDS:
            self.seed_agent_result(
                record_id, decision="VISION_REQUIRED" if record_id == "ch01-q0001" else "ACCEPT_PYTHON"
            )
        self.invoke("ingest-agent")
        index = self.work_root / "vision/vision-jobs.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b"\n")
        fake_job = mock.Mock(record_id="ch01-q0001", question_number=1, job_sha256="1" * 64)
        fake_evidence = mock.Mock(question_number=1)
        directory = self.work_root / "vision-results"
        cases = {
            "extra": ("other.json", "{}"),
            "case": ("CH01-Q0001.json", "{}"),
            "malformed": ("ch01-q0001.json", "{"),
        }
        for label, (filename, content) in cases.items():
            with self.subTest(label=label):
                if directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(parents=True)
                (directory / filename).write_text(content)
                with (
                    mock.patch.object(cli, "prepare_source_and_vision_jobs", return_value=((fake_evidence,), {"ch01-q0001": fake_job})),
                    mock.patch.object(cli, "_load_vision_jobs", return_value={"ch01-q0001": fake_job}),
                    mock.patch.object(cli._vision_protocol, "load_persisted_vision_evidence", return_value=(fake_evidence,)),
                    mock.patch.object(cli, "_checkpoint"),
                    mock.patch.object(cli, "ingest_vision_fallback_results", return_value={
                        "total": 1, "pending": 0, "vision_accepted": 0, "quarantined": 0,
                    }),
                ):
                    code, payload, _, diagnostics = self.invoke("ingest-vision")
                self.assertEqual(code, 22)
                self.assertEqual(payload["status"], "invalid")
                self.assertTrue(diagnostics.strip())

    def test_zero_vision_routes_use_empty_canonical_inventory_through_main_and_status(self) -> None:
        self.invoke_with_synthetic_extract("run")
        for record_id in cli.EXPECTED_RECORD_IDS:
            self.seed_agent_result(record_id)
        self.invoke("ingest-agent")

        prepare_code, prepare_payload, _, prepare_diagnostics = self.invoke("prepare-vision")
        status_code, status_payload, _, status_diagnostics = self.invoke("status")
        ingest_code, ingest_payload, _, ingest_diagnostics = self.invoke("ingest-vision")

        self.assertEqual(prepare_code, 0, prepare_diagnostics)
        self.assertEqual(status_code, 0, status_diagnostics)
        self.assertEqual(ingest_code, 0, ingest_diagnostics)
        self.assertEqual(prepare_payload["vision_required"], 0)
        self.assertEqual(status_payload["stage"], "ready_to_finalize")
        self.assertEqual(ingest_payload["vision_terminal"], 0)
        self.assertTrue((self.work_root / "vision/jobs").is_dir())
        self.assertEqual((self.work_root / "vision/vision-jobs.jsonl").read_bytes(), b"")
        self.assertEqual((self.work_root / "vision/vision-results.jsonl").read_bytes(), b"")
        self.assertFalse((self.work_root / "vision/source-evidence").exists())

    def test_status_does_not_rewrite_any_existing_pilot_artifact(self) -> None:
        self.invoke_with_synthetic_extract("run")
        self.seed_agent_result("ch01-q0001")
        self.invoke("ingest-agent")

        def inventory():
            return {
                path.relative_to(self.work_root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.work_root.rglob("*")
                if path.is_file()
            }

        before = inventory()
        code, payload, _, _ = self.invoke("status")
        after = inventory()
        self.assertEqual(code, 0)
        self.assertEqual(payload["pending_jobs"], 379)
        self.assertEqual(after, before)

    def test_library_stdout_is_redirected_to_stderr_and_never_corrupts_json(self) -> None:
        def noisy_extract(*args, **kwargs):
            print("incidental extractor output")
            return self.synthetic_baselines()

        with mock.patch.object(cli, "build_raw_baseline", side_effect=noisy_extract):
            code, payload, raw, diagnostics = self.invoke("prepare")

        self.assertEqual(code, 0)
        self.assertEqual(payload["stage"], "prepared")
        self.assertEqual(raw.count("\n"), 1)
        self.assertIn("incidental extractor output", diagnostics)

    def test_python_module_status_subprocess_prints_one_json_object(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "data-engineering"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "python_vision_calibration",
                "status",
                "--config",
                "data-engineering/python_vision_calibration/configs/chapter-001-agent-triage.json",
            ],
            cwd=cli.WORKSPACE_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(json.loads(completed.stdout)["command"], "status")

    def test_python_module_status_is_cwd_independent_with_absolute_pythonpath(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(cli.WORKSPACE_ROOT / "data-engineering")
        with tempfile.TemporaryDirectory() as unrelated:
            completed = subprocess.run(
                [
                    sys.executable, "-m", "python_vision_calibration", "status",
                    "--config", str(CliContractTests.CONFIG),
                ],
                cwd=unrelated,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertEqual(json.loads(completed.stdout)["command"], "status")


if __name__ == "__main__":
    unittest.main()
