from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


class Chapter001PilotInvariantTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[3]
    SUMMARY = (
        ROOT
        / "data-engineering"
        / "python_vision_calibration"
        / "reports"
        / "chapter-001-agent-triage-summary.json"
    )
    AUDIT = (
        ROOT
        / "data-engineering"
        / "python_vision_calibration"
        / "audits"
        / "chapter-001-agent-triage.json"
    )
    CANDIDATE = (
        ROOT
        / "question-banks"
        / "candidates"
        / "agent-triage"
        / "ch01_number_system_candidate.zip"
    )
    PUBLISHED = ROOT / "question-banks" / "ch01_number_system_complete.zip"

    def test_chapter1_pilot_outputs_are_complete_and_non_promoting(self) -> None:
        self.assertTrue(self.SUMMARY.is_file(), "terminal pilot summary is missing")
        self.assertTrue(self.AUDIT.is_file(), "portable pilot audit is missing")
        self.assertTrue(self.CANDIDATE.is_file(), "manual-review candidate ZIP is missing")

        summary = json.loads(self.SUMMARY.read_text(encoding="utf-8"))
        self.assertEqual(summary["total_baselines"], 380)
        self.assertEqual(summary["agent_terminal"], 380)
        self.assertEqual(summary["vision_terminal"], summary["vision_required"])
        self.assertEqual(summary["included"] + summary["quarantined"], 380)
        self.assertEqual(summary["candidate_question_count"], summary["included"])
        self.assertEqual(
            summary["candidate_sha256"],
            hashlib.sha256(self.CANDIDATE.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            summary["audit_sha256"],
            hashlib.sha256(self.AUDIT.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            summary["published_sha256_before"], summary["published_sha256_after"]
        )
        self.assertEqual(
            summary["published_sha256_after"],
            hashlib.sha256(self.PUBLISHED.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
