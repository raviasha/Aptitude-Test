import subprocess
import sys
import tempfile
import threading
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import client_app
from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.store import ClientStore
from ksat.coordinator.tls import load_or_create_coordinator_security
from ksat.lab_builder.profile import make_lab_profile
from ksat.public_trust import validate_public_bundle
from ksat.client.install_guard import inspect_installation, InstallationLease, MaintenanceGate, MaintenanceBusy
from tests import test_client_runtime as runtime_fixtures
from tests.test_client_identity import PrefixProtector


class ClientInstallGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server_dir = tempfile.TemporaryDirectory()
        cls.security = load_or_create_coordinator_security(Path(cls.server_dir.name), hostname="lab.example.edu")
        trust = validate_public_bundle("https://lab.example.edu:8443", cls.security.ca_certificate_pem,
                                       (cls.security.public_export_dir / "coordinator-public.json").read_bytes(),
                                       now=datetime.now(timezone.utc))
        cls.profile = make_lab_profile("Lab One", trust)

    @classmethod
    def tearDownClass(cls):
        cls.server_dir.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.program_data = Path(self.directory.name)
        self.root = self.program_data / "KSAT Client"
        self.protector = patch("ksat.client.identity._default_protector", return_value=PrefixProtector())
        self.protector.start()
        self.addCleanup(self.protector.stop)
        self.version = patch("ksat.client.install_guard.installed_version", return_value="2.1.1")
        self.version.start()
        self.addCleanup(self.version.stop)

    def install(self):
        client_app.install_client_configuration(
            self.program_data, base_url=self.profile.trust.base_url, ca_source=self.security.ca_certificate_path,
            metadata_source=self.security.public_export_dir / "coordinator-public.json")
        identity = DeviceIdentityStore(self.root / "identity").load_or_create()
        with ClientStore(self.root / "state" / "client.sqlite3", integrity_key=derive_state_integrity_key(identity),
                         integrity_anchor_path=self.root / "identity" / "state-anchor.json"):
            pass
        return identity

    def bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and not p.name.endswith(".lock")}

    def inspect(self):
        return inspect_installation(self.program_data, self.profile, "2.1.1")

    def test_fresh_and_same_server(self):
        self.assertEqual("fresh", self.inspect().state)
        self.assertFalse(self.root.exists())
        self.install()
        before = self.bytes()
        self.assertEqual("same_server", self.inspect().state)
        self.assertEqual(before, self.bytes())

    def test_runtime_url_conflict_blocks_without_writes(self):
        self.install()
        config = client_app.ClientRuntimeConfigStore(client_app.ClientConfigStore(self.root / "client-config.json"),
                                                   self.root / "coordinator-url.json")
        config.update_base_url("https://other.example.edu:8443")
        before = self.bytes()
        self.assertEqual("different_server", self.inspect().diagnostic_code)
        self.assertEqual(before, self.bytes())

    def prepare_attempt(self, submit=False):
        self.install()
        fixture = runtime_fixtures.ClientRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.store.close()
        identities = DeviceIdentityStore(self.root / "identity")
        fixture.coordinator_private = self.security.signing_private_key_b64
        fixture.coordinator_public = self.security.signing_public_key_b64
        fixture.summary = fixture._summary()
        fixture.identity = identities.save_enrollment(fixture.device_id, fixture.coordinator_public)
        fixture.store = ClientStore(self.root / "state" / "client.sqlite3",
                                   integrity_key=derive_state_integrity_key(fixture.identity),
                                   integrity_anchor_path=self.root / "identity" / "state-anchor.json")
        fixture.start_response = fixture._start_response()
        fixture.runtime = fixture._runtime()
        fixture.runtime.prepare(fixture.summary, fixture.manifest, fixture.pack_path)
        fixture.runtime.start(fixture.start_response, student_id=fixture.student_id)
        if submit:
            fixture.runtime.submit()
        fixture.store.close()

    def test_active_attempt_blocks(self):
        self.prepare_attempt()
        before = self.bytes()
        service = FakeService(running=True)
        with InstallationLease(self.program_data, service=service) as lease:
            self.assertEqual("active_attempt", lease.prepare(self.profile, "2.1.1").diagnostic_code)
        self.assertEqual([], service.actions)
        self.assertEqual(before, self.bytes())

    def test_pending_submission_blocks(self):
        self.prepare_attempt(submit=True)
        before = self.bytes()
        self.assertEqual("pending_submission", self.inspect().diagnostic_code)
        self.assertEqual(before, self.bytes())

    def test_unreadable_state_fails_closed(self):
        self.install()
        (self.root / "state" / "client.sqlite3").write_bytes(b"invalid")
        before = self.bytes()
        self.assertEqual("unreadable_state", self.inspect().diagnostic_code)
        self.assertEqual(before, self.bytes())

    def test_legacy_running_service_refused(self):
        self.install()
        service = FakeService(running=True)
        with patch("ksat.client.install_guard.installed_version", return_value="2.1.0"):
            with InstallationLease(self.program_data, service=service) as lease:
                self.assertEqual("stop_legacy_client", lease.prepare(self.profile, "2.1.1").diagnostic_code)
        self.assertEqual([], service.actions)

    def test_second_installer_is_rejected(self):
        self.install()
        with InstallationLease(self.program_data, service=FakeService()) as first:
            self.assertEqual("same_server", first.prepare(self.profile, "2.1.1").state)
            with self.assertRaises(MaintenanceBusy):
                with InstallationLease(self.program_data, service=FakeService()):
                    pass

    def test_start_attempt_race_is_serialized(self):
        self.install()
        attempted = []
        with InstallationLease(self.program_data, service=FakeService()) as lease:
            self.assertEqual("same_server", lease.prepare(self.profile, "2.1.1").state)
            def attempt():
                try:
                    with MaintenanceGate(self.root):
                        attempted.append(True)
                except MaintenanceBusy:
                    attempted.append(False)
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual([False], attempted)
        with MaintenanceGate(self.root):
            pass

    def test_dead_owner_releases_gate(self):
        self.install()
        code = ("import sys; from pathlib import Path; from ksat.client.install_guard import MaintenanceGate; "
                "gate=MaintenanceGate(Path(sys.argv[1])); gate.__enter__(); print('held',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual("held", child.stdout.readline().strip())
            with self.assertRaises(MaintenanceBusy):
                with MaintenanceGate(self.root):
                    pass
        finally:
            child.terminate()
            child.wait(10)
            child.stdin.close()
            child.stdout.close()
        with MaintenanceGate(self.root):
            pass

    def test_abort_restores_previous_service_availability(self):
        self.install()
        service = FakeService(running=True)
        with InstallationLease(self.program_data, service=service) as lease:
            self.assertEqual("same_server", lease.prepare(self.profile, "2.1.1").state)
        self.assertEqual(["stop", "start"], service.actions)

    def test_guard_parent_exit_aborts_and_restores_service(self):
        from client_install_guard import run_guard_session
        from ksat.lab_builder.profile import encode_profile
        self.install()
        stage = self.root / "updates" / "install-staging" / ("a" * 32)
        stage.mkdir(parents=True)
        (stage / "lab-profile.json").write_bytes(encode_profile(self.profile))
        (stage / "coordinator-ca.pem").write_bytes(self.profile.trust.ca_pem)
        (stage / "coordinator-public.json").write_bytes(self.profile.trust.metadata_json)
        service = FakeService(running=True)
        alive = iter([True, False])
        result = run_guard_session(self.program_data, stage, lambda: next(alive, False), service=service)
        self.assertEqual(1, result)
        self.assertEqual(["stop", "start"], service.actions)
        self.assertEqual("aborted", json.loads((stage / "status.json").read_bytes())["state"])
        with MaintenanceGate(self.root):
            pass

    def test_guard_changed_resource_blocks_before_service_change(self):
        from client_install_guard import run_guard_session
        from ksat.lab_builder.profile import encode_profile
        self.install()
        stage = self.root / "updates" / "install-staging" / ("b" * 32)
        stage.mkdir(parents=True)
        (stage / "lab-profile.json").write_bytes(encode_profile(self.profile))
        (stage / "coordinator-ca.pem").write_bytes(b"changed")
        (stage / "coordinator-public.json").write_bytes(self.profile.trust.metadata_json)
        service = FakeService(running=True)
        with self.assertRaises(ValueError):
            run_guard_session(self.program_data, stage, lambda: True, service=service)
        self.assertEqual([], service.actions)


class FakeService:
    def __init__(self, running=False):
        self.running = running
        self.actions = []

    def is_running(self):
        return self.running

    def stop(self):
        self.actions.append("stop")
        self.running = False

    def start(self):
        self.actions.append("start")
        self.running = True
