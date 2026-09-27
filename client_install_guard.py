"""Narrow elevated installation helper; fixed resources and parent-owned lifetime."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from ksat.client.install_guard import InstallationLease, reject_links, inspect_installation
from ksat.lab_builder.profile import decode_profile, encode_profile, make_lab_profile
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT
from ksat.public_trust import canonical_json, strict_json, validate_public_bundle

TARGET_VERSION = "2.1.1"


def publisher_certificate(stage: Path) -> bytes:
    data = bounded_read(stage / "publisher.cer", 16 * 1024)
    certificate = x509.load_der_x509_certificate(data)
    now = datetime.now(timezone.utc)
    if (certificate.fingerprint(hashes.SHA1()).hex().upper() != THUMBPRINT
            or certificate.subject.rfc4514_string() != PUBLISHER
            or not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc
            or ExtendedKeyUsageOID.CODE_SIGNING not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value):
        raise ValueError("The installer publisher certificate is not approved.")
    return data


def load_install_profile(stage: Path, program_data: Path):
    """Read only: lab resources, generic first-install inputs, or effective config."""
    profile_path = stage / "lab-profile.json"
    now = datetime.now(timezone.utc)
    if profile_path.exists():
        profile = decode_profile(bounded_read(profile_path, 512 * 1024), now=now)
        if (bounded_read(stage / "coordinator-ca.pem", 256 * 1024) != profile.trust.ca_pem
                or bounded_read(stage / "coordinator-public.json", 64 * 1024) != profile.trust.metadata_json):
            raise ValueError("Installer resources do not match the lab profile.")
        publisher_certificate(stage)
        return profile
    root = program_data / "KSAT Client"
    if (root / "client-config.json").exists():
        from client_app import ClientConfigStore, ClientRuntimeConfigStore
        config = ClientRuntimeConfigStore(ClientConfigStore(root / "client-config.json"), root / "coordinator-url.json").load()
        ca = bounded_read(Path(config.trusted_ca_path), 256 * 1024)
        url = config.coordinator_base_url
        parsed = urlsplit(url)
        key = config.coordinator_signing_public_key_b64
        metadata = canonical_json({"version": "2.1.0", "coordinator_url": url,
            "hostname": parsed.hostname, "port": parsed.port or 443,
            "ca_sha256": hashlib.sha256(ca).hexdigest(), "signing_public_key_b64": key,
            "signing_public_key_sha256": hashlib.sha256(base64.b64decode(key, validate=True)).hexdigest()})
    else:
        url = bounded_read(stage / "coordinator-url.txt", 4096).decode("utf-8-sig").strip()
        ca = bounded_read(stage / "coordinator-ca.pem", 256 * 1024)
        metadata = bounded_read(stage / "coordinator-public.json", 64 * 1024)
    return make_lab_profile("KSAT Lab", validate_public_bundle(url, ca, metadata, now=now))


class WindowsTrustStore:
    def ensure(self, store: str, certificate_der: bytes) -> bool:
        if store not in {"Root", "TrustedPublisher"}:
            raise ValueError("Invalid certificate store.")
        script = r"""
$ErrorActionPreference='Stop'
$c=[Security.Cryptography.X509Certificates.X509Certificate2]::new([Convert]::FromBase64String($env:KSAT_CERT_DER))
$s=[Security.Cryptography.X509Certificates.X509Store]::new($env:KSAT_CERT_STORE,'LocalMachine')
try{
 $s.Open('ReadWrite');$found=@($s.Certificates | Where-Object {$_.Thumbprint -eq $c.Thumbprint}).Count -gt 0
 if(-not $found){$s.Add($c)}
 if(@($s.Certificates | Where-Object {$_.Thumbprint -eq $c.Thumbprint}).Count -eq 0){throw 'certificate import failed'}
 if($found){'existing'}else{'added'}
}finally{$s.Close()}
"""
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=dict(os.environ, KSAT_CERT_STORE=store, KSAT_CERT_DER=base64.b64encode(certificate_der).decode("ascii")),
            shell=False, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or result.stdout.strip() not in {b"existing", b"added"}:
            raise ValueError("Public certificate trust could not be installed.")
        return result.stdout.strip() == b"added"


def install_public_trust(profile, stage: Path, stores=None) -> list[dict]:
    # Parse and pin every input before the first certificate store mutation.
    decode_profile(encode_profile(profile), now=datetime.now(timezone.utc))
    ca_der = x509.load_pem_x509_certificate(profile.trust.ca_pem).public_bytes(serialization.Encoding.DER)
    certificates = [("Root", ca_der)]
    if (stage / "publisher.cer").exists():
        publisher = publisher_certificate(stage)
        certificates.extend([("Root", publisher), ("TrustedPublisher", publisher)])
    stores = stores or WindowsTrustStore()
    added = []
    for name, data in certificates:
        if stores.ensure(name, data):
            added.append({"store": name, "thumbprint": hashlib.sha1(data).hexdigest().upper()})
    return added


def authorized_rollback(program_data: Path, stage: Path, target_version: str) -> bool:
    """No user flag: the protected updater journal binds the exact cached EXE."""
    try:
        context = strict_json(bounded_read(stage / "install-context.json", 8192), 8192)
        updates = program_data / "KSAT Client" / "updates"
        require_protected_directory(updates, allow_service=True)
        journal = strict_json(bounded_read(updates / "update-journal.json", 8192), 8192)
        installer = Path(context["installer"]).absolute()
        reject_links(installer)
        if installer != updates / "last-known-good" / f"KSATClientSetup-{target_version}.exe":
            return False
        return (journal.get("stage") == "rollback_installing"
                and journal.get("rollback_version") == target_version
                and journal.get("rollback_sha256") == hashlib.sha256(installer.read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def configure_installation(program_data: Path, stage: Path, profile, *, fresh: bool, stores=None):
    """Called only while the parent-owned maintenance/lifecycle lease is held."""
    from client_app import install_client_configuration
    from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
    from ksat.client.store import ClientStore
    root = program_data / "KSAT Client"
    if not fresh:
        # Generic profile loader compares effective runtime URL, not the old seed.
        with tempfile.TemporaryDirectory(dir=stage) as empty:
            existing = load_install_profile(Path(empty), program_data)
        if existing.trust != profile.trust:
            raise ValueError("This client belongs to a different coordinator.")
    else:
        if (root / "client-config.json").exists():
            raise ValueError("Client configuration appeared during installation.")
        (stage / "coordinator-ca.pem").write_bytes(profile.trust.ca_pem)
        (stage / "coordinator-public.json").write_bytes(profile.trust.metadata_json)
        install_client_configuration(program_data, base_url=profile.trust.base_url,
            ca_source=stage / "coordinator-ca.pem", metadata_source=stage / "coordinator-public.json")
        for name in ("identity", "state", "packs", "updates"):
            (root / name).mkdir(exist_ok=True)
        identity = DeviceIdentityStore(root / "identity").load_or_create()
        with ClientStore(root / "state/client.sqlite3", integrity_key=derive_state_integrity_key(identity),
                         integrity_anchor_path=root / "identity/state-anchor.json"):
            pass
    added = install_public_trust(profile, stage, stores)
    if added:
        # Never remove shared trust at uninstall: absence of another consumer
        # cannot be established from this product's ownership marker alone.
        audit = root / "trust" / "installer-added-trust.json"
        previous = strict_json(bounded_read(audit, 64 * 1024), 64 * 1024) if audit.exists() else []
        if not isinstance(previous, list):
            raise ValueError("Public trust audit is invalid.")
        audit.write_bytes(canonical_json(previous + added))


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
if(($env:KSAT_GUARD_ALLOW_SERVICE -eq '1') -and (Get-Service KSATLabClientAuthority -ErrorAction SilentlyContinue)){
 $account=[Security.Principal.NTAccount]::new('NT SERVICE\KSATLabClientAuthority')
 $allowed+=@($account.Translate([Security.Principal.SecurityIdentifier]).Value)
}
$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$owners=@('S-1-5-18','S-1-5-32-544',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
if($owners -notcontains $owner){throw 'unprotected owner'}
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
        # Fixed machine-readable values only; no raw paths or student data.
        (stage / "status.ini").write_text(f"[Guard]\nState={state}\nDiagnostic={diagnostic or ''}\n", encoding="ascii")
    finally:
        temporary.unlink(missing_ok=True)


def run_guard_session(program_data: Path, stage: Path, parent_alive, *, service=None,
                      profile=None) -> int:
    profile = profile or load_install_profile(stage, program_data)
    if not parent_alive():
        raise ValueError("The parent installer is no longer running.")
    with InstallationLease(program_data, service=service) as lease:
        rollback = authorized_rollback(program_data, stage, TARGET_VERSION)
        check = lease.prepare(profile, TARGET_VERSION, rollback_authorized=rollback)
        if check.state == "blocked":
            _status(stage, "blocked", check.diagnostic_code)
            return 1
        _status(stage, "ready")
        configured = False
        # A slow/suspended installer may still replace files. Only its explicit
        # terminal command or process exit can release exclusion, never a clock.
        while parent_alive():
            control = stage / "control.json"
            if control.exists():
                command = strict_json(bounded_read(control, 1024), 1024)
                if command == {"action": "configure"}:
                    if not configured:
                        configure_installation(program_data, stage, profile, fresh=check.state == "fresh")
                        configured = True
                        _status(stage, "configured")
                    time.sleep(0.1)
                    continue
                if command == {"action": "commit"}:
                    if not configured:
                        raise ValueError("Installation has not been configured.")
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
    try:
        profile = load_install_profile(stage, program_data)
        check = inspect_installation(program_data, profile, TARGET_VERSION,
            rollback_authorized=authorized_rollback(program_data, stage, TARGET_VERSION))
        if check.state == "blocked":
            _status(stage, "blocked", check.diagnostic_code)
            return 1
        if check.state == "fresh":
            secure_fresh_directory(program_data / "KSAT Client", public_read=True)
            secure_fresh_directory(program_data / "KSAT Client" / "state")
        require_protected_directory(program_data / "KSAT Client" / "state", allow_service=True)
        return run_guard_session(program_data, stage, parent.is_running, profile=profile)
    except Exception:
        _status(stage, "blocked", "guard_failed")
        return 1


def secure_fresh_directory(path: Path, *, public_read=False):
    reject_links(path)
    script = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')
$p=$env:KSAT_NEW_DIRECTORY
[IO.Directory]::CreateDirectory($p)|Out-Null
$acl=[Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
$acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
foreach($sid in @('S-1-5-18','S-1-5-32-544')){
 $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
if($env:KSAT_PUBLIC_READ -eq '1'){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),'ReadAndExecute','None','None','Allow'))}
Set-Acl -LiteralPath $p -AclObject $acl
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, KSAT_NEW_DIRECTORY=str(path), KSAT_PUBLIC_READ="1" if public_read else "0"),
        shell=False, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Client directories could not be protected.")


if __name__ == "__main__":
    raise SystemExit(main())
