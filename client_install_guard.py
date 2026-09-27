"""Narrow elevated installation helper; fixed resources and parent-owned lifetime."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from ksat.client.install_guard import InstallationLease, reject_links
from ksat.lab_builder.profile import decode_profile
from ksat.public_trust import canonical_json, strict_json

TARGET_VERSION = "2.1.1"


def bounded_read(path: Path, limit: int) -> bytes:
    reject_links(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Installer resource is too large.")
    return data


def validate_stage(stage: Path, program_data: Path) -> None:
    reject_links(stage)
    expected = Path(program_data).resolve() / "KSAT Installer Staging"
    if stage.parent.resolve() != expected or not re.fullmatch(r"[0-9a-f]{32}", stage.name):
        raise ValueError("Installer staging location is invalid.")
    require_protected_directory(expected)
    require_protected_directory(stage)


def require_protected_directory(path: Path, *, allow_service: bool = False) -> None:
    """Reject user-owned/user-writable staging before writing any result files."""
    reject_links(path)
    script = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$acl=Get-Acl -LiteralPath $env:KSAT_GUARD_DIRECTORY
$allowed=@('S-1-5-18','S-1-5-32-544')
if($env:KSAT_GUARD_ALLOW_SERVICE -eq '1'){
 $account=[Security.Principal.NTAccount]::new('NT SERVICE\KSATLabClientAuthority')
 $allowed+=@($account.Translate([Security.Principal.SecurityIdentifier]).Value)
}
$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
if($allowed -notcontains $owner){throw 'unprotected owner'}
foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
 if($rule.AccessControlType -eq 'Allow' -and $allowed -notcontains $rule.IdentityReference.Value){throw 'unprotected access'}
}
if(-not $acl.AreAccessRulesProtected){throw 'inherited access'}
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=dict(os.environ, KSAT_GUARD_DIRECTORY=str(path),
                                     KSAT_GUARD_ALLOW_SERVICE="1" if allow_service else "0"), shell=False,
                            capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Installer directory is not restricted to administrators and SYSTEM.")


def _status(stage: Path, state: str, diagnostic: str | None = None):
    descriptor, name = tempfile.mkstemp(prefix=".status-", dir=stage)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json({"state": state, "diagnostic_code": diagnostic}))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, stage / "status.json")
    finally:
        temporary.unlink(missing_ok=True)


def run_guard_session(program_data: Path, stage: Path, parent_alive, *, service=None,
                      timeout: float = 600) -> int:
    profile = decode_profile(bounded_read(stage / "lab-profile.json", 512 * 1024), now=datetime.now(timezone.utc))
    if (bounded_read(stage / "coordinator-ca.pem", 256 * 1024) != profile.trust.ca_pem
            or bounded_read(stage / "coordinator-public.json", 64 * 1024) != profile.trust.metadata_json):
        raise ValueError("Installer resources do not match the signed lab profile.")
    if not parent_alive():
        raise ValueError("The parent installer is no longer running.")
    with InstallationLease(program_data, service=service) as lease:
        check = lease.prepare(profile, TARGET_VERSION)
        if check.state == "blocked":
            _status(stage, "blocked", check.diagnostic_code)
            return 1
        _status(stage, "ready")
        deadline = time.monotonic() + timeout
        while parent_alive() and time.monotonic() < deadline:
            control = stage / "control.json"
            if control.exists():
                command = strict_json(bounded_read(control, 1024), 1024)
                if command == {"action": "commit"}:
                    lease.commit()
                    _status(stage, "committed")
                    return 0
                if command == {"action": "abort"}:
                    break
                raise ValueError("Invalid installation control message.")
            time.sleep(0.1)
    _status(stage, "aborted")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="KSATClientInstallGuard")
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--installer-pid", required=True, type=int)
    args = parser.parse_args(argv)
    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise ValueError("The installation guard requires administrator approval.")
    parent = psutil.Process(args.installer_pid)
    if parent.pid not in {p.pid for p in psutil.Process().parents()}:
        raise ValueError("The installation guard must be started by its parent installer.")
    program_data = Path(os.environ["ProgramData"])
    stage = args.stage.absolute()
    validate_stage(stage, program_data)
    require_protected_directory(program_data / "KSAT Client" / "state", allow_service=True)
    try:
        return run_guard_session(program_data, stage, parent.is_running)
    except Exception:
        _status(stage, "blocked", "guard_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
