import base64
import io
import tempfile
import threading
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ksat.client.identity import DeviceIdentity
from ksat.client.runtime import AssessmentRuntime
from ksat.client.store import AttemptSealedError, ClientStore
from ksat.crypto import (
    decrypt_pack,
    encrypt_pack,
    generate_ed25519_keypair,
    sha256_hex,
    sign_json,
    verify_json,
)
from ksat.protocol import (
    AttemptStartResponse,
    AttemptTicket,
    PublicQuestion,
    PublicReleaseDescriptor,
    ReleaseManifest,
    ReleaseSummary,
    SignedAttemptTicket,
    canonical_json,
    deterministic_question_order,
)


UTC = timezone.utc
STARTED = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, wall=STARTED, monotonic=1000.0):
        self.wall = wall
        self.monotonic_value = monotonic

    def utcnow(self):
        return self.wall

    def monotonic(self):
        return self.monotonic_value

    def advance(self, seconds, *, wall=True, monotonic=True):
        if wall:
            self.wall += timedelta(seconds=seconds)
        if monotonic:
            self.monotonic_value += seconds


class ClientRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.db_path = self.root / "client.sqlite3"
        self.pack_path = self.root / "download.ksatpack"
        self.release_id = str(uuid.uuid4())
        self.attempt_id = str(uuid.uuid4())
        self.device_id = str(uuid.uuid4())
        self.student_id = "S100"
        self.coordinator_private, self.coordinator_public = generate_ed25519_keypair()
        self.device_private, self.device_public = generate_ed25519_keypair()
        self.identity = DeviceIdentity(
            private_key_b64=self.device_private,
            public_key_b64=self.device_public,
            device_id=self.device_id,
            coordinator_public_key_b64=self.coordinator_public,
        )
        self.clock = FakeClock()
        self.store = ClientStore(self.db_path)
        self.manifest = ReleaseManifest(
            release_id=self.release_id,
            test_id=41,
            test_name="Placement Set",
            duration_seconds=1800,
            canonical_question_ids=[7, 3],
        )
        self.questions = [
            PublicQuestion(
                question_id=7,
                source_key="q-7",
                category="Reasoning",
                chapter="Series",
                difficulty="Medium",
                question_text="Seven?",
                options={"A": "6", "B": "7", "C": "8", "D": "9"},
            ),
            PublicQuestion(
                question_id=3,
                source_key="q-3",
                category="Quantitative",
                chapter="Numbers",
                difficulty="Easy",
                question_text="Three?",
                options={"A": "3", "B": "4", "C": "5", "D": "6"},
            ),
        ]
        self.content_key = bytes(range(32))
        self._write_pack(self.questions)
        self.summary = self._summary()
        self.start_response = self._start_response()
        self.runtime = self._runtime()

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self.temporary_directory.cleanup()

    def _pack_plaintext(self, questions, *, manifest=None, extra_entries=()):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", canonical_json(manifest or self.manifest))
            archive.writestr(
                "questions.json",
                canonical_json([
                    item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                    for item in questions
                ]),
            )
            for name, value in extra_entries:
                archive.writestr(name, value)
        return stream.getvalue()

    def _write_pack(self, questions, *, manifest=None, extra_entries=()):
        plaintext = self._pack_plaintext(
            questions, manifest=manifest, extra_entries=extra_entries
        )
        self.pack_path.write_bytes(encrypt_pack(self.content_key, self.release_id, plaintext))

    def _summary(self, *, content_hash=None, signature=None):
        digest = content_hash or sha256_hex(self.pack_path.read_bytes())
        signature = signature or sign_json(
            self.coordinator_private,
            {
                "release_id": self.release_id,
                "content_hash": digest,
                "manifest": self.manifest.model_dump(mode="json"),
            },
        )
        return ReleaseSummary(
            release_id=self.release_id,
            test_id=41,
            state="prepared",
            duration_seconds=1800,
            canonical_question_ids=[7, 3],
            content_pack_filename=f"{self.release_id}.ksatpack",
            content_hash=digest,
            content_signature_b64=signature,
            wrapped_content_key_b64=base64.b64encode(b"w" * 60).decode("ascii"),
        )

    def _start_response(self, **changes):
        values = {
            "attempt_id": self.attempt_id,
            "student_id": self.student_id,
            "device_id": self.device_id,
            "release_id": self.release_id,
            "content_hash": sha256_hex(self.pack_path.read_bytes()),
            "started_at": STARTED,
            "deadline": STARTED + timedelta(minutes=30),
            "order_seed_b64": base64.b64encode(bytes(range(32))).decode("ascii"),
            "content_key_b64": base64.b64encode(self.content_key).decode("ascii"),
        }
        values.update(changes)
        ticket = AttemptTicket(**values)
        signed = SignedAttemptTicket(
            ticket=ticket,
            signature_b64=sign_json(self.coordinator_private, ticket),
        )
        return AttemptStartResponse(
            ticket=signed,
            canonical_question_ids=[7, 3],
            server_time=STARTED,
        )

    def _runtime(self, store=None):
        return AssessmentRuntime(store or self.store, self.identity, self.clock)

    def _prepare_and_start(self):
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        return self.runtime.start(self.start_response, student_id=self.student_id)

    def _reopen(self):
        self.store.close()
        self.store = ClientStore(self.db_path)
        return self._runtime()

    def _alternate_artifact(self):
        release_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        content_key = b"z" * 32
        manifest = self.manifest.model_copy(
            update={"release_id": release_id, "test_id": 42}
        )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", canonical_json(manifest))
            archive.writestr(
                "questions.json",
                canonical_json(
                    [item.model_dump(mode="json") for item in self.questions]
                ),
            )
        path = self.root / "alternate.ksatpack"
        path.write_bytes(encrypt_pack(content_key, release_id, stream.getvalue()))
        digest = sha256_hex(path.read_bytes())
        summary = ReleaseSummary(
            release_id=release_id,
            test_id=42,
            state="prepared",
            duration_seconds=1800,
            canonical_question_ids=[7, 3],
            content_pack_filename=f"{release_id}.ksatpack",
            content_hash=digest,
            content_signature_b64=sign_json(
                self.coordinator_private,
                {
                    "release_id": release_id,
                    "content_hash": digest,
                    "manifest": manifest.model_dump(mode="json"),
                },
            ),
            wrapped_content_key_b64=base64.b64encode(b"w" * 60).decode("ascii"),
        )
        ticket = AttemptTicket(
            attempt_id=attempt_id,
            student_id="S200",
            device_id=self.device_id,
            release_id=release_id,
            content_hash=digest,
            started_at=STARTED,
            deadline=STARTED + timedelta(minutes=30),
            order_seed_b64=base64.b64encode(b"a" * 32).decode("ascii"),
            content_key_b64=base64.b64encode(content_key).decode("ascii"),
        )
        response = AttemptStartResponse(
            ticket=SignedAttemptTicket(
                ticket=ticket,
                signature_b64=sign_json(self.coordinator_private, ticket),
            ),
            canonical_question_ids=[7, 3],
            server_time=STARTED,
        )
        return summary, manifest, path, response

    def test_answer_is_local_and_survives_store_reopen(self):
        snapshot = self._prepare_and_start()
        self.assertEqual(snapshot.state, "in_progress")

        answered = self.runtime.answer(7, "B")
        self.assertEqual(answered.responses[7], "B")
        self.assertEqual(self._reopen().recover().responses[7], "B")

    def test_live_monotonic_and_restart_bounds_never_extend_remaining_time(self):
        self._prepare_and_start()
        self.clock.advance(300)
        self.clock.wall = STARTED - timedelta(hours=1)
        live = self.runtime.snapshot()
        self.assertEqual(live.remaining_seconds, 1500)

        recovered = self._reopen().recover()
        self.assertLessEqual(recovered.remaining_seconds, 1500)
        self.clock.wall = STARTED + timedelta(minutes=15)
        suspended = self._reopen().recover()
        self.assertLessEqual(suspended.remaining_seconds, 900)

    def test_live_monotonic_rollback_fails_closed(self):
        self._prepare_and_start()
        self.clock.advance(300)
        self.assertEqual(self.runtime.snapshot().remaining_seconds, 1500)
        self.clock.monotonic_value -= 100
        with self.assertRaisesRegex(RuntimeError, "moved backwards"):
            self.runtime.snapshot()

    def test_question_order_matches_ticket_and_options_are_not_reordered(self):
        snapshot = self._prepare_and_start()
        expected = deterministic_question_order(
            [7, 3], self.start_response.ticket.ticket.order_seed_b64
        )
        self.assertEqual(list(snapshot.question_order), expected)
        self.assertEqual(
            list(self.runtime.question(7).options.items()),
            [("A", "6"), ("B", "7"), ("C", "8"), ("D", "9")],
        )

    def test_repeated_identical_start_does_not_reanchor_the_live_timer(self):
        self._prepare_and_start()
        self.clock.advance(300)
        repeated = self.runtime.start(self.start_response, student_id=self.student_id)
        self.assertLessEqual(repeated.remaining_seconds, 1500)

    def test_two_runtime_connections_cannot_start_different_active_assessments(self):
        second_store = ClientStore(self.db_path)
        second_runtime = self._runtime(second_store)
        try:
            alternate = self._alternate_artifact()
            self.runtime.prepare(self.summary, self.manifest, self.pack_path)
            second_runtime.prepare(alternate[0], alternate[1], alternate[2])
            barrier = threading.Barrier(2)
            outcomes = []

            def start(runtime, response, student_id):
                barrier.wait()
                try:
                    outcomes.append(runtime.start(response, student_id=student_id))
                except ValueError as error:
                    outcomes.append(error)

            threads = (
                threading.Thread(
                    target=start,
                    args=(self.runtime, self.start_response, self.student_id),
                ),
                threading.Thread(
                    target=start,
                    args=(second_runtime, alternate[3], "S200"),
                ),
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(1, sum(not isinstance(item, Exception) for item in outcomes))
            self.assertEqual(1, sum(isinstance(item, ValueError) for item in outcomes))
            error = next(item for item in outcomes if isinstance(item, ValueError))
            self.assertEqual(
                "Another local assessment attempt is already active.", str(error)
            )
            active = self.store.active_attempt()
            self.assertIn(active.attempt_id, {self.attempt_id, alternate[3].ticket.ticket.attempt_id})
        finally:
            second_store.close()

    def test_identical_start_replay_returns_same_sealed_attempt_from_second_runtime(self):
        self._prepare_and_start()
        sealed = self.runtime.submit()
        self.clock.advance(300)
        second_store = ClientStore(self.db_path)
        try:
            second_runtime = self._runtime(second_store)
            second_runtime.prepare(self.summary, self.manifest, self.pack_path)
            replayed = second_runtime.start(
                self.start_response, student_id=self.student_id
            )
            self.assertEqual(replayed, sealed)
            self.assertEqual(len(second_store.pending_submissions()), 1)
        finally:
            second_store.close()

    def test_ticket_processing_time_is_not_added_back_to_the_duration(self):
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)

        def slow_decrypt(*args):
            value = decrypt_pack(*args)
            self.clock.advance(5)
            return value

        with patch("ksat.client.runtime.decrypt_pack", side_effect=slow_decrypt):
            started = self.runtime.start(
                self.start_response, student_id=self.student_id
            )
        self.assertEqual(started.remaining_seconds, 1795)

    def test_stale_start_processed_at_or_after_deadline_is_durably_sealed(self):
        for wall in (
            STARTED + timedelta(minutes=30),
            STARTED + timedelta(hours=1),
        ):
            with self.subTest(wall=wall):
                self.clock.wall = wall
                self.runtime.prepare(self.summary, self.manifest, self.pack_path)
                snapshot = self.runtime.start(
                    self.start_response, student_id=self.student_id
                )
                self.assertEqual(snapshot.state, "sealed_pending")
                self.assertEqual(snapshot.remaining_seconds, 0)
                self.assertEqual(len(self.store.pending_submissions()), 1)
                self.store.close()
                self.store = ClientStore(self.root / f"{wall.hour}.sqlite3")
                self.runtime = self._runtime()

    def test_start_uses_strictest_server_and_local_wall_deadline_bound(self):
        self.clock.wall = STARTED - timedelta(hours=1)
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        server_limited = self.runtime.start(
            self._start_response().model_copy(
                update={"server_time": STARTED + timedelta(minutes=5)}
            ),
            student_id=self.student_id,
        )
        self.assertEqual(server_limited.remaining_seconds, 1500)

        self.store.close()
        self.store = ClientStore(self.root / "wall-limited.sqlite3")
        self.runtime = self._runtime()
        self.clock.wall = STARTED + timedelta(minutes=5)
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        wall_limited = self.runtime.start(
            self._start_response(), student_id=self.student_id
        )
        self.assertEqual(wall_limited.remaining_seconds, 1500)

    def test_expired_start_seal_failure_preserves_zero_time_for_recovery(self):
        self.clock.wall = STARTED + timedelta(minutes=30)
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        with patch.object(self.store, "seal_attempt", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                self.runtime.start(self.start_response, student_id=self.student_id)
        record = self.store.load_attempt(self.attempt_id)
        self.assertEqual(record.state, "in_progress")
        self.assertEqual(record.remaining_seconds, 0)
        self.assertEqual(self.store.pending_submissions(), [])
        recovered = self._reopen().recover()
        self.assertEqual(recovered.state, "sealed_pending")
        self.assertEqual(len(self.store.pending_submissions()), 1)

    def test_prepare_rejects_hash_or_signature_without_verified_cache_state(self):
        for summary in (
            self._summary(content_hash="0" * 64),
            self._summary(signature=base64.b64encode(b"x" * 64).decode("ascii")),
        ):
            with self.subTest(summary=summary.content_hash):
                with self.assertRaises(ValueError):
                    self.runtime.prepare(summary, self.manifest, self.pack_path)
                self.assertIsNone(self.store.verified_pack(self.release_id))

    def test_prepare_consumes_the_transportable_public_descriptor(self):
        descriptor = PublicReleaseDescriptor(
            release_id=self.summary.release_id,
            test_id=self.summary.test_id,
            state=self.summary.state,
            duration_seconds=self.summary.duration_seconds,
            canonical_question_ids=self.summary.canonical_question_ids,
            content_pack_filename=self.summary.content_pack_filename,
            content_hash=self.summary.content_hash,
            content_signature_b64=self.summary.content_signature_b64,
            manifest=self.manifest,
        )
        prepared = self.runtime.prepare(descriptor, self.pack_path)
        self.assertEqual(prepared, self.pack_path.resolve())
        self.assertEqual(
            self.pack_path.resolve(),
            self.store.verified_pack(self.release_id, self.summary.content_hash),
        )

    def test_prepare_and_start_reject_unsupported_protocol_versions(self):
        unsupported_manifest = self.manifest.model_copy(
            update={"pack_format_version": 2}
        )
        self._write_pack(self.questions, manifest=unsupported_manifest)
        unsupported_summary = self._summary()
        unsupported_summary = unsupported_summary.model_copy(
            update={
                "content_signature_b64": sign_json(
                    self.coordinator_private,
                    {
                        "release_id": self.release_id,
                        "content_hash": unsupported_summary.content_hash,
                        "manifest": unsupported_manifest.model_dump(mode="json"),
                    },
                )
            }
        )
        with self.assertRaises(ValueError):
            self.runtime.prepare(
                unsupported_summary, unsupported_manifest, self.pack_path
            )
        self.assertIsNone(self.store.verified_pack(self.release_id))

        self._write_pack(self.questions)
        self.summary = self._summary()
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        unsupported_ticket = self._start_response(protocol_version=2)
        with self.assertRaises(ValueError):
            self.runtime.start(unsupported_ticket, student_id=self.student_id)
        self.assertIsNone(self.store.active_attempt())

    def test_start_rejects_bad_decryption_ticket_binding_and_duration_without_attempt(self):
        self.runtime.prepare(self.summary, self.manifest, self.pack_path)
        cases = (
            self._start_response(content_key_b64=base64.b64encode(b"z" * 32).decode("ascii")),
            self._start_response(device_id=str(uuid.uuid4())),
            self._start_response(student_id="S999"),
            self._start_response(deadline=STARTED + timedelta(minutes=29)),
            self.start_response.model_copy(
                update={"server_time": STARTED - timedelta(seconds=1)}
            ),
        )
        for response in cases:
            with self.subTest(ticket=response.ticket.ticket):
                with self.assertRaises(ValueError):
                    self.runtime.start(response, student_id=self.student_id)
                self.assertIsNone(self.store.active_attempt())

    def test_recursive_answer_metadata_and_unknown_question_fields_fail_closed(self):
        unsafe_questions = (
            [
                {
                    **self.questions[0].model_dump(mode="json"),
                    "Correct_Answer": "B",
                },
                self.questions[1],
            ],
            [{**self.questions[0].model_dump(mode="json"), "mystery": "x"}, self.questions[1]],
            [
                {
                    **self.questions[0].model_dump(mode="json"),
                    "stimulus": {
                        "id": "c",
                        "type": "chart",
                        "content": {
                            "series": [{"name": "x", "Feedback": "no"}]
                        },
                    },
                },
                self.questions[1],
            ],
        )
        for questions in unsafe_questions:
            with self.subTest(questions=questions):
                self._write_pack(questions)
                summary = self._summary()
                self.runtime.prepare(summary, self.manifest, self.pack_path)
                response = self._start_response(content_hash=summary.content_hash)
                with self.assertRaises(ValueError):
                    self.runtime.start(response, student_id=self.student_id)
                self.assertIsNone(self.store.active_attempt())
                self.store.connection.execute("DELETE FROM cached_content_packs")
                self.store.connection.commit()

    def test_duplicate_question_ids_and_manifest_mismatch_fail_closed(self):
        self._write_pack([self.questions[0], self.questions[0]])
        summary = self._summary()
        self.runtime.prepare(summary, self.manifest, self.pack_path)
        with self.assertRaises(ValueError):
            self.runtime.start(
                self._start_response(content_hash=summary.content_hash),
                student_id=self.student_id,
            )
        self.assertIsNone(self.store.active_attempt())

    def test_invalid_question_option_and_post_seal_edit_are_rejected(self):
        self._prepare_and_start()
        with self.assertRaises(ValueError):
            self.runtime.answer(999, "A")
        with self.assertRaises(ValueError):
            self.runtime.answer(7, "E")
        self.runtime.submit()
        with self.assertRaises(AttemptSealedError):
            self.runtime.answer(7, "A")

    def test_violation_deduplicates_same_type_within_two_seconds(self):
        self._prepare_and_start()
        self.runtime.record_violation("focus_lost")
        self.clock.advance(1)
        self.runtime.record_violation("focus_lost")
        self.runtime.record_violation("window_hidden")
        self.clock.advance(2)
        snapshot = self.runtime.record_violation("focus_lost")
        self.assertEqual(snapshot.violations, 3)

    def test_expiry_seals_once_with_verifiable_all_question_bundle(self):
        self._prepare_and_start()
        self.runtime.answer(7, "B")
        self.clock.advance(1800)
        first = self.runtime.tick()
        second = self.runtime.tick()
        self.assertEqual(first.state, "sealed_pending")
        self.assertEqual(second, first)
        pending = self.store.pending_submissions()
        self.assertEqual(len(pending), 1)
        bundle = pending[0].bundle
        self.assertEqual(
            [(item.question_id, item.selected_answer) for item in bundle.bundle.responses],
            [(item, "B" if item == 7 else None) for item in first.question_order],
        )
        self.assertEqual(bundle.bundle.sealed_at, STARTED + timedelta(minutes=30))
        verify_json(self.device_public, bundle.bundle, bundle.device_signature_b64)

    def test_manual_submit_and_reopen_preserve_one_immutable_outbox(self):
        self._prepare_and_start()
        self.clock.advance(600)
        first = self.runtime.submit()
        self.assertEqual(first.remaining_seconds, 1200)
        raw = canonical_json(self.store.pending_submissions()[0].bundle)
        reopened = self._reopen()
        second = reopened.submit()
        self.assertEqual(second.state, "sealed_pending")
        self.assertEqual(first.attempt_id, second.attempt_id)
        self.assertEqual(canonical_json(self.store.pending_submissions()[0].bundle), raw)

    def test_concurrent_tick_and_submit_create_exactly_one_bundle(self):
        self._prepare_and_start()
        self.clock.advance(1800)
        barrier = threading.Barrier(8)
        results = []
        errors = []

        def seal(index):
            try:
                barrier.wait()
                results.append(self.runtime.tick() if index % 2 else self.runtime.submit())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=seal, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({item.state for item in results}, {"sealed_pending"})
        self.assertEqual(len(self.store.pending_submissions()), 1)

    def test_failed_seal_leaves_attempt_editable_and_retry_succeeds(self):
        self._prepare_and_start()
        real_seal = self.store.seal_attempt
        with patch.object(self.store, "seal_attempt", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                self.runtime.submit()
        self.assertEqual(self.store.load_attempt(self.attempt_id).state, "in_progress")
        self.assertEqual(self.store.pending_submissions(), [])
        with patch.object(self.store, "seal_attempt", side_effect=real_seal):
            self.assertEqual(self.runtime.submit().state, "sealed_pending")

    def test_recovery_rejects_a_saved_choice_not_present_in_public_options(self):
        self._prepare_and_start()
        self.runtime.answer(7, "B")
        self.store.connection.execute(
            "UPDATE local_responses SET selected_answer='E' WHERE attempt_id=?",
            (self.attempt_id,),
        )
        self.store.connection.commit()
        with self.assertRaises(ValueError):
            self._reopen().recover()

    def test_mismatched_device_private_key_cannot_seal_an_outbox_bundle(self):
        self._prepare_and_start()
        wrong_private, _ = generate_ed25519_keypair()
        mismatched = DeviceIdentity(
            private_key_b64=wrong_private,
            public_key_b64=self.device_public,
            device_id=self.device_id,
            coordinator_public_key_b64=self.coordinator_public,
        )
        runtime = AssessmentRuntime(self.store, mismatched, self.clock)
        runtime.recover()
        with self.assertRaises(ValueError):
            runtime.submit()
        self.assertEqual(self.store.load_attempt(self.attempt_id).state, "in_progress")
        self.assertEqual(self.store.pending_submissions(), [])


if __name__ == "__main__":
    unittest.main()
