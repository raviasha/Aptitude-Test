"""Sign from an explicitly selected Windows store key; never handle PFX secrets."""
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ksat.public_trust import strict_json
from ksat.windows_authenticode import AuthenticodeIdentity, verify_authenticode

PUBLISHER = "CN=KSAT LAB RELEASE SIGNING"
THUMBPRINT = "13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908"


@dataclass(frozen=True)
class SignerSelection:
    store_location: Literal["CurrentUser", "LocalMachine"]
    thumbprint: str
    publisher: str
    timestamp_url: str | None
    lab_identity: bool

    def __post_init__(self):
        if (self.store_location not in {"CurrentUser", "LocalMachine"}
                or not re.fullmatch(r"[0-9A-F]{40}", self.thumbprint)
                or not self.publisher or type(self.lab_identity) is not bool):
            raise ValueError("Select an authorised Windows signing identity.")
        if self.lab_identity:
            if self.publisher != PUBLISHER or self.thumbprint != THUMBPRINT or self.timestamp_url is not None:
                raise ValueError("The private-lab signing identity does not match the approved pin.")
        else:
            parsed = urlsplit(self.timestamp_url or "")
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment
                    or any(c.isspace() for c in self.timestamp_url)):
                raise ValueError("An HTTPS timestamp service is required.")


_INSPECT_SCRIPT = r"""
$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$path='Cert:\'+$env:KSAT_SIGN_STORE+'\My\'+$env:KSAT_SIGN_THUMBPRINT
$c=Get-Item -LiteralPath $path
[ordered]@{
 publisher=$c.Subject; thumbprint=$c.Thumbprint; has_private_key=$c.HasPrivateKey;
 not_before=$c.NotBefore.ToUniversalTime().ToString('o');
 not_after=$c.NotAfter.ToUniversalTime().ToString('o');
 eku=@($c.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
       ForEach-Object { $_.EnhancedKeyUsages } | ForEach-Object { $_.Value })
} | ConvertTo-Json -Compress
"""


def inspect_signer(selection: SignerSelection) -> AuthenticodeIdentity:
    if sys.platform != "win32":
        raise ValueError("The lab package signer requires Windows.")
    environment = dict(os.environ, KSAT_SIGN_STORE=selection.store_location,
                       KSAT_SIGN_THUMBPRINT=selection.thumbprint)
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _INSPECT_SCRIPT],
                               env=environment, shell=False, capture_output=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode != 0:
        raise ValueError("The authorised signing key is unavailable. Ask IT to provision it on the packaging PC.")
    try:
        raw = completed.stdout.encode() if isinstance(completed.stdout, str) else completed.stdout
        record = strict_json(raw, 16 * 1024)
        identity = AuthenticodeIdentity(record["publisher"], record["thumbprint"],
                                        datetime.fromisoformat(record["not_before"]),
                                        datetime.fromisoformat(record["not_after"]))
        now = datetime.now(timezone.utc)
        minimum_end = now + timedelta(days=365) if selection.lab_identity else now
        if (identity.publisher != selection.publisher or identity.thumbprint != selection.thumbprint
                or record["has_private_key"] is not True or "1.3.6.1.5.5.7.3.3" not in record["eku"]
                or not identity.not_before <= now or identity.not_after < minimum_end):
            raise ValueError
        return identity
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("Signing identity, code-signing usage or certificate validity is unsuitable.") from error


def sign_from_store(path: Path, selection: SignerSelection, signtool: Path) -> AuthenticodeIdentity:
    inspect_signer(selection)
    path, signtool = Path(path).resolve(strict=True), Path(signtool).resolve(strict=True)
    if not path.is_file() or not signtool.is_file():
        raise ValueError("Signing input or Windows SignTool is unavailable.")
    args = [str(signtool), "sign", "/fd", "SHA256", "/s", "My", "/sha1", selection.thumbprint]
    if selection.store_location == "LocalMachine":
        args.append("/sm")
    if selection.timestamp_url:
        args.extend(["/tr", selection.timestamp_url, "/td", "SHA256"])
    args.append(str(path))
    completed = subprocess.run(args, shell=False, capture_output=True, timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode != 0:
        raise ValueError("Signing failed. Check key access and timestamp connectivity.")
    identity = verify_authenticode(path, selection.publisher)
    if identity.thumbprint != selection.thumbprint:
        raise ValueError("The completed signature does not match the authorised key.")
    return identity
