import base64
import hashlib
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ksat.crypto import generate_ed25519_keypair, sign_bytes
from ksat.protocol import canonical_json
from ksat.update_protocol import (
    ClientUpdateManifest,
    compare_versions,
    load_update_public_key,
    parse_client_update,
)


class ClientUpdateProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.private_key, self.public_key = generate_ed25519_keypair()
        self.installer = b"signed windows installer"
        self.publisher = "CN=KSIT Release Signing, O=KSIT"
        self.manifest = ClientUpdateManifest(
            format_version=1,
            release_id=str(uuid.uuid4()),
            client_version="2.1.0",
            minimum_source_version="2.0.0",
            target_os="windows",
            target_architecture="x86_64",
            installer_filename="KSATClientSetup-2.1.0.exe",
            installer_size=len(self.installer),
            installer_sha256=hashlib.sha256(self.installer).hexdigest(),
            authenticode_publisher=self.publisher,
            published_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
            release_notes="Adds managed updates.",
            health_check_timeout_seconds=60,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _bundle(self, *, manifest_bytes=None, installer=None, signature=None, extra=()):
        manifest_bytes = manifest_bytes or canonical_json(self.manifest)
        installer = self.installer if installer is None else installer
        signature = signature or sign_bytes(self.private_key, manifest_bytes)
        path = self.root / f"{uuid.uuid4()}.ksat-client-update"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            archive.writestr(self.manifest.installer_filename, installer)
            archive.writestr("manifest.sig", signature)
            for name, value in extra:
                archive.writestr(name, value)
        return path

    def _authenticode(self, path, expected_publisher):
        self.assertEqual(self.installer, Path(path).read_bytes())
        self.assertEqual(self.publisher, expected_publisher)
        return {"publisher": expected_publisher, "thumbprint": "A" * 40}

    def test_verified_bundle_binds_manifest_signature_and_installer(self):
        result = parse_client_update(self._bundle(), self.public_key, self._authenticode)

        self.assertEqual("2.1.0", result.manifest.client_version)
        self.assertEqual(hashlib.sha256(self.installer).hexdigest(), result.manifest.installer_sha256)
        self.assertEqual(hashlib.sha256(self._bundle().read_bytes()).hexdigest(), parse_client_update(self._bundle(), self.public_key, self._authenticode).bundle_sha256)

    def test_signature_and_installer_mutation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "signature"):
            self._bundle(signature=base64.b64encode(b"x" * 64).decode("ascii"))
            parse_client_update(self._bundle(signature=base64.b64encode(b"x" * 64).decode("ascii")), self.public_key, self._authenticode)
        with self.assertRaisesRegex(ValueError, "digest|size"):
            parse_client_update(self._bundle(installer=b"changed"), self.public_key, self._authenticode)

    def test_rejects_duplicate_json_keys_extra_members_and_oversized_metadata(self):
        duplicate = canonical_json(self.manifest).decode("utf-8").replace(
            '"format_version":1', '"format_version":1,"format_version":1', 1
        ).encode("utf-8")
        cases = (
            self._bundle(manifest_bytes=duplicate, signature=sign_bytes(self.private_key, duplicate)),
            self._bundle(extra=(("unexpected.txt", b"x"),)),
        )
        for bad in cases:
            with self.subTest(path=bad.name), self.assertRaises(ValueError):
                parse_client_update(bad, self.public_key, self._authenticode)

        with patch("ksat.update_protocol.MAX_UNCOMPRESSED_BYTES", 20):
            with self.assertRaisesRegex(ValueError, "large"):
                parse_client_update(self._bundle(), self.public_key, self._authenticode)

    def test_manifest_rejects_unsafe_names_versions_platform_and_noncanonical_json(self):
        base = self.manifest.model_dump(mode="json")
        invalid = (
            {**base, "installer_filename": "../setup.exe"},
            {**base, "client_version": "2.1"},
            {**base, "target_architecture": "arm64"},
            {**base, "published_at": "2026-09-24T00:00:00"},
        )
        for value in invalid:
            raw = canonical_json(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_client_update(self._bundle(manifest_bytes=raw, signature=sign_bytes(self.private_key, raw)), self.public_key, self._authenticode)
        pretty = json.dumps(base, indent=2).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "canonical"):
            parse_client_update(self._bundle(manifest_bytes=pretty, signature=sign_bytes(self.private_key, pretty)), self.public_key, self._authenticode)

    def test_version_comparison_is_numeric_and_strict(self):
        self.assertEqual(1, compare_versions("2.10.0", "2.9.9"))
        self.assertEqual(0, compare_versions("2.1.0", "2.1.0"))
        self.assertEqual(-1, compare_versions("2.0.9", "2.1.0"))
        for invalid in ("2.1", "v2.1.0", "2.1.0-beta"):
            with self.assertRaises(ValueError):
                compare_versions(invalid, "2.1.0")

    def test_authenticode_publisher_failure_is_propagated(self):
        def rejected(_path, _publisher):
            raise ValueError("Authenticode publisher mismatch.")

        with self.assertRaisesRegex(ValueError, "publisher mismatch"):
            parse_client_update(self._bundle(), self.public_key, rejected)

    def test_public_key_metadata_is_strict_and_never_accepts_private_material(self):
        valid = self.root / "update-release-public.json"
        valid.write_bytes(canonical_json({
            "format_version": 1,
            "update_signing_public_key_b64": self.public_key,
        }))
        self.assertEqual(self.public_key, load_update_public_key(valid))

        for value in (
            {"format_version": 1, "update_signing_public_key_b64": self.public_key, "private_key": "secret"},
            {"format_version": 1, "update_signing_public_key_b64": "bad"},
        ):
            valid.write_bytes(canonical_json(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_update_public_key(valid)


if __name__ == "__main__":
    unittest.main()
