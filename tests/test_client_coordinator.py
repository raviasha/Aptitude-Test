import base64
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ksat.client.identity import DeviceIdentity
from ksat.crypto import generate_ed25519_keypair, sign_json
from ksat.protocol import (
    ClientSession,
    AttemptStartResponse,
    AttemptTicket,
    PublicReleaseDescriptor,
    ReleaseManifest,
    SignedAttemptTicket,
    SubmissionReceipt,
    device_request_bytes,
)


class FakeIdentityStore:
    def __init__(self, identity):
        self.identity = identity
        self.saved = []

    def load_or_create(self):
        return self.identity

    def save_enrollment(self, device_id, coordinator_public_key_b64):
        self.saved.append((device_id, coordinator_public_key_b64))
        self.identity = DeviceIdentity(
            self.identity.private_key_b64,
            self.identity.public_key_b64,
            device_id,
            coordinator_public_key_b64,
        )
        return self.identity


class CoordinatorClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.device_private, self.device_public = generate_ed25519_keypair()
        self.coordinator_private, self.coordinator_public = generate_ed25519_keypair()
        self.device_id = str(uuid.uuid4())
        self.identity = DeviceIdentity(
            self.device_private,
            self.device_public,
            self.device_id,
            self.coordinator_public,
        )
        self.release_id = str(uuid.uuid4())
        self.pack = b"encrypted assessment pack"
        self.content_hash = hashlib.sha256(self.pack).hexdigest()
        self.manifest = ReleaseManifest(
            pack_format_version=1,
            release_id=self.release_id,
            test_id=7,
            test_name="Aptitude",
            duration_seconds=1800,
            canonical_question_ids=[11, 12],
        )
        signature = sign_json(self.coordinator_private, {
            "release_id": self.release_id,
            "content_hash": self.content_hash,
            "manifest": self.manifest.model_dump(mode="json"),
        })
        self.descriptor = PublicReleaseDescriptor(
            release_id=self.release_id,
            test_id=7,
            state="prepared",
            duration_seconds=1800,
            canonical_question_ids=[11, 12],
            content_pack_filename=f"{self.release_id}.ksatpack",
            content_hash=self.content_hash,
            content_signature_b64=signature,
            manifest=self.manifest,
        )

    def tearDown(self):
        self.temp.cleanup()

    def make_client(self, handler, identity=None):
        from ksat.client.coordinator import CoordinatorClient

        return CoordinatorClient(
            "http://coordinator.test",
            self.directory / "unused-ca.pem",
            identity or self.identity,
            transport=httpx.MockTransport(handler),
        )

    def catalog_body(self, **changes):
        entry = {
            "release_id": self.release_id,
            "filename": f"{self.release_id}.ksatpack",
            "content_hash": self.content_hash,
            "pack_signature_b64": self.descriptor.content_signature_b64,
            "byte_size": len(self.pack),
            "pack_format_version": 1,
            "descriptor": self.descriptor.model_dump(mode="json"),
        }
        entry.update(changes)
        return {"releases": [entry]}

    def assert_device_proof(self, request, seen_nonces, *, bearer=None):
        self.assertEqual(self.device_id, request.headers["X-KSAT-Device"])
        nonce = request.headers["X-KSAT-Nonce"]
        self.assertNotIn(nonce, seen_nonces)
        seen_nonces.add(nonce)
        uuid.UUID(nonce)
        timestamp = request.headers["X-KSAT-Timestamp"]
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.utcoffset())
        signature = base64.b64decode(request.headers["X-KSAT-Signature"], validate=True)
        public = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(self.device_private)
        ).public_key()
        public.verify(
            signature,
            device_request_bytes(
                request.method,
                request.url.path,
                request.content,
                timestamp,
                nonce,
            ),
        )
        if bearer is None:
            self.assertNotIn("Authorization", request.headers)
        else:
            self.assertEqual(f"Bearer {bearer}", request.headers["Authorization"])

    def test_enrollment_is_unsigned_and_persists_receipt_binding(self):
        unenrolled = DeviceIdentity(self.device_private, self.device_public)
        store = FakeIdentityStore(unenrolled)

        def handler(request):
            self.assertEqual("/api/client/v1/devices/enroll", request.url.path)
            self.assertNotIn("X-KSAT-Signature", request.headers)
            self.assertEqual({
                "label": "Lab 01",
                "public_key_b64": self.device_public,
            }, json.loads(request.content))
            return httpx.Response(200, json={
                "device_id": self.device_id,
                "coordinator_public_key_b64": self.coordinator_public,
            })

        receipt = self.make_client(handler, store).enroll("Lab 01")
        self.assertEqual(self.device_id, receipt.device_id)
        self.assertEqual([(self.device_id, self.coordinator_public)], store.saved)

    def test_student_registration_is_device_signed_and_returns_normalized_identity(self):
        seen = set()

        def handler(request):
            self.assertEqual("/api/client/v1/students/register", request.url.path)
            self.assert_device_proof(request, seen)
            self.assertEqual({
                "student_id": "1ks26ai007",
                "name": "New Student",
                "student_class": "AIML",
                "section": "B",
                "password": "new-password",
            }, json.loads(request.content))
            return httpx.Response(201, json={
                "student_id": "1KS26AI007",
                "name": "New Student",
            })

        registered = self.make_client(handler).register_student(
            student_id="1ks26ai007",
            name="New Student",
            student_class="AIML",
            section="B",
            password="new-password",
        )

        self.assertEqual("1KS26AI007", registered.student_id)
        self.assertEqual("New Student", registered.name)
        self.assertEqual(1, len(seen))

    def test_completed_reviews_and_grant_are_typed_and_authenticated(self):
        attempt_id = str(uuid.uuid4())
        seen_nonces = set()
        token = "student-token"

        def handler(request):
            self.assert_device_proof(request, seen_nonces, bearer=token)
            if request.url.path == "/api/client/v1/reviews":
                return httpx.Response(200, json=[{
                    "attempt_id": attempt_id,
                    "release_id": self.release_id,
                    "test_id": 7,
                    "test_name": "Aptitude",
                    "accepted_at": "2026-09-05T09:00:00Z",
                    "score": 1,
                    "total_questions": 2,
                    "percentage": 50.0,
                    "review_state": "available",
                }])
            self.assertEqual(f"/api/client/v1/reviews/{attempt_id}", request.url.path)
            return httpx.Response(200, json={
                "attempt_id": attempt_id,
                "student_id": "S100",
                "release_id": self.release_id,
                "content_hash": self.content_hash,
                "content_key_b64": base64.b64encode(b"c" * 32).decode("ascii"),
                "review_key_b64": base64.b64encode(b"r" * 32).decode("ascii"),
                "responses": [
                    {"question_id": 7, "question_order": 0, "selected_answer": "B"},
                    {"question_id": 3, "question_order": 1, "selected_answer": None},
                ],
            })

        client = self.make_client(handler)
        client._session = ClientSession(
            access_token=token,
            student_id="S100",
            student_name="Student",
            device_id=self.device_id,
            expires_in_seconds=300,
        )
        self.assertEqual(attempt_id, client.completed_reviews()[0].attempt_id)
        self.assertEqual([7, 3], [item.question_id for item in client.review(attempt_id).responses])

    def test_already_enrolled_identity_is_not_sent_for_reenrollment(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.fail("Re-enrollment must be rejected before transport.")

        client = self.make_client(handler)
        with self.assertRaisesRegex(Exception, "already enrolled"):
            client.enroll("Lab 01", "new-code")
        self.assertEqual([], requests)

    def test_build_probe_is_real_unsigned_request_and_exact_version(self):
        def handler(request):
            self.assertEqual("/api/build", request.url.path)
            self.assertNotIn("X-KSAT-Signature", request.headers)
            return httpx.Response(200, json={"version": "2.0.0"})

        self.assertEqual("2.0.0", self.make_client(handler).probe_build())
        with self.assertRaisesRegex(Exception, "incompatible build"):
            self.make_client(
                lambda _request: httpx.Response(200, json={"version": "1.3.3"})
            ).probe_build()

    def test_login_signs_exact_transmitted_bytes_and_failed_login_clears_old_token(self):
        seen = set()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            self.assert_device_proof(request, seen)
            if calls == 1:
                return httpx.Response(200, json={
                    "access_token": "memory-token",
                    "student_id": "S100",
                    "student_name": "Student",
                    "device_id": self.device_id,
                    "expires_in_seconds": 43200,
                })
            return httpx.Response(401, json={"detail": {
                "code": "invalid_credentials",
                "message": "The student credentials are invalid.",
                "retryable": False,
            }})

        client = self.make_client(handler)
        client.login("S100", "secret")
        with self.assertRaises(Exception):
            client.login("S100", "wrong")
        self.assertIsNone(client.session)

    def test_login_normalizes_requested_student_id_to_server_contract(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual("S100", payload["student_id"])
            return httpx.Response(200, json={
                "access_token": "memory-token", "student_id": "S100",
                "student_name": "Student", "device_id": self.device_id,
                "expires_in_seconds": 43200,
            })

        session = self.make_client(handler).login("  s100  ", "secret")
        self.assertEqual("S100", session.student_id)

    def test_mismatched_login_response_clears_previous_session_and_token(self):
        from ksat.client.coordinator import CoordinatorProblem

        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            returned_student = "S100" if calls == 1 else "S999"
            return httpx.Response(200, json={
                "access_token": f"token-{calls}", "student_id": returned_student,
                "student_name": "Student", "device_id": self.device_id,
                "expires_in_seconds": 43200,
            })

        client = self.make_client(handler)
        self.assertEqual("token-1", client.login("S100", "secret").access_token)
        with self.assertRaises(CoordinatorProblem) as caught:
            client.login(" s100 ", "replacement")
        self.assertEqual("invalid_coordinator_response", caught.exception.code)
        self.assertFalse(caught.exception.retryable)
        self.assertIsNone(client.session)
        with self.assertRaises(CoordinatorProblem) as missing:
            client.assessments()
        self.assertEqual("client_session_required", missing.exception.code)
        self.assertEqual(2, calls)

    def test_bearer_is_scoped_only_to_assessments_and_start_not_submission_or_catalog(self):
        seen = set()
        ticket = AttemptTicket(
            attempt_id=str(uuid.uuid4()), student_id="S100", device_id=self.device_id,
            release_id=self.release_id, content_hash=self.content_hash,
            started_at=datetime.now(timezone.utc),
            deadline=datetime.now(timezone.utc) + timedelta(minutes=30),
            order_seed_b64=base64.b64encode(b"o" * 32).decode(),
            content_key_b64=base64.b64encode(b"k" * 32).decode(),
        )
        signed = SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(self.coordinator_private, ticket))
        start = AttemptStartResponse(ticket=signed, canonical_question_ids=[11, 12], server_time=ticket.started_at)
        receipt = SubmissionReceipt(
            attempt_id=ticket.attempt_id, accepted_at=ticket.deadline, score=0,
            total_questions=2, attempted=0, percentage=0.0, violations=0,
        )

        def handler(request):
            bearer = "memory-token" if request.url.path in {
                "/api/client/v1/assessments", "/api/client/v1/attempts/start"
            } else None
            self.assert_device_proof(request, seen, bearer=bearer)
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json={
                    "access_token": "memory-token", "student_id": "S100",
                    "student_name": "Student", "device_id": self.device_id,
                    "expires_in_seconds": 43200,
                })
            if request.url.path.endswith("/releases"):
                return httpx.Response(200, json=self.catalog_body())
            if request.url.path.endswith("/assessments"):
                return httpx.Response(200, json={"assessments": []})
            if request.url.path.endswith("/start"):
                return httpx.Response(200, content=start.model_dump_json().encode())
            return httpx.Response(200, content=receipt.model_dump_json().encode())

        client = self.make_client(handler)
        client.login("S100", "secret")
        client.prefetch_catalog()
        client.assessments()
        client.start_attempt(self.release_id, self.content_hash)
        # A minimal invalid bundle never reaches transport parsing; use a model-shaped copy.
        from ksat.protocol import ResponseBundle, ResponseEntry, SignedResponseBundle
        raw = ResponseBundle(
            ticket=signed, content_hash=self.content_hash, sealed_at=ticket.deadline,
            responses=[ResponseEntry(question_id=11, selected_answer=None), ResponseEntry(question_id=12, selected_answer=None)],
        )
        bundle = SignedResponseBundle(bundle=raw, device_signature_b64=sign_json(self.device_private, raw))
        client.submit_bundle(bundle)
        client.logout()
        self.assertIsNone(client.session)

    def test_catalog_strictly_links_legacy_fields_and_preserves_nested_descriptor(self):
        client = self.make_client(lambda request: httpx.Response(200, json=self.catalog_body()))
        entries = client.prefetch_catalog()
        self.assertEqual(self.descriptor, entries[0].descriptor)
        self.assertEqual(len(self.pack), entries[0].byte_size)
        self.assertIs(entries[0].descriptor.manifest.__class__, ReleaseManifest)

        bad = self.catalog_body(content_hash="f" * 64)
        with self.assertRaisesRegex(Exception, "catalog"):
            self.make_client(lambda request: httpx.Response(200, json=bad)).prefetch_catalog()

    def test_assessments_and_start_are_strict_typed_responses(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        attempt_id = str(uuid.uuid4())
        assessment = {
            "release_id": self.release_id, "test_id": 7, "test_name": "Aptitude",
            "duration_seconds": 1800, "content_hash": self.content_hash,
            "launch_closes_at": now.isoformat(), "attempt_id": None,
            "attempt_deadline": None,
        }
        ticket = AttemptTicket(
            attempt_id=attempt_id, student_id="S100", device_id=self.device_id,
            release_id=self.release_id, content_hash=self.content_hash, started_at=now,
            deadline=now + timedelta(minutes=30), order_seed_b64=base64.b64encode(b"s" * 32).decode(),
            content_key_b64=base64.b64encode(b"k" * 32).decode(),
        )
        response = AttemptStartResponse(
            ticket=SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(self.coordinator_private, ticket)),
            canonical_question_ids=[11, 12], server_time=now,
        )

        def handler(request):
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json={
                    "access_token": "t", "student_id": "S100", "student_name": "Student",
                    "device_id": self.device_id, "expires_in_seconds": 43200,
                })
            if request.url.path.endswith("/assessments"):
                return httpx.Response(200, json={"assessments": [assessment]})
            return httpx.Response(200, content=response.model_dump_json().encode())

        client = self.make_client(handler)
        client.login("S100", "secret")
        self.assertEqual("Aptitude", client.assessments()[0].test_name)
        self.assertEqual(response, client.start_attempt(self.release_id, self.content_hash))

    def test_start_rejects_ticket_for_a_different_logged_in_student(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        ticket = AttemptTicket(
            attempt_id=str(uuid.uuid4()), student_id="S999", device_id=self.device_id,
            release_id=self.release_id, content_hash=self.content_hash, started_at=now,
            deadline=now + timedelta(minutes=30),
            order_seed_b64=base64.b64encode(b"s" * 32).decode(),
            content_key_b64=base64.b64encode(b"k" * 32).decode(),
        )
        response = AttemptStartResponse(
            ticket=SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(self.coordinator_private, ticket)),
            canonical_question_ids=[11, 12], server_time=now,
        )

        def handler(request):
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json={
                    "access_token": "t", "student_id": "S100", "student_name": "Student",
                    "device_id": self.device_id, "expires_in_seconds": 43200,
                })
            return httpx.Response(200, content=response.model_dump_json().encode())

        client = self.make_client(handler)
        client.login("S100", "secret")
        with self.assertRaisesRegex(Exception, "invalid response"):
            client.start_attempt(self.release_id, self.content_hash)

    def test_assessment_list_rejects_duplicate_release_rows(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        assessment = {
            "release_id": self.release_id, "test_id": 7, "test_name": "Aptitude",
            "duration_seconds": 1800, "content_hash": self.content_hash,
            "launch_closes_at": now.isoformat(), "attempt_id": None,
            "attempt_deadline": None,
        }

        def handler(request):
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json={
                    "access_token": "t", "student_id": "S100", "student_name": "Student",
                    "device_id": self.device_id, "expires_in_seconds": 43200,
                })
            return httpx.Response(200, json={"assessments": [assessment, assessment]})

        client = self.make_client(handler)
        client.login("S100", "secret")
        with self.assertRaisesRegex(Exception, "invalid response"):
            client.assessments()

    def test_assessment_list_rejects_coerced_numeric_wire_fields(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        assessment = {
            "release_id": self.release_id, "test_id": "7", "test_name": "Aptitude",
            "duration_seconds": 1800, "content_hash": self.content_hash,
            "launch_closes_at": now.isoformat(), "attempt_id": None,
            "attempt_deadline": None,
        }

        def handler(request):
            if request.url.path.endswith("/session"):
                return httpx.Response(200, json={
                    "access_token": "t", "student_id": "S100", "student_name": "Student",
                    "device_id": self.device_id, "expires_in_seconds": 43200,
                })
            return httpx.Response(200, json={"assessments": [assessment]})

        client = self.make_client(handler)
        client.login("S100", "secret")
        with self.assertRaisesRegex(Exception, "invalid response"):
            client.assessments()

    def test_structured_problem_preserves_message_and_bounded_retry_after(self):
        from ksat.client.coordinator import CoordinatorProblem

        response = httpx.Response(503, headers={"Retry-After": "999999"}, json={"detail": {
            "code": "submission_busy", "message": "Submission is queued.", "retryable": True,
        }})
        client = self.make_client(lambda request: response)
        with self.assertRaises(CoordinatorProblem) as caught:
            client.submit_bundle(self._dummy_bundle())
        self.assertEqual("submission_busy", caught.exception.code)
        self.assertEqual("Submission is queued.", caught.exception.message)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(300.0, caught.exception.retry_after)

    def test_authentication_error_cannot_be_made_retryable_by_server(self):
        from ksat.client.coordinator import CoordinatorProblem

        client = self.make_client(lambda request: httpx.Response(503, json={"detail": {
            "code": "invalid_bundle_signature", "message": "Signature invalid.", "retryable": True,
        }}))
        with self.assertRaises(CoordinatorProblem) as caught:
            client.submit_bundle(self._dummy_bundle())
        self.assertFalse(caught.exception.retryable)

    def test_submission_rejects_receipt_for_a_different_attempt(self):
        bundle = self._dummy_bundle()
        receipt = SubmissionReceipt(
            attempt_id=str(uuid.uuid4()), accepted_at=datetime.now(timezone.utc),
            score=0, total_questions=1, attempted=0, percentage=0.0, violations=0,
        )
        client = self.make_client(
            lambda request: httpx.Response(200, content=receipt.model_dump_json().encode())
        )
        with self.assertRaisesRegex(Exception, "invalid response"):
            client.submit_bundle(bundle)

    def test_malformed_error_and_success_fail_closed_with_stable_codes(self):
        from ksat.client.coordinator import CoordinatorProblem

        for response, code in (
            (httpx.Response(500, content=b"not-json"), "invalid_coordinator_error"),
            (httpx.Response(200, json={"unexpected": []}), "invalid_coordinator_response"),
        ):
            with self.subTest(code=code):
                client = self.make_client(lambda request, response=response: response)
                with self.assertRaises(CoordinatorProblem) as caught:
                    client.prefetch_catalog()
                self.assertEqual(code, caught.exception.code)
                if response.status_code == 500:
                    self.assertTrue(caught.exception.retryable)

    def test_every_signed_request_uses_a_fresh_nonce(self):
        nonces = []

        def handler(request):
            nonces.append(request.headers["X-KSAT-Nonce"])
            return httpx.Response(200, json=self.catalog_body())

        client = self.make_client(handler)
        client.prefetch_catalog()
        client.prefetch_catalog()
        self.assertEqual(2, len(set(nonces)))

    def test_production_transport_requires_https_real_ca_and_secure_client_options(self):
        from ksat.client.coordinator import CoordinatorClient

        with self.assertRaises(ValueError):
            CoordinatorClient("http://host:8443", self.directory / "ca.pem", self.identity)
        ca = self.directory / "coordinator-ca.pem"
        ca.write_text("placeholder")
        context = object()
        with patch("ksat.client.coordinator.ssl.create_default_context", return_value=context) as make_context, \
             patch("ksat.client.coordinator.httpx.Client") as make_http:
            CoordinatorClient("https://host:8443", ca, self.identity)
        make_context.assert_called_once_with(cafile=str(ca.resolve()))
        kwargs = make_http.call_args.kwargs
        self.assertIs(context, kwargs["verify"])
        self.assertFalse(kwargs["follow_redirects"])
        self.assertIsInstance(kwargs["timeout"], httpx.Timeout)

    def test_request_timeout_property_is_validated_immutable_and_matches_transport(self):
        from ksat.client.coordinator import CoordinatorClient

        default = self.make_client(lambda request: httpx.Response(200, json={}))
        self.assertEqual(10.0, default.request_timeout_seconds)
        custom = CoordinatorClient(
            "http://coordinator.test",
            self.directory / "unused-ca.pem",
            self.identity,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            timeout_seconds=2.75,
        )
        self.assertEqual(2.75, custom.request_timeout_seconds)
        self.assertEqual(2.75, custom._client.timeout.read)
        with self.assertRaises(AttributeError):
            custom.request_timeout_seconds = 99
        for invalid in (0, -1, 301, float("nan"), float("inf"), True, "10"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                CoordinatorClient(
                    "http://coordinator.test",
                    self.directory / "unused-ca.pem",
                    self.identity,
                    transport=httpx.MockTransport(lambda request: httpx.Response(200)),
                    timeout_seconds=invalid,
                )

    def test_redirect_is_rejected(self):
        from ksat.client.coordinator import CoordinatorProblem

        client = self.make_client(lambda request: httpx.Response(302, headers={"Location": "https://evil.test"}))
        with self.assertRaises(CoordinatorProblem) as caught:
            client.prefetch_catalog()
        self.assertEqual("coordinator_redirect_rejected", caught.exception.code)

    def test_download_streams_exact_size_hash_and_publishes_without_partial(self):
        # Populate the catalog's authenticated size before downloading.
        client = self.make_client(lambda request: (
            httpx.Response(200, json=self.catalog_body()) if request.url.path.endswith("/releases")
            else httpx.Response(200, headers={"Content-Length": str(len(self.pack))}, content=self.pack)
        ))
        catalog_entry = client.prefetch_catalog()[0]
        destination = self.directory / self.descriptor.content_pack_filename
        self.assertEqual(destination, client.download_pack(catalog_entry, destination))
        self.assertEqual(self.pack, destination.read_bytes())
        self.assertEqual([], list(self.directory.glob("*.partial")))

    def test_download_rejects_oversize_truncation_hash_and_atomic_collision(self):
        from ksat.client.coordinator import ContentVerificationError

        cases = [
            (self.pack + b"x", str(len(self.pack) + 1)),
            (self.pack[:-1], str(len(self.pack))),
            (b"x" * len(self.pack), str(len(self.pack))),
        ]
        for index, (body, length) in enumerate(cases):
            with self.subTest(index=index):
                def handler(request, body=body, length=length):
                    if request.url.path.endswith("/releases"):
                        return httpx.Response(200, json=self.catalog_body())
                    return httpx.Response(200, headers={"Content-Length": length}, content=body)
                client = self.make_client(handler)
                entry = client.prefetch_catalog()[0]
                destination = self.directory / f"bad-{index}.ksatpack"
                with self.assertRaises(ContentVerificationError):
                    client.download_pack(entry, destination)
                self.assertFalse(destination.exists())
                self.assertEqual([], list(self.directory.glob("*.partial")))

        destination = self.directory / "collision.ksatpack"
        destination.write_bytes(b"existing-mismatch")
        client = self.make_client(lambda request: (
            httpx.Response(200, json=self.catalog_body()) if request.url.path.endswith("/releases")
            else httpx.Response(200, headers={"Content-Length": str(len(self.pack))}, content=self.pack)
        ))
        entry = client.prefetch_catalog()[0]
        with self.assertRaises(ContentVerificationError):
            client.download_pack(entry, destination)
        self.assertEqual(b"existing-mismatch", destination.read_bytes())

    def test_download_rejects_traversal_and_symlink_destination(self):
        from ksat.client.coordinator import ContentVerificationError

        client = self.make_client(lambda request: httpx.Response(200, content=self.pack))
        with self.assertRaises(ContentVerificationError):
            client.download_pack(self.descriptor, self.directory / "nested" / ".." / "pack")
        if hasattr(os, "symlink"):
            target = self.directory / "target"
            target.write_bytes(b"target")
            link = self.directory / "link"
            try:
                link.symlink_to(target)
            except OSError:
                return
            with self.assertRaises(ContentVerificationError):
                client.download_pack(self.descriptor, link)

    def _dummy_bundle(self):
        from ksat.protocol import ResponseBundle, ResponseEntry, SignedResponseBundle

        now = datetime.now(timezone.utc)
        ticket = AttemptTicket(
            attempt_id=str(uuid.uuid4()), student_id="S100", device_id=self.device_id,
            release_id=self.release_id, content_hash=self.content_hash, started_at=now,
            deadline=now + timedelta(minutes=30), order_seed_b64=base64.b64encode(b"o" * 32).decode(),
            content_key_b64=base64.b64encode(b"k" * 32).decode(),
        )
        signed = SignedAttemptTicket(ticket=ticket, signature_b64=sign_json(self.coordinator_private, ticket))
        body = ResponseBundle(
            ticket=signed, content_hash=self.content_hash, sealed_at=now,
            responses=[ResponseEntry(question_id=11, selected_answer=None)],
        )
        return SignedResponseBundle(bundle=body, device_signature_b64=sign_json(self.device_private, body))


if __name__ == "__main__":
    unittest.main()
