"""Non-elevated launcher for the one installed KSAT authority service."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from html.parser import HTMLParser
import time

import httpx

SERVICE_NAME = "KSATLabClientAuthority"
LOCAL_URL = "http://127.0.0.1:8010"


class LauncherError(RuntimeError):
    pass


class WindowsClientService:
    def __init__(self, access: int = 0x0014):
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenSCManagerW.restype = wintypes.HANDLE
        self.api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        self.api.OpenServiceW.restype = wintypes.HANDLE
        self.api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseServiceHandle.restype = wintypes.BOOL
        self.api.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        self.api.StartServiceW.restype = wintypes.BOOL
        self.api.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.api.QueryServiceStatus.restype = wintypes.BOOL
        manager = self.api.OpenSCManagerW(None, None, 1)
        if not manager:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.handle = self.api.OpenServiceW(manager, SERVICE_NAME, access)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseServiceHandle(manager)

    def query(self) -> int:
        status = (wintypes.DWORD * 7)()
        if not self.api.QueryServiceStatus(self.handle, status):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(status[1])

    def start(self) -> None:
        if not self.api.StartServiceW(self.handle, 0, None):
            code = ctypes.get_last_error()
            if code != 1056:  # Another launcher already started the same service.
                raise ctypes.WinError(code)

    def close(self) -> None:
        if self.handle:
            self.api.CloseServiceHandle(self.handle)
            self.handle = None


class _CsrfParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "ksat-csrf":
            self.token = values.get("content")


def reserve_client_launch(client, *, expected_version: str, timeout: float) -> bool:
    """Check the expected listener, then reserve time to open a browser tab."""
    try:
        build = client.get("/api/build", timeout=timeout)
        if build.status_code == 503:
            return False
        if build.status_code != 200 or build.json() != {"version": expected_version}:
            raise LauncherError("The local KSAT version does not match. Run the latest client installer.")
        page = client.get("/", timeout=timeout)
        parser = _CsrfParser()
        parser.feed(page.text)
        if page.status_code != 200 or not parser.token:
            raise LauncherError("The local KSAT page could not be verified. Please repair the client installation.")
        response = client.post("/api/lifecycle/launch", json={}, timeout=timeout,
                               headers={"Origin": LOCAL_URL, "X-KSAT-CSRF": parser.token})
        if response.status_code in (409, 503):
            return False
        if response.status_code != 200 or response.json() != {"state": "ready", "protocol": 1}:
            raise LauncherError("KSAT could not prepare a new window. Please repair the client installation.")
        return True
    except httpx.TransportError:
        return False
    except (ValueError, UnicodeError) as exc:
        raise LauncherError("The local KSAT response is invalid. Please repair the client installation.") from exc


def ensure_client_running(*, timeout_seconds=120.0, service=None, probe=None,
                          clock=time.monotonic, sleep=time.sleep) -> None:
    if timeout_seconds <= 0:
        raise LauncherError("The KSAT startup timeout must be positive.")
    deadline = clock() + timeout_seconds
    owned_service = service is None
    client = None
    try:
        if service is None:
            service = WindowsClientService()
        if probe is None:
            from client_app import _CLIENT_VERSION
            client = httpx.Client(base_url=LOCAL_URL, trust_env=False, follow_redirects=False)
            probe = lambda remaining: reserve_client_launch(
                client, expected_version=_CLIENT_VERSION, timeout=min(2.0, remaining / 3))
        while clock() < deadline:
            state = service.query()
            if state == 1:
                service.start()
            elif state == 4:
                if probe(max(0.001, deadline - clock())):
                    return
            elif state not in (2, 3):
                raise LauncherError("The KSAT service is paused or unavailable. Ask an administrator to repair it.")
            sleep(min(0.25, max(0, deadline - clock())))
        raise LauncherError("KSAT is still starting or finishing a previous session. Wait a moment and reopen KSAT.")
    except OSError as exc:
        raise LauncherError("Windows could not start the KSAT service. Ask an administrator to run the latest client installer; do not delete student data.") from exc
    finally:
        if client is not None:
            client.close()
        if owned_service and service is not None:
            service.close()


def show_launcher_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, "KSAT could not open", 0x10)
