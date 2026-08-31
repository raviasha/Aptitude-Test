import base64
import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

import app
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import device_request_bytes


class ClientAuthApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = app.DATA_DIR
        self.original_db_path = app.DB_PATH
        self.original_backup_dir = app.BACKUP_DIR
        self.original_question_banks_dir = app.QUESTION_BANKS_DIR
        self.original_coordinator_config = app.app.state.coordinator_config
        self.original_enrollment_code = os.environ.get("KSAT_DEVICE_ENROLLMENT_CODE")
        self.original_session_secret = os.environ.get("SESSION_SECRET")
        self.original_client_session_secret = os.environ.get("KSAT_SESSION_SECRET")
        app.DATA_DIR = Path(self.temp_dir.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = "lab-enroll-test"
        os.environ["SESSION_SECRET"] = "client-auth-test-secret"
        os.environ.pop("KSAT_SESSION_SECRET", None)
        app.ensure_schema()
        app.register_student("S100", "Student One", "AIML", "A", "student123")
        self.browser_session_secret = "client-auth-test-secret"
        app.configure_coordinator_state(app.app)
        self.session_secret = app.app.state.coordinator_config.session_secret
        self.private_key, self.public_key = generate_ed25519_keypair()
        self.client = TestClient(app.app)

    def tearDown(self):
        self.client.close()
        if self.original_enrollment_code is None:
            os.environ.pop("KSAT_DEVICE_ENROLLMENT_CODE", None)
        else:
            os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = self.original_enrollment_code
        if self.original_session_secret is None:
            os.environ.pop("SESSION_SECRET", None)
        else:
            os.environ["SESSION_SECRET"] = self.original_session_secret
        if self.original_client_session_secret is None:
            os.environ.pop("KSAT_SESSION_SECRET", None)
        else:
            os.environ["KSAT_SESSION_SECRET"] = self.original_client_session_secret
        app.DATA_DIR = self.original_data_dir
        app.DB_PATH = self.original_db_path
        app.BACKUP_DIR = self.original_backup_dir
        app.QUESTION_BANKS_DIR = self.original_question_banks_dir
        app.app.state.coordinator_config = self.original_coordinator_config
        self.temp_dir.cleanup()

    def enroll(self, label):
        response = self.client.post("/api/client/v1/devices/enroll", json={
            "label": label,
            "public_key_b64": self.public_key,
            "enrollment_code": "lab-enroll-test",
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def device_post(
        self,
        path,
        device_id,
        *,
        payload,
        nonce=None,
        timestamp=None,
        private_key=None,
        signed_path=None,
        signed_payload=None,
    ):
        body = _json_bytes(payload)
        signed_body = body if signed_payload is None else _json_bytes(signed_payload)
        timestamp = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        nonce = nonce or str(uuid.uuid4())
        headers = self.device_headers(
            "POST",
            signed_path or path,
            device_id,
            signed_body,
            timestamp,
            nonce,
            private_key=private_key,
        )
        return self.client.post(path, content=body, headers=headers)

    def device_headers(self, method, path, device_id, body, timestamp, nonce, *, private_key=None):
        signing_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key or self.private_key)
        )
        signature = signing_key.sign(device_request_bytes(method, path, body, timestamp, nonce))
        return {
            "Content-Type": "application/json",
            "X-KSAT-Device": device_id,
            "X-KSAT-Timestamp": timestamp,
            "X-KSAT-Nonce": nonce,
            "X-KSAT-Signature": base64.b64encode(signature).decode("ascii"),
        }

    def test_enrollment_requires_the_configured_code(self):
        response = self.client.post("/api/client/v1/devices/enroll", json={
            "label": "Lab-01",
            "public_key_b64": self.public_key,
            "enrollment_code": "wrong",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_enrollment_code")

    def test_unicode_enrollment_code_returns_structured_rejection(self):
        client = TestClient(app.app, raise_server_exceptions=False)
        try:
            response = client.post("/api/client/v1/devices/enroll", json={
                "label": "Lab-01",
                "public_key_b64": self.public_key,
                "enrollment_code": "wrong-🔒",
            })
        finally:
            client.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_enrollment_code")

    def test_generated_client_session_secret_is_stable_and_separate_from_browser_secret(self):
        first = app.app.state.coordinator_config.session_secret
        app.configure_coordinator_state(app.app)
        second = app.app.state.coordinator_config.session_secret

        self.assertNotEqual(first, self.browser_session_secret)
        self.assertEqual(first, second)
        self.assertEqual((app.DATA_DIR / "secrets" / "client-session.key").stat().st_size, 32)

    def test_client_session_secret_honors_explicit_environment_override(self):
        os.environ["KSAT_SESSION_SECRET"] = "explicit-client-session-secret"
        app.configure_coordinator_state(app.app)

        self.assertEqual(
            app.app.state.coordinator_config.session_secret,
            "explicit-client-session-secret",
        )

    def test_corrupt_persisted_client_session_secret_refuses_startup(self):
        os.environ.pop("KSAT_SESSION_SECRET", None)
        secret_path = app.DATA_DIR / "secrets" / "client-session.key"
        secret_path.write_bytes(b"corrupt")

        with self.assertRaises(ValueError):
            app.configure_coordinator_state(app.app)

    def test_concurrent_client_session_secret_creation_publishes_one_winner(self):
        from ksat.coordinator.auth import load_or_create_client_session_secret

        caller_count = 8
        barrier = threading.Barrier(caller_count)
        candidate_lock = threading.Lock()
        candidate_number = 0
        secrets_dir = app.DATA_DIR / "concurrent-secrets"

        def distinct_candidate(_length):
            nonlocal candidate_number
            with candidate_lock:
                candidate_number += 1
                candidate = bytes([candidate_number]) * 32
            barrier.wait(timeout=5)
            return candidate

        with patch("ksat.coordinator.auth.os.urandom", side_effect=distinct_candidate):
            with ThreadPoolExecutor(max_workers=caller_count) as executor:
                futures = [
                    executor.submit(load_or_create_client_session_secret, secrets_dir)
                    for _ in range(caller_count)
                ]
                returned_secrets = []
                errors = []
                for future in futures:
                    try:
                        returned_secrets.append(future.result(timeout=10))
                    except Exception as error:  # The assertion below reports concurrent failures.
                        errors.append(type(error).__name__)

        self.assertEqual(errors, [])
        persisted_secret = (secrets_dir / "client-session.key").read_bytes()
        encoded_persisted_secret = base64.urlsafe_b64encode(persisted_secret).decode("ascii")
        self.assertEqual(len(set(returned_secrets)), 1)
        self.assertEqual(returned_secrets, [encoded_persisted_secret] * caller_count)
        self.assertEqual(len(persisted_secret), 32)

    def test_student_session_is_bound_to_active_device(self):
        enrolled = self.enroll("Lab-01")
        response = self.device_post("/api/client/v1/session", enrolled["device_id"], payload={
            "student_id": "S100",
            "password": "student123",
            "device_id": enrolled["device_id"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        from ksat.coordinator.auth import verify_student_access_token

        claims = verify_student_access_token(
            self.session_secret, response.json()["access_token"], max_age_seconds=43_200
        )
        self.assertEqual(claims, {"student_id": "S100", "device_id": enrolled["device_id"]})
        with app.db() as connection:
            session_count = connection.execute("SELECT COUNT(*) FROM student_sessions").fetchone()[0]
        self.assertEqual(session_count, 0)

    def test_enrollment_rejects_non_raw_ed25519_key(self):
        response = self.client.post("/api/client/v1/devices/enroll", json={
            "label": "Lab-01",
            "public_key_b64": base64.b64encode(b"x" * 31).decode("ascii"),
            "enrollment_code": "lab-enroll-test",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_enrollment_rejects_blank_device_label(self):
        response = self.client.post("/api/client/v1/devices/enroll", json={
            "label": "   ",
            "public_key_b64": self.public_key,
            "enrollment_code": "lab-enroll-test",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_inactive_device_cannot_create_session(self):
        enrolled = self.enroll("Lab-01")
        with app.db() as connection:
            connection.execute(
                "UPDATE devices SET status = 'inactive' WHERE device_id = ?", (enrolled["device_id"],)
            )
        response = self.device_post("/api/client/v1/session", enrolled["device_id"], payload={
            "student_id": "S100",
            "password": "student123",
            "device_id": enrolled["device_id"],
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "device_inactive")

    def test_session_rejects_invalid_student_credentials(self):
        enrolled = self.enroll("Lab-01")
        response = self.device_post("/api/client/v1/session", enrolled["device_id"], payload={
            "student_id": "S100",
            "password": "wrong-password",
            "device_id": enrolled["device_id"],
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "invalid_credentials")

    def test_session_payload_must_match_signing_device(self):
        enrolled = self.enroll("Lab-01")
        response = self.device_post("/api/client/v1/session", enrolled["device_id"], payload={
            "student_id": "S100",
            "password": "student123",
            "device_id": "another-device",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_session_rejects_signature_from_another_device_key(self):
        enrolled = self.enroll("Lab-01")
        other_private_key, _ = generate_ed25519_keypair()
        response = self.device_post(
            "/api/client/v1/session",
            enrolled["device_id"],
            private_key=other_private_key,
            payload={
                "student_id": "S100",
                "password": "student123",
                "device_id": enrolled["device_id"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_rejects_timestamp_outside_five_minutes(self):
        enrolled = self.enroll("Lab-01")
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5, seconds=1)).isoformat(
            timespec="seconds"
        )
        response = self.device_post(
            "/api/client/v1/session",
            enrolled["device_id"],
            timestamp=stale,
            payload={
                "student_id": "S100",
                "password": "student123",
                "device_id": enrolled["device_id"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_rejects_future_timestamp_outside_five_minutes(self):
        enrolled = self.enroll("Lab-01")
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
            timespec="seconds"
        )
        response = self.device_post(
            "/api/client/v1/session",
            enrolled["device_id"],
            timestamp=future,
            payload={
                "student_id": "S100",
                "password": "student123",
                "device_id": enrolled["device_id"],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_signature_binds_exact_body(self):
        enrolled = self.enroll("Lab-01")
        device_id = enrolled["device_id"]
        signed_payload = {
            "student_id": "S100",
            "password": "student123",
            "device_id": device_id,
        }
        response = self.device_post(
            "/api/client/v1/session",
            device_id,
            signed_payload=signed_payload,
            payload={**signed_payload, "student_id": "S101"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_signature_binds_exact_path(self):
        enrolled = self.enroll("Lab-01")
        device_id = enrolled["device_id"]
        response = self.device_post(
            "/api/client/v1/session",
            device_id,
            signed_path="/api/client/v1/not-session",
            payload={
                "student_id": "S100",
                "password": "student123",
                "device_id": device_id,
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_nonce_is_rejected_on_replay(self):
        enrolled = self.enroll("Lab-01")
        nonce = str(uuid.uuid4())
        payload = {
            "student_id": "S100",
            "password": "student123",
            "device_id": enrolled["device_id"],
        }
        first = self.device_post(
            "/api/client/v1/session", enrolled["device_id"], nonce=nonce, payload=payload
        )
        replay = self.device_post(
            "/api/client/v1/session", enrolled["device_id"], nonce=nonce, payload=payload
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(replay.status_code, 403)
        self.assertEqual(replay.json()["detail"]["code"], "invalid_device_key")

    def test_concurrent_device_requests_accept_same_nonce_exactly_once(self):
        enrolled = self.enroll("Lab-01")
        device_id = enrolled["device_id"]
        nonce = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {
            "student_id": "S100",
            "password": "student123",
            "device_id": device_id,
        }
        barrier = threading.Barrier(2)

        def send():
            barrier.wait(timeout=5)
            return self.device_post(
                "/api/client/v1/session",
                device_id,
                nonce=nonce,
                timestamp=timestamp,
                payload=payload,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(send), executor.submit(send))
            responses = [future.result(timeout=10) for future in futures]

        self.assertEqual(sorted(response.status_code for response in responses), [200, 403])
        rejected = next(response for response in responses if response.status_code == 403)
        self.assertEqual(rejected.json()["detail"]["code"], "invalid_device_key")

    def test_device_request_nonce_is_pruned_after_replay_window(self):
        from ksat.coordinator.auth import AuthenticationProblem, verify_device_request

        enrolled = self.enroll("Lab-01")
        device_id = enrolled["device_id"]
        nonce = str(uuid.uuid4())
        body = _json_bytes({"probe": "replay-window"})
        first_time = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        second_time = first_time + timedelta(minutes=5, seconds=1)

        accepted = 0
        with app.db() as connection:
            for current_time in (first_time, second_time):
                timestamp = current_time.isoformat(timespec="seconds")
                headers = self.device_headers(
                    "POST", "/api/client/v1/probe", device_id, body, timestamp, nonce
                )
                try:
                    verify_device_request(
                        connection,
                        device_id=device_id,
                        method="POST",
                        path="/api/client/v1/probe",
                        body=body,
                        timestamp=timestamp,
                        nonce=nonce,
                        signature_b64=headers["X-KSAT-Signature"],
                        now_utc=current_time,
                    )
                    accepted += 1
                except AuthenticationProblem:
                    pass
        self.assertEqual(accepted, 2)

    def test_student_token_requires_exact_nonblank_claims(self):
        from ksat.coordinator.auth import AuthenticationProblem, TOKEN_SALT, verify_student_access_token

        token = URLSafeTimedSerializer(self.session_secret, salt=TOKEN_SALT).dumps({
            "student_id": "S100",
            "device_id": "device-1",
            "role": "student",
        })
        with self.assertRaises(AuthenticationProblem) as caught:
            verify_student_access_token(self.session_secret, token)
        self.assertEqual(caught.exception.code, "invalid_client_session")

        blank_token = URLSafeTimedSerializer(self.session_secret, salt=TOKEN_SALT).dumps({
            "student_id": " ",
            "device_id": "device-1",
        })
        with self.assertRaises(AuthenticationProblem) as blank:
            verify_student_access_token(self.session_secret, blank_token)
        self.assertEqual(blank.exception.code, "invalid_client_session")

    def test_student_token_converts_bad_signature_to_problem_code(self):
        from ksat.coordinator.auth import (
            AuthenticationProblem,
            issue_student_access_token,
            verify_student_access_token,
        )

        with self.assertRaises(AuthenticationProblem) as caught:
            verify_student_access_token(self.session_secret, "not-a-signed-token")
        self.assertEqual(caught.exception.code, "invalid_client_session")

        expired_token = issue_student_access_token(self.session_secret, "S100", "device-1")
        with self.assertRaises(AuthenticationProblem) as expired:
            verify_student_access_token(self.session_secret, expired_token, max_age_seconds=-1)
        self.assertEqual(expired.exception.code, "invalid_client_session")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
