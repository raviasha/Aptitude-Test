import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ksat.coordinator.tls import load_or_create_coordinator_security
from ksat.public_trust import validate_public_bundle
from ksat.lab_builder.profile import make_lab_profile, encode_profile, decode_profile


class LabProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        security = load_or_create_coordinator_security(root, hostname="lab.example.edu", port=8443)
        cls.ca = security.ca_certificate_path.read_bytes()
        cls.metadata = (root / "public" / "coordinator-public.json").read_bytes()
        cls.url = "https://lab.example.edu:8443"
        cls.now = datetime.now(timezone.utc)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def bundle(self, **changes):
        return validate_public_bundle(changes.get("url", self.url), changes.get("ca", self.ca),
                                      changes.get("metadata", self.metadata), now=changes.get("now", self.now))

    def test_current_metadata_remains_compatible(self):
        self.assertEqual("2.1.0", json.loads(self.bundle().metadata_json)["version"])

    def test_duplicate_json_keys_rejected(self):
        for raw in (self.metadata.rstrip()[:-1] + b',"version":"2.1.0"}',
                    self.metadata.replace(b'"2.1.0"', b'NaN')):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                self.bundle(metadata=raw)

    def test_private_key_pem_rejected(self):
        for ca in (self.ca + b"\n-----BEGIN PRIVATE KEY-----\nsecret", self.ca * 2):
            metadata = json.loads(self.metadata)
            metadata["ca_sha256"] = hashlib.sha256(ca).hexdigest()
            with self.assertRaises(ValueError):
                self.bundle(ca=ca, metadata=json.dumps(metadata).encode())

    def test_url_binding_and_ca_key_fingerprint(self):
        for changes in ({"coordinator_url": "https://other.example.edu:8443"},
                        {"ca_sha256": "0" * 64}, {"signing_public_key_sha256": "0" * 64},
                        {"hostname": "other.example.edu"}, {"port": 443}, {"extra": 1},
                        {"version": "2.1.1"}):
            metadata = json.loads(self.metadata)
            metadata.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.bundle(metadata=json.dumps(metadata).encode())
        key = b"x" * 32
        metadata = json.loads(self.metadata)
        metadata.update(signing_public_key_b64=base64.b64encode(key).decode(),
                        signing_public_key_sha256=hashlib.sha256(key).hexdigest())
        with self.assertRaises(ValueError):
            self.bundle(metadata=json.dumps(metadata).encode())

    def test_expiry_checked(self):
        for days in (-36500, 36500):
            with self.assertRaises(ValueError):
                self.bundle(now=self.now + timedelta(days=days))

    def test_slug_cannot_escape_output(self):
        for name in ('Lab 1', 'Lab "/../ & whoami', 'Láb two'):
            slug = make_lab_profile(name, self.bundle()).lab_slug
            self.assertRegex(slug, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertLessEqual(len(slug), 64)
        for name in ('', ' ', 'CON', 'COM1', 'NUL', '数学', 'x' * 81):
            with self.subTest(name=name), self.assertRaises(ValueError):
                make_lab_profile(name, self.bundle())

    def test_profile_round_trip_is_canonical(self):
        profile = make_lab_profile(" Lab One ", self.bundle())
        encoded = encode_profile(profile)
        decoded = decode_profile(encoded, now=self.now)
        self.assertEqual(profile, decoded)
        self.assertEqual(encoded, encode_profile(decoded))
        tampered = json.loads(encoded)
        tampered["lab_name"] = "Lab Two"
        with self.assertRaises(ValueError):
            decode_profile(json.dumps(tampered).encode(), now=self.now)

    def test_sizes_and_unsafe_urls_rejected(self):
        for url in ("http://lab.example.edu:8443", self.url + "/x", self.url + "?q=x",
                    "https://user:pass@lab.example.edu:8443", self.url + "#x"):
            with self.assertRaises(ValueError):
                self.bundle(url=url)
        for changes in ({"ca": b"x" * (256 * 1024 + 1)},
                        {"metadata": b" " * (64 * 1024 + 1)}):
            with self.assertRaises(ValueError):
                self.bundle(**changes)
        with self.assertRaises(ValueError):
            decode_profile(b" " * (512 * 1024 + 1), now=self.now)
