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
from unittest.mock import patch

import ksat.client.store as client_store_module
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

    def _other_ticket(self, *, release_id, attempt_id, student_id):
        return self.ticket.model_copy(
            update={
                "ticket": self.ticket.ticket.model_copy(
                    update={
                        "release_id": release_id,
                        "attempt_id": attempt_id,
                        "student_id": student_id,
                        "content_hash": "b" * 64,
                    }
                )
            }
        )

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
        self.assertEqual(3, active.current_question_id)
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

    def test_current_question_position_defaults_persists_and_rejects_corruption(self):
        created = self._cache_and_create()
        self.assertEqual(3, created.current_question_id)
        self.store.save_position(self.attempt_id, 1)
        self._reopen()
        self.assertEqual(1, self.store.load_attempt(self.attempt_id).current_question_id)
        with self.assertRaises(ValueError):
            self.store.save_position(self.attempt_id, 999)
        self.store.connection.execute(
            "UPDATE local_attempts SET current_question_id=999 WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaisesRegex(ValueError, "Stored attempt data is invalid"):
            self.store.load_attempt(self.attempt_id)

    def test_position_change_and_seal_are_serialized_without_post_seal_mutation(self):
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.record_integrity_event(
            self.attempt_id, "focus_lost", occurred_at=self.now
        )
        barrier = threading.Barrier(2)
        outcomes = []

        def move():
            barrier.wait()
            try:
                self.store.save_position(self.attempt_id, 1)
            except AttemptSealedError:
                outcomes.append("sealed")
            else:
                outcomes.append("moved")

        def seal():
            barrier.wait()
            self.store.seal_attempt(
                self.attempt_id, self._bundle(), sealed_at=self.deadline
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(move), pool.submit(seal))
            for future in futures:
                future.result(timeout=5)
        record = self.store.load_attempt(self.attempt_id)
        self.assertEqual("sealed_pending", record.state)
        self.assertIn(record.current_question_id, record.question_order)
        self.assertIn(outcomes, (["moved"], ["sealed"]))
        with self.assertRaises(AttemptSealedError):
            self.store.save_position(self.attempt_id, 2)

    def test_schema_migrates_existing_attempt_table_with_position_column(self):
        self.store.close()
        self.database_path.unlink()
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """CREATE TABLE local_attempts (
               attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL,
               release_id TEXT NOT NULL, ticket_json TEXT NOT NULL,
               question_order_json TEXT NOT NULL, state TEXT NOT NULL,
               deadline TEXT NOT NULL, remaining_seconds INTEGER NOT NULL,
               last_wall_time TEXT NOT NULL, created_at TEXT NOT NULL,
               sealed_at TEXT, sealed_bundle_json TEXT, receipt_json TEXT
            )"""
        )
        connection.commit()
        connection.close()
        self.store = ClientStore(self.database_path)
        columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(local_attempts)")
        }
        self.assertIn("current_question_id", columns)

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

    def test_start_or_resume_attempt_returns_the_stored_winner_samples(self):
        self.store.cache_pack(
            self.release_id, self.content_hash, self.pack_path, verified=True
        )
        winner_created = self.now + timedelta(seconds=0.1)
        winner_wall = self.now + timedelta(seconds=0.25)
        winner = self.store.start_or_resume_attempt(
            self.ticket,
            [3, 1, 2],
            remaining_seconds=1799,
            created_at=winner_created,
            last_wall_time=winner_wall,
        )
        second = ClientStore(self.database_path)
        try:
            replayed = second.start_or_resume_attempt(
                self.ticket,
                [3, 1, 2],
                remaining_seconds=1798,
                created_at=winner_created + timedelta(seconds=1),
                last_wall_time=winner_wall + timedelta(seconds=1),
            )
        finally:
            second.close()
        self.assertEqual(winner, replayed)
        self.assertEqual(1799, replayed.remaining_seconds)
        self.assertEqual(winner_wall, replayed.last_wall_time)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM local_attempts"
        ).fetchone()[0]
        self.assertEqual(1, count)

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
        with self.assertRaisesRegex(ValueError, "Multiple active attempts"):
            self.store.start_or_resume_attempt(
                self.ticket,
                [3, 1, 2],
                remaining_seconds=1800,
                created_at=self.now,
                last_wall_time=self.now,
            )

    def test_create_attempt_rejects_any_other_active_client_attempt(self):
        self._cache_and_create()
        other_release = str(uuid.uuid4())
        other_ticket = self._other_ticket(
            release_id=other_release,
            attempt_id=str(uuid.uuid4()),
            student_id="S2",
        )
        self.store.cache_pack(
            other_release, "b" * 64, self.pack_path, verified=True
        )
        with self.assertRaisesRegex(
            ValueError, "Another local assessment attempt is already active"
        ):
            self.store.create_attempt(other_ticket, [3, 1, 2])
        self.assertEqual(self.attempt_id, self.store.active_attempt().attempt_id)

    def test_two_store_connections_cannot_race_two_active_attempts(self):
        second = ClientStore(self.database_path)
        try:
            other_release = str(uuid.uuid4())
            other_ticket = self._other_ticket(
                release_id=other_release,
                attempt_id=str(uuid.uuid4()),
                student_id="S2",
            )
            self.store.cache_pack(
                self.release_id, self.content_hash, self.pack_path, verified=True
            )
            second.cache_pack(
                other_release, "b" * 64, self.pack_path, verified=True
            )
            barrier = threading.Barrier(2)

            def create(store, ticket):
                barrier.wait()
                return store.create_attempt(ticket, [3, 1, 2])

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = (
                    pool.submit(create, self.store, self.ticket),
                    pool.submit(create, second, other_ticket),
                )
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except ValueError as error:
                        outcomes.append(error)
            self.assertEqual(1, sum(not isinstance(item, Exception) for item in outcomes))
            self.assertEqual(1, sum(isinstance(item, ValueError) for item in outcomes))
            error = next(item for item in outcomes if isinstance(item, ValueError))
            self.assertEqual(
                "Another local assessment attempt is already active.", str(error)
            )
            self.assertIsNotNone(self.store.active_attempt())
        finally:
            second.close()

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

    def test_pending_submissions_rejects_jointly_tampered_content_hash(self):
        self._seal_pending()
        altered = self._resigned_bundle(content_hash="b" * 64)
        self._tamper_sealed_snapshot(altered)

        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_pending_submissions_rejects_jointly_tampered_seal_chronology(self):
        self._seal_pending()
        altered = self._resigned_bundle(sealed_at=self.now - timedelta(seconds=1))
        self._tamper_sealed_snapshot(altered)

        with self.assertRaisesRegex(ValueError, r"^Stored submission outbox data is invalid\.$"):
            self.store.pending_submissions()

    def test_acknowledge_rejects_jointly_tampered_content_hash_atomically(self):
        self._seal_pending()
        altered = self._resigned_bundle(content_hash="b" * 64)
        self._tamper_sealed_snapshot(altered)

        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.acknowledge(self.attempt_id, self.receipt)
        self._assert_raw_pending_state()

    def test_acknowledge_rejects_jointly_tampered_seal_chronology_atomically(self):
        self._seal_pending()
        altered = self._resigned_bundle(sealed_at=self.now - timedelta(seconds=1))
        self._tamper_sealed_snapshot(altered)

        with self.assertRaisesRegex(ValueError, r"^Stored attempt data is invalid\.$"):
            self.store.acknowledge(self.attempt_id, self.receipt)
        self._assert_raw_pending_state()

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

    def _resigned_bundle(self, **updates):
        bundle = self._bundle(answer="B").bundle.model_copy(update=updates)
        return SignedResponseBundle(
            bundle=bundle,
            device_signature_b64=sign_json(self.device_private, bundle),
        )

    def _tamper_sealed_snapshot(self, bundle):
        bundle_json = canonical_json(bundle).decode("utf-8")
        self.store.connection.execute(
            """UPDATE local_attempts
               SET sealed_at=?, sealed_bundle_json=? WHERE attempt_id=?""",
            (bundle.bundle.sealed_at.isoformat(), bundle_json, self.attempt_id),
        )
        self.store.connection.execute(
            "UPDATE submission_outbox SET bundle_json=? WHERE attempt_id=?",
            (bundle_json, self.attempt_id),
        )
        self.store.connection.commit()

    def _assert_raw_pending_state(self):
        row = self.store.connection.execute(
            "SELECT state FROM local_attempts WHERE attempt_id=?", (self.attempt_id,)
        ).fetchone()
        outbox = self.store.connection.execute(
            "SELECT COUNT(*) FROM submission_outbox WHERE attempt_id=?", (self.attempt_id,)
        ).fetchone()[0]
        self.assertEqual("sealed_pending", row["state"])
        self.assertEqual(1, outbox)

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

    def _authenticated_store(self) -> tuple[bytes, Path]:
        self.store.close()
        self.database_path.unlink(missing_ok=True)
        integrity_key = b"state-integrity-test-key-32byte!"[:32]
        anchor_path = Path(self.temporary_directory.name) / "identity" / "state-anchor.json"
        anchor_path.parent.mkdir()
        self.store = ClientStore(
            self.database_path,
            integrity_key=integrity_key,
            integrity_anchor_path=anchor_path,
        )
        return integrity_key, anchor_path

    def _prepare_legacy_state(self, state: str) -> dict[str, list[tuple]]:
        self.store.close()
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            path.unlink(missing_ok=True)
        self.store = ClientStore(self.database_path)
        self.store.cache_pack(
            self.release_id,
            self.content_hash,
            self.pack_path,
            verified=True,
            cached_at=self.now,
        )
        if state != "cached":
            self.store.create_attempt(
                self.ticket,
                [3, 1, 2],
                created_at=self.now,
                last_wall_time=self.now,
            )
            self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
            self.store.record_integrity_event(
                self.attempt_id, "focus_lost", occurred_at=self.now
            )
        if state in {"sealed_pending", "acknowledged"}:
            self.store.seal_attempt(
                self.attempt_id, self._bundle(), sealed_at=self.deadline
            )
        if state == "acknowledged":
            self.store.acknowledge(self.attempt_id, self.receipt)
        self.store.close()
        return self._authenticated_table_snapshot()

    def _authenticated_table_snapshot(self) -> dict[str, list[tuple]]:
        connection = sqlite3.connect(self.database_path)
        try:
            tables = (
                "cached_content_packs",
                "local_attempts",
                "local_responses",
                "local_integrity_events",
                "submission_outbox",
            )
            return {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY rowid'
                ).fetchall()
                for table in tables
            }
        finally:
            connection.close()

    def _legacy_metadata_snapshot(self) -> tuple[int, tuple[str, ...], int]:
        connection = sqlite3.connect(self.database_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            )
            journal_count = connection.execute(
                "SELECT COUNT(*) FROM authenticated_state_journal"
            ).fetchone()[0]
            return version, tables, journal_count
        finally:
            connection.close()

    def test_protected_store_blocks_nonempty_legacy_state_without_mutation(self):
        expected_rows = self._prepare_legacy_state("in_progress")
        expected_metadata = self._legacy_metadata_snapshot()
        anchor_path = Path(self.temporary_directory.name) / "identity" / "state-anchor.json"

        with self.assertRaisesRegex(ValueError, "explicit administrator migration") as raised:
            ClientStore(
                self.database_path,
                integrity_key=b"state-integrity-test-key-32byte!"[:32],
                integrity_anchor_path=anchor_path,
            )

        self.assertIsInstance(
            raised.exception, client_store_module.ClientStateMigrationRequired
        )
        self.assertEqual(expected_rows, self._authenticated_table_snapshot())
        self.assertEqual(expected_metadata, self._legacy_metadata_snapshot())
        self.assertFalse(anchor_path.exists())

    def test_confirmed_legacy_migration_preserves_all_lifecycle_states(self):
        expected_counts = {
            "cached": {
                "cached_packs": 1,
                "in_progress_attempts": 0,
                "sealed_pending_attempts": 0,
                "acknowledged_attempts": 0,
                "pending_outbox": 0,
            },
            "in_progress": {
                "cached_packs": 1,
                "in_progress_attempts": 1,
                "sealed_pending_attempts": 0,
                "acknowledged_attempts": 0,
                "pending_outbox": 0,
            },
            "sealed_pending": {
                "cached_packs": 1,
                "in_progress_attempts": 0,
                "sealed_pending_attempts": 1,
                "acknowledged_attempts": 0,
                "pending_outbox": 1,
            },
            "acknowledged": {
                "cached_packs": 1,
                "in_progress_attempts": 0,
                "sealed_pending_attempts": 0,
                "acknowledged_attempts": 1,
                "pending_outbox": 0,
            },
        }
        integrity_key = b"state-integrity-test-key-32byte!"[:32]
        anchor_path = Path(self.temporary_directory.name) / "identity" / "state-anchor.json"
        for state, counts in expected_counts.items():
            with self.subTest(state=state):
                anchor_path.unlink(missing_ok=True)
                expected_rows = self._prepare_legacy_state(state)
                self.store = ClientStore(
                    self.database_path,
                    integrity_key=integrity_key,
                    integrity_anchor_path=anchor_path,
                    allow_legacy_state_migration=True,
                )
                self.assertEqual(counts, self.store.migration_summary())
                self.assertEqual(
                    2,
                    self.store.connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.store.close()
                self.assertEqual(expected_rows, self._authenticated_table_snapshot())
                self.store = ClientStore(
                    self.database_path,
                    integrity_key=integrity_key,
                    integrity_anchor_path=anchor_path,
                )
                if state == "cached":
                    self.assertEqual(
                        self.pack_path.resolve(),
                        self.store.verified_pack(self.release_id, self.content_hash),
                    )
                elif state == "sealed_pending":
                    self.assertEqual(1, len(self.store.pending_submissions()))
                else:
                    self.assertEqual(state, self.store.load_attempt(self.attempt_id).state)

    def test_confirmed_legacy_migration_rejects_invalid_state_without_mutation(self):
        integrity_key = b"state-integrity-test-key-32byte!"[:32]
        anchor_path = Path(self.temporary_directory.name) / "identity" / "state-anchor.json"
        corruptions = {
            "cached timestamp": (
                "cached",
                "UPDATE cached_content_packs SET cached_at='not-a-time'",
            ),
            "outbox bundle": (
                "sealed_pending",
                "UPDATE submission_outbox SET bundle_json='{}'",
            ),
        }
        for label, (state, statement) in corruptions.items():
            with self.subTest(case=label):
                anchor_path.unlink(missing_ok=True)
                self._prepare_legacy_state(state)
                connection = sqlite3.connect(self.database_path)
                connection.execute(statement)
                connection.commit()
                connection.close()
                expected_rows = self._authenticated_table_snapshot()
                expected_metadata = self._legacy_metadata_snapshot()

                with self.assertRaisesRegex(ValueError, "Legacy client state is invalid"):
                    ClientStore(
                        self.database_path,
                        integrity_key=integrity_key,
                        integrity_anchor_path=anchor_path,
                        allow_legacy_state_migration=True,
                    )

                self.assertEqual(expected_rows, self._authenticated_table_snapshot())
                self.assertEqual(expected_metadata, self._legacy_metadata_snapshot())
                self.assertFalse(anchor_path.exists())

    def test_confirmed_legacy_migration_recovers_anchor_failure_only_when_reconfirmed(self):
        expected_rows = self._prepare_legacy_state("sealed_pending")
        integrity_key = b"state-integrity-test-key-32byte!"[:32]
        anchor_path = Path(self.temporary_directory.name) / "identity" / "state-anchor.json"

        with patch.object(
            ClientStore,
            "_write_anchor",
            side_effect=OSError("injected migration anchor failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected migration"):
                ClientStore(
                    self.database_path,
                    integrity_key=integrity_key,
                    integrity_anchor_path=anchor_path,
                    allow_legacy_state_migration=True,
                )
        self.assertFalse(anchor_path.exists())
        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

        self.store = ClientStore(
            self.database_path,
            integrity_key=integrity_key,
            integrity_anchor_path=anchor_path,
            allow_legacy_state_migration=True,
        )
        self.assertEqual(expected_rows, self._authenticated_table_snapshot())
        self.assertEqual(1, len(self.store.pending_submissions()))
        self.assertTrue(anchor_path.is_file())

    def test_client_schema_versions_plain_and_authenticated_stores(self):
        self.assertEqual(
            1, self.store.connection.execute("PRAGMA user_version").fetchone()[0]
        )
        integrity_key, _anchor_path = self._authenticated_store()
        self.assertEqual(2, self.store.connection.execute("PRAGMA user_version").fetchone()[0])
        self.store.connection.execute("PRAGMA user_version = 0")
        self.store.close()
        self.store = ClientStore(
            self.database_path,
            integrity_key=integrity_key,
            integrity_anchor_path=_anchor_path,
        )
        self.assertEqual(2, self.store.connection.execute("PRAGMA user_version").fetchone()[0])

    def test_authenticated_state_rejects_coherent_unseal_and_outbox_deletion(self):
        integrity_key, anchor_path = self._authenticated_store()
        self._seal_pending()
        self.store.close()

        connection = sqlite3.connect(self.database_path)
        connection.execute("DELETE FROM submission_outbox WHERE attempt_id=?", (self.attempt_id,))
        connection.execute(
            """UPDATE local_attempts
               SET state='in_progress', sealed_at=NULL, sealed_bundle_json=NULL
               WHERE attempt_id=?""",
            (self.attempt_id,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_rejects_deleted_integrity_event(self):
        integrity_key, anchor_path = self._authenticated_store()
        self._cache_and_create()
        self.store.record_integrity_event(
            self.attempt_id, "focus_lost", occurred_at=self.now
        )
        self.store.close()

        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "DELETE FROM local_integrity_events WHERE attempt_id=?", (self.attempt_id,)
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_anchor_detects_journal_tail_rollback(self):
        integrity_key, anchor_path = self._authenticated_store()
        self._cache_and_create()
        self.store.save_answer(self.attempt_id, 3, "B", saved_at=self.now)
        self.store.close()

        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "DELETE FROM authenticated_state_journal WHERE sequence=(SELECT MAX(sequence) FROM authenticated_state_journal)"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_recovers_exactly_one_committed_entry_after_anchor_failure(self):
        integrity_key, anchor_path = self._authenticated_store()

        with patch.object(
            self.store,
            "_write_anchor",
            side_effect=OSError("injected post-commit anchor failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected post-commit"):
                self.store.cache_pack(
                    self.release_id,
                    self.content_hash,
                    self.pack_path,
                    verified=True,
                    cached_at=self.now,
                )
        self.store.close()

        self.store = ClientStore(
            self.database_path,
            integrity_key=integrity_key,
            integrity_anchor_path=anchor_path,
        )
        self.assertEqual(
            self.pack_path.resolve(),
            self.store.verified_pack(self.release_id, self.content_hash),
        )
        latest = self.store.connection.execute(
            "SELECT sequence, state_digest, entry_mac FROM authenticated_state_journal "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(
            ClientStore._anchor_bytes(
                latest["sequence"], latest["state_digest"], latest["entry_mac"]
            ),
            anchor_path.read_bytes(),
        )

    def test_authenticated_state_recovers_initial_anchor_creation_failure(self):
        self.store.close()
        self.database_path.unlink(missing_ok=True)
        integrity_key = b"state-integrity-test-key-32byte!"[:32]
        anchor_path = Path(self.temporary_directory.name) / "identity" / "initial-anchor.json"

        with patch.object(
            ClientStore,
            "_write_anchor",
            side_effect=OSError("injected initial anchor failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected initial"):
                ClientStore(
                    self.database_path,
                    integrity_key=integrity_key,
                    integrity_anchor_path=anchor_path,
                )

        self.store = ClientStore(
            self.database_path,
            integrity_key=integrity_key,
            integrity_anchor_path=anchor_path,
        )
        row = self.store.connection.execute(
            "SELECT sequence, operation, previous_mac FROM authenticated_state_journal"
        ).fetchone()
        self.assertEqual((1, "initialize", "0" * 64), tuple(row))
        self.assertTrue(anchor_path.is_file())

    def test_authenticated_state_recovery_rejects_two_entry_gap(self):
        integrity_key, anchor_path = self._authenticated_store()
        with patch.object(
            self.store,
            "_write_anchor",
            side_effect=OSError("injected post-commit anchor failure"),
        ):
            with self.assertRaises(OSError):
                self.store.cache_pack(
                    self.release_id,
                    self.content_hash,
                    self.pack_path,
                    verified=True,
                    cached_at=self.now,
                )
        self.store.connection.execute("BEGIN IMMEDIATE")
        self.store._append_authenticated_entry(self.store.connection, "test-second-gap")
        self.store.connection.commit()
        self.store.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_recovery_rejects_forged_tail(self):
        integrity_key, anchor_path = self._authenticated_store()
        with patch.object(self.store, "_write_anchor", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.store.cache_pack(
                    self.release_id,
                    self.content_hash,
                    self.pack_path,
                    verified=True,
                    cached_at=self.now,
                )
        self.store.connection.execute(
            "UPDATE authenticated_state_journal SET entry_mac=? "
            "WHERE sequence=(SELECT MAX(sequence) FROM authenticated_state_journal)",
            ("f" * 64,),
        )
        self.store.connection.commit()
        self.store.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_recovery_rejects_mismatched_current_digest(self):
        integrity_key, anchor_path = self._authenticated_store()
        with patch.object(self.store, "_write_anchor", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.store.cache_pack(
                    self.release_id,
                    self.content_hash,
                    self.pack_path,
                    verified=True,
                    cached_at=self.now,
                )
        self.store.connection.execute(
            "UPDATE cached_content_packs SET content_hash=? WHERE release_id=?",
            ("b" * 64, self.release_id),
        )
        self.store.connection.commit()
        self.store.close()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_authenticated_state_recovery_rejects_missing_anchor_over_nonempty_state(self):
        integrity_key, anchor_path = self._authenticated_store()
        self.store.cache_pack(
            self.release_id,
            self.content_hash,
            self.pack_path,
            verified=True,
            cached_at=self.now,
        )
        self.store.close()
        anchor_path.unlink()

        with self.assertRaisesRegex(ValueError, "Authenticated client state is invalid"):
            ClientStore(
                self.database_path,
                integrity_key=integrity_key,
                integrity_anchor_path=anchor_path,
            )

    def test_answer_and_timer_checkpoint_share_one_authenticated_transition(self):
        self._authenticated_store()
        self._cache_and_create()
        before = self.store.connection.execute(
            "SELECT MAX(sequence) FROM authenticated_state_journal"
        ).fetchone()[0]

        self.store.save_answer_checkpoint(
            self.attempt_id,
            3,
            "B",
            saved_at=self.now + timedelta(seconds=1),
            remaining_seconds=1799,
            last_wall_time=self.now + timedelta(seconds=1),
        )

        after = self.store.connection.execute(
            "SELECT MAX(sequence) FROM authenticated_state_journal"
        ).fetchone()[0]
        record = self.store.load_attempt(self.attempt_id)
        self.assertEqual(before + 1, after)
        self.assertEqual("B", record.responses[3])
        self.assertEqual(1799, record.remaining_seconds)
        self.assertEqual(self.now + timedelta(seconds=1), record.last_wall_time)

if __name__ == "__main__":
    unittest.main()
