from __future__ import annotations

import unittest

from scripts.generate_v2_marker_overrides import answer_windows, explicit_segments


class ExplicitSegmentTests(unittest.TestCase):
    def test_parses_page_specific_answer_windows(self) -> None:
        windows = answer_windows(
            ["408:540:750", "409:50:310"],
            default_minimum=90.0,
            default_maximum=570.0,
        )

        self.assertEqual(windows[408], (540.0, 750.0))
        self.assertEqual(windows[409], (50.0, 310.0))
        self.assertEqual(windows[410], (90.0, 570.0))

    def test_bounds_records_in_the_same_column_by_the_next_marker(self) -> None:
        markers = {
            1: (10, 50.0, 100.0),
            2: (10, 50.0, 140.0),
        }

        segments = explicit_segments(markers, page_bottoms={}, final_bottom=1870)

        self.assertEqual(
            segments[1],
            [{"page": 10, "left": 95, "top": 242, "right": 760, "bottom": 342}],
        )

    def test_preserves_cross_page_continuation_before_the_next_marker(self) -> None:
        markers = {
            1: (10, 320.0, 700.0),
            2: (11, 50.0, 200.0),
        }

        segments = explicit_segments(markers, page_bottoms={}, final_bottom=1870)

        self.assertEqual(
            segments[1],
            [
                {"page": 10, "left": 775, "top": 1742, "right": 1435, "bottom": 1870},
                {"page": 11, "left": 95, "top": 170, "right": 760, "bottom": 492},
            ],
        )

    def test_honours_a_page_specific_content_bottom(self) -> None:
        markers = {
            1: (10, 50.0, 100.0),
            2: (10, 320.0, 110.0),
        }

        segments = explicit_segments(markers, page_bottoms={10: 575}, final_bottom=1870)

        self.assertEqual(segments[1][0]["bottom"], 575)


if __name__ == "__main__":
    unittest.main()
