"""Installer input and trust transaction boundaries (no machine mutation)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests.test_client_install_guard import ClientInstallGuardTests, FakeService
from ksat.lab_builder.profile import encode_profile
import client_install_guard as helper


class LabInstallerTests(ClientInstallGuardTests):
    def stage(self):
        stage = self.program_data / "KSAT Installer Staging" / ("a" * 32)
        stage.mkdir(parents=True)
        (stage / "lab-profile.json").write_bytes(encode_profile(self.profile))
        (stage / "coordinator-ca.pem").write_bytes(self.profile.trust.ca_pem)
        (stage / "coordinator-public.json").write_bytes(self.profile.trust.metadata_json)
        (stage / "publisher.cer").write_bytes(
            (Path(__file__).resolve().parents[1] / "release/KSATLabReleaseSigning.cer").read_bytes())
        return stage

    def test_lab_resources_return_readonly_summary(self):
        profile = helper.load_install_profile(self.stage(), self.program_data)
        self.assertEqual("Lab One", profile.lab_name)
        self.assertEqual("https://lab.example.edu:8443", profile.trust.base_url)
        self.assertFalse(self.root.exists())

    def test_changed_bundle_fails_before_trust_write(self):
        stage = self.stage()
        (stage / "coordinator-public.json").write_bytes(b"{}")
        with self.assertRaises(ValueError):
            helper.load_install_profile(stage, self.program_data)
        self.assertFalse(self.root.exists())

    def test_wrong_publisher_fails_before_trust_write(self):
        stage = self.stage()
        (stage / "publisher.cer").write_bytes(self.security.ca_certificate_pem)
        with self.assertRaises(ValueError):
            helper.load_install_profile(stage, self.program_data)

    def test_generic_upgrade_uses_effective_existing_configuration(self):
        self.install()
        stage = self.program_data / "generic"
        stage.mkdir()
        before = self.bytes()
        profile = helper.load_install_profile(stage, self.program_data)
        self.assertEqual(self.profile.trust, profile.trust)
        self.assertEqual(before, self.bytes())

    def test_publisher_trust_idempotent_and_shared_entries_retained(self):
        stage = self.stage()
        profile = helper.load_install_profile(stage, self.program_data)
        stores = MemoryTrust()
        first = helper.install_public_trust(profile, stage, stores)
        self.assertEqual(3, len(first))
        self.assertEqual([], helper.install_public_trust(profile, stage, stores))
        self.assertEqual(3, len(stores.entries))

    def test_fresh_lease_can_recheck_after_its_own_lifecycle_lock(self):
        from ksat.client.install_guard import InstallationLease
        (self.root / "state").mkdir(parents=True)
        with InstallationLease(self.program_data, service=FakeService()) as lease:
            self.assertEqual("fresh", lease.prepare(self.profile, "2.1.1").state)

    def test_unsigned_rollback_context_cannot_bypass_downgrade(self):
        stage = self.stage()
        (stage / "install-context.json").write_text(json.dumps({"installer": "C:/arbitrary.exe"}))
        self.assertFalse(helper.authorized_rollback(self.program_data, stage, "2.1.1"))

    def test_fresh_configuration_creates_unique_device_state(self):
        stage = self.stage()
        helper.configure_installation(self.program_data, stage, self.profile, fresh=True, stores=MemoryTrust())
        self.assertEqual("same_server", self.inspect().state)

    def test_same_server_configuration_keeps_all_existing_bytes(self):
        self.install()
        before = self.bytes()
        helper.configure_installation(self.program_data, self.stage(), self.profile, fresh=False, stores=MemoryTrust())
        # Trust ownership audit is the only addition; identity/database/config stay identical.
        after = self.bytes()
        self.assertEqual(before, {key: after[key] for key in before})

    def test_wrong_server_configuration_refuses_before_trust(self):
        self.install()
        from client_app import ClientConfigStore, ClientRuntimeConfigStore
        ClientRuntimeConfigStore(ClientConfigStore(self.root / "client-config.json"), self.root / "coordinator-url.json").update_base_url("https://other.example.edu:8443")
        before = self.bytes()
        trust = MemoryTrust()
        with self.assertRaises(ValueError):
            helper.configure_installation(self.program_data, self.stage(), self.profile, fresh=False, stores=trust)
        self.assertEqual(before, self.bytes())
        self.assertEqual(set(), trust.entries)


class MemoryTrust:
    def __init__(self):
        self.entries = set()

    def ensure(self, store, certificate_der):
        key = (store, certificate_der)
        added = key not in self.entries
        self.entries.add(key)
        return added


@unittest.skipUnless(os.environ.get("KSAT_ISCC") and os.environ.get("KSAT_INNOEXTRACT"), "Native installer tools required")
class CompiledInstallerTests(unittest.TestCase):
    def test_generic_and_lab_installers_extract_exact_public_payload(self):
        root = Path(__file__).resolve().parents[1]
        fixture = root / "release/KSATClient-2.1.0.exe"
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            payload = work / "payload"
            payload.mkdir()
            for name in ("KSATClient.exe", "KSATClientUpdater.exe", "KSATClientInstallGuard.exe"):
                shutil.copyfile(fixture, payload / name)
            shutil.copyfile(root / "release/KSATLabReleaseSigning.cer", payload / "publisher.cer")
            for name in ("lab-profile.json", "coordinator-ca.pem", "coordinator-public.json"):
                (payload / name).write_bytes(b"compile fixture: " + name.encode())
            (payload / "lab-summary.ini").write_text("[Lab]\nName=Test Lab\nURL=https://lab.example.edu:8443\n", encoding="utf-16")
            for mode in (0, 1):
                with self.subTest(mode=mode):
                    output = work / str(mode)
                    result = subprocess.run([os.environ["KSAT_ISCC"], f"/DKSAT_LAB_MODE={mode}",
                        f"/DKSAT_PAYLOAD_DIR={payload}", f"/DKSAT_OUTPUT_DIR={output}",
                        "/DKSAT_CLIENT_VERSION=2.1.1", str(root / "installer/KSATClient.iss")],
                        capture_output=True, text=True, timeout=120)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    exe = output / "KSATClientSetup-2.1.1.exe"
                    self.assertTrue(exe.is_file())
                    extracted = output / "extracted"
                    result = subprocess.run([os.environ["KSAT_INNOEXTRACT"], "-d", str(extracted), str(exe)],
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(0, result.returncode, result.stderr)
                    contents = {p.name: p.read_bytes() for p in extracted.rglob("*") if p.is_file()}
                    for name in ("KSATClient.exe", "KSATClientUpdater.exe", "KSATClientInstallGuard.exe", "publisher.cer"):
                        self.assertEqual((payload / name).read_bytes(), contents[name])
                    self.assertEqual(bool(mode), "lab-profile.json" in contents)
