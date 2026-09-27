import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ksat.lab_builder.signing import SignerSelection, inspect_signer, sign_from_store, PUBLISHER, THUMBPRINT
from ksat.lab_builder.signing import _INSPECT_SCRIPT
from ksat.windows_authenticode import AuthenticodeIdentity


class LabSignerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows certificate provider required")
    def test_native_certificate_metadata_reports_code_signing_usage(self):
        certificate = Path(__file__).resolve().parents[1] / "release" / "KSATLabReleaseSigning.cer"
        # Substitute only the certificate lookup with the shipped PUBLIC fixture.
        script = _INSPECT_SCRIPT.replace(
            "$c=Get-Item -LiteralPath $path",
            "$c=[Security.Cryptography.X509Certificates.X509Certificate2]::new($env:KSAT_TEST_PUBLIC_CERT)",
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=dict(os.environ, KSAT_TEST_PUBLIC_CERT=str(certificate)), shell=False,
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertEqual(["1.3.6.1.5.5.7.3.3"], metadata["eku"])
        self.assertFalse(metadata["has_private_key"])

    @unittest.skipUnless(sys.platform == "win32", "Windows certificate provider required")
    def test_fresh_powershell_can_lookup_certificate_store(self):
        # Read-only native boundary check: no real private key or certificate import.
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _INSPECT_SCRIPT],
            env=dict(os.environ, KSAT_SIGN_STORE="CurrentUser", KSAT_SIGN_THUMBPRINT="0" * 40),
            shell=False, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("DriveNotFound", result.stderr)
        self.assertIn("ItemNotFound", result.stderr)

    def setUp(self):
        self.selection = SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True)
        self.now = datetime.now(timezone.utc)
        self.record = dict(publisher=PUBLISHER, thumbprint=THUMBPRINT,
                           not_before=(self.now - timedelta(days=1)).isoformat(),
                           not_after=(self.now + timedelta(days=730)).isoformat(),
                           has_private_key=True, eku=["1.3.6.1.5.5.7.3.3"])

    def run_inspection(self, record):
        with patch("ksat.lab_builder.signing.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(record))) as child:
            result = inspect_signer(self.selection)
        self.assertFalse(child.call_args.kwargs["shell"])
        return result

    def test_exact_store_and_pin_without_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.exe"
            path.write_bytes(b"test executable")
            tool = Path(directory) / "signtool.exe"
            tool.write_bytes(b"tool")
            identity = AuthenticodeIdentity(PUBLISHER, THUMBPRINT, self.now - timedelta(days=1),
                                           self.now + timedelta(days=730))
            with patch("ksat.lab_builder.signing.inspect_signer", return_value=identity), \
                    patch("ksat.lab_builder.signing.verify_authenticode", return_value=identity), \
                    patch("ksat.lab_builder.signing.subprocess.run", return_value=SimpleNamespace(returncode=0)) as child:
                self.assertEqual(identity, sign_from_store(path, self.selection, tool))
            args = child.call_args.args[0]
            self.assertEqual(THUMBPRINT, args[args.index("/sha1") + 1])
            self.assertEqual("My", args[args.index("/s") + 1])
            self.assertNotIn("/sm", args)
            for forbidden in ("/p", "/f", "/a"):
                self.assertNotIn(forbidden, args)
            self.assertFalse(child.call_args.kwargs["shell"])
            self.assertLessEqual(child.call_args.kwargs["timeout"], 180)

    def test_rejects_wrong_eku_expiry_missing_key_and_identity(self):
        for changes in ({"eku": []}, {"has_private_key": False}, {"publisher": "CN=Other"},
                        {"thumbprint": "0" * 40}, {"not_after": self.now.isoformat()},
                        {"not_before": (self.now + timedelta(days=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_inspection(self.record | changes)
        self.assertEqual(THUMBPRINT, self.run_inspection(self.record).thumbprint)

    def test_untrusted_or_wrong_completed_signature_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.exe"
            path.touch()
            with patch("ksat.lab_builder.signing.inspect_signer"), \
                    patch("ksat.lab_builder.signing.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                    patch("ksat.lab_builder.signing.verify_authenticode", side_effect=ValueError("Untrusted")):
                with self.assertRaises(ValueError):
                    sign_from_store(path, self.selection, path)
            wrong = AuthenticodeIdentity(PUBLISHER, "0" * 40, self.now, self.now)
            with patch("ksat.lab_builder.signing.inspect_signer"), \
                    patch("ksat.lab_builder.signing.subprocess.run", return_value=SimpleNamespace(returncode=0)), \
                    patch("ksat.lab_builder.signing.verify_authenticode", return_value=wrong):
                with self.assertRaises(ValueError):
                    sign_from_store(path, self.selection, path)

    def test_invalid_selection_never_runs_child(self):
        for args in (("Other", THUMBPRINT, PUBLISHER, None, True),
                     ("CurrentUser", "0" * 40, PUBLISHER, None, True),
                     ("CurrentUser", THUMBPRINT, PUBLISHER, "https://timestamp.example.edu", True),
                     ("CurrentUser", THUMBPRINT, PUBLISHER, "http://timestamp.example.edu", False)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SignerSelection(*args)
