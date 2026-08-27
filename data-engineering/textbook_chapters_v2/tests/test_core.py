from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import CropBox, SourceCrop
from textbook_chapters_v2.store import ArtifactStore, canonical_json, dependency_fingerprint


class CoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "work"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_canonical_json_and_dependency_fingerprint_have_literal_stable_values(self) -> None:
        self.assertEqual(
            canonical_json({"b": 2, "a": "7⁸⁴"}),
            b'{"a":"7\xe2\x81\xb8\xe2\x81\xb4","b":2}',
        )
        self.assertEqual(
            dependency_fingerprint({"crop": "a", "policy": 1}),
            "2e3600fc57b23766f19919e0cb78af5a36f1cd259143bef2853cb00c0c68b15b",
        )

    def test_cache_reuses_only_identical_dependencies(self) -> None:
        store = ArtifactStore(self.root)
        ref = store.write_json(
            "extract",
            "ch01-q0334",
            {"question_text": "7⁸⁴"},
            {"crop": "a", "policy": 1},
        )

        self.assertTrue(ref.path.is_file())
        self.assertIsNotNone(
            store.read_if_current(
                "extract", "ch01-q0334", {"crop": "a", "policy": 1}
            )
        )
        self.assertIsNone(
            store.read_if_current(
                "extract", "ch01-q0334", {"crop": "a", "policy": 2}
            )
        )
        self.assertEqual(
            json.loads(ref.path.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "stage": "extract",
                "key": "ch01-q0334",
                "dependency_fingerprint": "2e3600fc57b23766f19919e0cb78af5a36f1cd259143bef2853cb00c0c68b15b",
                "payload_sha256": "3805b0899963be801fdaa831423373eb9e373294cc6220f85b10a36f8b35748c",
                "payload": {"question_text": "7⁸⁴"},
            },
        )

    def test_store_refuses_path_escape_and_returns_a_fresh_payload(self) -> None:
        store = ArtifactStore(self.root)
        with self.assertRaisesRegex(ValueError, "stage"):
            store.write_json("../escape", "ch01-q0334", {}, {})
        with self.assertRaisesRegex(ValueError, "key"):
            store.write_json("extract", "nested/ch01-q0334", {}, {})

        store.write_json("extract", "ch01-q0334", {"options": {"A": "1"}}, {})
        payload = store.read_if_current("extract", "ch01-q0334", {})
        assert payload is not None
        payload["options"]["A"] = "changed"
        self.assertEqual(
            store.read_if_current("extract", "ch01-q0334", {}),
            {"options": {"A": "1"}},
        )

    def test_chapter_config_allows_shared_boundary_and_rejects_incomplete_ranges(self) -> None:
        config = ChapterConfig.from_dict(
            {
                "chapter": 1,
                "question_pages": [23, 40],
                "answer_pages": [40, 41],
                "solution_pages": [42, 59],
                "question_numbers": [1, 3],
            }
        )
        self.assertEqual(config.question_pages, (23, 40))
        self.assertEqual(config.answer_pages, (40, 41))
        with self.assertRaisesRegex(ValueError, "question_pages"):
            ChapterConfig.from_dict({"chapter": 1, "question_pages": [40, 23]})

    def test_chapter_config_is_frozen_and_exposes_immutable_collections(self) -> None:
        config = ChapterConfig.from_dict(
            {
                "chapter": 1,
                "bank_name": "Number System",
                "question_pages": [23, 40],
                "answer_pages": [41, 41],
                "solution_pages": [42, 59],
                "question_numbers": [1, 3],
                "intentional_exclusions": [2],
                "marker_overrides": {"1": {"page": 23, "top": 40}},
            }
        )

        self.assertEqual(config.question_numbers, (1, 3))
        self.assertEqual(config.intentional_exclusions, (2,))
        with self.assertRaises(FrozenInstanceError):
            config.chapter = 2  # type: ignore[misc]
        with self.assertRaises(TypeError):
            config.marker_overrides["1"] = {}  # type: ignore[index]

    def test_cross_stage_models_are_frozen_and_copy_collections(self) -> None:
        box = CropBox(0, 40, 100, 120)
        crop = SourceCrop(
            role="question",
            question_number=1,
            page_number=23,
            box=box,
            path=Path("crop.png"),
            width=100,
            height=80,
            sha256="a" * 64,
            source_image_sha256="b" * 64,
            source_dpi=180,
        )
        self.assertEqual(crop.box, box)
        self.assertEqual((crop.source_image_sha256, crop.source_dpi), ("b" * 64, 180))
        with self.assertRaises(FrozenInstanceError):
            crop.width = 101  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
