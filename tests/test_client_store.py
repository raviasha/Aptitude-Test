import base64
import json
import math
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ksat.client.store import AttemptSealedError, ClientStore
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    AttemptTicket,
    IntegrityEvent,
    ResponseBundle,
    ResponseEntry,
    SignedAttemptTicket,
    SignedResponseBundle,
    SubmissionReceipt,
    canonical_json,
)


class ClientStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "client.db"
        self.pack_path = Path(self.temporary_directory.name) / "pack.ksat"
        self.pack_path.write_bytes(b"encrypted")
        self.store = ClientStore(self.database_path)
        self.now = datetime(2026, 8, 31, 9, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=30)
        self.release_id = str(uuid.uuid4())
        self.attempt_id = str(uuid.uuid4())
        self.content_hash = "a" * 64
        self.device_private, _ = generate_ed25519_keypair()
        coordinator_private, _ = generate_ed25519_keypair()
        ticket = AttemptTicket(
            attempt_id=self.attempt_id,
            student_id="S1",
            device_id=str(uuid.uuid4()),
            release_id=self.release_id,
            content_hash=self.content_hash,
            started_at=self.now,
            deadline=self.deadline,
            order_seed_b64=base64.b64encode(b"o" * 32).decode("ascii"),
            content_key_b64=base64.b64encode(b"k" * 32).decode("ascii"),
        )
        self.ticket = SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(coordinator_private, ticket))
        self.receipt = SubmissionReceipt(
            attempt_id=self.attempt_id,
            accepted_at=self.deadline + timedelta(seconds=2),
            score=1,
            total_questions=3,
            attempted=1,
            percentage=33.3,
            violations=1,
        )

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def _cache_and_create(self):
        self.store.cache_pack(self.release_id, self.content_hash, self.pack_path, verified=True)
        return self.store.create_attempt(self.ticket, [3, 1, 2])

    def _bundle(self, *, answer="B", sealed_at=None):
        sealed_at = sealed_at or self.deadline
        bundle = ResponseBundle(
            ticket=self.ticket,
            content_hash=self.content_hash,
            sealed_at=sealed_at,
            responses=[
                ResponseEntry(question_id=3, selected_answer=answer),
                ResponseEntry(question_id=1, selected_answer=None),
                ResponseEntry(question_id=2, selected_answer=None),
            ],
            integrity_events=[IntegrityEvent(event_type="focus_lost", occurred_at=self.now)],
        )
        return SignedResponseBundle(bundle=bundle, device_signature_b64=sign_json(self.device_private, bundle))

    def test_connection_configuration_and_answer_seal_acknowledgment_are_transactional(self):
        self.assertEqual("wal", self.store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
        self.assertEqual(10_000, self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0])
        self.assertEqual(1, self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.assertEqual(self.store.load_attempt(self.attempt_id).responses[3], "B")
        sealed = self.store.seal_attempt(self.attempt_id, self._bundle(), sealed_at=self.deadline)
        self.assertEqual(sealed.state, "sealed_pending")
        for operation in (
            lambda: self.store.save_answer(self.attempt_id, 3, "A", saved_at=self.deadline),
            lambda: self.store.update_timer_checkpoint(
                self.attempt_id, 0, last_wall_time=self.deadline
            ),
            lambda: self.store.record_integrity_event(
                self.attempt_id, "late", occurred_at=self.deadline
            ),
        ):
            with self.assertRaises(AttemptSealedError):
                operation()
        self.assertEqual(len(self.store.pending_submissions()), 1)
        self.store.acknowledge(self.attempt_id, self.receipt)
        acknowledged = self.store.load_attempt(self.attempt_id)
        self.assertEqual("acknowledged", acknowledged.state)
        self.assertEqual(self.receipt, acknowledged.receipt)
        self.assertEqual([], self.store.pending_submissions())

    def test_close_and_reopen_preserves_every_lifecycle_stage(self):
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.update_timer_checkpoint(
            self.attempt_id, 1499, last_wall_time=self.now + timedelta(minutes=5)
        )
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self._reopen()
        active = self.store.load_attempt(self.attempt_id)
        self.assertEqual({3: "B", 1: None, 2: None}, active.responses)
        self.assertEqual(1499, active.remaining_seconds)
        self.assertEqual(self.now + timedelta(minutes=5), active.last_wall_time)
        self.assertEqual((IntegrityEvent(event_type="focus_lost", occurred_at=self.now),), self.store.integrity_events(self.attempt_id))

        self.store.seal_attempt(self.attempt_id, self._bundle(), sealed_at=self.deadline)
        self.store.record_retry(
            self.attempt_id,
            next_attempt_at=self.deadline + timedelta(seconds=10),
            last_error="coordinator unavailable",
        )
        self._reopen()
        pending = self.store.pending_submissions()[0]
        self.assertEqual(self._bundle(), pending.bundle)
        self.assertEqual(1, pending.retry_count)
        self.assertEqual("coordinator unavailable", pending.last_error)

        self.store.acknowledge(self.attempt_id, self.receipt)
        self._reopen()
        self.assertEqual("acknowledged", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual(self.receipt, self.store.load_attempt(self.attempt_id).receipt)
        self.assertEqual([], self.store.pending_submissions())

    def test_timestamps_persist_in_utc_and_due_filter_compares_instants(self):
        offset = timezone(timedelta(hours=5, minutes=30))
        offset_now = self.now.astimezone(offset)
        offset_deadline = self.deadline.astimezone(offset)
        self.store.cache_pack(
            self.release_id,
            self.content_hash,
            self.pack_path,
            verified=True,
            cached_at=offset_now,
        )
        self.store.create_attempt(
            self.ticket,
            [3, 1, 2],
            created_at=offset_now,
            last_wall_time=offset_now,
        )
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=offset_now)
        self.store.record_integrity_event(
            self.attempt_id, "focus_lost", occurred_at=offset_now
        )
        self.store.seal_attempt(
            self.attempt_id, self._bundle(), sealed_at=offset_deadline
        )
        retry_utc = self.deadline + timedelta(seconds=10)
        self.store.record_retry(
            self.attempt_id,
            next_attempt_at=retry_utc.astimezone(offset),
            last_error="busy",
        )

        self.assertEqual(
            [], self.store.pending_submissions(due_at=retry_utc - timedelta(microseconds=1))
        )
        self.assertEqual(
            [self.attempt_id],
            [item.attempt_id for item in self.store.pending_submissions(due_at=retry_utc)],
        )
        scalar_times = [
            self.store.connection.execute(
                "SELECT cached_at FROM cached_content_packs WHERE release_id=?",
                (self.release_id,),
            ).fetchone()[0],
            *self.store.connection.execute(
                """SELECT deadline, last_wall_time, created_at, sealed_at
                   FROM local_attempts WHERE attempt_id=?""",
                (self.attempt_id,),
            ).fetchone(),
            self.store.connection.execute(
                "SELECT saved_at FROM local_responses WHERE attempt_id=? AND question_id=3",
                (self.attempt_id,),
            ).fetchone()[0],
            self.store.connection.execute(
                "SELECT occurred_at FROM local_integrity_events WHERE attempt_id=?",
                (self.attempt_id,),
            ).fetchone()[0],
            *self.store.connection.execute(
                """SELECT next_attempt_at, created_at FROM submission_outbox
                   WHERE attempt_id=?""",
                (self.attempt_id,),
            ).fetchone(),
        ]
        self.assertTrue(all(value.endswith("+00:00") for value in scalar_times), scalar_times)

    def test_cache_requires_exact_record_and_never_downgrades_verification(self):
        self.store.cache_pack(self.release_id, self.content_hash, self.pack_path, verified=True)
        self.store.cache_pack(self.release_id, self.content_hash, self.pack_path, verified=False)
        self.assertEqual(
            self.pack_path,
            self.store.verified_pack(self.release_id, self.content_hash, self.pack_path),
        )
        self.assertIsNone(self.store.verified_pack(self.release_id, "b" * 64, self.pack_path))
        self.assertIsNone(self.store.verified_pack(self.release_id, self.content_hash, Path("wrong")))
        with self.assertRaisesRegex(ValueError, "Cached content pack conflicts"):
            self.store.cache_pack(self.release_id, "b" * 64, self.pack_path, verified=True)

        self.store.connection.execute(
            "UPDATE cached_content_packs SET content_hash='corrupt' WHERE release_id=?",
            (self.release_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored content pack record is invalid\.$"):
            self.store.verified_pack(self.release_id)

    def test_create_attempt_is_idempotent_only_for_same_ticket_and_order(self):
        first = self._cache_and_create()
        self.assertEqual(first, self.store.create_attempt(self.ticket, [3, 1, 2]))
        with self.assertRaisesRegex(ValueError, "Attempt identifier conflicts"):
            self.store.create_attempt(self.ticket, [1, 3, 2])
        altered = self.ticket.model_copy(
            update={"signature_b64": base64.b64encode(b"x" * 64).decode("ascii")}
        )
        with self.assertRaisesRegex(ValueError, "Attempt identifier conflicts"):
            self.store.create_attempt(altered, [3, 1, 2])

    def test_create_attempt_idempotency_includes_all_resolved_creation_inputs(self):
        self.store.cache_pack(self.release_id, self.content_hash, self.pack_path, verified=True)
        created_at = self.now + timedelta(seconds=1)
        last_wall_time = self.now + timedelta(seconds=2)
        first = self.store.create_attempt(
            self.ticket,
            [3, 1, 2],
            remaining_seconds=1700,
            created_at=created_at,
            last_wall_time=last_wall_time,
        )
        offset = timezone(timedelta(hours=5, minutes=30))
        equivalent = self.store.create_attempt(
            self.ticket,
            [3, 1, 2],
            remaining_seconds=1700,
            created_at=created_at.astimezone(offset),
            last_wall_time=last_wall_time.astimezone(offset),
        )
        self.assertEqual(first, equivalent)

        conflicts = (
            {"remaining_seconds": 1699, "created_at": created_at, "last_wall_time": last_wall_time},
            {
                "remaining_seconds": 1700,
                "created_at": created_at + timedelta(seconds=1),
                "last_wall_time": last_wall_time,
            },
            {
                "remaining_seconds": 1700,
                "created_at": created_at,
                "last_wall_time": last_wall_time + timedelta(seconds=1),
            },
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                with self.assertRaisesRegex(ValueError, "Attempt identifier conflicts"):
                    self.store.create_attempt(self.ticket, [3, 1, 2], **conflict)
                unchanged = self.store.load_attempt(self.attempt_id)
                self.assertEqual(1700, unchanged.remaining_seconds)
                self.assertEqual(last_wall_time, unchanged.last_wall_time)

    def test_active_attempt_refuses_ambiguous_persisted_state(self):
        self._cache_and_create()
        second_ticket = self.ticket.model_copy(
            update={"ticket": self.ticket.ticket.model_copy(update={"attempt_id": str(uuid.uuid4())})}
        )
        with self.store.connection:
            self.store.connection.execute("DROP INDEX one_active_student_release")
            self.store.connection.execute(
                """INSERT INTO local_attempts
                   (attempt_id, student_id, release_id, ticket_json, question_order_json,
                    state, deadline, remaining_seconds, last_wall_time, created_at)
                   SELECT ?, student_id, release_id, ?, question_order_json, state, deadline,
                          remaining_seconds, last_wall_time, created_at
                   FROM local_attempts WHERE attempt_id=?""",
                (
                    second_ticket.ticket.attempt_id,
                    canonical_json(second_ticket).decode("utf-8"),
                    self.attempt_id,
                ),
            )
        with self.assertRaisesRegex(ValueError, "Multiple active attempts"):
            self.store.active_attempt(student_id="S1", release_id=self.release_id)

    def test_seal_and_acknowledge_are_exactly_idempotent(self):
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        bundle = self._bundle()
        first = self.store.seal_attempt(self.attempt_id, bundle, sealed_at=self.deadline)
        self.assertEqual(first, self.store.seal_attempt(self.attempt_id, bundle, sealed_at=self.deadline))
        with self.assertRaisesRegex(ValueError, "Sealed attempt conflicts"):
            self.store.seal_attempt(self.attempt_id, self._bundle(answer="A"), sealed_at=self.deadline)
        self.store.acknowledge(self.attempt_id, self.receipt)
        self.store.acknowledge(self.attempt_id, self.receipt)
        conflicting = self.receipt.model_copy(update={"score": 0, "percentage": 0.0})
        with self.assertRaisesRegex(ValueError, "Acknowledgment conflicts"):
            self.store.acknowledge(self.attempt_id, conflicting)

    def test_seal_rejects_missing_bundle_without_transition(self):
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        with self.assertRaises((TypeError, ValueError)):
            self.store.seal_attempt(self.attempt_id, None, sealed_at=self.deadline)
        self.assertEqual("in_progress", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual([], self.store.pending_submissions())

    def test_seal_rejects_zero_signature_without_transition(self):
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        invalid = self._bundle(answer=None).model_copy(
            update={"device_signature_b64": base64.b64encode(bytes(64)).decode("ascii")}
        )
        with self.assertRaisesRegex(ValueError, "Device signature"):
            self.store.seal_attempt(self.attempt_id, invalid, sealed_at=self.deadline)
        self.assertEqual("in_progress", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual([], self.store.pending_submissions())

    def test_seal_rejects_malformed_signature_and_wrong_attempt_without_transition(self):
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        malformed = self._bundle(answer=None).model_copy(update={"device_signature_b64": "%%%"})
        with self.assertRaisesRegex(ValueError, "Device signature"):
            self.store.seal_attempt(self.attempt_id, malformed, sealed_at=self.deadline)
        wrong_attempt = self._bundle(answer=None).model_copy(
            update={
                "bundle": self._bundle(answer=None).bundle.model_copy(
                    update={
                        "ticket": self.ticket.model_copy(
                            update={
                                "ticket": self.ticket.ticket.model_copy(
                                    update={"attempt_id": str(uuid.uuid4())}
                                )
                            }
                        )
                    }
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.seal_attempt(self.attempt_id, wrong_attempt, sealed_at=self.deadline)
        self.assertEqual("in_progress", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual([], self.store.pending_submissions())

    def test_invalid_seal_or_receipt_time_rolls_back_lifecycle_transition(self):
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        too_early = self.now - timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "Seal time"):
            self.store.seal_attempt(
                self.attempt_id,
                self._bundle(answer=None, sealed_at=too_early),
                sealed_at=too_early,
            )
        self.assertEqual("in_progress", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual([], self.store.pending_submissions())

        self.store.seal_attempt(self.attempt_id, self._bundle(answer=None), sealed_at=self.deadline)
        early_receipt = self.receipt.model_copy(
            update={
                "accepted_at": self.deadline - timedelta(seconds=1),
                "score": 0,
                "attempted": 0,
                "percentage": 0.0,
            }
        )
        with self.assertRaisesRegex(ValueError, "acceptance time"):
            self.store.acknowledge(self.attempt_id, early_receipt)
        self.assertEqual("sealed_pending", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual(1, len(self.store.pending_submissions()))

    def test_acknowledge_rejects_score_above_attempted_without_mutation(self):
        self._seal_pending()
        impossible = self.receipt.model_copy(
            update={"score": 2, "attempted": 1, "total_questions": 3, "percentage": 66.7}
        )
        self._assert_receipt_rejected(impossible)

    def test_acknowledge_rejects_wrong_total_and_percentage_without_mutation(self):
        self._seal_pending()
        wrong_total = self.receipt.model_copy(
            update={"score": 1, "attempted": 1, "total_questions": 2, "percentage": 50.0}
        )
        self._assert_receipt_rejected(wrong_total)

    def test_acknowledge_rejects_inconsistent_percentage_without_mutation(self):
        self._seal_pending()
        inconsistent = self.receipt.model_copy(
            update={"score": 1, "attempted": 1, "total_questions": 3, "percentage": 33.4}
        )
        self._assert_receipt_rejected(inconsistent)

    def test_acknowledge_rejects_response_and_violation_count_mismatch(self):
        self._seal_pending()
        impossible = self.receipt.model_copy(
            update={
                "score": 0,
                "attempted": 0,
                "total_questions": 3,
                "percentage": 0.0,
                "violations": 0,
            }
        )
        self._assert_receipt_rejected(impossible)

    def test_concurrent_answer_retry_and_acknowledgment_operations_are_safe(self):
        self._cache_and_create()
        barrier = threading.Barrier(8)

        def save(index):
            local = ClientStore(self.database_path)
            try:
                barrier.wait()
                local.save_answer(self.attempt_id, 3, "ABCD"[index % 4], saved_at=self.now)
            finally:
                local.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(save, range(8)))
        self.assertIn(self.store.load_attempt(self.attempt_id).responses[3], "ABCD")
        final_answer = self.store.load_attempt(self.attempt_id).responses[3]
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.seal_attempt(self.attempt_id, self._bundle(answer=final_answer), sealed_at=self.deadline)

        def retry(_):
            local = ClientStore(self.database_path)
            try:
                return local.record_retry(
                    self.attempt_id,
                    next_attempt_at=self.deadline + timedelta(seconds=30),
                    last_error="busy",
                )
            finally:
                local.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(retry, range(8)))
        self.assertEqual(8, self.store.pending_submissions()[0].retry_count)

        def acknowledge(_):
            local = ClientStore(self.database_path)
            try:
                return local.acknowledge(self.attempt_id, self.receipt)
            finally:
                local.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(acknowledge, range(8)))
        self.assertEqual("acknowledged", self.store.load_attempt(self.attempt_id).state)

    def test_load_attempt_reads_one_snapshot_during_concurrent_acknowledgment(self):
        self._seal_pending()
        attempt_selected = threading.Event()
        allow_reader = threading.Event()

        class PrefetchedCursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class PausingConnection(sqlite3.Connection):
            pause_enabled = False

            def execute(self, sql, parameters=()):
                cursor = super().execute(sql, parameters)
                if self.pause_enabled and "SELECT * FROM local_attempts" in sql:
                    self.pause_enabled = False
                    row = cursor.fetchone()
                    attempt_selected.set()
                    if not allow_reader.wait(timeout=10):
                        raise RuntimeError("Timed out waiting to resume snapshot reader.")
                    return PrefetchedCursor(row)
                return cursor

        def connection_factory(path):
            connection = sqlite3.connect(
                path,
                timeout=10.0,
                check_same_thread=False,
                factory=PausingConnection,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            return connection

        reader = ClientStore(self.database_path, connection_factory=connection_factory)
        reader.connection.pause_enabled = True
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(reader.load_attempt, self.attempt_id)
                self.assertTrue(attempt_selected.wait(timeout=10))
                try:
                    self.store.acknowledge(self.attempt_id, self.receipt)
                finally:
                    allow_reader.set()
                snapshot = future.result(timeout=10)
            self.assertEqual("sealed_pending", snapshot.state)
            self.assertEqual("acknowledged", self.store.load_attempt(self.attempt_id).state)
        finally:
            allow_reader.set()
            reader.close()

    def test_commit_failure_rolls_back_answer_and_seal_outbox(self):
        self._cache_and_create()
        self._use_failing_commit_connection()
        self.store.connection.fail_next_commit = True
        with self.assertRaisesRegex(RuntimeError, "Injected commit failure"):
            self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.assertIsNone(self.store.load_attempt(self.attempt_id).responses[3])
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.connection.fail_next_commit = True
        with self.assertRaisesRegex(RuntimeError, "Injected commit failure"):
            self.store.seal_attempt(self.attempt_id, self._bundle(answer=None), sealed_at=self.deadline)
        self.assertEqual("in_progress", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual([], self.store.pending_submissions())

    def test_timer_checkpoint_cannot_increase_persisted_remaining_time(self):
        self._cache_and_create()
        self.store.update_timer_checkpoint(self.attempt_id, 1200, last_wall_time=self.now)
        with self.assertRaisesRegex(ValueError, "cannot increase"):
            self.store.update_timer_checkpoint(
                self.attempt_id, 1201, last_wall_time=self.now + timedelta(seconds=1)
            )
        self.assertEqual(1200, self.store.load_attempt(self.attempt_id).remaining_seconds)

    def test_corrupt_sealed_bundle_is_rejected_when_loading_attempt(self):
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.seal_attempt(self.attempt_id, self._bundle(answer=None), sealed_at=self.deadline)
        self.store.connection.execute(
            "UPDATE local_attempts SET sealed_bundle_json='{bad' WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.load_attempt(self.attempt_id)

    def test_pending_submissions_rejects_nested_attempt_and_sealed_bundle_mismatch(self):
        self._seal_pending()
        wrong_attempt = self._bundle(answer=None).model_copy(
            update={
                "bundle": self._bundle(answer=None).bundle.model_copy(
                    update={
                        "ticket": self.ticket.model_copy(
                            update={
                                "ticket": self.ticket.ticket.model_copy(
                                    update={"attempt_id": str(uuid.uuid4())}
                                )
                            }
                        )
                    }
                )
            }
        )
        self.store.connection.execute(
            "UPDATE submission_outbox SET bundle_json=? WHERE attempt_id=?",
            (canonical_json(wrong_attempt).decode("utf-8"), self.attempt_id),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_pending_submissions_rejects_joint_bundle_tampering_against_saved_responses(self):
        self._seal_pending()
        altered = canonical_json(self._bundle(answer="A")).decode("utf-8")
        self.store.connection.execute(
            "UPDATE local_attempts SET sealed_bundle_json=? WHERE attempt_id=?",
            (altered, self.attempt_id),
        )
        self.store.connection.execute(
            "UPDATE submission_outbox SET bundle_json=? WHERE attempt_id=?",
            (altered, self.attempt_id),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

        self.store.connection.execute(
            "UPDATE submission_outbox SET bundle_json=? WHERE attempt_id=?",
            (canonical_json(self._bundle(answer="B")).decode("utf-8"), self.attempt_id),
        )
        mismatched_sealed = self._bundle(answer="A")
        self.store.connection.execute(
            "UPDATE local_attempts SET sealed_bundle_json=? WHERE attempt_id=?",
            (canonical_json(mismatched_sealed).decode("utf-8"), self.attempt_id),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_pending_submissions_rejects_wrong_state_and_missing_attempt(self):
        self._seal_pending()
        self.store.connection.execute(
            "UPDATE local_attempts SET state='acknowledged' WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

        self.store.connection.execute(
            "UPDATE local_attempts SET state='sealed_pending' WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        self.store.connection.execute("PRAGMA foreign_keys=OFF")
        self.store.connection.execute(
            "DELETE FROM local_attempts WHERE attempt_id=?", (self.attempt_id,)
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_pending_submissions_rejects_malformed_outbox_json(self):
        self._seal_pending()
        self.store.connection.execute(
            "UPDATE submission_outbox SET bundle_json='{bad' WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_load_rejects_partial_or_mismatched_sealed_lifecycle_state(self):
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.seal_attempt(self.attempt_id, self._bundle(answer="B"), sealed_at=self.deadline)
        self.store.connection.execute(
            """UPDATE local_responses SET selected_answer='A', saved_at=?
               WHERE attempt_id=? AND question_id=3""",
            (self.now.isoformat(), self.attempt_id),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.load_attempt(self.attempt_id)

    def test_load_rejects_persisted_receipt_semantic_corruption(self):
        self._seal_pending()
        self.store.acknowledge(self.attempt_id, self.receipt)
        corrupted = self.receipt.model_copy(
            update={
                "score": 0,
                "attempted": 0,
                "percentage": 0.0,
                "violations": 0,
            }
        )
        self.store.connection.execute(
            "UPDATE local_attempts SET receipt_json=? WHERE attempt_id=?",
            (canonical_json(corrupted).decode("utf-8"), self.attempt_id),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.load_attempt(self.attempt_id)
        self.store.connection.execute(
            "DELETE FROM local_responses WHERE attempt_id=?", (self.attempt_id,)
        )
        self.store.connection.execute(
            "DELETE FROM submission_outbox WHERE attempt_id=?", (self.attempt_id,)
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.load_attempt(self.attempt_id)

    def test_failed_schema_initialization_closes_connection(self):
        holder = {}

        class MigrationFailureConnection(sqlite3.Connection):
            closed = False

            def executescript(self, _script):
                raise sqlite3.DatabaseError("injected migration failure")

            def close(self):
                self.closed = True
                return super().close()

        def connection_factory(path):
            connection = sqlite3.connect(path, factory=MigrationFailureConnection)
            holder["connection"] = connection
            return connection

        with self.assertRaisesRegex(sqlite3.DatabaseError, "injected migration failure"):
            ClientStore(
                Path(self.temporary_directory.name) / "broken.db",
                connection_factory=connection_factory,
            )
        self.assertTrue(holder["connection"].closed)

    def test_corrupt_and_noncanonical_persisted_json_is_rejected_not_replaced(self):
        self._cache_and_create()
        cases = [
            "{bad",
            canonical_json(self.ticket).decode("utf-8").replace("{", "{\"ticket\":null,", 1),
            json.dumps(self.ticket.model_dump(mode="json"), indent=2),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid[:20]):
                self.store.connection.execute(
                    "UPDATE local_attempts SET ticket_json=? WHERE attempt_id=?",
                    (invalid, self.attempt_id),
                )
                self.store.connection.commit()
                with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
                    self.store.load_attempt(self.attempt_id)
                self.store.connection.execute(
                    "UPDATE local_attempts SET ticket_json=? WHERE attempt_id=?",
                    (canonical_json(self.ticket).decode("utf-8"), self.attempt_id),
                )
                self.store.connection.commit()

    def test_naive_times_and_non_finite_receipts_are_rejected(self):
        naive_ticket = self.ticket.model_copy(
            update={"ticket": self.ticket.ticket.model_copy(update={"deadline": self.deadline.replace(tzinfo=None)})}
        )
        self.store.cache_pack(self.release_id, self.content_hash, self.pack_path, verified=True)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.create_attempt(naive_ticket, [3, 1, 2])
        self._cache_and_create()
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.seal_attempt(self.attempt_id, self._bundle(answer=None), sealed_at=self.deadline)
        invalid_receipt = self.receipt.model_copy(update={"percentage": math.inf})
        with self.assertRaisesRegex(ValueError, "finite"):
            self.store.acknowledge(self.attempt_id, invalid_receipt)

    def test_unknown_questions_and_missing_attempts_are_rejected(self):
        self._cache_and_create()
        with self.assertRaisesRegex(ValueError, "Question is not part"):
            self.store.save_answer(self.attempt_id, 99, "A", saved_at=self.now)
        with self.assertRaisesRegex(KeyError, "Unknown local attempt"):
            self.store.load_attempt("missing")

    def _reopen(self):
        self.store.close()
        self.store = ClientStore(self.database_path)

    def _seal_pending(self):
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.record_integrity_event(self.attempt_id, "focus_lost", occurred_at=self.now)
        self.store.seal_attempt(
            self.attempt_id, self._bundle(answer="B"), sealed_at=self.deadline
        )

    def _assert_receipt_rejected(self, receipt):
        with self.assertRaisesRegex(ValueError, "receipt|Receipt|Acknowledgment"):
            self.store.acknowledge(self.attempt_id, receipt)
        self.assertEqual("sealed_pending", self.store.load_attempt(self.attempt_id).state)
        self.assertEqual(1, len(self.store.pending_submissions()))

    def _use_failing_commit_connection(self):
        class FailingCommitConnection(sqlite3.Connection):
            fail_next_commit = False

            def commit(self):
                if self.fail_next_commit:
                    self.fail_next_commit = False
                    raise RuntimeError("Injected commit failure")
                return super().commit()

        def connection_factory(path):
            connection = sqlite3.connect(
                path,
                timeout=10.0,
                check_same_thread=False,
                factory=FailingCommitConnection,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            return connection

        self.store.close()
        self.store = ClientStore(self.database_path, connection_factory=connection_factory)


if __name__ == "__main__":
    unittest.main()
