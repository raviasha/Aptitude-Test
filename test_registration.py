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


if __name__ == "__main__":
    unittest.main()
