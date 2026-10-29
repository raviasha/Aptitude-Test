"""Recovery contract tests use real authenticated stores; only OS boundaries are faked."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import unittest
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests import test_client_install_guard as guard_fixtures
from tests import test_client_runtime as runtime_fixtures
from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.store import ClientStore
from ksat.crypto import sign_json
from ksat.protocol import SubmissionReceipt, ResponseBundle, ResponseEntry, SignedResponseBundle


class ClientRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        guard_fixtures.ClientInstallGuardTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        guard_fixtures.ClientInstallGuardTests.tearDownClass()

    def setUp(self):
        from ksat.client import recovery
        self.recovery = recovery
        self.fixture = guard_fixtures.ClientInstallGuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install()
        self.root = self.fixture.root
        # Test directories and identity protector are deliberately non-production.
        self.protection = patch.object(recovery, '_require_protected_directory')
        self.protection.start()
        self.addCleanup(self.protection.stop)
        self.creation = patch.object(recovery, '_create_protected_directory',
                                     side_effect=lambda p: p.mkdir(parents=True, exist_ok=True))
        self.creation.start()
        self.addCleanup(self.creation.stop)
        self.service = guard_fixtures.FakeService(running=True)
        self.backend = recovery.ClientRecovery(self.root, service=self.service)

    def store(self):
        identity = DeviceIdentityStore(self.root / 'identity').load_existing()
        return ClientStore(self.root / 'state/client.sqlite3',
            integrity_key=derive_state_integrity_key(identity),
            integrity_anchor_path=self.root / 'identity/state-anchor.json')

    def attempt(self, *, active=False, error='attempt_identity_mismatch: private error detail', acknowledged=False):
        fixture = runtime_fixtures.ClientRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.store.close()
        identities = DeviceIdentityStore(self.root / 'identity')
        identity = identities.load_existing()
        fixture.device_id = identity.device_id or fixture.device_id
        fixture.coordinator_private = self.fixture.security.signing_private_key_b64
        fixture.coordinator_public = self.fixture.security.signing_public_key_b64
        fixture.summary = fixture._summary()
        fixture.identity = identities.save_enrollment(fixture.device_id, fixture.coordinator_public)
        fixture.store = ClientStore(fixture.db_path,
            integrity_key=derive_state_integrity_key(fixture.identity),
            integrity_anchor_path=fixture.root / 'state-anchor.json')
        fixture.start_response = fixture._start_response()
        fixture.runtime = fixture._runtime()
        fixture.runtime.prepare(fixture.summary, fixture.manifest, fixture.pack_path)
        fixture.store.create_attempt(fixture.start_response.ticket, [7, 3])
        fixture.store.save_answer(fixture.attempt_id, 7, 'A', saved_at=runtime_fixtures.STARTED)
        if not active:
            bundle = ResponseBundle(ticket=fixture.start_response.ticket,
                content_hash=fixture.start_response.ticket.ticket.content_hash,
                sealed_at=runtime_fixtures.STARTED,
                responses=[ResponseEntry(question_id=7, selected_answer='A'),
                           ResponseEntry(question_id=3, selected_answer=None)], integrity_events=[])
            fixture.store.seal_attempt(fixture.attempt_id, SignedResponseBundle(bundle=bundle,
                device_signature_b64=sign_json(fixture.identity.private_key_b64, bundle)),
                sealed_at=runtime_fixtures.STARTED)
            if acknowledged:
                fixture.store.acknowledge(fixture.attempt_id, SubmissionReceipt(
                    attempt_id=fixture.attempt_id, accepted_at=runtime_fixtures.STARTED + timedelta(seconds=1),
                    score=1, total_questions=2, attempted=1, percentage=50.0, violations=0))
            elif error:
                fixture.store.require_faculty_intervention(fixture.attempt_id, last_error=error)
        # Current issuance allows only one unfinished attempt. Older accumulated
        # queues are assembled from independently valid records, authenticated by
        # the real target store transaction, without weakening production guards.
        with self.store() as target:
            with target._transaction() as connection:
                for table in ('cached_content_packs', 'local_attempts', 'local_responses',
                              'local_integrity_events', 'submission_outbox'):
                    rows = fixture.store.connection.execute(f'SELECT * FROM {table}').fetchall()
                    if rows:
                        columns = ','.join('?' for _ in rows[0])
                        connection.executemany(f'INSERT INTO {table} VALUES ({columns})', [tuple(r) for r in rows])
        fixture.store.close()
        return fixture.attempt_id

    def selected(self):
        return self.backend.preview().candidates

    def test_automatic_recovery_archives_only_exact_obsolete_rejections(self):
        obsolete = self.attempt()
        other = self.attempt(error='device_revoked: keep')
        pending = self.attempt(error=None)
        acknowledged = self.attempt(acknowledged=True)
        with self.store() as store:
            retained = {i: store.load_attempt(i) for i in (other, pending, acknowledged)}
            result = self.recovery.archive_obsolete_attempts(self.root, store)
            self.assertEqual((obsolete,), result.removed_attempt_ids)
            manifest = self.recovery.verify_archive(result.archive_path)
            self.assertEqual('lab_policy_obsolete_identity_mismatch', manifest['authorization'])
            with self.assertRaises(KeyError):
                store.load_attempt(obsolete)
            for i, record in retained.items():
                self.assertEqual(record, store.load_attempt(i))
            self.assertIsNone(self.recovery.archive_obsolete_attempts(self.root, store))
        self.assertEqual([], self.service.actions)

    def test_automatic_recovery_defers_active_test(self):
        self.attempt()
        self.attempt(active=True)
        before = self.fixture.bytes()
        with self.store() as store:
            self.assertIsNone(self.recovery.archive_obsolete_attempts(self.root, store))
        self.assertEqual(before, self.fixture.bytes())

    def test_automatic_archive_failure_keeps_live_attempt(self):
        obsolete = self.attempt()
        with self.store() as store:
            before = store.load_attempt(obsolete)
            with patch.object(self.recovery, 'verify_archive', side_effect=self.recovery.RecoveryError('archive_failed')):
                with self.assertRaises(self.recovery.RecoveryError):
                    self.recovery.archive_obsolete_attempts(self.root, store)
            self.assertEqual(before, store.load_attempt(obsolete))

    def test_automatic_recovery_defers_until_update_health_is_recorded(self):
        obsolete = self.attempt()
        journal = self.root / 'updates/update-journal.json'
        journal.parent.mkdir(exist_ok=True)
        with self.store() as store:
            before = store.load_attempt(obsolete)
            for content in ('{"stage":"health_check"}', '{invalid', '{"stage":"rollback_failed"}'):
                journal.write_text(content)
                self.assertIsNone(self.recovery.archive_obsolete_attempts(self.root, store))
                self.assertEqual(before, store.load_attempt(obsolete))
            journal.write_text('{"stage":"healthy"}')
            self.assertEqual((obsolete,), self.recovery.archive_obsolete_attempts(self.root, store).removed_attempt_ids)

    def test_runtime_forgets_archived_attempt_and_can_return_to_test_selection(self):
        from ksat.client.runtime import AssessmentRuntime
        obsolete = self.attempt()
        with self.store() as store:
            identity = DeviceIdentityStore(self.root / 'identity').load_existing()
            runtime = AssessmentRuntime(store, identity, runtime_fixtures.FakeClock())
            runtime._attempt_id = obsolete
            runtime._questions = {'stale': 'must be removed'}
            result = runtime.archive_obsolete_attempts(self.root)
            self.assertEqual((obsolete,), result.removed_attempt_ids)
            self.assertIsNone(runtime.recover())
            self.assertEqual({}, runtime._questions)
            with self.assertRaisesRegex(RuntimeError, 'No local assessment'):
                runtime.snapshot()

    def test_context_recovers_startup_and_newly_rejected_attempt_without_restart(self):
        from client_app import _ClientContext, ClientServices
        from ksat.client.runtime import AssessmentRuntime
        from types import SimpleNamespace
        obsolete = self.attempt()
        with self.store() as store:
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            services = ClientServices(identities, store, runtime, SimpleNamespace(), SimpleNamespace(),
                maintenance_data_dir=self.root)
            context = _ClientContext(services)
            self.assertIsNone(context.recover())
            self.assertIsNone(context.startup_problem)
            self.assertEqual([], store.pending_submissions())
        rejected_later = self.attempt()
        with self.store() as store:
            services.store = store
            services.runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            services.runtime._attempt_id = rejected_later
            with self.recovery.MaintenanceGate(self.root):
                self.assertIsNone(context.archive_obsolete_attempts())
                self.assertIsNotNone(store.load_attempt(rejected_later))
            self.assertEqual((rejected_later,), context.archive_obsolete_attempts().removed_attempt_ids)
            self.assertEqual([], store.pending_submissions())

    def test_preview_exact_code_only_and_no_sensitive_text(self):
        eligible = self.attempt()
        self.attempt(error='attempt_identity_mismatch_extra: secret')
        self.attempt(error='device_revoked: secret')
        self.attempt(error=None)
        before = self.fixture.bytes()
        preview = self.backend.preview()
        self.assertEqual([eligible], [c.attempt_id for c in preview.candidates])
        self.assertIn('enrollment_revoked', preview.problem_codes)
        self.assertNotIn('private error', repr(preview))
        self.assertNotIn('secret', repr(preview))
        self.assertEqual(before, self.fixture.bytes())
        self.assertEqual([], self.service.actions)

    def test_state_api_returns_to_selection_after_deferred_automatic_archive(self):
        from client_app import ClientServices, create_client_app
        from ksat.client.runtime import AssessmentRuntime
        from tests.test_client_app_api import FakeCoordinator, FakeOutbox
        from fastapi.testclient import TestClient
        obsolete = self.attempt()
        self.attempt()  # Legacy PCs can have several obsolete queued attempts.
        journal = self.root / 'updates/update-journal.json'
        journal.parent.mkdir(exist_ok=True)
        journal.write_text('{"stage":"health_check"}')
        with self.store() as store:
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            coordinator = FakeCoordinator()
            coordinator.login('S100', 'fixture-password')
            services = ClientServices(identities, store, runtime, coordinator, FakeOutbox(),
                maintenance_data_dir=self.root)
            with TestClient(create_client_app(services), base_url='http://127.0.0.1:8010') as client:
                self.assertEqual('obsolete_attempt_recovery', client.get('/api/state').json()['state'])
                journal.write_text('{"stage":"healthy"}')
                state = client.get('/api/state').json()
                self.assertEqual('waiting_or_ready', state['state'])
                self.assertIsNone(state['problem'])
                self.assertIsNone(state['attempt'])
                self.assertEqual([], store.pending_submissions())
                self.assertEqual(409, client.get('/api/attempts/' + obsolete).status_code)

    def test_outcome_write_failure_does_not_leave_deleted_runtime_attempt(self):
        from ksat.client.runtime import AssessmentRuntime
        obsolete = self.attempt()
        write = self.recovery._durable_write
        def fail_completed(path, payload):
            if path.name == 'outcome.json' and json.loads(payload)['status'] == 'completed':
                raise OSError('outcome write failed')
            write(path, payload)
        with self.store() as store:
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            runtime._attempt_id = obsolete
            with patch.object(self.recovery, '_durable_write', side_effect=fail_completed):
                with self.assertRaises(OSError):
                    runtime.archive_obsolete_attempts(self.root)
            with self.assertRaisesRegex(RuntimeError, 'No local assessment'):
                runtime.snapshot()
            archives = list((self.root / 'recovery').glob('*/manifest.json'))
            self.assertEqual(1, len(archives))
            self.recovery.verify_archive(archives[0].parent)

    def test_deferred_archive_recovers_retained_submission_questions(self):
        from client_app import ClientServices, create_client_app
        from ksat.client.runtime import AssessmentRuntime
        from tests.test_client_app_api import FakeCoordinator, FakeOutbox
        from fastapi.testclient import TestClient
        self.attempt()
        retained = self.attempt(error='device_revoked: must retain')
        journal = self.root / 'updates/update-journal.json'
        journal.parent.mkdir(exist_ok=True)
        journal.write_text('{"stage":"health_check"}')
        with self.store() as store:
            before = store.load_attempt(retained)
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            coordinator = FakeCoordinator()
            coordinator.login('S100', 'fixture-password')
            services = ClientServices(identities, store, runtime, coordinator, FakeOutbox(),
                maintenance_data_dir=self.root)
            with TestClient(create_client_app(services), base_url='http://127.0.0.1:8010') as client:
                self.assertEqual('obsolete_attempt_recovery', client.get('/api/state').json()['state'])
                journal.write_text('{"stage":"healthy"}')
                self.assertEqual('faculty_intervention_required', client.get('/api/state').json()['state'])
                attempt = client.get('/api/attempts/' + retained)
                self.assertEqual(200, attempt.status_code, attempt.text)
                self.assertEqual(2, len(attempt.json()['questions']))
                self.assertEqual(before, store.load_attempt(retained))

    def test_live_archive_recovers_retained_submission_without_prior_deferral(self):
        from client_app import ClientServices, create_client_app
        from ksat.client.runtime import AssessmentRuntime
        from tests.test_client_app_api import FakeCoordinator, FakeOutbox
        from fastapi.testclient import TestClient
        obsolete = self.attempt(error=None)
        with self.store() as store:
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            coordinator = FakeCoordinator()
            coordinator.login('S100', 'fixture-password')
            services = ClientServices(identities, store, runtime, coordinator, FakeOutbox(),
                maintenance_data_dir=self.root)
            with TestClient(create_client_app(services), base_url='http://127.0.0.1:8010') as client:
                retained = self.attempt(error='device_revoked: must retain')
                before = store.load_attempt(retained)
                store.require_faculty_intervention(obsolete, last_error='attempt_identity_mismatch: obsolete')
                self.assertEqual('faculty_intervention_required', client.get('/api/state').json()['state'])
                attempt = client.get('/api/attempts/' + retained)
                self.assertEqual(200, attempt.status_code, attempt.text)
                self.assertEqual(2, len(attempt.json()['questions']))
                self.assertEqual(before, store.load_attempt(retained))

    def test_closing_browser_during_deferred_multi_attempt_recovery_preserves_queue(self):
        self.enterContext(patch('client_app._WINDOW_UPLOAD_GRACE_SECONDS', 0))
        import re
        import threading
        from client_app import ClientServices, create_client_app
        from ksat.client.runtime import AssessmentRuntime
        from ksat.client.lifecycle import BrowserLifetime
        from tests.test_client_app_api import FakeCoordinator, FakeOutbox
        from fastapi.testclient import TestClient
        self.attempt()
        self.attempt()
        journal = self.root / 'updates/update-journal.json'
        journal.parent.mkdir(exist_ok=True)
        journal.write_text('{"stage":"health_check"}')
        now = [0]
        lifetime = BrowserLifetime(clock=lambda: now[0])
        stopped = threading.Event()
        with self.store() as store:
            identities = DeviceIdentityStore(self.root / 'identity')
            runtime = AssessmentRuntime(store, identities.load_existing(), runtime_fixtures.FakeClock())
            services = ClientServices(identities, store, runtime, FakeCoordinator(), FakeOutbox(),
                maintenance_data_dir=self.root)
            app = create_client_app(services, lifetime=lifetime, request_shutdown=stopped.set)
            with TestClient(app, base_url='http://127.0.0.1:8010') as client:
                token = re.search(r'name="ksat-csrf" content="([\w-]+)"', client.get('/').text)[1]
                with client.websocket_connect('ws://127.0.0.1:8010/api/lifecycle/presence',
                        headers={'Origin': 'http://127.0.0.1:8010'}, subprotocols=['ksat-presence', token]) as socket:
                    socket.receive_json()
                now[0] = 500
                self.assertTrue(stopped.wait(5))
                self.assertEqual(2, len(store.pending_submissions()))

    def test_selected_archive_is_readable_and_unrelated_data_survives(self):
        selected = self.attempt()
        unrelated = self.attempt()
        pending = self.attempt(error=None)
        acknowledged = self.attempt(acknowledged=True)
        identity_before = (self.root / 'identity/device-key.bin').read_bytes()
        config_before = (self.root / 'client-config.json').read_bytes()
        with self.store() as store:
            retained = {i: store.load_attempt(i) for i in (unrelated, pending, acknowledged)}
        candidate = next(c for c in self.selected() if c.attempt_id == selected)
        result = self.backend.archive_practice([candidate], confirmed_practice=True)
        self.assertEqual((selected,), result.removed_attempt_ids)
        self.assertTrue(result.archive_path.is_relative_to(self.root / 'recovery'))
        manifest = self.recovery.verify_archive(result.archive_path)
        self.assertEqual([selected], manifest['selected_attempt_ids'])
        for name, digest in manifest['sha256'].items():
            self.assertEqual(digest, hashlib.sha256((result.archive_path / name).read_bytes()).hexdigest())
        self.assertNotIn('private_key', (result.archive_path / 'manifest.json').read_text())
        self.assertEqual(identity_before, (result.archive_path / 'identity/device-key.bin').read_bytes())
        with self.store() as store:
            with self.assertRaises(KeyError):
                store.load_attempt(selected)
            for i, record in retained.items():
                self.assertEqual(record, store.load_attempt(i))
            for table in ('submission_outbox', 'local_responses', 'local_integrity_events'):
                self.assertEqual(0, store.connection.execute(f'SELECT COUNT(*) FROM {table} WHERE attempt_id=?', (selected,)).fetchone()[0])
        self.assertEqual(identity_before, (self.root / 'identity/device-key.bin').read_bytes())
        self.assertEqual(config_before, (self.root / 'client-config.json').read_bytes())
        self.assertEqual(['stop', 'start'], self.service.actions)
        self.assertEqual('completed', json.loads((result.archive_path / 'outcome.json').read_bytes())['status'])

    def test_active_test_refused_before_service_stop(self):
        self.attempt()
        selected = self.selected()
        self.attempt(active=True)
        before = self.fixture.bytes()
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'active_attempt'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual([], self.service.actions)
        self.assertEqual(before, self.fixture.bytes())

    def test_active_test_race_after_stop_refused_and_service_restored(self):
        self.attempt()
        selected = self.selected()
        stop = self.service.stop
        def race():
            stop()
            self.attempt(active=True)
        self.service.stop = race
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'active_attempt'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual(['stop', 'start'], self.service.actions)
        self.assertEqual(1, len(self.selected()))

    def test_changed_selection_is_not_discarded(self):
        attempt = self.attempt()
        selected = self.selected()
        with self.store() as store:
            store.require_faculty_intervention(attempt, last_error='device_revoked: reason')
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'selection_changed'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        with self.store() as store:
            self.assertEqual('sealed_pending', store.load_attempt(attempt).state)

    def test_confirmation_required(self):
        self.attempt()
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'confirmation_required'):
            self.backend.archive_practice(self.selected(), confirmed_practice=False)
        self.assertEqual([], self.service.actions)

    def test_archive_failure_preserves_live_attempt(self):
        attempt = self.attempt()
        selected = self.selected()
        before = self.fixture.bytes()
        with patch.object(self.recovery, 'verify_archive', side_effect=OSError('disk failure secret')):
            with self.assertRaises(self.recovery.RecoveryError) as raised:
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertNotIn('secret', str(raised.exception))
        self.assertTrue(raised.exception.archive_path.exists())
        after = self.fixture.bytes()
        self.assertEqual(before, {k: after[k] for k in before})
        self.assertEqual(['stop', 'start'], self.service.actions)
        with self.store() as store:
            self.assertEqual('sealed_pending', store.load_attempt(attempt).state)

    def test_tampered_state_refused_without_anchor_repair(self):
        self.attempt()
        selected = self.selected()
        with closing(sqlite3.connect(self.root / 'state/client.sqlite3')) as db, db:
            db.execute("UPDATE submission_outbox SET last_error='tampered'")
        before = self.fixture.bytes()
        self.assertIn('authenticated_state_invalid', self.backend.preview().problem_codes)
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'authenticated_state_invalid'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual(before, self.fixture.bytes())
        self.assertEqual([], self.service.actions)

    def test_anchor_failure_retains_archive_and_does_not_rollback(self):
        attempt = self.attempt()
        selected = self.selected()
        old_anchor = (self.root / 'identity/state-anchor.json').read_bytes()
        with patch.object(ClientStore, '_write_anchor', side_effect=OSError('anchor failure')):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'mutation_requires_inspection') as raised:
                self.backend.archive_practice(selected, confirmed_practice=True)
        archive = raised.exception.archive_path
        self.recovery.verify_archive(archive)
        self.assertEqual(old_anchor, (self.root / 'identity/state-anchor.json').read_bytes())
        with closing(sqlite3.connect(self.root / 'state/client.sqlite3')) as db, db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM local_attempts WHERE attempt_id=?', (attempt,)).fetchone()[0])
        self.assertIn('authenticated_state_invalid', self.backend.preview().problem_codes)
        self.assertEqual(old_anchor, (self.root / 'identity/state-anchor.json').read_bytes())
        self.assertEqual(['stop', 'start'], self.service.actions)

    def test_rerun_does_not_mutate_or_create_second_archive(self):
        self.attempt()
        selected = self.selected()
        self.backend.archive_practice(selected, confirmed_practice=True)
        before = self.fixture.bytes()
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'selection_changed'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual(before, self.fixture.bytes())
        self.assertEqual((), self.selected())

    def test_missing_installation_not_created(self):
        root = self.root / 'missing'
        preview = self.recovery.ClientRecovery(root, service=self.service).preview()
        self.assertIn('installation_missing', preview.problem_codes)
        self.assertFalse(root.exists())

    def test_archive_tamper_detected(self):
        self.attempt()
        result = self.backend.archive_practice(self.selected(), confirmed_practice=True)
        (result.archive_path / 'identity/state-anchor.json').write_bytes(b'{}')
        with self.assertRaises(self.recovery.RecoveryError):
            self.recovery.verify_archive(result.archive_path)

    def test_lifecycle_exclusion_restores_service(self):
        self.attempt()
        selected = self.selected()
        with self.recovery.ClientRecoveryLock(self.root / 'state'):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'client_busy'):
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual(['stop', 'start'], self.service.actions)
        self.assertEqual(1, len(self.selected()))

    def test_cli_help_and_version_do_not_import_client_app(self):
        path = Path(__file__).resolve().parents[1]
        for flag in ('--help', '--version'):
            result = subprocess.run([sys.executable, str(path / 'client_recovery.py'), flag], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('KSAT', result.stdout)
        result = subprocess.run([sys.executable, '-c',
            "import ksat.client.recovery; import sys; assert 'client_app' not in sys.modules"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_actual_inactive_device_protocol_code_is_diagnosed_only(self):
        self.attempt(error='device_inactive: confidential')
        preview = self.backend.preview()
        self.assertEqual((), preview.candidates)
        self.assertEqual(('enrollment_revoked',), preview.problem_codes)

    def test_transaction_failure_rolls_back_and_preserves_verified_archive(self):
        attempt = self.attempt()
        selected = self.selected()
        with patch.object(ClientStore, '_append_authenticated_entry', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'mutation_requires_inspection') as raised:
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.recovery.verify_archive(raised.exception.archive_path)
        with self.store() as store:
            self.assertEqual('sealed_pending', store.load_attempt(attempt).state)
        self.assertEqual(selected, self.selected())
        self.assertEqual(['stop', 'start'], self.service.actions)

    def test_unrelated_trigger_side_effect_rolls_back_removal(self):
        attempt = self.attempt()
        with self.store() as store:
            store.connection.execute('CREATE TRIGGER unexpected_delete AFTER DELETE ON local_attempts BEGIN DELETE FROM cached_content_packs; END')
            store.connection.commit()
        selected = self.selected()
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'mutation_requires_inspection'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        with self.store() as store:
            self.assertEqual('sealed_pending', store.load_attempt(attempt).state)
            self.assertEqual(1, store.connection.execute('SELECT COUNT(*) FROM cached_content_packs').fetchone()[0])

    def test_selection_change_during_service_stop_is_revalidated(self):
        attempt = self.attempt()
        selected = self.selected()
        stop = self.service.stop
        def change():
            stop()
            with self.store() as store:
                store.require_faculty_intervention(attempt, last_error='device_inactive: confidential')
        self.service.stop = change
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'selection_changed'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertFalse((self.root / 'recovery').exists())
        self.assertEqual(['stop', 'start'], self.service.actions)

    def test_maintenance_exclusion_prevents_service_stop(self):
        self.attempt()
        selected = self.selected()
        with self.recovery.MaintenanceGate(self.root):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'maintenance_busy'):
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual([], self.service.actions)

    def test_stop_timeout_still_restores_service(self):
        self.attempt()
        selected = self.selected()
        stop = self.service.stop
        def timeout():
            stop()
            raise TimeoutError('sensitive detail')
        self.service.stop = timeout
        with self.assertRaisesRegex(self.recovery.RecoveryError, 'service_control_failed'):
            self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual(['stop', 'start'], self.service.actions)
        self.assertEqual(selected, self.selected())

    def test_restart_failure_reports_archive_of_completed_removal(self):
        self.attempt()
        selected = self.selected()
        with patch.object(self.service, 'start', side_effect=RuntimeError('private')):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'service_restart_failed') as raised:
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.recovery.verify_archive(raised.exception.archive_path)
        self.assertEqual((), self.selected())
        self.assertEqual('completed', json.loads((raised.exception.archive_path / 'outcome.json').read_bytes())['status'])

    def test_previous_authenticated_schema_without_event_ids_is_supported_without_migration(self):
        self.attempt()
        with self.store() as store:
            with store._transaction() as connection:
                connection.execute('DROP INDEX unique_client_integrity_event')
                connection.execute('ALTER TABLE local_integrity_events DROP COLUMN client_event_id')
        selected = self.selected()
        result = self.backend.archive_practice(selected, confirmed_practice=True)
        self.recovery.verify_archive(result.archive_path)
        with closing(sqlite3.connect(self.root / 'state/client.sqlite3')) as db, db:
            self.assertNotIn('client_event_id', [r[1] for r in db.execute('PRAGMA table_info(local_integrity_events)')])

    def test_invalid_config_and_identity_diagnosed_without_reset(self):
        self.attempt()
        config = self.root / 'client-config.json'
        original = config.read_bytes()
        config.write_bytes(b'{}')
        self.assertEqual(('configuration_invalid',), self.backend.preview().problem_codes)
        config.write_bytes(original)
        key = self.root / 'identity/device-key.bin'
        key.write_bytes(b'broken private material')
        self.assertEqual(('identity_invalid',), self.backend.preview().problem_codes)
        self.assertEqual(b'broken private material', key.read_bytes())

    def test_unprotected_directory_fails_before_stop(self):
        self.attempt()
        selected = self.selected()
        with patch.object(self.recovery, '_require_protected_directory', side_effect=ValueError('unsafe')):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'paths_unprotected'):
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual([], self.service.actions)

    def test_hardlinked_identity_refused(self):
        import os
        self.attempt()
        os.link(self.root / 'identity/device-key.bin', self.root / 'identity/other.bin')
        self.assertEqual(('paths_unprotected',), self.backend.preview().problem_codes)

    def test_gui_selection_confirmation_and_cancel_use_real_backend(self):
        import gc
        import tkinter as tk
        from tkinter import ttk
        import client_recovery
        # Tk objects must be finalized on their owning thread, before a later
        # concurrency test can trigger cyclic garbage collection on a worker.
        self.addCleanup(gc.collect)
        self.attempt()
        real_tk = tk.Tk
        for accept in (False, True):
            failures = []
            def make_window():
                root = real_tk()
                root.withdraw()
                def exercise():
                    try:
                        children = root.winfo_children()
                        tree = next(w for w in children if isinstance(w, ttk.Treeview))
                        tree.selection_set(tree.get_children()[0])
                        check = next(w for w in children if isinstance(w, ttk.Checkbutton))
                        check.invoke()
                        frame = next(w for w in children if isinstance(w, ttk.Frame))
                        button = next(w for w in frame.winfo_children() if w.cget('text') == 'Archive selected practice attempts')
                        button.invoke()
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        root.destroy()
                root.after(10, exercise)
                return root
            with patch('tkinter.Tk', side_effect=make_window), patch('tkinter.messagebox.askyesno', return_value=accept), patch('tkinter.messagebox.showinfo'), patch('tkinter.messagebox.showerror'):
                self.assertEqual(0, client_recovery.run_gui(self.backend))
            if failures:
                raise failures[0]
            self.assertEqual(0 if accept else 1, len(self.selected()))

    def test_process_crash_after_commit_preserves_archive_and_strict_anchor_diagnosis(self):
        self.attempt()
        old_anchor = (self.root / 'identity/state-anchor.json').read_bytes()
        code = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from tests.test_client_identity import PrefixProtector
from tests.test_client_install_guard import FakeService
from ksat.client import recovery
from ksat.client.store import ClientStore
with patch('ksat.client.identity._default_protector', return_value=PrefixProtector()), \
     patch.object(recovery, '_require_protected_directory'), \
     patch.object(recovery, '_create_protected_directory', side_effect=lambda p: p.mkdir(parents=True, exist_ok=True)):
    backend = recovery.ClientRecovery(Path(sys.argv[1]), service=FakeService(running=True))
    selected = backend.preview().candidates
    with patch.object(ClientStore, '_write_anchor', side_effect=lambda *a: os._exit(73)):
        backend.archive_practice(selected, confirmed_practice=True)
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, text=True, timeout=90)
        self.assertEqual(73, result.returncode, result.stderr)
        archives = list((self.root / 'recovery').iterdir())
        self.assertEqual(1, len(archives))
        self.recovery.verify_archive(archives[0])
        self.assertEqual('prepared', json.loads((archives[0] / 'outcome.json').read_bytes())['status'])
        self.assertEqual(('authenticated_state_invalid',), self.backend.preview().problem_codes)
        self.assertEqual(old_anchor, (self.root / 'identity/state-anchor.json').read_bytes())
        with self.recovery.MaintenanceGate(self.root), self.recovery.ClientRecoveryLock(self.root / 'state'):
            pass

    def test_link_in_protected_state_is_refused(self):
        import os
        self.attempt()
        link = self.root / 'state/linked-anchor'
        try:
            os.symlink(self.root / 'identity/state-anchor.json', link)
        except OSError as error:
            self.skipTest(f'Windows symlink creation unavailable: {error.winerror}')
        self.assertEqual(('paths_unprotected',), self.backend.preview().problem_codes)

    def test_service_initially_stopped_stays_stopped(self):
        self.attempt()
        self.service.running = False
        result = self.backend.archive_practice(self.selected(), confirmed_practice=True)
        self.recovery.verify_archive(result.archive_path)
        self.assertEqual([], self.service.actions)
        self.assertFalse(self.service.running)

    def test_writable_installation_parent_refused_before_service_stop(self):
        self.attempt()
        selected = self.selected()
        def protection(path, **kwargs):
            if path == self.root:
                raise ValueError('unprotected installation parent')
        with patch.object(self.recovery, '_require_protected_directory', side_effect=protection):
            with self.assertRaisesRegex(self.recovery.RecoveryError, 'paths_unprotected'):
                self.backend.archive_practice(selected, confirmed_practice=True)
        self.assertEqual([], self.service.actions)
