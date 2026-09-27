import hashlib
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_lab_builder_payload import LabPayloadTests
from tests.test_client_install_guard import ClientInstallGuardTests
from ksat.lab_builder.payload import load_approved_payload
from ksat.lab_builder.signing import SignerSelection, PUBLISHER, THUMBPRINT


class LabBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ClientInstallGuardTests.setUpClass()
        cls.profile = ClientInstallGuardTests.profile

    @classmethod
    def tearDownClass(cls):
        ClientInstallGuardTests.tearDownClass()

    def setUp(self):
        from ksat.lab_builder.build import BuildRequest, BuildTools
        self.fixture = LabPayloadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "Unicode ಲ್ಯಾಬ್ & ' lab"
        self.output.mkdir()
        self.request = BuildRequest(self.profile, load_approved_payload(self.fixture.root),
            SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True),
            BuildTools(Path("ISCC.exe"), Path("signtool.exe"), Path("innoextract.exe")), self.output)
        self.cancel = threading.Event()
        self.events = []
        self.commands = []
        self.source = None
        self.tool = patch("ksat.lab_builder.build.run_tool", side_effect=self.run_tool)
        self.tool.start(); self.addCleanup(self.tool.stop)
        self.signer = patch("ksat.lab_builder.build.sign_from_store", return_value=SimpleNamespace(thumbprint=THUMBPRINT))
        self.signer.start(); self.addCleanup(self.signer.stop)
        self.verify = patch("ksat.lab_builder.build.verify_authenticode", return_value=SimpleNamespace(thumbprint=THUMBPRINT))
        self.verify.start(); self.addCleanup(self.verify.stop)

    def run_tool(self, args, **kwargs):
        self.commands.append(args)
        if str(args[0]) == "ISCC.exe":
            self.source = Path(next(a.split("=", 1)[1] for a in args if a.startswith("/DKSAT_PAYLOAD_DIR=")))
            out = Path(next(a.split("=", 1)[1] for a in args if a.startswith("/DKSAT_OUTPUT_DIR=")))
            out.mkdir(exist_ok=True)
            (out / "KSATClientSetup-2.1.1.exe").write_bytes(b"MZ fixture installer")
            return "Compiler engine version: Inno Setup 6.7.3"
        from ksat.lab_builder.build import INSTALLER_FILES
        destination = Path(args[args.index("--output-dir") + 1])
        destination.mkdir()
        for name in INSTALLER_FILES:
            shutil.copyfile(self.source / name, destination / name)
        return "extracted"

    def build(self):
        from ksat.lab_builder.build import build_lab_installer
        return build_lab_installer(self.request, cancel=self.cancel, progress=self.events.append)

    def test_compile_sign_inspect_publish_order(self):
        result = self.build()
        self.assertEqual(["validation", "compilation", "signing", "verification", "complete"], [e.stage for e in self.events])
        self.assertEqual(hashlib.sha256(result.installer.read_bytes()).hexdigest(), result.sha256)
        self.assertTrue(result.receipt.is_file())

    def test_output_collision_never_overwrites(self):
        path = self.output / "KSATClientSetup-lab-one-2.1.1.exe"
        path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError): self.build()
        self.assertEqual(b"keep", path.read_bytes())
        self.assertEqual([], self.commands)

    def test_cancelled_signer_cannot_publish(self):
        with patch("ksat.lab_builder.build.sign_from_store", side_effect=lambda *a: self.cancel.set()):
            with self.assertRaises(InterruptedError): self.build()
        self.assertEqual([], list(self.output.iterdir()))

    def test_unicode_and_shell_paths_are_data(self):
        self.build()
        self.assertTrue(any(str(self.output) in arg for arg in self.commands[0]))
        self.assertIsInstance(self.commands[0], list)

    def test_private_payload_never_shipped(self):
        (self.fixture.root / "key.pfx").write_bytes(b"private")
        with self.assertRaises(ValueError): self.build()
        self.assertEqual([], list(self.output.iterdir()))

    def test_changed_resource_rejected_before_sign(self):
        original = self.run_tool
        def changed(args, **kw):
            value = original(args, **kw)
            if str(args[0]) == "ISCC.exe": (self.source / "lab-profile.json").write_bytes(b"changed")
            return value
        with patch("ksat.lab_builder.build.run_tool", side_effect=changed):
            with self.assertRaises(ValueError): self.build()
        self.assertEqual([], list(self.output.iterdir()))

    def test_receipt_failure_reports_no_success(self):
        with patch("ksat.lab_builder.build.publish_no_replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.build()
        self.assertEqual([], list(self.output.iterdir()))
