"""Run with the Ubuntu system Python under xvfb-run (real GTK widgets)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("DISPLAY"), "Requires Linux GTK and a display (or xvfb-run)")
class SetupGuiTests(unittest.TestCase):
    def setUp(self):
        source = Path(os.environ.get("KSAT_GUI_SOURCE", str(ROOT / "installer/linux/setup_gui.py")))
        self.assertTrue(source.is_file(), "The installed graphical setup wizard is missing")
        spec = importlib.util.spec_from_file_location("setup_gui", str(source))
        self.gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gui)
        self.temporary = tempfile.TemporaryDirectory(prefix="ksat-gui-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ca = self.root / "coordinator public CA.pem"
        self.ca.write_text("public CA fixture")
        self.metadata = self.root / "coordinator-public.json"
        self.metadata.write_text(json.dumps({"coordinator_url": "https://faculty.example:8443"}))
        self.window = self.gui.SetupWindow()
        self.addCleanup(self.window.destroy)

    def pump(self, until=lambda: True):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            while self.gui.Gtk.events_pending():
                self.gui.Gtk.main_iteration_do(False)
            if until():
                return
            time.sleep(.01)
        self.fail("GUI operation did not finish")

    def fill_form(self):
        self.window.ca_chooser.set_filename(str(self.ca))
        self.window.metadata_chooser.set_filename(str(self.metadata))
        self.pump()
        self.window.metadata_chooser.emit("file-set")
        self.pump()

    def test_metadata_selection_prefills_url_and_preserves_manual_edit(self):
        self.fill_form()
        self.assertEqual("https://faculty.example:8443", self.window.url_entry.get_text())
        self.window.url_entry.set_text("https://my-server:8443")
        self.window.metadata_chooser.emit("file-set")
        self.assertEqual("https://my-server:8443", self.window.url_entry.get_text())

    def test_missing_files_and_http_url_show_error_without_elevation(self):
        with patch.object(self.gui.subprocess, "run", side_effect=AssertionError("Must not elevate invalid form")):
            self.window.install_button.clicked()
            self.assertIn("HTTPS", self.window.status_label.get_text())
            self.window.url_entry.set_text("https://faculty.example:8443")
            self.window.install_button.clicked()
            self.assertIn("both", self.window.status_label.get_text())
            self.fill_form()
            self.window.url_entry.set_text("http://faculty.example:8443")
            self.window.install_button.clicked()
            self.assertIn("HTTPS", self.window.status_label.get_text())
        self.assertTrue(self.window.install_button.get_sensitive())

    def test_success_requires_admin_helper_and_ready_service_before_open_button(self):
        self.fill_form()
        def helper(command, **kwargs):
            self.assertEqual([
                "/usr/bin/pkexec", "--disable-internal-agent", "/opt/ksat-client/KSATClient",
                "--configure", "--base-url", "https://faculty.example:8443",
                "--ca", str(self.ca), "--metadata", str(self.metadata),
            ], command)
            self.assertFalse(kwargs.get("shell", False))
            return subprocess.CompletedProcess(command, 0, "configured", "")
        with patch.object(self.gui.subprocess, "run", side_effect=helper), patch.object(self.gui, "wait_for_client", return_value=True):
            self.window.install_button.clicked()
            self.pump(lambda: not self.window.busy)
        self.assertIn("ready", self.window.status_label.get_text())
        self.assertTrue(self.window.open_button.get_sensitive())

    def test_cancelled_authorization_can_retry_without_success_claim(self):
        self.fill_form()
        with patch.object(self.gui.subprocess, "run", return_value=subprocess.CompletedProcess([], 126, "", "")):
            self.window.install_button.clicked()
            self.pump(lambda: not self.window.busy)
        self.assertIn("cancel", self.window.status_label.get_text().lower())
        self.assertTrue(self.window.install_button.get_sensitive())
        self.assertFalse(self.window.open_button.get_sensitive())

    def test_validation_failure_is_visible_and_does_not_open_browser(self):
        self.fill_form()
        with patch.object(self.gui.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "KSAT: Coordinator public trust bundle is invalid.")):
            self.window.install_button.clicked()
            self.pump(lambda: not self.window.busy)
        self.assertIn("invalid", self.window.status_label.get_text())
        self.assertFalse(self.window.open_button.get_sensitive())

    def test_service_not_ready_is_not_reported_as_success(self):
        self.fill_form()
        with patch.object(self.gui.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), patch.object(self.gui, "wait_for_client", return_value=False):
            self.window.install_button.clicked()
            self.pump(lambda: not self.window.busy)
        self.assertIn("not ready", self.window.status_label.get_text())
        self.assertFalse(self.window.open_button.get_sensitive())
        self.assertTrue(self.window.retry_button.get_sensitive())

    def test_existing_client_retry_does_not_require_or_replace_trust_files(self):
        def helper(command, **kwargs):
            self.assertEqual(["/usr/bin/pkexec", "--disable-internal-agent", "/opt/ksat-client/KSATClient", "--start-service"], command)
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(self.gui.subprocess, "run", side_effect=helper), patch.object(self.gui, "wait_for_client", return_value=True):
            self.window.retry_button.clicked()
            self.pump(lambda: not self.window.busy)
        self.assertTrue(self.window.open_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
