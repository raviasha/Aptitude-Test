from __future__ import annotations

import sys
import unittest
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))


class LogicalReasoningBookMapTests(unittest.TestCase):
    def test_book_map_has_all_contents_chapters_in_source_order(self) -> None:
        from logical_reasoning_v2.book_map import chapters

        entries = chapters()

        self.assertEqual(len(entries), 47)
        self.assertEqual(entries[0].title, "Analogy")
        self.assertEqual(entries[-1].title, "Practice Question Set")
        self.assertEqual([entry.pipeline_id for entry in entries], list(range(101, 148)))

    def test_book_map_covers_each_section_without_page_gaps_or_overlap(self) -> None:
        from logical_reasoning_v2.book_map import chapters

        entries = chapters()
        for section_id, expected_start, expected_end in (
            ("s01", 17, 576),
            ("s02", 577, 735),
            ("s03", 736, 1171),
        ):
            section = [entry for entry in entries if entry.section_id == section_id]
            self.assertEqual(section[0].pdf_page_start, expected_start)
            self.assertEqual(section[-1].pdf_page_end, expected_end)
            self.assertTrue(
                all(
                    current.pdf_page_end + 1 == following.pdf_page_start
                    for current, following in zip(section, section[1:])
                )
            )


if __name__ == "__main__":
    unittest.main()
