import base64
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import app
from ksat.coordinator.attempts import AttemptProblem, issue_attempt_ticket
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    AttemptDeadlineUpdate,
    AttemptTicket,
    IntegrityEvent,
    ResponseBundle,
    ResponseEntry,
    SignedAttemptTicket,
    SignedAttemptDeadlineUpdate,
    SignedResponseBundle,
    canonical_json,
    device_request_bytes,
)


class DistributedSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.originals = (
            app.DATA_DIR,
            app.DB_PATH,
            app.BACKUP_DIR,
            app.QUESTION_BANKS_DIR,
            app.app.state.coordinator_config,
        )
        self.original_enrollment = os.environ.get("KSAT_DEVICE_ENROLLMENT_CODE")
        self.original_session_secret = os.environ.get("KSAT_SESSION_SECRET")
        app.DATA_DIR = Path(self.temporary_directory.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = "task-six-enrollment"
        os.environ["KSAT_SESSION_SECRET"] = "task-six-session"
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        self.config = app.app.state.coordinator_config
        writer = getattr(self.config, "submission_writer", None)
        if writer is not None:
            writer.start()
        self.client = TestClient(app.app)
        self.student_id = "S600"
        self.release_id = str(uuid.uuid4())
        self.attempt_id = str(uuid.uuid4())
        self.content_hash = "a" * 64
        self.started_at = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
        self.deadline = self.started_at + timedelta(minutes=2)
        self.device_private_key_b64, device_public_key_b64 = generate_ed25519_keypair()
        self.device_id = str(uuid.uuid4())
        with app.db() as connection:
            connection.execute(
                "INSERT INTO students VALUES (?, 'Student Six', 'unused', 'AIML', 'A', ?)",
                (self.student_id, app.now()),
            )
            test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, created_at, active, launched, mode)
                   VALUES ('Submission Set', '{}', ?, 1, 1, 'faculty')""",
                (app.now(),),
            ).lastrowid
            self.q1 = connection.execute(
                """INSERT INTO questions
                   (question_text, source_key, category, chapter, difficulty,
                    option_a, option_b, option_c, option_d, options_json,
                    correct_answer, created_at)
                   VALUES ('Q1', 'q1', 'Quantitative Aptitude', 'Arithmetic', 'Easy',
                           'one', 'two', 'three', 'four', ?, 'B', ?)""",
                (json.dumps({"A": "one", "B": "two", "C": "three", "D": "four"}), app.now()),
            ).lastrowid
            self.q2 = connection.execute(
                """INSERT INTO questions
                   (question_text, source_key, category, chapter, difficulty,
                    option_a, option_b, option_c, option_d, options_json,
                    correct_answer, created_at)
                   VALUES ('Q2', 'q2', 'Logical Reasoning', 'Logic', 'Easy',
                           'one', 'two', 'three', 'four', ?, 'C', ?)""",
                (json.dumps({"A": "one", "B": "two", "C": "three", "D": "four"}), app.now()),
            ).lastrowid
            manifest = {
                "protocol_version": 1,
                "pack_format_version": 1,
                "release_id": self.release_id,
                "test_id": test_id,
                "test_name": "Submission Set",
                "duration_seconds": 120,
                "canonical_question_ids": [self.q1, self.q2],
                "asset_names": [],
            }
            connection.execute("UPDATE tests SET release_id=? WHERE test_id=?", (self.release_id, test_id))
            connection.execute(
                """INSERT INTO assessment_releases
                   (release_id, test_id, state, duration_seconds, manifest_json,
                    content_pack_filename, content_hash, content_signature_b64,
                    wrapped_content_key_b64, created_at)
                   VALUES (?, ?, 'launched', 120, ?, ?, ?, ?, ?, ?)""",
                (
                    self.release_id,
                    test_id,
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                    f"{self.release_id}.ksatpack",
                    self.content_hash,
                    base64.b64encode(b"s" * 64).decode(),
                    base64.b64encode(b"w" * 60).decode(),
                    app.now(),
                ),
            )
            connection.executemany(
                "INSERT INTO release_questions (release_id, question_id, canonical_order) VALUES (?, ?, ?)",
                ((self.release_id, self.q1, 0), (self.release_id, self.q2, 1)),
            )
            release_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(release_questions)")
            }
            if {"options_json", "correct_answer", "category", "chapter"} <= release_columns:
                connection.execute(
                    """UPDATE release_questions
                       SET options_json='[\"A\",\"B\",\"C\",\"D\"]',
                           correct_answer='B', category='Quantitative Aptitude', chapter='Arithmetic'
                       WHERE release_id=? AND question_id=?""",
                    (self.release_id, self.q1),
                )
                connection.execute(
                    """UPDATE release_questions
                       SET options_json='[\"A\",\"B\",\"C\",\"D\"]',
                           correct_answer='C', category='Logical Reasoning', chapter='Logic'
                       WHERE release_id=? AND question_id=?""",
                    (self.release_id, self.q2),
                )
            connection.execute(
                "INSERT INTO devices VALUES (?, 'submission-device', ?, 'active', ?, NULL)",
                (self.device_id, device_public_key_b64, app.now()),
            )
            self.ticket = AttemptTicket(
                attempt_id=self.attempt_id,
                student_id=self.student_id,
                device_id=self.device_id,
                release_id=self.release_id,
                content_hash=self.content_hash,
                started_at=self.started_at,
                deadline=self.deadline,
                order_seed_b64=base64.b64encode(b"o" * 32).decode(),
                content_key_b64=base64.b64encode(b"k" * 32).decode(),
            )
            self.signed_ticket = SignedAttemptTicket(
                ticket=self.ticket,
                signature_b64=sign_json(self.config.signing_private_key_b64, self.ticket),
            )
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, release_id, device_id, order_seed,
                    ticket_json, started_at, status, total_questions, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', 2, ?)""",
                (
                    self.attempt_id,
                    self.student_id,
                    test_id,
                    self.release_id,
                    self.device_id,
                    self.ticket.order_seed_b64,
                    canonical_json(self.signed_ticket).decode(),
                    self.started_at.isoformat(timespec="seconds"),
                    self.deadline.isoformat(timespec="seconds"),
                ),
            )

    def tearDown(self):
        self.client.close()
        writer = getattr(self.config, "submission_writer", None)
        if writer is not None:
            writer.stop(timeout_seconds=5)
        app.DATA_DIR, app.DB_PATH, app.BACKUP_DIR, app.QUESTION_BANKS_DIR, original_config = self.originals
        app.app.state.coordinator_config = original_config
        if self.original_enrollment is None:
            os.environ.pop("KSAT_DEVICE_ENROLLMENT_CODE", None)
        else:
            os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = self.original_enrollment
        if self.original_session_secret is None:
            os.environ.pop("KSAT_SESSION_SECRET", None)
        else:
            os.environ["KSAT_SESSION_SECRET"] = self.original_session_secret
        self.temporary_directory.cleanup()

    def bundle(self, answers=None, *, sealed_at=None, ticket=None, events=()):
        answers = answers or {self.q1: "B", self.q2: None}
        bundle = ResponseBundle(
            ticket=ticket or self.signed_ticket,
            content_hash=self.content_hash,
            sealed_at=sealed_at or self.deadline,
            responses=[ResponseEntry(question_id=qid, selected_answer=answer) for qid, answer in answers.items()],
            integrity_events=list(events),
        )
        return SignedResponseBundle(
            bundle=bundle,
            device_signature_b64=sign_json(self.device_private_key_b64, bundle),
        )

    def submit(
        self,
        signed_bundle,
        *,
        received_at=None,
        extra_headers=None,
        request_device_id=None,
        request_private_key_b64=None,
    ):
        path = "/api/client/v1/submissions"
        body = canonical_json(signed_bundle)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        nonce = str(uuid.uuid4())
        private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(
            request_private_key_b64 or self.device_private_key_b64
        ))
        request_signature = private_key.sign(device_request_bytes("POST", path, body, timestamp, nonce))
        headers = {
            "Content-Type": "application/json",
            "X-KSAT-Device": request_device_id or self.device_id,
            "X-KSAT-Timestamp": timestamp,
            "X-KSAT-Nonce": nonce,
            "X-KSAT-Signature": base64.b64encode(request_signature).decode(),
            **(extra_headers or {}),
        }
        if received_at is None:
            return self.client.post(path, content=body, headers=headers)
        with patch("ksat.coordinator.routes.utc_now", return_value=received_at):
            return self.client.post(path, content=body, headers=headers)

    def count(self, table):
        with app.db() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_server_scores_bundle_and_duplicate_returns_same_receipt(self):
        signed = self.bundle()
        first = self.submit(signed, extra_headers={"Authorization": "Bearer expired-and-ignored"})
        second = self.submit(signed)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(1, first.json()["score"])
        self.assertEqual(1, first.json()["attempted"])
        self.assertEqual(1, self.count("submissions"))
        self.assertEqual(2, self.count("responses"))

    def test_modified_bundle_signature_is_rejected(self):
        signed = self.bundle({self.q1: "B", self.q2: "A"})
        signed.bundle.responses[0].selected_answer = "A"
        response = self.submit(signed)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("invalid_bundle_signature", response.json()["detail"]["code"])

    def test_late_offline_upload_is_accepted_when_sealed_by_deadline(self):
        response = self.submit(self.bundle(), received_at=self.deadline + timedelta(hours=1))
        self.assertEqual(200, response.status_code, response.text)

    def test_latest_cumulative_revision_authorizes_missed_updates_submission_deadline(self):
        effective = self.deadline + timedelta(minutes=10)
        update = AttemptDeadlineUpdate(
            attempt_id=self.attempt_id,
            release_id=self.release_id,
            device_id=self.device_id,
            base_deadline=self.deadline,
            prior_deadline=self.deadline + timedelta(minutes=5),
            deadline=effective,
            cumulative_extension_seconds=600,
            revision=2,
            issued_at=self.started_at + timedelta(minutes=1),
        )
        signed = SignedAttemptDeadlineUpdate(
            update=update,
            signature_b64=sign_json(self.config.signing_private_key_b64, update),
        )
        with app.db() as connection:
            connection.execute(
                """UPDATE attempts SET expires_at=?,deadline_revision=2,
                     deadline_extension_seconds=600,deadline_update_json=?
                   WHERE attempt_id=?""",
                (effective.isoformat(timespec="seconds"), canonical_json(signed).decode(), self.attempt_id),
            )
        response = self.submit(
            self.bundle(sealed_at=effective), received_at=effective + timedelta(hours=1)
        )
        self.assertEqual(200, response.status_code, response.text)

    def test_legacy_deadline_reads_never_finalize_a_distributed_attempt(self):
        with app.db() as connection:
            connection.execute(
                "INSERT INTO students VALUES ('LEGACY-EXPIRED', 'Legacy', 'unused', 'AIML', 'A', ?)",
                (app.now(),),
            )
            legacy_test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, created_at, active, launched, mode)
                   VALUES ('Legacy expired', '[]', ?, 1, 1, 'faculty')""",
                (app.now(),),
            ).lastrowid
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, started_at, status,
                    total_questions, expires_at)
                   VALUES ('legacy-expired', 'LEGACY-EXPIRED', ?, ?, 'in_progress', 0, ?)""",
                (legacy_test_id, app.now(), "2000-01-01T00:00:00+00:00"),
            )

            self.assertEqual(1, app.finalize_expired_attempts(connection))
            self.assertEqual(
                "in_progress",
                app.serialize_attempt(
                    connection, app.get_attempt(connection, self.attempt_id)
                )["status"],
            )

        admin_request = app.Request({
            "type": "http",
            "method": "GET",
            "path": "/api/admin/dashboard",
            "headers": [],
            "session": {"user": {"role": "admin", "id": "faculty", "name": "Faculty"}},
        })
        app.admin_dashboard(admin_request)

        student_request = app.Request({
            "type": "http",
            "method": "POST",
            "path": f"/api/attempts/{self.attempt_id}/submit",
            "headers": [],
            "session": {
                "user": {"role": "student", "id": self.student_id, "name": "Student Six"}
            },
        })
        with self.assertRaises(app.HTTPException) as legacy_submit:
            app.submit_attempt(
                self.attempt_id,
                app.SubmitPayload(confirmed=True),
                student_request,
            )
        self.assertEqual(409, legacy_submit.exception.status_code)

        with app.db() as connection:
            statuses = dict(connection.execute(
                "SELECT attempt_id, status FROM attempts WHERE attempt_id IN (?, ?)",
                (self.attempt_id, "legacy-expired"),
            ).fetchall())
        self.assertEqual("in_progress", statuses[self.attempt_id])
        self.assertEqual("submitted", statuses["legacy-expired"])

        uploaded = self.submit(
            self.bundle(sealed_at=self.deadline),
            received_at=self.deadline + timedelta(hours=1),
        )
        self.assertEqual(200, uploaded.status_code, uploaded.text)

    def test_question_set_options_hash_deadline_and_ticket_signature_are_validated(self):
        cases = []
        cases.append((self.bundle({self.q1: "B"}), "invalid_question_set"))
        cases.append((self.bundle({self.q1: "B", self.q2: None, 999999: "A"}), "invalid_question_set"))
        cases.append((self.bundle({self.q1: "Z", self.q2: None}), "invalid_response_option"))
        wrong_hash = self.bundle()
        wrong_hash.bundle.content_hash = "b" * 64
        wrong_hash.device_signature_b64 = sign_json(self.device_private_key_b64, wrong_hash.bundle)
        cases.append((wrong_hash, "content_hash_mismatch"))
        cases.append((self.bundle(sealed_at=self.deadline + timedelta(seconds=6)), "deadline_exceeded"))
        bad_ticket = self.signed_ticket.model_copy(update={"signature_b64": base64.b64encode(b"x" * 64).decode()})
        cases.append((self.bundle(ticket=bad_ticket), "invalid_ticket_signature"))
        for signed, expected_code in cases:
            with self.subTest(expected_code):
                response = self.submit(signed)
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual(expected_code, response.json()["detail"]["code"])

    def test_deadline_grace_boundary_is_inclusive(self):
        accepted = self.submit(self.bundle(sealed_at=self.deadline + timedelta(seconds=5)))
        self.assertEqual(200, accepted.status_code, accepted.text)

    def test_duplicate_authentication_precedes_immutable_receipt_short_circuit(self):
        original = self.submit(self.bundle())
        self.assertEqual(200, original.status_code, original.text)
        changed = self.bundle({self.q1: "A", self.q2: "A"})
        retry = self.submit(changed)
        self.assertEqual(original.json(), retry.json())
        changed.device_signature_b64 = base64.b64encode(b"z" * 64).decode()
        rejected = self.submit(changed)
        self.assertEqual(400, rejected.status_code, rejected.text)
        self.assertEqual("invalid_bundle_signature", rejected.json()["detail"]["code"])

    def test_later_question_bank_edits_do_not_change_frozen_scoring(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE questions SET correct_answer='A', options_json='{}' WHERE question_id IN (?, ?)",
                (self.q1, self.q2),
            )
        response = self.submit(self.bundle({self.q1: "B", self.q2: "C"}))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()["score"])

    def test_later_question_bank_deletes_do_not_change_frozen_scoring(self):
        with app.db() as connection:
            connection.execute(
                "DELETE FROM questions WHERE question_id IN (?, ?)",
                (self.q1, self.q2),
            )

        response = self.submit(self.bundle({self.q1: "B", self.q2: "C"}))

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, response.json()["score"])
        self.assertEqual(2, self.count("responses"))

    def test_startup_repairs_complete_unused_prepared_snapshot_from_exact_source(self):
        with app.db() as connection:
            connection.execute("DELETE FROM attempts WHERE attempt_id=?", (self.attempt_id,))
            connection.execute(
                "UPDATE assessment_releases SET state='prepared' WHERE release_id=?",
                (self.release_id,),
            )
            connection.execute(
                "DELETE FROM release_questions WHERE release_id=? AND question_id=?",
                (self.release_id, self.q2),
            )
            connection.execute(
                """UPDATE release_questions SET category=NULL
                   WHERE release_id=? AND question_id=?""",
                (self.release_id, self.q1),
            )

        app.ensure_schema()

        with app.db() as connection:
            rows = connection.execute(
                """SELECT question_id, canonical_order, options_json, correct_answer,
                          category, chapter
                   FROM release_questions WHERE release_id=? ORDER BY canonical_order""",
                (self.release_id,),
            ).fetchall()
            state = connection.execute(
                "SELECT state FROM assessment_releases WHERE release_id=?",
                (self.release_id,),
            ).fetchone()[0]
        self.assertEqual("prepared", state)
        self.assertEqual([self.q1, self.q2], [row["question_id"] for row in rows])
        self.assertEqual([0, 1], [row["canonical_order"] for row in rows])
        self.assertTrue(all(row["options_json"] == '["A","B","C","D"]' for row in rows))
        self.assertEqual(["B", "C"], [row["correct_answer"] for row in rows])
        self.assertTrue(all(row["category"] and row["chapter"] for row in rows))

    def test_incomplete_issued_snapshot_is_quarantined_before_another_ticket(self):
        with app.db() as connection:
            connection.execute(
                """UPDATE release_questions SET chapter=NULL
                   WHERE release_id=? AND question_id=?""",
                (self.release_id, self.q1),
            )

        app.ensure_schema()

        admin_request = app.Request({
            "type": "http",
            "method": "POST",
            "path": "/api/admin/tests/launch",
            "headers": [],
            "session": {"user": {"role": "admin", "id": "faculty", "name": "Faculty"}},
        })
        with self.assertRaises(app.HTTPException) as launch_error:
            app.launch_test(self._test_id(), admin_request)
        self.assertEqual(409, launch_error.exception.status_code)
        self.assertEqual(
            "release_answer_state_invalid", launch_error.exception.detail["code"]
        )

        with app.db() as connection:
            state = connection.execute(
                "SELECT state FROM assessment_releases WHERE release_id=?",
                (self.release_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO students VALUES ('S601', 'Second', 'unused', 'AIML', 'A', ?)",
                (app.now(),),
            )
            with self.assertRaises(AttemptProblem) as caught:
                issue_attempt_ticket(
                    connection,
                    release_id=self.release_id,
                    student_id="S601",
                    device_id=self.device_id,
                    confirmed_content_hash=self.content_hash,
                    signing_private_key_b64=self.config.signing_private_key_b64,
                    pack_master_key=self.config.pack_master_key,
                    now_utc=self.started_at,
                )
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE release_id=?",
                (self.release_id,),
            ).fetchone()[0]
        self.assertEqual("answer_state_invalid", state)
        self.assertEqual("release_answer_state_invalid", caught.exception.code)
        self.assertEqual(1, attempt_count)

    def test_quarantined_release_keeps_submitted_history_and_receipt_readable(self):
        accepted = self.submit(self.bundle({self.q1: "B", self.q2: "C"}))
        self.assertEqual(200, accepted.status_code, accepted.text)
        with app.db() as connection:
            connection.execute(
                """UPDATE release_questions SET options_json=NULL
                   WHERE release_id=? AND question_id=?""",
                (self.release_id, self.q1),
            )

        app.ensure_schema()

        with app.db() as connection:
            self.assertEqual(
                "answer_state_invalid",
                connection.execute(
                    "SELECT state FROM assessment_releases WHERE release_id=?",
                    (self.release_id,),
                ).fetchone()[0],
            )
            history = app.result_for_attempt(connection, self.attempt_id)
        retried = self.submit(self.bundle({self.q1: "A", self.q2: None}))
        self.assertEqual(accepted.json(), retried.json())
        self.assertEqual(2, history["attempt"]["score"])
        self.assertEqual(2, len(history["categories"]))

    def _test_id(self):
        with app.db() as connection:
            return connection.execute(
                "SELECT test_id FROM assessment_releases WHERE release_id=?",
                (self.release_id,),
            ).fetchone()[0]

    def test_integrity_events_and_unanswered_rows_are_persisted_atomically(self):
        event = IntegrityEvent(event_type="focus_lost", occurred_at=self.started_at + timedelta(seconds=10))
        response = self.submit(self.bundle(events=(event,)))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()["violations"])
        self.assertEqual(2, self.count("responses"))
        self.assertEqual(1, self.count("exam_violations"))

    def test_submission_requires_active_matching_device_request_proof(self):
        signed = self.bundle()
        body = canonical_json(signed)
        missing = self.client.post(
            "/api/client/v1/submissions",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(403, missing.status_code, missing.text)

        foreign_private, foreign_public = generate_ed25519_keypair()
        foreign_id = str(uuid.uuid4())
        with app.db() as connection:
            connection.execute(
                "INSERT INTO devices VALUES (?, 'foreign', ?, 'active', ?, NULL)",
                (foreign_id, foreign_public, app.now()),
            )
        foreign = self.submit(
            signed,
            request_device_id=foreign_id,
            request_private_key_b64=foreign_private,
        )
        self.assertEqual(403, foreign.status_code, foreign.text)
        self.assertEqual("device_identity_mismatch", foreign.json()["detail"]["code"])

        with app.db() as connection:
            connection.execute("UPDATE devices SET status='inactive' WHERE device_id=?", (self.device_id,))
        inactive = self.submit(signed)
        self.assertEqual(403, inactive.status_code, inactive.text)

    def test_attempt_ticket_and_event_timestamp_identity_is_exact(self):
        altered_ticket = self.ticket.model_copy(update={"student_id": "SOMEONE-ELSE"})
        altered_signed_ticket = SignedAttemptTicket(
            ticket=altered_ticket,
            signature_b64=sign_json(self.config.signing_private_key_b64, altered_ticket),
        )
        mismatch = self.submit(self.bundle(ticket=altered_signed_ticket))
        self.assertEqual(400, mismatch.status_code, mismatch.text)
        self.assertEqual("attempt_identity_mismatch", mismatch.json()["detail"]["code"])

        event = IntegrityEvent(
            event_type="focus_lost",
            occurred_at=self.deadline + timedelta(seconds=1),
        )
        invalid_time = self.submit(self.bundle(events=(event,)))
        self.assertEqual(400, invalid_time.status_code, invalid_time.text)
        self.assertEqual("invalid_timestamp", invalid_time.json()["detail"]["code"])

    def test_malformed_timestamps_nonfinite_timing_and_private_scores_are_rejected(self):
        payload = self.bundle().model_dump(mode="json")
        payload["bundle"]["sealed_at"] = "not-a-timestamp"
        malformed = self.submit(payload)
        self.assertEqual(422, malformed.status_code, malformed.text)

        payload = self.bundle().model_dump(mode="json")
        payload["bundle"]["responses"][0]["elapsed_seconds"] = float("nan")
        nonfinite = self.submit(payload)
        self.assertEqual(422, nonfinite.status_code, nonfinite.text)

        payload = self.bundle().model_dump(mode="json")
        payload["bundle"]["score"] = 999
        private_score = self.submit(payload)
        self.assertEqual(422, private_score.status_code, private_score.text)


if __name__ == "__main__":
    unittest.main()
