import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from client_app import _ClientContext, _queue_payload


class UpdateLifecycleTests(unittest.TestCase):
    def context(self):
        manager = Mock()
        manager.check.return_value = SimpleNamespace(stage='required')
        manager.download.return_value = SimpleNamespace(stage='ready_to_install')
        manager.prepare_install.return_value = 'request'
        services = SimpleNamespace(update_manager=manager, maintenance_data_dir=None,
            updater_launcher=Mock(), store=SimpleNamespace(update_deferral_reason=lambda: 'pending_submission'))
        context = _ClientContext(services)
        context.identity = SimpleNamespace(device_id='enrolled')
        return context, manager, services

    def test_launcher_failure_is_visible_and_does_not_become_startup_failure(self):
        context, manager, services = self.context()
        services.updater_launcher.side_effect = OSError('not available')
        manager.launch_failed.return_value = SimpleNamespace(stage='failed')
        context._check_client_update(None)
        self.assertEqual('failed', context.update_snapshot.stage)
        self.assertIsNone(context.startup_problem)

    def test_owned_poll_thread_checks_and_stops_before_resources_close(self):
        context, manager, services = self.context()
        checked = threading.Event()
        context._check_client_update = lambda recovered: checked.set()
        context._start_update_thread(interval=0.01)
        self.assertTrue(checked.wait(2))
        context._stop_update_thread()
        self.assertIsNone(context._update_thread)

    def test_known_submission_error_is_safe_and_actionable(self):
        item = SimpleNamespace(retry_count=3, next_attempt_at=None,
            last_error='attempt_identity_mismatch: PRIVATE STUDENT PAYLOAD',
            status='faculty_intervention_required')
        output = _queue_payload(item)
        self.assertEqual('attempt_identity_mismatch', output['diagnostic_code'])
        self.assertIn('Recovery', output['last_error'])
        self.assertNotIn('PRIVATE', str(output))

    def test_unknown_error_does_not_expose_raw_data(self):
        item = SimpleNamespace(retry_count=3, next_attempt_at=None,
            last_error='PRIVATE PASSWORD', status='faculty_intervention_required')
        self.assertNotIn('PRIVATE', str(_queue_payload(item)))
