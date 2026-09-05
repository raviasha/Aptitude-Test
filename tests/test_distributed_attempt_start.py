import asyncio
import base64
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request

import app
from ksat.client.identity import DeviceIdentity
from ksat.client.runtime import AssessmentRuntime
from ksat.client.store import ClientStore
from ksat.coordinator import routes as coordinator_routes
from ksat.coordinator.auth import issue_student_access_token
from ksat.coordinator.attempts import AttemptProblem, issue_attempt_ticket
from ksat.coordinator.releases import prepare_release, unwrap_release_content_key
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import FrozenReviewQuestion, PublicQuestion, PublicReleaseDescriptor, device_request_bytes
from ksat.sqlite import connect_sqlite


OPEN = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
CLOSE = datetime(2026, 8, 31, 9, 10, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return OPEN if tz is not None else OPEN.replace(tzinfo=None)


def _json_bytes(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class CountingSnapshot:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.wrapped.close()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


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
                review_questions=[
                    FrozenReviewQuestion(
                        question_id=question.question_id,
                        correct_answer="A",
                        solution_steps=[f"Solution for question {question.question_id}."],
                    )
                    for question in questions
                ],
                assets={},
                pack_dir=app.assessment_packs_dir(),
                signing_private_key_b64=self.config.signing_private_key_b64,
                pack_master_key=self.config.pack_master_key,
                now_iso="2026-08-31T08:30:00+00:00",
            )
            connection.execute(
                """UPDATE release_questions
                   SET options_json='["A","B","C","D"]', correct_answer='A',
                       category='Quantitative Aptitude', chapter='Arithmetic'
                   WHERE release_id=?""",
                (release.release_id,),
            )
            self.release_id = release.release_id
            self.content_hash = release.content_hash
        self.devices = {}
        for label in ("device-a", "device-b", "device-c", "device-d"):
            self.add_device(label)
        self.set_launch_state(True)

    def test_review_route_withholds_keys_until_close_and_enforces_student_ownership(self):
        attempt_id = str(uuid.uuid4())
        with app.db() as connection:
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id,student_id,test_id,started_at,submitted_at,status,total_questions,
                    attempted,correct,score,percentage,release_id,device_id)
                   VALUES (?,?,?,?,?,'submitted',2,1,1,1,50,?,?)""",
                (
                    attempt_id, "S100", self.test_id,
                    OPEN.isoformat(), OPEN.isoformat(), self.release_id,
                    self.devices["device-a"][0],
                ),
            )
            connection.executemany(
                """INSERT INTO responses
                   (attempt_id,question_id,selected_answer,correct,category,chapter,question_order)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (attempt_id, 7, "A", 1, "Quantitative Aptitude", "Arithmetic", 0),
                    (attempt_id, 3, None, 0, "Quantitative Aptitude", "Arithmetic", 1),
                ],
            )
            connection.execute(
                """INSERT INTO submissions
                   (attempt_id,bundle_hash,bundle_json,accepted_at,receipt_json)
                   VALUES (?,?,?,?,'{}')""",
                (attempt_id, "b" * 64, "{}", OPEN.isoformat()),
            )
        path = f"/api/client/v1/reviews/{attempt_id}"
        waiting = self.device_get(path, student_id="S100")
        self.assertEqual(409, waiting.status_code)
        self.assertNotIn("key_b64", waiting.text)

        with app.db() as connection:
            connection.execute("UPDATE tests SET launched=0 WHERE test_id=?", (self.test_id,))
        available = self.device_get(path, student_id="S100")
        self.assertEqual(200, available.status_code, available.text)
        self.assertEqual([7, 3], [item["question_id"] for item in available.json()["responses"]])
        foreign = self.device_get(path, student_id="S101")
        self.assertEqual(404, foreign.status_code)

    def tearDown(self):
        self.client.close()
        coordinator_routes.close_pack_registry(self.config)
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

    async def pack_response(self, *, label="device-a", count_closes=False, force_roll=False):
        del force_roll
        path = f"/api/client/v1/releases/{self.release_id}/pack"
        headers = self.headers("GET", path, label)
        scope = {
            "type": "http",
            "asgi": {"spec_version": "2.3"},
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
            "app": app.app,
        }
        request_received = False

        async def receive_request():
            nonlocal request_received
            if request_received:
                return {"type": "http.disconnect"}
            request_received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        snapshots = []
        real_open_snapshot = coordinator_routes.PackArtifactRegistry._open

        def capture_snapshot(entry):
            snapshot, byte_size = real_open_snapshot(entry)
            if count_closes:
                snapshot = CountingSnapshot(snapshot)
            snapshots.append(snapshot)
            return snapshot, byte_size

        with patch(
            "ksat.coordinator.routes.PackArtifactRegistry._open",
            side_effect=capture_snapshot,
        ):
            response = await coordinator_routes.release_pack(
                self.release_id, Request(scope, receive_request)
            )
        self.assertEqual(1, len(snapshots))
        return response, snapshots[0]

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
        self.assertIn("descriptor", item)
        self.assertEqual(
            {
                "release_id", "filename", "content_hash", "pack_signature_b64",
                "byte_size", "pack_format_version", "descriptor",
            },
            set(item),
        )
        descriptor = item["descriptor"]
        self.assertEqual(
            {
                "release_id",
                "test_id",
                "state",
                "duration_seconds",
                "canonical_question_ids",
                "content_pack_filename",
                "content_hash",
                "content_signature_b64",
                "manifest",
            },
            set(descriptor),
        )
        self.assertEqual(
            {
                "protocol_version",
                "pack_format_version",
                "release_id",
                "test_id",
                "test_name",
                "duration_seconds",
                "canonical_question_ids",
                "asset_names",
            },
            set(descriptor["manifest"]),
        )
        serialized_descriptor = json.dumps(descriptor, sort_keys=True).lower()
        for private_name in (
            "content_key",
            "wrapped_content_key",
            "correct_answer",
            "solution",
            "option_explanations",
            "feedback",
        ):
            self.assertNotIn(private_name, serialized_descriptor)
        pack_response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(200, pack_response.status_code, pack_response.text)
        self.assertEqual(hashlib.sha256(pack_response.content).hexdigest(), item["content_hash"])
        downloaded = Path(self.temporary_directory.name) / "catalog-pack.ksatpack"
        downloaded.write_bytes(pack_response.content)
        with app.db() as connection:
            public_key_b64 = connection.execute(
                "SELECT public_key_b64 FROM devices WHERE device_id=?",
                (self.devices["device-a"][0],),
            ).fetchone()["public_key_b64"]
        local_store = ClientStore(
            Path(self.temporary_directory.name) / "catalog-client.sqlite3"
        )
        try:
            runtime = AssessmentRuntime(
                local_store,
                DeviceIdentity(
                    private_key_b64=self.devices["device-a"][1],
                    public_key_b64=public_key_b64,
                    device_id=self.devices["device-a"][0],
                    coordinator_public_key_b64=self.config.signing_public_key_b64,
                ),
            )
            prepared = runtime.prepare(
                PublicReleaseDescriptor.model_validate(descriptor, strict=True),
                downloaded,
            )
            self.assertEqual(downloaded.resolve(), prepared)
            self.assertEqual(
                downloaded.resolve(),
                local_store.verified_pack(self.release_id, item["content_hash"]),
            )
        finally:
            local_store.close()

    def test_pack_asgi_23_disconnect_closes_snapshot_before_response_returns(self):
        async def exercise_disconnect():
            response, snapshot = await self.pack_response(force_roll=True)
            retained_iterator = response.body_iterator
            first_body_chunk = asyncio.Event()

            async def receive():
                await first_body_chunk.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    first_body_chunk.set()

            scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
            with patch("ksat.coordinator.routes._PACK_COPY_CHUNK_BYTES", 32):
                await response(scope, receive, send)
            self.assertIs(retained_iterator, response.body_iterator)
            self.assertTrue(snapshot.closed)

        asyncio.run(exercise_disconnect())

    def test_pack_asgi_normal_completion_preserves_bytes_headers_and_closes_snapshot(self):
        expected = (app.assessment_packs_dir() / f"{self.release_id}.ksatpack").read_bytes()

        async def exercise_completion():
            response, snapshot = await self.pack_response(force_roll=True)
            retained_response = response
            retained_iterator = response.body_iterator
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
            body = b"".join(
                message.get("body", b"")
                for message in messages
                if message["type"] == "http.response.body"
            )
            start = next(
                message for message in messages
                if message["type"] == "http.response.start"
            )
            headers = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in start["headers"]
            }
            self.assertIs(response, retained_response)
            self.assertIs(response.body_iterator, retained_iterator)
            self.assertEqual(expected, body)
            self.assertEqual(self.content_hash, hashlib.sha256(body).hexdigest())
            self.assertEqual(f'"{self.content_hash}"', headers["etag"])
            self.assertEqual(str(len(expected)), headers["content-length"])
            self.assertTrue(snapshot.closed)

        asyncio.run(exercise_completion())

    def test_pack_asgi_24_send_failure_closes_snapshot_before_raise(self):
        async def exercise_send_failure():
            response, snapshot = await self.pack_response(force_roll=True)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    raise OSError("client disconnected")

            with self.assertRaises(ClientDisconnect):
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
            self.assertTrue(snapshot.closed)

        asyncio.run(exercise_send_failure())

    def test_pack_asgi_task_cancellation_closes_snapshot_before_raise(self):
        async def exercise_cancellation():
            response, snapshot = await self.pack_response(force_roll=True)
            send_started = asyncio.Event()
            block_send = asyncio.Event()

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    send_started.set()
                    await block_send.wait()

            task = asyncio.create_task(
                response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
            )
            await send_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(snapshot.closed)

        asyncio.run(exercise_cancellation())

    def test_pack_asgi_cleanup_is_idempotent_with_retained_body_iterator(self):
        async def exercise_double_close():
            response, snapshot = await self.pack_response(
                count_closes=True,
                force_roll=True,
            )
            retained_iterator = response.body_iterator
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
            await response(scope, receive, send)
            await retained_iterator.aclose()
            await response(scope, receive, send)
            self.assertTrue(snapshot.closed)
            self.assertEqual(1, snapshot.close_calls)

        asyncio.run(exercise_double_close())

    def test_repeated_aborted_pack_downloads_close_all_cached_handles(self):
        async def exercise_repeated_aborts():
            retained = []
            for _ in range(6):
                response, snapshot = await self.pack_response(force_roll=True)
                snapshot_path = Path(snapshot.name)
                self.assertTrue(snapshot_path.exists())
                retained.append((response, response.body_iterator, snapshot, snapshot_path))
                first_body_chunk = asyncio.Event()

                async def receive():
                    await first_body_chunk.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    if message["type"] == "http.response.body" and message.get("body"):
                        first_body_chunk.set()

                with patch("ksat.coordinator.routes._PACK_COPY_CHUNK_BYTES", 32):
                    await response(
                        {"type": "http", "asgi": {"spec_version": "2.3"}},
                        receive,
                        send,
                    )
                self.assertTrue(snapshot.closed)
                self.assertTrue(snapshot_path.exists())
            self.assertTrue(all(item[2].closed for item in retained))
            coordinator_routes.close_pack_registry(self.config)
            self.assertTrue(all(not item[3].exists() for item in retained))

        asyncio.run(exercise_repeated_aborts())

    def test_concurrent_pack_responses_own_independent_snapshots(self):
        async def exercise_concurrent_responses():
            first_response, first_snapshot = await self.pack_response(force_roll=True)
            second_response, second_snapshot = await self.pack_response(
                label="device-b", force_roll=True
            )
            self.assertIsNot(first_snapshot, second_snapshot)
            second_send_started = asyncio.Event()
            release_second_send = asyncio.Event()

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def fail_first_send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    raise OSError("first client disconnected")

            async def hold_second_send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    second_send_started.set()
                    await release_second_send.wait()
                    raise OSError("second client disconnected")

            scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
            first_task = asyncio.create_task(first_response(scope, receive, fail_first_send))
            second_task = asyncio.create_task(second_response(scope, receive, hold_second_send))
            await second_send_started.wait()
            with self.assertRaises(ClientDisconnect):
                await first_task
            self.assertTrue(first_snapshot.closed)
            self.assertFalse(second_snapshot.closed)
            release_second_send.set()
            with self.assertRaises(ClientDisconnect):
                await second_task
            self.assertTrue(second_snapshot.closed)

        asyncio.run(exercise_concurrent_responses())

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

    def test_quarantined_new_start_preempts_pack_access_without_mutation(self):
        with app.db() as connection:
            connection.execute(
                "UPDATE assessment_releases SET state='answer_state_invalid' WHERE release_id=?",
                (self.release_id,),
            )

        path = "/api/client/v1/attempts/start"
        body = _json_bytes({
            "release_id": self.release_id,
            "confirmed_content_hash": self.content_hash,
        })
        unauthenticated = self.client.post(
            path,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(403, unauthenticated.status_code, unauthenticated.text)
        self.assertEqual("device_inactive", unauthenticated.json()["detail"]["code"])
        device_only = self.client.post(
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                **self.headers("POST", path, "device-a", body),
            },
        )
        self.assertEqual(401, device_only.status_code, device_only.text)
        self.assertEqual(
            "invalid_client_session", device_only.json()["detail"]["code"]
        )

        with (
            patch(
                "ksat.coordinator.routes._assert_encrypted_pack_ready",
                wraps=coordinator_routes._assert_encrypted_pack_ready,
            ) as pack_validator,
            patch(
                "ksat.coordinator.routes._pack_path",
                wraps=coordinator_routes._pack_path,
            ) as pack_path,
        ):
            response = self.start(
                "S100", "device-a", at="2026-08-31T09:02:00+00:00"
            )

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(
            "release_answer_state_invalid", response.json()["detail"]["code"]
        )
        pack_validator.assert_not_called()
        pack_path.assert_not_called()
        with app.db() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])

        releases = self.device_get("/api/client/v1/releases")
        assessments = self.device_get(
            "/api/client/v1/assessments",
            student_id="S100",
            at="2026-08-31T09:02:00+00:00",
        )
        self.assertEqual(200, releases.status_code, releases.text)
        self.assertNotIn(
            self.release_id, [item["release_id"] for item in releases.json()["releases"]]
        )
        self.assertEqual(200, assessments.status_code, assessments.text)
        self.assertNotIn(
            self.release_id,
            [item["release_id"] for item in assessments.json()["assessments"]],
        )

    def test_direct_start_service_quarantines_existing_attempt_without_key_access(self):
        started = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        self.assertEqual(200, started.status_code, started.text)
        with app.db() as connection:
            connection.execute(
                "UPDATE assessment_releases SET state='answer_state_invalid' WHERE release_id=?",
                (self.release_id,),
            )
            with (
                patch(
                    "ksat.coordinator.attempts.unwrap_release_content_key",
                    side_effect=AssertionError("quarantined start must not reveal the key"),
                ) as unwrap_key,
                self.assertRaises(AttemptProblem) as caught,
            ):
                issue_attempt_ticket(
                    connection,
                    release_id=self.release_id,
                    student_id="S100",
                    device_id=self.devices["device-a"][0],
                    confirmed_content_hash=self.content_hash,
                    signing_private_key_b64=self.config.signing_private_key_b64,
                    pack_master_key=self.config.pack_master_key,
                    now_utc=datetime(2026, 8, 31, 9, 3, tzinfo=timezone.utc),
                )
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE release_id=?", (self.release_id,)
            ).fetchone()[0]

        self.assertEqual("release_answer_state_invalid", caught.exception.code)
        unwrap_key.assert_not_called()
        self.assertEqual(1, attempt_count)

    def test_start_uses_encrypted_hash_readiness_without_redecrypting_the_pack(self):
        coordinator_routes.warm_pack_registry(self.config)
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

    def test_pack_304_does_not_open_or_validate_content(self):
        path = f"/api/client/v1/releases/{self.release_id}/pack"
        with (
            patch(
                "ksat.coordinator.routes._pack_path",
                side_effect=AssertionError("304 must not resolve or open the pack"),
            ),
            patch(
                "ksat.coordinator.routes.load_release_manifest",
                side_effect=AssertionError("304 must not validate the pack"),
            ),
        ):
            cached = self.device_get(
                path, **{"If-None-Match": f'W/"other", "{self.content_hash}"'}
            )
        self.assertEqual(304, cached.status_code, cached.text)
        self.assertEqual(b"", cached.content)
        self.assertEqual(f'"{self.content_hash}"', cached.headers["etag"])
        self.assertEqual("private, immutable", cached.headers["cache-control"])

    def test_pack_confines_database_path_before_task4_loader(self):
        with app.db() as connection:
            connection.execute(
                """UPDATE assessment_releases SET content_pack_filename = '../escape.ksatpack'
                   WHERE release_id = ?""",
                (self.release_id,),
            )
        with patch(
            "ksat.coordinator.routes.load_release_manifest",
            side_effect=AssertionError("unsafe path reached the Task 4 loader"),
        ) as loader:
            response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("content_not_ready", response.json()["detail"]["code"])
        loader.assert_not_called()

    def test_pack_rejects_symlink_escape_before_task4_loader(self):
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        escaped_path = app.DATA_DIR / "outside-release-root.ksatpack"
        original = pack_path.read_bytes()
        escaped_path.write_bytes(original)
        pack_path.unlink()
        try:
            pack_path.symlink_to(escaped_path)
            resolve_context = nullcontext()
        except OSError:
            pack_path.write_bytes(original)
            real_resolve = Path.resolve
            root = app.assessment_packs_dir().resolve()

            def escaped_resolution(path, strict=False):
                if path == root / pack_path.name:
                    return escaped_path
                return real_resolve(path, strict=strict)

            resolve_context = patch.object(
                Path, "resolve", autospec=True, side_effect=escaped_resolution
            )
        with (
            resolve_context,
            patch(
                "ksat.coordinator.routes.load_release_manifest",
                side_effect=AssertionError("symlink escape reached the Task 4 loader"),
            ) as loader,
        ):
            response = self.device_get(
                f"/api/client/v1/releases/{self.release_id}/pack"
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("content_not_ready", response.json()["detail"]["code"])
        loader.assert_not_called()

    def test_pack_streams_verified_snapshot_after_source_replacement(self):
        path = f"/api/client/v1/releases/{self.release_id}/pack"
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        expected = pack_path.read_bytes()
        unchecked_replacement = b"unchecked replacement bytes"
        real_loader = coordinator_routes.load_release_manifest

        def validate_then_replace(*args, **kwargs):
            summary = real_loader(*args, **kwargs)
            pack_path.write_bytes(unchecked_replacement)
            return summary

        with patch(
            "ksat.coordinator.routes.load_release_manifest",
            side_effect=validate_then_replace,
        ):
            response = self.device_get(path)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(expected, response.content)
        self.assertEqual(self.content_hash, hashlib.sha256(response.content).hexdigest())
        self.assertNotEqual(unchecked_replacement, response.content)

    def test_100_concurrent_catalog_and_pack_requests_validate_once_and_remain_responsive(self):
        path = f"/api/client/v1/releases/{self.release_id}/pack"
        expected = (app.assessment_packs_dir() / f"{self.release_id}.ksatpack").read_bytes()
        real_loader = coordinator_routes.load_release_manifest
        validation_started = threading.Event()
        allow_validation = threading.Event()
        validation_calls = 0
        validation_lock = threading.Lock()

        def slow_validation(*args, **kwargs):
            nonlocal validation_calls
            with validation_lock:
                validation_calls += 1
            validation_started.set()
            self.assertTrue(allow_validation.wait(10))
            return real_loader(*args, **kwargs)

        def fetch(index):
            target = "/api/client/v1/releases" if index % 2 == 0 else path
            return self.client.get(
                target,
                headers=self.headers("GET", target, "device-a"),
            )

        with (
            patch(
                "ksat.coordinator.routes.load_release_manifest",
                side_effect=slow_validation,
            ),
            ThreadPoolExecutor(max_workers=100) as executor,
        ):
            futures = [executor.submit(fetch, index) for index in range(100)]
            self.assertTrue(validation_started.wait(10))
            started = time.perf_counter()
            responsive = self.client.get("/api/build")
            responsiveness_ms = (time.perf_counter() - started) * 1000
            allow_validation.set()
            responses = [future.result(timeout=30) for future in futures]
        self.assertEqual(200, responsive.status_code)
        self.assertLess(responsiveness_ms, 500)
        self.assertEqual([200] * 100, [response.status_code for response in responses])
        self.assertEqual(1, validation_calls)
        for index, response in enumerate(responses):
            if index % 2:
                self.assertEqual(expected, response.content)

    def test_pack_rejects_a_truncated_snapshot_copy(self):
        def copy_only_prefix(source, snapshot):
            copied = source.read(16)
            snapshot.write(copied)
            return hashlib.sha256(copied).hexdigest(), len(copied)

        with patch(
            "ksat.coordinator.routes._copy_pack_to_snapshot",
            side_effect=copy_only_prefix,
            create=True,
        ):
            response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("content_not_ready", response.json()["detail"]["code"])

    def test_pack_rejects_source_mutation_during_snapshot_copy(self):
        pack_path = app.assessment_packs_dir() / f"{self.release_id}.ksatpack"
        original = pack_path.read_bytes()

        class MutatingSource(io.BytesIO):
            def __init__(self, content):
                super().__init__(content)
                self.read_count = 0

            def read(self, size=-1):
                self.read_count += 1
                if self.read_count == 2:
                    position = self.tell()
                    changed = bytearray(self.getvalue())
                    changed[position] ^= 1
                    self.seek(0)
                    self.write(changed)
                    self.seek(position)
                return super().read(size)

        with (
            patch(
                "ksat.coordinator.routes._open_pack_source",
                return_value=MutatingSource(original),
            ),
            patch("ksat.coordinator.routes._PACK_COPY_CHUNK_BYTES", 32),
        ):
            response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("content_not_ready", response.json()["detail"]["code"])

    def test_pack_snapshot_handle_closes_on_success_and_validation_error(self):
        created_snapshots = []

        def new_snapshot():
            snapshot = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
            created_snapshots.append(snapshot)
            return snapshot

        with patch(
            "ksat.coordinator.routes._new_pack_snapshot",
            side_effect=new_snapshot,
            create=True,
        ):
            success = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(200, success.status_code, success.text)
        self.assertTrue(created_snapshots)
        self.assertTrue(all(snapshot.closed for snapshot in created_snapshots))

        created_snapshots.clear()
        coordinator_routes.close_pack_registry(self.config)
        with (
            patch(
                "ksat.coordinator.routes._new_pack_snapshot",
                side_effect=new_snapshot,
                create=True,
            ),
            patch(
                "ksat.coordinator.routes.load_release_manifest",
                side_effect=ValueError("invalid snapshot"),
            ),
        ):
            rejected = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(409, rejected.status_code, rejected.text)
        self.assertTrue(created_snapshots)
        self.assertTrue(all(snapshot.closed for snapshot in created_snapshots))

    def test_pack_validation_never_reads_the_entire_snapshot_into_memory(self):
        snapshots = []

        class BoundedReadSnapshot:
            def __init__(self):
                self.wrapped = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")

            def read(self, size=-1):
                if size is None or size < 0:
                    raise AssertionError("pack validation attempted an unbounded read")
                return self.wrapped.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        def new_snapshot():
            snapshot = BoundedReadSnapshot()
            snapshots.append(snapshot)
            return snapshot

        with patch(
            "ksat.coordinator.routes._new_pack_snapshot",
            side_effect=new_snapshot,
        ):
            response = self.device_get(f"/api/client/v1/releases/{self.release_id}/pack")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(self.content_hash, hashlib.sha256(response.content).hexdigest())
        self.assertTrue(snapshots)
        self.assertTrue(all(snapshot.closed for snapshot in snapshots))

    def test_pack_internal_error_returns_safe_500_and_closes_snapshot(self):
        snapshots = []

        def new_snapshot():
            snapshot = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
            snapshots.append(snapshot)
            return snapshot

        path = f"/api/client/v1/releases/{self.release_id}/pack"
        safe_client = TestClient(app.app, raise_server_exceptions=False)
        try:
            with (
                patch(
                    "ksat.coordinator.routes._new_pack_snapshot",
                    side_effect=new_snapshot,
                ),
                patch(
                    "ksat.coordinator.routes._copy_pack_to_snapshot",
                    side_effect=RuntimeError("unexpected local I/O failure with secret marker"),
                ),
            ):
                response = safe_client.get(path, headers=self.headers("GET", path, "device-a"))
        finally:
            safe_client.close()
        self.assertEqual(500, response.status_code)
        self.assertNotIn("secret marker", response.text)
        self.assertTrue(snapshots)
        self.assertTrue(all(snapshot.closed for snapshot in snapshots))

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
        with patch("app.require_admin_mutation", return_value={"id": "faculty", "role": "admin"}), patch(
            "app.require_user", return_value={"role": "admin"}
        ):
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

    def test_retry_after_two_extensions_returns_original_ticket_and_attempt(self):
        first = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        self.assertEqual(200, first.status_code, first.text)
        original = first.json()
        attempt_id = original["ticket"]["ticket"]["attempt_id"]
        with patch("app.require_admin_mutation", return_value={"id": "faculty", "role": "admin"}), patch(
            "app.datetime", FrozenDateTime
        ):
            for reason in ("Power interruption", "Additional interruption"):
                extended = self.client.post(
                    f"/api/admin/attempts/{attempt_id}/extend",
                    json={"minutes": 5, "reason": reason},
                )
                self.assertEqual(200, extended.status_code, extended.text)
        retried = self.start("S100", "device-a", at="2026-08-31T09:06:00+00:00")
        self.assertEqual(200, retried.status_code, retried.text)
        self.assertEqual(original["ticket"], retried.json()["ticket"])
        self.assertEqual(original["canonical_question_ids"], retried.json()["canonical_question_ids"])
        with app.db() as connection:
            rows = connection.execute(
                "SELECT attempt_id,expires_at,deadline_revision FROM attempts WHERE student_id='S100'"
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(attempt_id, rows[0]["attempt_id"])
        self.assertEqual(2, rows[0]["deadline_revision"])
        self.assertEqual("2026-08-31T09:42:00+00:00", rows[0]["expires_at"])

    def test_extend_and_close_preserve_issued_tickets_but_control_new_starts(self):
        first = self.start("S100", "device-a", at="2026-08-31T09:02:00+00:00")
        second = self.start("S101", "device-b", at="2026-08-31T09:07:00+00:00")
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, second.status_code, second.text)
        with app.db() as connection:
            before = {
                row["attempt_id"]: (
                    row["started_at"], row["expires_at"], row["ticket_json"]
                )
                for row in connection.execute(
                    """SELECT attempt_id, started_at, expires_at, ticket_json
                       FROM attempts WHERE release_id = ? ORDER BY attempt_id""",
                    (self.release_id,),
                ).fetchall()
            }

        with patch("app.require_admin_mutation", return_value={"id": "faculty", "role": "admin"}):
            extended = self.client.post(
                f"/api/admin/tests/{self.test_id}/extend", json={"minutes": 5}
            )
        self.assertEqual(200, extended.status_code, extended.text)
        self.assertEqual(0, extended.json()["attempts_extended"])
        after_extension = self.start(
            "S102", "device-c", at="2026-08-31T09:12:00+00:00"
        )
        self.assertEqual(200, after_extension.status_code, after_extension.text)
        self.assertEqual(
            "2026-08-31T09:42:00Z",
            after_extension.json()["ticket"]["ticket"]["deadline"],
        )

        with patch("app.require_admin_mutation", return_value={"id": "faculty", "role": "admin"}), patch(
            "app.require_user", return_value={"role": "admin"}
        ):
            closed = self.client.post(f"/api/admin/tests/{self.test_id}/close")
        self.assertEqual(200, closed.status_code, closed.text)
        rejected = self.start("S103", "device-d", at="2026-08-31T09:13:00+00:00")
        self.assertEqual(409, rejected.status_code, rejected.text)
        self.assertEqual("assessment_not_launched", rejected.json()["detail"]["code"])

        resumed_first = self.start(
            "S100", "device-a", at="2026-08-31T09:14:00+00:00"
        )
        resumed_second = self.start(
            "S101", "device-b", at="2026-08-31T09:14:00+00:00"
        )
        self.assertEqual(first.json()["ticket"], resumed_first.json()["ticket"])
        self.assertEqual(second.json()["ticket"], resumed_second.json()["ticket"])
        with app.db() as connection:
            after = {
                row["attempt_id"]: (
                    row["started_at"], row["expires_at"], row["ticket_json"]
                )
                for row in connection.execute(
                    """SELECT attempt_id, started_at, expires_at, ticket_json
                       FROM attempts WHERE release_id = ? AND student_id IN ('S100', 'S101')
                       ORDER BY attempt_id""",
                    (self.release_id,),
                ).fetchall()
            }
            release_close = connection.execute(
                "SELECT launch_closes_at FROM assessment_releases WHERE release_id = ?",
                (self.release_id,),
            ).fetchone()["launch_closes_at"]
        self.assertEqual(before, after)
        self.assertEqual("2026-08-31T09:15:00+00:00", release_close)

    def test_faculty_launch_sets_exact_ten_minute_window_without_creating_attempt_deadlines(self):
        self.set_launch_state(False)
        with (
            patch("app.require_admin_mutation", return_value={"id": "faculty", "role": "admin"}),
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
