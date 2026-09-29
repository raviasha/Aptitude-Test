import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ksat.lab_builder.build import BuildTools
from ksat.lab_builder.controller import BuilderSettings
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT, SignerSelection


class BuilderSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = patch("pathlib.Path.home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.env = patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root / "local"),
            "ProgramFiles": str(self.root / "programs"),
            "ProgramFiles(x86)": str(self.root / "programs86"),
            "TEMP": str(self.root / "temp"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.which = patch("shutil.which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)

    def tool(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_missing_saved_paths_are_repaired_without_changing_signer(self):
        from ksat.lab_builder.setup import resolve_settings, validate_tools
        compiler = self.tool("local/Programs/Inno Setup 6/ISCC.exe")
        signer = self.tool("local/KSAT Build Tools/SDK/package/bin/10.0.28000.0/x64/signtool.exe")
        extractor = self.tool("temp/KSATBuildTools/innoextract670/innoextract.exe")
        selection = SignerSelection("LocalMachine", THUMBPRINT, PUBLISHER, None, True)
        old = BuilderSettings(BuildTools(Path("missing-iscc"), Path("signtool.exe"), Path("missing-extractor")), selection)
        resolved = resolve_settings(old)
        self.assertEqual(BuildTools(compiler, signer, extractor), resolved.tools)
        self.assertEqual(selection, resolved.signer)
        validate_tools(resolved.tools)

    def test_existing_absolute_overrides_are_preserved(self):
        from ksat.lab_builder.setup import resolve_settings
        tools = BuildTools(*(self.tool("chosen/" + name) for name in ("compiler.exe", "signer.exe", "extractor.exe")))
        selection = SignerSelection("CurrentUser", THUMBPRINT, PUBLISHER, None, True)
        self.assertEqual(BuilderSettings(tools, selection), resolve_settings(BuilderSettings(tools, selection)))

    def test_machine_install_locations_and_numeric_sdk_versions(self):
        from ksat.lab_builder.setup import resolve_settings
        compiler = self.tool("programs86/Inno Setup 6/ISCC.exe")
        self.tool("programs86/Windows Kits/10/bin/10.0.9999.0/x64/signtool.exe")
        signer = self.tool("programs86/Windows Kits/10/bin/10.0.28000.0/x64/signtool.exe")
        extractor = self.tool("local/KSAT Build Tools/innoextract670/innoextract.exe")
        self.assertEqual(BuildTools(compiler, signer, extractor), resolve_settings().tools)

    def test_packaging_tools_outside_app_virtualized_directories(self):
        from ksat.lab_builder.setup import resolve_settings
        compiler = self.tool("local/Programs/Inno Setup 6/ISCC.exe")
        signer = self.tool("KSAT Build Tools/SignTool/x64/signtool.exe")
        extractor = self.tool("KSAT Build Tools/innoextract670/innoextract.exe")
        self.assertEqual(BuildTools(compiler, signer, extractor), resolve_settings().tools)

    def test_missing_tools_message_names_missing_component(self):
        from ksat.lab_builder.setup import validate_tools
        compiler = self.tool("chosen/ISCC.exe")
        extractor = self.tool("chosen/innoextract.exe")
        with self.assertRaisesRegex(ValueError, "Microsoft SignTool"):
            validate_tools(BuildTools(compiler, Path("signtool.exe"), extractor))

    def test_lab_defaults_are_valid_and_output_never_overwrites(self):
        from ksat.lab_builder.setup import default_lab_name, prepare_output_folder
        from types import SimpleNamespace
        self.assertEqual("KSAT desktop-i7nsd5h.local", default_lab_name("https://desktop-i7nsd5h.local:8443"))
        with patch("pathlib.Path.home", return_value=self.root):
            profile = SimpleNamespace(lab_slug="ksat-desktop-i7nsd5h-local")
            first = prepare_output_folder(profile, "")
            (first / "keep.exe").write_bytes(b"existing installer")
            second = prepare_output_folder(profile, "")
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_dir())
        self.assertEqual(b"existing installer", (first / "keep.exe").read_bytes())
        self.assertEqual(self.root / "Downloads/KSAT Lab Installers", first.parent)
        self.assertEqual(self.root, prepare_output_folder(profile, str(self.root)))
        with self.assertRaises(ValueError):
            prepare_output_folder(profile, str(self.root / "not-an-existing-override"))


class BuilderSimpleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        # The application uses one Tcl interpreter. Reuse it across windows,
        # rather than repeatedly loading/unloading Tk around worker threads.
        cls.host = tk.Tk()
        cls.host.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.host.destroy()

    def setUp(self):
        import tkinter as tk
        from ksat.lab_builder.gui import BuilderWindow
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Toplevel(self.host)
        self.root.withdraw()
        self.addCleanup(self.close_window)
        with patch("ksat.lab_builder.gui.BuilderSettings.load", side_effect=FileNotFoundError), \
             patch.object(BuilderWindow, "load_payload"):
            self.window = BuilderWindow(self.root, Path(self.temp.name))
        self.window.settings_path = Path(self.temp.name) / "settings.json"

    def close_window(self):
        if self.window.controller.worker is not None:
            self.window.controller.worker.join(5)
            self.assertFalse(self.window.controller.worker.is_alive())
        for callback in self.root.tk.call("after", "info"):
            # Includes ttk's native progress timers, whose scripts are Tcl
            # lists, not Python command names accepted by after_cancel.
            self.root.tk.call("after", "cancel", callback)
        self.root.destroy()

    def populate_build(self):
        import httpx
        from types import SimpleNamespace
        from tests.test_client_install_guard import ClientInstallGuardTests
        ClientInstallGuardTests.setUpClass()
        self.addCleanup(ClientInstallGuardTests.tearDownClass)
        security = ClientInstallGuardTests.security
        self.window.fields["url"].set("https://lab.example.edu:8443")
        self.window.fields["ca"].set(str(security.ca_certificate_path))
        self.window.fields["metadata"].set(str(security.public_export_dir / "coordinator-public.json"))
        self.window.payload = SimpleNamespace(client_version="2.1.1")
        for name, variable in self.window.tools.items():
            path = Path(self.temp.name) / (name + ".exe")
            path.touch()
            variable.set(str(path))
        self.window.controller.transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"version": "2.1.0"}))

    def pump_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(predicate(), "GUI worker did not reach the expected state")

    def test_only_connection_inputs_are_visible_and_advanced_is_optional(self):
        window = self.window
        self.assertEqual("", window.advanced.winfo_manager())
        self.assertEqual({"url", "ca", "metadata"}, set(window.connection_entries))
        for entry in window.connection_entries.values():
            self.assertEqual("grid", entry.winfo_manager())
        window.toggle_advanced()
        self.assertEqual("grid", window.advanced.winfo_manager())
        window.toggle_advanced()
        self.assertEqual("", window.advanced.winfo_manager())

    def test_three_connection_inputs_are_enough_for_a_profile(self):
        from tests.test_client_install_guard import ClientInstallGuardTests
        ClientInstallGuardTests.setUpClass()
        self.addCleanup(ClientInstallGuardTests.tearDownClass)
        security = ClientInstallGuardTests.security
        self.window.fields["url"].set("https://lab.example.edu:8443")
        self.window.fields["ca"].set(str(security.ca_certificate_path))
        self.window.fields["metadata"].set(str(security.public_export_dir / "coordinator-public.json"))
        profile = self.window.profile()
        self.assertEqual("KSAT lab.example.edu", profile.lab_name)
        self.assertEqual("ksat-lab-example-edu", profile.lab_slug)

    def test_create_auto_checks_connection_and_uses_defaults(self):
        from ksat.lab_builder.build import BuildResult
        self.populate_build()
        requests = []
        def build(request, *, cancel, progress):
            requests.append(request)
            return BuildResult(request.output_dir / "client.exe", request.output_dir / "receipt.json", "a" * 64)
        with patch("ksat.lab_builder.gui.inspect_signer"), \
             patch("ksat.lab_builder.controller.build_lab_installer", side_effect=build), \
             patch("pathlib.Path.home", return_value=Path(self.temp.name)):
            self.window.create()
            self.pump_until(lambda: self.window.controller.result is not None)
        self.assertEqual(1, len(requests))
        self.assertEqual("KSAT lab.example.edu", requests[0].profile.lab_name)
        self.assertEqual(requests[0].profile.sha256, self.window.controller.verified_profile)
        self.assertTrue(requests[0].output_dir.is_dir())
        self.assertTrue(self.window.settings_path.is_file())

    def test_cancel_during_preparation_does_not_start_build(self):
        self.populate_build()
        entered, release = threading.Event(), threading.Event()
        def inspect(_selection):
            entered.set()
            release.wait(5)
        with patch("ksat.lab_builder.gui.inspect_signer", side_effect=inspect), \
             patch("ksat.lab_builder.controller.build_lab_installer") as build, \
             patch("pathlib.Path.home", return_value=Path(self.temp.name)):
            self.window.create()
            self.assertTrue(entered.wait(2))
            self.window.cancel_build()
            release.set()
            self.pump_until(lambda: not self.window.busy)
            self.assertIsNone(self.window.controller.worker)
            build.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "Downloads/KSAT Lab Installers").exists())

    def test_connection_failure_never_starts_build_or_saves_settings(self):
        import httpx
        self.populate_build()
        self.window.controller.transport = httpx.MockTransport(lambda _r: httpx.Response(503))
        with patch("ksat.lab_builder.gui.inspect_signer"), \
             patch("ksat.lab_builder.gui.messagebox.showerror"), \
             patch("pathlib.Path.home", return_value=Path(self.temp.name)):
            self.window.create()
            self.pump_until(lambda: not self.window.busy)
        self.assertIsNone(self.window.controller.worker)
        self.assertIsNone(self.window.controller.result)
        self.assertFalse(self.window.settings_path.exists())
        self.assertFalse((Path(self.temp.name) / "Downloads/KSAT Lab Installers").exists())
