import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import app
from ksat.coordinator.auth import issue_student_access_token
from ksat.coordinator.releases import prepare_release, unwrap_release_content_key
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import PublicQuestion, device_request_bytes
from ksat.sqlite import connect_sqlite


OPEN = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 31, 9, 10, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return OPEN if tz is not None else OPEN.replace(tzinfo=None)


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class DistributedAttemptStartTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = app.DATA_DIR
        self.original_db_path = app.DB_PATH
        self.original_backup_dir = app.BACKUP_DIR
        self.original_question_banks_dir = app.QUESTION_BANKS_DIR
        self.original_coordinator_config = app.app.state.coordinator_config
        self.original_enrollment_code = os.environ.get("KSAT_DEVICE_ENROLLMENT_CODE")
        self.original_session_secret = os.environ.get("KSAT_SESSION_SECRET")
        app.DATA_DIR = Path(self.temporary_directory.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = "task-five-enrollment"
        os.environ["KSAT_SESSION_SECRET"] = "task-five-session-secret"
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        self.config = app.app.state.coordinator_config
        self.client = TestClient(app.app)

        with app.db() as connection:
            for index in range(100, 108):
                connection.execute(
                    """INSERT INTO students
                       (student_id, name, password_hash, class, section, created_at)
                       VALUES (?, ?, 'unused', 'AIML', 'A', ?)""",
                    (f"S{index}", f"Student {index}", app.now()),
                )
            self.test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, created_at, active, launched, mode)
                   VALUES ('Distributed Set', '{}', ?, 1, 0, 'faculty')""",
                (app.now(),),
            ).lastrowid
            questions = [
                PublicQuestion(
                    question_id=index,
                    source_key=f"q-{index}",
                    category="Quantitative Aptitude",
                    chapter="Arithmetic",
                    difficulty="Easy",
                    question_text=f"Question {index}?",
                    question_html=f"<p>Question {index}?</p>",
                    options={"A": "1", "B": "2", "C": "3", "D": "4"},
                )
                for index in range(1, 31)
            ]
            release = prepare_release(
                connection,
                test_id=self.test_id,
                selected_questions=questions,
                assets={},
                pack_dir=app.assessment_packs_dir(),
                signing_private_key_b64=self.config.signing_private_key_b64,
                pack_master_key=self.config.pack_master_key,
                now_iso="2026-08-31T08:30:00+00:00",
            )
            self.release_id = release.release_id
            self.content_hash = release.content_hash
        self.devices = {}
        for label in ("device-a", "device-b", "device-c", "device-d"):
            self.add_device(label)
        self.set_launch_state(True)

    def tearDown(self):
        self.client.close()
        if self.original_enrollment_code is None:
            os.environ.pop("KSAT_DEVICE_ENROLLMENT_CODE", None)
        else:
            os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = self.original_enrollment_code
        if self.original_session_secret is None:
            os.environ.pop("KSAT_SESSION_SECRET", None)
        else:
            os.environ["KSAT_SESSION_SECRET"] = self.original_session_secret
        app.DATA_DIR = self.original_data_dir
        app.DB_PATH = self.original_db_path
        app.BACKUP_DIR = self.original_backup_dir
        app.QUESTION_BANKS_DIR = self.original_question_banks_dir
        app.app.state.coordinator_config = self.original_coordinator_config
        self.temporary_directory.cleanup()

    def add_device(self, label):
        private_key_b64, public_key_b64 = generate_ed25519_keypair()
        device_id = str(uuid.uuid4())
        with app.db() as connection:
            connection.execute(
                """INSERT INTO devices
                   (device_id, label, public_key_b64, status, enrolled_at)
                   VALUES (?, ?, ?, 'active', ?)""",
                (device_id, label, public_key_b64, app.now()),
            )
        self.devices[label] = (device_id, private_key_b64)
        return device_id

    def set_launch_state(self, launched, *, opens=OPEN, closes=CLOSE):
        with app.db() as connection:
            connection.execute(
                """UPDATE assessment_releases
                   SET state = ?, launch_opens_at = ?, launch_closes_at = ?
                   WHERE release_id = ?""",
                (
                    "launched" if launched else "prepared",
                    opens.isoformat(timespec="seconds") if launched else None,
                    closes.isoformat(timespec="seconds") if launched else None,
                    self.release_id,
                ),
            )
            connection.execute(
                """UPDATE tests SET launched = ?, launch_expires_at = ?, launch_closes_at = ?
                   WHERE test_id = ?""",
                (
                    1 if launched else 0,
                    closes.isoformat(timespec="seconds") if launched else None,
                    closes.isoformat(timespec="seconds") if launched else None,
                    self.test_id,
                ),
            )

    def headers(self, method, path, label, body=b"", *, token=None, private_key_b64=None, **extra):
        device_id, stored_private_key = self.devices[label]
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        nonce = str(uuid.uuid4())
        signing_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key_b64 or stored_private_key)
        )
        signature = signing_key.sign(device_request_bytes(method, path, body, timestamp, nonce))
        headers = {
            "X-KSAT-Device": device_id,
            "X-KSAT-Timestamp": timestamp,
            "X-KSAT-Nonce": nonce,
            "X-KSAT-Signature": base64.b64encode(signature).decode("ascii"),
            **extra,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def token(self, student_id, label):
        return issue_student_access_token(
            self.config.session_secret, student_id, self.devices[label][0]
        )

    def device_get(self, path, label="device-a", *, student_id=None, at=None, **extra_headers):
        token = self.token(student_id, label) if student_id else None
        request = lambda: self.client.get(
            path, headers=self.headers("GET", path, label, token=token, **extra_headers)
        )
        if at is None:
            return request()
        with patch("ksat.coordinator.routes.utc_now", return_value=datetime.fromisoformat(at)):
            return request()

    def start(self, student_id, label, *, at, payload=None, token_label=None):
        path = "/api/client/v1/attempts/start"
        body_value = payload or {
            "release_id": self.release_id,
            "confirmed_content_hash": self.content_hash,
        }
        body = _json_bytes(body_value)
        token = self.token(student_id, token_label or label)
        with patch("ksat.coordinator.routes.utc_now", return_value=datetime.fromisoformat(at)):
            return self.client.post(
                path,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    **self.headers("POST", path, label, body, token=token),
                },
            )

    def test_pack_is_downloadable_before_launch_but_key_is_not_disclosed(self):
        self.set_launch_state(False)
        catalog = self.device_get("/api/client/v1/releases")
        self.assertEqual(200, catalog.status_code, catalog.text)
        item = next(
            entry for entry in catalog.json()["releases"]
            if entry["release_id"] == self.release_id
        )
        self.assertNotIn("content_key_b64", item)
        self.assertNotIn("wrapped_content_key_b64", item)
        self.assertEqual(
            {
                "release_id", "filename", "content_hash", "pack_signature_b64",
                "byte_size", "pack_format_version",
            },
            set(item),
        )
        pack_response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(200, pack_response.status_code, pack_response.text)
        self.assertEqual(hashlib.sha256(pack_response.content).hexdigest(), item["content_hash"])

    def test_students_get_same_questions_different_orders_and_independent_deadlines(self):
        first = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        second = self.start("S101", "device-b", at="2026-08-31T09:07:00+00:00")
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, second.status_code, second.text)
        first_ticket = first.json()["ticket"]["ticket"]
        second_ticket = second.json()["ticket"]["ticket"]
        self.assertNotEqual(first_ticket["order_seed_b64"], second_ticket["order_seed_b64"])
        self.assertEqual(first.json()["canonical_question_ids"], list(range(1, 31)))
        self.assertEqual(first.json()["canonical_question_ids"], second.json()["canonical_question_ids"])
        self.assertEqual(first_ticket["deadline"], "2026-08-31T09:32:00Z")
        self.assertEqual(second_ticket["deadline"], "2026-08-31T09:37:00Z")
        self.assertNotEqual(first_ticket["attempt_id"], second_ticket["attempt_id"])

    def test_start_boundary_allows_close_timestamp_and_rejects_one_second_later(self):
        allowed = self.start("S102", "device-c", at="2026-08-31T09:10:00+00:00")
        rejected = self.start("S103", "device-d", at="2026-08-31T09:10:01+00:00")
        self.assertEqual(200, allowed.status_code, allowed.text)
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("start_window_closed", rejected.json()["detail"]["code"])

    def test_start_rejects_wrong_hash_not_launched_and_already_submitted(self):
        wrong_hash = self.start(
            "S100", "device-a", at="2026-08-31T09:01:00+00:00",
            payload={"release_id": self.release_id, "confirmed_content_hash": "0" * 64},
        )
        self.assertEqual("content_hash_mismatch", wrong_hash.json()["detail"]["code"])
        self.set_launch_state(False)
        not_launched = self.start("S100", "device-a", at="2026-08-31T09:01:00+00:00")
        self.assertEqual("assessment_not_launched", not_launched.json()["detail"]["code"])
        self.set_launch_state(True)
        with app.db() as connection:
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, release_id, device_id, started_at,
                    submitted_at, status, total_questions, expires_at)
                   VALUES ('submitted-attempt', 'S100', ?, ?, ?, ?, ?, 'submitted', 30, ?)""",
                (
                    self.test_id, self.release_id, self.devices["device-a"][0],
                    "2026-08-31T08:00:00+00:00", "2026-08-31T08:30:00+00:00",
                    "2026-08-31T08:30:00+00:00",
                ),
            )
        submitted = self.start("S100", "device-a", at="2026-08-31T09:01:00+00:00")
        self.assertEqual("already_submitted", submitted.json()["detail"]["code"])

    def test_same_device_resume_after_window_close_returns_exact_ticket_and_deadline(self):
        first = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        pack_path.write_bytes(pack_path.read_bytes() + b"unavailable-after-issuance")
        resumed = self.start("S100", "device-a", at="2026-08-31T09:15:00+00:00")
        other_device = self.start("S100", "device-b", at="2026-08-31T09:05:00+00:00")
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, resumed.status_code, resumed.text)
        self.assertEqual(first.json()["ticket"], resumed.json()["ticket"])
        self.assertEqual("2026-08-31T09:32:00Z", resumed.json()["ticket"]["ticket"]["deadline"])
        self.assertEqual(409, other_device.status_code)
        self.assertEqual("attempt_bound_to_other_device", other_device.json()["detail"]["code"])

    def test_ticket_is_only_key_disclosure_and_unwraps_release_content_key(self):
        started = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        key = base64.b64decode(started.json()["ticket"]["ticket"]["content_key_b64"])
        with app.db() as connection:
            wrapped = connection.execute(
                "SELECT wrapped_content_key_b64 FROM assessment_releases WHERE release_id = ?",
                (self.release_id,),
            ).fetchone()[0]
        self.assertEqual(
            unwrap_release_content_key(self.config.pack_master_key, self.release_id, wrapped), key
        )
        catalog_text = self.device_get("/api/client/v1/releases").text
        self.assertNotIn(base64.b64encode(key).decode("ascii"), catalog_text)

    def test_start_rejects_a_pack_that_is_no_longer_verified_content(self):
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        pack_path.write_bytes(pack_path.read_bytes() + b"tampered")
        response = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("content_not_ready", response.json()["detail"]["code"])
        with app.db() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_start_uses_encrypted_hash_readiness_without_redecrypting_the_pack(self):
        with patch(
            "ksat.coordinator.routes.load_release_manifest",
            side_effect=AssertionError("start hot path must not decrypt and inspect the immutable pack"),
        ):
            response = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        self.assertEqual(200, response.status_code, response.text)

    def test_all_routes_require_device_proof_and_student_routes_bind_token_to_device(self):
        for method, path, payload in (
            ("get", "/api/client/v1/releases", None),
            ("get", f"/api/client/v1/releases/{self.release_id}/pack", None),
            ("get", "/api/client/v1/assessments", None),
            ("post", "/api/client/v1/attempts/start", {
                "release_id": self.release_id, "confirmed_content_hash": self.content_hash,
            }),
        ):
            response = (
                self.client.post(path, json=payload)
                if method == "post"
                else self.client.get(path)
            )
            self.assertEqual(403, response.status_code, (path, response.text))
            self.assertEqual("device_inactive", response.json()["detail"]["code"])
        mismatch = self.start(
            "S100", "device-b", token_label="device-a", at="2026-08-31T09:02:00+00:00"
        )
        self.assertEqual(403, mismatch.status_code)
        self.assertEqual("invalid_client_session", mismatch.json()["detail"]["code"])

    def test_inactive_and_foreign_device_proofs_are_rejected(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE devices SET status = 'inactive' WHERE device_id = ?",
                (self.devices["device-a"][0],),
            )
        inactive = self.device_get("/api/client/v1/releases", "device-a")
        self.assertEqual("device_inactive", inactive.json()["detail"]["code"])
        foreign_private, _ = generate_ed25519_keypair()
        path = "/api/client/v1/releases"
        foreign = self.client.get(
            path,
            headers=self.headers(
                "GET", path, "device-b", private_key_b64=foreign_private
            ),
        )
        self.assertEqual("invalid_device_key", foreign.json()["detail"]["code"])

    def test_body_identity_fields_are_rejected_and_cannot_impersonate(self):
        response = self.start(
            "S100", "device-a", at="2026-08-31T09:02:00+00:00",
            payload={
                "release_id": self.release_id,
                "confirmed_content_hash": self.content_hash,
                "student_id": "S101",
                "device_id": self.devices["device-b"][0],
            },
        )
        self.assertEqual(422, response.status_code, response.text)
        with app.db() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_catalog_filters_not_launched_expired_and_accepted_assessments(self):
        listed = self.device_get(
            "/api/client/v1/assessments", student_id="S100",
            at="2026-08-31T09:05:00+00:00",
        )
        self.assertEqual([self.release_id], [item["release_id"] for item in listed.json()["assessments"]])
        with app.db() as connection:
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, release_id, device_id, started_at,
                    submitted_at, status, total_questions, expires_at)
                   VALUES ('accepted', 'S100', ?, ?, ?, ?, ?, 'submitted', 30, ?)""",
                (
                    self.test_id, self.release_id, self.devices["device-a"][0],
                    "2026-08-31T08:00:00+00:00", "2026-08-31T08:30:00+00:00",
                    "2026-08-31T08:30:00+00:00",
                ),
            )
        hidden = self.device_get(
            "/api/client/v1/assessments", student_id="S100",
            at="2026-08-31T09:05:00+00:00",
        )
        self.assertEqual([], hidden.json()["assessments"])

    def test_pack_etag_304_wrong_hash_and_database_filename_escape(self):
        path = f"/api/client/v1/releases/{self.release_id}/pack"
        first = self.device_get(path)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(f'"{self.content_hash}"', first.headers["etag"])
        self.assertEqual("private, immutable", first.headers["cache-control"])
        cached = self.device_get(path, **{"If-None-Match": f'W/"other", "{self.content_hash}"'})
        self.assertEqual(304, cached.status_code, cached.text)
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        pack_path.write_bytes(pack_path.read_bytes() + b"tampered")
        wrong_hash = self.device_get(path)
        self.assertEqual(409, wrong_hash.status_code)
        self.assertEqual("content_not_ready", wrong_hash.json()["detail"]["code"])
        with app.db() as connection:
            connection.execute(
                "UPDATE assessment_releases SET content_pack_filename = '../escape.ksatpack' WHERE release_id = ?",
                (self.release_id,),
            )
        escaped = self.device_get(path)
        self.assertEqual(409, escaped.status_code)
        self.assertEqual("content_not_ready", escaped.json()["detail"]["code"])

    def test_concurrent_duplicate_starts_store_one_attempt_one_ticket_and_no_responses(self):
        from ksat.coordinator.attempts import issue_attempt_ticket

        barrier = threading.Barrier(2)

        def issue():
            connection = connect_sqlite(app.DB_PATH)
            try:
                barrier.wait(timeout=5)
                return issue_attempt_ticket(
                    connection,
                    release_id=self.release_id,
                    student_id="S100",
                    device_id=self.devices["device-a"][0],
                    confirmed_content_hash=self.content_hash,
                    signing_private_key_b64=self.config.signing_private_key_b64,
                    pack_master_key=self.config.pack_master_key,
                    now_utc=datetime(2026, 8, 31, 9, 2, tzinfo=timezone.utc),
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result(timeout=15) for future in (executor.submit(issue), executor.submit(issue))]
        self.assertEqual(results[0].ticket, results[1].ticket)
        with app.db() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0])

    def test_launch_window_does_not_mutate_attempt_deadlines_and_second_launch_is_typed(self):
        started = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        deadline = started.json()["ticket"]["ticket"]["deadline"]
        with patch("app.require_user", return_value={"role": "admin"}):
            closed = self.client.post(f"/api/admin/tests/{self.test_id}/close")
            relaunched = self.client.post(f"/api/admin/tests/{self.test_id}/launch")
        self.assertEqual(200, closed.status_code, closed.text)
        self.assertEqual(409, relaunched.status_code, relaunched.text)
        self.assertEqual("release_already_used", relaunched.json()["detail"]["code"])
        with app.db() as connection:
            stored = connection.execute(
                "SELECT expires_at FROM attempts WHERE release_id = ?", (self.release_id,)
            ).fetchone()[0]
        self.assertEqual(deadline.replace("Z", "+00:00"), stored)

    def test_faculty_launch_sets_exact_ten_minute_window_without_creating_attempt_deadlines(self):
        self.set_launch_state(False)
        with (
            patch("app.require_user", return_value={"role": "admin"}),
            patch("app.datetime", FrozenDateTime),
        ):
            response = self.client.post(f"/api/admin/tests/{self.test_id}/launch")
        self.assertEqual(200, response.status_code, response.text)
        with app.db() as connection:
            release = connection.execute(
                """SELECT state, launch_opens_at, launch_closes_at
                   FROM assessment_releases WHERE release_id = ?""",
                (self.release_id,),
            ).fetchone()
            test = connection.execute(
                "SELECT launched, launch_closes_at FROM tests WHERE test_id = ?",
                (self.test_id,),
            ).fetchone()
            attempts = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual("launched", release["state"])
        self.assertEqual("2026-08-31T09:00:00+00:00", release["launch_opens_at"])
        self.assertEqual("2026-08-31T09:10:00+00:00", release["launch_closes_at"])
        self.assertEqual(1, test["launched"])
        self.assertEqual("2026-08-31T09:10:00+00:00", test["launch_closes_at"])
        self.assertEqual(0, attempts)


if __name__ == "__main__":
    unittest.main()
