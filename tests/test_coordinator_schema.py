import sqlite3
import tempfile
import unittest
from pathlib import Path

import app
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.sqlite import connect_sqlite


class CoordinatorSchemaTests(unittest.TestCase):
    def test_connection_enables_wal_busy_timeout_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_sqlite(Path(directory) / "coordinator.db")
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10_000)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            connection.close()

    def test_migration_is_idempotent_and_additive(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE tests (test_id INTEGER PRIMARY KEY);
            CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, test_id INTEGER, student_id TEXT);
        """)
        migrate_distributed_schema(connection)
        migrate_distributed_schema(connection)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"devices", "assessment_releases", "release_questions", "submissions", "audit_events"} <= tables)
        attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
        self.assertTrue({"release_id", "device_id", "order_seed", "ticket_json", "sealed_at", "submission_hash"} <= attempt_columns)

    def test_app_schema_migration_preserves_existing_tables_on_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            original_data_dir = app.DATA_DIR
            original_db_path = app.DB_PATH
            original_backup_dir = app.BACKUP_DIR
            original_question_banks_dir = app.QUESTION_BANKS_DIR
            try:
                app.DATA_DIR = Path(directory)
                app.DB_PATH = app.DATA_DIR / "aptitude.db"
                app.BACKUP_DIR = app.DATA_DIR / "backups"
                app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
                app.ensure_schema()
                with app.db() as connection:
                    connection.execute(
                        "INSERT INTO students (student_id, name, password_hash, class, section, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        ("S100", "Existing Student", "hash", "AIML", "A", "2026-01-01T00:00:00+00:00"),
                    )
                app.ensure_schema()
                with app.db() as connection:
                    student = connection.execute("SELECT name FROM students WHERE student_id = ?", ("S100",)).fetchone()
                    release_table = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'assessment_releases'"
                    ).fetchone()
                self.assertEqual(student["name"], "Existing Student")
                self.assertIsNotNone(release_table)
            finally:
                app.DATA_DIR = original_data_dir
                app.DB_PATH = original_db_path
                app.BACKUP_DIR = original_backup_dir
                app.QUESTION_BANKS_DIR = original_question_banks_dir

    def test_migration_preserves_responses_while_detaching_mutable_question_rows(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE tests (test_id INTEGER PRIMARY KEY);
            CREATE TABLE attempts (
              attempt_id TEXT PRIMARY KEY, test_id INTEGER, student_id TEXT
            );
            CREATE TABLE questions (question_id INTEGER PRIMARY KEY);
            CREATE TABLE responses (
              response_id INTEGER PRIMARY KEY AUTOINCREMENT,
              attempt_id TEXT NOT NULL,
              question_id INTEGER NOT NULL,
              selected_answer TEXT,
              correct INTEGER,
              category TEXT NOT NULL,
              chapter TEXT NOT NULL DEFAULT 'Uncategorized',
              question_order INTEGER NOT NULL,
              FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
              FOREIGN KEY(question_id) REFERENCES questions(question_id),
              UNIQUE(attempt_id, question_id)
            );
            INSERT INTO attempts VALUES ('a1', 1, 's1');
            INSERT INTO questions VALUES (7);
            INSERT INTO responses
              (attempt_id, question_id, selected_answer, correct, category, chapter, question_order)
            VALUES ('a1', 7, 'B', 1, 'Quantitative Aptitude', 'Arithmetic', 0);
        """)

        migrate_distributed_schema(connection)

        self.assertEqual(
            ("a1", 7, "B", 1),
            tuple(connection.execute(
                """SELECT attempt_id, question_id, selected_answer, correct
                   FROM responses"""
            ).fetchone()),
        )
        foreign_tables = {
            row[2] for row in connection.execute("PRAGMA foreign_key_list(responses)")
        }
        self.assertIn("attempts", foreign_tables)
        self.assertNotIn("questions", foreign_tables)
        connection.execute("DELETE FROM questions WHERE question_id=7")
        self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
