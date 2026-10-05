"""GUI-independent builder state; workers never call Tk."""
from __future__ import annotations
import os
import queue
import ssl
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from ksat.client.transport import HostnameFallbackTransport
from ksat.lab_builder import BUILDER_VERSION
from ksat.lab_builder.build import BuildEvent, BuildTools, build_lab_installer
from ksat.lab_builder.profile import make_lab_profile
from ksat.lab_builder.signing import SignerSelection
from ksat.public_trust import canonical_json, strict_json, validate_public_bundle


def read_public(path: Path, limit: int) -> bytes:
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Selected public file is too large.")
    return data


def metadata_url(path: Path, current: str) -> str:
    if current.strip():
        return current
    data = strict_json(read_public(path, 64 * 1024), 64 * 1024)
    url = data.get("coordinator_url") if isinstance(data, dict) else None
    if not isinstance(url, str):
        raise ValueError("Coordinator metadata has no URL.")
    return url


@dataclass(frozen=True)
class BuilderSettings:
    tools: BuildTools
    signer: SignerSelection

    def save(self, path: Path):
        value = {"tools": {name: str(value) for name, value in asdict(self.tools).items()}, "signer": asdict(self.signer)}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_json(value)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path):
        value = strict_json(read_public(path, 16 * 1024), 16 * 1024)
        if set(value) != {"tools", "signer"} or set(value["tools"]) != {"iscc", "signtool", "innoextract"}:
            raise ValueError("Builder settings are invalid.")
        return cls(BuildTools(**{key: Path(item) for key, item in value["tools"].items()}), SignerSelection(**value["signer"]))


class BuilderController:
    def __init__(self, *, transport=None):
        self.transport = transport
        self.worker = None
        self._cancel = threading.Event()
        self._events = queue.SimpleQueue()
        self._lock = threading.Lock()
        self.verified_profile = None
        self.result = None
        self.history = []

    @property
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def validate_inputs(self, lab_name, base_url, ca_path, metadata_path):
        trust = validate_public_bundle(base_url, read_public(ca_path, 256 * 1024),
            read_public(metadata_path, 64 * 1024), now=datetime.now(timezone.utc))
        return make_lab_profile(lab_name, trust)

    def test_connection(self, profile):
        self.verified_profile = None
        try:
            context = ssl.create_default_context(cadata=profile.trust.ca_pem.decode("ascii"))
            transport = self.transport
            if transport is None:
                transport = HostnameFallbackTransport(httpx.URL(profile.trust.base_url).host, verify=context)
            with httpx.Client(verify=context, timeout=5, follow_redirects=False, trust_env=False,
                              transport=transport) as client:
                with client.stream("GET", profile.trust.base_url + "/api/build") as response:
                    if response.status_code != 200:
                        return False
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 4096: return False
            if strict_json(bytes(data), 4096) != {"version": "2.1.0"}:
                return False
            self.verified_profile = profile.sha256
            return True
        except (ValueError, OSError, httpx.HTTPError):
            return False

    def start_build(self, request, *, offline_acknowledged):
        with self._lock:
            if self.running:
                raise RuntimeError("A build is already running.")
            if not request.output_dir.is_dir():
                raise ValueError("Choose an existing output folder.")
            if self.verified_profile != request.profile.sha256 and not offline_acknowledged:
                raise ValueError("Please acknowledge that the connection has not been verified.")
            self._cancel.clear()
            self.result = None
            self.worker = threading.Thread(target=self._run, args=(request,), daemon=False)
            self.worker.start()

    def _run(self, request):
        stage = "validation"
        def progress(event):
            nonlocal stage
            stage = event.stage
            self._events.put(event)
        try:
            self.result = build_lab_installer(request, cancel=self._cancel, progress=progress)
        except InterruptedError:
            self._events.put(BuildEvent("cancelled", "Build cancelled. No installer was published."))
        except FileExistsError:
            self._events.put(BuildEvent("failed", "output_exists: Choose another output folder; existing files were preserved."))
        except Exception:
            hints = {"validation": "Check the public files, HTTPS URL, clock, approved payload and signing setup.",
                "compilation": "Check Inno Setup 6.7.3 and free disk space.",
                "signing": "Check access to the approved certificate key and publisher trust.",
                "verification": "Check the compatible innoextract tool and available disk space."}
            self._events.put(BuildEvent("failed", stage + "_failed: " + hints.get(stage, "Check builder setup and retry.")))

    def cancel(self):
        self._cancel.set()

    def drain_events(self):
        result = []
        while not self._events.empty():
            event = self._events.get()
            result.append(event)
            self.history.append({"stage": event.stage, "message": event.message})
        self.history = self.history[-20:]
        return result

    def diagnostic_preview(self):
        return canonical_json({"builder_version": BUILDER_VERSION, "events": self.history[-20:]}).decode("ascii")
