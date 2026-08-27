from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.audit import classify_failure, create_rule_proposal
from textbook_chapters_v2.models import VerificationResult
from textbook_chapters_v2.rules import POLICY_VERSION
import textbook_chapters_v2.rules as rules_module


class ControlledFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.rules_before = Path(rules_module.__file__).read_bytes()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _result(self, field: str, difference: str) -> VerificationResult:
        verdicts = {
            "question": "pass",
            "options.A": "pass",
            "options.B": "pass",
            "options.C": "pass",
            "options.D": "pass",
            "answer_mapping": "pass",
            "solution": "pass",
            "readability": "pass",
            "clipping": "pass",
        }
        verdicts[field] = "fail"
        return VerificationResult(
            job_id="verify-ch07-q0084",
            job_fingerprint="9" * 64,
            verdicts=verdicts,
            differences={field: difference},
            reviewer="independent-vision-reviewer",
        )

    def test_recurring_exponent_failure_becomes_pending_general_rule_proposal_without_rule_mutation(self) -> None:
        classification = classify_failure(
            self._result(
                "question",
                "Recurring normalization failure: superscript exponent 84 was flattened to baseline text in 7 84.",
            )
        )
        fixture = {
            "id": "numeric-power-7-84",
            "chapter": 7,
            "work_root": str(self.root / "work"),
            "field_path": "question_text",
            "candidate": "The remainder when 7 84 is divided by 342 is",
            "expected_rule": "math.detached_numeric_power",
            "source_crop_hashes": ["a" * 64],
            "candidate_sha256": "b" * 64,
            "render_hashes": ["c" * 64, "d" * 64],
        }

        proposal = create_rule_proposal(classification, fixture)

        self.assertEqual(classification.category, "normalization")
        self.assertEqual(classification.status, "general_rule_candidate")
        self.assertEqual(proposal.status, "pending_rule_review")
        self.assertEqual(proposal.required_fixture_id, "numeric-power-7-84")
        self.assertEqual(POLICY_VERSION, 1)
        self.assertEqual(Path(rules_module.__file__).read_bytes(), self.rules_before)
        self.assertEqual(proposal.path, self.root / "work" / "chapter-007" / "rule-proposals" / "numeric-power-7-84.json")
        persisted = json.loads(proposal.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["classification"]["status"], "general_rule_candidate")
        self.assertEqual(persisted["evidence"]["source_crop_hashes"], ["a" * 64])
        self.assertEqual(persisted["evidence"]["candidate_sha256"], "b" * 64)
        self.assertEqual(persisted["evidence"]["render_hashes"], ["c" * 64, "d" * 64])
        self.assertEqual(persisted["proposed_fixture"]["candidate"], "The remainder when 7 84 is divided by 342 is")

    def test_one_off_multiline_matrix_is_record_specific_media_not_a_rule_change(self) -> None:
        classification = classify_failure(
            self._result(
                "question",
                "One-off multi-line matrix has a unique source layout that cannot be safely represented as text.",
            )
        )

        self.assertEqual(classification.category, "unique_source_layout")
        self.assertEqual(classification.status, "record_specific_media")
        self.assertEqual(POLICY_VERSION, 1)
        self.assertEqual(Path(rules_module.__file__).read_bytes(), self.rules_before)

    def test_failure_categories_are_derived_from_literal_verification_differences(self) -> None:
        cases = (
            ("question", "OCR omitted the final phrase from the source question.", "extraction"),
            ("options.A", "The image representation uses the wrong source crop.", "representation"),
            ("readability", "Rendered text is too small to read at the desktop viewport.", "rendering"),
            ("answer_mapping", "Candidate answer B disagrees with the source answer key D.", "association"),
        )
        for field, difference, category in cases:
            with self.subTest(category=category):
                self.assertEqual(classify_failure(self._result(field, difference)).category, category)

    def test_classification_rejects_results_without_a_concrete_failure(self) -> None:
        passing = VerificationResult(
            job_id="verify-ch07-q0084",
            job_fingerprint="9" * 64,
            verdicts={"question": "pass"},
            differences={},
            reviewer="independent-vision-reviewer",
        )

        with self.assertRaisesRegex(ValueError, "failed field"):
            classify_failure(passing)

    def test_proposal_requires_literal_fixture_and_complete_hash_evidence(self) -> None:
        classification = classify_failure(self._result("question", "OCR omitted an exponent from the source."))
        incomplete = {
            "id": "numeric-power-7-84",
            "chapter": 7,
            "work_root": str(self.root / "work"),
            "candidate": "7 84",
            "expected_rule": "math.detached_numeric_power",
        }

        with self.assertRaisesRegex(ValueError, "hash evidence"):
            create_rule_proposal(classification, incomplete)


if __name__ == "__main__":
    unittest.main()
