import base64
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import app
from ksat.coordinator.client_updates import ClientUpdateStore
from ksat.crypto import generate_ed25519_keypair
from ksat.protocol import canonical_json, device_request_bytes
from ksat.update_protocol import parse_client_update
from scripts.build_client_update import build_client_update


class ClientUpdateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.originals = (app.DATA_DIR, app.DB_PATH, app.BACKUP_DIR, app.QUESTION_BANKS_DIR, app.app.state.coordinator_config)
        self.original_enrollment_code = os.environ.get("KSAT_DEVICE_ENROLLMENT_CODE")
        app.DATA_DIR = Path(self.temp.name)
        app.DB_PATH = app.DATA_DIR / "aptitude.db"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.QUESTION_BANKS_DIR = app.DATA_DIR / "Question Banks"
        os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = "update-api-enroll"
        app.ensure_schema()
        app.seed_data()
        app.configure_coordinator_state(app.app)
        self.private, self.public = generate_ed25519_keypair()
        self.client = TestClient(app.app)
        enrolled = self.client.post("/api/client/v1/devices/enroll", json={"label": "LAB-A", "public_key_b64": self.public})
        self.device_id = enrolled.json()["device_id"]
        self.release = self._stage_release()

    def tearDown(self):
        self.client.close()
        app.close_pack_registry(app.app.state.coordinator_config)
        writer = getattr(app.app.state.coordinator_config, "submission_writer", None)
        if writer is not None:
            writer.stop(timeout_seconds=5)
        app.DATA_DIR, app.DB_PATH, app.BACKUP_DIR, app.QUESTION_BANKS_DIR, config = self.originals
        app.app.state.coordinator_config = config
        if self.original_enrollment_code is None:
            os.environ.pop("KSAT_DEVICE_ENROLLMENT_CODE", None)
        else:
            os.environ["KSAT_DEVICE_ENROLLMENT_CODE"] = self.original_enrollment_code
        self.temp.cleanup()

    def _stage_release(self):
        installer = Path(self.temp.name) / "setup.exe"
        installer.write_bytes(b"signed installer")
        signing_private, signing_public = generate_ed25519_keypair()
        key = Path(self.temp.name) / "offline.key"
        key.write_bytes(base64.b64decode(signing_private))
        bundle = Path(self.temp.name) / "release.ksat-client-update"
        build_client_update(
            installer=installer, version="2.1.0", minimum_source_version="2.0.0",
            publisher="CN=KSIT", private_key_file=key, output=bundle,
            release_notes="Managed update", release_id=str(uuid.uuid4()),
            published_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            authenticode_verifier=lambda _path, publisher: {"publisher": publisher},
        )
        self.bundle_path = bundle
        app.app.state.coordinator_config.update_signing_public_key_b64 = signing_public
        app.app.state.coordinator_config.update_authenticode_verifier = (
            lambda _path, publisher: {"publisher": publisher}
        )
        verified = parse_client_update(bundle, signing_public, lambda _path, publisher: {"publisher": publisher})
        with app.db() as connection:
            store = ClientUpdateStore(connection, app.DATA_DIR / "Client Updates")
            release = store.upload(verified)
            store.select_pilot(release.release_id, self.device_id)
        return release

    def _headers(self, method, path, body=b"", **extra):
        timestamp = datetime.now(timezone.utc).isoformat()
        nonce = str(uuid.uuid4())
        signature = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.private)).sign(
            device_request_bytes(method, path, body, timestamp, nonce)
        )
        return {
            "X-KSAT-Device": self.device_id, "X-KSAT-Timestamp": timestamp,
            "X-KSAT-Nonce": nonce, "X-KSAT-Signature": base64.b64encode(signature).decode(),
            **extra,
        }

    def test_pilot_policy_range_download_and_idempotent_status_require_device_proof(self):
        policy_path = "/api/client/v1/update-policy"
        unauthenticated = self.client.get(policy_path, headers={"X-KSAT-Client-Version": "2.0.0"})
        self.assertEqual(403, unauthenticated.status_code)
        policy = self.client.get(policy_path, headers=self._headers("GET", policy_path, **{"X-KSAT-Client-Version": "2.0.0"}))
        self.assertEqual(200, policy.status_code, policy.text)
        self.assertEqual(self.release.release_id, policy.json()["release_id"])

        bundle_path = f"/api/client/v1/updates/{self.release.release_id}/bundle"
        downloaded = self.client.get(bundle_path, headers=self._headers("GET", bundle_path, Range="bytes=5-"))
        self.assertEqual(206, downloaded.status_code)
        self.assertTrue(downloaded.headers["content-range"].startswith("bytes 5-"))

        status_path = f"/api/client/v1/updates/{self.release.release_id}/status"
        payload = {"stage": "downloading", "installed_version": "2.0.0", "diagnostic_code": None, "attempt_id": "11111111-1111-4111-8111-111111111111"}
        body = canonical_json(payload)
        first = self.client.post(status_path, content=body, headers={**self._headers("POST", status_path, body), "Content-Type": "application/json"})
        second = self.client.post(status_path, content=body, headers={**self._headers("POST", status_path, body), "Content-Type": "application/json"})
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), second.json())

    def test_admin_update_mutation_requires_authenticated_csrf_session(self):
        response = self.client.post("/api/admin/client-updates/upload", files={"bundle": ("bad.ksat-client-update", b"bad")})
        self.assertIn(response.status_code, (401, 403))

    def test_admin_can_upload_and_list_verified_update(self):
        login = self.client.post(
            "/api/login",
            json={"identifier": "faculty", "password": "faculty123", "role": "admin"},
        )
        self.assertEqual(200, login.status_code, login.text)
        csrf = login.json()["csrf_token"]
        with self.bundle_path.open("rb") as source:
            uploaded = self.client.post(
                "/api/admin/client-updates/upload",
                files={"bundle": ("release.ksat-client-update", source, "application/octet-stream")},
                headers={"X-KSAT-CSRF": csrf},
            )
        self.assertEqual(200, uploaded.status_code, uploaded.text)
        self.assertEqual(self.release.release_id, uploaded.json()["release"]["release_id"])
        listed = self.client.get("/api/admin/client-updates")
        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual(self.release.release_id, listed.json()["releases"][0]["release_id"])

    def test_restore_requires_admin_csrf_and_verified_unused_release(self):
        path = f'/api/admin/client-updates/{self.release.release_id}/restore'
        with app.db() as connection:
            ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').withdraw(self.release.release_id)
        self.assertIn(self.client.post(path).status_code, (401,403))
        login = self.client.post('/api/login', json={'identifier':'faculty','password':'faculty123','role':'admin'})
        csrf = {'X-KSAT-CSRF':login.json()['csrf_token']}
        self.assertEqual(403, self.client.post(path).status_code)
        listed = self.client.get('/api/admin/client-updates').json()['releases'][0]
        self.assertTrue(listed['can_restore'])
        restored = self.client.post(path, headers=csrf)
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual('uploaded', restored.json()['release']['state'])
        self.assertFalse(self.client.get('/api/admin/client-updates').json()['releases'][0]['can_restore'])
        self.assertEqual(409, self.client.post(path, headers=csrf).status_code)
        pilot = self.client.post(f'/api/admin/client-updates/{self.release.release_id}/pilot',
                                json={'device_id':self.device_id}, headers=csrf)
        self.assertEqual(200, pilot.status_code, pilot.text)

    def test_restore_rechecks_signature_and_leaves_withdrawal_on_failure(self):
        with app.db() as connection:
            ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').withdraw(self.release.release_id)
        login = self.client.post('/api/login', json={'identifier':'faculty','password':'faculty123','role':'admin'})
        def reject(_path, _publisher):
            raise ValueError('Untrusted signature')
        app.app.state.coordinator_config.update_authenticode_verifier = reject
        restored = self.client.post(f'/api/admin/client-updates/{self.release.release_id}/restore',
                                   headers={'X-KSAT-CSRF':login.json()['csrf_token']})
        self.assertEqual(400, restored.status_code, restored.text)
        with app.db() as connection:
            self.assertEqual('withdrawn', ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').release(self.release.release_id).state)

    def test_restore_rejects_untrusted_manifest_before_authenticode_check(self):
        with app.db() as connection:
            ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').withdraw(self.release.release_id)
        login = self.client.post('/api/login', json={'identifier':'faculty','password':'faculty123','role':'admin'})
        _, different_public = generate_ed25519_keypair()
        app.app.state.coordinator_config.update_signing_public_key_b64 = different_public
        calls = []
        app.app.state.coordinator_config.update_authenticode_verifier = lambda *args: calls.append(args)
        response = self.client.post(f'/api/admin/client-updates/{self.release.release_id}/restore',
                                   headers={'X-KSAT-CSRF':login.json()['csrf_token']})
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual([], calls)
        with app.db() as connection:
            self.assertEqual('withdrawn', ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').release(self.release.release_id).state)

    def test_restore_rechecks_activity_after_signature_verification(self):
        with app.db() as connection:
            ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').withdraw(self.release.release_id)
        login = self.client.post('/api/login', json={'identifier':'faculty','password':'faculty123','role':'admin'})
        def late_report(_path, publisher):
            with app.db() as connection:
                ClientUpdateStore(connection, app.DATA_DIR/'Client Updates').record_status(
                    self.release.release_id, self.device_id, 'downloading', '2.0.0', None,
                    attempt_id='11111111-1111-4111-8111-111111111111')
            return {'publisher':publisher}
        app.app.state.coordinator_config.update_authenticode_verifier = late_report
        response = self.client.post(f'/api/admin/client-updates/{self.release.release_id}/restore',
                                   headers={'X-KSAT-CSRF':login.json()['csrf_token']})
        self.assertEqual(409, response.status_code, response.text)
        with app.db() as connection:
            store = ClientUpdateStore(connection, app.DATA_DIR/'Client Updates')
            self.assertEqual('withdrawn', store.release(self.release.release_id).state)
            self.assertEqual('downloading', store.dashboard(self.release.release_id)[0]['stage'])

    def test_restore_cannot_redirect_to_a_different_valid_signed_release(self):
        other_id = str(uuid.uuid4())
        other_bundle = app.DATA_DIR/'other.ksat-client-update'
        build_client_update(installer=app.DATA_DIR/'setup.exe', version='2.1.1', minimum_source_version='2.0.0',
            publisher='CN=KSIT', private_key_file=app.DATA_DIR/'offline.key', output=other_bundle,
            release_notes='Other release', release_id=other_id, published_at=datetime(2026,9,24,tzinfo=timezone.utc),
            authenticode_verifier=lambda _path,publisher:{'publisher':publisher})
        config = app.app.state.coordinator_config
        other = parse_client_update(other_bundle, config.update_signing_public_key_b64, config.update_authenticode_verifier)
        with app.db() as connection:
            store = ClientUpdateStore(connection, app.DATA_DIR/'Client Updates')
            store.upload(other)
            store.withdraw(other_id)
            store.withdraw(self.release.release_id)
            store.bundle_path(self.release.release_id).write_bytes(other_bundle.read_bytes())
        login = self.client.post('/api/login', json={'identifier':'faculty','password':'faculty123','role':'admin'})
        response = self.client.post(f'/api/admin/client-updates/{self.release.release_id}/restore',
                                   headers={'X-KSAT-CSRF':login.json()['csrf_token']})
        self.assertEqual(400, response.status_code, response.text)
        with app.db() as connection:
            store = ClientUpdateStore(connection, app.DATA_DIR/'Client Updates')
            self.assertEqual(['withdrawn','withdrawn'], [store.release(key).state for key in (self.release.release_id,other_id)])


if __name__ == "__main__":
    unittest.main()
