from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.rules import (
    POLICY_VERSION,
    Finding,
    choose_representation,
    normalize_candidate_text,
    validate_record,
)


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "notation-regressions.json"
)


class DeterministicCorruptionRuleTests(unittest.TestCase):
    def test_literal_regression_fixtures_name_the_expected_rule_or_null(self) -> None:
        fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        for fixture in fixtures:
            with self.subTest(fixture_id=fixture["id"]):
                findings = validate_record({"question_text": fixture["candidate"]})
                self.assertEqual(
                    [finding.rule_id for finding in findings],
                    [] if fixture["expected_rule"] is None else [fixture["expected_rule"]],
                )

    def test_normalization_composes_unicode_and_stabilizes_newlines_without_math_inference(self) -> None:
        self.assertEqual(normalize_candidate_text("7⁸⁴\r\nis valid"), "7⁸⁴\nis valid")
        self.assertEqual(normalize_candidate_text("The value is 7 84"), "The value is 7 84")
        self.assertEqual(normalize_candidate_text("cafe\u0301  \rline  \r\n"), "café\nline\n")

    def test_findings_are_structured_and_scoped_to_the_source_field(self) -> None:
        findings = validate_record(
            {
                "question_text": "Readable question",
                "options": {"C": "2 1 x"},
                "solution_steps": ["(80) 2 - (65) 2 + 81"],
            }
        )

        self.assertEqual(
            findings,
            [
                Finding(
                    rule_id="math.flattened_fraction_power",
                    field_path="options.C",
                    severity="block",
                    evidence="2 1 x",
                ),
                Finding(
                    rule_id="math.detached_parenthesized_power",
                    field_path="solution_steps[0]",
                    severity="block",
                    evidence="(80) 2",
                ),
            ],
        )

    def test_representation_policy_is_selected_per_field_and_fails_closed(self) -> None:
        blocking = [
            Finding(
                rule_id="math.detached_numeric_power",
                field_path="question_text",
                severity="block",
                evidence="7 84",
            )
        ]

        self.assertEqual(POLICY_VERSION, 1)
        self.assertEqual(choose_representation("question", "image", []), "image")
        self.assertEqual(choose_representation("option", "image", []), "image")
        self.assertEqual(choose_representation("solution", "image", []), "image")
        self.assertEqual(choose_representation("answer", "image", []), "quarantine")
        self.assertEqual(choose_representation("answer", "text", []), "text")
        self.assertEqual(choose_representation("question", "unknown", []), "quarantine")
        self.assertEqual(choose_representation("question", "image", blocking), "quarantine")


if __name__ == "__main__":
    unittest.main()
