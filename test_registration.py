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


if __name__ == "__main__":
    unittest.main()
