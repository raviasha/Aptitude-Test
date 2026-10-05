"""Real TLS regressions: changing the dial name must not change server identity."""

import json
import socket
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import httpx

from ksat.client.coordinator import CoordinatorClient, CoordinatorProblem
from ksat.client.identity import DeviceIdentity
from ksat.coordinator.tls import load_or_create_coordinator_security
from ksat.crypto import generate_ed25519_keypair


@pytest.fixture(scope="module")
def security(tmp_path_factory):
    root = tmp_path_factory.mktemp("hostname-tls")
    return load_or_create_coordinator_security(root, hostname="lab-server.local")


@contextmanager
def server(security, host="127.0.0.1", port=0):
    received = []
    names = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.headers["Host"], self.path))
            data = json.dumps({"version": "2.1.0"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.headers["Host"], self.path, body, self.headers["Authorization"]))
            if self.path == "/drop-response":
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    httpd = ThreadingHTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(security.server_certificate_path, security.server_private_key_path)
    context.set_servername_callback(lambda sock, name, ctx: names.append(name))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, received, names
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def client(security, port, hostname="lab-server.local", timeout=3):
    private, public = generate_ed25519_keypair()
    return CoordinatorClient(
        f"https://{hostname}:{port}", security.ca_certificate_path,
        DeviceIdentity(private, public), timeout_seconds=timeout,
    )


def resolver(monkeypatch, *, primary=False, short_ip="127.0.0.1"):
    real = socket.getaddrinfo
    calls = []

    def resolve(host, port, *args, **kwargs):
        calls.append(host)
        if host == "lab-server.local" and primary:
            return real("127.0.0.1", port, *args, **kwargs)
        if host == "lab-server":
            return real(short_ip, port, *args, **kwargs)
        raise socket.gaierror(socket.EAI_NONAME, "Controlled lab DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    # No dependency on the test runner's proxy environment.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_dns_failure_uses_short_name_but_keeps_tls_and_http_identity(monkeypatch, security):
    # Removing the fallback breaks the real probe; changing SNI breaks this cert.
    with server(security) as (port, received, names):
        calls = resolver(monkeypatch)
        connection = client(security, port)
        try:
            assert connection.probe_build() == "2.1.0"
            assert connection.probe_build() == "2.1.0"
            assert connection.base_url == f"https://lab-server.local:{port}"
            assert received == [(f"lab-server.local:{port}", "/api/build")] * 2
            assert names == ["lab-server.local"] * 2
            assert calls == ["lab-server.local", "lab-server", "lab-server"]
        finally:
            connection.close()


def test_working_local_name_never_uses_short_name(monkeypatch, security):
    with server(security) as (port, received, names):
        calls = resolver(monkeypatch, primary=True)
        connection = client(security, port)
        try:
            assert connection.probe_build() == "2.1.0"
            assert calls == ["lab-server.local"]
        finally:
            connection.close()


def test_fallback_rejects_wrong_server_certificate(monkeypatch, security, tmp_path):
    rogue = load_or_create_coordinator_security(tmp_path, hostname="lab-server.local")
    with server(rogue) as (port, received, names):
        resolver(monkeypatch)
        connection = client(security, port)
        try:
            with pytest.raises(CoordinatorProblem):
                connection.probe_build()
            assert received == []  # No HTTP credentials/data reach an untrusted server.
            assert names == ["lab-server.local"]
        finally:
            connection.close()


@pytest.mark.parametrize("hostname", ["server.example.edu", "lab-server", "nested.lab.local"])
def test_non_single_label_local_names_do_not_fallback(monkeypatch, security, hostname):
    calls = resolver(monkeypatch)
    connection = client(security, 8443, hostname)
    try:
        with pytest.raises(CoordinatorProblem):
            connection.probe_build()
        assert calls == [hostname]
    finally:
        connection.close()


def test_certificate_failure_on_primary_does_not_trigger_fallback(monkeypatch, security, tmp_path):
    rogue = load_or_create_coordinator_security(tmp_path, hostname="lab-server.local")
    with server(rogue) as (port, received, names):
        calls = resolver(monkeypatch, primary=True)
        connection = client(security, port)
        try:
            with pytest.raises(CoordinatorProblem):
                connection.probe_build()
            assert calls == ["lab-server.local"]
            assert received == []
        finally:
            connection.close()


def test_fallback_posts_body_once_with_original_host(monkeypatch, security):
    with server(security) as (port, received, names):
        resolver(monkeypatch)
        connection = client(security, port)
        try:
            response = connection._client.post("/submission", content=b"saved answers",
                                               headers={"Authorization": "Bearer test-only-token"})
            assert response.status_code == 200
            assert received == [(f"lab-server.local:{port}", "/submission", b"saved answers", "Bearer test-only-token")]
            assert response.request.url.host == "lab-server.local"
        finally:
            connection.close()


def test_new_connections_resolve_current_address_not_cached_ip(monkeypatch, security):
    with server(security) as (port, first_received, _):
        with server(security, host="127.0.0.2", port=port) as (_, second_received, _):
            real = socket.getaddrinfo
            address = ["127.0.0.1"]

            def resolve(host, port, *args, **kwargs):
                if host == "lab-server":
                    return real(address[0], port, *args, **kwargs)
                raise socket.gaierror(socket.EAI_NONAME, "Controlled lab DNS failure")

            monkeypatch.setattr(socket, "getaddrinfo", resolve)
            connection = client(security, port)
            try:
                assert connection.probe_build() == "2.1.0"
                address[0] = "127.0.0.2"
                assert connection.probe_build() == "2.1.0"
                assert len(first_received) == len(second_received) == 1
            finally:
                connection.close()


def test_tcp_refusal_is_not_a_dns_failure(monkeypatch, security):
    # Reserved but not listening: a real refusal, not a simulated transport error.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        calls = resolver(monkeypatch, primary=True)
        connection = client(security, reserved.getsockname()[1])
        try:
            with pytest.raises(CoordinatorProblem):
                connection.probe_build()
            assert calls == ["lab-server.local"]
        finally:
            connection.close()


def test_builder_connection_check_uses_same_secure_fallback(monkeypatch, security):
    from types import SimpleNamespace
    from ksat.lab_builder.controller import BuilderController

    with server(security) as (port, received, names):
        resolver(monkeypatch)
        profile = SimpleNamespace(sha256="test-profile", trust=SimpleNamespace(
            base_url=f"https://lab-server.local:{port}", ca_pem=security.ca_certificate_pem))
        controller = BuilderController()
        assert controller.test_connection(profile) is True
        assert controller.verified_profile == "test-profile"
        assert received == [(f"lab-server.local:{port}", "/api/build")]
        assert names == ["lab-server.local"]


@pytest.mark.parametrize("primary", [True, False])
def test_post_is_not_replayed_after_server_receives_body(monkeypatch, security, primary):
    with server(security) as (port, received, names):
        resolver(monkeypatch, primary=primary)
        connection = client(security, port)
        try:
            assert connection.probe_build() == "2.1.0"
            with pytest.raises(httpx.RemoteProtocolError):
                connection._client.post("/drop-response", content=b"one submission",
                                        headers={"Authorization": "Bearer test-only-token"})
            posts = [item for item in received if item[1] == "/drop-response"]
            assert posts == [(f"lab-server.local:{port}", "/drop-response", b"one submission", "Bearer test-only-token")]
        finally:
            connection.close()


def test_original_name_is_retried_after_shortname_failure(monkeypatch, security):
    with server(security) as (port, received, names):
        real = socket.getaddrinfo
        mode = ["short"]
        calls = []

        def resolve(host, port, *args, **kwargs):
            calls.append(host)
            if (mode[0], host) in {("short", "lab-server"), ("primary", "lab-server.local")}:
                return real("127.0.0.1", port, *args, **kwargs)
            raise socket.gaierror(socket.EAI_NONAME, "Controlled DNS failure")

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        connection = client(security, port)
        try:
            assert connection.probe_build() == "2.1.0"
            mode[0] = "neither"
            with pytest.raises(CoordinatorProblem):
                connection.probe_build()
            mode[0] = "primary"
            assert connection.probe_build() == "2.1.0"
            assert calls == ["lab-server.local", "lab-server", "lab-server", "lab-server.local"]
            assert len(received) == 2
        finally:
            connection.close()


def test_same_ca_certificate_for_shortname_is_rejected(monkeypatch, security, tmp_path):
    from dataclasses import replace
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    leaf = x509.load_pem_x509_certificate(security.server_certificate_pem)
    # Controlled test CA only: issue a leaf with the WRONG name under the SAME CA.
    key = serialization.load_pem_private_key(
        (security.server_private_key_path.parent / "coordinator-ca.key.pem").read_bytes(), password=None)
    wrong = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
             .public_key(leaf.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(leaf.not_valid_before_utc).not_valid_after(leaf.not_valid_after_utc))
    for extension in leaf.extensions:
        value = (x509.SubjectAlternativeName([x509.DNSName("lab-server")])
                 if isinstance(extension.value, x509.SubjectAlternativeName) else extension.value)
        wrong = wrong.add_extension(value, extension.critical)
    cert = tmp_path / "wrong-hostname.pem"
    cert.write_bytes(wrong.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    with server(replace(security, server_certificate_path=cert)) as (port, received, names):
        resolver(monkeypatch)
        connection = client(security, port)
        try:
            with pytest.raises(CoordinatorProblem):
                connection.probe_build()
            assert names == ["lab-server.local"]
            assert received == []
        finally:
            connection.close()


def test_slow_failed_dns_does_not_prevent_first_fallback_probe(monkeypatch, security):
    import time

    with server(security) as (port, received, names):
        real = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host == "lab-server":
                return real("127.0.0.1", port, *args, **kwargs)
            # OS name resolution is not bounded by the socket connect timeout.
            time.sleep(.3)
            raise socket.gaierror(socket.EAI_NONAME, "Slow controlled DNS failure")

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        connection = client(security, port, timeout=.2)
        try:
            assert connection.probe_build() == "2.1.0"
            assert received == [(f"lab-server.local:{port}", "/api/build")]
            assert names == ["lab-server.local"]
        finally:
            connection.close()
