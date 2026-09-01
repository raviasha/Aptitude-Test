import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
RELEASE_ID = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)


@dataclass
class FakeSnapshot:
    attempt_id: str = ATTEMPT_ID
    state: str = "in_progress"
    question_order: tuple[int, ...] = (7, 3)
    responses: dict[int, str | None] = None
    remaining_seconds: int = 1200
    violations: int = 0
    current_question_id: int = 7

    def __post_init__(self):
        if self.responses is None:
            self.responses = {7: None, 3: None}


class FakeIdentityStore:
    def __init__(self, enrolled=True):
        self.identity = SimpleNamespace(
            device_id="44444444-4444-4444-8444-444444444444" if enrolled else None,
            coordinator_public_key_b64="public-binding" if enrolled else None,
            private_key_b64="must-never-leak",
            public_key_b64="device-public",
        )
        self.loads = 0

    def load_or_create(self):
        self.loads += 1
        return self.identity


class FakeStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.receipt = None
        self.pending = []
        self.closed = 0

    def active_attempt(self):
        if self.snapshot and self.snapshot.state in {"in_progress", "sealed_pending"}:
            return self.record()
        return None

    def load_attempt(self, attempt_id):
        if not self.snapshot or attempt_id != self.snapshot.attempt_id:
            raise KeyError("unknown attempt")
        return self.record()

    def record(self):
        return SimpleNamespace(
            attempt_id=self.snapshot.attempt_id,
            release_id=RELEASE_ID,
            state=self.snapshot.state,
            deadline=NOW + timedelta(minutes=20),
            question_order=self.snapshot.question_order,
            responses=dict(self.snapshot.responses),
            remaining_seconds=self.snapshot.remaining_seconds,
            sealed_at=NOW if self.snapshot.state != "in_progress" else None,
            receipt=self.receipt,
            ticket=SimpleNamespace(ticket=SimpleNamespace(student_id="S100")),
            last_wall_time=NOW,
            current_question_id=self.snapshot.current_question_id,
        )

    def verified_pack(self, release_id, content_hash=None):
        return None

    def pending_submissions(self, **_kwargs):
        return list(self.pending)

    def close(self):
        self.closed += 1


class FakeRuntime:
    def __init__(self, store, *, recovery_error=None, event_log=None):
        self.store = store
        self.recovery_error = recovery_error
        self.event_log = event_log if event_log is not None else []
        self.recover_count = 0
        self.answer_calls = []
        self.violation_calls = []
        self.prepare_calls = []
        self.start_calls = []
        self.position_calls = []
        self.seal_count = 0
        self.expire_on_snapshot = False
        self.questions = {
            7: SimpleNamespace(
                question_id=7,
                category="Aptitude",
                chapter="Logic",
                difficulty="Medium",
                question_text='<img src=x onerror="steal()">What is 2 + 2?',
                question_html='<script>steal()</script>',
                options={"A": "<b>3</b>", "B": "4", "C": "5", "D": "6"},
                stimulus=None,
                display_media=SimpleNamespace(model_dump=lambda **_: {"question": None, "options": {}}),
            ),
            3: SimpleNamespace(
                question_id=3,
                category="Aptitude",
                chapter="Logic",
                difficulty="Easy",
                question_text="Second question",
                question_html="",
                options={"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                stimulus=None,
                display_media=SimpleNamespace(model_dump=lambda **_: {"question": None, "options": {}}),
            ),
        }
        self.assets = {}

    def recover(self):
        self.recover_count += 1
        if self.recovery_error:
            raise self.recovery_error
        return self.store.snapshot

    def snapshot(self):
        if self.recovery_error:
            raise self.recovery_error
        if self.store.snapshot is None:
            raise RuntimeError("no active attempt")
        if self.expire_on_snapshot and self.store.snapshot.state == "in_progress":
            self.submit()
        return self.store.snapshot

    def question(self, question_id):
        return self.questions[question_id]

    def public_asset(self, attempt_id, reference):
        if attempt_id != self.store.snapshot.attempt_id:
            raise ValueError("attempt mismatch")
        try:
            return self.assets[reference]
        except KeyError as error:
            raise KeyError("missing public asset") from error

    def answer(self, question_id, selected_answer):
        self.answer_calls.append((question_id, selected_answer))
        self.store.snapshot.responses[question_id] = selected_answer
        return self.store.snapshot

    def record_violation(self, event_type):
        self.violation_calls.append(event_type)
        self.store.snapshot.violations += 1
        return self.store.snapshot

    def position(self, question_id):
        self.position_calls.append(question_id)
        self.store.snapshot.current_question_id = question_id
        return self.store.snapshot

    def submit(self):
        if self.store.snapshot.state == "in_progress":
            self.event_log.append("seal")
            self.seal_count += 1
            self.store.snapshot.state = "sealed_pending"
            self.store.pending = [SimpleNamespace(
                attempt_id=ATTEMPT_ID,
                retry_count=0,
                next_attempt_at=NOW,
                last_error=None,
                status="pending",
            )]
        return self.store.snapshot

    def prepare(self, descriptor, path):
        self.prepare_calls.append((descriptor, path))
        return path

    def start(self, response, *, student_id):
        self.start_calls.append((response, student_id))
        return self.store.snapshot


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self._session = None
        self.catalog = []
        self.assessment_rows = []
        self.start_response = object()
        self.closed = 0

    @property
    def session(self):
        return self._session

    def enroll(self, label, enrollment_code):
        self.calls.append(("enroll", label, enrollment_code))
        return SimpleNamespace(device_id="55555555-5555-4555-8555-555555555555")

    def login(self, student_id, password):
        self.calls.append(("login", student_id, password))
        self._session = SimpleNamespace(
            student_id=student_id.strip().upper(),
            student_name="Student One",
            device_id="44444444-4444-4444-8444-444444444444",
            access_token="secret-bearer",
            expires_in_seconds=600,
        )
        return self._session

    def logout(self):
        self.calls.append(("logout",))
        self._session = None

    def prefetch_catalog(self):
        self.calls.append(("catalog",))
        return self.catalog

    def assessments(self):
        self.calls.append(("assessments",))
        return self.assessment_rows

    def download_pack(self, entry, path):
        self.calls.append(("download", entry, path))
        return path

    def start_attempt(self, release_id, content_hash):
        self.calls.append(("start", release_id, content_hash))
        return self.start_response

    def close(self):
        self.closed += 1


class FakeOutbox:
    def __init__(self, event_log=None):
        self.event_log = event_log if event_log is not None else []
        self.starts = 0
        self.stops = 0
        self.wakes = 0

    def start(self):
        self.starts += 1

    def stop(self, *_args, **_kwargs):
        self.stops += 1

    def wake(self):
        self.event_log.append("wake")
        self.wakes += 1


class ClientAppApiTests(unittest.TestCase):
    def setUp(self):
        from client_app import ClientServices, create_client_app

        self.root = Path(tempfile.mkdtemp())
        self.events = []
        self.identity_store = FakeIdentityStore()
        self.snapshot = FakeSnapshot()
        self.store = FakeStore(self.snapshot)
        self.runtime = FakeRuntime(self.store, event_log=self.events)
        self.coordinator = FakeCoordinator()
        self.coordinator._session = SimpleNamespace(
            student_id="S100", student_name="Student One", access_token="secret-bearer"
        )
        self.outbox = FakeOutbox(self.events)
        self.services = ClientServices(
            self.identity_store,
            self.store,
            self.runtime,
            self.coordinator,
            self.outbox,
            cache_dir=self.root / "packs",
        )
        self.app = create_client_app(self.services)
        self.client_context = TestClient(
            self.app, base_url="http://127.0.0.1:8010"
        )
        self.client = self.client_context.__enter__()
        page = self.client.get("/", headers={"Host": "127.0.0.1:8010"})
        self.assertEqual(200, page.status_code)
        match = re.search(r'<meta name="ksat-csrf" content="([A-Za-z0-9_-]+)">', page.text)
        self.assertIsNotNone(match)
        self.mutation_headers = {
            "Host": "127.0.0.1:8010",
            "Origin": "http://127.0.0.1:8010",
            "X-KSAT-CSRF": match.group(1),
        }

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def test_answer_and_violation_routes_are_local_only(self):
        self.coordinator.calls.clear()
        answer = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            json={"answer": "B"},
            headers=self.mutation_headers,
        )
        violation = self.client.post(
            f"/api/attempts/{ATTEMPT_ID}/violations",
            json={"event_type": "focus_lost"},
            headers=self.mutation_headers,
        )
        self.assertEqual(200, answer.status_code)
        self.assertEqual("B", answer.json()["selected_answer"])
        self.assertEqual(200, violation.status_code)
        self.assertEqual(1, violation.json()["violations"])
        self.assertEqual([], self.coordinator.calls)

    def test_hostile_origin_host_and_cross_attempt_have_zero_side_effects(self):
        before = dict(self.snapshot.responses)
        hostile_origin = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            json={"answer": "B"},
            headers={**self.mutation_headers, "Origin": "https://evil.example"},
        )
        hostile_host = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            json={"answer": "B"},
            headers={**self.mutation_headers, "Host": "coordinator.lab"},
        )
        cross_attempt = self.client.put(
            f"/api/attempts/{OTHER_ATTEMPT_ID}/responses/7",
            json={"answer": "B"},
            headers=self.mutation_headers,
        )
        missing_origin = self.client.post(
            f"/api/attempts/{ATTEMPT_ID}/submit",
            json={"confirmed": True},
            headers={
                "Host": "127.0.0.1:8010",
                "X-KSAT-CSRF": self.mutation_headers["X-KSAT-CSRF"],
            },
        )
        self.assertEqual(403, hostile_origin.status_code)
        self.assertEqual(400, hostile_host.status_code)
        self.assertEqual(409, cross_attempt.status_code)
        self.assertEqual(403, missing_origin.status_code)
        self.assertEqual([], self.runtime.answer_calls)
        self.assertEqual(before, self.snapshot.responses)
        self.assertEqual(0, self.runtime.seal_count)

    def test_origin_must_normalize_to_the_exact_loopback_host(self):
        before = dict(self.snapshot.responses)
        rejected = (
            ("localhost:8010", "http://127.0.0.1:8010"),
            ("127.0.0.1:8010", "http://localhost:8010"),
            ("localhost:8010", "null"),
            ("localhost:8010", "http://localhost:8010/path"),
            ("localhost:8010", "http://user@localhost:8010"),
            ("localhost.evil:8010", "http://localhost.evil:8010"),
            ("localhost:", "http://localhost"),
        )
        for host, origin in rejected:
            with self.subTest(host=host, origin=origin):
                response = self.client.put(
                    f"/api/attempts/{ATTEMPT_ID}/responses/7",
                    json={"answer": "B"},
                    headers={
                        **self.mutation_headers,
                        "Host": host,
                        "Origin": origin,
                    },
                )
                self.assertIn(response.status_code, (400, 403))
        self.assertEqual([], self.runtime.answer_calls)
        self.assertEqual(before, self.snapshot.responses)

        accepted = (
            ("LOCALHOST:80", "HTTP://LOCALHOST"),
            ("localhost", "http://localhost:80"),
            ("[::1]:8010", "http://[::1]:8010"),
        )
        for host, origin in accepted:
            with self.subTest(host=host, origin=origin):
                response = self.client.put(
                    f"/api/attempts/{ATTEMPT_ID}/responses/7",
                    json={"answer": "B"},
                    headers={
                        **self.mutation_headers,
                        "Host": host,
                        "Origin": origin,
                    },
                )
                self.assertEqual(200, response.status_code)

    def test_polling_expiry_wakes_once_but_ordinary_polls_do_not(self):
        initial_wakes = self.outbox.wakes
        for _ in range(2):
            response = self.client.get(
                f"/api/attempts/{ATTEMPT_ID}",
                headers={"Host": "127.0.0.1:8010"},
            )
            self.assertEqual("in_progress", response.json()["state"])
        self.assertEqual(initial_wakes, self.outbox.wakes)

        self.events.clear()
        self.runtime.expire_on_snapshot = True
        expired = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}",
            headers={"Host": "127.0.0.1:8010"},
        )
        self.assertEqual("sealed_pending", expired.json()["state"])
        self.assertEqual(["seal", "wake"], self.events)
        first_wake_count = self.outbox.wakes
        for _ in range(3):
            self.client.get(
                f"/api/attempts/{ATTEMPT_ID}",
                headers={"Host": "127.0.0.1:8010"},
            )
        self.assertEqual(first_wake_count, self.outbox.wakes)

    def test_explicit_recovery_observes_durable_seal_and_wakes_once(self):
        self.events.clear()
        self.snapshot.state = "sealed_pending"
        self.store.pending = [SimpleNamespace(
            attempt_id=ATTEMPT_ID,
            retry_count=0,
            next_attempt_at=NOW,
            last_error=None,
            status="pending",
        )]
        context = self.app.state.client_context
        recovered = context.recover()
        self.assertEqual("sealed_pending", recovered.state)
        self.assertEqual(["wake"], self.events)
        context.recover()
        self.assertEqual(["wake"], self.events)

    def test_submit_seals_before_worker_wake_and_never_calls_coordinator(self):
        self.coordinator.calls.clear()
        self.events.clear()
        response = self.client.post(
            f"/api/attempts/{ATTEMPT_ID}/submit",
            json={"confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual("sealed_pending", response.json()["state"])
        self.assertEqual(
            "Your answers are safe and will upload automatically.",
            response.json()["message"],
        )
        self.assertEqual(["seal", "wake"], self.events)
        self.assertEqual([], self.coordinator.calls)
        duplicate = self.client.post(
            f"/api/attempts/{ATTEMPT_ID}/submit",
            json={"confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(202, duplicate.status_code)
        self.assertEqual(1, self.runtime.seal_count)

    def test_startup_recovers_same_attempt_and_starts_one_worker_per_lifespan(self):
        state = self.client.get("/api/state", headers={"Host": "127.0.0.1:8010"})
        self.assertEqual("in_progress", state.json()["state"])
        self.assertEqual(ATTEMPT_ID, state.json()["attempt"]["attempt_id"])
        self.assertEqual(1, self.runtime.recover_count)
        self.assertEqual(1, self.outbox.starts)

    def test_position_route_is_local_attempt_bound_and_rejects_sealed_changes(self):
        moved = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/position",
            json={"question_id": 3},
            headers=self.mutation_headers,
        )
        self.assertEqual(200, moved.status_code)
        self.assertEqual(3, moved.json()["current_question_id"])
        self.assertEqual([3], self.runtime.position_calls)
        self.assertEqual([], self.coordinator.calls)
        cross = self.client.put(
            f"/api/attempts/{OTHER_ATTEMPT_ID}/position",
            json={"question_id": 7},
            headers=self.mutation_headers,
        )
        self.assertEqual(409, cross.status_code)
        self.snapshot.state = "sealed_pending"
        sealed = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/position",
            json={"question_id": 7},
            headers=self.mutation_headers,
        )
        self.assertEqual(409, sealed.status_code)
        self.assertEqual([3], self.runtime.position_calls)

    def test_expiry_at_startup_is_reported_as_durably_sealed(self):
        self.snapshot.state = "sealed_pending"
        self.store.pending = [SimpleNamespace(
            attempt_id=ATTEMPT_ID, retry_count=2,
            next_attempt_at=NOW + timedelta(seconds=4), last_error="socket path C:/secret",
            status="pending",
        )]
        state = self.client.get("/api/state", headers={"Host": "127.0.0.1:8010"}).json()
        self.assertEqual("sealed_pending", state["state"])
        self.assertEqual(2, state["attempt"]["queue"]["retry_count"])
        self.assertNotIn("C:/secret", str(state))
        self.assertEqual(
            "Your answers are safe and will upload automatically.", state["attempt"]["message"]
        )

    def test_acknowledged_result_contains_only_authoritative_receipt(self):
        self.snapshot.state = "acknowledged"
        self.store.receipt = SimpleNamespace(
            attempt_id=ATTEMPT_ID,
            accepted_at=NOW,
            score=1,
            total_questions=2,
            attempted=1,
            percentage=50.0,
            violations=0,
            model_dump=lambda **_: {
                "attempt_id": ATTEMPT_ID,
                "accepted_at": NOW,
                "score": 1,
                "total_questions": 2,
                "attempted": 1,
                "percentage": 50.0,
                "violations": 0,
            },
        )
        result = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}/result",
            headers={"Host": "127.0.0.1:8010"},
        )
        self.assertEqual(200, result.status_code)
        self.assertEqual("acknowledged_result", result.json()["state"])
        self.assertEqual(1, result.json()["result"]["score"])
        self.assertNotIn("responses", result.json()["result"])
        polled = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}",
            headers={"Host": "127.0.0.1:8010"},
        ).json()
        self.assertEqual("acknowledged_result", polled["state"])
        self.assertEqual(1, polled["result"]["score"])

    def test_corrupt_recovery_and_intervention_are_stable_and_opaque(self):
        self.runtime.recovery_error = ValueError("raw sqlite row and secret path")
        self.app.state.client_context.startup_problem = None
        self.app.state.client_context.recover()
        first = self.client.get("/api/state", headers={"Host": "127.0.0.1:8010"}).json()
        second = self.client.get("/api/state", headers={"Host": "127.0.0.1:8010"}).json()
        self.assertEqual("faculty_intervention_required", first["state"])
        self.assertEqual("corrupt_local_attempt", first["problem"]["code"])
        self.assertRegex(first["problem"]["diagnostic_reference"], r"^KSAT-[A-Z0-9]{10}$")
        self.assertEqual(first, second)
        self.assertNotIn("sqlite", str(first).lower())

        self.runtime.recovery_error = None
        self.app.state.client_context.startup_problem = None
        self.snapshot.state = "sealed_pending"
        self.store.pending = [SimpleNamespace(
            attempt_id=ATTEMPT_ID, retry_count=1, next_attempt_at=NOW,
            last_error="invalid receipt leaked details", status="faculty_intervention_required",
        )]
        intervention = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}", headers={"Host": "127.0.0.1:8010"}
        ).json()
        self.assertEqual("faculty_intervention_required", intervention["state"])
        self.assertEqual(
            "The sealed submission needs Faculty attention. Your answers remain saved on this computer.",
            intervention["message"],
        )
        self.assertNotIn("leaked details", str(intervention))

    def test_prefetch_passes_task9_entry_and_task8_descriptor_unchanged(self):
        descriptor = SimpleNamespace(
            release_id=RELEASE_ID,
            content_hash="a" * 64,
            content_pack_filename=f"{RELEASE_ID}.ksatpack",
        )
        entry = SimpleNamespace(
            release_id=RELEASE_ID,
            filename=f"{RELEASE_ID}.ksatpack",
            content_hash="a" * 64,
            descriptor=descriptor,
        )
        self.coordinator.catalog = [entry]
        response = self.client.post(
            "/api/content/prefetch",
            json={"release_id": RELEASE_ID},
            headers=self.mutation_headers,
        )
        self.assertEqual(200, response.status_code)
        download = next(call for call in self.coordinator.calls if call[0] == "download")
        self.assertIs(entry, download[1])
        self.assertIs(descriptor, self.runtime.prepare_calls[0][0])

    def test_failed_pack_verification_is_not_prepared_or_reported_ready(self):
        from ksat.client.coordinator import ContentVerificationError

        descriptor = SimpleNamespace(
            release_id=RELEASE_ID, content_hash="a" * 64,
            content_pack_filename=f"{RELEASE_ID}.ksatpack",
        )
        entry = SimpleNamespace(
            release_id=RELEASE_ID, filename=f"{RELEASE_ID}.ksatpack",
            content_hash="a" * 64, descriptor=descriptor,
        )
        self.coordinator.catalog = [entry]
        self.coordinator.download_pack = lambda *_: (_ for _ in ()).throw(
            ContentVerificationError("private path and hash details")
        )
        response = self.client.post(
            "/api/content/prefetch", json={"release_id": RELEASE_ID},
            headers=self.mutation_headers,
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("content_hash_mismatch", response.json()["problem"]["code"])
        self.assertEqual([], self.runtime.prepare_calls)
        self.assertNotIn("private path", response.text)
        self.coordinator.calls.clear()
        start = self.client.post(
            f"/api/assessments/{RELEASE_ID}/start",
            json={"confirmed": True}, headers=self.mutation_headers,
        )
        self.assertEqual(409, start.status_code)
        self.assertEqual("content_not_ready", start.json()["problem"]["code"])
        self.assertFalse(any(call[0] == "start" for call in self.coordinator.calls))

    def test_start_requests_one_ticket_then_transitions_to_local_runtime(self):
        descriptor = SimpleNamespace(
            release_id=RELEASE_ID,
            content_hash="a" * 64,
            content_pack_filename=f"{RELEASE_ID}.ksatpack",
        )
        self.app.state.client_context.catalog[RELEASE_ID] = SimpleNamespace(
            release_id=RELEASE_ID, content_hash="a" * 64, descriptor=descriptor
        )
        self.app.state.client_context.ready_releases.add(RELEASE_ID)
        self.coordinator.calls.clear()
        response = self.client.post(
            f"/api/assessments/{RELEASE_ID}/start",
            json={"confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([("start", RELEASE_ID, "a" * 64)], self.coordinator.calls)
        self.assertEqual([(self.coordinator.start_response, "S100")], self.runtime.start_calls)

    def test_malformed_oversized_and_extra_json_do_not_mutate_state(self):
        malformed = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            content=b'{"answer":',
            headers={**self.mutation_headers, "Content-Type": "application/json"},
        )
        extra = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            json={"answer": "B", "coordinator_url": "https://secret"},
            headers=self.mutation_headers,
        )
        oversized = self.client.put(
            f"/api/attempts/{ATTEMPT_ID}/responses/7",
            content=b'{"answer":"' + (b"A" * 70000) + b'"}',
            headers={**self.mutation_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(400, malformed.status_code)
        self.assertEqual(422, extra.status_code)
        self.assertEqual(413, oversized.status_code)
        self.assertEqual([], self.runtime.answer_calls)

    def test_dynamic_routes_have_security_headers_and_do_not_leak_secrets(self):
        response = self.client.get("/api/state", headers={"Host": "127.0.0.1:8010"})
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        serialized = response.text.lower()
        for marker in ("private_key", "content_key", "access_token", "coordinator_url"):
            self.assertNotIn(marker, serialized)

    def test_public_asset_route_is_exact_attempt_bound_and_nosniff(self):
        content = b"\x89PNG\r\n\x1a\npublic-image"
        digest = __import__("hashlib").sha256(content).hexdigest()
        reference = f"assets/{digest}.png"
        self.runtime.assets[reference] = SimpleNamespace(
            reference=reference, media_type="image/png", content=content
        )
        self.runtime.questions[7].display_media = SimpleNamespace(
            model_dump=lambda **_: {
                "question": {
                    "url": reference, "alt_text": "Question chart",
                    "width": 40, "height": 30,
                },
                "options": {
                    "B": {
                        "url": reference, "alt_text": "Option B chart",
                        "width": 40, "height": 30,
                    }
                },
            }
        )
        attempt = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}",
            headers={"Host": "127.0.0.1:8010"},
        ).json()
        self.assertEqual(reference, attempt["questions"][0]["display_media"]["question"]["url"])
        response = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}/assets/{digest}.png",
            headers={"Host": "127.0.0.1:8010"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(content, response.content)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("default-src 'none'; sandbox", response.headers["content-security-policy"])
        cross = self.client.get(
            f"/api/attempts/{OTHER_ATTEMPT_ID}/assets/{digest}.png",
            headers={"Host": "127.0.0.1:8010"},
        )
        missing = self.client.get(
            f"/api/attempts/{ATTEMPT_ID}/assets/{'0' * 64}.png",
            headers={"Host": "127.0.0.1:8010"},
        )
        self.assertEqual(409, cross.status_code)
        self.assertEqual(404, missing.status_code)
        self.assertNotIn("private", missing.text.lower())
        for hostile_path in (
            f"/api/attempts/{ATTEMPT_ID}/assets/../private.json",
            f"/api/attempts/{ATTEMPT_ID}/assets/%2e%2e%2fprivate.json",
            f"/api/attempts/{ATTEMPT_ID}/assets/{digest}.png%2fsecret",
        ):
            with self.subTest(path=hostile_path):
                hostile = self.client.get(
                    hostile_path, headers={"Host": "127.0.0.1:8010"}
                )
                self.assertEqual(404, hostile.status_code)

    def test_injected_resources_remain_caller_owned_after_lifespan(self):
        self.assertFalse(self.services.owns_resources)
        self.client_context.__exit__(None, None, None)
        self.assertEqual(0, self.store.closed)
        self.assertEqual(0, self.coordinator.closed)
        self.assertEqual(1, self.outbox.stops)
        self.client_context = SimpleNamespace(__exit__=lambda *_: None)


class ClientProductionConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ca_path = self.root / "coordinator-ca.pem"
        self.ca_path.write_text("test CA fixture", encoding="utf-8")
        self.public_key = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, base_url):
        path = self.root / "client-config.json"
        path.write_text(json.dumps({
            "coordinator_base_url": base_url,
            "trusted_ca_path": str(self.ca_path),
            "coordinator_signing_public_key_b64": self.public_key,
        }), encoding="utf-8")
        return path

    def test_two_distinct_https_coordinator_urls_are_loaded_and_normalized(self):
        from client_app import _load_config

        first = _load_config(self._write("https://ksat-one.example.edu:8443/"))
        second = _load_config(self._write("https://10.20.30.40:9443"))
        self.assertEqual(
            "https://ksat-one.example.edu:8443", first["coordinator_base_url"]
        )
        self.assertEqual(
            "https://10.20.30.40:9443", second["coordinator_base_url"]
        )
        self.assertNotEqual(first["coordinator_base_url"], second["coordinator_base_url"])

    def test_insecure_credentialed_query_and_malformed_hosts_are_rejected(self):
        from client_app import _load_config

        invalid = (
            "http://ksat.example.edu:8443",
            "https://student:password@ksat.example.edu:8443",
            "https://ksat.example.edu:8443?redirect=evil",
            "https://ksat.example.edu:8443/#fragment",
            "https://bad host.example:8443",
            "https://ksat.example.edu:70000",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Client configuration is invalid"):
                    _load_config(self._write(value))

    def test_missing_config_returns_stable_problem_without_creating_identity(self):
        from client_app import create_client_app

        program_data = self.root / "empty-program-data"
        program_data.mkdir()
        with patch.dict(os.environ, {"ProgramData": str(program_data)}):
            with patch("client_app.DeviceIdentityStore") as identity_type:
                with TestClient(
                    create_client_app(), base_url="http://127.0.0.1:8010"
                ) as client:
                    response = client.get(
                        "/api/state", headers={"Host": "127.0.0.1:8010"}
                    )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "client_configuration_invalid", response.json()["problem"]["code"]
        )
        self.assertEqual(
            "The requested action could not be completed.",
            response.json()["problem"]["message"],
        )
        identity_type.assert_not_called()

    def test_initial_save_and_update_survive_strict_restart_read(self):
        from client_app import ClientConfig, ClientConfigStore

        path = self.root / "persistent" / "client-config.json"
        store = ClientConfigStore(path)
        initial = ClientConfig(
            coordinator_base_url="https://ksat-one.example.edu:8443",
            trusted_ca_path=str(self.ca_path),
            coordinator_signing_public_key_b64=self.public_key,
        )
        store.save(initial)
        self.assertEqual(initial, ClientConfigStore(path).load())
        updated = store.update_base_url("https://ksat-two.example.edu:9443/")
        self.assertEqual(
            "https://ksat-two.example.edu:9443", updated.coordinator_base_url
        )
        self.assertEqual(updated, ClientConfigStore(path).load())

    def test_atomic_update_failure_preserves_previous_configuration(self):
        from client_app import ClientConfig, ClientConfigStore

        path = self.root / "atomic" / "client-config.json"
        store = ClientConfigStore(path)
        original = ClientConfig(
            coordinator_base_url="https://ksat-one.example.edu:8443",
            trusted_ca_path=str(self.ca_path),
            coordinator_signing_public_key_b64=self.public_key,
        )
        store.save(original)
        with patch("client_app.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store.update_base_url("https://ksat-two.example.edu:9443")
        self.assertEqual(original, ClientConfigStore(path).load())

    def test_post_publish_sync_failure_rolls_back_previous_configuration(self):
        from client_app import ClientConfig, ClientConfigStore

        path = self.root / "post-sync" / "client-config.json"
        store = ClientConfigStore(path)
        original = ClientConfig(
            coordinator_base_url="https://ksat-one.example.edu:8443",
            trusted_ca_path=str(self.ca_path),
            coordinator_signing_public_key_b64=self.public_key,
        )
        store.save(original)
        real_fsync = os.fsync
        calls = 0

        def fail_second(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("post-publish sync failure")
            return real_fsync(descriptor)

        with patch("client_app.os.fsync", side_effect=fail_second):
            with self.assertRaises(OSError):
                store.update_base_url("https://ksat-two.example.edu:9443")
        self.assertEqual(original, ClientConfigStore(path).load())


class ClientOwnedLifecycleTests(unittest.TestCase):
    def test_partial_startup_failure_closes_each_owned_resource_once(self):
        from client_app import ClientServices, create_client_app

        identity = FakeIdentityStore()
        snapshot = FakeSnapshot()
        store = FakeStore(snapshot)
        runtime = FakeRuntime(store)
        coordinator = FakeCoordinator()

        class FailingOutbox(FakeOutbox):
            def start(self):
                super().start()
                raise OSError("thread unavailable")

        outbox = FailingOutbox()
        services = ClientServices(
            identity, store, runtime, coordinator, outbox, owns_resources=True
        )
        with TestClient(
            create_client_app(services), base_url="http://127.0.0.1:8010"
        ) as client:
            state = client.get(
                "/api/state", headers={"Host": "127.0.0.1:8010"}
            ).json()
            self.assertEqual("client_startup_failed", state["problem"]["code"])
        self.assertEqual(1, store.closed)
        self.assertEqual(1, coordinator.closed)
        self.assertEqual(1, outbox.stops)

    def test_sqlite_recovery_corruption_requires_intervention_without_starting_outbox(self):
        from client_app import ClientServices, create_client_app

        identity = FakeIdentityStore()
        store = FakeStore(FakeSnapshot())
        runtime = FakeRuntime(store, recovery_error=sqlite3.DatabaseError("corrupt page"))
        coordinator = FakeCoordinator()
        outbox = FakeOutbox()
        services = ClientServices(identity, store, runtime, coordinator, outbox)
        with TestClient(
            create_client_app(services), base_url="http://127.0.0.1:8010"
        ) as client:
            state = client.get(
                "/api/state", headers={"Host": "127.0.0.1:8010"}
            ).json()
        self.assertEqual("corrupt_local_attempt", state["problem"]["code"])
        self.assertEqual(0, outbox.starts)

    def test_production_factory_closes_transport_when_runtime_construction_fails(self):
        from client_app import _load_production_services

        with tempfile.TemporaryDirectory() as directory:
            program_data = Path(directory)
            data_dir = program_data / "KSAT Client"
            data_dir.mkdir()
            ca_path = data_dir / "coordinator-ca.pem"
            ca_path.write_text("test CA fixture", encoding="utf-8")
            public_key = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="
            (data_dir / "client-config.json").write_text(json.dumps({
                "coordinator_base_url": "https://ksat.example.edu:8443",
                "trusted_ca_path": str(ca_path),
                "coordinator_signing_public_key_b64": public_key,
            }), encoding="utf-8")
            identity = SimpleNamespace(
                device_id="44444444-4444-4444-8444-444444444444",
                coordinator_public_key_b64=public_key,
            )
            identity_store = SimpleNamespace(load_or_create=lambda: identity)
            store = FakeStore(None)
            coordinator = FakeCoordinator()
            with patch.dict(os.environ, {"ProgramData": str(program_data)}), patch(
                "client_app.DeviceIdentityStore", return_value=identity_store
            ), patch("client_app.ClientStore", return_value=store), patch(
                "client_app.CoordinatorClient", return_value=coordinator
            ), patch(
                "client_app.AssessmentRuntime", side_effect=OSError("runtime failure")
            ):
                with self.assertRaisesRegex(OSError, "runtime failure"):
                    _load_production_services()
            self.assertEqual(1, coordinator.closed)
            self.assertEqual(1, store.closed)


class ClientPrefetchLifecycleTests(unittest.TestCase):
    @staticmethod
    def _entry(release_id):
        descriptor = SimpleNamespace(
            release_id=release_id,
            content_hash="a" * 64,
            content_pack_filename=f"{release_id}.ksatpack",
        )
        return SimpleNamespace(
            release_id=release_id,
            filename=f"{release_id}.ksatpack",
            content_hash="a" * 64,
            descriptor=descriptor,
        )

    def test_blocking_prefetch_shutdown_waits_before_closing_and_leaks_no_thread(self):
        from client_app import ClientServices, create_client_app

        entered = threading.Event()
        release = threading.Event()
        coordinator = FakeCoordinator()

        def blocking_catalog():
            entered.set()
            self.assertTrue(release.wait(5))
            return []

        coordinator.prefetch_catalog = blocking_catalog
        store = FakeStore(None)
        services = ClientServices(
            FakeIdentityStore(), store, FakeRuntime(store), coordinator,
            FakeOutbox(), owns_resources=True, background_prefetch=True,
        )
        context = create_client_app(services).state.client_context
        context.initialize()
        self.assertTrue(entered.wait(2))
        shutdown_started = threading.Event()

        def shutdown():
            shutdown_started.set()
            context.shutdown()

        worker = threading.Thread(target=shutdown)
        worker.start()
        self.assertTrue(shutdown_started.wait(1))
        worker.join(0.1)
        self.assertTrue(worker.is_alive())
        self.assertEqual(0, coordinator.closed)
        self.assertEqual(0, store.closed)
        release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, coordinator.closed)
        self.assertEqual(1, store.closed)
        self.assertIsNotNone(context._prefetch_thread)
        self.assertFalse(context._prefetch_thread.is_alive())

    def test_prefetch_timeout_retains_owned_resources_until_worker_quiesces(self):
        from client_app import ClientServices, create_client_app

        entered = threading.Event()
        release = threading.Event()
        coordinator = FakeCoordinator()
        coordinator.request_timeout_seconds = 0.05

        def blocking_catalog():
            entered.set()
            release.wait(5)
            return []

        coordinator.prefetch_catalog = blocking_catalog
        store = FakeStore(None)
        services = ClientServices(
            FakeIdentityStore(), store, FakeRuntime(store), coordinator,
            FakeOutbox(), owns_resources=True, background_prefetch=True,
        )
        context = create_client_app(services).state.client_context
        context.initialize()
        self.assertTrue(entered.wait(2))
        context.shutdown()
        self.assertEqual(0, coordinator.closed)
        self.assertEqual(0, store.closed)
        self.assertTrue(context._prefetch_thread.is_alive())
        release.set()
        context._prefetch_thread.join(2)
        self.assertFalse(context._prefetch_thread.is_alive())
        context.shutdown()
        self.assertEqual(1, coordinator.closed)
        self.assertEqual(1, store.closed)

    def test_cancellation_between_catalog_entries_stops_before_prepare_or_next_entry(self):
        from client_app import ClientApiProblem, ClientServices, create_client_app

        first = self._entry(RELEASE_ID)
        second = self._entry("66666666-6666-4666-8666-666666666666")
        coordinator = FakeCoordinator()
        coordinator.catalog = [first, second]
        store = FakeStore(None)
        runtime = FakeRuntime(store)
        services = ClientServices(
            FakeIdentityStore(), store, runtime, coordinator, FakeOutbox()
        )
        context = create_client_app(services).state.client_context
        context.initialize()
        original_download = coordinator.download_pack

        def cancel_after_download(entry, path):
            result = original_download(entry, path)
            context._prefetch_stop.set()
            return result

        coordinator.download_pack = cancel_after_download
        with self.assertRaises(ClientApiProblem) as raised:
            context.prefetch()
        self.assertEqual("prefetch_cancelled", raised.exception.code)
        downloads = [call for call in coordinator.calls if call[0] == "download"]
        self.assertEqual(1, len(downloads))
        self.assertIs(first, downloads[0][1])
        self.assertEqual([], runtime.prepare_calls)
        self.assertEqual(set(), context.ready_releases)
        context.shutdown()

    def test_url_update_waits_for_old_prefetch_and_never_mixes_generations(self):
        from client_app import ClientServices, create_client_app

        entered = threading.Event()
        release = threading.Event()
        old_entry = self._entry(RELEASE_ID)
        coordinator = FakeCoordinator()
        coordinator.catalog = [old_entry]
        original_download = coordinator.download_pack

        def blocking_download(entry, path):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_download(entry, path)

        coordinator.download_pack = blocking_download
        store = FakeStore(None)
        runtime = FakeRuntime(store)
        old_outbox = FakeOutbox()
        services = ClientServices(
            FakeIdentityStore(), store, runtime, coordinator, old_outbox,
            cache_dir=Path(tempfile.mkdtemp()) / "packs",
            background_prefetch=True,
        )
        saved = []
        candidate_created = threading.Event()
        candidates = []
        replacement_outboxes = []
        services.config_store = SimpleNamespace(
            update_base_url=lambda value: saved.append(value)
        )

        def coordinator_factory(value):
            candidate_created.set()
            candidate = FakeCoordinator()
            candidate.base_url = value
            candidate.catalog = []
            candidates.append(candidate)
            return candidate

        def outbox_factory(candidate):
            worker = FakeOutbox()
            worker.coordinator = candidate
            replacement_outboxes.append(worker)
            return worker

        services.coordinator_factory = coordinator_factory
        services.outbox_factory = outbox_factory
        app = create_client_app(services)
        client_context = TestClient(app, base_url="http://127.0.0.1:8010")
        client = client_context.__enter__()
        self.assertTrue(entered.wait(2))
        page = client.get("/", headers={"Host": "127.0.0.1:8010"})
        token = re.search(
            r'<meta name="ksat-csrf" content="([A-Za-z0-9_-]+)">', page.text
        ).group(1)
        result = {}

        def update():
            result["response"] = client.post(
                "/api/device/coordinator",
                json={
                    "base_url": "https://new.example.edu:9443",
                    "confirmed": True,
                },
                headers={
                    "Host": "127.0.0.1:8010",
                    "Origin": "http://127.0.0.1:8010",
                    "X-KSAT-CSRF": token,
                },
            )

        update_thread = threading.Thread(target=update)
        update_thread.start()
        candidate_started_while_old_prefetch_blocked = candidate_created.wait(0.2)
        release.set()
        update_thread.join(5)
        client_context.__exit__(None, None, None)
        self.assertFalse(candidate_started_while_old_prefetch_blocked)
        self.assertFalse(update_thread.is_alive())
        self.assertEqual(200, result["response"].status_code)
        self.assertEqual(["https://new.example.edu:9443"], saved)
        self.assertIs(services.coordinator, candidates[0])
        self.assertEqual([], runtime.prepare_calls)
        self.assertNotIn(RELEASE_ID, app.state.client_context.ready_releases)
        self.assertEqual(1, replacement_outboxes[0].wakes)

    def test_url_update_timeout_keeps_old_generation_and_configuration(self):
        from client_app import ClientServices, create_client_app

        entered = threading.Event()
        release = threading.Event()
        coordinator = FakeCoordinator()
        coordinator.request_timeout_seconds = 0.05

        def blocking_catalog():
            entered.set()
            release.wait(5)
            return []

        coordinator.prefetch_catalog = blocking_catalog
        store = FakeStore(None)
        services = ClientServices(
            FakeIdentityStore(), store, FakeRuntime(store), coordinator,
            FakeOutbox(), background_prefetch=True,
        )
        saved = []
        candidates = []
        services.config_store = SimpleNamespace(
            update_base_url=lambda value: saved.append(value)
        )
        services.coordinator_factory = lambda value: candidates.append(value)
        services.outbox_factory = lambda candidate: FakeOutbox()
        app = create_client_app(services)
        client_context = TestClient(app, base_url="http://127.0.0.1:8010")
        client = client_context.__enter__()
        self.assertTrue(entered.wait(2))
        page = client.get("/", headers={"Host": "127.0.0.1:8010"})
        token = re.search(
            r'<meta name="ksat-csrf" content="([A-Za-z0-9_-]+)">', page.text
        ).group(1)
        response = client.post(
            "/api/device/coordinator",
            json={"base_url": "https://new.example.edu:9443", "confirmed": True},
            headers={
                "Host": "127.0.0.1:8010",
                "Origin": "http://127.0.0.1:8010",
                "X-KSAT-CSRF": token,
            },
        )
        self.assertEqual(503, response.status_code)
        self.assertEqual("prefetch_busy", response.json()["problem"]["code"])
        self.assertEqual([], saved)
        self.assertEqual([], candidates)
        self.assertIs(services.coordinator, coordinator)
        self.assertEqual(0, coordinator.closed)
        release.set()
        app.state.client_context._prefetch_thread.join(2)
        client_context.__exit__(None, None, None)

class ClientCoordinatorReconfigurationTests(unittest.TestCase):
    def setUp(self):
        from client_app import ClientServices, create_client_app

        self.root = Path(tempfile.mkdtemp())
        self.identity_store = FakeIdentityStore()
        self.snapshot = None
        self.store = FakeStore(self.snapshot)
        self.runtime = FakeRuntime(self.store)
        self.coordinator = FakeCoordinator()
        self.coordinator._session = SimpleNamespace(
            student_id="S100", student_name="Student One", access_token="secret-bearer"
        )
        self.outbox = FakeOutbox()
        self.services = ClientServices(
            self.identity_store, self.store, self.runtime, self.coordinator,
            self.outbox, cache_dir=self.root / "packs",
        )
        self.config_updates = []
        self.config_store = SimpleNamespace(
            update_base_url=lambda value: self._save_url(value)
        )
        self.candidates = []
        self.new_outboxes = []
        self.services.config_store = self.config_store
        self.services.coordinator_factory = self._coordinator_factory
        self.services.outbox_factory = self._outbox_factory
        self.client_context = TestClient(
            create_client_app(self.services), base_url="http://127.0.0.1:8010"
        )
        self.client = self.client_context.__enter__()
        page = self.client.get("/", headers={"Host": "127.0.0.1:8010"})
        match = re.search(r'<meta name="ksat-csrf" content="([A-Za-z0-9_-]+)">', page.text)
        self.mutation_headers = {
            "Host": "127.0.0.1:8010",
            "Origin": "http://127.0.0.1:8010",
            "X-KSAT-CSRF": match.group(1),
        }

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def _save_url(self, value):
        self.config_updates.append(value)
        return SimpleNamespace(coordinator_base_url=value.rstrip("/"))

    def _coordinator_factory(self, value):
        candidate = FakeCoordinator()
        candidate.base_url = value
        candidate.catalog = []
        self.candidates.append(candidate)
        return candidate

    def _outbox_factory(self, coordinator):
        worker = FakeOutbox()
        worker.coordinator = coordinator
        self.new_outboxes.append(worker)
        return worker

    def test_safe_update_validates_candidate_clears_session_and_replaces_services(self):
        old = self.coordinator
        old.calls.clear()
        response = self.client.post(
            "/api/device/coordinator",
            json={"base_url": "https://ksat-new.example.edu:9443/", "confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual({"state": "login", "configuration": "updated"}, response.json())
        self.assertEqual(
            ["https://ksat-new.example.edu:9443"], self.config_updates
        )
        self.assertEqual([("catalog",)], self.candidates[0].calls)
        self.assertIs(self.services.coordinator, self.candidates[0])
        self.assertIs(self.services.outbox, self.new_outboxes[0])
        self.assertEqual(1, self.new_outboxes[0].starts)
        self.assertEqual(1, self.new_outboxes[0].wakes)
        self.assertIn(("logout",), old.calls)
        self.assertEqual(1, self.outbox.stops)
        self.assertEqual(1, self.identity_store.loads)
        self.assertNotIn("ksat-new", response.text)

    def test_invalid_update_does_not_mutate_configuration_or_services(self):
        old = self.services.coordinator
        response = self.client.post(
            "/api/device/coordinator",
            json={"base_url": "http://insecure.example", "confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("invalid_coordinator_configuration", response.json()["problem"]["code"])
        self.assertEqual([], self.config_updates)
        self.assertEqual([], self.candidates)
        self.assertIs(old, self.services.coordinator)

    def test_active_and_sealed_attempts_both_block_configuration_change(self):
        for state in ("in_progress", "sealed_pending"):
            with self.subTest(state=state):
                self.snapshot = self.store.snapshot = FakeSnapshot(state=state)
                response = self.client.post(
                    "/api/device/coordinator",
                    json={"base_url": "https://ksat-new.example.edu:9443", "confirmed": True},
                    headers=self.mutation_headers,
                )
                self.assertEqual(409, response.status_code)
                self.assertEqual("active_attempt_configuration_locked", response.json()["problem"]["code"])
                self.snapshot = self.store.snapshot = None
        self.assertEqual([], self.config_updates)
        self.assertEqual([], self.candidates)

    def test_candidate_connectivity_failure_preserves_session_and_old_services(self):
        from ksat.client.coordinator import CoordinatorProblem

        old = self.services.coordinator

        def factory(value):
            candidate = FakeCoordinator()
            candidate.base_url = value
            candidate.prefetch_catalog = lambda: (_ for _ in ()).throw(
                CoordinatorProblem(
                    "coordinator_unavailable", "private network detail", True,
                    status_code=503,
                )
            )
            self.candidates.append(candidate)
            return candidate

        self.services.coordinator_factory = factory
        response = self.client.post(
            "/api/device/coordinator",
            json={"base_url": "https://offline.example.edu:9443", "confirmed": True},
            headers=self.mutation_headers,
        )
        self.assertEqual(503, response.status_code)
        self.assertEqual("coordinator_unavailable", response.json()["problem"]["code"])
        self.assertEqual([], self.config_updates)
        self.assertIs(old, self.services.coordinator)
        self.assertIsNotNone(old.session)


if __name__ == "__main__":
    unittest.main()
