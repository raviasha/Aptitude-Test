"""Installer-only provisioning of start/query access; no student stop rights."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess

from .launcher import WindowsClientService


def student_start_descriptor(sddl: str) -> str:
    # Use Windows' ACL implementation, not string surgery on security descriptors.
    # In particular, reject a null DACL before CommonSecurityDescriptor can
    # manufacture an Everyone/full-control ACE for it.
    encoded = base64.b64encode(sddl.encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
        "$r=[Security.AccessControl.RawSecurityDescriptor]::new($s);"
        "if($null-eq $r.DiscretionaryAcl){throw 'Null service DACL'};"
        "$d=[Security.AccessControl.CommonSecurityDescriptor]::new($false,$false,$r);"
        "$u=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-545');"
        "$d.DiscretionaryAcl.AddAccess([Security.AccessControl.AccessControlType]::Allow,$u,20,"
        "[Security.AccessControl.InheritanceFlags]::None,[Security.AccessControl.PropagationFlags]::None);"
        "$d.GetSddlForm([Security.AccessControl.AccessControlSections]::All)"
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Windows system directory is unavailable")
    executable = Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                            capture_output=True, text=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not result.stdout.strip():
        raise ValueError("The existing KSAT service permissions could not be safely updated.")
    return result.stdout.strip()


def configure_launcher_access() -> None:
    # Opening a fixed service with READ_CONTROL | WRITE_DAC requires setup's
    # administrator authority. Never grant these rights to the student launcher.
    service = WindowsClientService(access=0x00060000)
    api = service.api
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    api.QueryServiceObjectSecurity.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.QueryServiceObjectSecurity.restype = wintypes.BOOL
    api.SetServiceObjectSecurity.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    api.SetServiceObjectSecurity.restype = wintypes.BOOL
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    text = wintypes.LPWSTR()
    updated = ctypes.c_void_p()
    try:
        needed = wintypes.DWORD()
        api.QueryServiceObjectSecurity(service.handle, 4, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != 122 or not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        original = ctypes.create_string_buffer(needed.value)
        if not api.QueryServiceObjectSecurity(service.handle, 4, original, len(original), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(original, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = student_start_descriptor(text.value)
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(updated), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.SetServiceObjectSecurity(service.handle, 4, updated):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if updated:
            kernel.LocalFree(updated)
        if text:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        service.close()
