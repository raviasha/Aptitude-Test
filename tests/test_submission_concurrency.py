import base64
import sqlite3
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app
from ksat.coordinator.submissions import (
    ScoredSubmission,
    SubmissionProblem,
    SubmissionWriter,
    validate_and_score,
)
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    AttemptTicket,
    ResponseBundle,
    ResponseEntry,
    SignedAttemptTicket,
    SignedResponseBundle,
    SubmissionReceipt,
    canonical_json,
    device_request_bytes,
)
from ksat.sqlite import connect_sqlite
from tests import test_distributed_submission as submission_tests


class SubmissionWriterReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = submission_tests.DistributedSubmissionTests("test_server_scores_bundle_and_duplicate_returns_same_receipt")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def scored(self) -> ScoredSubmission:
        connection = connect_sqlite(app.DB_PATH)
        try:
            result = validate_and_score(
                connection,
                self.fixture.bundle(),
                coordinator_public_key_b64=self.fixture.config.signing_public_key_b64,
                received_at=self.fixture.deadline,
            )
        finally:
            connection.close()
        self.assertIsInstance(result, ScoredSubmission)
        return result

    def test_full_queue_is_retryable_and_never_blocks_caller(self):
        scored = self.scored()
        writer = SubmissionWriter(app.DB_PATH, max_pending=1)
        entered = threading.Event()
        release = threading.Event()
        original_commit = writer._commit

        def blocked_commit(connection, item):
            entered.set()
            self.assertTrue(release.wait(5), "test-controlled writer release timed out")
            return original_commit(connection, item)

        with patch.object(writer, "_commit", side_effect=blocked_commit):
            writer.start()
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(writer.submit, scored, 5)
                self.assertTrue(entered.wait(5))
                second = executor.submit(writer.submit, scored, 5)
                deadline = time.monotonic() + 5
                while writer.pending_count != 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                with self.assertRaises(SubmissionProblem) as caught:
                    writer.submit(scored, 1)
                self.assertEqual("submission_busy", caught.exception.code)
                self.assertTrue(caught.exception.retryable)
                release.set()
                self.assertEqual(first.result(5), second.result(5))
            writer.stop(5)

    def test_caller_timeout_may_commit_and_retry_recovers_same_receipt(self):
        scored = self.scored()
        writer = SubmissionWriter(app.DB_PATH)
        entered = threading.Event()
        release = threading.Event()
        original_commit = writer._commit

        def delayed_commit(connection, item):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_commit(connection, item)

        with patch.object(writer, "_commit", side_effect=delayed_commit):
            writer.start()
            with self.assertRaises(SubmissionProblem) as caught:
                writer.submit(scored, timeout_seconds=0.01)
            self.assertEqual("submission_timeout", caught.exception.code)
            self.assertTrue(caught.exception.retryable)
            self.assertTrue(entered.wait(5))
            release.set()
            deadline = time.monotonic() + 5
            while self.fixture.count("submissions") != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            writer.stop(5)
        retry = self.fixture.submit(self.fixture.bundle())
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual(scored.attempt_id, retry.json()["attempt_id"])
        self.assertEqual(1, self.fixture.count("submissions"))

    def test_worker_database_exception_completes_future_and_writer_continues(self):
        scored = self.scored()
        writer = SubmissionWriter(app.DB_PATH)
        original_commit = writer._commit
        calls = 0

        def fail_once(connection, item):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("injected worker failure")
            return original_commit(connection, item)

        with patch.object(writer, "_commit", side_effect=fail_once):
            writer.start()
            with self.assertRaises(SubmissionProblem) as caught:
                writer.submit(scored, 5)
            self.assertEqual("submission_failed", caught.exception.code)
            receipt = writer.submit(scored, 5)
            self.assertIsInstance(receipt, SubmissionReceipt)
            writer.stop(5)

    def test_worker_connection_failure_resolves_queued_future(self):
        scored = self.scored()
        writer = SubmissionWriter(app.DB_PATH)
        with patch(
            "ksat.coordinator.submissions.connect_sqlite",
            side_effect=sqlite3.OperationalError("injected open failure"),
        ):
            writer.start()
            with self.assertRaises(SubmissionProblem) as caught:
                writer.submit(scored, timeout_seconds=1)
        self.assertEqual("submission_failed", caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        writer.stop(1)

    def test_transaction_failure_rolls_back_every_submission_side_effect(self):
        event_bundle = self.fixture.bundle(events=(
            submission_tests.IntegrityEvent(
                event_type="focus_lost",
                occurred_at=self.fixture.started_at + timedelta(seconds=1),
            ),
        ))
        with app.db() as connection:
            connection.execute(
                """CREATE TRIGGER fail_violation BEFORE INSERT ON exam_violations
                   BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END"""
            )
        response = self.fixture.submit(event_bundle)
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual("submission_failed", response.json()["detail"]["code"])
        self.assertEqual("1", response.headers["Retry-After"])
        with app.db() as connection:
            attempt = connection.execute(
                "SELECT status, score, attempted, submission_hash FROM attempts WHERE attempt_id=?",
                (self.fixture.attempt_id,),
            ).fetchone()
            self.assertEqual(("in_progress", 0, 0, None), tuple(attempt))
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM exam_violations").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])

    def test_lifecycle_is_idempotent_and_rejects_when_not_running(self):
        writer = SubmissionWriter(app.DB_PATH)
        scored = self.scored()
        with self.assertRaises(SubmissionProblem) as before:
            writer.submit(scored)
        self.assertEqual("submission_writer_not_running", before.exception.code)
        writer.start()
        writer.start()
        writer.stop(5)
        writer.stop(5)
        with self.assertRaises(SubmissionProblem) as after:
            writer.submit(scored)
        self.assertEqual("submission_writer_not_running", after.exception.code)

    def test_concurrent_duplicate_requests_return_one_immutable_receipt(self):
        signed = self.fixture.bundle()
        gate = threading.Event()

        def submit_once():
            self.assertTrue(gate.wait(5))
            return self.fixture.submit(signed)

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(submit_once) for _ in range(16)]
            gate.set()
            responses = [future.result(timeout=15) for future in futures]
        self.assertEqual({200}, {response.status_code for response in responses})
        self.assertEqual(1, len({canonical_json(response.json()) for response in responses}))
        self.assertEqual(1, self.fixture.count("submissions"))
        self.assertEqual(2, self.fixture.count("responses"))

    def test_stop_timeout_resolves_the_inflight_caller_without_hanging(self):
        scored = self.scored()
        writer = SubmissionWriter(app.DB_PATH)
        entered = threading.Event()
        release = threading.Event()
        original_commit = writer._commit

        def blocked_commit(connection, item):
            entered.set()
            release.wait(5)
            return original_commit(connection, item)

        with patch.object(writer, "_commit", side_effect=blocked_commit):
            writer.start()
            with ThreadPoolExecutor(max_workers=1) as executor:
                caller = executor.submit(writer.submit, scored, 10)
                try:
                    self.assertTrue(entered.wait(5))
                    before = time.monotonic()
                    writer.stop(timeout_seconds=0.01)
                    self.assertLess(time.monotonic() - before, 1)
                    with self.assertRaises(SubmissionProblem) as caught:
                        caller.result(timeout=1)
                    self.assertEqual("submission_writer_stopping", caught.exception.code)
                finally:
                    release.set()
            writer.stop(5)


class SubmissionBurstTests(unittest.TestCase):
    CLIENTS = 100

    def setUp(self):
        self.fixture = submission_tests.DistributedSubmissionTests("test_server_scores_bundle_and_duplicate_returns_same_receipt")
        self.fixture.setUp()
        self.requests = []
        with app.db() as connection:
            test_id = connection.execute(
                "SELECT test_id FROM assessment_releases WHERE release_id=?",
                (self.fixture.release_id,),
            ).fetchone()[0]
            for index in range(self.CLIENTS):
                student_id = f"B{index:03d}"
                device_id = str(uuid.uuid4())
                attempt_id = str(uuid.uuid4())
                private_b64, public_b64 = generate_ed25519_keypair()
                connection.execute(
                    "INSERT INTO students VALUES (?, ?, 'unused', 'AIML', 'A', ?)",
                    (student_id, f"Burst {index}", app.now()),
                )
                connection.execute(
                    "INSERT INTO devices VALUES (?, ?, ?, 'active', ?, NULL)",
                    (device_id, f"burst-{index}", public_b64, app.now()),
                )
                ticket = AttemptTicket(
                    attempt_id=attempt_id,
                    student_id=student_id,
                    device_id=device_id,
                    release_id=self.fixture.release_id,
                    content_hash=self.fixture.content_hash,
                    started_at=self.fixture.started_at,
                    deadline=self.fixture.deadline,
                    order_seed_b64=base64.b64encode(index.to_bytes(4, "big") + b"o" * 28).decode(),
                    content_key_b64=base64.b64encode(b"k" * 32).decode(),
                )
                signed_ticket = SignedAttemptTicket(
                    ticket=ticket,
                    signature_b64=sign_json(self.fixture.config.signing_private_key_b64, ticket),
                )
                connection.execute(
                    """INSERT INTO attempts
                       (attempt_id, student_id, test_id, release_id, device_id, order_seed,
                        ticket_json, started_at, status, total_questions, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', 2, ?)""",
                    (
                        attempt_id, student_id, test_id, self.fixture.release_id, device_id,
                        ticket.order_seed_b64, canonical_json(signed_ticket).decode(),
                        self.fixture.started_at.isoformat(timespec="seconds"),
                        self.fixture.deadline.isoformat(timespec="seconds"),
                    ),
                )
                bundle = ResponseBundle(
                    ticket=signed_ticket,
                    content_hash=self.fixture.content_hash,
                    sealed_at=self.fixture.deadline,
                    responses=[
                        ResponseEntry(question_id=self.fixture.q1, selected_answer="B"),
                        ResponseEntry(question_id=self.fixture.q2, selected_answer="C"),
                    ],
                )
                signed = SignedResponseBundle(
                    bundle=bundle,
                    device_signature_b64=sign_json(private_b64, bundle),
                )
                self.requests.append((signed, device_id, private_b64))

    def tearDown(self):
        self.fixture.tearDown()

    def post(self, request_item):
        signed, device_id, private_b64 = request_item
        path = "/api/client/v1/submissions"
        body = canonical_json(signed)
        timestamp = datetime_now()
        nonce = str(uuid.uuid4())
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
        signature = private.sign(device_request_bytes("POST", path, body, timestamp, nonce))
        return self.fixture.client.post(
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-KSAT-Device": device_id,
                "X-KSAT-Timestamp": timestamp,
                "X-KSAT-Nonce": nonce,
                "X-KSAT-Signature": base64.b64encode(signature).decode(),
            },
        )

    def run_burst(self):
        gate = threading.Event()

        def gated_post(item):
            if not gate.wait(10):
                raise AssertionError("non-blocking start gate was not released")
            return self.post(item)

        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(gated_post, item) for item in self.requests]
            gate.set()
            return [future.result(timeout=45) for future in futures]

    def test_one_hundred_clients_and_retries_are_exactly_once_without_lock_errors(self):
        first = self.run_burst()
        errors = [response.text for response in first if response.status_code != 200]
        self.assertEqual([], errors)
        receipts = [response.json() for response in first]
        self.assertEqual(self.CLIENTS, len(receipts))
        self.assertEqual(self.CLIENTS, len({item["attempt_id"] for item in receipts}))
        with app.db() as connection:
            self.assertEqual(self.CLIENTS, connection.execute(
                "SELECT COUNT(*) FROM submissions WHERE attempt_id != ?", (self.fixture.attempt_id,)
            ).fetchone()[0])
            self.assertEqual(self.CLIENTS, connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE status='submitted'"
            ).fetchone()[0])
        self.assertNotIn("database is locked", " ".join(errors).lower())

        retried = self.run_burst()
        retry_errors = [response.text for response in retried if response.status_code != 200]
        self.assertEqual([], retry_errors)
        self.assertEqual(receipts, [response.json() for response in retried])
        with app.db() as connection:
            self.assertEqual(self.CLIENTS, connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
            self.assertEqual(self.CLIENTS * 2, connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0])


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    unittest.main()
