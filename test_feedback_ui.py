from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest


APP_JS = Path(__file__).with_name("static") / "app.js"
BRANDING_CSS = Path(__file__).with_name("static") / "branding.css"
INDEX_HTML = Path(__file__).with_name("static") / "index.html"


class LoginMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_department = False
        self.current_option = None
        self.departments = []
        self.current_small = None
        self.small_texts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "select" and attributes.get("id") == "department":
            self.in_department = True
        elif tag == "option" and self.in_department:
            self.current_option = {
                "value": attributes.get("value"),
                "selected": "selected" in attributes,
                "text": [],
            }
        elif tag == "small":
            self.current_small = []

    def handle_data(self, data):
        if self.current_option is not None:
            self.current_option["text"].append(data)
        if self.current_small is not None:
            self.current_small.append(data)

    def handle_endtag(self, tag):
        if tag == "option" and self.current_option is not None:
            self.current_option["label"] = "".join(self.current_option.pop("text")).strip()
            self.departments.append(self.current_option)
            self.current_option = None
        elif tag == "select" and self.in_department:
            self.in_department = False
        elif tag == "small" and self.current_small is not None:
            self.small_texts.append("".join(self.current_small).strip())
            self.current_small = None


def render_login_markup():
    javascript = r"""
const fs = require('fs');
const appNode = { innerHTML: '' };
const idleNode = { addEventListener() {}, classList: { add() {}, remove() {}, toggle() {} } };
global.document = {
  querySelector(selector) {
    if (selector === '#app') return appNode;
    return idleNode;
  },
  querySelectorAll() { return []; },
};
global.window = { addEventListener() {} };
global.fetch = () => Promise.reject(new Error('offline test'));
eval(fs.readFileSync(process.argv[1], 'utf8'));
setImmediate(() => process.stdout.write(appNode.innerHTML));
"""
    completed = subprocess.run(
        ["node", "-e", javascript, str(APP_JS)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    parser = LoginMarkupParser()
    parser.feed(completed.stdout)
    return parser


class FeedbackUiTests(unittest.TestCase):
    def test_faculty_header_uses_professor_name_for_account_and_role(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("<small>Faculty</small>", source)
        self.assertNotIn("<small>Prof R Ravi Shankar</small>", source)

    def test_login_brand_uses_spaced_matching_aiml_ksat_text(self):
        source = APP_JS.read_text(encoding="utf-8")

        self.assertIn("AIML - KSAT", source)
        self.assertNotIn("AIML-<i>KSAT</i>", source)

    def test_login_card_stays_below_institutional_header(self):
        styles = BRANDING_CSS.read_text(encoding="utf-8")

        self.assertIn("place-items: start end", styles)
        self.assertIn("margin-top: clamp(180px, 22vh, 240px)", styles)

    def test_login_defaults_to_student_and_aiml_without_faculty_credentials(self):
        source = APP_JS.read_text(encoding="utf-8")
        login = render_login_markup()

        self.assertIn('data-role="student" aria-pressed="true"', source)
        self.assertEqual(
            [department["value"] for department in login.departments if department["selected"]],
            ["AIML"],
        )
        self.assertIn('value="AIML" required', source)
        self.assertNotIn("Faculty demo:", source)
        self.assertNotIn("faculty123", source)
        self.assertNotIn("AI & DS", source)

    def test_coordinator_login_lists_all_supported_departments_in_order(self):
        login = render_login_markup()

        self.assertEqual(
            login.departments,
            [
                {"value": "CSE", "selected": False, "label": "CSE"},
                {"value": "AIML", "selected": True, "label": "AIML"},
                {"value": "CS&D", "selected": False, "label": "CS&D"},
                {"value": "CCE", "selected": False, "label": "CCE"},
                {"value": "CSE (ICB)", "selected": False, "label": "CSE (ICB)"},
                {"value": "ECE", "selected": False, "label": "ECE"},
                {"value": "ME", "selected": False, "label": "ME"},
                {"value": "MCA", "selected": False, "label": "MCA"},
                {
                    "value": "Science and Humanities",
                    "selected": False,
                    "label": "Science and Humanities",
                },
            ],
        )

    def test_coordinator_login_ends_with_department_rights_notice(self):
        login = render_login_markup()

        self.assertEqual(login.small_texts[-1], "Rights Reserved: AIML Department, KSIT")

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

        for filename in ("styles.css", "branding.css", "math.css"):
            self.assertIn(f'/static/{filename}?v=2.0.0', index)
        for filename in ("app.js", "faculty.css"):
            version = "20260911-3" if filename == "app.js" else "20260911"
            self.assertIn(f'/static/{filename}?v={version}', index)
            self.assertTrue((INDEX_HTML.parent / filename).is_file())
        self.assertNotIn('/static/app.js?v=2.0.0', index)
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

    def test_attempt_renderer_includes_verified_question_option_and_solution_media(self):
        source = APP_JS.read_text(encoding="utf-8")
        styles = Path(__file__).with_name("static").joinpath("styles.css").read_text(encoding="utf-8")
        start = source.index("function renderAttempt()")
        end = source.index("\nasync function saveAnswer", start)
        render_attempt = source[start:end]

        self.assertIn("function mediaMarkup(media, className)", source)
        self.assertIn("mediaMarkup(q.display_media?.question, 'question-media')", render_attempt)
        self.assertIn("mediaMarkup(q.display_media?.options?.[key], 'option-media')", render_attempt)
        self.assertIn("mediaMarkup(q.feedback?.display_media?.solution", render_attempt)
        self.assertIn("window.renderAttemptForValidation", source)
        for class_name in (".question-media", ".option-media", ".solution-media"):
            start = styles.index(class_name)
            rules = styles[start:styles.index("}", start)]
            self.assertIn("max-width:100%", rules)
            self.assertIn("height:auto", rules)
            self.assertNotIn("position:absolute", rules)


if __name__ == "__main__":
    unittest.main()
