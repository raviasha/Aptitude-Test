import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

import app
from ksat.coordinator.releases import (
    load_release_manifest,
    prepare_release,
    unwrap_release_content_key,
    wrap_release_content_key,
)
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.crypto import decrypt_pack, generate_ed25519_keypair, verify_json
from ksat.protocol import PublicQuestion


class AssessmentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pack_dir = Path(self.temporary_directory.name) / "packs"
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE tests (
              test_id INTEGER PRIMARY KEY,
              test_name TEXT NOT NULL,
              release_id TEXT
            );
            CREATE TABLE questions (
              question_id INTEGER PRIMARY KEY,
              question_text TEXT NOT NULL,
              correct_answer TEXT NOT NULL,
              explanation TEXT NOT NULL,
              solution_steps TEXT NOT NULL,
              option_explanations TEXT NOT NULL
            );
            CREATE TABLE attempts (
              attempt_id TEXT PRIMARY KEY,
              test_id INTEGER,
              student_id TEXT
            );
            """
        )
        migrate_distributed_schema(self.connection)
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (41, 'Placement Set')")
        self.connection.executemany(
            """INSERT INTO questions
               (question_id, question_text, correct_answer, explanation, solution_steps, option_explanations)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (7, "Seven?", "B", "private rationale", '["private step"]', '{"A":"private"}'),
                (3, "Three?", "A", "private rationale", '["private step"]', '{"B":"private"}'),
            ],
        )
        self.private_key_b64, self.public_key_b64 = generate_ed25519_keypair()
        self.master_key = bytes(range(32))
        self.asset_bytes = b"\x89PNG\r\n\x1a\npublic-image"
        self.asset_name = f"assets/{hashlib.sha256(self.asset_bytes).hexdigest()}.png"

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def public_questions(self):
        return [
            PublicQuestion(
                question_id=7,
                source_key="q-7",
                category="Reasoning",
                chapter="Series",
                difficulty="Medium",
                question_text="Seven?",
                question_html="<p>Seven?</p>",
                options={"A": "6", "B": "7"},
                display_media={
                    "question": {
                        "url": self.asset_name,
                        "alt_text": "Public diagram",
                        "width": 10,
                        "height": 5,
                    }
                },
            ),
            PublicQuestion(
                question_id=3,
                source_key="q-3",
                category="Quantitative",
                chapter="Numbers",
                difficulty="Easy",
                question_text="Three?",
                options={"A": "3", "B": "4"},
                stimulus={"id": "chart-1", "type": "chart", "content": {"values": [3, 4]}},
            ),
        ]

    def prepare_release_with_two_questions(self):
        return prepare_release(
            self.connection,
            test_id=41,
            selected_questions=self.public_questions(),
            assets={self.asset_name: self.asset_bytes},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )

    def test_release_freezes_questions_excludes_private_material_and_verifies_signature(self):
        release = self.prepare_release_with_two_questions()
        pack_path = self.pack_dir / release.content_pack_filename
        encrypted = pack_path.read_bytes()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        payload = decrypt_pack(content_key, release.release_id, encrypted)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            questions = json.loads(archive.read("questions.json"))
            manifest = json.loads(archive.read("manifest.json"))
            names = archive.namelist()
            timestamps = {entry.date_time for entry in archive.infolist()}
            embedded_asset = archive.read(self.asset_name)

        self.assertEqual([3, 7], release.canonical_question_ids)
        self.assertEqual([item["question_id"] for item in questions], release.canonical_question_ids)
        self.assertEqual(["manifest.json", "questions.json", self.asset_name], names)
        self.assertEqual({(1980, 1, 1, 0, 0, 0)}, timestamps)
        self.assertEqual(self.asset_bytes, embedded_asset)
        self.assertEqual(hashlib.sha256(encrypted).hexdigest(), release.content_hash)
        serialized = json.dumps(questions, sort_keys=True).lower()
        for forbidden in (
            "correct_answer",
            "answer",
            "explanation",
            "solution",
            "option_explanations",
            "feedback",
            "/api/question-banks/",
            "/api/question-assets/",
        ):
            self.assertNotIn(forbidden, serialized)
        verify_json(
            self.public_key_b64,
            {
                "release_id": release.release_id,
                "content_hash": release.content_hash,
                "manifest": manifest,
            },
            release.content_signature_b64,
        )

        before_bytes = pack_path.read_bytes()
        before_mtime = pack_path.stat().st_mtime_ns
        self.connection.execute("UPDATE questions SET question_text='changed' WHERE question_id=3")
        persisted = load_release_manifest(self.connection, release.release_id)
        repeated = prepare_release(
            self.connection,
            test_id=41,
            selected_questions=list(reversed(self.public_questions())),
            assets={},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T10:00:00+00:00",
        )
        self.assertEqual(release, persisted)
        self.assertEqual(release, repeated)
        self.assertEqual(before_bytes, pack_path.read_bytes())
        self.assertEqual(before_mtime, pack_path.stat().st_mtime_ns)
        self.assertEqual(
            [(3, 0), (7, 1)],
            [tuple(row) for row in self.connection.execute(
                "SELECT question_id, canonical_order FROM release_questions ORDER BY canonical_order"
            )],
        )

    def test_wrapped_content_keys_use_fresh_authenticated_encryption(self):
        content_key = b"k" * 32
        first = wrap_release_content_key(self.master_key, "release-1", content_key)
        second = wrap_release_content_key(self.master_key, "release-1", content_key)
        self.assertNotEqual(first, second)
        self.assertEqual(content_key, unwrap_release_content_key(self.master_key, "release-1", first))
        with self.assertRaises(ValueError):
            unwrap_release_content_key(self.master_key, "release-2", first)

    def test_database_failure_removes_pack_and_release_rows(self):
        self.connection.execute(
            """CREATE TRIGGER reject_release BEFORE INSERT ON assessment_releases
               BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.prepare_release_with_two_questions()
        self.assertEqual([], list(self.pack_dir.glob("*.ksatpack")))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM assessment_releases").fetchone()[0])
        self.assertIsNone(self.connection.execute("SELECT release_id FROM tests WHERE test_id=41").fetchone()[0])

    def test_release_rejects_answer_flags_hidden_in_question_html(self):
        unsafe = self.public_questions()[0].model_copy(
            update={"question_html": '<p data-correct-answer="B">Seven?</p>'}
        )
        with self.assertRaisesRegex(ValueError, "Private assessment material"):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=[unsafe],
                assets={self.asset_name: self.asset_bytes},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )
        self.assertEqual([], list(self.pack_dir.glob("*.ksatpack")))


class FacultyReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = app.DATA_DIR
        self.original_db_path = app.DB_PATH
        self.original_backup_dir = app.BACKUP_DIR
        self.original_question_banks_dir = app.QUESTION_BANKS_DIR
        self.original_coordinator_config = app.app.state.coordinator_config
        app.DATA_DIR = Path(self.temporary_directory.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        self.public_media = b"public display media"
        self.solution_media = b"private solution media"
        self.stimulus_media = b"public stimulus media"
        self.public_filename = hashlib.sha256(self.public_media).hexdigest() + ".png"
        self.solution_filename = hashlib.sha256(self.solution_media).hexdigest() + ".png"
        self.stimulus_filename = hashlib.sha256(self.stimulus_media).hexdigest() + ".png"
        with app.db() as connection:
            connection.execute(
                "INSERT INTO admins (username, name, password_hash) VALUES (?, ?, ?)",
                ("faculty", "Faculty", app.hash_password("faculty123")),
            )
            self.bank_id = connection.execute(
                """INSERT INTO question_banks
                   (bank_name, source_html_filename, answer_key_filename, imported_at, format_version)
                   VALUES ('Release Bank', 'questions.zip', 'manifest.json', ?, 3)""",
                (app.now(),),
            ).lastrowid
            asset_dir = app.question_assets_dir() / str(self.bank_id)
            asset_dir.mkdir(parents=True)
            (asset_dir / self.public_filename).write_bytes(self.public_media)
            (asset_dir / self.solution_filename).write_bytes(self.solution_media)
            (asset_dir / self.stimulus_filename).write_bytes(self.stimulus_media)
            connection.execute(
                """INSERT INTO stimuli
                   (bank_id, stimulus_id, stimulus_type, title, alt_text, asset_filename, content_json, created_at)
                   VALUES (?, 'stimulus-1', 'image', 'Prompt chart', 'Public chart', ?, '{}', ?)""",
                (self.bank_id, self.stimulus_filename, app.now()),
            )
            media = {
                "question": {
                    "asset_filename": self.public_filename,
                    "alt_text": "Public diagram",
                    "sha256": hashlib.sha256(self.public_media).hexdigest(),
                    "width": 8,
                    "height": 6,
                },
                "solution": [{
                    "asset_filename": self.solution_filename,
                    "alt_text": "Private worked diagram",
                    "sha256": hashlib.sha256(self.solution_media).hexdigest(),
                    "width": 8,
                    "height": 6,
                }],
            }
            self.question_id = connection.execute(
                """INSERT INTO questions
                   (source_key, question_text, question_html, category, chapter, stimulus_id, difficulty,
                    option_a, option_b, option_c, option_d, options_json, correct_answer, explanation,
                    bank_id, created_at, solution_steps, option_explanations, display_media_json)
                   VALUES ('release-q1', 'What is shown?', '<p>What is shown?</p>', 'Reasoning', 'Charts',
                           'stimulus-1', 'Easy', 'One', 'Two', 'Three', 'Four', ?, 'B',
                           'private rationale', ?, ?, '["private step"]', '{"A":"private"}', ?)""",
                (json.dumps({"A": "One", "B": "Two", "C": "Three", "D": "Four"}),
                 self.bank_id, app.now(), json.dumps(media)),
            ).lastrowid
        self.client = TestClient(app.app, raise_server_exceptions=False)
        login = self.client.post(
            "/api/login",
            json={"identifier": "faculty", "password": "faculty123", "role": "admin"},
        )
        self.assertEqual(200, login.status_code, login.text)

    def tearDown(self):
        self.client.close()
        app.DATA_DIR = self.original_data_dir
        app.DB_PATH = self.original_db_path
        app.BACKUP_DIR = self.original_backup_dir
        app.QUESTION_BANKS_DIR = self.original_question_banks_dir
        app.app.state.coordinator_config = self.original_coordinator_config
        self.temporary_directory.cleanup()

    def create_payload(self, name="Frozen Faculty Test"):
        return {
            "test_name": name,
            "bank_id": self.bank_id,
            "selection_rules": [{"category": "Reasoning", "chapter": "Charts", "quantity": 1}],
            "difficulties": ["Easy"],
        }

    def test_public_release_material_embeds_only_public_media(self):
        with app.db() as connection:
            selected = connection.execute(
                """SELECT question_id, category, chapter, stimulus_id
                   FROM questions WHERE question_id = ?""",
                (self.question_id,),
            ).fetchall()
            questions, assets = app.public_release_material(connection, selected)
        payload = questions[0].model_dump(mode="json", exclude_none=True)
        serialized = json.dumps(payload, sort_keys=True).lower()
        expected_assets = {
            f"assets/{hashlib.sha256(self.public_media).hexdigest()}.png": self.public_media,
            f"assets/{hashlib.sha256(self.stimulus_media).hexdigest()}.png": self.stimulus_media,
        }
        self.assertEqual(expected_assets, assets)
        self.assertEqual("<p>What is shown?</p>", payload["question_html"])
        self.assertEqual("assets/" + self.stimulus_filename, payload["stimulus"]["url"])
        self.assertEqual("assets/" + self.public_filename, payload["display_media"]["question"]["url"])
        for forbidden in ("solution", "answer", "explanation", "feedback", "/api/"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn(self.solution_media, assets.values())

    def test_create_and_legacy_launch_prepare_once_without_resampling_history(self):
        created = self.client.post("/api/admin/tests", json=self.create_payload())
        self.assertEqual(200, created.status_code, created.text)
        created_test_id = created.json()["test_id"]
        listing = self.client.get("/api/admin/tests")
        self.assertEqual(200, listing.status_code, listing.text)
        item = next(test for test in listing.json()["tests"] if test["test_id"] == created_test_id)
        self.assertEqual("prepared", item["release_state"])
        self.assertEqual(64, len(item["content_hash"]))
        self.assertTrue(item["release_id"])
        pack_path = app.DATA_DIR / "Assessment Releases" / f"{item['release_id']}.ksatpack"
        pack_bytes = pack_path.read_bytes()
        pack_mtime = pack_path.stat().st_mtime_ns

        launched = self.client.post(f"/api/admin/tests/{created_test_id}/launch")
        self.assertEqual(200, launched.status_code, launched.text)
        relaunched = self.client.post(f"/api/admin/tests/{created_test_id}/launch")
        self.assertEqual(200, relaunched.status_code, relaunched.text)
        self.assertEqual(pack_bytes, pack_path.read_bytes())
        self.assertEqual(pack_mtime, pack_path.stat().st_mtime_ns)

        with app.db() as connection:
            legacy_test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, bank_id, created_at, active, launched, mode, difficulties)
                   VALUES ('Legacy Unlaunched', ?, ?, ?, 1, 0, 'faculty', '[\"Easy\"]')""",
                (json.dumps(self.create_payload()["selection_rules"]), self.bank_id, app.now()),
            ).lastrowid
            historical_test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, bank_id, created_at, active, launched, mode, difficulties)
                   VALUES ('Historical Submitted', ?, ?, ?, 1, 0, 'faculty', '[\"Easy\"]')""",
                (json.dumps(self.create_payload()["selection_rules"]), self.bank_id, app.now()),
            ).lastrowid
            connection.execute(
                """INSERT INTO students
                   (student_id, name, password_hash, class, section, created_at)
                   VALUES ('S-HISTORY', 'History', 'hash', 'AIML', 'A', ?)""",
                (app.now(),),
            )
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, started_at, submitted_at, status, total_questions)
                   VALUES ('historical-attempt', 'S-HISTORY', ?, ?, ?, 'submitted', 1)""",
                (historical_test_id, app.now(), app.now()),
            )

        state_listing = self.client.get("/api/admin/tests")
        historical_state = next(
            test for test in state_listing.json()["tests"] if test["test_id"] == historical_test_id
        )
        self.assertEqual("failed", historical_state["release_state"])
        legacy_launch = self.client.post(f"/api/admin/tests/{legacy_test_id}/launch")
        self.assertEqual(200, legacy_launch.status_code, legacy_launch.text)
        historical_launch = self.client.post(f"/api/admin/tests/{historical_test_id}/launch")
        self.assertEqual(409, historical_launch.status_code, historical_launch.text)
        with app.db() as connection:
            legacy_release_id = connection.execute(
                "SELECT release_id FROM tests WHERE test_id = ?", (legacy_test_id,)
            ).fetchone()[0]
            historical = connection.execute(
                "SELECT release_id FROM tests WHERE test_id = ?", (historical_test_id,)
            ).fetchone()[0]
            historical_attempts = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE test_id = ? AND status = 'submitted'",
                (historical_test_id,),
            ).fetchone()[0]
        self.assertTrue(legacy_release_id)
        self.assertIsNone(historical)
        self.assertEqual(1, historical_attempts)


if __name__ == "__main__":
    unittest.main()
