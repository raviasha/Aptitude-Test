from __future__ import annotations

import csv
import importlib
import tempfile
import unittest
from pathlib import Path


class RecoveryRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module("scripts.build_logical_recovery_routing")

    def test_overlapping_exercise_page_gets_both_heading_contexts(self) -> None:
        rows = [
            {"chapter_id": "102", "exercise_id": "2A", "exercise_first": "112", "exercise_last": "117",
             "question_first": "112", "question_last": "115", "answer_first": "115", "answer_last": "117",
             "expected_terminal_number": "75"},
            {"chapter_id": "102", "exercise_id": "2B", "exercise_first": "117", "exercise_last": "122",
             "question_first": "117", "question_last": "121", "answer_first": "121", "answer_last": "122",
             "expected_terminal_number": "75"},
        ]

        result = self.module.routes_from_rows(rows)

        self.assertEqual(result["pages"]["112"], {
            "context_pages": [112], "exercise_ids": ["Exercise 2A"]})
        self.assertEqual(result["pages"]["117"], {
            "context_pages": [112, 117], "exercise_ids": ["Exercise 2A", "Exercise 2B"]})
        self.assertEqual(result["pages"]["122"], {
            "context_pages": [117], "exercise_ids": ["Exercise 2B"]})

    def test_reviewed_manifests_route_every_provenance_page_to_first_page(self) -> None:
        manifests = [{
            "exercise_id": "Exercise 1B",
            "review_provenance": [{"page": 28}, {"page": 29}, {"page": 33}],
        }]

        result = self.module.routes_from_reviewed_manifests(manifests)

        self.assertEqual(list(result["pages"]), ["28", "29", "30", "31", "32", "33"])
        self.assertEqual(result["pages"]["31"], {
            "context_pages": [28], "exercise_ids": ["Exercise 1B"]})

    def test_invalid_ranges_and_ambiguous_exercise_ids_fail_closed(self) -> None:
        bad = [{"chapter_id": "102", "exercise_id": "?", "exercise_first": "117",
                "exercise_last": "112", "question_first": "117", "question_last": "112",
                "answer_first": "117", "answer_last": "112", "expected_terminal_number": "0"}]
        with self.assertRaises(ValueError):
            self.module.routes_from_rows(bad)


if __name__ == "__main__":
    unittest.main()
