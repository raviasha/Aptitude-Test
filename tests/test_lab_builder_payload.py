import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ksat.lab_builder.payload import load_approved_payload, verify_payload, REQUIRED_FILES
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT


class LabPayloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in REQUIRED_FILES:
            (self.root / name).write_bytes(b"fixture public payload " + name.encode())
        (self.root / "publisher.cer").write_bytes(
            (Path(__file__).resolve().parents[1] / "release" / "KSATLabReleaseSigning.cer").read_bytes())
        (self.root / "update-release-public.json").write_text(json.dumps({
            "format_version": 1, "update_signing_public_key_b64": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="}))
        self.write_manifest()
        # OS signature verification is the only mocked trust boundary.
        self.signature = patch("ksat.lab_builder.payload.verify_authenticode",
                               return_value=SimpleNamespace(thumbprint=THUMBPRINT))
        self.signature.start()
        self.addCleanup(self.signature.stop)
        self.archive = patch("ksat.lab_builder.payload.pyinstaller_payload_manifest", create=True,
                             return_value={"C:x:update-release-public.json": hashlib.sha256(
                                 (self.root / "update-release-public.json").read_bytes()).hexdigest()})
        self.archive.start()
        self.addCleanup(self.archive.stop)

    def write_manifest(self):
        files = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in REQUIRED_FILES}
        self.manifest = dict(format_version=1, client_version="2.1.5", files=files,
                             publisher=PUBLISHER, thumbprint=THUMBPRINT)
        (self.root / "payload-manifest.json").write_text(json.dumps(self.manifest))

    def test_load_and_recheck_hashes(self):
        payload = load_approved_payload(self.root)
        verify_payload(payload)
        (self.root / "KSATClient.exe").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            verify_payload(payload)

    def test_unknown_files_and_private_material_rejected(self):
        (self.root / "key.pfx").write_bytes(b"private")
        with self.assertRaises(ValueError):
            load_approved_payload(self.root)
        (self.root / "key.pfx").unlink()
        (self.root / "publisher.cer").write_bytes(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
        self.write_manifest()
        with self.assertRaises(ValueError):
            load_approved_payload(self.root)

    def test_manifest_schema_pin_paths_and_version_rejected(self):
        for changes in ({"client_version": "2.1.0"}, {"publisher": "CN=Other"},
                        {"thumbprint": "0" * 40}, {"files": {"../secret": "0" * 64}}):
            (self.root / "payload-manifest.json").write_text(json.dumps(self.manifest | changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                load_approved_payload(self.root)

    def test_wrong_executable_signature_rejected(self):
        with patch("ksat.lab_builder.payload.verify_authenticode", return_value=SimpleNamespace(thumbprint="0" * 40)):
            with self.assertRaises(ValueError):
                load_approved_payload(self.root)

    def test_wrong_public_certificate_and_update_key_rejected(self):
        (self.root / "publisher.cer").write_bytes(b"invalid public certificate")
        self.write_manifest()
        with self.assertRaises(ValueError):
            load_approved_payload(self.root)

    def test_recursive_archive_failure_blocks(self):
        with patch("ksat.lab_builder.payload.pyinstaller_payload_manifest", create=True,
                   side_effect=ValueError("Private state inside archive")):
            with self.assertRaises(ValueError):
                load_approved_payload(self.root)
