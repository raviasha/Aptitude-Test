import base64
import hashlib
import json
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

    def test_active_attempt_blocks_but_completed_pending_submission_can_update(self):
        self.assertEqual("deferred_active_attempt", self.manager.check(active_attempt=True).stage)
        self.assertEqual("required", self.manager.check(pending_submission=True).stage)

    def test_install_request_keeps_reported_attempt_and_does_not_launch_twice(self):
        self.manager.check()
        self.manager.download()
        request = json.loads(self.manager.prepare_install().read_bytes())
        self.assertEqual(self.coordinator.statuses[-1][1]['attempt_id'], request['attempt_id'])
        self.assertEqual('installing', self.manager.check().stage)

    def _completed_install(self, **changes):
        self.manager.check()
        self.manager.download()
        request = json.loads(self.manager.prepare_install().read_bytes())
        journal = dict(format_version=2, stage='healthy', diagnostic_code=None,
                       release_id=self.release_id, attempt_id=request['attempt_id'],
                       target_version='2.1.0', installed_version='2.1.0',
                       bundle_sha256=self.policy['bundle_sha256'])
        journal.update(changes)
        (self.manager.root / 'update-journal.json').write_text(json.dumps(journal))
        self.manager.installed_version = '2.1.0'
        self.coordinator.policy = None

    def test_completed_install_reports_health_before_policy_clears_state(self):
        self._completed_install()
        self.assertEqual('current', self.manager.check().stage)
        report = self.coordinator.statuses[-1][1]
        self.assertEqual('healthy', report['stage'])
        self.assertEqual('2.1.0', report['installed_version'])

    def test_wrong_attempt_outcome_never_reports_health(self):
        self._completed_install(attempt_id=str(uuid.uuid4()))
        self.assertEqual('recovery_required', self.manager.check().stage)
        self.assertNotIn('healthy', [s['stage'] for _, s in self.coordinator.statuses])

    def test_legacy_unbound_journal_never_counts_as_verified_success(self):
        self._completed_install()
        (self.manager.root / 'update-journal.json').write_text('{"stage":"healthy"}')
        snapshot = self.manager.check()
        self.assertEqual('legacy_update_receipt_unavailable', snapshot.diagnostic_code)
        self.assertNotIn('healthy', [s['stage'] for _, s in self.coordinator.statuses])

    def test_failed_rollback_reports_failure_but_requires_recovery(self):
        self._completed_install(stage='rollback_failed', installed_version=None)
        self.assertEqual('recovery_required', self.manager.check().stage)
        self.assertEqual('failed', self.coordinator.statuses[-1][1]['stage'])
        self.assertEqual('recovery_required', self.manager.check(retry=True).stage)

    def test_failed_health_report_is_retried_without_reinstall(self):
        from unittest.mock import patch
        self._completed_install()
        with patch.object(self.coordinator, 'report_update_status', side_effect=OSError('offline')):
            self.assertEqual('outcome_report_pending', self.manager.check().diagnostic_code)
        self.assertEqual('current', self.manager.check().stage)
        self.assertEqual('healthy', self.coordinator.statuses[-1][1]['stage'])

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
        self.assertEqual("unavailable", self.manager.check().stage)
        self.assertEqual("coordinator_unavailable", self.manager.snapshot().diagnostic_code)

    def test_abandoned_install_is_actionable_without_relaunch(self):
        import os, time
        self.manager.check(); self.manager.download(); self.manager.prepare_install()
        old = time.time() - 180
        os.utime(self.manager.install_request_path, (old, old))
        self.assertEqual('recovery_required', self.manager.check().stage)
        self.assertEqual('update_interrupted', self.manager.check().diagnostic_code)

    def test_running_helper_keeps_installing_after_startup_grace(self):
        import os, time
        from ksat.client.updater import UpdaterLock
        self.manager.check(); self.manager.download(); self.manager.prepare_install()
        old = time.time() - 180
        os.utime(self.manager.install_request_path, (old, old))
        with UpdaterLock(self.manager.root):
            self.assertEqual('installing', self.manager.check().stage)

    def test_explicit_retry_archives_failed_outcome_and_uses_new_attempt(self):
        self._completed_install(stage='rolled_back', installed_version='2.0.0')
        self.manager.installed_version = '2.0.0'
        self.coordinator.policy = self.policy
        original = self.manager._read_state()['attempt_id']
        self.assertEqual('failed', self.manager.check().stage)
        self.assertEqual('required', self.manager.check(retry=True).stage)
        self.assertNotEqual(original, self.manager._read_state()['attempt_id'])
        self.assertFalse((self.manager.root/'update-journal.json').exists())
        self.assertEqual(1, len(list((self.manager.root/'previous-attempts').glob('*/update-journal.json'))))

    def test_randomized_poll_delay_stays_in_window(self):
        values = [self.manager.next_check_delay(300, 60) for _ in range(100)]
        self.assertTrue(all(240 <= value <= 360 for value in values))


if __name__ == "__main__": unittest.main()
