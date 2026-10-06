import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.test_client_app_api import FakeIdentityStore, FakeStore, FakeRuntime, FakeCoordinator, FakeOutbox, FakeSnapshot


class ClientLifecycleApiTests(unittest.TestCase):
    def setUp(self):
        from client_app import ClientServices, create_client_app
        from ksat.client.lifecycle import BrowserLifetime
        self.now = 0
        self.life = BrowserLifetime(clock=lambda: self.now)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'state').mkdir()
        self.store = FakeStore(None)
        self.runtime = FakeRuntime(self.store)
        self.runtime.checkpoint_for_close = lambda: self.runtime.snapshot()
        self.coord = FakeCoordinator()
        self.coord._session = SimpleNamespace(student_id='S100', student_name='Student', access_token='secret')
        self.outbox = FakeOutbox()
        self.services = ClientServices(FakeIdentityStore(), self.store, self.runtime, self.coord,
            self.outbox, maintenance_data_dir=root)
        self.stopped = threading.Event()
        self.app = create_client_app(self.services, lifetime=self.life, request_shutdown=self.stopped.set)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1:8010'))
        token = re.search(r'name="ksat-csrf" content="([\w-]+)"', self.client.get('/').text)[1]
        self.headers = {'Origin': 'http://127.0.0.1:8010', 'X-KSAT-CSRF': token}
        self.protocols = ['ksat-presence', token]

    def ws(self, **kwargs):
        return self.client.websocket_connect('ws://127.0.0.1:8010/api/lifecycle/presence',
            headers=kwargs.get('headers', {'Origin': self.headers['Origin']}),
            subprotocols=kwargs.get('protocols', self.protocols))

    def test_websocket_requires_exact_origin_host_and_csrf(self):
        for headers, protocols in [({}, self.protocols),
            ({'Origin': 'https://attacker.example'}, self.protocols),
            ({'Origin': self.headers['Origin'], 'Host': 'attacker.example'}, self.protocols),
            ({'Origin': self.headers['Origin']}, ['ksat-presence']),
            ({'Origin': self.headers['Origin']}, ['ksat-presence', 'wrong'])]:
            with self.subTest(headers=headers, protocols=protocols):
                with self.assertRaises(WebSocketDisconnect):
                    with self.ws(headers=headers, protocols=protocols): pass
        with self.ws() as socket:
            self.assertEqual('ksat-presence', socket.accepted_subprotocol)
            self.assertEqual({'state': 'connected'}, socket.receive_json())

    def test_last_tab_signs_out_and_stops_after_grace_but_refresh_cancels(self):
        with self.ws() as first:
            first.receive_json()
            with self.ws() as second:
                second.receive_json()
            self.now = 500
            self.assertFalse(self.stopped.wait(1.1))
        self.now = 514
        self.assertFalse(self.stopped.wait(1.1))
        with self.ws() as refreshed:
            refreshed.receive_json()
            self.now = 700
            self.assertFalse(self.stopped.wait(1.1))
        self.now = 715
        self.assertTrue(self.stopped.wait(3))
        self.assertIsNone(self.coord.session)
        self.assertEqual(409, self.client.post('/api/lifecycle/launch', json={}, headers=self.headers).status_code)
        self.assertEqual(503, self.client.post('/api/login', json={}, headers=self.headers).status_code)

    def test_maintenance_blocks_close_until_released(self):
        from ksat.client.install_guard import MaintenanceGate
        with MaintenanceGate(self.services.maintenance_data_dir):
            self.now = 121
            self.assertFalse(self.stopped.wait(1.2))
            self.assertFalse(self.life.closing)
        self.assertTrue(self.stopped.wait(3))

    def test_launch_reservation_requires_csrf_and_delays_stop(self):
        self.assertIn("ws://127.0.0.1:8010", self.client.get('/').headers['content-security-policy'])
        self.now = 100
        self.assertEqual(403, self.client.post('/api/lifecycle/launch', json={}).status_code)
        response = self.client.post('/api/lifecycle/launch', json={}, headers=self.headers)
        self.assertEqual({'state': 'ready', 'protocol': 1}, response.json())
        self.now = 219
        self.assertFalse(self.stopped.wait(1.2))
        self.now = 220
        self.assertTrue(self.stopped.wait(3))

    def test_checkpoint_failure_keeps_authority_alive(self):
        self.store.snapshot = FakeSnapshot()
        def fail(): raise OSError('disk unavailable')
        self.runtime.checkpoint_for_close = fail
        self.now = 121
        self.assertFalse(self.stopped.wait(1.2))
        self.assertFalse(self.life.closing)
        self.assertIsNotNone(self.coord.session)

    def test_pending_answers_are_retained_on_close(self):
        self.store.pending = [SimpleNamespace(attempt_id='pending')]
        with patch('client_app._WINDOW_UPLOAD_GRACE_SECONDS', 0):
            self.now = 121
            self.assertTrue(self.stopped.wait(3))
        self.assertEqual('pending', self.store.pending[0].attempt_id)

    def test_inflight_mutation_defers_shutdown(self):
        started, release = threading.Event(), threading.Event()
        async def work():
            from starlette.concurrency import run_in_threadpool
            started.set()
            await run_in_threadpool(release.wait, 5)
            return {'ok': True}
        self.app.post('/test-work')(work)
        thread = threading.Thread(target=lambda: self.client.post('/test-work', json={}, headers=self.headers))
        thread.start()
        self.assertTrue(started.wait(2))
        self.now = 121
        self.assertFalse(self.stopped.wait(1.1))
        release.set()
        thread.join(3)
        self.assertTrue(self.stopped.wait(3))
