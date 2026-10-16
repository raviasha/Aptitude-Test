from __future__ import annotations

import unittest
import tempfile
import copy
import hashlib
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.run_codex_extraction_queue import DEFAULT_SCHEMA, _prompt, _run_one, _validate_result, main, normalized_pipeline_result


class CodexExtractionQueueTests(unittest.TestCase):
    def _bound_job(self, root: Path, *, diagrams: bool = False) -> dict:
        schema = DEFAULT_SCHEMA.with_name("codex-extraction-diagram-result.schema.json") if diagrams else DEFAULT_SCHEMA
        self.assertTrue(schema.is_file(), f"Missing opt-in schema: {schema.name}")
        return {
            "job_id": "extract-ch05-q0001", "job_fingerprint": "a" * 64,
            "prompt": "Bound source policy", "output_path": str(root / "extraction-jobs" / "job.json"),
            "schema_bindings": {schema.name: hashlib.sha256(schema.read_bytes()).hexdigest()},
            "sources": [{"role": "answer_key", "sha256": "b" * 64, "path": str(root / "answer.png")}],
        }

    def _result(self, job: dict, mode: str, *, fifth: bool = False) -> dict:
        return {
            "job_id": job["job_id"], "job_fingerprint": job["job_fingerprint"],
            "question_text": "Original source diagram", "options": {**dict.fromkeys("ABCD", "Source option"), "E": "Fifth option" if fifth else None},
            "correct_answer": "E" if fifth else "A",
            "answer_key": {"correct_answer": "E" if fifth else "A", "crop_sha256": "b" * 64, "job_fingerprint": job["job_fingerprint"]},
            "solution_steps": ["Printed solution"], "differences_from_legacy": [], "reviewer": "test reviewer",
            "representation": {"question": mode, "solution": mode, "options": {**dict.fromkeys("ABCD", mode), "E": mode if fifth else None}},
        }

    def test_diagram_schema_only_extends_representation_enums(self) -> None:
        expected = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        representation = expected["properties"]["representation"]["properties"]
        for field in (representation["question"], representation["solution"], *representation["options"]["properties"].values()):
            field["enum"] = ["text", "image", "quarantine"] + ([None] if None in field["enum"] else [])
        schema = DEFAULT_SCHEMA.with_name("codex-extraction-diagram-result.schema.json")
        self.assertTrue(schema.is_file(), "The separate diagram schema must exist")
        self.assertEqual(json.loads(schema.read_text(encoding="utf-8")), expected)

    def test_runner_uses_each_job_bound_schema_and_normalizes_four_or_five_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for diagrams, mode in ((False, "text"), (True, "text"), (True, "image"), (True, "quarantine")):
                for fifth in (False, True):
                    with self.subTest(diagrams=diagrams, mode=mode, fifth=fifth):
                        job = self._bound_job(root, diagrams=diagrams)
                        raw = self._result(job, mode, fifth=fifth)
                        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
                        with patch("scripts.run_codex_extraction_queue.subprocess.run", return_value=completed) as run:
                            self.assertEqual(_run_one(job, schema=None, model="test", reasoning="low", timeout=1, force=True), (job["job_id"], "completed"))
                        command = run.call_args.args[0]
                        self.assertEqual(Path(command[command.index("--output-schema") + 1]).name, next(iter(job["schema_bindings"])))
                        result = json.loads((root / "extraction-results" / f"{job['job_id']}.json").read_text(encoding="utf-8"))
                        self.assertEqual(result, normalized_pipeline_result(raw))
                        with patch("scripts.run_codex_extraction_queue.subprocess.run") as run:
                            self.assertEqual(_run_one(job, schema=None, model="test", reasoning="low", timeout=1, force=False)[1], "skipped")
                            run.assert_not_called()

    def test_default_rejects_non_text_and_diagram_mode_rejects_unknown_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for diagrams, modes in ((False, ("image", "quarantine", "latex")), (True, ("latex", "", None))):
                job = self._bound_job(Path(directory), diagrams=diagrams)
                for mode in modes:
                    for field in ("question", "solution", "A", "E"):
                        with self.subTest(diagrams=diagrams, mode=mode, field=field):
                            value = normalized_pipeline_result(self._result(job, "text", fifth=True))
                            if field in ("question", "solution"):
                                value["representation"][field] = mode
                            else:
                                value["representation"]["options"][field] = mode
                            with self.assertRaises(ValueError):
                                _validate_result(value, job)

    def test_runner_rejects_unbound_ambiguous_tampered_or_wrong_schema_before_cache_or_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_job = self._bound_job(root)
            diagram_job = self._bound_job(root, diagrams=True)
            missing = {**text_job, "schema_bindings": {}}
            ambiguous = {**text_job, "schema_bindings": {**text_job["schema_bindings"], **diagram_job["schema_bindings"]}}
            tampered = {**diagram_job, "schema_bindings": {next(iter(diagram_job["schema_bindings"])): "0" * 64}}
            alternate = DEFAULT_SCHEMA.with_name("codex-extraction-diagram-result.schema.json")
            for job, schema in ((missing, None), (ambiguous, None), (tampered, None), (text_job, alternate), (diagram_job, DEFAULT_SCHEMA)):
                with self.subTest(bindings=job["schema_bindings"], schema=schema):
                    output = root / "extraction-results" / f"{job['job_id']}.json"
                    output.parent.mkdir(exist_ok=True)
                    output.write_text(json.dumps(normalized_pipeline_result(self._result(job, "text"))), encoding="utf-8")
                    with patch("scripts.run_codex_extraction_queue.subprocess.run") as run:
                        with self.assertRaisesRegex(ValueError, "schema"):
                            _run_one(job, schema=schema, model="test", reasoning="low", timeout=1, force=False)
                        run.assert_not_called()

    def test_result_validator_requires_a_bound_schema_and_keeps_answer_and_option_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = self._bound_job(Path(directory), diagrams=True)
            valid = normalized_pipeline_result(self._result(job, "image"))
            with self.assertRaisesRegex(ValueError, "schema"):
                _validate_result(valid, {**job, "schema_bindings": {}})
            for mutation in ("fingerprint", "answer", "labels"):
                value = copy.deepcopy(valid)
                if mutation == "fingerprint":
                    value["job_fingerprint"] = "0" * 64
                elif mutation == "answer":
                    value["answer_key"]["correct_answer"] = "B"
                else:
                    del value["representation"]["options"]["D"]
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    _validate_result(value, job)

    def test_text_job_does_not_reuse_cached_image_result_with_matching_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self._bound_job(root)
            output = root / "extraction-results" / f"{job['job_id']}.json"
            output.parent.mkdir()
            output.write_text(json.dumps(normalized_pipeline_result(self._result(job, "image"))), encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, json.dumps(self._result(job, "text")), "")
            with patch("scripts.run_codex_extraction_queue.subprocess.run", return_value=completed):
                self.assertEqual(_run_one(job, schema=DEFAULT_SCHEMA, model="test", reasoning="low", timeout=1, force=False)[1], "completed")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["representation"]["question"], "text")

    def test_cli_processes_mixed_bound_queue_without_a_schema_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_job = self._bound_job(root)
            diagram_job = {**self._bound_job(root, diagrams=True), "job_id": "extract-ch05-q0002", "job_fingerprint": "c" * 64}
            queue = root / "queue.jsonl"
            queue.write_text("\n".join(json.dumps(job) for job in (text_job, diagram_job)), encoding="utf-8")
            completed = [subprocess.CompletedProcess([], 0, json.dumps(self._result(job, mode)), "")
                         for job, mode in ((text_job, "text"), (diagram_job, "image"))]
            output = io.StringIO()
            with patch("sys.argv", ["runner", "--queue", str(queue), "--workers", "1"]), \
                    patch("scripts.run_codex_extraction_queue.subprocess.run", side_effect=completed), redirect_stdout(output):
                main()
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1]),
                             {"status": "finished", "completed": 2, "skipped": 0, "failures": [], "total": 2})
            for job, mode in ((text_job, "text"), (diagram_job, "image")):
                result = json.loads((root / "extraction-results" / f"{job['job_id']}.json").read_text(encoding="utf-8"))
                self.assertEqual(result["representation"]["question"], mode)

    def test_explicit_override_rejects_a_job_without_schema_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job = self._bound_job(Path(directory))
            del job["schema_bindings"]
            with patch("scripts.run_codex_extraction_queue.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "schema"):
                    _run_one(job, schema=DEFAULT_SCHEMA, model="test", reasoning="low", timeout=1, force=False)
                run.assert_not_called()

    def test_prompt_preserves_printed_textbook_inconsistencies(self) -> None:
        job = {
            "job_id": "extract-ch05-q0001",
            "job_fingerprint": "abc",
            "prompt": "JOB-BOUND EXTRACTION POLICY. Source fidelity outranks mathematical correction.",
            "sources": [
                {"role": "question", "sha256": "q"},
                {"role": "answer_key", "sha256": "a"},
                {"role": "solution", "sha256": "s"},
            ],
        }

        prompt = _prompt(job)

        self.assertIn("Source fidelity outranks mathematical correction", prompt)
        self.assertIn("JOB-BOUND EXTRACTION POLICY", prompt)

    def test_run_rejects_an_output_schema_not_bound_to_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            job = {
                "job_id": "extract-ch05-q0001",
                "job_fingerprint": "abc",
                "prompt": "Bound policy",
                "output_path": str(root / "jobs" / "job.json"),
                "schema_bindings": {"codex-extraction-result.schema.json": "0" * 64},
                "sources": [
                    {"role": "question", "sha256": "q", "path": str(root / "q.png")},
                    {"role": "answer_key", "sha256": "a", "path": str(root / "a.png")},
                ],
            }

            with patch("scripts.run_codex_extraction_queue.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "schema"):
                    _run_one(job, schema=schema, model="model", reasoning="low", timeout=1, force=False)
            run.assert_not_called()

    def test_removes_nullable_fifth_option_from_four_option_records(self) -> None:
        result = {
            "options": {"A": "1", "B": "2", "C": "3", "D": "4", "E": None},
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text", "E": None},
                "solution": "text",
            },
        }

        normalized = normalized_pipeline_result(result)

        self.assertEqual(set(normalized["options"]), {"A", "B", "C", "D"})
        self.assertEqual(set(normalized["representation"]["options"]), {"A", "B", "C", "D"})

    def test_preserves_a_real_fifth_option(self) -> None:
        result = {
            "options": {"A": "1", "B": "2", "C": "3", "D": "4", "E": "None of these"},
            "representation": {
                "question": "text",
                "options": {"A": "text", "B": "text", "C": "text", "D": "text", "E": "text"},
                "solution": "text",
            },
        }

        normalized = normalized_pipeline_result(result)

        self.assertEqual(normalized["options"]["E"], "None of these")
        self.assertEqual(normalized["representation"]["options"]["E"], "text")


if __name__ == "__main__":
    unittest.main()
