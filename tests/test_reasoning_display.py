import unittest

import app


class ReasoningDisplayTests(unittest.TestCase):
    def test_multiline_directions_and_title_case_preserve_internal_word_numbers(self):
        directions = ('DIRECTIONS (99–102): How are the given letters coded?\n'
                      'A C N P R M D Y Z Q\n4 9 0 6 2 1 7 8 3 5\n\n')
        self.assertEqual(directions + 'QRCYNPD', app.display_question_text(
            directions + '99. QRCYNPD', 'ch401-q0099'))
        directions = 'Directions (1–63): Arrange the given words in a meaningful sequence.\n\n'
        self.assertEqual(directions + '1. Key  2. Door  3. Lock  4. Room', app.display_question_text(
            directions + '1. 1. Key  2. Door  3. Lock  4. Room', 'ch403-q0001'))

    def test_removes_matching_textbook_label_not_test_number(self):
        self.assertEqual(
            'If LISTEN is coded as 593417, find SILENT.',
            app.display_question_text('12. If LISTEN is coded as 593417, find SILENT.', 'ch401-q0012'),
        )

    def test_keeps_directions_and_numbered_statements(self):
        self.assertEqual(
            'DIRECTIONS (94–96): Choose a diagram.\n\nTeacher, College, Student',
            app.display_question_text(
                'DIRECTIONS (94–96): Choose a diagram.\n\n94. Teacher, College, Student', 'ch416-q0094'),
        )
        text = 'Statements:\n1. All cats are animals.\n2. Some animals are pets.'
        self.assertEqual(text, app.display_question_text(text, 'ch417-q0001'))
        text = 'DIRECTIONS: Evaluate the statements.\n\nStatements:\n1. All cats are animals.'
        self.assertEqual(text, app.display_question_text(text, 'ch417-q0001'))

    def test_does_not_strip_decimals_other_books_or_unmatched_numbers(self):
        for text, key in [
            ('12.5 is what percentage of 50?', 'ch401-q0012'),
            ('12. A question from another bank', 'ch01-q0012'),
            ('12. A question without provenance', None),
            ('3. First statement', 'ch401-q0012'),
        ]:
            with self.subTest(text=text, key=key):
                self.assertEqual(text, app.display_question_text(text, key))
