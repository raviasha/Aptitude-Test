from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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


class _Win32FunctionProxy:
    def __init__(self, real_function, override=None) -> None:
        object.__setattr__(self, "_real_function", real_function)
        object.__setattr__(self, "_override", override)

    def __call__(self, *args):
        if self._override is not None:
            return self._override(self._real_function, *args)
        return self._real_function(*args)

    def __setattr__(self, name, value) -> None:
        setattr(self._real_function, name, value)


class _Kernel32Proxy:
    def __init__(self, real_kernel32, **overrides) -> None:
        self._real_kernel32 = real_kernel32
        self._overrides = overrides

    def __getattr__(self, name):
        return _Win32FunctionProxy(getattr(self._real_kernel32, name), self._overrides.get(name))


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

    def test_existing_candidate_is_fully_authenticated_without_republication(self) -> None:
        from python_vision_calibration.pilot_package import authenticate_pilot_candidate_package
        built = self.build()
        before = self.output.read_bytes(), self.output.stat().st_mtime_ns

        with patch(
            "python_vision_calibration.pilot_package._current_fingerprints",
            return_value=("7" * 64, "6" * 64, "8" * 64),
        ):
            authenticated = authenticate_pilot_candidate_package(
                self.config, (self.candidate,), self.audit, self.output
            )

        self.assertEqual(authenticated.sha256, built.sha256)
        self.assertEqual(authenticated.question_count, 1)
        self.assertEqual((self.output.read_bytes(), self.output.stat().st_mtime_ns), before)

    def test_existing_candidate_authentication_rejects_stale_runtime_fingerprints(self) -> None:
        from python_vision_calibration.pilot_package import authenticate_pilot_candidate_package
        self.build()
        with patch(
            "python_vision_calibration.pilot_package._current_fingerprints",
            return_value=("0" * 64, "6" * 64, "8" * 64),
        ):
            with self.assertRaisesRegex(PipelineBlocked, "runtime|render"):
                authenticate_pilot_candidate_package(
                    self.config, (self.candidate,), self.audit, self.output
                )

    def test_existing_candidate_authentication_rejects_arbitrary_or_mismatched_bytes(self) -> None:
        from python_vision_calibration.pilot_package import authenticate_pilot_candidate_package
        self.output.write_bytes(b"not a package")
        with self.assertRaises(PipelineBlocked):
            authenticate_pilot_candidate_package(
                self.config, (self.candidate,), self.audit, self.output
            )

    def test_existing_candidate_authentication_rejects_semantically_equal_repacked_archive(self) -> None:
        from python_vision_calibration.pilot_package import authenticate_pilot_candidate_package
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        repacked = self.root / "repacked.zip"
        with zipfile.ZipFile(repacked, "w", compression=zipfile.ZIP_STORED) as archive:
            for name in sorted(members):
                info = zipfile.ZipInfo(name, date_time=(2026, 8, 28, 12, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100600 << 16
                archive.writestr(info, members[name])

        with patch(
            "python_vision_calibration.pilot_package._current_fingerprints",
            return_value=("7" * 64, "6" * 64, "8" * 64),
        ):
            with self.assertRaisesRegex(PipelineBlocked, "canonical|bytes|archive"):
                authenticate_pilot_candidate_package(
                    self.config, (self.candidate,), self.audit, repacked
                )

        self.output.unlink()
        self.build()
        with zipfile.ZipFile(self.output, "a") as archive:
            archive.writestr("manifest.json", b"{}")
        with self.assertRaises(PipelineBlocked):
            authenticate_pilot_candidate_package(
                self.config, (self.candidate,), self.audit, self.output
            )

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

    def test_post_write_guard_failure_deletes_the_owned_candidate(self) -> None:
        import python_vision_calibration.pilot_package as package_module

        real_publish = package_module._publish_exclusively
        def fail_guard_after_write(source, destination, post_write_check, **kwargs):
            return real_publish(
                source,
                destination,
                post_write_check,
                before_commit=lambda: self.published.write_bytes(b"published-changed-after-write"),
                **kwargs,
            )

        with patch.object(package_module, "_publish_exclusively", side_effect=fail_guard_after_write):
            with self.assertRaisesRegex(PipelineBlocked, "published"):
                self.build()
        self.assertFalse(self.output.exists())

    def test_crashed_process_removes_delete_on_close_staging_without_publishing(self) -> None:
        source = self.root / "crash-source.zip"
        destination = self.root / "crash-candidate.zip"
        source.write_bytes(b"validated-candidate-bytes")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(DATA_ENGINEERING), environment.get("PYTHONPATH", "")))
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys\n"
                    "from pathlib import Path\n"
                    "from python_vision_calibration.pilot_package import _publish_exclusively\n"
                    "source, destination = map(Path, sys.argv[1:])\n"
                    "_publish_exclusively(source, destination, lambda: os._exit(73))\n"
                ),
                str(source),
                str(destination),
            ],
            env=environment,
            check=False,
        )
        self.assertEqual(child.returncode, 73)
        deadline = time.monotonic() + 5
        while destination.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(f".{destination.name}.*.publishing")), [])

    def test_successful_atomic_link_preserves_exact_bytes_and_removes_staging(self) -> None:
        from python_vision_calibration.pilot_package import _publish_exclusively

        source = self.root / "success-source.zip"
        destination = self.root / "success-candidate.zip"
        expected = b"validated-candidate-bytes-that-must-survive-close"
        source.write_bytes(expected)
        _publish_exclusively(source, destination, lambda: None)
        self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(list(self.root.glob(f".{destination.name}.*.publishing")), [])

    def test_initial_disposition_failure_removes_real_delete_on_close_staging(self) -> None:
        import ctypes
        from python_vision_calibration.pilot_package import _publish_exclusively

        source = self.root / "arm-failure-source.zip"
        destination = self.root / "arm-failure-candidate.zip"
        source.write_bytes(b"bytes-that-must-never-be-written")
        real_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        disposition_calls = 0

        def fail_initial_disposition(real_function, *args):
            nonlocal disposition_calls
            disposition_calls += 1
            ctypes.set_last_error(5)
            return False

        proxy = _Kernel32Proxy(real_kernel32, SetFileInformationByHandle=fail_initial_disposition)
        with patch.object(ctypes, "WinDLL", return_value=proxy):
            with self.assertRaisesRegex(PipelineBlocked, "arm"):
                _publish_exclusively(source, destination, lambda: None)
        self.assertEqual(disposition_calls, 1)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(f".{destination.name}.*.publishing")), [])

    def test_pinned_destination_hash_mismatch_is_never_committed(self) -> None:
        from python_vision_calibration.pilot_package import _publish_exclusively

        source = self.root / "corrupt-source.zip"
        destination = self.root / "corrupt-candidate.zip"
        validated = b"validated-temporary-zip"
        source.write_bytes(b"corrupted-after-validation")
        with self.assertRaisesRegex(PipelineBlocked, "hash|bytes"):
            _publish_exclusively(
                source,
                destination,
                lambda: None,
                expected_sha256=hashlib.sha256(validated).hexdigest(),
            )
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(f".{destination.name}.*.publishing")), [])

    def test_close_failures_are_never_reported_as_success(self) -> None:
        import ctypes
        from python_vision_calibration.pilot_package import _publish_exclusively

        source = self.root / "close-failure-source.zip"
        destination = self.root / "close-failure-candidate.zip"
        expected = b"valid-bytes-committed-before-close"
        source.write_bytes(expected)
        real_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_calls = 0

        def close_then_report_failure(real_function, *args):
            nonlocal close_calls
            close_calls += 1
            self.assertTrue(real_function(*args))
            ctypes.set_last_error(6)
            return False

        proxy = _Kernel32Proxy(real_kernel32, CloseHandle=close_then_report_failure)
        with patch.object(ctypes, "WinDLL", return_value=proxy):
            with self.assertRaisesRegex(PipelineBlocked, "close"):
                _publish_exclusively(source, destination, lambda: None)
        self.assertEqual(close_calls, 2)
        self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(list(self.root.glob(f".{destination.name}.*.publishing")), [])

    def test_pinned_failure_cleanup_never_deletes_a_waiting_replacement(self) -> None:
        import python_vision_calibration.pilot_package as package_module

        real_publish = package_module._publish_exclusively
        competitor = self.root / "competitor.tmp"
        competitor_bytes = b"competitor-replaced-the-failed-candidate"
        competitor.write_bytes(competitor_bytes)
        start_replacement = threading.Event()
        replacement_done = threading.Event()
        replacement_error: list[BaseException] = []

        def replace_when_the_pinned_handle_releases() -> None:
            start_replacement.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    competitor.replace(self.output)
                    replacement_done.set()
                    return
                except (FileNotFoundError, PermissionError, OSError):
                    time.sleep(0.005)
            replacement_error.append(RuntimeError("competitor could not replace the released destination"))

        worker = threading.Thread(target=replace_when_the_pinned_handle_releases, daemon=True)
        worker.start()

        def after_candidate_bytes_are_pinned() -> None:
            start_replacement.set()
            self.published.write_bytes(b"published-changed-with-candidate-pinned")

        def publish_with_waiting_competitor(source, destination, post_write_check, **kwargs):
            return real_publish(
                source,
                destination,
                post_write_check,
                before_commit=after_candidate_bytes_are_pinned,
                **kwargs,
            )

        with patch.object(package_module, "_publish_exclusively", side_effect=publish_with_waiting_competitor):
            with self.assertRaisesRegex(PipelineBlocked, "published"):
                self.build()
        worker.join(timeout=6)
        self.assertFalse(replacement_error)
        self.assertTrue(replacement_done.is_set())
        self.assertEqual(self.output.read_bytes(), competitor_bytes)

    def test_published_package_is_pinned_through_the_atomic_candidate_commit(self) -> None:
        import python_vision_calibration.pilot_package as package_module

        real_publish = package_module._publish_exclusively
        begin_mutation = threading.Event()
        mutation_was_blocked = threading.Event()
        mutation_done = threading.Event()
        output_existed_at_mutation: list[bool] = []

        def mutate_published_when_guard_is_pinned() -> None:
            begin_mutation.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    self.published.write_bytes(b"published-changed-after-candidate-commit")
                    output_existed_at_mutation.append(self.output.exists())
                    mutation_done.set()
                    return
                except (PermissionError, OSError):
                    mutation_was_blocked.set()
                    time.sleep(0.005)

        worker = threading.Thread(target=mutate_published_when_guard_is_pinned, daemon=True)
        worker.start()

        def start_waiting_mutation() -> None:
            begin_mutation.set()
            if not mutation_was_blocked.wait(timeout=2):
                raise AssertionError("published mutation was not blocked by the pinned guard")

        def publish_with_waiting_mutation(source, destination, post_write_check, **kwargs):
            return real_publish(
                source,
                destination,
                post_write_check,
                after_published_guard=start_waiting_mutation,
                **kwargs,
            )

        with patch.object(package_module, "_publish_exclusively", side_effect=publish_with_waiting_mutation):
            result = self.build()
        worker.join(timeout=6)
        self.assertEqual(result.path, self.output)
        self.assertTrue(mutation_done.is_set())
        self.assertEqual(output_existed_at_mutation, [True])

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
        real_publish = package_module._publish_exclusively

        def competing(source, destination, post_write_check, **kwargs):
            Path(destination).write_bytes(b"")
            return real_publish(source, destination, post_write_check, **kwargs)

        with patch.object(package_module, "_publish_exclusively", side_effect=competing):
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
