from pathlib import Path
import unittest


APP_JS = Path(__file__).with_name("static") / "app.js"


class FeedbackUiTests(unittest.TestCase):
    def test_submit_click_passes_a_boolean_instead_of_the_click_event(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn("addEventListener('click', () => submitAttempt(false))", source)
        self.assertNotIn("addEventListener('click', submitAttempt)", source)

    def test_feedback_only_renders_correct_answer_and_solution_steps(self):
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("function renderAttempt()")
        end = source.index("\nasync function saveAnswer", start)
        render_attempt = source[start:end]

        self.assertIn("<strong>Correct answer:</strong>", render_attempt)
        self.assertIn("<h4>Solution steps</h4>", render_attempt)
        self.assertNotIn("q.feedback.explanation", render_attempt)
        self.assertNotIn("option_explanations", render_attempt)
        self.assertNotIn("This option is not the correct answer.", render_attempt)


if __name__ == "__main__":
    unittest.main()
