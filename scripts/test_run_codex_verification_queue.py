from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.run_codex_verification_queue import _prompt, _run_one, normalized_pipeline_result


class CodexVerificationQueueTests(unittest.TestCase):
    def test_prompt_does_not_misclassify_sticky_header_tiling_as_clipping(self) -> None:
        job = {
            "job_id": "verify-ch05-q0001",
            "job_fingerprint": "abc",
            "prompt": "JOB-BOUND VERIFICATION POLICY. A repeated sticky application header is a capture artifact.",
            "sources": [
                {"kind": "candidate_record", "record": {"options": {"A": "1"}}},
                {"kind": "question_render", "path": "question.png", "viewport": "1024x768"},
            ],
        }

        prompt = _prompt(job)

        self.assertIn("repeated sticky application header", prompt)
        self.assertIn("JOB-BOUND VERIFICATION POLICY", prompt)

    def test_run_rejects_an_output_schema_not_bound_to_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            job = {
                "job_id": "verify-ch05-q0001",
                "job_fingerprint": "abc",
                "prompt": "Bound policy",
                "schema_bindings": {"codex-verification-result.schema.json": "0" * 64},
                "sources": [
                    {"kind": "candidate_record", "record": {"options": {"A": "1"}}},
                    {"kind": "question_render", "path": str(root / "q.png"), "viewport": "1024x768"},
                ],
            }

            with patch("scripts.run_codex_verification_queue.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "schema"):
                    _run_one(job, root / "results", schema, "model", "low", 1, False)
            run.assert_not_called()

    def test_removes_nullable_fifth_option_and_empty_differences(self) -> None:
        value = {
            "verdicts": {
                "question": "pass",
                "options.A": "pass",
                "options.B": "pass",
                "options.C": "pass",
                "options.D": "pass",
                "options.E": None,
                "answer_mapping": "pass",
                "solution": "pass",
                "readability": "pass",
                "clipping": "pass",
            },
            "differences": {
                "question": None,
                "options.A": None,
                "options.B": None,
                "options.C": None,
                "options.D": None,
                "options.E": None,
                "answer_mapping": None,
                "solution": None,
                "readability": None,
                "clipping": None,
            },
        }

        normalized = normalized_pipeline_result(value)

        self.assertNotIn("options.E", normalized["verdicts"])
        self.assertEqual(normalized["differences"], {})

    def test_preserves_failed_field_detail(self) -> None:
        value = {
            "verdicts": {"options.E": "fail"},
            "differences": {"options.E": "The fifth option is clipped."},
        }

        normalized = normalized_pipeline_result(value)

        self.assertEqual(normalized["verdicts"]["options.E"], "fail")
        self.assertEqual(normalized["differences"]["options.E"], "The fifth option is clipped.")


if __name__ == "__main__":
    unittest.main()
