from pathlib import Path
import unittest


APP_JS = Path(__file__).with_name("static") / "app.js"
BRANDING_CSS = Path(__file__).with_name("static") / "branding.css"
INDEX_HTML = Path(__file__).with_name("static") / "index.html"


class FeedbackUiTests(unittest.TestCase):
    def test_login_defaults_to_student_and_aiml_without_faculty_credentials(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn('data-role="student" aria-pressed="true"', source)
        self.assertIn('<option value="AIML" selected>AIML</option>', source)
        self.assertIn('value="AIML" required', source)
        self.assertNotIn("Faculty demo:", source)
        self.assertNotIn("faculty123", source)
        self.assertNotIn("AI & DS", source)

    def test_timed_attempt_and_question_status_are_rendered_for_students(self):
        script = APP_JS.read_text(encoding="utf-8")
        styles = BRANDING_CSS.read_text(encoding="utf-8")
        start = script.index("function renderAttempt()")
        end = script.index("\nasync function saveAnswer", start)
        render_attempt = script[start:end]

        self.assertIn("const timed = Number.isFinite(attempt.remaining_seconds)", render_attempt)
        self.assertIn("timed ? `<div class=\"exam-timer\"", render_attempt)
        self.assertIn("if (timed) startExamTimer()", render_attempt)
        self.assertIn("<i class=\"answered\"></i>Attempted", render_attempt)
        self.assertIn("<i class=\"unanswered\"></i>Unattempted", render_attempt)
        self.assertIn(".institutional-assessment .numbers button.answered", styles)
        self.assertIn("background: #e9f8f1", styles)

    def test_packaged_assets_use_the_current_cache_version(self):
        index = INDEX_HTML.read_text(encoding="utf-8")

        self.assertEqual(index.count("?v=1.3.3"), 4)
        self.assertNotIn("?v=1.3.1", index)

    def test_institution_logos_and_assessment_use_separate_grid_columns(self):
        source = BRANDING_CSS.read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(92px, 220px) minmax(0, 1180px) minmax(92px, 220px)", source)
        self.assertIn("main.institutional-assessment", source)
        self.assertIn("position: sticky", source)
        self.assertNotIn("position: fixed", source)

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
