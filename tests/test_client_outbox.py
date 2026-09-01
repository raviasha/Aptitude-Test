import base64
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ksat.client.store import ClientStore
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    AttemptTicket, ResponseBundle, ResponseEntry, SignedAttemptTicket,
    SignedResponseBundle, SubmissionReceipt,
)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def utcnow(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeCoordinator:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.lock = threading.Lock()

    def submit_bundle(self, bundle):
        with self.lock:
            self.calls += 1
            result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ClientOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "client.db"
        self.store = ClientStore(self.database)
        self.now = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=30)
        self.attempt_id = str(uuid.uuid4())
        self.release_id = str(uuid.uuid4())
        device_private, _ = generate_ed25519_keypair()
        coordinator_private, _ = generate_ed25519_keypair()
        ticket = AttemptTicket(
            attempt_id=self.attempt_id, student_id="S1", device_id=str(uuid.uuid4()),
            release_id=self.release_id, content_hash="a" * 64, started_at=self.now,
            deadline=self.deadline, order_seed_b64=base64.b64encode(b"o" * 32).decode(),
            content_key_b64=base64.b64encode(b"k" * 32).decode(),
        )
        self.ticket = SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(coordinator_private, ticket))
        self.bundle_body = ResponseBundle(
            ticket=self.ticket, content_hash="a" * 64, sealed_at=self.deadline,
            responses=[ResponseEntry(question_id=1, selected_answer=None)],
        )
        self.bundle = SignedResponseBundle(
            bundle=self.bundle_body, device_signature_b64=sign_json(device_private, self.bundle_body)
        )
        self.receipt = SubmissionReceipt(
            attempt_id=self.attempt_id, accepted_at=self.deadline + timedelta(seconds=1),
            score=0, total_questions=1, attempted=0, percentage=0.0, violations=0,
        )
        pack = Path(self.temp.name) / "pack"
        pack.write_bytes(b"pack")
        self.store.cache_pack(self.release_id, "a" * 64, pack, verified=True)
        self.store.create_attempt(self.ticket, [1], created_at=self.now, last_wall_time=self.now)
        self.store.seal_attempt(self.attempt_id, self.bundle, sealed_at=self.deadline)
        self.clock = FakeClock(self.deadline)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_worker(self, coordinator, random_source=lambda: 0.5, store=None):
        from ksat.client.outbox import OutboxWorker

        return OutboxWorker(store or self.store, coordinator, self.clock, random_source=random_source)

    def test_offline_retry_survives_store_restart_and_acknowledges(self):
        coordinator = FakeCoordinator([ConnectionError("offline"), self.receipt])
        worker = self.make_worker(coordinator)
        self.assertEqual(1, worker.process_due_once())
        pending = self.store.pending_submissions()[0]
        self.assertEqual(1, pending.retry_count)
        self.assertIn("offline", pending.last_error)
        next_attempt = pending.next_attempt_at

        self.store.close()
        self.store = ClientStore(self.database)
        self.assertEqual(next_attempt, self.store.pending_submissions()[0].next_attempt_at)
        self.clock.advance(1)
        self.assertEqual(1, self.make_worker(coordinator).process_due_once())
        self.assertEqual([], self.store.pending_submissions())
        self.assertEqual("acknowledged", self.store.load_attempt(self.attempt_id).state)

    def test_builtin_timeout_is_a_durable_retryable_transport_failure(self):
        coordinator = FakeCoordinator([TimeoutError("timed out")])
        self.assertEqual(1, self.make_worker(coordinator).process_due_once())
        pending = self.store.pending_submissions()[0]
        self.assertEqual(1, pending.retry_count)
        self.assertIn("timed out", pending.last_error)

    def test_backoff_sequence_cap_and_jitter_bounds_are_persisted(self):
        coordinator = FakeCoordinator([ConnectionError("offline")] * 7)
        worker = self.make_worker(coordinator, random_source=lambda: 0.0)
        low_delays = []
        for _ in range(6):
            worker.process_due_once()
            pending = self.store.pending_submissions()[0]
            low_delays.append((pending.next_attempt_at - self.clock.now).total_seconds())
            self.clock.now = pending.next_attempt_at
        self.assertEqual([0.8, 1.6, 3.2, 6.4, 12.8, 24.0], low_delays)

        # Invalid entropy fails closed without mutating durable retry state.
        count = self.store.pending_submissions()[0].retry_count
        with self.assertRaises(ValueError):
            self.make_worker(coordinator, random_source=lambda: 1.1).process_due_once()
        self.assertEqual(count, self.store.pending_submissions()[0].retry_count)

    def test_retry_after_is_respected_and_never_schedules_in_past(self):
        from ksat.client.coordinator import CoordinatorProblem

        coordinator = FakeCoordinator([
            CoordinatorProblem("submission_busy", "Busy.", True, 20.0, status_code=503)
        ])
        self.make_worker(coordinator).process_due_once()
        pending = self.store.pending_submissions()[0]
        self.assertEqual(self.clock.now + timedelta(seconds=20), pending.next_attempt_at)

    def test_nonretryable_problem_preserves_exact_bundle_for_intervention_without_hot_loop(self):
        from ksat.client.coordinator import CoordinatorProblem

        coordinator = FakeCoordinator([
            CoordinatorProblem("invalid_bundle_signature", "Signature invalid.", False)
        ])
        worker = self.make_worker(coordinator)
        self.assertEqual(1, worker.process_due_once())
        pending = self.store.pending_submissions()[0]
        self.assertEqual(self.bundle, pending.bundle)
        self.assertEqual("faculty_intervention_required", pending.status)
        self.assertIn("invalid_bundle_signature", pending.last_error)
        self.assertEqual(0, worker.process_due_once())
        self.assertEqual(1, coordinator.calls)
        self.store.close()
        self.store = ClientStore(self.database)
        reopened = self.store.pending_submissions()[0]
        self.assertEqual("faculty_intervention_required", reopened.status)
        self.assertEqual(self.bundle, reopened.bundle)

    def test_semantically_invalid_receipt_requires_intervention_instead_of_retry(self):
        invalid = self.receipt.model_copy(update={"total_questions": 2})
        coordinator = FakeCoordinator([invalid])
        self.make_worker(coordinator).process_due_once()
        pending = self.store.pending_submissions()[0]
        self.assertEqual("faculty_intervention_required", pending.status)
        self.assertEqual(0, pending.retry_count)
        self.assertIn("invalid_submission_receipt", pending.last_error)

    def test_corrupt_intervention_state_without_error_is_rejected(self):
        self.store.connection.execute(
            """UPDATE submission_outbox
               SET status='faculty_intervention_required', last_error=NULL
               WHERE attempt_id=?""",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "outbox data"):
            self.store.pending_submissions()

    def test_acknowledgment_failure_preserves_outbox_for_duplicate_receipt_retry(self):
        coordinator = FakeCoordinator([self.receipt, self.receipt])
        worker = self.make_worker(coordinator)
        original = self.store.acknowledge
        self.store.acknowledge = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        worker.process_due_once()
        self.assertEqual("sealed_pending", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual(1, len(self.store.pending_submissions()))
        self.store.acknowledge = original
        self.clock.now = self.store.pending_submissions()[0].next_attempt_at
        worker.process_due_once()
        self.assertEqual("acknowledged", self.store.load_attempt(self.attempt_id).state)

    def test_sqlite_acknowledgment_failure_is_persisted_for_receipt_replay(self):
        coordinator = FakeCoordinator([self.receipt])
        worker = self.make_worker(coordinator)
        self.store.acknowledge = lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is busy")
        )
        self.assertEqual(1, worker.process_due_once())
        pending = self.store.pending_submissions()[0]
        self.assertEqual(1, pending.retry_count)
        self.assertIn("database is busy", pending.last_error)

    def test_two_concurrent_process_calls_cannot_send_same_attempt_twice(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCoordinator:
            calls = 0
            def submit_bundle(inner, bundle):
                inner.calls += 1
                entered.set()
                release.wait(2)
                return self.receipt

        coordinator = BlockingCoordinator()
        worker = self.make_worker(coordinator)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(worker.process_due_once)
            entered.wait(1)
            second = executor.submit(worker.process_due_once)
            self.assertEqual(0, second.result(timeout=1))
            release.set()
            self.assertEqual(1, first.result(timeout=1))
        self.assertEqual(1, coordinator.calls)

    def test_background_lifecycle_is_idempotent_and_wake_drains_due_work(self):
        coordinator = FakeCoordinator([self.receipt])
        worker = self.make_worker(coordinator)
        worker.start()
        worker.start()
        worker.wake()
        deadline = time.monotonic() + 2
        while self.store.pending_submissions() and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.stop()
        worker.stop()
        self.assertEqual([], self.store.pending_submissions())
        self.assertEqual(1, coordinator.calls)

    def test_bounded_stop_during_inflight_send_returns_and_keeps_outbox(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingCoordinator:
            def submit_bundle(inner, bundle):
                entered.set()
                release.wait(2)
                raise ConnectionError("offline")

        worker = self.make_worker(BlockingCoordinator())
        worker.start()
        entered.wait(1)
        before = time.monotonic()
        worker.stop(timeout_seconds=0.02)
        self.assertLess(time.monotonic() - before, 0.3)
        self.assertEqual(self.bundle, self.store.pending_submissions()[0].bundle)
        release.set()
        worker.stop(timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
