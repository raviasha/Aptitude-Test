import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_client_install_guard as fixtures


class BuilderControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ClientInstallGuardTests.setUpClass()
        cls.profile = fixtures.ClientInstallGuardTests.profile
        cls.security = fixtures.ClientInstallGuardTests.security

    @classmethod
    def tearDownClass(cls): fixtures.ClientInstallGuardTests.tearDownClass()

    def setUp(self):
        from ksat.lab_builder.controller import BuilderController
        self.controller = BuilderController()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def test_all_input_fields_checked_and_edited_url_not_overridden(self):
        from ksat.lab_builder.controller import metadata_url
        metadata = self.security.public_export_dir / "coordinator-public.json"
        self.assertEqual("https://edited:8443", metadata_url(metadata, "https://edited:8443"))
        self.assertEqual("https://lab.example.edu:8443", metadata_url(metadata, ""))
        self.assertEqual(self.profile.trust, self.controller.validate_inputs("Lab One", self.profile.trust.base_url,
            self.security.ca_certificate_path, metadata).trust)
        with self.assertRaises(ValueError):
            self.controller.validate_inputs("", self.profile.trust.base_url, self.security.ca_certificate_path, metadata)
        with self.assertRaises(ValueError):
            self.controller.validate_inputs("Lab", "https://wrong:8443", self.security.ca_certificate_path, metadata)

    def test_offline_build_requires_acknowledgement(self):
        request = SimpleNamespace(profile=self.profile, output_dir=self.output)
        with self.assertRaisesRegex(ValueError, "acknowledge"):
            self.controller.start_build(request, offline_acknowledged=False)

    def test_duplicate_build_rejected_and_cancel_is_cooperative(self):
        entered, release = threading.Event(), threading.Event()
        def build(request, *, cancel, progress):
            entered.set()
            release.wait(5)
            if cancel.is_set(): raise InterruptedError("cancelled")
        request = SimpleNamespace(profile=self.profile, output_dir=self.output)
        with patch("ksat.lab_builder.controller.build_lab_installer", side_effect=build):
            self.controller.start_build(request, offline_acknowledged=True)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(RuntimeError): self.controller.start_build(request, offline_acknowledged=True)
            self.controller.cancel()
            release.set()
            self.controller.worker.join(5)
        self.assertFalse(self.controller.running)
        self.assertIn("cancelled", [event.stage for event in self.controller.drain_events()])

    def test_settings_contain_only_build_configuration(self):
        from ksat.lab_builder.controller import BuilderSettings
        from ksat.lab_builder.build import BuildTools
        from ksat.lab_builder.signing import SignerSelection, THUMBPRINT, PUBLISHER
        settings = BuilderSettings(BuildTools(Path("compiler"), Path("signer"), Path("inspector")),
            SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True))
        path = self.output / "settings.json"
        settings.save(path)
        self.assertEqual({"tools", "signer"}, set(json.loads(path.read_bytes())))
        self.assertEqual(settings, BuilderSettings.load(path))

    def test_error_export_does_not_leak_exception_paths(self):
        request = SimpleNamespace(profile=self.profile, output_dir=self.output)
        with patch("ksat.lab_builder.controller.build_lab_installer", side_effect=OSError("C:/private/password.txt")):
            self.controller.start_build(request, offline_acknowledged=True)
            self.controller.worker.join(5)
        events = self.controller.drain_events()
        self.assertEqual("failed", events[-1].stage)
        self.assertNotIn("password", self.controller.diagnostic_preview())

    def test_connection_never_accepts_redirect_or_wrong_version(self):
        from ksat.lab_builder.controller import BuilderController
        import httpx
        for status, body in ((302, b"{}"), (200, b'{"version":"9.0.0"}')):
            transport = httpx.MockTransport(lambda _r: httpx.Response(status, content=body))
            self.assertFalse(BuilderController(transport=transport).test_connection(self.profile))
