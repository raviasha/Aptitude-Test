from __future__ import annotations

import sys
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


class MarkerProposalTests(unittest.TestCase):
    def test_finds_exercise_start_and_excludes_illustrative_example_numbers(self) -> None:
        from logical_reasoning_v2.marker_proposals import (
            NumberedMarker,
            exercise_starts_from_words,
            numbered_markers_from_words,
        )

        words = (
            {"text": "Ex.", "x0": 44.0, "top": 80.0},
            {"text": "1.", "x0": 70.0, "top": 80.0},
            {"text": "EXERCISE", "x0": 42.0, "top": 120.0},
            {"text": "1A", "x0": 130.0, "top": 120.0},
            {"text": "1.", "x0": 43.0, "top": 170.0},
            {"text": "2.", "x0": 43.0, "top": 210.0},
        )

        self.assertEqual(exercise_starts_from_words(22, words), (("1A", 22, 120.0),))
        self.assertEqual(
            numbered_markers_from_words(22, words, minimum_top=120.0, maximum_top=300.0),
            (NumberedMarker(1, 22, 43.0, 170.0), NumberedMarker(2, 22, 43.0, 210.0)),
        )

    def test_exercise_windows_stop_before_the_next_exercise(self) -> None:
        from logical_reasoning_v2.marker_proposals import exercise_windows

        windows = exercise_windows(
            (("1A", 22, 510.0), ("1B", 28, 157.0)),
            chapter_end_page=31,
        )

        self.assertEqual(windows[0], ("1A", 22, 510.0, 28, 157.0))
        self.assertEqual(windows[1], ("1B", 28, 157.0, 31, float("inf")))

    def test_groups_question_and_answer_markers_by_exercise_window(self) -> None:
        from logical_reasoning_v2.marker_proposals import exercise_marker_groups

        pages = {
            22: (
                {"text": "EXERCISE", "x0": 230.0, "top": 100.0},
                {"text": "1A", "x0": 310.0, "top": 100.0},
                {"text": "1.", "x0": 43.0, "top": 140.0},
                {"text": "2.", "x0": 43.0, "top": 180.0},
                {"text": "ANSWERS", "x0": 230.0, "top": 220.0},
                {"text": "1.", "x0": 43.0, "top": 250.0},
                {"text": "2.", "x0": 43.0, "top": 270.0},
            ),
            23: (
                {"text": "EXERCISE", "x0": 230.0, "top": 100.0},
                {"text": "1B", "x0": 310.0, "top": 100.0},
                {"text": "1.", "x0": 43.0, "top": 140.0},
                {"text": "ANSWERS", "x0": 230.0, "top": 200.0},
                {"text": "1.", "x0": 43.0, "top": 230.0},
            ),
        }

        groups = exercise_marker_groups(pages, chapter_end_page=23)

        self.assertEqual([group.exercise_id for group in groups], ["1A", "1B"])
        self.assertEqual([marker.printed_number for marker in groups[0].question_markers], [1, 2])
        self.assertEqual([marker.printed_number for marker in groups[0].answer_markers], [1, 2])
        self.assertEqual([marker.printed_number for marker in groups[1].question_markers], [1])
        self.assertEqual([marker.printed_number for marker in groups[1].answer_markers], [1])

    def test_assigns_unique_internal_numbers_when_exercises_restart_printed_numbers(self) -> None:
        from logical_reasoning_v2.marker_proposals import NumberedMarker, number_exercises

        proposed = number_exercises(
            (
                ("1A", (NumberedMarker(1, 22, 45.0, 150.0), NumberedMarker(2, 22, 45.0, 190.0))),
                ("1B", (NumberedMarker(1, 28, 45.0, 180.0),)),
            )
        )

        self.assertEqual([item.internal_number for item in proposed], [1, 2, 3])
        self.assertEqual([item.printed_number for item in proposed], [1, 2, 1])
        self.assertEqual([item.exercise_id for item in proposed], ["1A", "1A", "1B"])

    def test_builds_column_bounded_segments_from_source_order_markers(self) -> None:
        from logical_reasoning_v2.marker_proposals import NumberedMarker, explicit_column_segments

        segments = explicit_column_segments(
            (
                NumberedMarker(1, 22, 45.0, 150.0),
                NumberedMarker(2, 22, 310.0, 120.0),
            ),
            page_heights={22: 2340},
        )

        self.assertEqual(
            segments[0],
            ({"page": 22, "left": 95, "top": 367, "right": 760, "bottom": 2280},),
        )
        self.assertEqual(
            segments[1],
            ({"page": 22, "left": 775, "top": 292, "right": 1435, "bottom": 2280},),
        )

    def test_builds_full_width_segments_for_verbal_exercise_rows(self) -> None:
        from logical_reasoning_v2.marker_proposals import NumberedMarker, explicit_single_column_segments

        segments = explicit_single_column_segments(
            (
                NumberedMarker(1, 22, 45.0, 550.0),
                NumberedMarker(2, 22, 45.0, 610.0),
            ),
            page_heights={22: 2340},
        )

        self.assertEqual(
            segments[0],
            ({"page": 22, "left": 95, "top": 1367, "right": 1435, "bottom": 1517},),
        )
        self.assertEqual(
            segments[1],
            ({"page": 22, "left": 95, "top": 1517, "right": 1435, "bottom": 2280},),
        )


if __name__ == "__main__":
    unittest.main()
