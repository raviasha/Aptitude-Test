"""Minimal native Windows Service Control Manager host for the lab client."""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable


_SERVICE_WIN32_OWN_PROCESS = 0x00000010
_SERVICE_STOPPED = 0x00000001
_SERVICE_START_PENDING = 0x00000002
_SERVICE_STOP_PENDING = 0x00000003
_SERVICE_RUNNING = 0x00000004
_SERVICE_ACCEPT_STOP = 0x00000001
_SERVICE_ACCEPT_SHUTDOWN = 0x00000004
_SERVICE_CONTROL_STOP = 0x00000001
_SERVICE_CONTROL_SHUTDOWN = 0x00000005
_NO_ERROR = 0


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("service_type", ctypes.c_uint32),
        ("current_state", ctypes.c_uint32),
        ("controls_accepted", ctypes.c_uint32),
        ("win32_exit_code", ctypes.c_uint32),
        ("service_specific_exit_code", ctypes.c_uint32),
        ("check_point", ctypes.c_uint32),
        ("wait_hint", ctypes.c_uint32),
    ]


def run_windows_service(
    service_name: str,
    target: Callable[[threading.Event], None],
    *,
    advapi32=None,
) -> None:
    """Dispatch ``target`` under SCM control and propagate startup/runtime failures."""
    if os.name != "nt" and advapi32 is None:
        raise RuntimeError("The Windows service dispatcher is available only on Windows.")
    if not isinstance(service_name, str) or not service_name or not callable(target):
        raise ValueError("Windows service configuration is invalid.")
    callback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    handler_type = callback(
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    main_type = callback(None, ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p))

    class _ServiceTableEntry(ctypes.Structure):
        _fields_ = [("service_name", ctypes.c_wchar_p), ("service_main", main_type)]

    api = advapi32 or ctypes.WinDLL("advapi32", use_last_error=True)
    api.RegisterServiceCtrlHandlerExW.argtypes = [
        ctypes.c_wchar_p,
        handler_type,
        ctypes.c_void_p,
    ]
    api.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    api.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ServiceStatus)]
    api.SetServiceStatus.restype = ctypes.c_int
    api.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(_ServiceTableEntry)]
    api.StartServiceCtrlDispatcherW.restype = ctypes.c_int

    stopped = threading.Event()
    status_handle = ctypes.c_void_p()
    errors: list[BaseException] = []

    def publish(state: int, *, exit_code: int = 0, wait_hint: int = 0) -> None:
        accepted = (
            _SERVICE_ACCEPT_STOP | _SERVICE_ACCEPT_SHUTDOWN
            if state == _SERVICE_RUNNING
            else 0
        )
        status = _ServiceStatus(
            _SERVICE_WIN32_OWN_PROCESS,
            state,
            accepted,
            exit_code,
            0,
            0,
            wait_hint,
        )
        if status_handle.value and not api.SetServiceStatus(status_handle, ctypes.byref(status)):
            raise OSError(ctypes.get_last_error(), "SetServiceStatus failed")

    @handler_type
    def handler(control, _event_type, _event_data, _context):
        if control in (_SERVICE_CONTROL_STOP, _SERVICE_CONTROL_SHUTDOWN):
            try:
                publish(_SERVICE_STOP_PENDING, wait_hint=30_000)
            finally:
                stopped.set()
        return _NO_ERROR

    @main_type
    def service_main(_argc, _argv):
        nonlocal status_handle
        try:
            status_handle = ctypes.c_void_p(
                api.RegisterServiceCtrlHandlerExW(service_name, handler, None)
            )
            if not status_handle.value:
                raise OSError(
                    ctypes.get_last_error(), "RegisterServiceCtrlHandlerExW failed"
                )
            publish(_SERVICE_START_PENDING, wait_hint=30_000)
            publish(_SERVICE_RUNNING)
            target(stopped)
            publish(_SERVICE_STOPPED)
        except BaseException as error:
            errors.append(error)
            try:
                publish(_SERVICE_STOPPED, exit_code=1)
            except BaseException:
                pass

    table = (_ServiceTableEntry * 2)()
    table[0] = _ServiceTableEntry(service_name, service_main)
    table[1] = _ServiceTableEntry(None, main_type())
    if not api.StartServiceCtrlDispatcherW(table):
        raise OSError(ctypes.get_last_error(), "StartServiceCtrlDispatcherW failed")
    if errors:
        raise RuntimeError("The KSAT client Windows service failed.") from errors[0]
