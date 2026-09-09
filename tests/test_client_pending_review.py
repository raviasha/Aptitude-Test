"""Close-authorized local review must not consume or modify the durable upload."""

import base64
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from client_app import ClientServices, create_client_app
from ksat.client.coordinator import CoordinatorProblem
from ksat.client.store import ClientStore
from ksat.crypto import encrypt_pack, sha256_hex
from ksat.protocol import ClientSession, FrozenReviewQuestion, ReviewContent, SubmissionReceipt, canonical_json
from tests import test_client_coordinator as transport_fixtures
from tests import test_client_runtime as runtime_fixtures
from tests.test_client_app_api import FakeCoordinator, FakeOutbox


class PendingReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixtures.ClientRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        item = self.fixture
        item.store.close()
        item.db_path = item.root / "protected.sqlite3"
        item.store = ClientStore(
            item.db_path, integrity_key=b"i" * 32,
            integrity_anchor_path=item.root / "protected.anchor",
        )
        item.manifest = item.manifest.model_copy(update={"pack_format_version": 2})
        self.review_key = b"r" * 32
        ciphertext = encrypt_pack(self.review_key, f"{item.release_id}:review:v1", canonical_json(
            ReviewContent(questions=[
                FrozenReviewQuestion(question_id=7, correct_answer="B", solution_steps=["Seven step"]),
                FrozenReviewQuestion(question_id=3, correct_answer="A", solution_steps=["Three step"]),
            ])
        ))
        item._write_pack(item.questions, extra_entries=(("review.json.enc", ciphertext),))
        item.summary = item._summary()
        item.start_response = item._start_response(
            order_seed_b64=base64.b64encode(bytes(32)).decode("ascii")
        )
        item.runtime = item._runtime()
        item._prepare_and_start()
        item.runtime.answer(7, "B")

    def seal(self):
        self.fixture.runtime.submit()
        self.bundle = self.fixture.store.pending_submissions()[0].bundle
        self.bundle_hash = sha256_hex(canonical_json(self.bundle))
        return self.bundle_hash

    def grant(self, **changes):
        item = self.fixture
        fields = dict(
            attempt_id=item.attempt_id, student_id=item.student_id,
            release_id=item.release_id, content_hash=item.summary.content_hash,
            content_key_b64=base64.b64encode(item.content_key).decode("ascii"),
            review_key_b64=base64.b64encode(self.review_key).decode("ascii"),
            bundle_hash=self.bundle_hash,
        )
        fields.update(changes)
        return SimpleNamespace(**fields)

    def client(self, grant_or_error):
        item = self.fixture
        coordinator = FakeCoordinator()
        coordinator.login(item.student_id, "password")

        def sealed_review(attempt_id, bundle_hash):
            self.assertEqual(item.attempt_id, attempt_id)
            self.assertEqual(self.bundle_hash, bundle_hash)
            if isinstance(grant_or_error, Exception):
                raise grant_or_error
            return grant_or_error

        coordinator.sealed_review = sealed_review
        services = ClientServices(
            SimpleNamespace(load_or_create=lambda: item.identity),
            item.store, item.runtime, coordinator, FakeOutbox(), cache_dir=item.root,
        )
        app = create_client_app(services)
        client = TestClient(app, base_url="http://127.0.0.1:8010")
        self.addCleanup(client.__exit__, None, None, None)
        client.__enter__()
        return client, coordinator

    def test_sealed_review_scores_shuffled_local_answers_without_receipt_or_outbox_mutation(self):
        self.seal()
        item = self.fixture
        before = item.store.pending_submissions()
        anchor_before = (item.root / "protected.anchor").read_bytes()
        payload = item.runtime.open_sealed_review(self.grant(), student_id=item.student_id)
        self.assertEqual([3, 7], [entry["question"]["question_id"] for entry in payload["questions"]])
        self.assertIsNone(payload["questions"][0]["selected_answer"])
        self.assertEqual("B", payload["questions"][1]["selected_answer"])
        self.assertEqual(["Three step"], payload["questions"][0]["solution_steps"])
        self.assertEqual({"score": 1, "total_questions": 2, "attempted": 1, "percentage": 50.0}, payload["local_result"])
        self.assertEqual("Placement Set", payload["test_name"])
        self.assertTrue(payload["upload_pending"])
        self.assertIsNone(payload["result"])
        self.assertEqual(before, item.store.pending_submissions())
        self.assertEqual(anchor_before, (item.root / "protected.anchor").read_bytes())
        self.assertEqual("sealed_pending", item.runtime.snapshot().state)

    def test_same_review_works_after_ack_with_original_bundle_hash_and_authoritative_receipt(self):
        self.seal()
        item = self.fixture
        receipt = SubmissionReceipt(
            attempt_id=item.attempt_id, accepted_at=runtime_fixtures.STARTED + timedelta(seconds=1),
            score=1, total_questions=2, attempted=1, percentage=50.0, violations=0,
        )
        item.store.acknowledge(item.attempt_id, receipt)
        self.assertEqual(self.bundle_hash, item.runtime.sealed_review_bundle_hash(item.attempt_id, student_id=item.student_id))
        payload = item.runtime.open_sealed_review(self.grant(), student_id=item.student_id)
        self.assertFalse(payload["upload_pending"])
        self.assertEqual(receipt.model_dump(mode="json"), payload["result"])
        self.assertEqual([], item.store.pending_submissions())

    def test_editable_attempt_or_other_student_or_device_cannot_request_sealed_review(self):
        item = self.fixture
        with self.assertRaisesRegex(ValueError, "sealed"):
            item.runtime.sealed_review_bundle_hash(item.attempt_id, student_id=item.student_id)
        self.seal()
        with self.assertRaises(ValueError):
            item.runtime.sealed_review_bundle_hash(item.attempt_id, student_id="S200")
        item.runtime.identity = replace(item.identity, device_id="11111111-1111-4111-8111-111111111111")
        with self.assertRaises(ValueError):
            item.runtime.sealed_review_bundle_hash(item.attempt_id, student_id=item.student_id)

    def test_grant_must_match_owned_immutable_attempt_and_bundle(self):
        self.seal()
        for changes in (
            {"bundle_hash": "f" * 64}, {"content_hash": "f" * 64},
            {"student_id": "S200"},
            {"attempt_id": "11111111-1111-4111-8111-111111111111"},
            {"release_id": "11111111-1111-4111-8111-111111111111"},
            {"content_key_b64": base64.b64encode(b"z" * 32).decode("ascii")},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, KeyError)):
                self.fixture.runtime.open_sealed_review(self.grant(**changes), student_id=self.fixture.student_id)
        self.assertEqual(canonical_json(self.bundle), canonical_json(self.fixture.store.pending_submissions()[0].bundle))

    def test_protected_answer_tampering_is_rejected_before_requesting_keys(self):
        self.seal()
        item = self.fixture
        item.store.connection.execute(
            "UPDATE local_responses SET selected_answer='A' WHERE attempt_id=? AND question_id=7",
            (item.attempt_id,),
        )
        item.store.connection.commit()
        with self.assertRaises(ValueError):
            item.runtime.sealed_review_bundle_hash(item.attempt_id, student_id=item.student_id)

    def test_local_review_route_works_while_upload_is_pending(self):
        self.seal()
        client, _ = self.client(self.grant())
        response = client.get(f"/api/attempts/{self.fixture.attempt_id}/review")
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["upload_pending"])
        self.assertEqual(1, response.json()["local_result"]["score"])
        self.assertNotIn("key_b64", response.text)
        self.assertEqual(canonical_json(self.bundle), canonical_json(self.fixture.store.pending_submissions()[0].bundle))

    def test_before_close_denial_preserves_sealed_upload_and_exposes_no_answers(self):
        self.seal()
        client, _ = self.client(CoordinatorProblem("review_not_released", "Not closed", False, status_code=409))
        response = client.get(f"/api/attempts/{self.fixture.attempt_id}/review")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("review_not_released", response.json()["problem"]["code"])
        self.assertNotIn("correct_answer", response.text)
        self.assertEqual(1, len(self.fixture.store.pending_submissions()))

    def test_other_student_is_denied_locally_without_coordinator_request(self):
        self.seal()
        client, coordinator = self.client(self.grant())
        coordinator.login("S200", "password")
        coordinator.sealed_review = lambda *_: self.fail("An unrelated student requested review keys.")
        response = client.get(f"/api/attempts/{self.fixture.attempt_id}/review")
        self.assertEqual(403, response.status_code, response.text)

    def test_editable_attempt_is_denied_locally_without_requesting_keys(self):
        client, coordinator = self.client(None)
        coordinator.sealed_review = lambda *_: self.fail("Editable work requested review keys.")
        response = client.get(f"/api/attempts/{self.fixture.attempt_id}/review")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("review_not_sealed", response.json()["problem"]["code"])
        self.assertEqual([], self.fixture.store.pending_submissions())
        self.assertEqual("in_progress", self.fixture.runtime.snapshot().state)

    def test_slow_sealed_grant_leaves_local_state_responsive_and_logout_ordered(self):
        self.seal()
        client, coordinator = self.client(self.grant())
        entered, release = threading.Event(), threading.Event()
        original = coordinator.sealed_review

        def blocked_grant(attempt_id, bundle_hash):
            entered.set()
            release.wait(5)
            return original(attempt_id, bundle_hash)

        coordinator.sealed_review = blocked_grant
        with ThreadPoolExecutor(max_workers=3) as pool:
            review = pool.submit(client.get, f"/api/attempts/{self.fixture.attempt_id}/review")
            try:
                self.assertTrue(entered.wait(1))
                logout = pool.submit(
                    client.post, "/api/logout", json={"confirmed": True}, headers={
                        "Origin": "http://127.0.0.1:8010",
                        "X-KSAT-CSRF": client.app.state.client_context.csrf_token,
                    },
                )
                state = pool.submit(client.get, "/api/state").result(timeout=1)
                self.assertEqual("S100", state.json()["student"]["student_id"])
            finally:
                release.set()
            self.assertEqual(200, review.result(timeout=2).status_code)
            self.assertEqual(200, logout.result(timeout=2).status_code)
        self.assertIsNone(coordinator.session)
        self.assertEqual(401, client.get(f"/api/attempts/{self.fixture.attempt_id}/review").status_code)
        self.assertEqual(canonical_json(self.bundle), canonical_json(self.fixture.store.pending_submissions()[0].bundle))


class SealedReviewTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = transport_fixtures.CoordinatorClientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_sealed_review_sends_only_bound_hash_with_student_and_device_authentication(self):
        item = self.fixture
        attempt_id = "11111111-1111-4111-8111-111111111111"
        bundle_hash = "a" * 64
        replies = dict(
            attempt_id=attempt_id, student_id="S100", release_id=item.release_id,
            content_hash=item.content_hash, bundle_hash=bundle_hash,
            content_key_b64=base64.b64encode(b"c" * 32).decode("ascii"),
            review_key_b64=base64.b64encode(b"r" * 32).decode("ascii"),
        )

        def handler(request):
            item.assert_device_proof(request, set(), bearer="student-token")
            self.assertEqual("POST", request.method)
            self.assertEqual(f"/api/client/v1/reviews/{attempt_id}/sealed", request.url.path)
            self.assertEqual({"bundle_hash": bundle_hash}, json.loads(request.content))
            return httpx.Response(200, json=replies)

        client = item.make_client(handler)
        self.addCleanup(client.close)
        client._session = ClientSession(
            student_id="S100", student_name="Student", access_token="student-token",
            device_id=item.device_id, expires_in_seconds=300,
        )
        grant = client.sealed_review(attempt_id, bundle_hash)
        self.assertEqual(bundle_hash, grant.bundle_hash)
        for changed in ({"bundle_hash": "b" * 64}, {"student_id": "S200"}, {"attempt_id": item.release_id}):
            original = dict(replies)
            replies.update(changed)
            with self.subTest(changed=changed), self.assertRaises(CoordinatorProblem):
                client.sealed_review(attempt_id, bundle_hash)
            replies.clear()
            replies.update(original)
