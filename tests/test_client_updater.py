import base64
import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ksat.client.updater import HealthVerifier, Updater
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import canonical_json
from scripts.build_client_update import build_client_update


class Services:
    def __init__(self): self.calls=[]
    def stop(self, name, timeout): self.calls.append(("stop",name,timeout))
    def start(self, name, timeout): self.calls.append(("start",name,timeout))

class Installers:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def run(self, path, args, timeout):
        self.calls.append((Path(path),list(args),timeout))
        if self.fail:
            self.fail=False
            raise RuntimeError("installer failed")

class Health:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def verify(self, *args):
        self.calls.append(args)
        if self.fail: raise ValueError("unhealthy")
        return True

class ClientUpdaterTests(unittest.TestCase):
    def test_rollback_permission_expires_when_journal_or_cached_bytes_change(self):
        from client_install_guard import authorized_rollback
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            updates = data / "KSAT Client/updates"
            cached = updates / "last-known-good/KSATClientSetup-2.1.1.exe"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"signed lab installer")
            stage = data / "stage"
            stage.mkdir()
            (stage / "install-context.json").write_bytes(canonical_json({"installer": str(cached)}))
            journal = updates / "update-journal.json"
            journal.write_bytes(canonical_json({"stage": "rollback_installing", "rollback_version": "2.1.1",
                "rollback_sha256": hashlib.sha256(cached.read_bytes()).hexdigest()}))
            with patch("client_install_guard.require_protected_directory"):
                self.assertTrue(authorized_rollback(data, stage, "2.1.1"))
                self.assertFalse(authorized_rollback(data, stage, "2.1.0"))
                cached.write_bytes(b"changed")
                self.assertFalse(authorized_rollback(data, stage, "2.1.1"))

    def test_runtime_url_is_included_in_update_preservation_digest(self):
        from client_updater import configuration_digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client-config.json").write_bytes(b"seed")
            before = configuration_digest(root)
            (root / "coordinator-url.json").write_bytes(b"effective server")
            self.assertNotEqual(before, configuration_digest(root))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)/"updates"; self.root.mkdir()
        installer=Path(self.temp.name)/"setup.exe"; installer.write_bytes(b"signed installer")
        private,self.public=generate_ed25519_keypair(); key=Path(self.temp.name)/"key"; key.write_bytes(base64.b64decode(private))
        self.release_id=str(uuid.uuid4()); self.bundle=self.root/"bundle.ksat-client-update"
        build_client_update(installer=installer,version="2.1.0",minimum_source_version="2.0.0",publisher="CN=KSIT",private_key_file=key,output=self.bundle,release_notes="Update",release_id=self.release_id,published_at=datetime(2026,9,24,tzinfo=timezone.utc),authenticode_verifier=lambda _p,p:{"publisher":p})
        self.request=self.root/"install-request.json"
        self.request.write_bytes(canonical_json({"format_version":1,"release_id":self.release_id,"target_version":"2.1.0","bundle_path":str(self.bundle),"attempt_id":str(uuid.uuid4())}))
        self.services=Services(); self.installers=Installers(); self.health=Health()
        self.updater=Updater(self.root,self.public,lambda _p,p:{"publisher":p},self.services,self.installers,self.health,installed_version="2.0.0",identity_digest="i",config_digest="c",state_digest="s")

    def tearDown(self): self.temp.cleanup()

    def test_success_installs_with_fixed_arguments_and_promotes_rollback(self):
        result=self.updater.run(self.request)
        self.assertTrue(result.success)
        self.assertEqual(["/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART"],self.installers.calls[0][1])
        self.assertTrue(self.updater.last_known_good_path.is_file())
        # The guarded installer owns quiescing; stopping here would bypass its
        # active-attempt/pending-submission checks before it can reject safely.
        self.assertEqual([], self.services.calls)

    def test_failure_runs_last_known_good_and_reports_rollback(self):
        self.updater.last_known_good_path.parent.mkdir(parents=True,exist_ok=True)
        self.updater.last_known_good_path.write_bytes(b"old signed installer")
        self.installers.fail=True
        result=self.updater.run(self.request)
        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertEqual("update_install_failed",result.diagnostic_code)

    def test_rollback_journal_binds_only_exact_cached_installer(self):
        self.updater.last_known_good_path.parent.mkdir(parents=True, exist_ok=True)
        self.updater.last_known_good_path.write_bytes(b"old signed installer")
        previous_run = self.installers.run
        observed = []
        def run(path, args, timeout):
            if Path(path) == self.updater.last_known_good_path:
                observed.append(json.loads(self.updater.journal_path.read_bytes()))
            return previous_run(path, args, timeout)
        self.installers.run = run
        self.installers.fail = True
        self.updater.run(self.request)
        self.assertEqual(hashlib.sha256(b"old signed installer").hexdigest(), observed[0]["rollback_sha256"])
        self.assertEqual("2.0.0", observed[0]["rollback_version"])
        self.assertNotIn("rollback_sha256", json.loads(self.updater.journal_path.read_bytes()))

    def test_identical_generic_update_preserves_two_lab_profiles(self):
        for label, url in (("a", "https://lab-a:8443"), ("b", "https://lab-b:8443")):
            with self.subTest(label=label):
                request = json.loads(self.request.read_bytes())
                request["attempt_id"] = str(uuid.uuid4())
                self.request.write_bytes(canonical_json(request))
                client = Path(self.temp.name) / label
                client.mkdir()
                config = client / "client-config.json"
                identity = client / "device-key.bin"
                config.write_bytes(canonical_json({"coordinator_base_url": url}))
                identity.write_bytes(label.encode() * 32)
                before = (config.read_bytes(), identity.read_bytes())
                self.assertTrue(self.updater.run(self.request).success)
                self.assertEqual(before, (config.read_bytes(), identity.read_bytes()))

    def test_refuses_request_or_bundle_outside_protected_root(self):
        outside=Path(self.temp.name)/"outside.json"; outside.write_bytes(self.request.read_bytes())
        with self.assertRaises(ValueError): self.updater.run(outside)
        value=json.loads(self.request.read_bytes()); value["bundle_path"]=str(Path(self.temp.name)/"outside.ksat-client-update"); self.request.write_bytes(canonical_json(value))
        with self.assertRaises(ValueError): self.updater.run(self.request)

    def test_health_verifier_checks_version_and_immutable_digests(self):
        verifier=HealthVerifier(lambda:{"version":"2.1.0"},lambda:True)
        self.assertTrue(verifier.verify("2.1.0","i","c","s","i","c","s"))
        with self.assertRaises(ValueError): verifier.verify("2.2.0","i","c","s","i","c","s")

    def test_terminal_journal_binds_verified_bundle_and_same_request_attempt(self):
        self.updater.run(self.request)
        journal = json.loads(self.updater.journal_path.read_bytes())
        request = json.loads(self.request.read_bytes())
        self.assertEqual(2, journal.get("format_version"))
        for key in ("release_id", "attempt_id", "target_version"):
            self.assertEqual(request[key], journal[key])
        self.assertEqual(hashlib.sha256(self.bundle.read_bytes()).hexdigest(), journal["bundle_sha256"])
        self.assertEqual("2.1.0", journal["installed_version"])

    def test_completed_request_cannot_launch_installer_twice(self):
        self.assertTrue(self.updater.run(self.request).success)
        self.assertEqual("request_replayed", self.updater.run(self.request).stage)
        self.assertEqual(1, len(self.installers.calls))

    def test_missing_rollback_still_persists_bound_failure(self):
        self.installers.fail = True
        self.assertFalse(self.updater.run(self.request).success)
        journal = json.loads(self.updater.journal_path.read_bytes())
        self.assertEqual("rollback_failed", journal["stage"])
        self.assertEqual(json.loads(self.request.read_bytes())["attempt_id"], journal["attempt_id"])
        self.assertIsNone(journal["installed_version"])

    def test_invalid_request_does_not_replace_existing_outcome(self):
        self.updater.run(self.request)
        before = self.updater.journal_path.read_bytes()
        self.request.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            self.updater.run(self.request)
        self.assertEqual(before, self.updater.journal_path.read_bytes())
        failure = json.loads((self.root / "invalid-request-journal.json").read_bytes())
        self.assertEqual("invalid_install_request", failure["diagnostic_code"])

    def test_second_helper_cannot_overwrite_journal_or_launch_during_install(self):
        original = self.installers.run
        def run(path, args, timeout):
            before = self.updater.journal_path.read_bytes()
            result = self.updater.run(self.request)
            self.assertEqual("updater_busy", result.stage)
            self.assertEqual(before, self.updater.journal_path.read_bytes())
            original(path, args, timeout)
        self.installers.run = run
        self.assertTrue(self.updater.run(self.request).success)
        self.assertEqual(1, len(self.installers.calls))

    def test_extraction_rechecks_signed_installer_hash(self):
        import zipfile
        original = self.updater._extract_installer
        def changed_bundle(verified):
            with zipfile.ZipFile(self.bundle, "w") as archive:
                archive.writestr(verified.manifest.installer_filename, b"different signed installer")
            return original(verified)
        with patch.object(self.updater, "_extract_installer", side_effect=changed_bundle):
            result = self.updater.run(self.request)
        self.assertFalse(result.success)
        self.assertEqual([], self.installers.calls)
        self.assertEqual("candidate_verification_failed", json.loads(self.updater.journal_path.read_bytes())["diagnostic_code"])

    def test_new_attempt_cannot_replace_interrupted_preparing_journal(self):
        old = dict(json.loads(self.request.read_bytes()), stage="preparing", attempt_id=str(uuid.uuid4()))
        self.updater.journal_path.write_bytes(canonical_json(old))
        self.assertEqual("unfinished_update", self.updater.run(self.request).stage)
        self.assertEqual(old, json.loads(self.updater.journal_path.read_bytes()))

    def test_health_waits_for_restart_with_signed_manifest_timeout(self):
        clock = [0.0]
        def probe():
            if clock[0] < 2:
                raise ConnectionRefusedError("service starting")
            return {"version": "2.1.0"}
        self.updater.health = HealthVerifier(probe, lambda: True)
        with patch("ksat.client.updater.time.monotonic", create=True, side_effect=lambda: clock[0]), \
             patch("ksat.client.updater.time.sleep", create=True, side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.assertTrue(self.updater.run(self.request).success)
        self.assertEqual(2.0, clock[0])

    def test_health_timeout_is_bounded_and_failed_journal_reports_observed_version(self):
        clock = [0.0]
        self.updater.health = HealthVerifier(lambda: {"version": "2.0.0"}, lambda: False)
        with patch("ksat.client.updater.time.monotonic", create=True, side_effect=lambda: clock[0]), \
             patch("ksat.client.updater.time.sleep", create=True, side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            self.assertFalse(self.updater.run(self.request).success)
        self.assertEqual(60.0, clock[0])
        self.assertEqual("2.0.0", json.loads(self.updater.journal_path.read_bytes())["installed_version"])

    def test_failed_candidate_cache_promotion_keeps_rollback_installer_available(self):
        self.updater.last_known_good_path.parent.mkdir()
        self.updater.last_known_good_path.write_bytes(b"old signed installer")
        with patch("ksat.client.updater.shutil.copy2", side_effect=OSError("disk full")):
            result = self.updater.run(self.request)
        self.assertTrue(result.rolled_back)
        self.assertEqual("KSATClientSetup-2.0.0.exe", self.installers.calls[-1][0].name)

    def test_health_requires_preservation_mode_or_explicit_legacy_digests(self):
        with self.assertRaises(ValueError):
            Updater(self.root, self.public, lambda *_: None, self.services,
                    self.installers, self.health, installed_version="2.0.0")


if __name__ == "__main__": unittest.main()
