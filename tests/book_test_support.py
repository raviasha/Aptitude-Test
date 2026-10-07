"""Disposable Coordinator fixture for book/selection integration tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import app


class BookFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in {"DATA_DIR": root, "DB_PATH": root / "aptitude.db",
                            "BACKUP_DIR": root / "backups",
                            "QUESTION_BANKS_DIR": root / "Question Banks"}.items():
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)
        old_config = app.app.state.coordinator_config
        self.addCleanup(setattr, app.app.state, "coordinator_config", old_config)
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)
        with app.db() as db:
            db.execute("INSERT OR REPLACE INTO admins VALUES ('bookadmin','Faculty',?)",
                       (app.hash_password("password123"),))
        login = self.client.post("/api/login", json={"role": "admin", "identifier": "bookadmin",
                                                     "password": "password123"})
        assert login.status_code == 200, login.text
        self.headers = {"X-KSAT-CSRF": login.json()["csrf_token"]}

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def bank(self, name="Chapter 1", count=30, chapter="Arithmetic"):
        with app.db() as db:
            bank = db.execute("""INSERT INTO question_banks
                (bank_name,source_html_filename,answer_key_filename,imported_at)
                VALUES (?,?,?,?)""", (name, name + ".zip", "manifest.json", app.now())).lastrowid
            for i in range(count):
                db.execute("""INSERT INTO questions
                    (question_text,source_key,category,chapter,difficulty,option_a,option_b,
                     option_c,option_d,correct_answer,bank_id,created_at)
                    VALUES (?,?,?,?,?,'A','B','C','D','A',?,?)""",
                    (f"{name} question {i}", f"q{i}", "Category A" if i % 2 else "Category B",
                     chapter, "Easy", bank, app.now()))
        return bank
