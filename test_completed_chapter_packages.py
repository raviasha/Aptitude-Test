from __future__ import annotations

from collections import Counter
from pathlib import Path
import unittest

import app


ROOT = Path(__file__).resolve().parent
PACKAGES = {
    "ch01_number_system_complete.zip": (293, {"Easy": 156, "Medium": 123, "Hard": 14}, 0),
    "ch02_hcf_lcm_complete.zip": (106, {"Easy": 67, "Medium": 37, "Hard": 2}, 0),
    "ch03_decimal_fractions_complete.zip": (77, {"Easy": 64, "Medium": 12, "Hard": 1}, 0),
    "ch04_simplification_complete.zip": (218, {"Easy": 75, "Medium": 119, "Hard": 24}, 0),
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

    def test_number_system_square_exponents_are_preserved(self) -> None:
        with (ROOT / "question-banks" / "ch01_number_system_complete.zip").open("rb") as package:
            _, questions, _ = app.parse_v2_package(package)
        question = next(question for question in questions if question["key"] == "ch01-q0128")

        self.assertEqual(question["question_text"], "(80)² − (65)² + 81 = ?")
        self.assertIn("(80)² − (65)²", question["solution_steps"][0])

    def test_number_system_reciprocal_powers_match_textbook(self) -> None:
        with (ROOT / "question-banks" / "ch01_number_system_complete.zip").open("rb") as package:
            _, questions, _ = app.parse_v2_package(package)
        question = next(question for question in questions if question["key"] == "ch01-q0044")

        self.assertEqual(
            question["question_text"],
            "If 0 < x < 1, which of the following is greatest? (Campus Recruitment, 2007)",
        )
        self.assertEqual(
            question["options"],
            {"A": "x", "B": "x²", "C": "1/x", "D": "1/x²"},
        )
        self.assertEqual(question["correct_answer"], "D")
        self.assertEqual(
            question["solution_steps"],
            [
                "0 < x < 1 ⇒ x² < x < 1 ...(i)",
                "⇒ 1/x² > 1/x > 1 > x > x² [using (i)]",
                "Hence, 1/x² is the greatest.",
            ],
        )

    def test_number_system_even_square_sum_matches_textbook(self) -> None:
        with (ROOT / "question-banks" / "ch01_number_system_complete.zip").open("rb") as package:
            _, questions, _ = app.parse_v2_package(package)
        question = next(question for question in questions if question["key"] == "ch01-q0173")

        self.assertEqual(
            question["question_text"],
            "Given that (1² + 2² + 3² + … + 20²) = 2870, the value of (2² + 4² + 6² + … + 40²) is",
        )
        self.assertEqual(
            question["options"],
            {"A": "2870", "B": "5740", "C": "11480", "D": "28700"},
        )
        self.assertEqual(question["correct_answer"], "C")
        self.assertEqual(
            question["solution_steps"],
            [
                "2² + 4² + 6² + … + 40² = (1 × 2)² + (2 × 2)² + (2 × 3)² + … + (2 × 20)².",
                "= 2² × (1² + 2² + 3² + … + 20²).",
                "= (4 × 2870) = 11480.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
