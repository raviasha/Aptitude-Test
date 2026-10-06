import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor
import httpx


class LauncherTests(unittest.TestCase):
    def test_native_handle_is_exact_service_start_query_only(self):
        from ksat.client.launcher import WindowsClientService
        api = MagicMock()
        api.OpenSCManagerW.return_value = 11
        api.OpenServiceW.return_value = 12
        with patch('ksat.client.launcher.ctypes.WinDLL', return_value=api):
            service = WindowsClientService()
            service.close()
        api.OpenSCManagerW.assert_called_once_with(None, None, 1)
        api.OpenServiceW.assert_called_once_with(11, 'KSATLabClientAuthority', 0x14)
        self.assertEqual([((11,), {}), ((12,), {})], api.CloseServiceHandle.call_args_list)

    def test_two_launchers_tolerate_already_started_race(self):
        from ksat.client.launcher import WindowsClientService, ensure_client_running
        import ctypes
        def launch():
            service = WindowsClientService.__new__(WindowsClientService)
            service.api = MagicMock()
            service.handle = 12
            service.api.StartServiceW.return_value = False
            states = iter([1, 4])
            service.query = lambda: next(states)
            # Windows last-error is thread-local; exercise the real wrapper's
            # already-running case without creating or changing any service.
            service.api.StartServiceW.side_effect = lambda *_: ctypes.set_last_error(1056) or False
            ensure_client_running(service=service, probe=lambda _: True, sleep=lambda _: None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: launch(), range(2)))

    def test_missing_service_is_a_readable_error(self):
        from ksat.client.launcher import ensure_client_running, LauncherError
        with patch('ksat.client.launcher.WindowsClientService', side_effect=OSError(1060, 'missing')):
            with self.assertRaisesRegex(LauncherError, 'installer'):
                ensure_client_running()

    def test_permission_provisioning_requires_admin_and_is_separate_operation(self):
        from client_app import main
        with patch('ksat.client.service_access.configure_launcher_access') as provision:
            with self.assertRaises(PermissionError):
                main(['--configure-launcher-access'], environ={}, administrator_check=lambda: False)
            provision.assert_not_called()
            self.assertEqual(0, main(['--configure-launcher-access'], environ={}, administrator_check=lambda: True))
            provision.assert_called_once_with()
            with self.assertRaises(SystemExit):
                main(['--configure-launcher-access', '--open-client'], environ={})

    def test_states_start_only_when_stopped_and_open_only_after_lease(self):
        from ksat.client.launcher import ensure_client_running
        for states, starts in [([1, 2, 4], 1), ([4], 0), ([3, 1, 2, 4], 1), ([2, 4], 0)]:
            with self.subTest(states=states):
                service = FakeService(states)
                probes = []
                ensure_client_running(service=service, probe=lambda _remaining: probes.append(True) or True, sleep=lambda _: None)
                self.assertEqual(starts, service.starts)
                self.assertEqual([True], probes)

    def test_stopping_after_failed_probe_restarts_and_retries(self):
        from ksat.client.launcher import ensure_client_running
        service = FakeService([4, 3, 1, 2, 4])
        probes = iter([False, True])
        ensure_client_running(service=service, probe=lambda _: next(probes), sleep=lambda _: None)
        self.assertEqual(1, service.starts)

    def test_timeout_and_start_failure_never_open_browser(self):
        from ksat.client.launcher import ensure_client_running, LauncherError
        now = [0]
        with self.assertRaises(LauncherError):
            ensure_client_running(service=FakeService([2]), timeout_seconds=2,
                clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        service = FakeService([1])
        def denied(): raise OSError(5, 'Access denied')
        service.start = denied
        with self.assertRaises(LauncherError):
            ensure_client_running(service=service, probe=lambda _: True)

    def test_probe_validates_product_version_csrf_and_reserves_lease(self):
        from ksat.client.launcher import reserve_client_launch
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == '/api/build': return httpx.Response(200, json={'version': '2.1.2'})
            if request.url.path == '/': return httpx.Response(200, text='<meta name="ksat-csrf" content="abc_DEF-123">')
            self.assertEqual('http://127.0.0.1:8010', request.headers['origin'])
            self.assertEqual('abc_DEF-123', request.headers['x-ksat-csrf'])
            return httpx.Response(200, json={'state': 'ready', 'protocol': 1})
        with httpx.Client(transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8010') as client:
            self.assertTrue(reserve_client_launch(client, expected_version='2.1.2', timeout=2))
        self.assertEqual(['/api/build', '/', '/api/lifecycle/launch'], [r.url.path for r in calls])

    def test_wrong_local_listener_cannot_pass_readiness(self):
        from ksat.client.launcher import reserve_client_launch, LauncherError
        for payload in ({'hello': 'world'}, {'version': 'unexpected'}):
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)), base_url='http://127.0.0.1:8010') as client:
                with self.assertRaises(LauncherError):
                    reserve_client_launch(client, expected_version='2.1.2', timeout=2)

    def test_main_does_not_open_browser_when_start_fails(self):
        from client_app import main
        from ksat.client.launcher import LauncherError
        opened, errors = [], []
        def fail(): raise LauncherError('Not ready')
        self.assertEqual(1, main(['--open-client'], environ={}, browser_opener=opened.append,
            client_starter=fail, launcher_error=errors.append))
        self.assertEqual([], opened)
        self.assertEqual(['Not ready'], errors)


class FakeService:
    def __init__(self, states):
        self.states = list(states)
        self.starts = 0
    def query(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]
    def start(self): self.starts += 1


class ServiceAccessTests(unittest.TestCase):
    def test_native_acl_adds_start_query_only_and_preserves_existing_aces(self):
        from ksat.client.service_access import student_start_descriptor
        original = 'D:(A;;KA;;;SY)(A;;KA;;;BA)(A;;LCRP;;;S-1-5-21-1-2-3-1001)'
        updated = student_start_descriptor(original)
        self.assertIn('(A;;LCRP;;;BU)', updated)
        self.assertIn('(A;;KA;;;SY)', updated)
        self.assertIn('(A;;KA;;;BA)', updated)
        self.assertIn('(A;;LCRP;;;S-1-5-21-1-2-3-1001)', updated)
        self.assertEqual(updated, student_start_descriptor(updated))

    def test_null_dacl_is_rejected_rather_than_widening_access(self):
        from ksat.client.service_access import student_start_descriptor
        with self.assertRaises(ValueError): student_start_descriptor('O:SYG:SY')

    def test_native_acl_preserves_explicit_deny(self):
        from ksat.client.service_access import student_start_descriptor
        updated = student_start_descriptor('D:(D;;WP;;;BU)(A;;KA;;;SY)(A;;KA;;;BA)')
        self.assertIn('(D;;WP;;;BU)', updated)
        self.assertIn('(A;;LCRP;;;BU)', updated)
