"""Fail-closed Windows Authenticode trust and signer identity checks."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class AuthenticodeIdentity:
    publisher: str
    thumbprint: str
    not_before: datetime
    not_after: datetime


def _normalize_publisher(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Authenticode publisher is invalid.")
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _win_verify_trust(path: Path) -> int:
    if sys.platform != "win32":
        raise ValueError("Authenticode verification requires Windows.")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", ctypes.c_ulong), ("pcwszFilePath", ctypes.c_wchar_p), ("hFile", ctypes.c_void_p), ("pgKnownSubject", ctypes.c_void_p)]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", ctypes.c_ulong), ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", ctypes.c_ulong),
            ("fdwRevocationChecks", ctypes.c_ulong), ("dwUnionChoice", ctypes.c_ulong),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", ctypes.c_ulong),
            ("hWVTStateData", ctypes.c_void_p), ("pwszURLReference", ctypes.c_wchar_p),
            ("dwProvFlags", ctypes.c_ulong), ("dwUIContext", ctypes.c_ulong),
        ]

    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    data = WINTRUST_DATA(
        ctypes.sizeof(WINTRUST_DATA), None, None, 2, 1, 1,
        ctypes.pointer(file_info), 0, None, None, 0x00000010, 0,
    )
    win_verify_trust = ctypes.windll.wintrust.WinVerifyTrust
    win_verify_trust.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(WINTRUST_DATA)]
    win_verify_trust.restype = ctypes.c_long
    return int(win_verify_trust(None, ctypes.byref(action), ctypes.byref(data)))


def _read_identity(path: Path) -> AuthenticodeIdentity:
    if sys.platform != "win32":
        raise ValueError("Authenticode verification requires Windows.")
    script = r"""
$ErrorActionPreference='Stop'
$s=Get-AuthenticodeSignature -LiteralPath $env:KSAT_AUTHENTICODE_FILE
if ($null -eq $s.SignerCertificate) { throw 'missing signer certificate' }
[ordered]@{
 publisher=$s.SignerCertificate.Subject
 thumbprint=$s.SignerCertificate.Thumbprint
 not_before=$s.SignerCertificate.NotBefore.ToUniversalTime().ToString('o')
 not_after=$s.SignerCertificate.NotAfter.ToUniversalTime().ToString('o')
} | ConvertTo-Json -Compress
"""
    environment = dict(os.environ)
    environment["KSAT_AUTHENTICODE_FILE"] = str(path)
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise ValueError("Authenticode signer identity could not be read.")
    try:
        value = json.loads(completed.stdout)
        return AuthenticodeIdentity(
            publisher=value["publisher"],
            thumbprint=value["thumbprint"],
            not_before=datetime.fromisoformat(value["not_before"]),
            not_after=datetime.fromisoformat(value["not_after"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Authenticode signer identity is invalid.") from error


def verify_authenticode(
    path: Path,
    expected_publisher: str,
    *,
    trust_verifier: Callable[[Path], int] = _win_verify_trust,
    identity_reader: Callable[[Path], AuthenticodeIdentity] = _read_identity,
    now: datetime | None = None,
) -> AuthenticodeIdentity:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError("Authenticode target is not a file.")
    if trust_verifier(candidate) != 0:
        raise ValueError("Authenticode signature is not trusted.")
    identity = identity_reader(candidate)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or identity.not_before.tzinfo is None or identity.not_after.tzinfo is None:
        raise ValueError("Authenticode certificate validity is invalid.")
    if not identity.not_before.astimezone(timezone.utc) <= current.astimezone(timezone.utc) <= identity.not_after.astimezone(timezone.utc):
        raise ValueError("Authenticode certificate is not currently valid.")
    if _normalize_publisher(identity.publisher) != _normalize_publisher(expected_publisher):
        raise ValueError("Authenticode publisher mismatch.")
    return identity
