from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
warnings.filterwarnings("ignore", category=DeprecationWarning)
DATA_ENGINEERING = PROJECT_ROOT / "data-engineering"
if str(DATA_ENGINEERING) not in sys.path:
    sys.path.insert(0, str(DATA_ENGINEERING))

from python_vision_calibration.tests import test_pilot_audit as audit_tests
from textbook_chapters_v2.config import ChapterConfig
from textbook_chapters_v2.models import PipelineBlocked


class PilotPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = audit_tests.PilotAuditTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.candidate = self.fixture.merge(self.fixture.accept_route)[0]
        self.manifest = self.fixture.make_render_manifest(self.candidate)
        self.audit = self.fixture.write_audit(self.fixture.accept_route, (), (self.candidate,), (self.manifest,))
        self.published = self.root / "published.zip"
        self.published.write_bytes(b"published-stays-read-only")
        self.output = self.root / "candidate.zip"
        self.config = ChapterConfig.from_dict({
            "chapter": 1,
            "bank_name": "Agent Triage Number System",
            "question_pages": [1, 1],
            "answer_pages": [1, 1],
            "solution_pages": [1, 1],
            "question_numbers": [44, 44],
            "published_path": str(self.published),
            "source_pdf": str(PROJECT_ROOT / "data-engineering" / "dokumen.pub_quantitative-aptitude-for-competitive-examinations-by-rs-aggarwal-reprint-2017nbsped-9352534026-9789352534029.pdf"),
            "source_pdf_sha256": "0723862418cd7b088341bcfc78a10745fd434b3f4db695986b1ff4f40a7223bf",
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, output=None, audit=None, candidates=None):
        from python_vision_calibration.pilot_package import build_pilot_candidate_package
        return build_pilot_candidate_package(
            self.config,
            (self.candidate,) if candidates is None else candidates,
            self.audit if audit is None else audit,
            self.output if output is None else output,
        )

    def test_published_guard_detects_change(self) -> None:
        from python_vision_calibration.pilot_package import PublishedPackageGuard
        guard = PublishedPackageGuard.capture(self.published)
        self.published.write_bytes(b"changed")
        with self.assertRaisesRegex(PipelineBlocked, "published"):
            guard.verify()

    def test_package_requires_exact_terminal_rendered_audit_inventory(self) -> None:
        pending = self.fixture.write_audit(self.fixture.accept_route, (), (self.candidate,), ())
        with self.assertRaisesRegex(PipelineBlocked, "render|terminal"):
            self.build(audit=pending)
        self.audit = self.fixture.write_audit(
            self.fixture.accept_route, (), (self.candidate,), (self.manifest,)
        )

        duplicate = (self.candidate, self.candidate)
        with self.assertRaisesRegex(PipelineBlocked, "duplicate"):
            self.build(candidates=duplicate)

        missing = replace(self.audit, records=())
        with self.assertRaisesRegex(PipelineBlocked, "audit"):
            self.build(audit=missing)

    def test_existing_output_remains_byte_identical(self) -> None:
        self.output.write_bytes(b"existing")
        with self.assertRaisesRegex(PipelineBlocked, "overwrite|existing"):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_non_candidate_input_is_rejected_before_sorting(self) -> None:
        with self.assertRaisesRegex(TypeError, "CandidateRecord"):
            self.build(candidates=({"question_number": 44},))

    def test_written_package_has_exact_members_and_round_trips(self) -> None:
        import app
        result = self.build()
        with result.path.open("rb") as package:
            bank, questions, _, version = app.parse_question_package(package)
        self.assertEqual(version, 3)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question_text"], self.candidate.question_text.strip())
        with zipfile.ZipFile(result.path) as archive:
            names = [member.filename for member in archive.infolist()]
            self.assertEqual(names, sorted(names))
            self.assertEqual(set(names), {
                "manifest.json", "questions/ch01.jsonl",
                "metadata/agent-triage-audit.json", "metadata/lineage.json",
            })
            self.assertTrue(all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in archive.infolist()))
            manifest = json.loads(archive.read("manifest.json"))
            raw_question = json.loads(archive.read("questions/ch01.jsonl"))
            self.assertEqual(raw_question["question_text"], self.candidate.question_text)
            self.assertTrue(manifest["manual_review_required"])
            self.assertEqual(manifest["audit_sha256"], self.audit.sha256)
            self.assertEqual(bank, self.config.bank_name)

    def test_same_inputs_in_separate_directories_produce_identical_bytes(self) -> None:
        first = self.build(output=self.root / "one" / "candidate.zip").path
        second = self.build(output=self.root / "two" / "candidate.zip").path
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_changed_published_zip_during_build_prevents_candidate_publication(self) -> None:
        import python_vision_calibration.pilot_package as package_module
        original = package_module._validate_written_package

        def mutate_published(*args, **kwargs):
            original(*args, **kwargs)
            self.published.write_bytes(b"changed-during-build")

        with patch.object(package_module, "_validate_written_package", side_effect=mutate_published):
            with self.assertRaisesRegex(PipelineBlocked, "published"):
                self.build()
        self.assertFalse(self.output.exists())

    def test_stale_screenshot_or_forged_audit_blocks_packaging(self) -> None:
        screenshot = Path(self.manifest["screenshots"]["1024x768"]["submitted"]["path"])
        screenshot.write_bytes(b"changed")
        with self.assertRaisesRegex(PipelineBlocked, "screenshot|render"):
            self.build()

    def test_configured_source_pdf_hash_must_match_the_audit(self) -> None:
        changed = replace(
            self.config,
            extras={**dict(self.config.extras), "source_pdf_sha256": "0" * 64},
        )
        from python_vision_calibration.pilot_package import build_pilot_candidate_package
        with self.assertRaisesRegex(PipelineBlocked, "source PDF"):
            build_pilot_candidate_package(changed, (self.candidate,), self.audit, self.output)

        missing = replace(
            self.config,
            extras={**dict(self.config.extras), "source_pdf": str(self.root / "missing.pdf")},
        )
        with self.assertRaisesRegex(PipelineBlocked, "source PDF"):
            build_pilot_candidate_package(missing, (self.candidate,), self.audit, self.output)

    def test_destination_created_at_publish_race_is_preserved(self) -> None:
        import python_vision_calibration.pilot_package as package_module
        real_link = package_module.os.link

        def competing(source, destination):
            Path(destination).write_bytes(b"")
            return real_link(source, destination)

        with patch.object(package_module.os, "link", side_effect=competing):
            with self.assertRaisesRegex(PipelineBlocked, "overwrite|existing"):
                self.build()
        self.assertTrue(self.output.exists())
        self.assertEqual(self.output.read_bytes(), b"")

    def test_media_is_hash_addressed_and_unreferenced_or_unsafe_assets_cannot_enter_zip(self) -> None:
        # A caller cannot inject archive members: members are derived only from validated candidate media.
        malicious = replace(
            self.candidate,
            representation={
                **dict(self.candidate.representation),
                "media": {"question": {"asset": "../evil.png", "alt_text": "evil"}},
            },
        )
        with self.assertRaisesRegex(PipelineBlocked, "media|fingerprint|source"):
            self.build(candidates=(malicious,))


if __name__ == "__main__":
    unittest.main()
