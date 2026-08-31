from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.run_codex_extraction_queue import _prompt, _run_one, normalized_pipeline_result


class CodexExtractionQueueTests(unittest.TestCase):
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
