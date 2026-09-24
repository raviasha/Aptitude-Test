import base64
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ksat.crypto import generate_ed25519_keypair
from ksat.update_protocol import parse_client_update
from scripts.build_client_update import build_client_update


class BuildClientUpdateTests(unittest.TestCase):
    def test_builder_creates_deterministic_self_verifying_bundle_without_private_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = root / "KSATClientSetup-2.1.0.exe"
            installer.write_bytes(b"signed installer")
            private_b64, public_b64 = generate_ed25519_keypair()
            private_file = root / "offline.key"
            private_file.write_bytes(base64.b64decode(private_b64))
            output_one = root / "one.ksat-client-update"
            output_two = root / "two.ksat-client-update"
            kwargs = dict(
                installer=installer,
                version="2.1.0",
                minimum_source_version="2.0.0",
                publisher="CN=KSIT",
                private_key_file=private_file,
                release_id="11111111-1111-4111-8111-111111111111",
                published_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                release_notes="Managed updates.",
                authenticode_verifier=lambda _path, publisher: {"publisher": publisher},
            )
            build_client_update(output=output_one, **kwargs)
            build_client_update(output=output_two, **kwargs)

            self.assertEqual(output_one.read_bytes(), output_two.read_bytes())
            self.assertNotIn(base64.b64decode(private_b64), output_one.read_bytes())
            with zipfile.ZipFile(output_one) as archive:
                self.assertEqual(
                    ["manifest.json", "KSATClientSetup-2.1.0.exe", "manifest.sig"],
                    archive.namelist(),
                )
            parsed = parse_client_update(output_one, public_b64, lambda _path, publisher: {"publisher": publisher})
            self.assertEqual("2.1.0", parsed.manifest.client_version)

    def test_builder_rejects_equal_or_older_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = root / "setup.exe"
            installer.write_bytes(b"signed")
            private_b64, _ = generate_ed25519_keypair()
            key = root / "key"
            key.write_bytes(base64.b64decode(private_b64))
            with self.assertRaisesRegex(ValueError, "newer"):
                build_client_update(
                    installer=installer,
                    version="2.0.0",
                    minimum_source_version="2.0.0",
                    publisher="CN=KSIT",
                    private_key_file=key,
                    output=root / "bad.ksat-client-update",
                    release_id="11111111-1111-4111-8111-111111111111",
                    published_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                    release_notes="Bad version.",
                    authenticode_verifier=lambda _path, publisher: {"publisher": publisher},
                )


if __name__ == "__main__":
    unittest.main()
