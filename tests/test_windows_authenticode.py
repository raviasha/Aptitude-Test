import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ksat.windows_authenticode import AuthenticodeIdentity, verify_authenticode


class WindowsAuthenticodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "setup.exe"
        self.path.write_bytes(b"installer")
        self.now = datetime(2026, 9, 24, tzinfo=timezone.utc)
        self.identity = AuthenticodeIdentity(
            publisher="CN=KSIT Release Signing, O=KSIT",
            thumbprint="A" * 40,
            not_before=self.now - timedelta(days=1),
            not_after=self.now + timedelta(days=1),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_trusted_current_exact_publisher_is_returned(self):
        result = verify_authenticode(
            self.path,
            " cn=KSIT   Release Signing, o=KSIT ",
            trust_verifier=lambda _path: 0,
            identity_reader=lambda _path: self.identity,
            now=self.now,
        )
        self.assertEqual(self.identity, result)

    def test_untrusted_expired_future_and_wrong_publisher_are_rejected(self):
        cases = (
            (lambda _path: 5, self.identity, self.identity.publisher, "trusted"),
            (lambda _path: 0, AuthenticodeIdentity(self.identity.publisher, "A" * 40, self.now - timedelta(days=2), self.now - timedelta(seconds=1)), self.identity.publisher, "valid"),
            (lambda _path: 0, AuthenticodeIdentity(self.identity.publisher, "A" * 40, self.now + timedelta(seconds=1), self.now + timedelta(days=2)), self.identity.publisher, "valid"),
            (lambda _path: 0, self.identity, "CN=Another Publisher", "publisher"),
        )
        for trust, identity, publisher, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                verify_authenticode(
                    self.path,
                    publisher,
                    trust_verifier=trust,
                    identity_reader=lambda _path, value=identity: value,
                    now=self.now,
                )


if __name__ == "__main__":
    unittest.main()
