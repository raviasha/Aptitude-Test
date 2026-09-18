#!/usr/bin/python3
"""Unprivileged GTK setup/launcher. Compatible with Ubuntu 18.04's Python 3.6.

Only the fixed, installed client helper is elevated through polkit; the GUI,
file chooser and browser always remain in the desktop user's session.
"""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

CLIENT_URL = "http://127.0.0.1:8010/"
HELPER = "/opt/ksat-client/KSATClient"


def client_ready():
    try:
        # Never send loopback requests through a desktop's configured proxy.
        with build_opener(ProxyHandler({})).open(CLIENT_URL, timeout=1) as response:
            return response.status == 200 and b'name="ksat-csrf"' in response.read(65536)
    except (OSError, ValueError):
        return False


def wait_for_client():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if client_ready():
            return True
        time.sleep(.3)
    return False


def open_client():
    subprocess.Popen(["/usr/bin/xdg-open", CLIENT_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SetupWindow(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="KSAT Lab Client Setup")
        self.set_default_size(640, 560)
        self.set_border_width(24)
        self.busy = False
        self.connect("delete-event", lambda *_: self.busy)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(content)
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<big><b>Connect this computer to KSAT</b></big>")
        content.pack_start(heading, False, False, 0)
        intro = Gtk.Label(label="Enter the faculty coordinator address and choose its two public files.\nAn administrator password is required to finish setup.", xalign=0)
        intro.set_line_wrap(True)
        content.pack_start(intro, False, False, 0)
        self.form = Gtk.Grid(column_spacing=16, row_spacing=16)
        content.pack_start(self.form, False, False, 0)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text("https://faculty-server:8443")
        self.url_entry.set_hexpand(True)
        self.ca_chooser = Gtk.FileChooserButton(title="Select coordinator-ca.pem", action=Gtk.FileChooserAction.OPEN)
        self.metadata_chooser = Gtk.FileChooserButton(title="Select coordinator-public.json", action=Gtk.FileChooserAction.OPEN)
        for row, (label, widget) in enumerate((
            ("Server HTTPS address", self.url_entry),
            ("Coordinator CA (.pem)", self.ca_chooser),
            ("Coordinator metadata (.json)", self.metadata_chooser),
        )):
            self.form.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            self.form.attach(widget, 1, row, 1, 1)
        for chooser, pattern in ((self.ca_chooser, "*.pem"), (self.metadata_chooser, "*.json")):
            file_filter = Gtk.FileFilter()
            file_filter.set_name(pattern)
            file_filter.add_pattern(pattern)
            chooser.add_filter(file_filter)
        self.metadata_chooser.connect("file-set", self._metadata_selected)
        note = Gtk.Label(label="Use only coordinator-ca.pem and coordinator-public.json exported by your faculty coordinator. Never select private keys. Existing student data is preserved.", xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(70)
        content.pack_start(note, False, False, 0)
        self.status_label = Gtk.Label(label="Ready for setup. Select metadata to fill the server address automatically.", xalign=0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_max_width_chars(70)
        self.status_label.set_selectable(True)
        content.pack_start(self.status_label, True, True, 0)
        self.spinner = Gtk.Spinner()
        content.pack_start(self.spinner, False, False, 0)
        self.retry_button = Gtk.Button(label="Start existing client")
        self.retry_button.connect("clicked", lambda *_: self._start(["--start-service"]))
        content.pack_start(self.retry_button, False, False, 0)
        buttons = Gtk.Box(spacing=12)
        content.pack_start(buttons, False, False, 0)
        self.close_button = Gtk.Button(label="Close")
        self.close_button.connect("clicked", lambda *_: self.destroy())
        buttons.pack_start(self.close_button, False, False, 0)
        self.open_button = Gtk.Button(label="Open KSAT")
        self.open_button.set_sensitive(False)
        self.open_button.connect("clicked", self._open)
        buttons.pack_end(self.open_button, False, False, 0)
        self.install_button = Gtk.Button(label="Configure client")
        self.install_button.get_style_context().add_class("suggested-action")
        self.install_button.connect("clicked", self._configure)
        buttons.pack_end(self.install_button, False, False, 0)

    def _metadata_selected(self, *_):
        if self.url_entry.get_text().strip():
            return
        try:
            with open(self.metadata_chooser.get_filename(), "rb") as stream:
                metadata = json.loads(stream.read(65537).decode("utf-8"))
            url = metadata.get("coordinator_url", "")
            if isinstance(url, str) and url.startswith("https://"):
                self.url_entry.set_text(url)
        except (OSError, ValueError, TypeError, AttributeError):
            self.status_label.set_text("Cannot read this metadata file. Choose coordinator-public.json exported by the coordinator.")

    def _configure(self, *_):
        url = self.url_entry.get_text().strip()
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path in ("", "/") and (parsed.port is None or parsed.port > 0))
        except ValueError:
            valid = False
        if not valid:
            self.status_label.set_text("Enter a valid HTTPS server address, for example https://faculty-server:8443.")
            return
        ca = self.ca_chooser.get_filename()
        metadata = self.metadata_chooser.get_filename()
        if not ca or not metadata or not Path(ca).is_file() or not Path(metadata).is_file():
            self.status_label.set_text("Select both public files: coordinator-ca.pem and coordinator-public.json.")
            return
        self._start(["--configure", "--base-url", url, "--ca", ca, "--metadata", metadata])

    def _start(self, arguments):
        if self.busy:
            return
        self.busy = True
        for widget in (self.form, self.install_button, self.retry_button, self.close_button, self.open_button):
            widget.set_sensitive(False)
        self.spinner.start()
        self.status_label.set_text("Approve the administrator password prompt. Validating configuration and starting KSAT…")
        threading.Thread(target=self._run_helper, args=(arguments,), daemon=True).start()

    def _run_helper(self, arguments):
        try:
            # No shell and no elevated GTK process; cancellation remains graphical.
            result = subprocess.run(["/usr/bin/pkexec", "--disable-internal-agent", HELPER] + arguments,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            ready = False
            if result.returncode == 126:
                message = "Administrator approval was cancelled. No setup was applied; you can try again."
            elif result.returncode == 127:
                message = "Administrator approval was not available. Sign in to a normal Ubuntu desktop with an administrator account and try again."
            elif result.returncode:
                message = "Setup could not finish. " + (result.stderr.strip()[-1500:] or "Check the server address and both public files, then retry.")
            elif wait_for_client():
                ready = True
                message = "KSAT is ready. Click Open KSAT to continue. The client will start automatically after reboot."
            else:
                message = "Configuration was accepted, but the local client is not ready. Click Start existing client to retry. If this persists, ask your lab administrator."
        except OSError as error:
            ready = False
            message = "Setup could not start: " + str(error)
        GLib.idle_add(self._finished, ready, message)

    def _finished(self, ready, message):
        self.busy = False
        self.spinner.stop()
        for widget in (self.form, self.install_button, self.retry_button, self.close_button):
            widget.set_sensitive(True)
        self.open_button.set_sensitive(ready)
        self.status_label.set_text(message)
        return False

    def _open(self, *_):
        try:
            open_client()
        except OSError:
            self.status_label.set_text("Could not open a browser. Open http://127.0.0.1:8010/ in your browser.")


def main():
    parser = argparse.ArgumentParser(description="KSAT graphical first-run setup")
    parser.add_argument("--launch", action="store_true", help="Open an already running client, otherwise show setup")
    args = parser.parse_args()
    if args.launch and client_ready():
        try:
            open_client()
            return
        except OSError:
            pass
    window = SetupWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
