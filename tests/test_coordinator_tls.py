import base64
import hashlib
import ipaddress
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from client_app import ClientConfig, _validate_production_trust
from ksat.coordinator.tls import (
    COORDINATOR_SIGNING_KEY_OID,
    load_or_create_coordinator_security,
    renew_coordinator_server_certificate,
)


UTC = timezone.utc


class CoordinatorTlsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 9, 2, 8, 30, tzinfo=UTC)

    def tearDown(self):
        self.temporary.cleanup()

    def _load(self, **overrides):
        values = {
            "hostname": "ksat-coordinator.local",
            "port": 8443,
            "lan_ip_addresses": ["192.168.10.20", "10.4.0.12"],
            "now": self.now,
        }
        values.update(overrides)
        return load_or_create_coordinator_security(self.root, **values)

    def test_first_run_is_stable_and_certificate_has_exact_server_purpose(self):
        first = self._load()
        second = self._load()

        self.assertEqual(first.signing_public_key_b64, second.signing_public_key_b64)
        self.assertEqual(first.pack_master_key, second.pack_master_key)
        self.assertEqual(first.enrollment_code, second.enrollment_code)
        self.assertGreaterEqual(len(first.enrollment_code), 24)
        self.assertEqual(first.ca_certificate_pem, second.ca_certificate_pem)
        self.assertEqual(first.server_certificate_pem, second.server_certificate_pem)

        ca = x509.load_pem_x509_certificate(first.ca_certificate_pem)
        server = x509.load_pem_x509_certificate(first.server_certificate_pem)
        self.assertTrue(
            ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        self.assertFalse(
            server.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        usages = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual([ExtendedKeyUsageOID.SERVER_AUTH], list(usages))
        key_usage = server.extensions.get_extension_for_class(x509.KeyUsage).value
        self.assertTrue(key_usage.digital_signature)
        self.assertTrue(key_usage.key_encipherment)
        self.assertFalse(key_usage.key_cert_sign)
        self.assertEqual(ca.subject, server.issuer)
        ca.public_key().verify(
            server.signature,
            server.tbs_certificate_bytes,
            padding.PKCS1v15(),
            server.signature_hash_algorithm,
        )

        names = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        self.assertEqual(
            ["ksat-coordinator.local"], names.get_values_for_type(x509.DNSName)
        )
        self.assertEqual(
            {
                ipaddress.ip_address("127.0.0.1"),
                ipaddress.ip_address("192.168.10.20"),
                ipaddress.ip_address("10.4.0.12"),
            },
            set(names.get_values_for_type(x509.IPAddress)),
        )
        self.assertNotIn(
            ipaddress.ip_address("0.0.0.0"), names.get_values_for_type(x509.IPAddress)
        )

        private_key = serialization.load_pem_private_key(
            first.server_private_key_pem, password=None
        )
        self.assertIsInstance(private_key, rsa.RSAPrivateKey)
        self.assertGreaterEqual(private_key.key_size, 3072)
        self.assertEqual(
            private_key.public_key().public_numbers(), server.public_key().public_numbers()
        )

    def test_ca_cryptographically_binds_the_protocol_signing_key(self):
        security = self._load()
        ca = x509.load_pem_x509_certificate(security.ca_certificate_pem)
        extension = ca.extensions.get_extension_for_oid(COORDINATOR_SIGNING_KEY_OID)
        self.assertEqual(
            base64.b64decode(security.signing_public_key_b64, validate=True),
            extension.value.value,
        )

        good = ClientConfig(
            coordinator_base_url="https://ksat-coordinator.local:8443",
            trusted_ca_path=str(security.ca_certificate_path),
            coordinator_signing_public_key_b64=security.signing_public_key_b64,
        )
        _validate_production_trust(good)
        self.assertEqual(
            "https://ksat-coordinator.local:8443", good.coordinator_base_url
        )
        wrong_key = base64.b64encode(b"x" * 32).decode("ascii")
        with self.assertRaisesRegex(ValueError, "Client configuration is invalid"):
            _validate_production_trust(ClientConfig(
                coordinator_base_url="https://ksat-coordinator.local:8443",
                trusted_ca_path=str(security.ca_certificate_path),
                coordinator_signing_public_key_b64=wrong_key,
            ))

        trailing = self.root / "trailing-ca.pem"
        trailing.write_bytes(security.ca_certificate_pem + b"\nnot-a-certificate")
        with self.assertRaisesRegex(ValueError, "Client configuration is invalid"):
            _validate_production_trust(ClientConfig(
                coordinator_base_url="https://ksat-coordinator.local:8443",
                trusted_ca_path=str(trailing),
                coordinator_signing_public_key_b64=security.signing_public_key_b64,
            ))

    def test_public_export_contains_only_public_trust_and_normalized_guidance(self):
        security = self._load()
        exported = {
            path.name: path.read_bytes()
            for path in security.public_export_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(
            {"coordinator-ca.pem", "coordinator-public.json"}, set(exported)
        )
        metadata = json.loads(exported["coordinator-public.json"])
        self.assertEqual(
            {
                "ca_sha256",
                "coordinator_url",
                "hostname",
                "port",
                "signing_public_key_b64",
                "signing_public_key_sha256",
                "version",
            },
            set(metadata),
        )
        self.assertEqual(
            "https://ksat-coordinator.local:8443", metadata["coordinator_url"]
        )
        self.assertEqual(
            hashlib.sha256(exported["coordinator-ca.pem"]).hexdigest(),
            metadata["ca_sha256"],
        )
        combined = b"\n".join(exported.values())
        forbidden = [
            first
            for first in (
                security.signing_private_key_b64.encode("ascii"),
                security.pack_master_key,
                security.enrollment_code.encode("ascii"),
                security.server_private_key_pem,
                security.ca_private_key_pem,
                security.browser_session_secret.encode("ascii"),
                security.client_session_secret.encode("ascii"),
            )
            if first in combined
        ]
        self.assertEqual([], forbidden)

    def test_missing_public_export_is_rebuilt_but_private_loss_or_corruption_fails_closed(self):
        security = self._load()
        public_json = security.public_export_dir / "coordinator-public.json"
        public_json.unlink()
        recovered = self._load()
        self.assertEqual(security.signing_public_key_b64, recovered.signing_public_key_b64)
        self.assertTrue(public_json.is_file())

        private_files = [
            self.root / "secrets" / "protocol-signing.key",
            self.root / "secrets" / "pack-master.key",
            self.root / "secrets" / "enrollment.code",
            self.root / "secrets" / "coordinator-ca.key.pem",
            self.root / "secrets" / "coordinator-server.key.pem",
            self.root / "secrets" / "browser-session.key",
            self.root / "secrets" / "client-session.key",
        ]
        for path in private_files:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.unlink()
                before = self._snapshot()
                with self.assertRaises(ValueError):
                    self._load()
                self.assertEqual(before, self._snapshot())
                path.write_bytes(original)

        target = self.root / "secrets" / "coordinator-server.key.pem"
        original = target.read_bytes()
        target.write_bytes(b"corrupt")
        before = self._snapshot()
        with self.assertRaises(ValueError):
            self._load()
        self.assertEqual(before, self._snapshot())
        target.write_bytes(original)

    def test_expiry_hostname_and_lan_changes_require_explicit_renewal(self):
        security = self._load()
        before = self._snapshot()
        with self.assertRaises(ValueError):
            self._load(now=self.now + timedelta(days=900))
        self.assertEqual(before, self._snapshot())
        with self.assertRaises(ValueError):
            self._load(hostname="changed.example.edu")
        self.assertEqual(before, self._snapshot())
        with self.assertRaises(ValueError):
            self._load(lan_ip_addresses=["192.168.10.99"])
        self.assertEqual(before, self._snapshot())

        renewed = renew_coordinator_server_certificate(
            self.root,
            hostname="changed.example.edu",
            port=9443,
            lan_ip_addresses=["192.168.10.99"],
            now=self.now + timedelta(days=30),
            confirmed=True,
        )
        self.assertEqual(security.signing_public_key_b64, renewed.signing_public_key_b64)
        self.assertEqual(security.ca_certificate_pem, renewed.ca_certificate_pem)
        self.assertNotEqual(
            security.server_certificate_pem, renewed.server_certificate_pem
        )
        metadata = json.loads(
            (renewed.public_export_dir / "coordinator-public.json").read_text("utf-8")
        )
        self.assertEqual("https://changed.example.edu:9443", metadata["coordinator_url"])

    def test_concurrent_first_run_publishes_one_complete_identity(self):
        barrier = threading.Barrier(12)

        def load(_index):
            barrier.wait()
            return self._load()

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(load, range(12)))
        self.assertEqual(1, len({item.signing_public_key_b64 for item in results}))
        self.assertEqual(1, len({item.ca_certificate_pem for item in results}))
        self.assertEqual(1, len({item.server_certificate_pem for item in results}))
        self.assertEqual(1, len({item.enrollment_code for item in results}))
        self._load()

    def _snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
