import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from build_logical_reasoning_vision_zips import get_book_profile, select_chapters


class NewReasoningBookProfileTests(unittest.TestCase):
    def test_new_book_profile_maps_first_two_chapters(self):
        profile = get_book_profile("new_reasoning")
        chapters = select_chapters(profile, "1-2")

        self.assertEqual(
            [
                (
                    chapter.chapter,
                    chapter.slug,
                    chapter.printed_page_start,
                    chapter.printed_page_end,
                    chapter.pdf_page_start,
                    chapter.pdf_page_end,
                )
                for chapter in chapters
            ],
            [
                (1, "coding_decoding", 1, 34, 5, 38),
                (2, "alphabet_test", 35, None, 39, 58),
            ],
        )

    def test_existing_profile_remains_available(self):
        profile = get_book_profile("modern_approach")
        chapters = select_chapters(profile, "1")

        self.assertEqual(chapters[0].slug, "analogy")
        self.assertEqual((chapters[0].pdf_page_start, chapters[0].pdf_page_end), (17, 110))

    def test_invalid_chapter_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            select_chapters(get_book_profile("new_reasoning"), "3")


if __name__ == "__main__":
    unittest.main()
