import tempfile
import unittest
from pathlib import Path

import app


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
