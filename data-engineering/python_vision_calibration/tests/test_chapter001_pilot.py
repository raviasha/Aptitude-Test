from __future__ import annotations

import hashlib
import json
import unittest
import zipfile
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

    def test_candidate_does_not_reuse_question_or_option_crops_as_answers(self) -> None:
        with zipfile.ZipFile(self.CANDIDATE) as archive:
            questions = [
                json.loads(line)
                for line in archive.read("questions/ch01.jsonl")
                .decode("utf-8")
                .splitlines()
                if line.strip()
            ]

        violations: list[str] = []
        for question in questions:
            media = question.get("display_media", {})
            question_media = media.get("question", [])
            if isinstance(question_media, dict):
                question_media = [question_media]
            question_hashes = {
                item.get("sha256")
                for item in question_media
                if item.get("sha256")
            }
            option_hashes: dict[str, str] = {
                label: item.get("sha256", "")
                for label, item in media.get("options", {}).items()
            }
            reused_question = question_hashes.intersection(option_hashes.values())
            duplicate_options = {
                crop_hash
                for crop_hash in option_hashes.values()
                if crop_hash
                and list(option_hashes.values()).count(crop_hash) > 1
            }
            if reused_question or duplicate_options:
                violations.append(question["key"])

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
