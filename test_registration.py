import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import app
from scripts.build_quantitative_bank import clean_math_text
from scripts.solution_quality import audit_questions


class StudentRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = app.DATA_DIR
        self.original_db_path = app.DB_PATH
        self.original_backup_dir = app.BACKUP_DIR
        self.original_question_banks_dir = app.QUESTION_BANKS_DIR
        app.DATA_DIR = Path(self.temp_dir.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        app.ensure_schema()

    def tearDown(self):
        app.DATA_DIR = self.original_data_dir
        app.DB_PATH = self.original_db_path
        app.BACKUP_DIR = self.original_backup_dir
        app.QUESTION_BANKS_DIR = self.original_question_banks_dir
        self.temp_dir.cleanup()

    def test_private_use_math_fragments_are_not_returned_to_the_browser(self):
        self.assertEqual(app.clean_display_text("x\uf8eb + y\uf8f6"), "x + y")
        self.assertEqual(clean_math_text("a\uf8ec = b"), "a = b")

    def test_malformed_di_solution_is_replaced_with_readable_calculation(self):
        raw_step = "Amount spent on Groceries, Entertainment and Investments = { } (23 10 15) 4845800"
        self.assertEqual(app.clean_display_value([raw_step]), next(iter(app.SOLUTION_STEP_OVERRIDES.values())))

    def test_line_graph_import_solution_is_replaced_with_readable_calculation(self):
        question = "If the imports in 2008 was ₹ 250 crores and the total exports in the years 2008 and 2009 together was ₹ 500 crores, then the imports in 2009 was"
        steps = app.display_solution_steps(question, '["garbled formula"]')
        self.assertEqual(steps[-1], "Imports in 2009 are 140% of exports. So imports = 1.40 × ₹300 = ₹420 crores. Therefore, option D is correct.")

    def test_unverified_malformed_solution_is_hidden_from_students(self):
        result = app.clean_display_value(["Required %= 250 100250 2501.25 200 == ×="])
        self.assertEqual(result, app.SOLUTION_REVIEW_NOTICE)

    def test_solution_audit_reports_critical_and_clean_questions(self):
        records, summary = audit_questions([
            {"key": "bad", "category": "Data Interpretation", "chapter": "Line Graphs", "solution_steps": ["250 100250 2501.25 200 == ×="]},
            {"key": "good", "category": "Data Interpretation", "chapter": "Line Graphs", "solution_steps": ["Exports = ₹250 ÷ 1.25 = ₹200 crores."]},
        ])
        self.assertEqual(summary["critical_questions"], 1)
        self.assertEqual(summary["clean_questions"], 1)
        self.assertEqual(records[0]["key"], "bad")

    def test_admin_can_delete_an_unused_question_bank(self):
        with app.db() as connection:
            bank_id = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, '', '', ?)",
                ("Discard me", app.now()),
            ).lastrowid
        request = app.Request({"type": "http", "method": "DELETE", "path": "/", "headers": [], "session": {"user": {"role": "admin", "id": "admin", "name": "Admin"}}})
        result = app.delete_question_bank(bank_id, request)
        with app.db() as connection:
            remaining = connection.execute("SELECT 1 FROM question_banks WHERE bank_id = ?", (bank_id,)).fetchone()
        self.assertTrue(result["deleted"])
        self.assertIsNone(remaining)

    def test_student_can_register_and_duplicate_id_is_rejected(self):
        result = app.register_student(" s123 ", "New Student", "AI & DS", "A", "secret123")

        self.assertEqual(result["student_id"], "S123")
        with app.db() as connection:
            student = connection.execute("SELECT * FROM students WHERE student_id = ?", ("S123",)).fetchone()
        self.assertEqual(student["name"], "New Student")
        self.assertTrue(app.check_password("secret123", student["password_hash"]))

        with self.assertRaises(app.RegistrationError):
            app.register_student("S123", "Another Student", "AI & DS", "A", "secret123")

    def test_student_delete_is_allowed_without_attempts(self):
        app.register_student("S456", "Delete Me", "AI & DS", "A", "secret123")

        result = app.delete_student("S456")

        self.assertEqual(result["student_id"], "S456")
        with app.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM students WHERE student_id = ?", ("S456",)).fetchone())

    def test_student_sees_only_launched_test_when_one_exists(self):
        with app.db() as connection:
            connection.execute("INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)", ("Available", "{}", app.now(), 1, 0))
            connection.execute("INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)", ("Launched", "{}", app.now(), 1, 1))
            selected = app.student_available_tests(connection)

        self.assertEqual([item["test_name"] for item in selected], ["Launched"])

    def test_feedback_is_available_only_for_practice_attempts(self):
        with app.db() as connection:
            connection.execute("INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)", ("Practice", "{}", app.now(), 1, 0))
            practice = connection.execute("SELECT * FROM tests WHERE test_name = 'Practice'").fetchone()
            connection.execute("INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)", ("Exam", "{}", app.now(), 1, 1))
            exam = connection.execute("SELECT * FROM tests WHERE test_name = 'Exam'").fetchone()
        self.assertTrue(app.feedback_allowed(practice))
        self.assertFalse(app.feedback_allowed(exam))

    def test_question_bank_preserves_solution_steps_and_option_explanations(self):
        html = '<section data-question-key="q1"><h3>What is 2 + 2?</h3></section>'
        answer_key = '{"bank_name":"Steps","questions":[{"key":"q1","category":"Quantitative Aptitude","difficulty":"Easy","options":{"A":"3","B":"4","C":"5","D":"6"},"correct_answer":"B","explanation":"Adding two and two gives four.","solution_steps":["Start with 2.","Add 2.","Get 4."],"option_explanations":{"A":"Too low.","B":"Correct.","C":"Too high.","D":"Too high."}}]}'
        _, questions = app.parse_question_bank(html, answer_key)

        self.assertEqual(questions[0]["solution_steps"], ["Start with 2.", "Add 2.", "Get 4."])
        self.assertEqual(questions[0]["option_explanations"]["B"], "Correct.")
        self.assertEqual(list(questions[0]["options"]), ["A", "B", "C", "D"])

    def test_question_bank_preserves_category_chapter_and_stimulus_metadata(self):
        html = '<section data-question-key="q1"><h3>Read the graph.</h3></section>'
        answer_key = json.dumps({
            "bank_name": "Metadata",
            "questions": [{
                "key": "q1", "category": "Data Interpretation", "chapter": "Bar Graphs",
                "stimulus_id": "sales-graph", "difficulty": "Easy",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "B",
            }],
        })

        _, questions = app.parse_question_bank(html, answer_key)

        self.assertEqual(questions[0]["chapter"], "Bar Graphs")
        self.assertEqual(questions[0]["stimulus_id"], "sales-graph")

    def test_taxonomy_validation_and_sampling_use_leaf_chapters(self):
        with app.db() as connection:
            bank_id = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, '', '', ?)",
                ("Chapter bank", app.now()),
            ).lastrowid
            for index, (category, chapter, stimulus_id) in enumerate((
                ("Arithmetical Ability", "Percentages", None),
                ("Arithmetical Ability", "Percentages", None),
                ("Data Interpretation", "Bar Graphs", "bar-1"),
                ("Data Interpretation", "Bar Graphs", "bar-1"),
            ), start=1):
                connection.execute(
                    """INSERT INTO questions
                       (source_key, question_text, category, chapter, stimulus_id, difficulty,
                        option_a, option_b, option_c, option_d, correct_answer, created_at, bank_id)
                       VALUES (?, ?, ?, ?, ?, 'Easy', '1', '2', '3', '4', 'A', ?, ?)""",
                    (f"q{index}", f"Question {index}", category, chapter, stimulus_id, app.now(), bank_id),
                )
            taxonomy = app.question_bank_taxonomy(connection, bank_id)
            rules = app.normalize_selection_rules([
                {"category": "Arithmetical Ability", "chapter": "Percentages", "quantity": 2},
                {"category": "Data Interpretation", "chapter": "Bar Graphs", "quantity": 2},
            ])
            app.validate_selection_rules(connection, bank_id, rules, 10)
            sampled = app.sample_questions(connection, bank_id, rules)

        self.assertEqual(taxonomy["question_count"], 4)
        self.assertEqual(len(sampled), 4)
        self.assertEqual({(item["category"], item["chapter"]) for item in sampled}, {
            ("Arithmetical Ability", "Percentages"), ("Data Interpretation", "Bar Graphs")
        })
        bar_positions = [index for index, item in enumerate(sampled) if item["stimulus_id"] == "bar-1"]
        self.assertEqual(bar_positions, list(range(min(bar_positions), max(bar_positions) + 1)))

    def test_v2_package_imports_shared_graph_asset(self):
        manifest = {
            "format_version": 2,
            "bank_name": "V2 graph bank",
            "question_files": ["questions/data.jsonl"],
            "stimuli": [{
                "id": "bar-1", "type": "image", "file": "assets/bar.svg",
                "title": "Sales", "alt_text": "Bar graph of sales",
            }],
        }
        question = {
            "key": "di-1", "question_text": "Which bar is highest?", "category": "Data Interpretation",
            "chapter": "Bar Graphs", "stimulus_id": "bar-1", "difficulty": "Easy",
            "options": {"A": "A", "B": "B", "C": "C", "D": "D"}, "correct_answer": "B",
        }
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("questions/data.jsonl", json.dumps(question) + "\n")
            archive.writestr("assets/bar.svg", '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="8"/></svg>')
        package.seek(0)

        bank_name, questions, stimuli = app.parse_v2_package(package)
        result = app.save_v2_question_bank(bank_name, questions, stimuli, "graph-bank.zip")

        self.assertEqual(result["question_count"], 1)
        self.assertEqual(result["stimulus_count"], 1)
        with app.db() as connection:
            stored = connection.execute(
                "SELECT chapter, stimulus_id FROM questions WHERE bank_id = ?", (result["bank_id"],)
            ).fetchone()
            stimulus = connection.execute(
                "SELECT asset_filename FROM stimuli WHERE bank_id = ?", (result["bank_id"],)
            ).fetchone()
        self.assertEqual(stored["chapter"], "Bar Graphs")
        self.assertEqual(stored["stimulus_id"], "bar-1")
        self.assertTrue((app.question_assets_dir() / str(result["bank_id"]) / stimulus["asset_filename"]).is_file())

    def test_v2_package_rejects_executable_svg(self):
        manifest = {
            "format_version": 2, "bank_name": "Unsafe", "question_files": ["questions.jsonl"],
            "stimuli": [{"id": "bad", "file": "bad.svg"}],
        }
        question = {
            "key": "q1", "question_text": "Unsafe?", "category": "Data Interpretation", "chapter": "Charts",
            "stimulus_id": "bad", "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "correct_answer": "A",
        }
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("questions.jsonl", json.dumps(question))
            archive.writestr("bad.svg", "<svg><script>alert(1)</script></svg>")
        package.seek(0)

        with self.assertRaises(app.HTTPException):
            app.parse_v2_package(package)

    def test_student_can_build_personal_practice_by_chapter(self):
        app.register_student("P100", "Practice Student", "AI & DS", "A", "secret123")
        with app.db() as connection:
            bank_id = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, '', '', ?)",
                ("Practice bank", app.now()),
            ).lastrowid
            for index in range(3):
                connection.execute(
                    """INSERT INTO questions
                       (question_text, category, chapter, difficulty, option_a, option_b, option_c, option_d,
                        correct_answer, created_at, bank_id) VALUES (?, 'Arithmetical Ability', 'Percentages',
                        'Easy', '1', '2', '3', '4', 'A', ?, ?)""",
                    (f"Practice {index}", app.now(), bank_id),
                )
        request = app.Request({
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "session": {"user": {"role": "student", "id": "P100", "name": "Practice Student"}},
        })
        payload = app.PracticePayload(bank_id=bank_id, selection_rules=[
            app.SelectionRule(category="Arithmetical Ability", chapter="Percentages", quantity=2)
        ])

        result = app.start_student_practice(payload, request)

        with app.db() as connection:
            attempt = app.get_attempt(connection, result["attempt_id"])
            response_count = connection.execute(
                "SELECT COUNT(*) AS count FROM responses WHERE attempt_id = ? AND chapter = 'Percentages'",
                (result["attempt_id"],),
            ).fetchone()["count"]
        self.assertEqual(attempt["mode"], "student_practice")
        self.assertTrue(app.feedback_allowed(attempt))
        self.assertEqual(response_count, 2)

    def test_new_practice_selection_is_not_replaced_by_an_open_practice_session(self):
        app.register_student("P101", "New Practice Student", "AI & DS", "A", "secret123")
        with app.db() as connection:
            first_bank = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, '', '', ?)",
                ("Old practice bank", app.now()),
            ).lastrowid
            second_bank = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, '', '', ?)",
                ("Selected DI bank", app.now()),
            ).lastrowid
            for bank_id, category, chapter in ((first_bank, "Arithmetical Ability", "Percentages"), (second_bank, "Data Interpretation", "Bar Graphs")):
                connection.execute(
                    """INSERT INTO questions (question_text, category, chapter, difficulty, option_a, option_b, option_c, option_d,
                       correct_answer, created_at, bank_id) VALUES (?, ?, ?, 'Easy', '1', '2', '3', '4', 'A', ?, ?)""",
                    (f"Question for {chapter}", category, chapter, app.now(), bank_id),
                )
            old_test = connection.execute(
                """INSERT INTO tests (test_name, composition, bank_id, created_at, active, launched, mode, owner_student_id)
                   VALUES ('Older practice', '[]', ?, ?, 0, 0, 'student_practice', 'P101')""",
                (first_bank, app.now()),
            ).lastrowid
            old_question = connection.execute(
                "SELECT question_id, category, chapter, stimulus_id FROM questions WHERE bank_id = ?", (first_bank,)
            ).fetchone()
            app.create_attempt_from_questions(connection, "P101", old_test, [old_question])
        request = app.Request({"type": "http", "method": "POST", "path": "/", "headers": [], "session": {"user": {"role": "student", "id": "P101", "name": "New Practice Student"}}})
        result = app.start_student_practice(
            app.PracticePayload(bank_id=second_bank, selection_rules=[app.SelectionRule(category="Data Interpretation", chapter="Bar Graphs", quantity=1)]), request
        )
        with app.db() as connection:
            attempt = app.get_attempt(connection, result["attempt_id"])
        self.assertFalse(result["resumed"])
        self.assertEqual(attempt["bank_id"], second_bank)

    def test_complete_quantitative_bank_imports_four_and_five_choice_questions(self):
        bank_path = app.SOURCE_ROOT / "question-banks" / "quantitative_aptitude_complete_extended"
        html_source = bank_path.with_suffix(".html").read_text(encoding="utf-8-sig")
        answer_key_source = bank_path.with_suffix(".json").read_text(encoding="utf-8-sig")

        bank_name, questions = app.parse_question_bank(html_source, answer_key_source)

        self.assertEqual(len(questions), 5151)
        self.assertEqual(sum(len(question["options"]) == 4 for question in questions), 4035)
        self.assertEqual(sum(len(question["options"]) == 5 for question in questions), 1116)
        self.assertEqual(sum(question["correct_answer"] == "E" for question in questions), 226)

        result = app.save_question_bank(bank_name, questions, bank_path.name + ".html", bank_path.name + ".json")

        self.assertEqual(result["question_count"], 5151)
        with app.db() as connection:
            imported = connection.execute(
                "SELECT COUNT(*) AS count FROM questions WHERE bank_id = ?", (result["bank_id"],)
            ).fetchone()["count"]
            option_e = connection.execute(
                "SELECT options_json FROM questions WHERE bank_id = ? AND correct_answer = 'E' LIMIT 1",
                (result["bank_id"],),
            ).fetchone()
        self.assertEqual(imported, 5151)
        self.assertIn("E", json.loads(option_e["options_json"]))

    def test_legacy_question_rows_are_backfilled_with_json_options(self):
        app.DB_PATH.unlink()
        connection = sqlite3.connect(app.DB_PATH)
        connection.executescript(
            """
            CREATE TABLE questions (
              question_id INTEGER PRIMARY KEY AUTOINCREMENT, question_text TEXT NOT NULL,
              category TEXT NOT NULL, difficulty TEXT NOT NULL, option_a TEXT NOT NULL,
              option_b TEXT NOT NULL, option_c TEXT NOT NULL, option_d TEXT NOT NULL,
              correct_answer TEXT NOT NULL, explanation TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
              bank_id INTEGER, question_html TEXT NOT NULL DEFAULT '', solution_steps TEXT NOT NULL DEFAULT '[]',
              option_explanations TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """INSERT INTO questions
               (question_text, category, difficulty, option_a, option_b, option_c, option_d, correct_answer, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("Legacy question", "Quantitative Aptitude", "Easy", "1", "2", "3", "4", "D", app.now()),
        )
        connection.commit()
        connection.close()

        app.ensure_schema()

        with app.db() as migrated:
            question = migrated.execute(
                "SELECT option_a, option_b, option_c, option_d, options_json FROM questions"
            ).fetchone()
        self.assertEqual(json.loads(question["options_json"]), {"A": "1", "B": "2", "C": "3", "D": "4"})
        self.assertEqual(app.question_options(question), {"A": "1", "B": "2", "C": "3", "D": "4"})

    def test_five_choice_answer_is_serialized_saved_and_scored(self):
        app.register_student("S555", "Five Choice Student", "AI & DS", "A", "secret123")
        options = {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"}
        with app.db() as connection:
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_text, category, difficulty, option_a, option_b, option_c, option_d,
                    options_json, correct_answer, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("Choose five.", "Quantitative Aptitude", "Easy", "1", "2", "3", "4", json.dumps(options), "E", "Five is correct.", app.now()),
            ).lastrowid
            test_id = connection.execute(
                "INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)",
                ("Five choices", "{}", app.now(), 1, 0),
            ).lastrowid
            connection.execute(
                "INSERT INTO attempts (attempt_id, student_id, test_id, started_at, total_questions) VALUES (?, ?, ?, ?, ?)",
                ("five-choice-attempt", "S555", test_id, app.now(), 1),
            )
            connection.execute(
                "INSERT INTO responses (attempt_id, question_id, category, question_order) VALUES (?, ?, ?, ?)",
                ("five-choice-attempt", question_id, "Quantitative Aptitude", 1),
            )
            attempt = app.get_attempt(connection, "five-choice-attempt")
            serialized = app.serialize_attempt(connection, attempt)

        self.assertEqual(serialized["questions"][0]["options"], options)
        request = app.Request({
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [],
            "session": {"user": {"role": "student", "id": "S555", "name": "Five Choice Student"}},
        })

        saved = app.save_answer("five-choice-attempt", question_id, app.AnswerPayload(answer="E"), request)
        result = app.submit_attempt("five-choice-attempt", app.SubmitPayload(confirmed=True), request)

        self.assertTrue(saved["feedback"]["correct"])
        self.assertEqual(result["attempt"]["score"], 1)

    def test_practice_answers_lock_but_exam_answers_can_change(self):
        self.assertTrue(app.answer_locked({"launched": 0, "selected_answer": "A"}))
        self.assertFalse(app.answer_locked({"launched": 1, "selected_answer": "A"}))
        self.assertFalse(app.answer_locked({"launched": 0, "selected_answer": None}))

    def test_save_answer_locks_practice_but_allows_exam_reselection(self):
        app.register_student("S789", "Answer Student", "AI & DS", "A", "secret123")
        with app.db() as connection:
            question_id = connection.execute(
                """INSERT INTO questions
                   (question_text, category, difficulty, option_a, option_b, option_c, option_d,
                    correct_answer, explanation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("What is 2 + 2?", "Quantitative Aptitude", "Easy", "3", "4", "5", "6", "B", "Two plus two is four.", app.now()),
            ).lastrowid
            practice_test_id = connection.execute(
                "INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)",
                ("Practice", "{}", app.now(), 1, 0),
            ).lastrowid
            exam_test_id = connection.execute(
                "INSERT INTO tests (test_name, composition, created_at, active, launched) VALUES (?, ?, ?, ?, ?)",
                ("Faculty-led", "{}", app.now(), 1, 1),
            ).lastrowid
            for attempt_id, test_id in (("practice-attempt", practice_test_id), ("exam-attempt", exam_test_id)):
                connection.execute(
                    "INSERT INTO attempts (attempt_id, student_id, test_id, started_at, total_questions) VALUES (?, ?, ?, ?, ?)",
                    (attempt_id, "S789", test_id, app.now(), 1),
                )
                connection.execute(
                    "INSERT INTO responses (attempt_id, question_id, category, question_order) VALUES (?, ?, ?, ?)",
                    (attempt_id, question_id, "Quantitative Aptitude", 1),
                )

        request = app.Request({
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [],
            "session": {"user": {"role": "student", "id": "S789", "name": "Answer Student"}},
        })

        with self.assertRaises(app.HTTPException) as invalid_option:
            app.save_answer("practice-attempt", question_id, app.AnswerPayload(answer="E"), request)
        self.assertEqual(invalid_option.exception.status_code, 400)

        practice_result = app.save_answer("practice-attempt", question_id, app.AnswerPayload(answer="B"), request)
        self.assertTrue(practice_result["feedback"]["correct"])
        with self.assertRaises(app.HTTPException) as locked:
            app.save_answer("practice-attempt", question_id, app.AnswerPayload(answer="A"), request)
        self.assertEqual(locked.exception.status_code, 409)

        first_exam_result = app.save_answer("exam-attempt", question_id, app.AnswerPayload(answer="A"), request)
        second_exam_result = app.save_answer("exam-attempt", question_id, app.AnswerPayload(answer="B"), request)
        self.assertIsNone(first_exam_result["feedback"])
        self.assertIsNone(second_exam_result["feedback"])

        with app.db() as connection:
            practice_answer = connection.execute(
                "SELECT selected_answer FROM responses WHERE attempt_id = ?", ("practice-attempt",)
            ).fetchone()["selected_answer"]
            exam_answer = connection.execute(
                "SELECT selected_answer FROM responses WHERE attempt_id = ?", ("exam-attempt",)
            ).fetchone()["selected_answer"]
        self.assertEqual(practice_answer, "B")
        self.assertEqual(exam_answer, "B")


if __name__ == "__main__":
    unittest.main()
