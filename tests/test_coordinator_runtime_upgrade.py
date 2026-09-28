"""Exercise the actual installer validation/startup paths with legacy data."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import coordinator_main as coordinator
from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock


class CoordinatorRuntimeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "KSAT Coordinator"
        self.data.mkdir()
        self.path = self.data / "coordinator-runtime.json"
        self.value = dict(bind_host="192.168.44.23", hostname="desktop-i7nsd5h.local",
                          interactive=False, port=9443, version="2.0.0")
        self.raw = self.encode(self.value)
        self.path.write_bytes(self.raw)
        self.backup = self.data / "coordinator-runtime.pre-2.1.0.json.bak"
        # The migration must not touch unrelated data or security identity.
        self.untouched = {"aptitude.db": b"existing student records", "secrets/identity.key": b"existing identity"}
        for name, data in self.untouched.items():
            target = self.data / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)

    @staticmethod
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def validate(self):
        return coordinator.main(["--validate-config"], environ={"ProgramData": str(self.root)})

    def assert_migrated(self):
        expected = dict(self.value, version="2.1.0")
        self.assertEqual(self.encode(expected), self.path.read_bytes())
        self.assertEqual(self.raw, self.backup.read_bytes())
        for name, data in self.untouched.items():
            self.assertEqual(data, (self.data / name).read_bytes())

    def test_installer_validation_migrates_without_resetting_settings_or_records(self):
        self.assertEqual(0, self.validate())
        self.assert_migrated()
        original_backup_time = self.backup.stat().st_mtime_ns
        self.assertEqual(0, self.validate())
        self.assertEqual(original_backup_time, self.backup.stat().st_mtime_ns)
        self.assert_migrated()

    def test_loading_legacy_settings_is_read_only_until_exclusive_startup(self):
        settings = coordinator.CoordinatorRuntimeSettings.from_environment({"ProgramData": str(self.root)})
        self.assertEqual(9443, settings.port)
        self.assertEqual(self.raw, self.path.read_bytes())
        self.assertFalse(self.backup.exists())
        security = SimpleNamespace(browser_session_secret="b", client_session_secret="c",
                                   server_certificate_path=self.data / "public.pem",
                                   server_private_key_path=self.data / "private.pem")
        def load_security(*args, **kwargs):
            self.assert_migrated()
            return security
        coordinator.run_coordinator(settings, security_loader=load_security,
                                    app_loader=lambda: SimpleNamespace(state=SimpleNamespace()),
                                    uvicorn_runner=lambda *args, **kwargs: None)
        self.assert_migrated()

    def test_invalid_or_unknown_config_is_unchanged(self):
        malformed = [dict(self.value, version="1.9.0"), dict(self.value, version="2.2.0"),
                     dict(self.value, port="8443"), dict(self.value, interactive=1),
                     dict(self.value, hostname="https://server"), dict(self.value, unexpected=True),
                     dict(self.value, bind_host=1), dict(self.value, bind_host=2130706433),
                     dict(self.value, bind_host=True)]
        raws = [self.encode(v) for v in malformed] + [self.raw+b"\n", b"\xef\xbb\xbf"+self.raw,
               self.raw.replace(b'"version":"2.0.0"', b'"version":"2.0.0","version":"2.0.0"')]
        for raw in raws:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.validate()
                self.assertEqual(raw, self.path.read_bytes())
                self.assertFalse(self.backup.exists())

    def test_running_server_prevents_migration(self):
        with CoordinatorProcessLock(self.data):
            with self.assertRaises(CoordinatorLockHeld):
                self.validate()
        self.assertEqual(self.raw, self.path.read_bytes())
        self.assertFalse(self.backup.exists())

    def test_replace_failure_keeps_old_config_and_recoverable_backup(self):
        with patch.object(coordinator.os, "replace", side_effect=OSError("test replacement failure")):
            with self.assertRaises(OSError):
                self.validate()
        self.assertEqual(self.raw, self.path.read_bytes())
        self.assertEqual(self.raw, self.backup.read_bytes())
        self.assertEqual([], list(self.data.glob(".coordinator-runtime.*.tmp")))
        self.validate()
        self.assert_migrated()

    def test_existing_different_backup_is_not_overwritten(self):
        self.backup.write_bytes(b"earlier backup")
        with self.assertRaises(ValueError):
            self.validate()
        self.assertEqual(self.raw, self.path.read_bytes())
        self.assertEqual(b"earlier backup", self.backup.read_bytes())

    def test_current_config_does_not_create_migration_backup(self):
        raw = self.encode(dict(self.value, version="2.1.0"))
        self.path.write_bytes(raw)
        self.assertEqual(0, self.validate())
        self.assertEqual(raw, self.path.read_bytes())
        self.assertFalse(self.backup.exists())
