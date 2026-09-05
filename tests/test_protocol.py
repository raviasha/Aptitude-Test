import base64
import time
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from ksat.protocol import (
    CURRENT_PACK_FORMAT_VERSION,
    AttemptDeadlineUpdate,
    AttemptTicket,
    FrozenReviewQuestion,
    MATH_FLOOR_DIVISION_ERROR,
    ReleaseManifest,
    ReviewContent,
    SUPPORTED_PACK_FORMAT_VERSIONS,
    SignedAttemptDeadlineUpdate,
    canonical_json,
    canonicalize_math_floor_division_markup,
    deterministic_question_order,
    device_request_bytes,
)


class ProtocolTests(unittest.TestCase):
    def test_review_contracts_are_strict_and_question_ids_are_unique(self):
        question = FrozenReviewQuestion(
            question_id=7,
            correct_answer="B",
            solution_steps=["Add the two values.", "The result is 4."],
        )
        self.assertEqual(7, ReviewContent(questions=[question]).questions[0].question_id)
        with self.assertRaises(ValidationError):
            ReviewContent(questions=[question, question])
        with self.assertRaises(ValidationError):
            FrozenReviewQuestion(question_id=7, correct_answer="Z", solution_steps=["step"])
        with self.assertRaises(ValidationError):
            FrozenReviewQuestion(question_id=7, correct_answer="B", solution_steps=[" "])

    def test_new_manifests_use_v2_while_v1_remains_supported(self):
        self.assertEqual(2, CURRENT_PACK_FORMAT_VERSION)
        self.assertEqual(frozenset({1, 2}), SUPPORTED_PACK_FORMAT_VERSIONS)
        manifest = ReleaseManifest(
            release_id="33333333-3333-4333-8333-333333333333",
            test_id=7,
            test_name="Aptitude",
            duration_seconds=120,
            canonical_question_ids=[3, 7],
        )
        self.assertEqual(2, manifest.pack_format_version)

    def test_unterminated_code_tokens_do_not_tail_scan_each_candidate(self):
        malformed = "<code" * 16_000
        started = time.perf_counter()
        with self.assertRaisesRegex(ValueError, MATH_FLOOR_DIVISION_ERROR):
            canonicalize_math_floor_division_markup(malformed)
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_unterminated_code_token_is_bounded_at_import_size_limit(self):
        malformed = "x" * (25 * 1024 * 1024 - len("<code")) + "<code"
        started = time.perf_counter()
        with self.assertRaisesRegex(ValueError, MATH_FLOOR_DIVISION_ERROR):
            canonicalize_math_floor_division_markup(malformed)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_unterminated_code_scan_has_linear_growth(self):
        durations = []
        for size in (2 * 1024 * 1024, 8 * 1024 * 1024):
            malformed = "x" * (size - len("<code")) + "<code"
            started = time.perf_counter()
            with self.assertRaises(ValueError):
                canonicalize_math_floor_division_markup(malformed)
            durations.append(time.perf_counter() - started)
        self.assertLess(durations[1], durations[0] * 5 + 0.05)

    def test_canonical_json_is_stable(self):
        left = canonical_json({"b": 2, "a": "é"})
        right = canonical_json({"a": "é", "b": 2})
        self.assertEqual(left, right)
        self.assertEqual(left, b'{"a":"\xc3\xa9","b":2}')

    def test_shuffle_is_reproducible_and_preserves_membership(self):
        question_ids = [11, 12, 13, 14, 15]
        seed = base64.b64encode(bytes(range(32))).decode("ascii")
        first = deterministic_question_order(question_ids, seed)
        second = deterministic_question_order(question_ids, seed)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(question_ids))
        self.assertNotEqual(first, question_ids)

    def test_ticket_never_contains_an_answer_key(self):
        ticket = AttemptTicket(
            attempt_id="attempt-1",
            student_id="S1",
            device_id="device-1",
            release_id="release-1",
            content_hash="a" * 64,
            started_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            deadline=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            order_seed_b64=base64.b64encode(b"x" * 32).decode("ascii"),
            content_key_b64=base64.b64encode(b"k" * 32).decode("ascii"),
        )
        serialized = canonical_json(ticket).lower()
        self.assertNotIn(b"correct", serialized)
        self.assertNotIn(b"solution", serialized)
        self.assertNotIn(b"duration_extension_seconds", serialized)
        self.assertEqual(ticket.duration_extension_seconds, 0)

    def test_deadline_update_is_a_strict_answer_free_signed_payload(self):
        self.assertIn("base_deadline", AttemptDeadlineUpdate.model_fields)
        update = AttemptDeadlineUpdate(
            attempt_id="attempt-1",
            release_id="release-1",
            device_id="device-1",
            base_deadline=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            prior_deadline=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            deadline=datetime(2026, 8, 31, 1, 5, tzinfo=timezone.utc),
            cumulative_extension_seconds=300,
            revision=1,
            issued_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        signed = SignedAttemptDeadlineUpdate(update=update, signature_b64="c2ln")
        serialized = canonical_json(signed).lower()
        self.assertNotIn(b"answer", serialized)
        self.assertNotIn(b"content_key", serialized)
        with self.assertRaises(ValidationError):
            AttemptDeadlineUpdate(**{**update.model_dump(), "revision": 0})

    def test_device_request_bytes_bind_all_request_components(self):
        request = device_request_bytes(
            "post", "/api/client/v1/attempts", b'{"answer":"A"}', "2026-08-31T00:00:00Z", "n-1"
        )
        self.assertEqual(
            request,
            b"POST\n/api/client/v1/attempts\n"
            b"ae174266a26b7a56a85c0665f563091caeb5bf79c86a24a8cd0cafb36a6c22dc\n"
            b"2026-08-31T00:00:00Z\nn-1",
        )

    def test_protocol_models_reject_undocumented_fields(self):
        with self.assertRaises(ValidationError):
            ReleaseManifest(
                release_id="release-1",
                test_id=1,
                test_name="Math",
                duration_seconds=600,
                canonical_question_ids=[1, 2],
                answer_key={1: "A"},
            )
