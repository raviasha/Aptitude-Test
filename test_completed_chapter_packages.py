from __future__ import annotations

from collections import Counter
from pathlib import Path
import unittest

import app


ROOT = Path(__file__).resolve().parent
PACKAGES = {
    "ch01_number_system_complete.zip": (312, {"Easy": 169, "Medium": 129, "Hard": 14}, 0),
    "ch02_hcf_lcm_complete.zip": (119, {"Easy": 76, "Medium": 39, "Hard": 4}, 0),
    "ch03_decimal_fractions_complete.zip": (90, {"Easy": 79, "Medium": 10, "Hard": 1}, 0),
    "ch04_simplification_complete.zip": (291, {"Easy": 96, "Medium": 161, "Hard": 34}, 0),
    "ch36_tabulation_complete.zip": (60, {"Easy": 29, "Medium": 27, "Hard": 4}, 12),
}


class CompletedChapterPackageTests(unittest.TestCase):
    def test_every_completed_package_imports_without_solution_fallbacks(self) -> None:
        for filename, (expected_count, expected_difficulties, expected_stimuli) in PACKAGES.items():
            with self.subTest(filename=filename):
                with (ROOT / "question-banks" / filename).open("rb") as package:
                    _, questions, stimuli = app.parse_v2_package(package)
                self.assertEqual(len(questions), expected_count)
                self.assertEqual(len(stimuli), expected_stimuli)
                self.assertEqual(Counter(question["difficulty"] for question in questions), expected_difficulties)
                self.assertFalse(
                    any(
                        app.clean_display_value(question["solution_steps"])
                        == app.SOLUTION_REVIEW_NOTICE
                        for question in questions
                    )
                )

    def test_reported_continued_fraction_is_restored(self) -> None:
        with (ROOT / "question-banks" / "ch04_simplification_complete.zip").open("rb") as package:
            _, questions, _ = app.parse_v2_package(package)
        question = next(question for question in questions if question["key"] == "ch04-q0165")
        self.assertEqual(
            question["question_text"],
            "If [2 + 1 / (3 4/5)] / [2 + 1 / (3 + 1 / (1 + 1/4))] = x, what is x?",
        )
        self.assertEqual(question["options"], {"A": "1/7", "B": "3/7", "C": "1", "D": "8/7"})
        self.assertEqual(question["correct_answer"], "C")
        self.assertEqual(question["difficulty"], "Medium")


if __name__ == "__main__":
    unittest.main()
