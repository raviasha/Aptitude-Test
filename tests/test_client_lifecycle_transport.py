"""Real Uvicorn transport, disposable state; never touches Windows services."""
import json
import re
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import uvicorn
from websockets.sync.client import connect

from client_app import ClientServices, create_client_app
from ksat.client.lifecycle import BrowserLifetime
from tests.test_client_app_api import FakeIdentityStore, FakeStore, FakeRuntime, FakeCoordinator, FakeOutbox


def test_real_transport_last_tab_shutdown_and_reopen(tmp_path):
    (tmp_path / 'state').mkdir()
    now = [0.0]
    store = FakeStore(None)
    store.pending = [SimpleNamespace(attempt_id='offline-pending')]

    def launch():
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{listener.getsockname()[1]}'
        runtime = FakeRuntime(store)
        runtime.checkpoint_for_close = runtime.snapshot
        coordinator = FakeCoordinator()
        services = ClientServices(FakeIdentityStore(), store, runtime, coordinator,
                                  FakeOutbox(), maintenance_data_dir=tmp_path)
        life = BrowserLifetime(clock=lambda: now[0])
        app = create_client_app(services, lifetime=life, request_shutdown=lambda: setattr(server, 'should_exit', True))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', ws='websockets-sansio'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        return url, server, thread, listener, life, coordinator

    with patch('client_app._WINDOW_UPLOAD_GRACE_SECONDS', 0):
        for iteration in range(2):
            url, server, thread, listener, life, coordinator = launch()
            sockets = []
            try:
                with httpx.Client(base_url=url, trust_env=False) as client:
                    token = re.search(r'name="ksat-csrf" content="([\w-]+)"', client.get('/').text)[1]
                    headers = {'Origin': url, 'X-KSAT-CSRF': token}
                    assert client.post('/api/lifecycle/launch', json={}, headers=headers).status_code == 200
                    # Each connection represents a live document. Closing one
                    # and opening its refresh must not stop the other document.
                    for _ in range(2):
                        ws = connect(url.replace('http:', 'ws:') + '/api/lifecycle/presence',
                                     origin=url, subprotocols=['ksat-presence', token], proxy=None)
                        assert json.loads(ws.recv(timeout=2)) == {'state': 'connected'}
                        sockets.append(ws)
                    assert coordinator.session is None  # no previous student's session
                    login = client.post('/api/login', json={'student_id': 'S100', 'password': 'password'}, headers=headers)
                    assert login.status_code == 200, login.text
                    assert coordinator.session is not None
                    sockets.pop().close()
                    now[0] += 200
                    time.sleep(.4)
                    assert not life.closing
                    refreshed = connect(url.replace('http:', 'ws:') + '/api/lifecycle/presence',
                                        origin=url, subprotocols=['ksat-presence', token], proxy=None)
                    assert json.loads(refreshed.recv(timeout=2)) == {'state': 'connected'}
                    refreshed.close()
                    sockets.pop().close()
                    time.sleep(.1)  # let the server observe the final disconnect
                    now[0] += 15
                    thread.join(10)
                    assert not thread.is_alive()
                    assert coordinator.session is None
                    assert store.pending[0].attempt_id == 'offline-pending'
            finally:
                for ws in sockets:
                    ws.close()
                server.should_exit = True
                thread.join(10)
                listener.close()
