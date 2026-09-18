from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


class ExtractionRunnerTests(unittest.TestCase):
    def test_uses_schema_json_from_stdout_when_codex_output_file_is_missing(self) -> None:
        import run_codex_extraction_queue as runner

        payload = {"job_id": "extract-ch101-q0053", "question_text": "source-bound"}
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "missing.codex.json"
            value = runner.load_generated_result(temporary, json.dumps(payload) + "\n")

        self.assertEqual(value, payload)

    def test_rejects_non_json_stdout_when_output_file_is_missing(self) -> None:
        import run_codex_extraction_queue as runner

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "did not write"):
                runner.load_generated_result(Path(directory) / "missing.codex.json", "not JSON")


if __name__ == "__main__":
    unittest.main()
