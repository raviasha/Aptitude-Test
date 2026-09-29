"""Administrator-facing desktop interface for preconfigured lab installers."""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from ksat.lab_builder.build import BuildRequest, BuildTools
from ksat.lab_builder.controller import BuilderController, BuilderSettings, metadata_url
from ksat.lab_builder.payload import load_approved_payload
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT, SignerSelection, inspect_signer
from ksat.lab_builder import BUILDER_VERSION
from ksat.lab_builder.setup import default_lab_name, prepare_output_folder, resolve_settings, validate_tools


def default_settings():
    return resolve_settings()


class BuilderWindow:
    def __init__(self, root, payload_root):
        self.root = root
        self.payload_root = payload_root
        self.controller = BuilderController()
        self.payload = None
        self.messages = queue.SimpleQueue()
        self.busy = False
        self.closing = False
        self.preparation_cancel = threading.Event()
        self.settings_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KSAT Lab Package Builder/settings.json"
        try: saved = BuilderSettings.load(self.settings_path)
        except (OSError, ValueError, TypeError, KeyError): saved = None
        settings = resolve_settings(saved)
        root.title("KSAT Lab Package Builder")
        root.geometry("960x650")
        root.minsize(640, 480)
        style = ttk.Style(root)
        if "vista" in style.theme_names(): style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"))
        style.configure("Subtitle.TLabel", foreground="#315064")
        canvas = tk.Canvas(root, highlightthickness=0)
        scroll = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        panel = ttk.Frame(canvas, padding=24)
        window = canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        panel.columnconfigure(0, weight=1)
        ttk.Label(panel, text="One lab. One client installer.", style="Title.TLabel").grid(row=0, sticky="w")
        ttk.Label(panel, text=f"KSAT Lab Package Builder {BUILDER_VERSION}", style="Subtitle.TLabel").grid(row=1, sticky="w", pady=(4, 16))
        self.status = tk.StringVar(value="Checking bundled client payload…")
        ttk.Label(panel, textvariable=self.status, wraplength=760).grid(row=2, sticky="ew", pady=(0, 12))
        self.fields = {key: tk.StringVar() for key in ("lab", "url", "ca", "metadata", "output")}
        form = ttk.LabelFrame(panel, text="Connect this lab", padding=12)
        form.grid(row=3, sticky="ew"); form.columnconfigure(1, weight=1)
        self.connection_entries = {}
        for row, (key, label) in enumerate((("url", "Coordinator HTTPS URL"), ("ca", "Coordinator CA (.pem)"), ("metadata", "Coordinator metadata (.json)"))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
            entry = ttk.Entry(form, textvariable=self.fields[key])
            entry.grid(row=row, column=1, sticky="ew")
            self.connection_entries[key] = entry
            if key != "url":
                ttk.Button(form, text="Browse…", command=lambda k=key: self.browse_field(k)).grid(row=row, column=2, padx=(8, 0))
        ttk.Label(panel, text="Your installer will be saved in Downloads → KSAT Lab Installers.\nThe lab name is taken from the server hostname. Change these only if needed in Advanced.", wraplength=760).grid(row=4, sticky="w", pady=12)
        self.advanced_button = ttk.Button(panel, text="Advanced ▸", command=self.toggle_advanced)
        self.advanced_button.grid(row=5, sticky="w", pady=(0, 8))
        self.advanced = ttk.Frame(panel)
        self.advanced.grid(row=6, sticky="ew")
        self.advanced.columnconfigure(0, weight=1)
        options = ttk.LabelFrame(self.advanced, text="Optional lab settings", padding=12)
        options.grid(row=0, sticky="ew", pady=(0, 10)); options.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((("lab", "Lab name (optional)"), ("output", "Output folder (optional)"))):
            ttk.Label(options, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            ttk.Entry(options, textvariable=self.fields[key]).grid(row=row, column=1, sticky="ew")
            if key == "output":
                ttk.Button(options, text="Browse…", command=lambda: self.browse_field("output")).grid(row=row, column=2, padx=(8, 0))
        self.offline = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, variable=self.offline, text="Build offline: I understand that this server connection has not been verified.").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        setup = ttk.LabelFrame(self.advanced, text="Packaging PC — detected automatically", padding=12)
        setup.grid(row=1, sticky="ew", pady=(0, 16)); setup.columnconfigure(1, weight=1)
        self.tools = {}
        for row, (name, label) in enumerate((("iscc", "Inno Setup 6.7.3"), ("signtool", "Microsoft SignTool"), ("innoextract", "Compatible innoextract"))):
            self.tools[name] = tk.StringVar(value=str(getattr(settings.tools, name)))
            ttk.Label(setup, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            ttk.Entry(setup, textvariable=self.tools[name]).grid(row=row, column=1, sticky="ew")
            ttk.Button(setup, text="Browse…", command=lambda n=name: self.browse(self.tools[n])).grid(row=row, column=2, padx=(8, 0))
        self.store = tk.StringVar(value=settings.signer.store_location)
        ttk.Label(setup, text="Signing key store").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Combobox(setup, textvariable=self.store, values=("CurrentUser", "LocalMachine"), state="readonly", width=20).grid(row=3, column=1, sticky="w")
        ttk.Label(setup, text="Approved publisher: KSAT LAB RELEASE SIGNING\nPrivate keys stay on this PC.", wraplength=600).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(setup, text="Check setup", command=self.check_setup).grid(row=5, column=0, sticky="w", pady=6)
        self.advanced.grid_remove()
        ttk.Label(panel, text="On each student PC: run the generated EXE and approve Windows administrator access.\nNo separate URL entry, certificate selection or trust script is needed.", wraplength=760).grid(row=7, sticky="w", pady=14)
        actions = ttk.Frame(panel); actions.grid(row=8, sticky="ew")
        self.test_button = ttk.Button(actions, text="Test connection", command=self.test_connection)
        self.test_button.pack(side="left")
        self.create_button = ttk.Button(actions, text="Create Client Installer", command=self.create, state="disabled")
        self.create_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Cancel build", command=self.cancel_build).pack(side="left")
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.grid(row=9, sticky="ew", pady=12)
        self.result_text = tk.StringVar(value="Distribute only the EXE. Keep its receipt for your records.")
        ttk.Label(panel, textvariable=self.result_text, wraplength=760).grid(row=10, sticky="ew")
        footer = ttk.Frame(panel); footer.grid(row=11, sticky="w", pady=(12, 0))
        ttk.Button(footer, text="Open output folder", command=self.open_output).pack(side="left")
        ttk.Button(footer, text="Preview diagnostics", command=self.diagnostics).pack(side="left", padx=8)
        root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.load_payload, daemon=True).start()
        root.after(100, self.poll)

    def toggle_advanced(self):
        if self.advanced.winfo_manager():
            self.advanced.grid_remove()
            self.advanced_button.configure(text="Advanced ▸")
        else:
            self.advanced.grid()
            self.advanced_button.configure(text="Advanced ▾")

    def resolved_settings(self):
        settings = resolve_settings(BuilderSettings(
            BuildTools(**{key: Path(value.get()) for key, value in self.tools.items()}),
            SignerSelection(self.store.get(), THUMBPRINT, PUBLISHER, None, True)))
        for key, variable in self.tools.items():
            variable.set(str(getattr(settings.tools, key)))
        return settings

    def browse(self, variable):
        selected = filedialog.askopenfilename(parent=self.root)
        if selected: variable.set(selected)

    def browse_field(self, key):
        if key == "output":
            value = filedialog.askdirectory(parent=self.root)
            if value: self.fields[key].set(value)
        else:
            self.browse(self.fields[key])
            if key == "metadata" and self.fields[key].get():
                try: self.fields["url"].set(metadata_url(Path(self.fields[key].get()), self.fields["url"].get()))
                except (ValueError, OSError): messagebox.showerror("Invalid metadata", "Select the coordinator's public metadata JSON file.")

    def load_payload(self):
        try: self.messages.put(("payload", load_approved_payload(self.payload_root)))
        except Exception: self.messages.put(("error", "Bundled payload verification failed. Check publisher trust or download a verified builder again."))

    def check_setup(self):
        if self.busy or self.controller.running: return
        settings = self.resolved_settings()
        self.busy = True
        def check():
            try:
                validate_tools(settings.tools)
                inspect_signer(settings.signer)
                settings.save(self.settings_path)
                self.messages.put(("setup", "Build tools found; approved signing key is accessible. Compiler compatibility is checked during each build."))
            except ValueError as error:
                self.messages.put(("setup", str(error)))
            except Exception:
                self.messages.put(("setup", "Could not check or save this packaging PC's setup. Check Advanced and folder permissions."))
        threading.Thread(target=check, daemon=True).start()

    def profile(self):
        return self.controller.validate_inputs(self.fields["lab"].get().strip() or default_lab_name(self.fields["url"].get()), self.fields["url"].get(),
            Path(self.fields["ca"].get()), Path(self.fields["metadata"].get()))

    def test_connection(self):
        if self.busy or self.controller.running: return
        try: profile = self.profile()
        except (ValueError, OSError):
            messagebox.showerror("Check lab inputs", "Check the lab name, HTTPS URL, both public files and this PC's clock."); return
        self.busy = True
        self.status.set("Testing TLS connection…")
        threading.Thread(target=lambda: self.messages.put(("connection", self.controller.test_connection(profile))), daemon=True).start()

    def create(self):
        if self.busy or self.controller.running or self.payload is None: return
        try:
            profile = self.profile()
            settings = self.resolved_settings()
            validate_tools(settings.tools)
            requested_output = self.fields["output"].get()
            offline = self.offline.get()
            payload = self.payload
            self.busy = True
            self.preparation_cancel.clear()
            self.controller.result = None
            self.status.set("Checking packaging PC and server connection…")
            self.progress.start()
            self.result_text.set("")
            def prepare():
                try:
                    inspect_signer(settings.signer)
                    if self.preparation_cancel.is_set(): raise InterruptedError
                    if not offline and not self.controller.test_connection(profile):
                        raise ValueError("Cannot verify the server connection. Check the server/network, or explicitly choose Build offline in Advanced.")
                    if self.preparation_cancel.is_set(): raise InterruptedError
                    output = prepare_output_folder(profile, requested_output)
                    request = BuildRequest(profile, payload, settings.signer, settings.tools, output)
                    self.messages.put(("prepared", (request, settings, offline)))
                except InterruptedError:
                    self.messages.put(("preparation_cancelled", None))
                except (ValueError, OSError) as error:
                    self.messages.put(("preparation_error", str(error) if isinstance(error, ValueError) else "Cannot prepare the output folder or packaging tools. Check Advanced and folder permissions."))
                except Exception:
                    self.messages.put(("preparation_error", "Cannot check this packaging PC. Check Advanced or ask IT to verify its signing setup."))
            threading.Thread(target=prepare, daemon=True).start()
        except (ValueError, OSError, RuntimeError) as error:
            messagebox.showerror("Cannot create installer", str(error) if isinstance(error, (ValueError, RuntimeError)) else "Check selected files and output-folder permissions.")

    def poll(self):
        while not self.messages.empty():
            kind, value = self.messages.get()
            if kind == "payload":
                self.payload = value
                self.status.set(f"Approved client {value.client_version} verified. Select this lab's public connection files.")
            elif kind == "connection":
                self.busy = False
                self.status.set("TLS connection verified; coordinator version is compatible." if value else "Connection unverified. Check hostname/network/clock, or explicitly acknowledge an offline build.")
            elif kind == "setup":
                self.busy = False
                self.status.set(value)
            elif kind == "prepared":
                self.busy = False
                if self.closing:
                    self.root.destroy(); return
                if self.preparation_cancel.is_set():
                    self.status.set("Build cancelled; no installer created.")
                    continue
                request, settings, offline = value
                try:
                    settings.save(self.settings_path)
                    self.controller.start_build(request, offline_acknowledged=offline)
                except (ValueError, OSError, RuntimeError) as error:
                    self.status.set("Build could not start.")
                    messagebox.showerror("Cannot create installer", str(error))
            elif kind == "preparation_error":
                self.busy = False
                self.status.set(value)
                if not self.closing: messagebox.showerror("Cannot create installer", value)
            elif kind == "preparation_cancelled":
                self.busy = False
                self.status.set("Build cancelled; no installer created.")
            else: self.status.set(value)
        for event in self.controller.drain_events():
            self.status.set(event.message)
        running = self.controller.running
        self.create_button.configure(state="disabled" if running or self.busy or self.payload is None else "normal")
        self.test_button.configure(state="disabled" if running or self.busy else "normal")
        if not running and not self.busy:
            self.progress.stop()
            if self.controller.result:
                result = self.controller.result
                self.result_text.set(f"Created: {result.installer.name}\nFolder: {result.installer.parent}\nSHA-256: {result.sha256}")
            if self.closing: self.root.destroy(); return
        self.root.after(100, self.poll)

    def open_output(self):
        if self.controller.result: os.startfile(self.controller.result.installer.parent)

    def diagnostics(self):
        preview = self.controller.diagnostic_preview()
        if messagebox.askyesno("Diagnostic preview", preview + "\n\nSave this diagnostic report?"):
            path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
            if path: Path(path).write_text(preview, encoding="ascii")

    def close(self):
        if self.controller.running or self.busy:
            self.closing = True
            self.cancel_build()
            self.status.set("Cancelling safely. Waiting for the current tool to finish…")
        else: self.root.destroy()

    def cancel_build(self):
        self.preparation_cancel.set()
        self.controller.cancel()
        if self.busy: self.status.set("Cancelling safely. Waiting for the current check to finish…")


def run_gui(payload_root):
    root = tk.Tk()
    BuilderWindow(root, Path(payload_root))
    root.mainloop()
    return 0
