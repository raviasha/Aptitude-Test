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


if __name__ == "__main__":
    unittest.main()
