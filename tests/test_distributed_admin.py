import base64
import json
import tempfile
import threading
import unittest
import uuid
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    AttemptTicket,
    PublicQuestion,
    SignedAttemptTicket,
    canonical_json,
    deterministic_question_order,
)
from ksat.coordinator.releases import prepare_release
from ksat.coordinator.attempts import issue_attempt_ticket


NOW = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)


class DistributedAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.originals = (
            app.DATA_DIR,
            app.DB_PATH,
            app.BACKUP_DIR,
            app.QUESTION_BANKS_DIR,
            app.app.state.coordinator_config,
        )
        app.DATA_DIR = Path(self.temp.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        with app.db() as connection:
            if not connection.execute("SELECT 1 FROM admins WHERE username='faculty'").fetchone():
                connection.execute(
                    "INSERT INTO admins VALUES ('faculty','Faculty',?)",
                    (app.hash_password("faculty123"),),
                )
            for index in range(3):
                connection.execute(
                    "INSERT INTO students VALUES (?,?,?,?,?,?)",
                    (f"S{index}", f"Student {index}", "unused", "AIML", "A", app.now()),
                )
            bank_id = connection.execute(
                "INSERT INTO question_banks (bank_name,source_html_filename,answer_key_filename,imported_at,format_version) VALUES ('Bank','bank.html','answers.json',?,2)",
                (app.now(),),
            ).lastrowid
            question_ids = []
            for index in range(3):
                question_ids.append(connection.execute(
                    """INSERT INTO questions
                    (question_text,source_key,category,chapter,difficulty,option_a,option_b,option_c,option_d,
                     options_json,correct_answer,explanation,active,bank_id,question_html,solution_steps,
                     option_explanations,display_media_json,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
                    (f"Question {index}", f"q-{index}", "Quantitative Aptitude", "Arithmetic", "Easy",
                     "A", "B", "C", "D", '{}', "A", "private answer", bank_id,
                     f"<p>Question {index}</p>", '[]', '{}', '{}', app.now()),
                ).lastrowid)
            self.test_id = connection.execute(
                """INSERT INTO tests
                (test_name,composition,bank_id,created_at,active,launched,mode,difficulties)
                VALUES ('Distributed Set',?, ?, ?,1,1,'faculty','[\"Easy\"]')""",
                (json.dumps([{"category":"Quantitative Aptitude","chapter":"Arithmetic","quantity":3}]), bank_id, app.now()),
            ).lastrowid
            questions = [PublicQuestion(
                question_id=qid, source_key=f"q-{index}", category="Quantitative Aptitude",
                chapter="Arithmetic", difficulty="Easy", question_text=f"Question {index}",
                question_html=f"<p>Question {index}</p>", options={"A":"A","B":"B","C":"C","D":"D"},
            ) for index, qid in enumerate(question_ids)]
            release = prepare_release(
                connection, test_id=self.test_id, selected_questions=questions, assets={},
                pack_dir=app.assessment_packs_dir(),
                signing_private_key_b64=app.app.state.coordinator_config.signing_private_key_b64,
                pack_master_key=app.app.state.coordinator_config.pack_master_key,
                now_iso=app.now(),
            )
            self.release_id = release.release_id
            connection.execute(
                "UPDATE release_questions SET options_json='[\"A\",\"B\",\"C\",\"D\"]', correct_answer='A', category='Quantitative Aptitude', chapter='Arithmetic' WHERE release_id=?",
                (self.release_id,),
            )
            connection.execute(
                "UPDATE assessment_releases SET state='launched',launch_opens_at=?,launch_closes_at=? WHERE release_id=?",
                (NOW.isoformat(), (NOW + timedelta(minutes=10)).isoformat(), self.release_id),
            )
            connection.execute(
                "UPDATE tests SET release_id=?,launch_closes_at=?,launch_expires_at=? WHERE test_id=?",
                (self.release_id, (NOW + timedelta(minutes=10)).isoformat(), (NOW + timedelta(minutes=10)).isoformat(), self.test_id),
            )
            _, public = generate_ed25519_keypair()
            self.device_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO devices VALUES (?,?,?,'active',?,NULL)",
                (self.device_id, "Lab 01", public, app.now()),
            )
            self.seed = base64.b64encode(b"o" * 32).decode("ascii")
            self.attempt_id = str(uuid.uuid4())
            ticket = AttemptTicket(
                attempt_id=self.attempt_id,
                student_id="S0",
                device_id=self.device_id,
                release_id=self.release_id,
                content_hash=release.content_hash,
                started_at=NOW,
                deadline=NOW + timedelta(minutes=30),
                order_seed_b64=self.seed,
                content_key_b64=base64.b64encode(b"k" * 32).decode("ascii"),
            )
            signed_ticket = SignedAttemptTicket(
                ticket=ticket,
                signature_b64=sign_json(
                    app.app.state.coordinator_config.signing_private_key_b64, ticket
                ),
            )
            connection.execute(
                """INSERT INTO attempts
                (attempt_id,student_id,test_id,release_id,device_id,order_seed,ticket_json,started_at,status,total_questions,expires_at)
                VALUES (?,?,?,?,?,?,?,?,'in_progress',3,?)""",
                (self.attempt_id, "S0", self.test_id, self.release_id, self.device_id, self.seed,
                 canonical_json(signed_ticket).decode("utf-8"), NOW.isoformat(),
                 (NOW + timedelta(minutes=30)).isoformat()),
            )
        self.client = TestClient(app.app)
        login = self.client.post(
            "/api/login",
            json={"identifier": "faculty", "password": "faculty123", "role": "admin"},
        )
        self.assertEqual(login.status_code, 200)
        self.csrf_token = login.json()["csrf_token"]

    def tearDown(self):
        self.client.close()
        app.DATA_DIR, app.DB_PATH, app.BACKUP_DIR, app.QUESTION_BANKS_DIR, app.app.state.coordinator_config = self.originals
        self.temp.cleanup()

    def admin_get(self, path):
        return self.client.get(path)

    def admin_post(self, path, payload=None):
        return self.client.post(
            path,
            json=payload or {},
            headers={"X-KSAT-CSRF": self.csrf_token},
        )

    def test_status_and_devices_are_operational_only(self):
        response = self.admin_get("/api/admin/tests")
        self.assertEqual(response.status_code, 200)
        item = next(value for value in response.json()["tests"] if value["test_id"] == self.test_id)
        self.assertNotIn("content_hash", item)
        self.assertEqual(item["distributed_status"], {"eligible": 3, "started": 1, "submitted": 0, "voided": 0})
        self.assertEqual(len(item["content_hash_prefix"]), 12)
        self.assertIsInstance(response.json()["submission_queue_pending"], int)

        devices = self.admin_get("/api/admin/devices").json()["devices"]
        self.assertEqual(devices[0]["label"], "Lab 01")
        self.assertEqual(len(devices[0]["public_key_fingerprint"]), 12)
        serialized = json.dumps({"tests": response.json(), "devices": devices}).lower()
        for forbidden in ("wrapped_content_key", "correct_answer", "bundle_json", "ticket_json", "public_key_b64", "password_hash"):
            self.assertNotIn(forbidden, serialized)

    def test_device_state_is_idempotent_audited_and_enforced(self):
        first = self.admin_post(f"/api/admin/devices/{self.device_id}/revoke", {"reason":"Computer retired"})
        second = self.admin_post(f"/api/admin/devices/{self.device_id}/revoke", {"reason":"Computer retired"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM devices WHERE device_id=?", (self.device_id,)).fetchone()[0], "revoked")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='device_revoked' AND actor_id='faculty'").fetchone()[0], 1)
        restored = self.admin_post(f"/api/admin/devices/{self.device_id}/reactivate", {"reason":"Machine repaired"})
        self.assertEqual(restored.status_code, 200)

    def test_rotate_enrollment_code_returns_secret_once_and_invalidates_old(self):
        old = app.app.state.coordinator_config.device_enrollment_code
        response = self.admin_post("/api/admin/devices/enrollment-code/rotate", {"reason":"Routine lab rotation"})
        self.assertEqual(response.status_code, 200)
        new = response.json()["enrollment_code"]
        self.assertNotEqual(new, old)
        self.assertGreaterEqual(len(new), 24)
        self.assertNotIn(new, json.dumps(self.admin_get("/api/admin/devices").json()))
        self.assertEqual(app.app.state.coordinator_config.device_enrollment_code, new)
        app.configure_coordinator_state(app.app)
        self.assertEqual(new, app.app.state.coordinator_config.device_enrollment_code)

    def test_externally_managed_enrollment_code_rejects_rotation_without_mutation(self):
        before = app.app.state.coordinator_config.device_enrollment_code
        with patch.dict(os.environ, {"KSAT_DEVICE_ENROLLMENT_CODE": "managed-by-it"}):
            response = self.admin_post(
                "/api/admin/devices/enrollment-code/rotate",
                {"reason": "Must not override IT"},
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual("enrollment_code_managed_externally", response.json()["detail"]["code"])
        self.assertEqual(before, app.app.state.coordinator_config.device_enrollment_code)
        with app.db() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE event_type='device_enrollment_code_rotated'"
                ).fetchone()[0],
            )

    def test_exact_attempt_order_is_derived_without_answers(self):
        response = self.admin_get(f"/api/admin/attempts/{self.attempt_id}/order")
        self.assertEqual(response.status_code, 200)
        with app.db() as connection:
            canonical = [row[0] for row in connection.execute(
                "SELECT question_id FROM release_questions WHERE release_id=? ORDER BY canonical_order", (self.release_id,)
            ).fetchall()]
        self.assertEqual(response.json()["question_order"], deterministic_question_order(canonical, self.seed))
        self.assertNotIn("answer", json.dumps(response.json()).lower())

    def test_attempt_order_fails_closed_when_private_snapshot_is_invalid(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE release_questions SET correct_answer=NULL WHERE release_id=?",
                (self.release_id,),
            )
        response = self.admin_get(f"/api/admin/attempts/{self.attempt_id}/order")
        self.assertEqual(409, response.status_code)
        serialized = json.dumps(response.json()).lower()
        self.assertIn("faculty intervention", serialized)
        self.assertNotIn("correct_answer", serialized)
        self.assertNotIn("private answer", serialized)

    def test_void_requires_confirmation_for_submission_and_preserves_evidence(self):
        with app.db() as connection:
            connection.execute("UPDATE attempts SET status='submitted',submitted_at=? WHERE attempt_id=?", (app.now(), self.attempt_id))
            connection.execute("INSERT INTO submissions VALUES (?,?,?,?,'{}')", (self.attempt_id, "a"*64, '{"sealed":"evidence"}', app.now()))
        rejected = self.admin_post(f"/api/admin/attempts/{self.attempt_id}/void", {
            "reason":"Hardware failure", "authorize_retake":True,
        })
        self.assertEqual(rejected.status_code, 409)
        accepted = self.admin_post(f"/api/admin/attempts/{self.attempt_id}/void", {
            "reason":"Hardware failure", "authorize_retake":True, "confirm_submitted":True,
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["retake_authorized"])
        with app.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM attempts WHERE attempt_id=?", (self.attempt_id,)).fetchone()[0], "voided")
            self.assertEqual(connection.execute("SELECT bundle_json FROM submissions WHERE attempt_id=?", (self.attempt_id,)).fetchone()[0], '{"sealed":"evidence"}')
            event = connection.execute("SELECT actor_id,details_json FROM audit_events WHERE event_type='attempt_voided'").fetchone()
            self.assertEqual(event[0], "faculty")
            self.assertIn("Hardware failure", event[1])

    def test_attempt_and_test_extensions_change_only_active_deadlines(self):
        one = self.admin_post(f"/api/admin/attempts/{self.attempt_id}/extend", {"minutes":5,"reason":"Power interruption"})
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["deadline_after"], (NOW + timedelta(minutes=35)).isoformat(timespec="seconds"))
        all_active = self.admin_post(f"/api/admin/tests/{self.test_id}/extend", {"minutes":5,"reason":"Power interruption"})
        self.assertEqual(all_active.status_code, 200)
        with app.db() as connection:
            deadline = connection.execute("SELECT expires_at FROM attempts WHERE attempt_id=?", (self.attempt_id,)).fetchone()[0]
            extension = connection.execute("SELECT duration_extension_seconds FROM assessment_releases WHERE release_id=?", (self.release_id,)).fetchone()[0]
            self.assertEqual(deadline, (NOW + timedelta(minutes=40)).isoformat(timespec="seconds"))
            self.assertEqual(extension, 5 * 60)

    def test_concurrent_start_and_test_extension_produce_one_consistent_duration(self):
        barrier = threading.Barrier(2)
        result = {}
        with app.db() as connection:
            release = connection.execute(
                "SELECT content_hash,duration_seconds FROM assessment_releases WHERE release_id=?",
                (self.release_id,),
            ).fetchone()
            content_hash = release["content_hash"]
            duration_seconds = release["duration_seconds"]

        def start_second_student():
            barrier.wait()
            with app.db() as connection:
                result["start"] = issue_attempt_ticket(
                    connection,
                    release_id=self.release_id,
                    student_id="S1",
                    device_id=self.device_id,
                    confirmed_content_hash=content_hash,
                    signing_private_key_b64=app.app.state.coordinator_config.signing_private_key_b64,
                    pack_master_key=app.app.state.coordinator_config.pack_master_key,
                    now_utc=NOW,
                )

        worker = threading.Thread(target=start_second_student)
        worker.start()
        barrier.wait()
        extended = self.admin_post(
            f"/api/admin/tests/{self.test_id}/extend",
            {"minutes": 5, "reason": "Power interruption"},
        )
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(200, extended.status_code)
        signed = result["start"].ticket
        expected_deadline = NOW + timedelta(seconds=duration_seconds + 300)
        with app.db() as connection:
            row = connection.execute(
                """SELECT expires_at,deadline_revision,deadline_extension_seconds
                   FROM attempts WHERE attempt_id=?""",
                (signed.ticket.attempt_id,),
            ).fetchone()
        self.assertEqual(expected_deadline.isoformat(timespec="seconds"), row["expires_at"])
        if signed.ticket.duration_extension_seconds == 300:
            self.assertEqual((0, 0), (row["deadline_revision"], row["deadline_extension_seconds"]))
        else:
            self.assertEqual(0, signed.ticket.duration_extension_seconds)
            self.assertEqual((1, 300), (row["deadline_revision"], row["deadline_extension_seconds"]))

    def test_duplicate_copies_configuration_but_not_history_or_release(self):
        response = self.admin_post(f"/api/admin/tests/{self.test_id}/duplicate", {})
        self.assertEqual(response.status_code, 200)
        duplicate = response.json()
        self.assertNotEqual(duplicate["test_id"], self.test_id)
        self.assertNotEqual(duplicate["release_id"], self.release_id)
        with app.db() as connection:
            copied = connection.execute("SELECT * FROM tests WHERE test_id=?", (duplicate["test_id"],)).fetchone()
            self.assertEqual(copied["test_name"], "Distributed Set Copy")
            self.assertEqual(copied["composition"], connection.execute("SELECT composition FROM tests WHERE test_id=?", (self.test_id,)).fetchone()[0])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM attempts WHERE test_id=?", (duplicate["test_id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE test_id=?", (duplicate["test_id"],)).fetchone()[0], 0)

    def test_new_admin_routes_reject_non_admin_without_side_effects(self):
        stranger = TestClient(app.app)
        before = app.app.state.coordinator_config.device_enrollment_code
        self.assertEqual(stranger.post("/api/admin/devices/enrollment-code/rotate", json={"reason":"Not allowed"}).status_code, 401)
        self.assertEqual(stranger.post(f"/api/admin/attempts/{self.attempt_id}/void", json={"reason":"Not allowed"}).status_code, 401)
        self.assertEqual(app.app.state.coordinator_config.device_enrollment_code, before)
        stranger.close()

    def test_hostile_origin_and_cross_session_csrf_fail_before_duplicate_side_effects(self):
        before = None
        with app.db() as connection:
            before = connection.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
        hostile = self.client.post(
            f"/api/admin/tests/{self.test_id}/duplicate",
            headers={"Origin": "https://evil.example", "X-KSAT-CSRF": self.csrf_token},
        )
        other = TestClient(app.app)
        other_login = other.post(
            "/api/login",
            json={"identifier": "faculty", "password": "faculty123", "role": "admin"},
        )
        cross = other.post(
            f"/api/admin/tests/{self.test_id}/duplicate",
            headers={"X-KSAT-CSRF": self.csrf_token},
        )
        self.assertEqual(403, hostile.status_code)
        self.assertEqual(403, cross.status_code)
        self.assertNotEqual(self.csrf_token, other_login.json()["csrf_token"])
        with app.db() as connection:
            self.assertEqual(before, connection.execute("SELECT COUNT(*) FROM tests").fetchone()[0])
        other.close()


if __name__ == "__main__":
    unittest.main()
