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

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

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

    def test_positive_jitter_never_exceeds_client_backoff_cap(self):
        coordinator = FakeCoordinator([ConnectionError("offline")] * 8)
        worker = self.make_worker(coordinator, random_source=lambda: 1.0)
        delays = []
        for _ in range(7):
            worker.process_due_once()
            pending = self.store.pending_submissions()[0]
            delays.append((pending.next_attempt_at - self.clock.now).total_seconds())
            self.clock.now = pending.next_attempt_at
        self.assertEqual([1.2, 2.4, 4.8, 9.6, 19.2, 30.0, 30.0], delays)

    def test_retry_after_is_applied_after_capped_client_delay(self):
        from ksat.client.coordinator import CoordinatorProblem

        coordinator = FakeCoordinator([
            CoordinatorProblem("submission_busy", "Busy.", True, 45.0, status_code=503)
        ])
        # Put the client-computed delay at the 30-second cap before the request.
        for _ in range(5):
            self.store.record_retry(
                self.attempt_id,
                next_attempt_at=self.clock.now,
                last_error="offline",
            )
        self.make_worker(coordinator, random_source=lambda: 1.0).process_due_once()
        pending = self.store.pending_submissions()[0]
        self.assertEqual(self.clock.now + timedelta(seconds=45), pending.next_attempt_at)

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

    def test_wake_between_empty_query_and_condition_wait_is_not_lost(self):
        coordinator = FakeCoordinator([self.receipt])
        worker = self.make_worker(coordinator)
        real_pending = self.store.pending_submissions
        empty_query_reached = threading.Event()
        release_empty_query = threading.Event()
        first_due_query = True
        first_schedule_query = True

        def pause_after_empty_query(*args, **kwargs):
            nonlocal first_due_query, first_schedule_query
            if kwargs.get("due_at") is not None and first_due_query:
                first_due_query = False
                return []
            if kwargs.get("due_at") is None and first_schedule_query:
                first_schedule_query = False
                empty_query_reached.set()
                self.assertTrue(release_empty_query.wait(2))
                return []
            return real_pending(*args, **kwargs)

        self.store.pending_submissions = pause_after_empty_query
        worker.start()
        try:
            self.assertTrue(empty_query_reached.wait(1))
            worker.wake()
            release_empty_query.set()
            self.wait_until(
                lambda: self.store.load_attempt(self.attempt_id).state == "acknowledged"
            )
        finally:
            release_empty_query.set()
            worker.stop()
        self.assertEqual(1, coordinator.calls)

    def test_multiple_wakes_coalesce_without_busy_loop(self):
        self.store.acknowledge(self.attempt_id, self.receipt)
        calls = 0
        waiting = threading.Event()
        real_pending = self.store.pending_submissions

        def counted_pending(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = real_pending(*args, **kwargs)
            if calls >= 2:
                waiting.set()
            return result

        self.store.pending_submissions = counted_pending
        worker = self.make_worker(FakeCoordinator([]))
        worker.start()
        try:
            self.assertTrue(waiting.wait(1))
            with worker._condition:
                for _ in range(20):
                    worker.wake()
            self.wait_until(lambda: calls >= 4)
            time.sleep(0.08)
            self.assertEqual(4, calls)
        finally:
            worker.stop()

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

    def test_daemon_recovers_from_one_shot_sqlite_outbox_read_failure(self):
        coordinator = FakeCoordinator([self.receipt])
        original = self.store.pending_submissions
        failed = threading.Event()
        calls = 0

        def flaky_pending(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                failed.set()
                raise sqlite3.OperationalError("database temporarily busy")
            return original(*args, **kwargs)

        self.store.pending_submissions = flaky_pending
        worker = self.make_worker(coordinator)
        worker.start()
        self.assertTrue(failed.wait(1))
        time.sleep(0.08)
        self.assertLessEqual(calls, 2)
        worker.wake()
        self.wait_until(lambda: self.store.load_attempt(self.attempt_id).state == "acknowledged")
        self.assertEqual(1, coordinator.calls)
        worker.stop()

    def test_daemon_recovers_when_retry_persistence_fails_once_without_fabricating_state(self):
        coordinator = FakeCoordinator([ConnectionError("offline"), self.receipt])
        original = self.store.record_retry
        failed = threading.Event()

        def flaky_retry(*args, **kwargs):
            if not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("retry persistence busy")
            return original(*args, **kwargs)

        self.store.record_retry = flaky_retry
        worker = self.make_worker(coordinator)
        worker.start()
        self.assertTrue(failed.wait(1))
        pending = self.store.pending_submissions()[0]
        self.assertEqual(0, pending.retry_count)
        self.assertIsNone(pending.last_error)
        worker.wake()
        self.wait_until(lambda: self.store.load_attempt(self.attempt_id).state == "acknowledged")
        self.assertEqual(2, coordinator.calls)
        worker.stop()

    def test_daemon_recovers_when_intervention_persistence_fails_once(self):
        from ksat.client.coordinator import CoordinatorProblem

        problem = CoordinatorProblem("invalid_bundle_signature", "Invalid.", False)
        coordinator = FakeCoordinator([problem, problem])
        original = self.store.require_faculty_intervention
        failed = threading.Event()

        def flaky_intervention(*args, **kwargs):
            if not failed.is_set():
                failed.set()
                raise OSError("intervention persistence busy")
            return original(*args, **kwargs)

        self.store.require_faculty_intervention = flaky_intervention
        worker = self.make_worker(coordinator)
        worker.start()
        self.assertTrue(failed.wait(1))
        self.assertEqual("pending", self.store.pending_submissions()[0].status)
        worker.wake()
        self.wait_until(
            lambda: self.store.pending_submissions()[0].status
            == "faculty_intervention_required"
        )
        self.assertEqual(2, coordinator.calls)
        worker.stop()

    def test_daemon_recovers_when_ack_and_retry_persistence_each_fail_once(self):
        coordinator = FakeCoordinator([self.receipt, self.receipt])
        real_acknowledge = self.store.acknowledge
        real_retry = self.store.record_retry
        ack_failed = threading.Event()
        retry_failed = threading.Event()

        def flaky_acknowledge(*args, **kwargs):
            if not ack_failed.is_set():
                ack_failed.set()
                raise sqlite3.OperationalError("ack persistence busy")
            return real_acknowledge(*args, **kwargs)

        def flaky_retry(*args, **kwargs):
            if not retry_failed.is_set():
                retry_failed.set()
                raise sqlite3.OperationalError("retry persistence busy")
            return real_retry(*args, **kwargs)

        self.store.acknowledge = flaky_acknowledge
        self.store.record_retry = flaky_retry
        worker = self.make_worker(coordinator)
        worker.start()
        self.assertTrue(ack_failed.wait(1))
        self.assertTrue(retry_failed.wait(1))
        self.assertEqual("sealed_pending", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual(0, self.store.pending_submissions()[0].retry_count)
        worker.wake()
        self.wait_until(lambda: self.store.load_attempt(self.attempt_id).state == "acknowledged")
        self.assertEqual(2, coordinator.calls)
        worker.stop()

    def test_stop_interrupts_local_store_recovery_wait_without_hot_loop(self):
        calls = 0
        failed = threading.Event()

        def unavailable_store(*args, **kwargs):
            nonlocal calls
            calls += 1
            failed.set()
            raise OSError("store unavailable")

        self.store.pending_submissions = unavailable_store
        worker = self.make_worker(FakeCoordinator([]))
        worker.start()
        self.assertTrue(failed.wait(1))
        time.sleep(0.08)
        self.assertIsNotNone(worker._thread)
        self.assertTrue(worker._thread.is_alive())
        before = time.monotonic()
        worker.stop(timeout_seconds=0.5)
        self.assertLess(time.monotonic() - before, 0.2)
        self.assertLessEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
