"""Run the actual old helper code; its unbound journal cannot certify new health."""
import importlib.util
import json
import unittest
from pathlib import Path

from tests.test_client_updates import ClientUpdateManagerTests


class LegacyTransitionTests(ClientUpdateManagerTests):
    def test_actual_legacy_helper_success_is_not_promoted_to_managed_health(self):
        path = Path(__file__).parent / 'fixtures/legacy_client_updater_2_1_5.py'
        spec = importlib.util.spec_from_file_location('legacy_client_updater_test', path)
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.manager.check()
        self.manager.download()
        request = self.manager.prepare_install()
        class Installers:
            def run(self, *_args):
                pass  # OS installer boundary; real old journal/health logic runs.
        helper = module.Updater(self.manager.root, self.public, lambda *_args: None,
            None, Installers(), module.HealthVerifier(lambda: {'version':'2.1.0'}, lambda: True),
            installed_version='2.0.0', identity_digest='identity', config_digest='config', state_digest='state')
        self.assertTrue(helper.run(request).success)
        self.assertEqual({'stage':'healthy', 'diagnostic_code':None},
                         json.loads((self.manager.root/'update-journal.json').read_bytes()))
        self.manager.installed_version = '2.1.0'
        self.coordinator.policy = None
        snapshot = self.manager.check()
        self.assertEqual('legacy_update_receipt_unavailable', snapshot.diagnostic_code)
        self.assertNotIn('healthy', [s['stage'] for _, s in self.coordinator.statuses])
        self.assertEqual(1, len(list((self.manager.root/'legacy-evidence').iterdir())))
        self.assertEqual('current', self.manager.check().stage)


if __name__ == '__main__':
    unittest.main()
