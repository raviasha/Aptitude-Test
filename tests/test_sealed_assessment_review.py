"""Review keys never allow answers to change while their upload is pending."""

import base64
import hashlib
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app
from ksat.coordinator.auth import issue_student_access_token
from ksat.coordinator.releases import wrap_release_content_key, wrap_release_review_key
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.protocol import canonical_json, device_request_bytes
from ksat.crypto import generate_ed25519_keypair
from ksat.sqlite import connect_sqlite
from tests import test_distributed_submission as submission_tests


class SealedAssessmentReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = submission_tests.DistributedSubmissionTests("test_server_scores_bundle_and_duplicate_returns_same_receipt")
        self.fixture.setUp()
        self.signed = self.fixture.bundle()
        self.bundle_hash = hashlib.sha256(canonical_json(self.signed)).hexdigest()
        with app.db() as connection:
            connection.execute(
                """UPDATE assessment_releases
                   SET wrapped_content_key_b64=?, wrapped_review_key_b64=? WHERE release_id=?""",
                (
                    wrap_release_content_key(self.fixture.config.pack_master_key, self.fixture.release_id, b"c" * 32),
                    wrap_release_review_key(self.fixture.config.pack_master_key, self.fixture.release_id, b"r" * 32),
                    self.fixture.release_id,
                ),
            )

    def tearDown(self):
        self.fixture.tearDown()

    def close_assessment(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE tests SET launched=0,review_released_at=? WHERE release_id=?",
                (app.now(), self.fixture.release_id),
            )

    def request_grant(self, bundle_hash=None, *, student_id=None, device_id=None, private_key=None,
                      bearer=True, extra=None):
        path = f"/api/client/v1/reviews/{self.fixture.attempt_id}/sealed"
        body = canonical_json({"bundle_hash": self.bundle_hash if bundle_hash is None else bundle_hash, **(extra or {})})
        device_id = device_id or self.fixture.device_id
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        nonce = str(uuid.uuid4())
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(
            private_key or self.fixture.device_private_key_b64
        ))
        signature = private.sign(device_request_bytes("POST", path, body, timestamp, nonce))
        headers = {
            "Content-Type": "application/json", "X-KSAT-Device": device_id,
            "X-KSAT-Timestamp": timestamp, "X-KSAT-Nonce": nonce,
            "X-KSAT-Signature": base64.b64encode(signature).decode(),
        }
        if bearer:
            token = issue_student_access_token(
                self.fixture.config.session_secret, student_id or self.fixture.student_id, device_id,
            )
            headers["Authorization"] = f"Bearer {token}"
        return self.fixture.client.post(path, content=body, headers=headers)

    def assert_problem(self, response, status, code):
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, response.json()["detail"]["code"])
        self.assertNotIn("review_key_b64", response.text)

    def test_pending_review_requires_explicit_faculty_close(self):
        self.assert_problem(self.request_grant(), 409, "review_not_released")
        with app.db() as connection:
            connection.execute("UPDATE tests SET launched=0 WHERE release_id=?", (self.fixture.release_id,))
        self.assert_problem(self.request_grant(), 409, "review_not_released")
        with app.db() as connection:
            connection.execute("UPDATE tests SET launched=1,review_released_at=? WHERE release_id=?",
                               (app.now(), self.fixture.release_id))
        self.assert_problem(self.request_grant(), 409, "review_not_released")
        with app.db() as connection:
            self.assertIsNone(connection.execute("SELECT review_seal_hash FROM attempts WHERE attempt_id=?",
                                                (self.fixture.attempt_id,)).fetchone()[0])

    def test_closed_pending_review_commits_hash_without_upload_and_retries_after_reopen(self):
        self.close_assessment()
        first = self.request_grant()
        self.assertEqual(200, first.status_code, first.text)
        grant = first.json()
        self.assertEqual({"attempt_id", "student_id", "release_id", "content_hash",
                          "content_key_b64", "review_key_b64", "bundle_hash"}, set(grant))
        self.assertEqual(self.bundle_hash, grant["bundle_hash"])
        self.assertEqual(b"r" * 32, base64.b64decode(grant["review_key_b64"]))
        self.assertEqual(b"c" * 32, base64.b64decode(grant["content_key_b64"]))
        self.assertEqual(0, self.fixture.count("submissions"))
        self.assertEqual(0, self.fixture.count("responses"))
        connection = connect_sqlite(app.DB_PATH)
        try:
            migrate_distributed_schema(connection)
            connection.commit()
            row = connection.execute("SELECT status,review_seal_hash FROM attempts WHERE attempt_id=?",
                                     (self.fixture.attempt_id,)).fetchone()
            self.assertEqual(("in_progress", self.bundle_hash), tuple(row))
        finally:
            connection.close()
        again = self.request_grant()
        self.assertEqual(200, again.status_code, again.text)
        self.assertEqual(grant, again.json())
        self.assert_problem(self.request_grant("f" * 64), 409, "review_seal_conflict")

    def test_pending_review_requires_student_session_original_device_and_active_owner(self):
        self.close_assessment()
        self.assert_problem(self.request_grant(bearer=False), 401, "invalid_client_session")
        self.assert_problem(self.request_grant(student_id="OTHER"), 404, "review_not_found")
        private, public = generate_ed25519_keypair()
        other_device = str(uuid.uuid4())
        with app.db() as connection:
            connection.execute("INSERT INTO devices VALUES (?, 'other', ?, 'active', ?, NULL)",
                               (other_device, public, app.now()))
        self.assert_problem(self.request_grant(device_id=other_device, private_key=private), 404, "review_not_found")
        with app.db() as connection:
            connection.execute("UPDATE devices SET status='revoked' WHERE device_id=?", (self.fixture.device_id,))
        self.assert_problem(self.request_grant(), 403, "device_inactive")
        with app.db() as connection:
            connection.execute("UPDATE devices SET status='active' WHERE device_id=?", (self.fixture.device_id,))
        app.delete_student(self.fixture.student_id)
        self.assert_problem(self.request_grant(), 404, "review_not_found")

    def test_pending_review_rejects_malformed_hash_and_unknown_fields(self):
        self.close_assessment()
        for value in ("a" * 63, "A" * 64, "g" * 64, 123, None):
            with self.subTest(value=value):
                response = self.request_grant(value if value is not None else "")
                self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(422, self.request_grant(extra={"review_released": True}).status_code)

    def test_review_rejects_missing_keys_and_voided_attempt_without_commitment(self):
        self.close_assessment()
        with app.db() as connection:
            connection.execute("UPDATE assessment_releases SET wrapped_review_key_b64=NULL WHERE release_id=?",
                               (self.fixture.release_id,))
        self.assert_problem(self.request_grant(), 409, "review_unavailable")
        with app.db() as connection:
            connection.execute("UPDATE attempts SET status='voided' WHERE attempt_id=?", (self.fixture.attempt_id,))
        self.assert_problem(self.request_grant(), 404, "review_not_found")

    def test_review_commitment_rejects_changed_upload_but_original_still_scores(self):
        self.close_assessment()
        response = self.request_grant()
        self.assertEqual(200, response.status_code, response.text)
        changed = self.fixture.bundle({self.fixture.q1: "A", self.fixture.q2: "C"})
        self.assert_problem(self.fixture.submit(changed), 409, "review_seal_conflict")
        self.assertEqual(0, self.fixture.count("submissions"))
        accepted = self.fixture.submit(self.signed)
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual(1, accepted.json()["score"])
        self.assertEqual(1, self.fixture.count("submissions"))

    def test_upload_winning_race_only_allows_its_accepted_hash(self):
        accepted = self.fixture.submit(self.signed)
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.close_assessment()
        self.assert_problem(self.request_grant("f" * 64), 409, "review_seal_conflict")
        grant = self.request_grant()
        self.assertEqual(200, grant.status_code, grant.text)
        self.assertEqual(self.bundle_hash, grant.json()["bundle_hash"])
        duplicate = self.fixture.submit(self.signed)
        self.assertEqual(accepted.json(), duplicate.json())

    def test_prevalidated_queued_conflicting_upload_cannot_commit_after_review_grant(self):
        self.close_assessment()
        changed = self.fixture.bundle({self.fixture.q1: "A", self.fixture.q2: "C"})
        entered, release = threading.Event(), threading.Event()
        writer = self.fixture.config.submission_writer
        original_commit = writer._commit

        def held_commit(connection, scored):
            entered.set()
            if not release.wait(5):
                raise AssertionError("The test did not release the queued submission.")
            return original_commit(connection, scored)

        with patch.object(writer, "_commit", side_effect=held_commit), ThreadPoolExecutor(max_workers=1) as executor:
            uploading = executor.submit(self.fixture.submit, changed)
            try:
                self.assertTrue(entered.wait(5))
                grant = self.request_grant()
                self.assertEqual(200, grant.status_code, grant.text)
            finally:
                release.set()
            rejected = uploading.result(timeout=5)
        self.assert_problem(rejected, 409, "review_seal_conflict")
        self.assertEqual(0, self.fixture.count("submissions"))
        accepted = self.fixture.submit(self.signed)
        self.assertEqual(200, accepted.status_code, accepted.text)


if __name__ == "__main__":
    unittest.main()
