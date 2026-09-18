from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.relabel_question_package import relabel_package


class RelabelQuestionPackageTests(unittest.TestCase):
    def test_relabels_manifest_and_question_categories_without_changing_other_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            output = root / "output.zip"
            manifest = {
                "format_version": 3,
                "bank_name": "Original bank",
                "question_files": ["questions/ch01.jsonl"],
            }
            question = {"key": "q1", "category": "Original bank", "question": "2 + 2?"}
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.writestr("questions/ch01.jsonl", json.dumps(question) + "\n")
                archive.writestr("metadata/keep.txt", b"unchanged")

            relabel_package(source, output, "Comparison bank")

            with zipfile.ZipFile(output) as archive:
                updated_manifest = json.loads(archive.read("manifest.json"))
                updated_question = json.loads(archive.read("questions/ch01.jsonl"))
                self.assertEqual(updated_manifest["bank_name"], "Comparison bank")
                self.assertEqual(updated_question["category"], "Comparison bank")
                self.assertEqual(archive.read("metadata/keep.txt"), b"unchanged")
            with zipfile.ZipFile(source) as archive:
                self.assertEqual(json.loads(archive.read("manifest.json"))["bank_name"], "Original bank")

    def test_refuses_to_overwrite_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.zip"
            output = root / "output.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"format_version": 3, "bank_name": "Old", "question_files": []}),
                )
            output.write_bytes(b"existing")

            with self.assertRaises(FileExistsError):
                relabel_package(source, output, "New")


if __name__ == "__main__":
    unittest.main()
