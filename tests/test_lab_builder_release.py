import tempfile
import unittest
from pathlib import Path
from tests import test_lab_builder_payload as fixtures
from ksat.lab_builder.payload import load_approved_payload, REQUIRED_FILES


class BuilderReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LabPayloadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "payload"

    def test_assembly_copies_only_approved_public_resources(self):
        from scripts.build_lab_package_builder import assemble_builder_resources
        manifest = assemble_builder_resources(self.fixture.root, self.destination)
        self.assertEqual("payload-manifest.json", manifest.name)
        self.assertEqual(REQUIRED_FILES | {"payload-manifest.json"}, {p.name for p in self.destination.iterdir()})
        self.assertEqual("2.1.3", load_approved_payload(self.destination).client_version)
        self.assertEqual((self.fixture.root / "update-release-public.json").read_bytes(), (self.destination / "update-release-public.json").read_bytes())

    def test_private_input_and_existing_output_are_rejected(self):
        from scripts.build_lab_package_builder import assemble_builder_resources
        self.destination.mkdir()
        (self.destination / "keep").write_bytes(b"keep")
        with self.assertRaises(FileExistsError): assemble_builder_resources(self.fixture.root, self.destination)
        self.assertEqual(b"keep", (self.destination / "keep").read_bytes())
        (self.fixture.root / "pfx-password.txt").write_bytes(b"private")
        with self.assertRaises(ValueError): assemble_builder_resources(self.fixture.root, Path(self.temp.name) / "other")

    def test_windowed_builder_freezes_python_and_tk(self):
        from scripts.build_lab_package_builder import builder_command
        root = Path(__file__).resolve().parents[1]
        args = builder_command(root, Path("python.exe"), Path(self.temp.name), self.destination)
        self.assertIn("--onefile", args)
        self.assertIn("--windowed", args)
        self.assertIn("tkinter", args)
        self.assertIn("KSATLabPackageBuilder-2.1.3", args)
        self.assertEqual(str(root / "lab_package_builder.py"), args[-1])
