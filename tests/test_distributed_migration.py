import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.upgrade_distributed_assessments import upgrade


class DistributedMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "aptitude.db"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
        CREATE TABLE students (student_id TEXT PRIMARY KEY,name TEXT,password_hash TEXT,class TEXT,section TEXT,created_at TEXT);
        CREATE TABLE admins (username TEXT PRIMARY KEY,name TEXT,password_hash TEXT);
        CREATE TABLE question_banks (bank_id INTEGER PRIMARY KEY,bank_name TEXT,source_html_filename TEXT,answer_key_filename TEXT,imported_at TEXT,format_version INTEGER DEFAULT 1);
        CREATE TABLE questions (question_id INTEGER PRIMARY KEY,question_text TEXT,source_key TEXT,category TEXT,chapter TEXT,difficulty TEXT,option_a TEXT,option_b TEXT,option_c TEXT,option_d TEXT,options_json TEXT DEFAULT '{}',correct_answer TEXT,explanation TEXT DEFAULT '',active INTEGER DEFAULT 1,bank_id INTEGER,stimulus_id TEXT,question_html TEXT DEFAULT '',solution_steps TEXT DEFAULT '[]',option_explanations TEXT DEFAULT '{}',display_media_json TEXT DEFAULT '{}',created_at TEXT);
        CREATE TABLE tests (test_id INTEGER PRIMARY KEY,test_name TEXT,composition TEXT,bank_id INTEGER,created_at TEXT,active INTEGER DEFAULT 1,launched INTEGER DEFAULT 0,mode TEXT DEFAULT 'faculty',owner_student_id TEXT,difficulties TEXT DEFAULT '["Easy"]',launch_expires_at TEXT);
        CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY,student_id TEXT,test_id INTEGER,started_at TEXT,submitted_at TEXT,status TEXT,total_questions INTEGER,attempted INTEGER,correct INTEGER,score INTEGER,percentage REAL,expires_at TEXT);
        CREATE TABLE responses (response_id INTEGER PRIMARY KEY,attempt_id TEXT,question_id INTEGER,selected_answer TEXT,correct INTEGER,category TEXT,chapter TEXT,question_order INTEGER);
        CREATE TABLE exam_violations (violation_id INTEGER PRIMARY KEY,attempt_id TEXT,violation_type TEXT,occurred_at TEXT);
        CREATE TABLE stimuli (bank_id INTEGER,stimulus_id TEXT,stimulus_type TEXT,title TEXT,alt_text TEXT,asset_filename TEXT,content_json TEXT,created_at TEXT,PRIMARY KEY(bank_id,stimulus_id));
        INSERT INTO students VALUES ('S1','Student','hash','AIML','A','2026-01-01T00:00:00+00:00');
        INSERT INTO admins VALUES ('faculty','Faculty','hash');
        INSERT INTO question_banks VALUES (1,'Bank','bank.html','answers.json','2026-01-01T00:00:00+00:00',1);
        INSERT INTO questions VALUES (1,'One','q1','Quantitative Aptitude','Arithmetic','Easy','1','2','3','4','{}','A','secret',1,1,NULL,'<p>One</p>','[]','{}','{}','2026-01-01T00:00:00+00:00');
        INSERT INTO tests VALUES (1,'Historical','[{"category":"Quantitative Aptitude","chapter":"Arithmetic","quantity":1}]',1,'2026-01-01T00:00:00+00:00',1,0,'faculty',NULL,'["Easy"]',NULL);
        INSERT INTO tests VALUES (2,'Submitted','[{"category":"Quantitative Aptitude","chapter":"Arithmetic","quantity":1}]',1,'2026-01-01T00:00:00+00:00',1,0,'faculty',NULL,'["Easy"]',NULL);
        INSERT INTO attempts VALUES ('A1','S1',2,'2026-01-01T00:00:00+00:00','2026-01-01T00:10:00+00:00','submitted',1,1,1,1,100,NULL);
        INSERT INTO responses VALUES (1,'A1',1,'A',1,'Quantitative Aptitude','Arithmetic',0);
        INSERT INTO exam_violations VALUES (1,'A1','focus_lost','2026-01-01T00:05:00+00:00');
        """)
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        connection = sqlite3.connect(self.db_path)
        value = {
            "students": connection.execute("SELECT * FROM students").fetchall(),
            "attempts": connection.execute("SELECT attempt_id,student_id,test_id,started_at,submitted_at,status,total_questions,attempted,correct,score,percentage,expires_at FROM attempts").fetchall(),
            "responses": connection.execute("SELECT response_id,attempt_id,question_id,selected_answer,correct,category,chapter,question_order FROM responses").fetchall(),
            "violations": connection.execute("SELECT * FROM exam_violations").fetchall(),
        }
        connection.close()
        return value

    def test_dry_run_is_complete_without_modifying_live_database_or_data(self):
        before_bytes = self.db_path.read_bytes()
        before_files = sorted(path.relative_to(self.data_dir) for path in self.data_dir.rglob('*'))
        result = upgrade(self.db_path, self.data_dir, dry_run=True)
        self.assertEqual(self.db_path.read_bytes(), before_bytes)
        self.assertEqual(sorted(path.relative_to(self.data_dir) for path in self.data_dir.rglob('*')), before_files)
        self.assertEqual(result["prepared_releases"], 1)
        self.assertEqual(result["preserved_attempts"], 1)

    def test_live_upgrade_is_backed_up_idempotent_and_preserves_history(self):
        before = self.snapshot()
        first = upgrade(self.db_path, self.data_dir)
        second = upgrade(self.db_path, self.data_dir)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(first["prepared_releases"], 1)
        self.assertEqual(second["prepared_releases"], 0)
        self.assertTrue(Path(first["backup_path"]).is_file())
        connection = sqlite3.connect(self.db_path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM assessment_releases").fetchone()[0], 1)
        self.assertIsNone(connection.execute("SELECT release_id FROM tests WHERE test_id=2").fetchone()[0])
        connection.close()

    def test_upgrade_refuses_active_faculty_assessment(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE tests SET status='in_progress' WHERE test_id=1") if "status" in {row[1] for row in connection.execute('PRAGMA table_info(tests)')} else connection.execute("UPDATE tests SET launched=1 WHERE test_id=1")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "in progress|active"):
            upgrade(self.db_path, self.data_dir)


if __name__ == "__main__":
    unittest.main()
