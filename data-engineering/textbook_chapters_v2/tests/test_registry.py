from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


DATA_ENGINEERING_ROOT = Path(__file__).resolve().parents[2]
if str(DATA_ENGINEERING_ROOT) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING_ROOT))

from textbook_chapters_v2.cli import main
from textbook_chapters_v2.registry import classify_field, inventory_banks


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bank(path: Path, chapter: int, questions: list[dict]) -> None:
    manifest = {
        "bank_name": f"Chapter {chapter}",
        "format_version": 3,
        "question_files": [f"questions/ch{chapter:02d}.jsonl"],
    }
    lines = "".join(
        json.dumps(question, ensure_ascii=False, sort_keys=True) + "\n"
        for question in questions
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        archive.writestr(f"questions/ch{chapter:02d}.jsonl", lines)


class RenderingRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_math_categories_preserve_text_and_distinguish_rendering_needs(self) -> None:
        self.assertEqual(classify_field("112 × 5⁴")["category"], "unicode_math")
        self.assertEqual(classify_field("2.6\u03054\u0305")["category"], "structured_math")
        self.assertEqual(classify_field("2.64")["category"], "plain_text")
        self.assertEqual(classify_field("graph", spatial=True)["category"], "source_visual")
        self.assertEqual(classify_field("See the graph below")["category"], "review_needed")
        self.assertEqual(classify_field("A | B\n1 | 2")["category"], "structured_table")
        self.assertEqual(classify_field("112 × 5⁴")["text"], "112 × 5⁴")

    def test_inventory_uses_chapter_scoped_keys_and_does_not_mutate_archives(self) -> None:
        bank_one = self.root / "ch01_first_all_vision_text_only.zip"
        bank_two = self.root / "ch02_second_all_vision_text_only.zip"
        _write_bank(
            bank_one,
            1,
            [{
                "key": "ch01-q0001", "chapter": "1", "question_text": "2.64",
                "options": {"A": "1", "B": "2"}, "correct_answer": "A",
                "solution_steps": ["Use place value."],
            }],
        )
        _write_bank(
            bank_two,
            2,
            [{
                "key": "ch02-q0001", "chapter": "2", "question_text": "112 × 5⁴",
                "options": {"A": "7000", "B": "70000"}, "correct_answer": "B",
                "solution_steps": ["112 × 625 = 70000"],
            }],
        )
        before = {path.name: _sha256(path) for path in (bank_one, bank_two)}

        inventory = inventory_banks(self.root)

        self.assertEqual(inventory["bank_count"], 2)
        self.assertEqual(inventory["question_count"], 2)
        self.assertEqual(set(inventory["records"]), {"ch01-q0001", "ch02-q0001"})
        self.assertEqual(
            inventory["records"]["ch02-q0001"]["fields"]["question"]["category"],
            "unicode_math",
        )
        self.assertEqual(
            before,
            {path.name: _sha256(path) for path in (bank_one, bank_two)},
        )

    def test_inventory_rejects_duplicate_source_keys(self) -> None:
        bank_one = self.root / "ch01_first_all_vision_text_only.zip"
        bank_two = self.root / "ch02_second_all_vision_text_only.zip"
        question = {
            "key": "ch01-q0001", "chapter": "1", "question_text": "One",
            "options": {"A": "1"}, "correct_answer": "A", "solution_steps": [],
        }
        _write_bank(bank_one, 1, [question])
        _write_bank(bank_two, 2, [question])

        with self.assertRaisesRegex(ValueError, "Duplicate source key"):
            inventory_banks(self.root)

    def test_inventory_cli_writes_canonical_sidecar_only(self) -> None:
        bank = self.root / "ch01_first_all_vision_text_only.zip"
        output = self.root / "work" / "rendering-registry.json"
        _write_bank(
            bank,
            1,
            [{
                "key": "ch01-q0001", "chapter": "1", "question_text": "Question",
                "options": {"A": "1"}, "correct_answer": "A", "solution_steps": [],
            }],
        )
        before = _sha256(bank)

        exit_code = main(["inventory", "--bank-dir", str(self.root), "--output", str(output)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(_sha256(bank), before)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["bank_count"], 1)
        self.assertEqual(payload["records"]["ch01-q0001"]["fields"]["question"]["text"], "Question")


if __name__ == "__main__":
    unittest.main()
