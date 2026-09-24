import base64
import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ksat.client.updates import ClientUpdateManager
from ksat.crypto import generate_ed25519_keypair
from scripts.build_client_update import build_client_update


class FakeCoordinator:
    def __init__(self, policy, bundle):
        self.policy, self.bundle = policy, bundle
        self.offsets, self.statuses = [], []
        self.available = True

    def update_policy(self, installed_version):
        if not self.available:
            raise OSError("offline")
        return self.policy

    def download_update_range(self, release_id, offset, destination):
        self.offsets.append(offset)
        mode = "ab" if offset else "wb"
        with Path(destination).open(mode) as stream:
            stream.write(self.bundle[offset:])
        return len(self.bundle), len(self.bundle)

    def report_update_status(self, release_id, **status):
        self.statuses.append((release_id, status))
        return status


class ClientUpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        installer = root / "setup.exe"; installer.write_bytes(b"signed installer")
        private, self.public = generate_ed25519_keypair()
        key = root / "release.key"; key.write_bytes(base64.b64decode(private))
        bundle = root / "source.ksat-client-update"
        self.release_id = str(uuid.uuid4())
        build_client_update(
            installer=installer, version="2.1.0", minimum_source_version="2.0.0",
            publisher="CN=KSIT", private_key_file=key, output=bundle,
            release_notes="Required", release_id=self.release_id,
            published_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            authenticode_verifier=lambda _path, publisher: {"publisher": publisher},
        )
        self.bundle = bundle.read_bytes()
        self.policy = {
            "release_id": self.release_id, "client_version": "2.1.0",
            "minimum_source_version": "2.0.0", "bundle_sha256": hashlib.sha256(self.bundle).hexdigest(),
            "bundle_size": len(self.bundle), "manifest": {}, "state": "published",
        }
        self.coordinator = FakeCoordinator(self.policy, self.bundle)
        self.manager = ClientUpdateManager(
            root / "updates", self.coordinator, "2.0.0", self.public,
            lambda _path, publisher: {"publisher": publisher}, random_source=lambda: .5,
        )

    def tearDown(self): self.temp.cleanup()

    def test_idle_outdated_client_prepares_verified_update(self):
        self.assertEqual("required", self.manager.check().stage)
        self.manager.download()
        self.assertEqual("ready_to_install", self.manager.snapshot().stage)
        request = self.manager.prepare_install()
        self.assertTrue(request.is_file())

    def test_active_attempt_and_pending_submission_defer_update(self):
        self.assertEqual("deferred_active_attempt", self.manager.check(active_attempt=True).stage)
        self.assertEqual("deferred_pending_submission", self.manager.check(pending_submission=True).stage)

    def test_partial_download_resumes_and_changed_release_discards_partial(self):
        self.manager.check()
        partial = self.manager.partial_path
        partial.write_bytes(self.bundle[:17])
        self.manager.download()
        self.assertEqual(17, self.coordinator.offsets[-1])
        self.manager._write_state({**self.manager._read_state(), "release_id": str(uuid.uuid4()), "stage": "downloading"})
        self.coordinator.offsets.clear()
        self.manager.check()
        self.manager.download()
        self.assertEqual(0, self.coordinator.offsets[-1])

    def test_unavailable_coordinator_preserves_current_client(self):
        self.coordinator.available = False
        self.assertEqual("current", self.manager.check().stage)
        self.assertEqual("coordinator_unavailable", self.manager.snapshot().diagnostic_code)

    def test_randomized_poll_delay_stays_in_window(self):
        values = [self.manager.next_check_delay(300, 60) for _ in range(100)]
        self.assertTrue(all(240 <= value <= 360 for value in values))


if __name__ == "__main__": unittest.main()
