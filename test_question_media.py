from __future__ import annotations

import base64
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import question_media


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class DisplayMediaTests(unittest.TestCase):
    def package_with(self, assets: dict[str, bytes]) -> io.BytesIO:
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as writer:
            for filename, content in assets.items():
                writer.writestr(filename, content)
        package.seek(0)
        return package

    def parse(self, raw: dict, assets: dict[str, bytes]) -> dict:
        package = self.package_with(assets)
        with zipfile.ZipFile(package, "r") as archive:
            members = {item.filename: item for item in archive.infolist()}
            return question_media.parse_display_media(
                raw, archive=archive, members=members, question_key="ch01-q0334"
            )

    def test_parse_display_media_accepts_question_option_and_solution_pngs(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()
        raw = {
            "question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "seven to the power eighty-four"},
            "options": {"D": {"asset": "assets/d.png", "sha256": digest, "alt_text": "one divided by x squared"}},
            "solution": [{"asset": "assets/s.png", "sha256": digest, "alt_text": "textbook solution"}],
        }

        parsed = self.parse(raw, {
            "assets/q.png": PNG_1X1,
            "assets/d.png": PNG_1X1,
            "assets/s.png": PNG_1X1,
        })

        self.assertEqual(parsed["question"]["width"], 1)
        self.assertEqual(parsed["options"]["D"]["sha256"], digest)
        self.assertEqual(len(parsed["solution"]), 1)

    def test_parse_display_media_rejects_missing_alt_text(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()

        with self.assertRaisesRegex(ValueError, "requires alt_text"):
            self.parse({"question": {"asset": "assets/q.png", "sha256": digest}}, {"assets/q.png": PNG_1X1})

    def test_parse_display_media_rejects_mismatched_hash(self):
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            self.parse(
                {"question": {"asset": "assets/q.png", "sha256": "0" * 64, "alt_text": "one pixel"}},
                {"assets/q.png": PNG_1X1},
            )

    def test_parse_display_media_rejects_non_png_payload(self):
        payload = b"not an image"
        digest = hashlib.sha256(payload).hexdigest()

        with self.assertRaisesRegex(ValueError, "valid PNG"):
            self.parse(
                {"question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "not an image"}},
                {"assets/q.png": payload},
            )

    def test_parse_display_media_rejects_escape_path(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()

        with self.assertRaisesRegex(ValueError, "safe forward-slash"):
            self.parse(
                {"question": {"asset": "../escape.png", "sha256": digest, "alt_text": "escape"}},
                {"../escape.png": PNG_1X1},
            )

    def test_parse_display_media_rejects_zero_dimension(self):
        zero_width = PNG_1X1[:16] + b"\x00\x00\x00\x00" + PNG_1X1[20:]
        digest = hashlib.sha256(zero_width).hexdigest()

        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.parse(
                {"question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "zero width"}},
                {"assets/q.png": zero_width},
            )

    def test_parse_display_media_rejects_dimension_above_limit(self):
        too_wide = PNG_1X1[:16] + b"\x00\x00'\x11" + PNG_1X1[20:]
        digest = hashlib.sha256(too_wide).hexdigest()

        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.parse(
                {"question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "too wide"}},
                {"assets/q.png": too_wide},
            )

    def test_parse_display_media_rejects_option_outside_a_to_e(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()

        with self.assertRaisesRegex(ValueError, "A-E"):
            self.parse(
                {"options": {"F": {"asset": "assets/f.png", "sha256": digest, "alt_text": "invalid option"}}},
                {"assets/f.png": PNG_1X1},
            )

    def test_parse_display_media_rejects_payload_above_limit(self):
        payload = PNG_1X1 + (b"x" * 15_000_000)
        digest = hashlib.sha256(payload).hexdigest()

        with self.assertRaisesRegex(ValueError, "missing or too large"):
            self.parse(
                {"question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "large image"}},
                {"assets/q.png": payload},
            )

    def test_stored_public_media_excludes_solution_when_answers_are_hidden(self):
        digest = hashlib.sha256(PNG_1X1).hexdigest()
        parsed = self.parse(
            {
                "question": {"asset": "assets/q.png", "sha256": digest, "alt_text": "question image"},
                "solution": [{"asset": "assets/s.png", "sha256": digest, "alt_text": "solution image"}],
            },
            {"assets/q.png": PNG_1X1, "assets/s.png": PNG_1X1},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            stored = question_media.store_display_media(parsed, asset_dir=Path(temporary_directory))

            self.assertTrue((Path(temporary_directory) / stored["question"]["asset_filename"]).is_file())
            self.assertTrue(question_media.media_owns_filename(
                __import__("json").dumps(stored), stored["question"]["asset_filename"]
            ))
            public = question_media.public_display_media(stored, bank_id=19, include_solution=False)

        self.assertEqual(public["question"]["asset_url"], "/api/question-assets/19/" + stored["question"]["asset_filename"])
        self.assertNotIn("solution", public)


if __name__ == "__main__":
    unittest.main()
