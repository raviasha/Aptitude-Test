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


if __name__ == "__main__": unittest.main()
