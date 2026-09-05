import base64
import hashlib
import html
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from ksat.coordinator.releases import (
    load_release_manifest,
    prepare_release,
    unwrap_release_content_key,
    unwrap_release_review_key,
    wrap_release_content_key,
)
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.crypto import decrypt_pack, encrypt_pack, generate_ed25519_keypair, sign_json, verify_json
from ksat.protocol import (
    FrozenReviewQuestion,
    MATH_FLOOR_DIVISION_ERROR,
    PublicQuestion,
    ReviewContent,
    canonical_json,
    canonicalize_math_floor_division_markup,
)
from ksat.sqlite import connect_sqlite


_MARKUP_DECODE_BOUND = 8


def nested_pseudo_math(family, rounds):
    pseudo = '<code class="math-floor-division">c // d</code>'
    if family == "control-separated":
        pseudo = (
            '<c\u200do\u200dd\u200de class="math-floor-\u200ddivision">c // d'
            '</c\u200do\u200dd\u200de>'
        )
        family = "entity"
    if family == "entity":
        encoded = "".join(f"&#{ord(character)};" for character in pseudo)
    elif family == "percent":
        encoded = "".join(f"%{byte:02X}" for byte in pseudo.encode("utf-8"))
    elif family == "alternating":
        parts = []
        for index, character in enumerate(pseudo):
            if index % 2:
                parts.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
            else:
                parts.append(f"&#{ord(character)};")
        encoded = "".join(parts)
    else:
        raise AssertionError(f"Unknown pseudo-markup family: {family}")
    for _ in range(rounds - 1):
        if family == "percent":
            encoded = encoded.replace("%", "%25")
        else:
            encoded = encoded.replace("&", "&amp;").replace("%", "&#37;")
    return encoded


def one_question_answer_key(bank_name="Decode boundary bank"):
    return json.dumps({
        "bank_name": bank_name,
        "questions": [{
            "key": "bad-q",
            "category": "Quantitative Aptitude",
            "chapter": "Arithmetic",
            "difficulty": "Easy",
            "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
            "correct_answer": "A",
        }],
    })


def pseudo_math_depth_matrix():
    valid_math = '<code class="math-floor-division">a // b</code>'
    cases = []
    for family in ("entity", "percent", "alternating", "control-separated"):
        for label, rounds in (
            ("below", _MARKUP_DECODE_BOUND - 1),
            ("exact", _MARKUP_DECODE_BOUND),
            ("one-over", _MARKUP_DECODE_BOUND + 1),
            ("deep", 64),
        ):
            cases.append((f"{family}-{label}", nested_pseudo_math(family, rounds)))
    for label, rounds in (
        ("below", _MARKUP_DECODE_BOUND - 1),
        ("exact", _MARKUP_DECODE_BOUND),
        ("one-over", _MARKUP_DECODE_BOUND + 1),
        ("deep", 64),
    ):
        cases.append((
            f"mixed-valid-and-pseudo-{label}",
            valid_math + "<p>" + nested_pseudo_math("entity", rounds) + "</p>",
        ))
    return tuple(cases)


class AssessmentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.pack_dir = Path(self.temporary_directory.name) / "packs"
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE tests (
              test_id INTEGER PRIMARY KEY,
              test_name TEXT NOT NULL,
              release_id TEXT
            );
            CREATE TABLE questions (
              question_id INTEGER PRIMARY KEY,
              question_text TEXT NOT NULL,
              correct_answer TEXT NOT NULL,
              explanation TEXT NOT NULL,
              solution_steps TEXT NOT NULL,
              option_explanations TEXT NOT NULL
            );
            CREATE TABLE attempts (
              attempt_id TEXT PRIMARY KEY,
              test_id INTEGER,
              student_id TEXT
            );
            """
        )
        migrate_distributed_schema(self.connection)
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (41, 'Placement Set')")
        self.connection.executemany(
            """INSERT INTO questions
               (question_id, question_text, correct_answer, explanation, solution_steps, option_explanations)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (7, "Seven?", "B", "private rationale", '["private step"]', '{"A":"private"}'),
                (3, "Three?", "A", "private rationale", '["private step"]', '{"B":"private"}'),
            ],
        )
        self.private_key_b64, self.public_key_b64 = generate_ed25519_keypair()
        self.master_key = bytes(range(32))
        self.asset_bytes = b"\x89PNG\r\n\x1a\npublic-image"
        self.asset_name = f"assets/{hashlib.sha256(self.asset_bytes).hexdigest()}.png"

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def public_questions(self):
        return [
            PublicQuestion(
                question_id=7,
                source_key="q-7",
                category="Reasoning",
                chapter="Series",
                difficulty="Medium",
                question_text="Seven?",
                question_html="<p>Seven?</p>",
                options={"A": "6", "B": "7", "C": "8", "D": "9"},
                display_media={
                    "question": {
                        "url": self.asset_name,
                        "alt_text": "Public diagram",
                        "width": 10,
                        "height": 5,
                    }
                },
            ),
            PublicQuestion(
                question_id=3,
                source_key="q-3",
                category="Quantitative",
                chapter="Numbers",
                difficulty="Easy",
                question_text="Three?",
                options={"A": "3", "B": "4", "C": "5", "D": "6"},
                stimulus={
                    "id": "chart-1",
                    "type": "chart",
                    "content": {
                        "chart_type": "bar",
                        "labels": ["A", "B"],
                        "series": [{"name": "Values", "values": [3, 4]}],
                    },
                },
            ),
        ]

    def prepare_release_with_two_questions(self):
        return prepare_release(
            self.connection,
            test_id=41,
            selected_questions=self.public_questions(),
            assets={self.asset_name: self.asset_bytes},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )

    def test_frozen_review_material_uses_steps_and_explanation_fallback(self):
        reviews = app.frozen_review_material([
            {
                "question_id": 7,
                "question_text": "Seven?",
                "source_key": "q-7",
                "correct_answer": "B",
                "solution_steps": '["First frozen step.", "Second frozen step."]',
                "explanation": "Ignored explanation.",
            },
            {
                "question_id": 3,
                "question_text": "Three?",
                "source_key": "q-3",
                "correct_answer": "A",
                "solution_steps": "[]",
                "explanation": "Fallback explanation.",
            },
        ])
        self.assertEqual(["First frozen step.", "Second frozen step."], reviews[0].solution_steps)
        self.assertEqual(["Fallback explanation."], reviews[1].solution_steps)

    def test_release_freezes_questions_excludes_private_material_and_verifies_signature(self):
        release = self.prepare_release_with_two_questions()
        pack_path = self.pack_dir / release.content_pack_filename
        encrypted = pack_path.read_bytes()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        payload = decrypt_pack(content_key, release.release_id, encrypted)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            questions = json.loads(archive.read("questions.json"))
            manifest = json.loads(archive.read("manifest.json"))
            names = archive.namelist()
            timestamps = {entry.date_time for entry in archive.infolist()}
            embedded_asset = archive.read(self.asset_name)

        self.assertEqual([3, 7], release.canonical_question_ids)
        self.assertEqual([item["question_id"] for item in questions], release.canonical_question_ids)
        self.assertEqual(["manifest.json", "questions.json", self.asset_name], names)
        self.assertEqual({(1980, 1, 1, 0, 0, 0)}, timestamps)
        self.assertEqual(self.asset_bytes, embedded_asset)
        self.assertEqual(hashlib.sha256(encrypted).hexdigest(), release.content_hash)
        serialized = json.dumps(questions, sort_keys=True).lower()
        for forbidden in (
            "correct_answer",
            "answer",
            "explanation",
            "solution",
            "option_explanations",
            "feedback",
            "/api/question-banks/",
            "/api/question-assets/",
        ):
            self.assertNotIn(forbidden, serialized)
        verify_json(
            self.public_key_b64,
            {
                "release_id": release.release_id,
                "content_hash": release.content_hash,
                "manifest": manifest,
            },
            release.content_signature_b64,
        )

        before_bytes = pack_path.read_bytes()
        before_mtime = pack_path.stat().st_mtime_ns
        self.connection.execute("UPDATE questions SET question_text='changed' WHERE question_id=3")
        persisted = load_release_manifest(self.connection, release.release_id)
        repeated = prepare_release(
            self.connection,
            test_id=41,
            selected_questions=list(reversed(self.public_questions())),
            assets={},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T10:00:00+00:00",
        )
        self.assertEqual(release, persisted)
        self.assertEqual(release, repeated)
        self.assertEqual(before_bytes, pack_path.read_bytes())
        self.assertEqual(before_mtime, pack_path.stat().st_mtime_ns)
        self.assertEqual(
            [(3, 0), (7, 1)],
            [tuple(row) for row in self.connection.execute(
                "SELECT question_id, canonical_order FROM release_questions ORDER BY canonical_order"
            )],
        )

    def test_review_material_is_separately_encrypted_and_frozen(self):
        release = prepare_release(
            self.connection,
            test_id=41,
            selected_questions=self.public_questions(),
            review_questions=[
                FrozenReviewQuestion(question_id=7, correct_answer="B", solution_steps=["Seven is the answer."]),
                FrozenReviewQuestion(question_id=3, correct_answer="A", solution_steps=["Three is the answer."]),
            ],
            assets={self.asset_name: self.asset_bytes},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        row = self.connection.execute(
            "SELECT * FROM assessment_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        outer = decrypt_pack(
            content_key, release.release_id, (self.pack_dir / release.content_pack_filename).read_bytes()
        )
        with zipfile.ZipFile(io.BytesIO(outer)) as archive:
            encrypted_review = archive.read("review.json.enc")
        self.assertNotIn(b"Seven is the answer", encrypted_review)
        review_key = unwrap_release_review_key(
            self.master_key, release.release_id, row["wrapped_review_key_b64"]
        )
        review = ReviewContent.model_validate_json(
            decrypt_pack(review_key, f"{release.release_id}:review:v1", encrypted_review), strict=True
        )
        self.assertEqual([3, 7], [item.question_id for item in review.questions])
        with self.assertRaises(ValueError):
            decrypt_pack(content_key, f"{release.release_id}:review:v1", encrypted_review)

    def test_wrapped_content_keys_use_fresh_authenticated_encryption(self):
        content_key = b"k" * 32
        first = wrap_release_content_key(self.master_key, "release-1", content_key)
        second = wrap_release_content_key(self.master_key, "release-1", content_key)
        self.assertNotEqual(first, second)
        self.assertEqual(content_key, unwrap_release_content_key(self.master_key, "release-1", first))
        with self.assertRaises(ValueError):
            unwrap_release_content_key(self.master_key, "release-2", first)

    def test_database_failure_removes_pack_and_release_rows(self):
        self.connection.execute(
            """CREATE TRIGGER reject_release BEFORE INSERT ON assessment_releases
               BEGIN SELECT RAISE(ABORT, 'forced failure'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.prepare_release_with_two_questions()
        self.assertEqual([], list(self.pack_dir.glob("*.ksatpack")))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM assessment_releases").fetchone()[0])
        self.assertIsNone(self.connection.execute("SELECT release_id FROM tests WHERE test_id=41").fetchone()[0])

    def test_release_sanitizes_answer_flags_hidden_in_question_html(self):
        unsafe = self.public_questions()[0].model_copy(
            update={"question_html": '<p data-correct-answer="B">Seven?</p>'}
        )
        release = prepare_release(
            self.connection,
            test_id=41,
            selected_questions=[unsafe],
            assets={self.asset_name: self.asset_bytes},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        key = unwrap_release_content_key(self.master_key, release.release_id, release.wrapped_content_key_b64)
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(key, release.release_id, encrypted))) as archive:
            packed = json.loads(archive.read("questions.json"))[0]
        self.assertEqual("", packed["question_html"])

    def test_adversarial_nested_content_html_and_unreferenced_assets_cannot_leak(self):
        unsafe_nested = self.public_questions()[0].model_dump(mode="json")
        unsafe_nested["display_media"]["feedback_html"] = "private feedback"
        with self.assertRaises(ValueError):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=[unsafe_nested],
                assets={self.asset_name: self.asset_bytes},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )

        constructed = PublicQuestion.model_construct(
            question_id=7,
            source_key="constructed-bypass",
            category="Reasoning",
            chapter="Series",
            difficulty="Medium",
            question_text="Constructed?",
            options={"A": "1", "B": "2", "C": "3", "D": "4"},
            display_media={"feedback_html": "private feedback"},
        )
        with self.assertRaises(ValueError):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=[constructed],
                assets={},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )

        unsafe_score = self.public_questions()[1].model_dump(mode="json")
        unsafe_score["stimulus"]["content"]["series"][0]["score_value"] = 100
        with self.assertRaises(ValueError):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=[unsafe_score],
                assets={},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )

        unsafe_stimulus = self.public_questions()[1].model_dump(mode="json")
        unsafe_stimulus["stimulus"] = {
            "id": "table-1",
            "type": "table",
            "title": "Table",
            "alt_text": "Values",
            "content": {"columns": ["A"], "rows": [[1]], "correct_option": "A"},
        }
        with self.assertRaises(ValueError):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=[unsafe_stimulus],
                assets={},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )

        sanitized_question = self.public_questions()[0].model_copy(update={
            "question_html": (
                '<div onclick="steal()"><strong>Seven?</strong>'
                '<script>correct_option="B"</script><!-- feedback_html=private -->'
                '<span data-score-value="100">private score</span></div>'
            )
        })
        release = prepare_release(
            self.connection,
            test_id=41,
            selected_questions=[sanitized_question],
            assets={self.asset_name: self.asset_bytes},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        key = unwrap_release_content_key(self.master_key, release.release_id, release.wrapped_content_key_b64)
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(key, release.release_id, encrypted))) as archive:
            packed = json.loads(archive.read("questions.json"))[0]
        packed_html = packed["question_html"].lower()
        self.assertIn("<strong>seven?</strong>", packed_html)
        for forbidden in ("onclick", "script", "correct_option", "feedback_html", "private score", "data-score"):
            self.assertNotIn(forbidden, packed_html)

    def test_unreferenced_or_unsafe_assets_are_rejected_before_pack_publication(self):
        unused = b"private solution bytes"
        unused_name = f"assets/{hashlib.sha256(unused).hexdigest()}.png"
        with self.assertRaisesRegex(ValueError, "exactly match"):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=self.public_questions(),
                assets={self.asset_name: self.asset_bytes, unused_name: unused},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "unsafe"):
            prepare_release(
                self.connection,
                test_id=41,
                selected_questions=self.public_questions(),
                assets={"assets/../collision.png": self.asset_bytes},
                pack_dir=self.pack_dir,
                signing_private_key_b64=self.private_key_b64,
                pack_master_key=self.master_key,
                now_iso="2026-08-31T09:00:00+00:00",
            )
        for unsafe_html in (
            '<a href="https://coordinator.example/answers">Open</a>',
            '<img src="/api/question-assets/41/solution.png">',
        ):
            with self.subTest(unsafe_html=unsafe_html):
                question = self.public_questions()[1].model_copy(
                    update={"question_html": unsafe_html}
                )
                with self.assertRaisesRegex(ValueError, "URL"):
                    prepare_release(
                        self.connection,
                        test_id=41,
                        selected_questions=[question],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
        self.assertEqual([], list(self.pack_dir.glob("*.ksatpack")))

    def test_obfuscated_urls_and_plain_double_slashes_are_rejected(self):
        adversarial = (
            {"question_text": "Visit HtT\nPs%3A%2F%2Fevil.example now"},
            {"question_html": "<p>&#47;API&#47;question-assets/41/private.png</p>"},
            {"question_html": '<span title="java%73cript%3Aalert(1)">Safe-looking</span>'},
            {"source_key": "data%3Atext/html%2Cprivate"},
            {"question_html": "<p>http://www.w3.org/2000/svg.evil.example/private</p>"},
            {
                "question_html": (
                    '<span title="http://www.w3.org/2000/svg.evil.example/private">x</span>'
                )
            },
            {"question_html": '<span xmlns="http://www.w3.org/2000/svg">not svg</span>'},
            {"question_text": "Read f%69le%3A%2F%2Fserver/private"},
            {"source_key": "mail&#116;o%3Ateacher%40example.com"},
            {"question_text": "Open %2F%2Fevil.example/private"},
            {"question_html": "<p>&#47;&#47;evil.example/private</p>"},
            {"question_text": "Connect w s s %3A%2F%2Fevil.example/socket"},
            {"question_text": "Open sMb%3A%2F%2Fserver/share"},
            {"question_text": "Load bLoB%3Anull%2Fprivate-id"},
            {"question_text": "Data: values. Compute 6 // 2, then compare x/y."},
            {"question_text": "6 // 2.0"},
        )
        for offset, mutation in enumerate(adversarial, start=50):
            with self.subTest(mutation=mutation):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (offset, f"URL case {offset}"),
                )
                question = self.public_questions()[1].model_copy(update=mutation)
                before = set(self.pack_dir.glob("*.ksatpack"))
                with self.assertRaisesRegex(ValueError, "URL|coordinator"):
                    prepare_release(
                        self.connection,
                        test_id=offset,
                        selected_questions=[question],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
                self.assertEqual(before, set(self.pack_dir.glob("*.ksatpack")))

    def test_protocol_relative_authorities_are_rejected_after_normalization(self):
        attacks = (
            {"question_text": "//127.0.0.1"},
            {"question_text": "//127.0.0.1:8080/private?answer=1#key"},
            {"question_text": "//10.20.30.4?private=1"},
            {"question_text": "//192.168.1.254#private"},
            {"question_text": "//server"},
            {"question_text": "//server:8080"},
            {"question_text": "//server/private"},
            {"question_text": "//server?private=1"},
            {"question_text": "//server#private"},
            {"question_text": "//user:pass@server/private"},
            {"question_text": "//localhost"},
            {"question_text": "//localhost/private"},
            {"question_text": "//localhost?private=1"},
            {"question_text": "//localhost#private"},
            {"question_text": "//localhost:8080?private=1#key"},
            {"question_text": "//example.com"},
            {"question_text": "//example.com:8443?private=1"},
            {"question_text": "//[::1]"},
            {"question_text": "//[2001:db8::1]:8443/private"},
            {"question_text": "%2F %2F LoCaL\u200bHoSt:8080/private"},
            {"question_text": "\x00/\t/\rSeRvEr\n:8080/private"},
            {"question_text": "&#47;&#47;127.0.0.1/private"},
            {"question_html": "<p>&#47;&#47;127.0.0.1/private</p>"},
            {"question_html": '<span title="&#47;&#47;SeRvEr?private=1">x</span>'},
            {"question_text": "//example.com./private"},
            {"question_text": "//internal_server/private"},
            {"question_text": "//@server/private"},
            {"question_text": "//server:/private"},
            {"question_text": "//127.1/private"},
            {"question_text": "//2130706433/private"},
            {"question_text": "//例子.com/private"},
            {"question_text": "%2F %2F example%2Ecom%2E%2Fp"},
            {"question_html": "<p>&#47;&#47;example.com.&#47;p</p>"},
            {"question_text": "x // server-name/private"},
            {"question_text": "x // server~name/private"},
            {"question_text": "x // user;param@server/private"},
            {"question_text": "x // 0x7f000001/private"},
        )
        for test_id, mutation in enumerate(attacks, start=80):
            with self.subTest(mutation=mutation):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (test_id, f"Protocol-relative case {test_id}"),
                )
                question = self.public_questions()[1].model_copy(update=mutation)
                before = set(self.pack_dir.glob("*.ksatpack"))
                with self.assertRaisesRegex(ValueError, "external URL"):
                    prepare_release(
                        self.connection,
                        test_id=test_id,
                        selected_questions=[question],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
                self.assertEqual(before, set(self.pack_dir.glob("*.ksatpack")))

    def test_plain_double_slashes_fail_closed_with_author_guidance(self):
        plain_values = (
            "x // y",
            "Compute total // count",
            "6 // 2",
            "6 // 2.0",
            "items // groups",
            "(left + right) // divisor",
            "remainder = total // bucket_count",
        )
        for test_id, plain_value in enumerate(plain_values, start=120):
            with self.subTest(plain_value=plain_value):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (test_id, f"Plain slash case {test_id}"),
                )
                question = self.public_questions()[1].model_copy(
                    update={"question_text": plain_value}
                )
                before = set(self.pack_dir.glob("*.ksatpack"))
                with self.assertRaisesRegex(ValueError, "explicit math markup"):
                    prepare_release(
                        self.connection,
                        test_id=test_id,
                        selected_questions=[question],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
                self.assertEqual(before, set(self.pack_dir.glob("*.ksatpack")))

        structured_mutations = (
            {"options": {"A": "x // y", "B": "safe", "C": "safe", "D": "safe"}},
            {
                "stimulus": {
                    "id": "plain",
                    "type": "chart",
                    "content": {"values": [1], "note": "x // y"},
                }
            },
            {
                "stimulus": {
                    "id": "plain-key",
                    "type": "chart",
                    "content": {"values": [1], "x // y": "safe"},
                }
            },
            {"question_html": "<p>x // y</p>"},
            {"question_html": '<span title="x // y">safe</span>'},
        )
        for test_id, mutation in enumerate(structured_mutations, start=135):
            with self.subTest(mutation=mutation):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (test_id, f"Structured slash case {test_id}"),
                )
                with self.assertRaisesRegex(ValueError, "explicit math markup"):
                    prepare_release(
                        self.connection,
                        test_id=test_id,
                        selected_questions=[self.public_questions()[1].model_copy(update=mutation)],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )

    def test_explicit_math_floor_division_is_canonicalized_and_verified_on_reload(self):
        source_html = (
            '<p>Compute <code class="math-floor-division">total // count</code> and '
            '<code class="math-floor-division">6 // 2.0</code>; '
            '<code>already ÷ safe</code>.</p>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">'
            '<path d="M0 0 L1 1"></path></svg>'
        )
        canonical_html = (
            '<p>Compute <code class="math-floor-division">⌊total ÷ count⌋</code> and '
            '<code class="math-floor-division">⌊6 ÷ 2.0⌋</code>; '
            '<code>already ÷ safe</code>.</p>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">'
            '<path d="M0 0 L1 1"></path></svg>'
        )
        self.assertEqual(canonical_html, app.sanitize_visual_html(source_html))
        question = self.public_questions()[1].model_copy(
            update={
                "question_text": "Compute the floor division of total by count and of six by two.",
                "question_html": source_html,
            }
        )
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (150, 'Math markup')")
        release = prepare_release(
            self.connection,
            test_id=150,
            selected_questions=[question],
            assets={},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(
            content_key,
            release.release_id,
            (self.pack_dir / release.content_pack_filename).read_bytes(),
        ))) as archive:
            packed = json.loads(archive.read("questions.json"))[0]
        self.assertEqual(canonical_html, packed["question_html"])
        self.assertNotIn("//", packed["question_text"])
        self.assertEqual(1, packed["question_html"].count("//"))
        invariant_payload = json.loads(json.dumps(packed))
        invariant_payload["question_html"] = invariant_payload["question_html"].replace(
            ' xmlns="http://www.w3.org/2000/svg"', "", 1
        )
        pending = [invariant_payload]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                decoded = value
                for _ in range(8):
                    expanded = html.unescape(unquote(decoded))
                    if expanded == decoded:
                        break
                    decoded = expanded
                compact = "".join(character for character in decoded if not character.isspace())
                self.assertNotIn("//", compact)
        reloaded = self.verified_load(release.release_id)
        self.assertEqual(release.content_hash, reloaded.content_hash)

    def test_markup_decode_bound_requires_a_fixed_point_and_rejects_hidden_pseudo_elements(self):
        for label, fragment in pseudo_math_depth_matrix():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "exact explicit math markup"):
                    canonicalize_math_floor_division_markup(fragment)

        safe_prose = "AT&T retained 50% of 3 < 5 examples."
        converged = "".join(f"&#{ord(character)};" for character in safe_prose)
        for _ in range(_MARKUP_DECODE_BOUND - 2):
            converged = converged.replace("&", "&amp;")
        self.assertEqual(converged, canonicalize_math_floor_division_markup(converged))

        non_converged = converged.replace("&", "&amp;")
        with self.assertRaisesRegex(ValueError, "exact explicit math markup"):
            canonicalize_math_floor_division_markup(non_converged)

    def test_benign_markup_convergence_preserves_supported_content(self):
        source = (
            '<p>AT&amp;T retained 50%2C while 3 &lt; 5. '
            '<code>alpha &amp; beta</code>.</p>'
            '<code class="math-floor-division">total // count</code>'
            '<code class="math-floor-division">6 // 2</code>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">'
            '<path d="M0 0"></path></svg>'
        )
        expected = source.replace("total // count", "⌊total ÷ count⌋").replace(
            "6 // 2", "⌊6 ÷ 2⌋"
        )
        self.assertEqual(expected, canonicalize_math_floor_division_markup(source))

    def test_malformed_math_floor_division_markup_is_rejected_without_artifacts(self):
        malformed = (
            '<code class="math-floor-division">a // b // c</code>',
            '<code class="math-floor-division">a + b // c</code>',
            '<code class="math-floor-division">a // b + c</code>',
            '<code class="math-floor-division"><span>a</span> // b</code>',
            '<code class="math-floor-division extra">a // b</code>',
            '<code class="math-floor-division" title="x">a // b</code>',
            '<code class="math&#45;floor-division">a // b</code>',
            '<code>a // b</code>',
            '<code class="math-floor-division">a // server/private</code>',
            '<code class="math-floor-division">a // user@server</code>',
            '<code class="math-floor-division">a // server:80</code>',
            '<code class="math-floor-division">a &#47;&#47; b</code>',
            '<code class="math-floor-division">a %2F%2F b</code>',
            '<code class="math-floor-division">a /\x00/ b</code>',
            '<code class="math-floor-division">a<!--hidden--> // b</code>',
            '<code class="math-floor-division">a<?hidden?> // b</code>',
            '<code class="math-floor-division">a // b</CODE>',
            '<code class="math-floor-division">a // b</code x>',
            '<code class="math-floor-division">a // b</code></code>',
            '</code><code class="math-floor-division">a // b</code>',
            '<code class="math-floor-division">a // b',
            '<code class="math-floor-division"/>',
            '<code><code class="math-floor-division">a // b</code></code>',
            '<span><code class="math-floor-division">a // b</span></code>',
            '<code class="math-floor-division">a // b</code><code>',
            f'<code class="math-floor-division">{"a" * 65} // b</code>',
            nested_pseudo_math("entity", _MARKUP_DECODE_BOUND + 1),
            nested_pseudo_math("percent", 64),
        )
        for test_id, question_html in enumerate(malformed, start=160):
            with self.subTest(question_html=question_html):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (test_id, f"Malformed math case {test_id}"),
                )
                before = set(self.pack_dir.glob("*.ksatpack"))
                with self.assertRaisesRegex(ValueError, "math markup"):
                    prepare_release(
                        self.connection,
                        test_id=test_id,
                        selected_questions=[self.public_questions()[1].model_copy(
                            update={"question_html": question_html}
                        )],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
                self.assertEqual(before, set(self.pack_dir.glob("*.ksatpack")))

    def test_supported_structured_stimuli_and_safe_visual_html_survive_real_packs(self):
        chart = self.public_questions()[1].model_dump(mode="json")
        chart["stimulus"]["content"] = {
            "values": [10, 20, 30],
            "axis": {"labels": ["Q1", "Q2", "Q3"], "unit": "%"},
        }
        safe_table = self.public_questions()[0].model_dump(mode="json")
        safe_table["display_media"] = {}
        safe_table["stimulus"] = {
            "id": "results-table",
            "type": "table",
            "title": "Results",
            "alt_text": "Quarterly results",
            "content": {"columns": ["Quarter", "Value"], "rows": [["Q1", 10], ["Q2", 20]]},
        }
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (61, 'Values chart')")
        chart_release = prepare_release(
            self.connection,
            test_id=61,
            selected_questions=[chart, safe_table],
            assets={},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        chart_key = unwrap_release_content_key(
            self.master_key, chart_release.release_id, chart_release.wrapped_content_key_b64
        )
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(
            chart_key,
            chart_release.release_id,
            (self.pack_dir / chart_release.content_pack_filename).read_bytes(),
        ))) as archive:
            packed_structured = json.loads(archive.read("questions.json"))
        packed_chart = next(item for item in packed_structured if item["question_id"] == 3)
        packed_safe_table = next(item for item in packed_structured if item["question_id"] == 7)
        self.assertEqual(chart["stimulus"]["content"], packed_chart["stimulus"]["content"])
        self.assertEqual(
            safe_table["stimulus"]["content"], packed_safe_table["stimulus"]["content"]
        )

        second_asset = b"\x89PNG\r\n\x1a\npublic-stimulus"
        second_name = f"assets/{hashlib.sha256(second_asset).hexdigest()}.png"
        table_question = self.public_questions()[0].model_dump(mode="json")
        table_question["question_html"] = (
            '<table><tbody><tr><th scope="col" colspan="2">x<sub>1</sub></th></tr></tbody></table>'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2">'
            '<path d="M0 0 L1 1" fill="#000"></path></svg><sup>2</sup>'
        )
        table_question["stimulus"] = {
            "id": "legacy-table",
            "type": "table",
            "title": "Legacy empty table",
            "alt_text": "Legacy table",
            "content": {},
        }
        table_question["display_media"]["options"] = {
            "A": {"url": second_name, "alt_text": "Option diagram", "width": 8, "height": 6}
        }
        image_question = self.public_questions()[1].model_dump(mode="json")
        image_question["question_id"] = 8
        image_question["source_key"] = "image-stimulus"
        image_question["stimulus"] = {
            "id": "image-stimulus",
            "type": "image",
            "title": "Embedded image",
            "alt_text": "Public stimulus diagram",
            "url": second_name,
        }
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (62, 'Safe visuals')")
        table_release = prepare_release(
            self.connection,
            test_id=62,
            selected_questions=[table_question, image_question],
            assets={self.asset_name: self.asset_bytes, second_name: second_asset},
            pack_dir=self.pack_dir,
            signing_private_key_b64=self.private_key_b64,
            pack_master_key=self.master_key,
            now_iso="2026-08-31T09:00:00+00:00",
        )
        table_key = unwrap_release_content_key(
            self.master_key, table_release.release_id, table_release.wrapped_content_key_b64
        )
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(
            table_key,
            table_release.release_id,
            (self.pack_dir / table_release.content_pack_filename).read_bytes(),
        ))) as archive:
            packed_visuals = json.loads(archive.read("questions.json"))
        packed_table = next(item for item in packed_visuals if item["question_id"] == 7)
        packed_image = next(item for item in packed_visuals if item["question_id"] == 8)
        self.assertEqual({}, packed_table["stimulus"]["content"])
        self.assertEqual(second_name, packed_table["display_media"]["options"]["A"]["url"])
        self.assertEqual(second_name, packed_image["stimulus"]["url"])
        for fragment in ("<table>", '<th scope="col" colspan="2">', "<sub>1</sub>", "<svg", "viewBox=", "<path", "<sup>2</sup>"):
            self.assertIn(fragment, packed_table["question_html"])
        self.assertEqual(
            table_release,
            load_release_manifest(
                self.connection,
                table_release.release_id,
                pack_dir=self.pack_dir,
                signing_public_key_b64=self.public_key_b64,
                pack_master_key=self.master_key,
            ),
        )

    def test_structured_stimulus_rejects_nested_private_and_url_fields(self):
        for test_id, content in (
            (63, {"values": [1], "meta": {"feedback_html": "private"}}),
            (64, {"values": [1], "s%6furce_%75rl": "hTTps%3A%2F%2Fevil.example"}),
            (65, {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}),
            (66, {"values": [1], "asset": {"path": "diagram.png"}}),
        ):
            with self.subTest(content=content):
                self.connection.execute(
                    "INSERT INTO tests (test_id, test_name) VALUES (?, ?)",
                    (test_id, f"Unsafe structure {test_id}"),
                )
                question = self.public_questions()[1].model_dump(mode="json")
                question["stimulus"]["content"] = content
                with self.assertRaises(ValueError):
                    prepare_release(
                        self.connection,
                        test_id=test_id,
                        selected_questions=[question],
                        assets={},
                        pack_dir=self.pack_dir,
                        signing_private_key_b64=self.private_key_b64,
                        pack_master_key=self.master_key,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )

    def verified_load(self, release_id):
        return load_release_manifest(
            self.connection,
            release_id,
            pack_dir=self.pack_dir,
            signing_public_key_b64=self.public_key_b64,
            pack_master_key=self.master_key,
        )

    def install_resigned_questions(self, release, manifest, asset_bytes, questions_json):
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        plaintext = io.BytesIO()
        with zipfile.ZipFile(plaintext, "w") as archive:
            entries = (
                (
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
                ),
                ("questions.json", questions_json),
                *((name, asset_bytes[name]) for name in manifest["asset_names"]),
            )
            for name, value in entries:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, value)
        encrypted = encrypt_pack(content_key, release.release_id, plaintext.getvalue())
        content_hash = hashlib.sha256(encrypted).hexdigest()
        signature = sign_json(
            self.private_key_b64,
            {"release_id": release.release_id, "content_hash": content_hash, "manifest": manifest},
        )
        verify_json(
            self.public_key_b64,
            {"release_id": release.release_id, "content_hash": content_hash, "manifest": manifest},
            signature,
        )
        (self.pack_dir / release.content_pack_filename).write_bytes(encrypted)
        self.connection.execute(
            """UPDATE assessment_releases SET content_hash = ?, content_signature_b64 = ?
               WHERE release_id = ?""",
            (content_hash, signature, release.release_id),
        )

    def test_verified_load_requires_canonical_duplicate_free_questions_json_bytes(self):
        release = self.prepare_release_with_two_questions()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(content_key, release.release_id, encrypted))) as archive:
            canonical_questions = archive.read("questions.json")
            questions = json.loads(canonical_questions)
            manifest = json.loads(archive.read("manifest.json"))
            asset_bytes = {name: archive.read(name) for name in manifest["asset_names"]}
        self.assertEqual(canonical_json(questions), canonical_questions)

        duplicate_key_json = canonical_questions.replace(
            b'"question_text":"Three?"',
            b'"question_text":"forbidden // value","question_text":"Three?"',
            1,
        )
        noncanonical_json = json.dumps(
            questions, ensure_ascii=False, sort_keys=False, indent=2
        ).encode("utf-8")
        alternate_order = json.loads(canonical_questions)
        alternate_order[0] = dict(reversed(list(alternate_order[0].items())))
        alternate_order_json = json.dumps(
            alternate_order, ensure_ascii=False, sort_keys=False, separators=(",", ":")
        ).encode("utf-8")
        escaped_json = canonical_questions.replace(b'"source_key":"q-3"', b'"source_key":"\\u0071-3"', 1)
        noncanonical_number_json = canonical_questions.replace(b'"question_id":3', b'"question_id":3.0', 1)
        for label, questions_json in (
            ("duplicate key", duplicate_key_json),
            ("alternate formatting", noncanonical_json),
            ("alternate key order", alternate_order_json),
            ("alternate escape", escaped_json),
            ("noncanonical number", noncanonical_number_json),
        ):
            with self.subTest(label=label):
                self.install_resigned_questions(release, manifest, asset_bytes, questions_json)
                with self.assertRaisesRegex(ValueError, "questions.*(?:JSON|canonical)"):
                    self.verified_load(release.release_id)

    def test_verified_load_rejects_duplicate_key_hiding_malformed_math_source(self):
        release = self.prepare_release_with_two_questions()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(content_key, release.release_id, encrypted))) as archive:
            canonical_questions = archive.read("questions.json")
            manifest = json.loads(archive.read("manifest.json"))
            asset_bytes = {name: archive.read(name) for name in manifest["asset_names"]}

        for malformed_close in ("</CODE>", "</code x>", "</code></code>"):
            with self.subTest(malformed_close=malformed_close):
                malicious_html = (
                    '<code class="math-floor-division">left // right' + malformed_close
                )
                duplicate_key_json = canonical_questions.replace(
                    b'"question_html":""',
                    (
                        '"question_html":'
                        + json.dumps(malicious_html, ensure_ascii=False)
                        + ',"question_html":""'
                    ).encode("utf-8"),
                    1,
                )
                self.install_resigned_questions(
                    release, manifest, asset_bytes, duplicate_key_json
                )
                with self.assertRaisesRegex(ValueError, "questions.*(?:JSON|canonical)"):
                    self.verified_load(release.release_id)

    def test_verified_load_rejects_canonical_json_with_malformed_math_source(self):
        release = self.prepare_release_with_two_questions()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(content_key, release.release_id, encrypted))) as archive:
            original_questions = json.loads(archive.read("questions.json"))
            manifest = json.loads(archive.read("manifest.json"))
            asset_bytes = {name: archive.read(name) for name in manifest["asset_names"]}

        malformed_sources = tuple(
            '<code class="math-floor-division">left // right' + malformed_close
            for malformed_close in ("</CODE>", "</code x>", "</code></code>")
        ) + (
            nested_pseudo_math("entity", _MARKUP_DECODE_BOUND + 1),
            nested_pseudo_math("alternating", 64),
        )
        for malicious_html in malformed_sources:
            with self.subTest(malicious_html=malicious_html):
                questions = json.loads(json.dumps(original_questions))
                questions[0]["question_html"] = malicious_html
                self.install_resigned_questions(
                    release, manifest, asset_bytes, canonical_json(questions)
                )
                with self.assertRaisesRegex(ValueError, "pack questions"):
                    self.verified_load(release.release_id)

    def test_verified_load_rejects_manifest_linkage_and_encoding_corruption(self):
        release = self.prepare_release_with_two_questions()
        row = self.connection.execute(
            "SELECT manifest_json FROM assessment_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone()
        duplicate_manifest = row["manifest_json"].replace(
            '"test_id":41', '"test_id":999,"test_id":41', 1
        )
        self.connection.execute(
            "UPDATE assessment_releases SET manifest_json = ? WHERE release_id = ?",
            (duplicate_manifest, release.release_id),
        )
        with self.assertRaisesRegex(ValueError, "release manifest is invalid"):
            self.verified_load(release.release_id)
        self.connection.execute(
            "UPDATE assessment_releases SET manifest_json = ? WHERE release_id = ?",
            (row["manifest_json"], release.release_id),
        )
        manifest = json.loads(row["manifest_json"])
        manifest["test_id"] = 999
        self.connection.execute(
            "UPDATE assessment_releases SET manifest_json = ? WHERE release_id = ?",
            (json.dumps(manifest), release.release_id),
        )
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

        manifest["test_id"] = 41
        self.connection.execute(
            "UPDATE assessment_releases SET manifest_json = ?, content_signature_b64 = '***' WHERE release_id = ?",
            (json.dumps(manifest), release.release_id),
        )
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

        self.connection.execute(
            "UPDATE assessment_releases SET content_signature_b64 = ?, wrapped_content_key_b64 = 'AAAA' WHERE release_id = ?",
            (release.content_signature_b64, release.release_id),
        )
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

    def test_verified_load_rejects_pack_hash_signature_and_question_linkage_corruption(self):
        release = self.prepare_release_with_two_questions()
        pack_path = self.pack_dir / release.content_pack_filename
        original = pack_path.read_bytes()
        pack_path.write_bytes(original + b"tampered")
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)
        pack_path.write_bytes(original)
        self.connection.execute(
            "DELETE FROM release_questions WHERE release_id = ? AND question_id = 7", (release.release_id,)
        )
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

    def test_verified_load_cross_checks_every_stored_identity_field(self):
        release = self.prepare_release_with_two_questions()
        self.connection.execute("INSERT INTO tests (test_id, test_name) VALUES (999, 'Wrong test')")
        mutations = (
            ("state", "failed"),
            ("test_id", 999),
            ("duration_seconds", 999),
            ("content_pack_filename", "../wrong.ksatpack"),
            ("content_hash", "0" * 64),
            ("content_signature_b64", base64.b64encode(b"s" * 64).decode("ascii")),
            ("wrapped_content_key_b64", base64.b64encode(b"w" * 60).decode("ascii")),
        )
        original = dict(self.connection.execute(
            "SELECT * FROM assessment_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone())
        for column, value in mutations:
            with self.subTest(column=column):
                self.connection.execute(
                    f"UPDATE assessment_releases SET {column} = ? WHERE release_id = ?",
                    (value, release.release_id),
                )
                with self.assertRaises(ValueError):
                    self.verified_load(release.release_id)
                self.connection.execute(
                    f"UPDATE assessment_releases SET {column} = ? WHERE release_id = ?",
                    (original[column], release.release_id),
                )
        self.connection.execute("UPDATE tests SET release_id = NULL WHERE test_id = 41")
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

    def test_stored_release_rejects_unknown_versions_and_unsafe_identity(self):
        release = self.prepare_release_with_two_questions()
        original_manifest = self.connection.execute(
            "SELECT manifest_json FROM assessment_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone()[0]
        manifest = json.loads(original_manifest)
        for field in ("protocol_version", "pack_format_version"):
            with self.subTest(field=field):
                corrupt = dict(manifest)
                corrupt[field] = 999
                self.connection.execute(
                    "UPDATE assessment_releases SET manifest_json = ? WHERE release_id = ?",
                    (json.dumps(corrupt), release.release_id),
                )
                with self.assertRaises(ValueError):
                    load_release_manifest(self.connection, release.release_id)
                self.connection.execute(
                    "UPDATE assessment_releases SET manifest_json = ? WHERE release_id = ?",
                    (original_manifest, release.release_id),
                )

        unsafe_release_id = "../escape"
        self.connection.execute(
            "INSERT INTO tests (test_id, test_name, release_id) VALUES (42, 'Unsafe', ?)",
            (unsafe_release_id,),
        )
        unsafe_manifest = dict(manifest)
        unsafe_manifest.update({
            "release_id": unsafe_release_id,
            "test_id": 42,
            "test_name": "Unsafe",
            "duration_seconds": 60,
            "canonical_question_ids": [3],
            "asset_names": [],
        })
        self.connection.execute(
            """INSERT INTO assessment_releases
               (release_id, test_id, state, duration_seconds, manifest_json, content_pack_filename,
                content_hash, content_signature_b64, wrapped_content_key_b64, created_at)
               VALUES (?, 42, 'prepared', 60, ?, ?, ?, ?, ?, ?)""",
            (
                unsafe_release_id,
                json.dumps(unsafe_manifest),
                "../escape.ksatpack",
                "0" * 64,
                base64.b64encode(b"s" * 64).decode("ascii"),
                base64.b64encode(b"w" * 60).decode("ascii"),
                "2026-08-31T09:00:00+00:00",
            ),
        )
        self.connection.execute(
            "INSERT INTO release_questions (release_id, question_id, canonical_order) VALUES (?, 3, 0)",
            (unsafe_release_id,),
        )
        with self.assertRaises(ValueError):
            load_release_manifest(self.connection, unsafe_release_id)

    def test_verified_load_rejects_unsafe_or_colliding_zip_entries_even_when_resigned(self):
        release = self.prepare_release_with_two_questions()
        row = self.connection.execute(
            "SELECT manifest_json FROM assessment_releases WHERE release_id = ?", (release.release_id,)
        ).fetchone()
        manifest = json.loads(row["manifest_json"])
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        malicious_zip = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(malicious_zip, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.writestr("questions.json", "[]")
                archive.writestr("../solution.json", '{"correct_option":"B"}')
                archive.writestr("questions.json", "[]")
        encrypted = encrypt_pack(content_key, release.release_id, malicious_zip.getvalue())
        content_hash = hashlib.sha256(encrypted).hexdigest()
        signature = sign_json(
            self.private_key_b64,
            {"release_id": release.release_id, "content_hash": content_hash, "manifest": manifest},
        )
        (self.pack_dir / release.content_pack_filename).write_bytes(encrypted)
        self.connection.execute(
            """UPDATE assessment_releases SET content_hash = ?, content_signature_b64 = ?
               WHERE release_id = ?""",
            (content_hash, signature, release.release_id),
        )
        with self.assertRaises(ValueError):
            self.verified_load(release.release_id)

    def test_verified_load_rejects_signed_pack_with_unreferenced_asset(self):
        release = self.prepare_release_with_two_questions()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(content_key, release.release_id, encrypted))) as archive:
            questions = json.loads(archive.read("questions.json"))
            manifest = json.loads(archive.read("manifest.json"))
            original_asset = archive.read(self.asset_name)

        private_bytes = b"unreferenced private solution diagram"
        private_name = f"assets/{hashlib.sha256(private_bytes).hexdigest()}.png"
        manifest["asset_names"] = sorted([self.asset_name, private_name])
        plaintext = io.BytesIO()
        with zipfile.ZipFile(plaintext, "w") as archive:
            for name, value in (
                ("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()),
                ("questions.json", json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()),
                (self.asset_name, original_asset),
                (private_name, private_bytes),
            ):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, value)
        tampered_encrypted = encrypt_pack(content_key, release.release_id, plaintext.getvalue())
        content_hash = hashlib.sha256(tampered_encrypted).hexdigest()
        signature = sign_json(
            self.private_key_b64,
            {"release_id": release.release_id, "content_hash": content_hash, "manifest": manifest},
        )
        (self.pack_dir / release.content_pack_filename).write_bytes(tampered_encrypted)
        self.connection.execute(
            """UPDATE assessment_releases
               SET manifest_json = ?, content_hash = ?, content_signature_b64 = ?
               WHERE release_id = ?""",
            (json.dumps(manifest), content_hash, signature, release.release_id),
        )
        with self.assertRaisesRegex(ValueError, "asset|reference"):
            self.verified_load(release.release_id)

    def test_verified_load_rejects_signed_pack_with_obfuscated_external_references(self):
        release = self.prepare_release_with_two_questions()
        content_key = unwrap_release_content_key(
            self.master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (self.pack_dir / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(content_key, release.release_id, encrypted))) as archive:
            original_questions = json.loads(archive.read("questions.json"))
            manifest = json.loads(archive.read("manifest.json"))
            asset_bytes = {name: archive.read(name) for name in manifest["asset_names"]}

        attacks = (
            ("question_html", "<p>http://www.w3.org/2000/svg.evil.example/private</p>"),
            ("question_text", "Read f%69le%3A%2F%2Fserver/private"),
            ("source_key", "mail&#116;o%3Ateacher%40example.com"),
            ("question_text", "Open %2F%2Fevil.example/private"),
            ("question_html", "<p>&#47;&#47;127.0.0.1/private</p>"),
            ("question_text", "//server"),
            ("question_text", "//[2001:db8::1]:8443/private"),
            ("question_text", "//example.com./private"),
            ("question_text", "//internal_server/private"),
            ("question_text", "//@server/private"),
            ("question_text", "//server:/private"),
            ("question_text", "//127.1/private"),
            ("question_text", "//2130706433/private"),
            ("question_text", "//例子.com/private"),
            ("question_text", "%2F %2F example%2Ecom%2E%2Fp"),
            ("question_html", "<p>&#47;&#47;example.com.&#47;p</p>"),
            ("question_text", "x // y"),
            ("question_text", "Compute total // count"),
            ("question_text", "x // server-name/private"),
            ("question_text", "x // server~name/private"),
            ("question_text", "x // user;param@server/private"),
            ("question_text", "x // 0x7f000001/private"),
            (
                "question_html",
                '<code class="math-floor-division">a &#47;&#47; b</code>',
            ),
        )
        for field, malicious_value in attacks:
            with self.subTest(field=field, malicious_value=malicious_value):
                questions = json.loads(json.dumps(original_questions))
                questions[0][field] = malicious_value
                plaintext = io.BytesIO()
                with zipfile.ZipFile(plaintext, "w") as archive:
                    entries = (
                        (
                            "manifest.json",
                            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                        ),
                        (
                            "questions.json",
                            json.dumps(questions, sort_keys=True, separators=(",", ":")).encode(),
                        ),
                        *((name, asset_bytes[name]) for name in manifest["asset_names"]),
                    )
                    for name, value in entries:
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_STORED
                        info.create_system = 3
                        info.external_attr = 0o600 << 16
                        archive.writestr(info, value)
                malicious_encrypted = encrypt_pack(
                    content_key, release.release_id, plaintext.getvalue()
                )
                content_hash = hashlib.sha256(malicious_encrypted).hexdigest()
                signature = sign_json(
                    self.private_key_b64,
                    {
                        "release_id": release.release_id,
                        "content_hash": content_hash,
                        "manifest": manifest,
                    },
                )
                verify_json(
                    self.public_key_b64,
                    {
                        "release_id": release.release_id,
                        "content_hash": content_hash,
                        "manifest": manifest,
                    },
                    signature,
                )
                (self.pack_dir / release.content_pack_filename).write_bytes(malicious_encrypted)
                self.connection.execute(
                    """UPDATE assessment_releases
                       SET content_hash = ?, content_signature_b64 = ?
                       WHERE release_id = ?""",
                    (content_hash, signature, release.release_id),
                )
                with self.assertRaises(ValueError):
                    self.verified_load(release.release_id)

    def test_key_wrapping_rejects_wrong_lengths_and_malformed_envelopes_stably(self):
        for invalid_key in (b"", b"x" * 16, b"x" * 31, b"x" * 33):
            with self.subTest(length=len(invalid_key)):
                with self.assertRaisesRegex(ValueError, "32 bytes"):
                    wrap_release_content_key(invalid_key, "release-1", b"c" * 32)
                with self.assertRaisesRegex(ValueError, "32 bytes"):
                    wrap_release_content_key(b"m" * 32, "release-1", invalid_key)
                with self.assertRaisesRegex(ValueError, "32 bytes"):
                    unwrap_release_content_key(invalid_key, "release-1", "AAAA")
        for malformed in ("***", "AAAA", 123):
            with self.subTest(value=malformed):
                with self.assertRaisesRegex(ValueError, "Wrapped release content key is invalid"):
                    unwrap_release_content_key(b"m" * 32, "release-1", malformed)
        valid = wrap_release_content_key(b"m" * 32, "release-1", b"c" * 32)
        tampered = bytearray(base64.b64decode(valid))
        tampered[-1] ^= 1
        for master_key, envelope in (
            (b"m" * 32, base64.b64encode(tampered).decode("ascii")),
            (b"w" * 32, valid),
        ):
            with self.subTest(master_key=master_key, envelope=envelope):
                with self.assertRaisesRegex(ValueError, "Wrapped release content key is invalid"):
                    unwrap_release_content_key(master_key, "release-1", envelope)

    def test_pack_collision_never_clobbers_or_deletes_preexisting_artifact(self):
        fixed_release_id = "11111111-1111-1111-1111-111111111111"
        self.pack_dir.mkdir(parents=True)
        existing_path = self.pack_dir / f"{fixed_release_id}.ksatpack"
        existing_path.write_bytes(b"pre-existing verified bytes")
        with patch("ksat.coordinator.releases.uuid.uuid4", return_value=fixed_release_id):
            with self.assertRaises(FileExistsError):
                self.prepare_release_with_two_questions()
        self.assertEqual(b"pre-existing verified bytes", existing_path.read_bytes())
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM assessment_releases").fetchone()[0])

    def test_commit_failure_preserves_identical_preexisting_pack(self):
        fixed_release_id = "22222222-2222-2222-2222-222222222222"
        deterministic_random = lambda size: b"r" * size
        with (
            patch("ksat.coordinator.releases.uuid.uuid4", return_value=fixed_release_id),
            patch("os.urandom", side_effect=deterministic_random),
        ):
            release = self.prepare_release_with_two_questions()
        pack_path = self.pack_dir / release.content_pack_filename
        original = pack_path.read_bytes()
        self.connection.execute("DELETE FROM release_questions WHERE release_id = ?", (release.release_id,))
        self.connection.execute("DELETE FROM assessment_releases WHERE release_id = ?", (release.release_id,))
        self.connection.execute("UPDATE tests SET release_id = NULL WHERE test_id = 41")
        self.connection.execute(
            """CREATE TRIGGER reject_recreated_release BEFORE INSERT ON assessment_releases
               BEGIN SELECT RAISE(ABORT, 'forced commit failure'); END"""
        )
        created_artifacts = []
        with (
            patch("ksat.coordinator.releases.uuid.uuid4", return_value=fixed_release_id),
            patch("os.urandom", side_effect=deterministic_random),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                prepare_release(
                    self.connection,
                    test_id=41,
                    selected_questions=self.public_questions(),
                    assets={self.asset_name: self.asset_bytes},
                    pack_dir=self.pack_dir,
                    signing_private_key_b64=self.private_key_b64,
                    pack_master_key=self.master_key,
                    now_iso="2026-08-31T09:00:00+00:00",
                    _created_artifact_paths=created_artifacts,
                )
        self.assertEqual([], created_artifacts)
        self.assertEqual(original, pack_path.read_bytes())


class ConcurrentAssessmentReleaseTests(unittest.TestCase):
    def test_two_connections_publish_and_return_one_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "release.db"
            connection = connect_sqlite(database)
            connection.executescript(
                """
                CREATE TABLE tests (test_id INTEGER PRIMARY KEY, test_name TEXT NOT NULL, release_id TEXT);
                CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, test_id INTEGER, student_id TEXT);
                """
            )
            migrate_distributed_schema(connection)
            connection.execute("INSERT INTO tests (test_id, test_name) VALUES (1, 'Concurrent')")
            connection.commit()
            connection.close()
            private_key, _ = generate_ed25519_keypair()
            barrier = threading.Barrier(2)

            def worker(question_id):
                local = connect_sqlite(database)
                try:
                    barrier.wait(timeout=5)
                    result = prepare_release(
                        local,
                        test_id=1,
                        selected_questions=[PublicQuestion(
                            question_id=question_id,
                            source_key=f"q-{question_id}",
                            category="Reasoning",
                            chapter="Series",
                            difficulty="Easy",
                            question_text=f"Question {question_id}",
                            options={"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                        )],
                        assets={},
                        pack_dir=root / "packs",
                        signing_private_key_b64=private_key,
                        pack_master_key=b"m" * 32,
                        now_iso="2026-08-31T09:00:00+00:00",
                    )
                    local.commit()
                    return result
                finally:
                    local.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(worker, (1, 2)))
            self.assertEqual(results[0], results[1])
            self.assertIn(results[0].canonical_question_ids, ([1], [2]))
            self.assertEqual(1, len(list((root / "packs").glob("*.ksatpack"))))
            check = connect_sqlite(database)
            try:
                self.assertEqual(1, check.execute("SELECT COUNT(*) FROM assessment_releases").fetchone()[0])
                self.assertEqual(1, check.execute("SELECT COUNT(*) FROM release_questions").fetchone()[0])
            finally:
                check.close()


class FacultyReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = app.DATA_DIR
        self.original_db_path = app.DB_PATH
        self.original_backup_dir = app.BACKUP_DIR
        self.original_question_banks_dir = app.QUESTION_BANKS_DIR
        self.original_coordinator_config = app.app.state.coordinator_config
        app.DATA_DIR = Path(self.temporary_directory.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        app.ensure_schema()
        app.configure_coordinator_state(app.app)
        self.public_media = b"public display media"
        self.solution_media = b"private solution media"
        self.stimulus_media = b"public stimulus media"
        self.public_filename = hashlib.sha256(self.public_media).hexdigest() + ".png"
        self.solution_filename = hashlib.sha256(self.solution_media).hexdigest() + ".png"
        self.stimulus_filename = hashlib.sha256(self.stimulus_media).hexdigest() + ".png"
        with app.db() as connection:
            connection.execute(
                "INSERT INTO admins (username, name, password_hash) VALUES (?, ?, ?)",
                ("faculty", "Faculty", app.hash_password("faculty123")),
            )
            self.bank_id = connection.execute(
                """INSERT INTO question_banks
                   (bank_name, source_html_filename, answer_key_filename, imported_at, format_version)
                   VALUES ('Release Bank', 'questions.zip', 'manifest.json', ?, 3)""",
                (app.now(),),
            ).lastrowid
            asset_dir = app.question_assets_dir() / str(self.bank_id)
            asset_dir.mkdir(parents=True)
            (asset_dir / self.public_filename).write_bytes(self.public_media)
            (asset_dir / self.solution_filename).write_bytes(self.solution_media)
            (asset_dir / self.stimulus_filename).write_bytes(self.stimulus_media)
            connection.execute(
                """INSERT INTO stimuli
                   (bank_id, stimulus_id, stimulus_type, title, alt_text, asset_filename, content_json, created_at)
                   VALUES (?, 'stimulus-1', 'image', 'Prompt chart', 'Public chart', ?, '{}', ?)""",
                (self.bank_id, self.stimulus_filename, app.now()),
            )
            media = {
                "question": {
                    "asset_filename": self.public_filename,
                    "alt_text": "Public diagram",
                    "sha256": hashlib.sha256(self.public_media).hexdigest(),
                    "width": 8,
                    "height": 6,
                },
                "solution": [{
                    "asset_filename": self.solution_filename,
                    "alt_text": "Private worked diagram",
                    "sha256": hashlib.sha256(self.solution_media).hexdigest(),
                    "width": 8,
                    "height": 6,
                }],
            }
            self.question_id = connection.execute(
                """INSERT INTO questions
                   (source_key, question_text, question_html, category, chapter, stimulus_id, difficulty,
                    option_a, option_b, option_c, option_d, options_json, correct_answer, explanation,
                    bank_id, created_at, solution_steps, option_explanations, display_media_json)
                   VALUES ('release-q1', 'What is shown?', '<p>What is shown?</p>', 'Reasoning', 'Charts',
                           'stimulus-1', 'Easy', 'One', 'Two', 'Three', 'Four', ?, 'B',
                           'private rationale', ?, ?, '["private step"]', '{"A":"private"}', ?)""",
                (json.dumps({"A": "One", "B": "Two", "C": "Three", "D": "Four"}),
                 self.bank_id, app.now(), json.dumps(media)),
            ).lastrowid
        self.client = TestClient(app.app, raise_server_exceptions=False)
        login = self.client.post(
            "/api/login",
            json={"identifier": "faculty", "password": "faculty123", "role": "admin"},
        )
        self.assertEqual(200, login.status_code, login.text)
        self.csrf_token = login.json()["csrf_token"]
        self.client.headers["X-KSAT-CSRF"] = self.csrf_token

    def tearDown(self):
        self.client.close()
        app.DATA_DIR = self.original_data_dir
        app.DB_PATH = self.original_db_path
        app.BACKUP_DIR = self.original_backup_dir
        app.QUESTION_BANKS_DIR = self.original_question_banks_dir
        app.app.state.coordinator_config = self.original_coordinator_config
        self.temporary_directory.cleanup()

    def create_payload(self, name="Frozen Faculty Test"):
        return {
            "test_name": name,
            "bank_id": self.bank_id,
            "selection_rules": [{"category": "Reasoning", "chapter": "Charts", "quantity": 1}],
            "difficulties": ["Easy"],
        }

    def test_html_pair_import_rejects_raw_malformed_explicit_math_before_writes(self):
        malformed_fragments = (
            '<code class="math-floor-division">left // right</CODE>',
            '<code class="math-floor-division">left // right</code x>',
            '<code class="math-floor-division">left // right</code></code>',
            '</code><code class="math-floor-division">left // right</code>',
            '<code class="math-floor-division"/>',
            '<code><code class="math-floor-division">left // right</code></code>',
            '<code class="math-floor-division">left // right</span></code>',
            '&lt;code class="math-floor-division"&gt;left // right&lt;/code&gt;',
            '<CODE class="math-floor-division">left // right</code>',
            (
                '<p>Otherwise valid before.</p><div><code class="math-floor-division">'
                'left // right</CODE></div><svg viewBox="0 0 1 1"><path d="M0 0"></path></svg>'
            ),
        )
        with app.db() as connection:
            baseline = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] for table in ("question_banks", "questions", "assessment_releases"))
        pack_dir = app.assessment_packs_dir()
        baseline_packs = set(pack_dir.glob("*.ksatpack")) if pack_dir.exists() else set()

        for index, fragment in enumerate(malformed_fragments):
            with self.subTest(fragment=fragment):
                answer_key = json.dumps({
                    "bank_name": f"Rejected raw math {index}",
                    "questions": [{
                        "key": "bad-q",
                        "category": "Quantitative Aptitude",
                        "chapter": "Arithmetic",
                        "difficulty": "Easy",
                        "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                        "correct_answer": "A",
                    }],
                })
                response = self.client.post(
                    "/api/admin/question-banks/import",
                    files={
                        "html_file": (
                            f"rejected-{index}.html",
                            f'<section data-question-key="bad-q">{fragment}</section>',
                            "text/html",
                        ),
                        "answer_key_file": (
                            f"rejected-{index}.json", answer_key, "application/json"
                        ),
                    },
                )
                self.assertEqual(400, response.status_code, response.text)
                with app.db() as connection:
                    current = tuple(connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0] for table in (
                        "question_banks", "questions", "assessment_releases"
                    ))
                self.assertEqual(baseline, current)
                current_packs = set(pack_dir.glob("*.ksatpack")) if pack_dir.exists() else set()
                self.assertEqual(baseline_packs, current_packs)

    def test_parse_question_bank_rejects_decode_depth_matrix(self):
        for label, fragment in pseudo_math_depth_matrix():
            with self.subTest(label=label):
                with self.assertRaises(app.HTTPException) as captured:
                    app.parse_question_bank(
                        f'<section data-question-key="bad-q">{fragment}</section>',
                        one_question_answer_key(f"Rejected parse {label}"),
                    )
                self.assertEqual(400, captured.exception.status_code)
                self.assertEqual(MATH_FLOOR_DIVISION_ERROR, captured.exception.detail)

    def test_import_routes_reject_decode_depth_matrix_before_persistence(self):
        pack_dir = app.assessment_packs_dir()

        def persisted_state():
            with app.db() as connection:
                counts = tuple(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0] for table in (
                    "question_banks", "questions", "assessment_releases"
                ))
            packs = set(pack_dir.glob("*.ksatpack")) if pack_dir.exists() else set()
            return counts, packs

        for index, (label, fragment) in enumerate(pseudo_math_depth_matrix()):
            with self.subTest(route="multipart", label=label):
                before = persisted_state()
                response = self.client.post(
                    "/api/admin/question-banks/import",
                    files={
                        "html_file": (
                            f"decode-bound-{index}.html",
                            f'<section data-question-key="bad-q">{fragment}</section>',
                            "text/html",
                        ),
                        "answer_key_file": (
                            f"decode-bound-{index}.json",
                            one_question_answer_key(f"Rejected route {index}"),
                            "application/json",
                        ),
                    },
                )
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual(MATH_FLOOR_DIVISION_ERROR, response.json()["detail"])
                self.assertEqual(before, persisted_state())

        staged_html = app.QUESTION_BANKS_DIR / "decode-bound-staged.html"
        staged_answer = app.QUESTION_BANKS_DIR / "decode-bound-staged.json"
        staged_html.parent.mkdir(parents=True, exist_ok=True)
        staged_html.write_text(
            '<section data-question-key="bad-q">'
            '<code class="math-floor-division">a // b</code><p>'
            + nested_pseudo_math("alternating", 64)
            + "</p></section>",
            encoding="utf-8",
        )
        staged_answer.write_text(
            one_question_answer_key("Rejected deep staged route"), encoding="utf-8"
        )
        before = persisted_state()
        response = self.client.post(
            "/api/admin/question-banks/import-from-folder",
            json={
                "html_filename": staged_html.name,
                "answer_key_filename": staged_answer.name,
            },
        )
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(MATH_FLOOR_DIVISION_ERROR, response.json()["detail"])
        self.assertEqual(before, persisted_state())

    def test_html_pair_import_rejects_decoded_code_pseudo_markup_before_writes(self):
        valid_math = '<code class="math-floor-division">a // b</code>'
        pseudo_math = '&lt;code class="math-floor-division"&gt;c // d&lt;/code&gt;'
        malformed_fragments = (
            valid_math + f"<p>{pseudo_math}</p>",
            f"<p>{pseudo_math}</p>" + valid_math,
            valid_math + f"<p>{pseudo_math}</p>" + valid_math,
            valid_math + "<p>&amp;lt;code class=\"math-floor-division\"&amp;gt;c // d"
            "&amp;lt;/code&amp;gt;</p>",
            valid_math + "<p>%3Ccode%20class%3D%22math-floor-division%22%3Ec%20%2F%2F%20d"
            "%3C%2Fcode%3E</p>",
            valid_math + "<p>&lt;&#99;ode class=\"math-floor-division\"&gt;c // d"
            "&lt;/code&gt;</p>",
            valid_math + '<p>&lt;code class="math-floor-division">c // d</code></p>',
            valid_math + '<p><code class="math-floor-division">c // d&lt;/code&gt;</p>',
            valid_math + "<p>&lt;CoDe class=\"math-floor-division\"&gt;c // d"
            "&lt;/cOdE&gt;</p>",
            valid_math + "<p>&lt;code class=\"math-floor-&#100;ivision\"&gt;c // d"
            "&lt;/code&gt;</p>",
            valid_math + '<p title="&lt;code class=&quot;math-floor-division&quot;&gt;'
            'c // d&lt;/code&gt;">safe text</p>',
            valid_math + '<svg aria-label="&lt;code class=&quot;math-floor-division&quot;&gt;'
            'c // d&lt;/code&gt;"><path d="M0 0"></path></svg>',
            valid_math + '<p class="math-floor-division">safe text</p>',
            valid_math + '<p>math-floor-division</p>',
            valid_math + '<p><co\u200dde class="math-floor-division">c // d</code></p>',
            valid_math + '<p><c o d e class="math-floor-division">c // d</code></p>',
            valid_math + '<p>%3Cc%6F\u200Dd%65 class="math-floor-division">c // d%3C/%63ode%3E</p>',
            '<code>c // d</code>',
            '<code>c /&#47; d</code>',
            '<code>c /\u200d / d</code>',
            '&lt;code&gt;c // d&lt;/code&gt;',
        )
        with app.db() as connection:
            baseline = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] for table in ("question_banks", "questions", "assessment_releases"))
        pack_dir = app.assessment_packs_dir()
        baseline_packs = set(pack_dir.glob("*.ksatpack")) if pack_dir.exists() else set()

        def assert_unchanged() -> None:
            with app.db() as connection:
                current = tuple(connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0] for table in (
                    "question_banks", "questions", "assessment_releases"
                ))
            self.assertEqual(baseline, current)
            current_packs = set(pack_dir.glob("*.ksatpack")) if pack_dir.exists() else set()
            self.assertEqual(baseline_packs, current_packs)

        for index, fragment in enumerate(malformed_fragments):
            with self.subTest(route="multipart", fragment=fragment):
                answer_key = json.dumps({
                    "bank_name": f"Rejected decoded math {index}",
                    "questions": [{
                        "key": "bad-q",
                        "category": "Quantitative Aptitude",
                        "chapter": "Arithmetic",
                        "difficulty": "Easy",
                        "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                        "correct_answer": "A",
                    }],
                })
                response = self.client.post(
                    "/api/admin/question-banks/import",
                    files={
                        "html_file": (
                            f"decoded-{index}.html",
                            f'<section data-question-key="bad-q">{fragment}</section>',
                            "text/html",
                        ),
                        "answer_key_file": (
                            f"decoded-{index}.json", answer_key, "application/json"
                        ),
                    },
                )
                self.assertEqual(400, response.status_code, response.text)
                assert_unchanged()

        staged_html = app.QUESTION_BANKS_DIR / "decoded-staged.html"
        staged_answer = app.QUESTION_BANKS_DIR / "decoded-staged.json"
        staged_html.parent.mkdir(parents=True, exist_ok=True)
        staged_html.write_text(
            f'<section data-question-key="bad-q">{valid_math}<p>{pseudo_math}</p></section>',
            encoding="utf-8",
        )
        staged_answer.write_text(json.dumps({
            "bank_name": "Rejected decoded staged math",
            "questions": [{
                "key": "bad-q",
                "category": "Quantitative Aptitude",
                "chapter": "Arithmetic",
                "difficulty": "Easy",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "A",
            }],
        }), encoding="utf-8")
        response = self.client.post(
            "/api/admin/question-banks/import-from-folder",
            json={
                "html_filename": staged_html.name,
                "answer_key_filename": staged_answer.name,
            },
        )
        self.assertEqual(400, response.status_code, response.text)
        assert_unchanged()

    def test_html_pair_import_route_preserves_valid_math_code_svg_and_pairing(self):
        html_source = (
            '<section data-question-key="math-one"><p>Compute '
            '<code class="math-floor-division">total // count</code>.</p>'
            '<p>AT&amp;T uses 3 &lt; 5; <code>alpha &amp; beta</code>.</p>'
            '<p>Discount token 50%2C stays ordinary prose.</p>'
            '<code>ordinary ÷ code</code><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">'
            '<path d="M0 0"></path></svg></section>'
            '<section data-question-key="math-two"><div>'
            '<code class="math-floor-division">6 // 2</code> then '
            '<code class="math-floor-division">items // groups</code>.</div></section>'
        )
        answer_key = json.dumps({
            "bank_name": "Valid raw math pairing",
            "questions": [
                {
                    "key": "math-two",
                    "category": "Quantitative Aptitude",
                    "chapter": "Arithmetic",
                    "difficulty": "Medium",
                    "options": {"A": "5", "B": "6", "C": "7", "D": "8"},
                    "correct_answer": "D",
                },
                {
                    "key": "math-one",
                    "category": "Quantitative Aptitude",
                    "chapter": "Arithmetic",
                    "difficulty": "Easy",
                    "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                    "correct_answer": "B",
                },
            ],
        })
        response = self.client.post(
            "/api/admin/question-banks/import",
            files={
                "html_file": ("valid-math.html", html_source, "text/html"),
                "answer_key_file": ("valid-math.json", answer_key, "application/json"),
            },
        )
        self.assertEqual(200, response.status_code, response.text)

        with app.db() as connection:
            stored = connection.execute(
                """SELECT source_key, question_text, question_html, correct_answer
                   FROM questions WHERE bank_id = ? ORDER BY source_key""",
                (response.json()["bank_id"],),
            ).fetchall()
        self.assertEqual(["math-one", "math-two"], [row["source_key"] for row in stored])
        self.assertEqual(["B", "D"], [row["correct_answer"] for row in stored])
        self.assertEqual(
            "Compute ⌊total ÷ count⌋ . AT&T uses 3 < 5; alpha & beta . "
            "Discount token 50%2C stays ordinary prose. ordinary ÷ code",
            stored[0]["question_text"],
        )
        self.assertIn(
            '<code class="math-floor-division">⌊total ÷ count⌋</code>',
            stored[0]["question_html"],
        )
        self.assertIn("<code>ordinary ÷ code</code>", stored[0]["question_html"])
        self.assertIn("AT&amp;T uses 3 &lt; 5", stored[0]["question_html"])
        self.assertIn("Discount token 50%2C stays ordinary prose.", stored[0]["question_html"])
        self.assertIn("<code>alpha &amp; beta</code>", stored[0]["question_html"])
        self.assertIn(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">',
            stored[0]["question_html"],
        )
        self.assertEqual(2, stored[1]["question_html"].count("math-floor-division"))
        self.assertNotIn("//", stored[1]["question_html"])

    def test_public_release_material_embeds_only_public_media(self):
        with app.db() as connection:
            selected = connection.execute(
                """SELECT question_id, category, chapter, stimulus_id
                   FROM questions WHERE question_id = ?""",
                (self.question_id,),
            ).fetchall()
            questions, assets = app.public_release_material(connection, selected)
        payload = questions[0].model_dump(mode="json", exclude_none=True)
        serialized = json.dumps(payload, sort_keys=True).lower()
        expected_assets = {
            f"assets/{hashlib.sha256(self.public_media).hexdigest()}.png": self.public_media,
            f"assets/{hashlib.sha256(self.stimulus_media).hexdigest()}.png": self.stimulus_media,
        }
        self.assertEqual(expected_assets, assets)
        self.assertEqual("<p>What is shown?</p>", payload["question_html"])
        self.assertEqual("assets/" + self.stimulus_filename, payload["stimulus"]["url"])
        self.assertEqual("assets/" + self.public_filename, payload["display_media"]["question"]["url"])
        for forbidden in ("solution", "answer", "explanation", "feedback", "/api/"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn(self.solution_media, assets.values())

    def test_public_release_material_preserves_empty_legacy_table_content(self):
        with app.db() as connection:
            connection.execute(
                """UPDATE stimuli
                   SET stimulus_type = 'table', asset_filename = NULL, content_json = '{}'
                   WHERE bank_id = ? AND stimulus_id = 'stimulus-1'""",
                (self.bank_id,),
            )
            selected = connection.execute(
                """SELECT question_id, category, chapter, stimulus_id
                   FROM questions WHERE question_id = ?""",
                (self.question_id,),
            ).fetchall()
            questions, assets = app.public_release_material(connection, selected)
        payload = questions[0].model_dump(mode="json", exclude_none=True)
        self.assertEqual(
            {"id": "stimulus-1", "type": "table", "title": "Prompt chart", "alt_text": "Public chart", "content": {}},
            payload["stimulus"],
        )
        self.assertEqual(
            {f"assets/{hashlib.sha256(self.public_media).hexdigest()}.png": self.public_media},
            assets,
        )

    def test_html_pair_math_import_survives_storage_public_material_and_real_pack(self):
        html_source = (
            '<section data-question-key="math-q"><p>Compute '
            '<code class="math-floor-division">total // count</code> now.</p></section>'
        )
        answer_key_source = json.dumps({
            "bank_name": "Imported explicit math",
            "questions": [{
                "key": "math-q",
                "category": "Quantitative Aptitude",
                "chapter": "Arithmetic",
                "difficulty": "Easy",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "A",
            }],
        })

        bank_name, imported = app.parse_question_bank(html_source, answer_key_source)
        expected_html = (
            '<p>Compute <code class="math-floor-division">⌊total ÷ count⌋</code> now.</p>'
        )
        self.assertEqual(expected_html, imported[0]["question_html"])
        self.assertEqual("Compute ⌊total ÷ count⌋ now.", imported[0]["question_text"])
        saved = app.save_question_bank(bank_name, imported, "math.html", "math.json")

        with app.db() as connection:
            stored = connection.execute(
                "SELECT * FROM questions WHERE bank_id = ? AND source_key = 'math-q'",
                (saved["bank_id"],),
            ).fetchone()
            self.assertEqual(expected_html, stored["question_html"])
            self.assertEqual("Compute ⌊total ÷ count⌋ now.", stored["question_text"])
            public_questions, assets = app.public_release_material(connection, [stored])
            test_id = connection.execute(
                "INSERT INTO tests (test_name, composition, bank_id, created_at) VALUES (?, '[]', ?, ?)",
                ("Imported math release", saved["bank_id"], app.now()),
            ).lastrowid
            config = app.app.state.coordinator_config
            release = prepare_release(
                connection,
                test_id=test_id,
                selected_questions=public_questions,
                assets=assets,
                pack_dir=app.assessment_packs_dir(),
                signing_private_key_b64=config.signing_private_key_b64,
                pack_master_key=config.pack_master_key,
                now_iso=app.now(),
            )

        content_key = unwrap_release_content_key(
            config.pack_master_key, release.release_id, release.wrapped_content_key_b64
        )
        encrypted = (app.assessment_packs_dir() / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(
            content_key, release.release_id, encrypted
        ))) as archive:
            packed = json.loads(archive.read("questions.json"))[0]
        self.assertEqual("Compute ⌊total ÷ count⌋ now.", packed["question_text"])
        self.assertEqual(expected_html, packed["question_html"])
        self.assertNotIn("//", packed["question_text"])
        self.assertNotIn("//", packed["question_html"])
        with app.db() as connection:
            reloaded = load_release_manifest(
                connection,
                release.release_id,
                pack_dir=app.assessment_packs_dir(),
                signing_public_key_b64=config.signing_public_key_b64,
                pack_master_key=config.pack_master_key,
            )
        self.assertEqual(release, reloaded)

    def test_create_and_legacy_launch_prepare_once_without_resampling_history(self):
        created = self.client.post("/api/admin/tests", json=self.create_payload())
        self.assertEqual(200, created.status_code, created.text)
        created_test_id = created.json()["test_id"]
        listing = self.client.get("/api/admin/tests")
        self.assertEqual(200, listing.status_code, listing.text)
        item = next(test for test in listing.json()["tests"] if test["test_id"] == created_test_id)
        self.assertEqual("prepared", item["release_state"])
        self.assertNotIn("content_hash", item)
        self.assertEqual(12, len(item["content_hash_prefix"]))
        self.assertTrue(item["release_id"])
        with app.db() as connection:
            frozen_release = connection.execute(
                "SELECT manifest_json,wrapped_review_key_b64 FROM assessment_releases WHERE release_id=?",
                (item["release_id"],),
            ).fetchone()
            frozen_question = connection.execute(
                "SELECT solution_steps_json FROM release_questions WHERE release_id=?",
                (item["release_id"],),
            ).fetchone()
        self.assertEqual(2, json.loads(frozen_release["manifest_json"])["pack_format_version"])
        self.assertTrue(frozen_release["wrapped_review_key_b64"])
        self.assertTrue(frozen_question["solution_steps_json"])
        pack_path = app.DATA_DIR / "Assessment Releases" / f"{item['release_id']}.ksatpack"
        pack_bytes = pack_path.read_bytes()
        pack_mtime = pack_path.stat().st_mtime_ns

        launched = self.client.post(f"/api/admin/tests/{created_test_id}/launch")
        self.assertEqual(200, launched.status_code, launched.text)
        relaunched = self.client.post(f"/api/admin/tests/{created_test_id}/launch")
        self.assertEqual(200, relaunched.status_code, relaunched.text)
        self.assertEqual(pack_bytes, pack_path.read_bytes())
        self.assertEqual(pack_mtime, pack_path.stat().st_mtime_ns)

        with app.db() as connection:
            legacy_test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, bank_id, created_at, active, launched, mode, difficulties)
                   VALUES ('Legacy Unlaunched', ?, ?, ?, 1, 0, 'faculty', '[\"Easy\"]')""",
                (json.dumps(self.create_payload()["selection_rules"]), self.bank_id, app.now()),
            ).lastrowid
            historical_test_id = connection.execute(
                """INSERT INTO tests
                   (test_name, composition, bank_id, created_at, active, launched, mode, difficulties)
                   VALUES ('Historical Submitted', ?, ?, ?, 1, 0, 'faculty', '[\"Easy\"]')""",
                (json.dumps(self.create_payload()["selection_rules"]), self.bank_id, app.now()),
            ).lastrowid
            connection.execute(
                """INSERT INTO students
                   (student_id, name, password_hash, class, section, created_at)
                   VALUES ('S-HISTORY', 'History', 'hash', 'AIML', 'A', ?)""",
                (app.now(),),
            )
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, started_at, submitted_at, status, total_questions)
                   VALUES ('historical-attempt', 'S-HISTORY', ?, ?, ?, 'submitted', 1)""",
                (historical_test_id, app.now(), app.now()),
            )

        state_listing = self.client.get("/api/admin/tests")
        historical_state = next(
            test for test in state_listing.json()["tests"] if test["test_id"] == historical_test_id
        )
        self.assertEqual("failed", historical_state["release_state"])
        legacy_launch = self.client.post(f"/api/admin/tests/{legacy_test_id}/launch")
        self.assertEqual(200, legacy_launch.status_code, legacy_launch.text)
        historical_launch = self.client.post(f"/api/admin/tests/{historical_test_id}/launch")
        self.assertEqual(409, historical_launch.status_code, historical_launch.text)
        with app.db() as connection:
            legacy_release_id = connection.execute(
                "SELECT release_id FROM tests WHERE test_id = ?", (legacy_test_id,)
            ).fetchone()[0]
            historical = connection.execute(
                "SELECT release_id FROM tests WHERE test_id = ?", (historical_test_id,)
            ).fetchone()[0]
            historical_attempts = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE test_id = ? AND status = 'submitted'",
                (historical_test_id,),
            ).fetchone()[0]
        self.assertTrue(legacy_release_id)
        self.assertIsNone(historical)
        self.assertEqual(1, historical_attempts)

    def test_referenced_release_cannot_relaunch_or_rewrite_attempt_deadline(self):
        created = self.client.post("/api/admin/tests", json=self.create_payload("One shot"))
        self.assertEqual(200, created.status_code, created.text)
        test_id = created.json()["test_id"]
        release_id = created.json()["release_id"]
        first_launch = self.client.post(f"/api/admin/tests/{test_id}/launch")
        self.assertEqual(200, first_launch.status_code, first_launch.text)
        fixed_deadline = "2026-09-01T10:00:00+00:00"
        with app.db() as connection:
            connection.execute(
                """INSERT INTO students
                   (student_id, name, password_hash, class, section, created_at)
                   VALUES ('S-ISSUED', 'Issued', 'hash', 'AIML', 'A', ?)""",
                (app.now(),),
            )
            connection.execute(
                """INSERT INTO attempts
                   (attempt_id, student_id, test_id, release_id, started_at, status,
                    total_questions, expires_at)
                   VALUES ('issued-attempt', 'S-ISSUED', ?, ?, ?, 'in_progress', 1, ?)""",
                (test_id, release_id, app.now(), fixed_deadline),
            )
            launch_deadline = connection.execute(
                "SELECT launch_expires_at FROM tests WHERE test_id = ?", (test_id,)
            ).fetchone()[0]
        relaunch = self.client.post(f"/api/admin/tests/{test_id}/launch")
        self.assertEqual(409, relaunch.status_code, relaunch.text)
        with app.db() as connection:
            unchanged = connection.execute(
                "SELECT expires_at FROM attempts WHERE attempt_id = 'issued-attempt'"
            ).fetchone()[0]
            unchanged_launch = connection.execute(
                "SELECT launch_expires_at FROM tests WHERE test_id = ?", (test_id,)
            ).fetchone()[0]
        self.assertEqual(fixed_deadline, unchanged)
        self.assertEqual(launch_deadline, unchanged_launch)

    def test_create_commit_failure_rolls_back_rows_and_removes_only_owned_pack(self):
        @contextmanager
        def database_with_failing_commit():
            connection = connect_sqlite(app.DB_PATH)
            try:
                yield connection
                connection.rollback()
                raise sqlite3.OperationalError("forced commit failure")
            finally:
                connection.close()

        with patch("app.db", database_with_failing_commit):
            response = self.client.post(
                "/api/admin/tests", json=self.create_payload("Commit failure")
            )
        self.assertEqual(500, response.status_code, response.text)
        self.assertEqual([], list(app.assessment_packs_dir().glob("*.ksatpack")))
        with app.db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM tests WHERE test_name = 'Commit failure'"
                ).fetchone()
            )

    def test_private_snapshot_insert_failure_rolls_back_release_and_owned_pack(self):
        with app.db() as connection:
            connection.execute(
                """CREATE TRIGGER reject_complete_private_snapshot
                   BEFORE INSERT ON release_questions
                   WHEN NEW.correct_answer IS NOT NULL
                   BEGIN SELECT RAISE(ABORT, 'injected private snapshot failure'); END"""
            )

        response = self.client.post(
            "/api/admin/tests", json=self.create_payload("Snapshot failure")
        )

        self.assertEqual(500, response.status_code, response.text)
        self.assertEqual([], list(app.assessment_packs_dir().glob("*.ksatpack")))
        with app.db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM tests WHERE test_name='Snapshot failure'"
                ).fetchone()
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM assessment_releases"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM release_questions").fetchone()[0],
            )

    def test_delete_prepared_test_removes_release_rows_and_pack(self):
        created = self.client.post("/api/admin/tests", json=self.create_payload("Delete release"))
        self.assertEqual(200, created.status_code, created.text)
        test_id = created.json()["test_id"]
        release_id = created.json()["release_id"]
        pack_path = app.assessment_packs_dir() / f"{release_id}.ksatpack"
        self.assertTrue(pack_path.is_file())
        deleted = self.client.delete(f"/api/admin/tests/{test_id}")
        self.assertEqual(200, deleted.status_code, deleted.text)
        with app.db() as connection:
            counts = (
                connection.execute("SELECT COUNT(*) FROM assessment_releases WHERE release_id = ?", (release_id,)).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM release_questions WHERE release_id = ?", (release_id,)).fetchone()[0],
            )
        self.assertEqual((0, 0), counts)
        self.assertFalse(pack_path.exists())


if __name__ == "__main__":
    unittest.main()
