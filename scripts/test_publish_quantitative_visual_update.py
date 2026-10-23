"""Protect chapter bytes and provenance during outer README replacement."""
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import publish_quantitative_visual_update as publication


class PublicationTests(unittest.TestCase):
    def test_publication_rolls_back_if_second_replacement_fails(self):
        with tempfile.TemporaryDirectory(prefix="ksat-publication-rollback-") as temporary:
            output = Path(temporary)
            before = {"one.zip": b"old one", "two.zip": b"old two", "publication.json": b"old index"}
            for name, content in before.items():
                (output / name).write_bytes(content)
            original_replace = Path.replace
            failed = False
            def fail_second(path, destination):
                nonlocal failed
                if Path(destination) == output / "two.zip" and not failed:
                    failed = True
                    raise OSError("injected publication interruption")
                return original_replace(path, destination)
            with patch.object(Path, "replace", fail_second):
                with self.assertRaisesRegex(OSError, "injected publication interruption"):
                    publication.replace_outputs(output, {name: b"new " + data for name, data in before.items()})
            self.assertEqual(before, {name: (output / name).read_bytes() for name in before})
            publication.replace_outputs(output, {name: b"new " + data for name, data in before.items()})
            self.assertEqual({name: b"new " + data for name, data in before.items()},
                             {name: (output / name).read_bytes() for name in before})

    def test_failed_initial_publication_removes_only_new_outputs(self):
        with tempfile.TemporaryDirectory(prefix="ksat-publication-initial-") as temporary:
            output = Path(temporary)
            (output / "one.zip").write_bytes(b"original")
            original_replace = Path.replace
            def fail_index(path, destination):
                if Path(destination) == output / "publication.json":
                    raise OSError("index failure")
                return original_replace(path, destination)
            with patch.object(Path, "replace", fail_index):
                with self.assertRaisesRegex(OSError, "index failure"):
                    publication.replace_outputs(output, {"one.zip": b"new", "two.zip": b"new", "publication.json": b"new"})
            self.assertEqual((output / "one.zip").read_bytes(), b"original")
            self.assertFalse((output / "two.zip").exists())
            self.assertFalse((output / "publication.json").exists())

    def bundle(self, chapter=b"original chapter ZIP bytes", extra=False):
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w") as archive:
            archive.comment = b"container history"
            archive.writestr("chapters/example.zip", chapter)
            archive.writestr("README.txt", b"creation instructions")
            archive.writestr("SHA256SUMS.json", json.dumps({
                "chapters/example.zip": publication.sha(chapter)
            }, indent=2))
            if extra:
                archive.writestr("unexpected.txt", b"must not silently discard")
        return result.getvalue()

    def test_repack_changes_only_readme_and_is_repeatable(self):
        before = self.bundle()
        chapters = {"chapters/example.zip": b"original chapter ZIP bytes"}
        after = publication.repack(before, chapters, b"authorized release instructions")
        with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
            self.assertEqual(old.namelist(), new.namelist())
            self.assertEqual(old.comment, new.comment)
            for name in old.namelist():
                if name != "README.txt":
                    self.assertEqual(old.read(name), new.read(name))
            self.assertEqual(new.read("README.txt"), b"authorized release instructions")
        self.assertEqual(after, publication.repack(after, chapters, b"authorized release instructions"))

    def test_repack_rejects_changed_chapter_or_inventory(self):
        chapters = {"chapters/example.zip": b"original chapter ZIP bytes"}
        with self.assertRaisesRegex(ValueError, "hash inventory mismatch"):
            publication.repack(self.bundle(b"unexpected chapter"), chapters, b"new")
        with self.assertRaisesRegex(ValueError, "Unexpected outer ZIP inventory"):
            publication.repack(self.bundle(extra=True), chapters, b"new")
        content = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.bundle())) as source, zipfile.ZipFile(content, "w") as target:
            for name in source.namelist():
                # Advertise the correct hash but substitute different embedded bytes.
                target.writestr(name, b"unexpected chapter" if name.endswith(".zip") else source.read(name))
        with self.assertRaisesRegex(ValueError, "Embedded chapter differs"):
            publication.repack(content.getvalue(), chapters, b"new")

    def test_real_artifacts_preserve_inputs_and_reject_tampering_before_writes(self):
        # Exercise the exact publication dataset in isolation; never write to source artifacts.
        with tempfile.TemporaryDirectory(prefix="ksat-publication-test-") as temporary:
            root = Path(temporary)
            output = root / publication.DIRECTORY
            shutil.copytree(publication.ROOT / publication.DIRECTORY, output)
            latest = root / "question-banks/latest"
            original_latest = publication.ROOT / "question-banks/latest"
            shutil.copytree(original_latest / "quantitative-aptitude", latest / "quantitative-aptitude")
            shutil.copy2(original_latest / "selection.json", latest / "selection.json")
            (latest / "textbook-downloads").mkdir()
            name = "quantitative-aptitude-chapters-01-39.zip"
            shutil.copy2(original_latest / "textbook-downloads" / name, latest / "textbook-downloads" / name)
            protected = list(latest.rglob("*.zip")) + [latest / "selection.json"]
            protected += list((output / "chapters").glob("*.zip")) + [output / "validation.json"]
            before = {path: path.read_bytes() for path in protected}
            result = publication.prepare(root)
            self.assertTrue(result["release_authorized"])
            self.assertFalse(result["approval_scope"]["new_vision_approvals"])
            self.assertEqual(result["github_publication"], "not_confirmed")
            self.assertEqual(len(result["chapters"]), 39)
            self.assertEqual([row["chapters"] for row in result["bundles"]], [6, 39])
            self.assertEqual(result, publication.prepare(root, check=True))
            self.assertEqual(result, publication.prepare(root))
            self.assertEqual(before, {path: path.read_bytes() for path in protected})
            targets = [output / name for name in (publication.UPDATE, publication.MASTER, "publication.json")]
            prior_outputs = {path: path.read_bytes() for path in targets}
            chapter = next((output / "chapters").glob("*.zip"))
            chapter.write_bytes(chapter.read_bytes() + b"changed after review")
            with self.assertRaisesRegex(ValueError, "Revised chapter hash differs"):
                publication.prepare(root)
            self.assertEqual(prior_outputs, {path: path.read_bytes() for path in targets})


if __name__ == "__main__":
    unittest.main()
