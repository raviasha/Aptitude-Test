import base64
import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
        self.assertEqual(["stop","start"],[item[0] for item in self.services.calls])

    def test_failure_runs_last_known_good_and_reports_rollback(self):
        self.updater.last_known_good_path.parent.mkdir(parents=True,exist_ok=True)
        self.updater.last_known_good_path.write_bytes(b"old signed installer")
        self.installers.fail=True
        result=self.updater.run(self.request)
        self.assertFalse(result.success)
        self.assertTrue(result.rolled_back)
        self.assertEqual("update_install_failed",result.diagnostic_code)

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
