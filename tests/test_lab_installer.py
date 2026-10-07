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
    def test_actual_helper_allows_prior_release_upgrade_and_current_reinstall(self):
        # Catch stale helper targets even when installer labels/resources are new.
        self.install()
        before = self.bytes()
        for installed in ("2.1.2", "2.1.3", "2.1.4"):
            with self.subTest(installed=installed), patch(
                    "ksat.client.install_guard.installed_version", return_value=installed):
                stage = self.program_data / installed
                stage.mkdir()
                service = FakeService(running=True)
                alive = iter([True, False])
                helper.run_guard_session(self.program_data, stage,
                    lambda: next(alive, False), service=service, profile=self.profile)
                status = json.loads((stage / "status.json").read_bytes())
                self.assertEqual("aborted", status["state"], status)
                self.assertEqual(["stop", "start"], service.actions)
                self.assertEqual(before, self.bytes())

    def test_actual_helper_refuses_newer_installed_version_without_changes(self):
        self.install()
        before = self.bytes()
        stage = self.program_data / "newer"
        stage.mkdir()
        service = FakeService(running=True)
        with patch("ksat.client.install_guard.installed_version", return_value="9.0.0"):
            helper.run_guard_session(self.program_data, stage, lambda: True,
                service=service, profile=self.profile)
        self.assertEqual("downgrade_refused", json.loads((stage / "status.json").read_bytes())["diagnostic_code"])
        self.assertEqual([], service.actions)
        self.assertEqual(before, self.bytes())

    def _assert_current_upgrade_preserves_pending_work(self, submit, reason):
        self.prepare_attempt(submit=submit)
        before = self.bytes()
        stage = self.program_data / "pending-work"
        stage.mkdir()
        service = FakeService(running=True)
        with patch("ksat.client.install_guard.installed_version", return_value="2.1.3"):
            helper.run_guard_session(self.program_data, stage, lambda: True,
                service=service, profile=self.profile)
        self.assertEqual(reason, json.loads((stage / "status.json").read_bytes())["diagnostic_code"])
        self.assertEqual([], service.actions)
        self.assertEqual(before, self.bytes())

    def test_current_upgrade_keeps_active_attempt_protected(self):
        self._assert_current_upgrade_preserves_pending_work(False, "active_attempt")

    def test_current_upgrade_keeps_pending_submission_protected(self):
        self._assert_current_upgrade_preserves_pending_work(True, "pending_submission")

    def test_two_labs_fresh_install_independently_and_cross_lab_upgrade_is_blocked(self):
        from ksat.coordinator.tls import load_or_create_coordinator_security
        from ksat.public_trust import validate_public_bundle
        from ksat.lab_builder.profile import make_lab_profile
        from client_app import ClientConfigStore
        profiles = []
        for number in (1, 2):
            name = f"lab{number}.example.edu"
            security = load_or_create_coordinator_security(self.program_data / f"server{number}", hostname=name)
            trust = validate_public_bundle(f"https://{name}:8443", security.ca_certificate_pem,
                (security.public_export_dir / "coordinator-public.json").read_bytes(), now=datetime.now(timezone.utc))
            profile = make_lab_profile(f"Lab {number}", trust)
            profiles.append(profile)
            data = self.program_data / f"pc{number}"
            stage = self.program_data / f"stage{number}"
            stage.mkdir()
            helper.configure_installation(data, stage, profile, fresh=True, stores=MemoryTrust())
            config = ClientConfigStore(data / "KSAT Client/client-config.json").load()
            self.assertEqual(f"https://{name}:8443", config.coordinator_base_url)
            self.assertEqual(trust.ca_pem, Path(config.trusted_ca_path).read_bytes())
            self.assertEqual(trust.signing_public_key_b64, config.coordinator_signing_public_key_b64)
        self.assertNotEqual(profiles[0].trust.ca_sha256, profiles[1].trust.ca_sha256)
        data = self.program_data / "pc1"
        before = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file() and not p.name.endswith(".lock")}
        stage = self.program_data / "cross-lab"
        stage.mkdir()
        service = FakeService(running=True)
        helper.run_guard_session(data, stage, lambda: True, service=service, profile=profiles[1])
        self.assertEqual("different_server", json.loads((stage / "status.json").read_bytes())["diagnostic_code"])
        self.assertEqual([], service.actions)
        after = {p.relative_to(data): p.read_bytes() for p in data.rglob("*") if p.is_file() and not p.name.endswith(".lock")}
        self.assertEqual(before, after)

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
            for mode, bundled_trust in ((0, 1), (1, 1), (0, 0)):
                with self.subTest(mode=mode, bundled_trust=bundled_trust):
                    from ksat.client.version import CLIENT_VERSION
                    # Exercise the real template default as well as explicit overrides.
                    use_default = (mode, bundled_trust) == (0, 1)
                    version_args = [] if use_default else ["/DKSAT_CLIENT_VERSION=2.1.1"]
                    expected_version = CLIENT_VERSION if use_default else "2.1.1"
                    output = work / f"{mode}-{bundled_trust}"
                    result = subprocess.run([os.environ["KSAT_ISCC"], f"/DKSAT_LAB_MODE={mode}",
                        f"/DKSAT_PAYLOAD_DIR={payload}", f"/DKSAT_OUTPUT_DIR={output}",
                        *version_args, f"/DKSAT_BUNDLE_PUBLISHER_TRUST={bundled_trust}", str(root / "installer/KSATClient.iss")],
                        capture_output=True, text=True, timeout=120)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    exe = output / f"KSATClientSetup-{expected_version}.exe"
                    self.assertTrue(exe.is_file())
                    extracted = output / "extracted"
                    result = subprocess.run([os.environ["KSAT_INNOEXTRACT"], "-d", str(extracted), str(exe)],
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(0, result.returncode, result.stderr)
                    contents = {p.name: p.read_bytes() for p in extracted.rglob("*") if p.is_file()}
                    for name in ("KSATClient.exe", "KSATClientUpdater.exe", "KSATClientInstallGuard.exe"):
                        self.assertEqual((payload / name).read_bytes(), contents[name])
                    self.assertEqual(bool(bundled_trust), "publisher.cer" in contents)
                    if bundled_trust:
                        self.assertEqual((payload / "publisher.cer").read_bytes(), contents["publisher.cer"])
                    self.assertEqual(bool(mode), "lab-profile.json" in contents)
