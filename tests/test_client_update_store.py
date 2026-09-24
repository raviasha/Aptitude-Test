import base64
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ksat.coordinator.client_updates import ClientUpdateStore, UpdateStateError
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.crypto import generate_ed25519_keypair
from ksat.update_protocol import parse_client_update
from scripts.build_client_update import build_client_update


class ClientUpdateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.connection = sqlite3.connect(self.root / "coordinator.db")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE tests (test_id INTEGER PRIMARY KEY);
            CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, student_id TEXT);
            CREATE TABLE responses (
              response_id INTEGER PRIMARY KEY, attempt_id TEXT, question_id INTEGER,
              selected_answer TEXT, correct INTEGER, category TEXT, chapter TEXT,
              question_order INTEGER
            );
            """
        )
        migrate_distributed_schema(self.connection)
        self.now = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
        self.connection.executemany(
            "INSERT INTO devices (device_id,label,public_key_b64,status,enrolled_at,last_seen_at) VALUES (?,?,?,?,?,?)",
            [
                ("device-a", "LAB-A", "key", "active", self.now.isoformat(), self.now.isoformat()),
                ("device-b", "LAB-B", "key", "active", self.now.isoformat(), (self.now - timedelta(hours=2)).isoformat()),
                ("device-disabled", "OLD", "key", "inactive", self.now.isoformat(), self.now.isoformat()),
            ],
        )
        self.connection.commit()
        self.verified = self._verified_bundle("2.1.0")
        self.store = ClientUpdateStore(
            self.connection,
            self.root / "Client Updates",
            now=lambda: self.now,
        )

    def tearDown(self):
        self.connection.close()
        self.temporary_directory.cleanup()

    def _verified_bundle(self, version):
        installer = self.root / f"KSATClientSetup-{version}.exe"
        installer.write_bytes(f"signed-{version}".encode())
        private, public = generate_ed25519_keypair()
        key = self.root / f"{version}.key"
        key.write_bytes(base64.b64decode(private))
        bundle = self.root / f"{version}.ksat-client-update"
        build_client_update(
            installer=installer,
            version=version,
            minimum_source_version="2.0.0",
            publisher="CN=KSIT",
            private_key_file=key,
            output=bundle,
            release_notes=f"Release {version}",
            release_id=str(uuid.uuid4()),
            published_at=self.now,
            authenticode_verifier=lambda _path, publisher: {"publisher": publisher},
        )
        return parse_client_update(bundle, public, lambda _path, publisher: {"publisher": publisher})

    def test_migration_and_upload_store_immutable_bundle_below_owned_root(self):
        release = self.store.upload(self.verified)

        self.assertEqual("uploaded", release.state)
        stored = self.root / "Client Updates" / release.release_id / "bundle.ksat-client-update"
        self.assertTrue(stored.is_file())
        self.assertEqual(self.verified.bundle_sha256, release.bundle_sha256)
        self.assertEqual(self.verified.bundle_path.read_bytes(), stored.read_bytes())
        for table in ("client_update_releases", "client_update_device_status"):
            self.assertIsNotNone(self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone())

    def test_publish_requires_successful_exact_pilot(self):
        release = self.store.upload(self.verified)
        self.store.select_pilot(release.release_id, "device-a")
        with self.assertRaises(UpdateStateError):
            self.store.publish(release.release_id)
        self.store.record_status(
            release.release_id, "device-a", "healthy", "2.1.0", None,
            attempt_id="11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual("published", self.store.publish(release.release_id).state)

    def test_inactive_device_and_wrong_pilot_health_cannot_publish(self):
        release = self.store.upload(self.verified)
        with self.assertRaises(UpdateStateError):
            self.store.select_pilot(release.release_id, "device-disabled")
        self.store.select_pilot(release.release_id, "device-a")
        self.store.record_status(
            release.release_id, "device-b", "healthy", "2.1.0", None,
            attempt_id="22222222-2222-4222-8222-222222222222",
        )
        with self.assertRaises(UpdateStateError):
            self.store.publish(release.release_id)

    def test_duplicate_identity_or_version_cannot_replace_artifact(self):
        release = self.store.upload(self.verified)
        self.verified.bundle_path.write_bytes(b"changed after verification")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.upload(self.verified)
        self.assertEqual(release.bundle_sha256, self.store.release(release.release_id).bundle_sha256)

        second = self._verified_bundle("2.1.0")
        with self.assertRaises(UpdateStateError):
            self.store.upload(second)

    def test_status_retry_is_idempotent_and_stale_attempt_is_rejected(self):
        release = self.store.upload(self.verified)
        attempt_id = "11111111-1111-4111-8111-111111111111"
        first = self.store.record_status(release.release_id, "device-a", "downloading", "2.0.0", None, attempt_id=attempt_id)
        repeated = self.store.record_status(release.release_id, "device-a", "downloading", "2.0.0", None, attempt_id=attempt_id)
        self.assertEqual(first, repeated)
        with self.assertRaises(UpdateStateError):
            self.store.record_status(
                release.release_id, "device-a", "healthy", "2.1.0", None,
                attempt_id="22222222-2222-4222-8222-222222222222",
            )

    def test_policy_respects_pilot_publish_withdraw_and_installed_version(self):
        release = self.store.upload(self.verified)
        self.store.select_pilot(release.release_id, "device-a")
        self.assertEqual(release.release_id, self.store.policy_for_device("device-a", "2.0.0")["release_id"])
        self.assertIsNone(self.store.policy_for_device("device-b", "2.0.0"))
        self.store.record_status(
            release.release_id, "device-a", "healthy", "2.1.0", None,
            attempt_id="11111111-1111-4111-8111-111111111111",
        )
        self.store.publish(release.release_id)
        self.assertEqual(release.release_id, self.store.policy_for_device("device-b", "2.0.0")["release_id"])
        self.assertIsNone(self.store.policy_for_device("device-b", "2.1.0"))
        self.store.withdraw(release.release_id)
        self.assertIsNone(self.store.policy_for_device("device-b", "2.0.0"))

    def test_dashboard_marks_unseen_active_device_offline(self):
        release = self.store.upload(self.verified)
        rows = {item["device_id"]: item for item in self.store.dashboard(release.release_id)}
        self.assertEqual("Waiting", rows["device-a"]["status"])
        self.assertEqual("Offline", rows["device-b"]["status"])
        self.assertNotIn("device-disabled", rows)


if __name__ == "__main__":
    unittest.main()
