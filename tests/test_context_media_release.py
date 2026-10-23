import base64
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app
from ksat.coordinator.releases import (
    load_release_manifest,
    prepare_release,
    unwrap_release_content_key,
)
from ksat.crypto import decrypt_pack, generate_ed25519_keypair
from ksat.protocol import PublicImageStimulus, PublicQuestion


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
STEM = "DIRECTIONS: Study the diagram.\n\nWhat percentage is shaded?"
HTML = "<p>DIRECTIONS: Study the diagram.</p><p>What percentage is shaded?</p>"


class ContextMediaReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(directory)
        self.enterContext(patch.multiple(
            app, DATA_DIR=root, DB_PATH=root / "aptitude.db",
            BACKUP_DIR=root / "backups", QUESTION_BANKS_DIR=root / "Question Banks",
        ))
        app.ensure_schema()

    def import_question(self, bank_name="Context bank", key="ch38-q0001",
                        placement="context", stimulus=False):
        # Exercise the package parser and storage, including placement persistence.
        def media(filename, content, alt_text):
            return {"asset": filename, "sha256": hashlib.sha256(content).hexdigest(),
                    "alt_text": alt_text}

        question_media = media("assets/diagram.png", PNG, "Shaded circle")
        if placement is not None:
            question_media["placement"] = placement
        private_png = PNG + b"private worked diagram"
        entry = {
            "key": key, "category": "Quantitative Aptitude", "chapter": "Pie Charts",
            "difficulty": "Easy", "question_text": STEM, "question_html": HTML,
            "options": {"A": "25%", "B": "50%", "C": "75%", "D": "100%"},
            "correct_answer": "A", "solution_steps": ["One quarter is shaded."],
            "display_media": {
                "question": question_media,
                "options": {"A": media("assets/diagram.png", PNG, "One quarter")},
                "solution": [media("assets/worked.png", private_png, "Private working")],
            },
        }
        manifest = {"format_version": 3, "bank_name": bank_name,
                    "question_files": ["questions.json"]}
        if stimulus:
            entry["stimulus_id"] = "existing-table"
            manifest["stimuli"] = [{"id": "existing-table", "type": "table",
                                    "title": "Existing data", "content": {}}]
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("questions.json", json.dumps([entry]))
            archive.writestr("assets/diagram.png", PNG)
            archive.writestr("assets/worked.png", private_png)
        package.seek(0)
        name, questions, stimuli, version = app.parse_question_package(package)
        saved = app.save_question_package(name, questions, stimuli, "context.zip", version)
        with app.db() as connection:
            return connection.execute(
                "SELECT * FROM questions WHERE bank_id = ?", (saved["bank_id"],)
            ).fetchone()

    def test_classic_media_preserves_stored_context_and_solution_access_gate(self):
        row = self.import_question()
        stored = json.loads(row["display_media_json"])
        self.assertEqual("context", stored["question"]["placement"])
        media = app.display_media_for_attempt(
            row["display_media_json"], row["bank_id"], include_solution=False,
        )
        self.assertEqual("context", media["question"].get("placement"))
        self.assertEqual(
            f"/api/question-banks/{row['bank_id']}/media/{hashlib.sha256(PNG).hexdigest()}.png",
            media["question"]["url"],
        )
        self.assertNotIn("placement", media["options"]["A"])
        self.assertNotIn("solution", media)
        review = app.display_media_for_attempt(
            row["display_media_json"], row["bank_id"], include_solution=True, attempt_id="attempt-1",
        )
        self.assertTrue(review["solution"][0]["url"].endswith("?attempt_id=attempt-1"))
        self.assertNotIn("solution", app.display_media_for_attempt(
            row["display_media_json"], row["bank_id"], include_solution=True,
        ))

    def test_stored_context_survives_public_protocol_and_real_encrypted_pack(self):
        row = self.import_question()
        private_key, public_key = generate_ed25519_keypair()
        master_key = bytes(range(32))
        with app.db() as connection:
            questions, assets = app.public_release_material(connection, [row])
            wire = questions[0].model_dump_json(exclude_none=True)
            public = PublicQuestion.model_validate_json(wire, strict=True)
            self.assertIsInstance(public.stimulus, PublicImageStimulus)
            self.assertIsNone(public.display_media.question)
            self.assertEqual(STEM, public.question_text)
            self.assertEqual(HTML, public.question_html)
            self.assertEqual("Shaded circle", public.stimulus.alt_text)
            self.assertNotIn("placement", json.loads(wire)["stimulus"])
            test_id = connection.execute(
                "INSERT INTO tests (test_name, composition, bank_id, created_at) VALUES (?, '[]', ?, ?)",
                ("Context release", row["bank_id"], app.now()),
            ).lastrowid
            release = prepare_release(
                connection, test_id=test_id, selected_questions=questions, assets=assets,
                review_questions=app.frozen_review_material([row]),
                pack_dir=app.assessment_packs_dir(), signing_private_key_b64=private_key,
                pack_master_key=master_key, now_iso=app.now(),
            )
            verified = load_release_manifest(
                connection, release.release_id, pack_dir=app.assessment_packs_dir(),
                signing_public_key_b64=public_key, pack_master_key=master_key,
            )
        self.assertEqual(release, verified)
        key = unwrap_release_content_key(master_key, release.release_id, release.wrapped_content_key_b64)
        encrypted = (app.assessment_packs_dir() / release.content_pack_filename).read_bytes()
        with zipfile.ZipFile(io.BytesIO(decrypt_pack(key, release.release_id, encrypted))) as archive:
            payload = json.loads(archive.read("questions.json"))[0]
            packed = PublicQuestion.model_validate(payload, strict=True)
            self.assertEqual(STEM, packed.question_text)
            self.assertEqual(HTML, packed.question_html)
            self.assertIsNone(packed.display_media.question)
            self.assertEqual(public.stimulus, packed.stimulus)
            self.assertEqual(PNG, archive.read(packed.stimulus.url))
            self.assertEqual(PNG, archive.read(packed.display_media.options["A"].url))
            self.assertEqual(
                [packed.stimulus.url], json.loads(archive.read("manifest.json"))["asset_names"],
            )
        self.assertEqual({packed.stimulus.url: PNG}, assets)
        for private_field in ("correct_answer", "solution_steps", "explanation", "solution"):
            self.assertNotIn(private_field, json.dumps(payload))
        self.assertNotIn("placement", json.dumps(payload))

    def test_context_identity_is_stable_per_bank_and_source_key(self):
        first = self.import_question()
        other_bank = self.import_question(bank_name="Other bank")
        with app.db() as connection:
            # Same image, different question key: each must get its own identity.
            connection.execute(
                "UPDATE questions SET source_key = 'ch38-q0002' WHERE question_id = ?",
                (other_bank["question_id"],),
            )
            single, _ = app.public_release_material(connection, [first])
            mixed, _ = app.public_release_material(connection, [other_bank, first])
            self.assertIsNotNone(single[0].stimulus)
            self.assertIsNotNone(mixed[0].stimulus)
            self.assertEqual(single[0].stimulus.id, mixed[1].stimulus.id)
            self.assertNotEqual(single[0].stimulus.id, mixed[0].stimulus.id)
            previous_id = mixed[0].stimulus.id
            connection.execute(
                "UPDATE questions SET source_key = ? WHERE question_id = ?",
                (first["source_key"], other_bank["question_id"]),
            )
            changed, _ = app.public_release_material(connection, [other_bank])
            self.assertNotEqual(previous_id, changed[0].stimulus.id)
            self.assertNotEqual(single[0].stimulus.id, changed[0].stimulus.id)

    def test_context_with_existing_or_unresolved_stimulus_fails_explicitly(self):
        row = self.import_question(stimulus=True)
        with app.db() as connection:
            for stimulus_id in ("existing-table", "unresolved-stimulus"):
                with self.subTest(stimulus_id=stimulus_id):
                    connection.execute(
                        "UPDATE questions SET stimulus_id = ? WHERE question_id = ?",
                        (stimulus_id, row["question_id"]),
                    )
                    with self.assertRaisesRegex(ValueError, "[Cc]ontext.*stimulus") as error:
                        app.public_release_material(connection, [row])
                    self.assertIn(row["source_key"], str(error.exception))

    def test_replacement_media_and_existing_stimulus_keep_legacy_shape(self):
        for placement in (None, "replacement"):
            with self.subTest(placement=placement):
                row = self.import_question(bank_name=f"Replacement {placement}",
                                           placement=placement, stimulus=True)
                with app.db() as connection:
                    questions, assets = app.public_release_material(connection, [row])
                payload = questions[0].model_dump(mode="json", exclude_none=True)
                self.assertEqual("existing-table", payload["stimulus"]["id"])
                self.assertEqual("table", payload["stimulus"]["type"])
                self.assertEqual(
                    {"url", "alt_text", "width", "height"},
                    set(payload["display_media"]["question"]),
                )
                self.assertEqual(PNG, assets[payload["display_media"]["question"]["url"]])
                self.assertEqual(STEM, payload["question_text"])
                self.assertNotIn("placement", json.dumps(payload))
