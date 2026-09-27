"""Read-only replacement preflight and process-owned installation exclusion.

The gate lives in the existing admin/SYSTEM protected state directory. SCM is
the authenticated local control channel: no unauthenticated HTTP stop endpoint.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.store import ClientStore
from ksat.coordinator.process_lock import CoordinatorLockHeld, CoordinatorProcessLock
from ksat.lab_builder.profile import LabProfile, decode_profile, encode_profile


@dataclass(frozen=True)
class InstallCheck:
    state: Literal["fresh", "same_server", "blocked"]
    installed_version: str | None
    diagnostic_code: str | None


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise ValueError("Client version is invalid.")
    return tuple(int(part) for part in value.split("."))


def installed_version(program_data: Path) -> str | None:
    if os.name != "nt":
        return None
    import winreg
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{F08E1406-AD96-445E-9940-5D54AC2181AE}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value = winreg.QueryValueEx(key, "DisplayVersion")[0]
            version_tuple(value)
            return value
    except FileNotFoundError:
        return None


def reject_links(path: Path) -> None:
    for item in (path, *path.parents):
        if os.path.lexists(item):
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Installation paths must not contain links or reparse points.")


def inspect_installation(program_data: Path, profile: LabProfile, target_version: str, *, rollback_authorized=False) -> InstallCheck:
    version = None
    try:
        decode_profile(encode_profile(profile), now=datetime.now(timezone.utc))
        target = version_tuple(target_version)
        root = Path(program_data) / "KSAT Client"
        reject_links(root)
        if not root.exists():
            return InstallCheck("fresh", None, None)
        for path in root.rglob("*"):
            reject_links(path)
        config_path = root / "client-config.json"
        if not config_path.exists():
            # The lease may have created its protected lock but no client data.
            if all(p.relative_to(root).as_posix() in {"state", "state/.maintenance.lock", "state/.client.lock"} for p in root.rglob("*")):
                return InstallCheck("fresh", None, None)
            return InstallCheck("blocked", None, "partial_installation")
        from client_app import ClientConfigStore, ClientRuntimeConfigStore, _validate_production_trust
        config = ClientRuntimeConfigStore(ClientConfigStore(config_path), root / "coordinator-url.json").load()
        _validate_production_trust(config)
        if (config.coordinator_base_url != profile.trust.base_url
                or config.coordinator_signing_public_key_b64 != profile.trust.signing_public_key_b64
                or hashlib.sha256(Path(config.trusted_ca_path).read_bytes()).hexdigest() != profile.trust.ca_sha256):
            return InstallCheck("blocked", None, "different_server")
        version = installed_version(program_data)
        if version is None:
            return InstallCheck("blocked", None, "unknown_installed_version")
        if version_tuple(version) > target and not rollback_authorized:
            return InstallCheck("blocked", version, "downgrade_refused")
        identity = DeviceIdentityStore(root / "identity").load_existing()
        if (identity.device_id is not None
                and identity.coordinator_public_key_b64 != profile.trust.signing_public_key_b64):
            raise ValueError("Existing enrollment does not match the coordinator.")
        reason = ClientStore.inspect_replacement_safety(
            root / "state" / "client.sqlite3", integrity_key=derive_state_integrity_key(identity),
            integrity_anchor_path=root / "identity" / "state-anchor.json")
        return InstallCheck("blocked" if reason else "same_server", version, reason)
    except (ValueError, OSError, RuntimeError, sqlite3.Error):
        return InstallCheck("blocked", version, "unreadable_state")


class MaintenanceBusy(RuntimeError):
    pass


class _MaintenanceLock(CoordinatorProcessLock):
    lock_filename = ".maintenance.lock"
    lock_held_message = "Client maintenance or attempt issuance is already in progress."


class MaintenanceGate:
    """OS byte-range mutex released even when its owning process crashes."""
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "state"
        self.lock = None

    def __enter__(self):
        reject_links(self.path)
        # Production directories are created/ACL-checked by the elevated installer.
        self.lock = _MaintenanceLock(self.path)
        try:
            self.lock.acquire()
        except CoordinatorLockHeld as error:
            raise MaintenanceBusy(str(error)) from error
        return self

    def __exit__(self, *_exc):
        if self.lock is not None:
            self.lock.release()
            self.lock = None


class WindowsClientService:
    """Fixed SCM operations only; credentials/commands are never caller supplied."""
    def _invoke(self, action: str):
        scripts = {
            "query": "$s=Get-Service -Name KSATLabClientAuthority -ErrorAction SilentlyContinue; if($s){[int]$s.Status}else{0}",
            "stop": "$s=Get-Service -Name KSATLabClientAuthority; Stop-Service -InputObject $s -ErrorAction Stop; $s.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))",
            "start": "$s=Get-Service -Name KSATLabClientAuthority; Start-Service -InputObject $s -ErrorAction Stop; $s.WaitForStatus('Running',[TimeSpan]::FromSeconds(30))",
        }
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", scripts[action]],
                                shell=False, capture_output=True, timeout=40,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("The client service could not be controlled safely.")
        return result.stdout.strip()

    def is_running(self):
        status = self._invoke("query")
        if status not in {b"0", b"1", b"4"}:
            raise RuntimeError("Wait for the client service to finish changing state.")
        return status == b"4"

    def stop(self):
        self._invoke("stop")

    def start(self):
        self._invoke("start")


class InstallationLease:
    """Hold exclusion through replacement; restore prior availability on abort.

    The privileged helper owns the lease and monitors the installer PID. Caller
    must prepare a protected state directory before acquiring a fresh-PC lease.
    """
    def __init__(self, program_data: Path, *, service=None):
        self.program_data = Path(program_data)
        self.root = self.program_data / "KSAT Client"
        self.service = service if service is not None else WindowsClientService()
        self.gate = MaintenanceGate(self.root)
        self.lifecycle = None
        self.was_running = False
        self.stopped = False
        self.closed = False
        self.prepared = False

    def __enter__(self):
        self.gate.__enter__()
        return self

    def prepare(self, profile: LabProfile, target_version: str, *, rollback_authorized=False) -> InstallCheck:
        if self.closed or self.gate.lock is None or self.prepared:
            raise RuntimeError("Installation lease is not available.")
        check = inspect_installation(self.program_data, profile, target_version, rollback_authorized=rollback_authorized)
        if check.state == "blocked":
            return check
        self.was_running = self.service.is_running()
        if self.was_running and (check.installed_version is None or version_tuple(check.installed_version) < (2, 1, 1)):
            return InstallCheck("blocked", check.installed_version, "stop_legacy_client")
        try:
            if self.was_running:
                # Record before invoking SCM so timeout also restores availability.
                self.stopped = True
                self.service.stop()
            from client_app import ClientProcessLock
            self.lifecycle = ClientProcessLock(self.root / "state").acquire()
            checked = inspect_installation(self.program_data, profile, target_version, rollback_authorized=rollback_authorized)
            if checked.state == "blocked":
                self.abort()
                return checked
            self.prepared = True
            return checked
        except Exception:
            self.abort()
            raise

    def _release(self):
        if self.lifecycle is not None:
            self.lifecycle.release()
            self.lifecycle = None
        self.gate.__exit__()
        self.closed = True

    def commit(self):
        if not self.prepared or self.closed:
            raise RuntimeError("No prepared installation to commit.")
        self._release()

    def abort(self):
        if self.closed:
            return
        self._release()
        if self.stopped:
            self.service.start()

    def __exit__(self, *_exc):
        self.abort()
