import unittest

from scripts.build_quantitative_bank import (
    SOURCE_VISUAL_CROPS,
    question_top_from_word_index,
    select_visual_page,
)


class QuantitativeBankVisualTests(unittest.TestCase):
    def test_question_above_new_graph_keeps_previous_page_graph(self):
        regions = {
            947: [(34.0, 90.0, 570.0, 363.0)],
            948: [(140.0, 251.0, 466.0, 486.0)],
        }

        self.assertEqual(select_visual_page(948, 160.0, [947, 948], regions), 947)
        self.assertEqual(select_visual_page(948, 510.0, [947, 948], regions), 948)

    def test_normalized_word_index_maps_question_to_its_vertical_position(self):
        word_index = (
            "question18ifeachofthecompaniesaandbinvestedquestion19whatwastheratio",
            [(0, 10, 75.0), (10, 47, 90.0), (47, 57, 510.0), (57, 72, 525.0)],
        )

        self.assertEqual(
            question_top_from_word_index(word_index, "If each of the companies A and B invested"),
            90.0,
        )

    def test_reported_questions_have_dedicated_source_crops(self):
        assignments = {
            question_key: specification["id"]
            for specification in SOURCE_VISUAL_CROPS
            for question_key in specification["question_keys"]
        }

        self.assertEqual(assignments["qa-4063"], "qa-4063-adjoining-figure")
        self.assertEqual(assignments["qa-5102"], "di-company-profit-2004-2010")


if __name__ == "__main__":
    unittest.main()
