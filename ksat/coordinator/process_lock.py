"""OS-held exclusive ownership for one coordinator data directory."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_PROCESS_START_DIAGNOSTIC = time.time_ns()
_LOCK_RELEASE_GRACE_SECONDS = 0.5
_LOCK_RETRY_INTERVAL_SECONDS = 0.01


class CoordinatorLockHeld(RuntimeError):
    pass


class CoordinatorProcessLock:
    """Hold the first byte of a persistent lock file until release or process death."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve(strict=True)
        self.path = self.data_dir / ".coordinator.lock"
        self.token = secrets.token_hex(16)
        self.held = False
        self._handle: BinaryIO | None = None

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        deadline = time.monotonic() + _LOCK_RELEASE_GRACE_SECONDS
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoordinatorLockHeld(
                        "The coordinator data directory is already in use."
                    ) from error
                time.sleep(min(_LOCK_RETRY_INTERVAL_SECONDS, remaining))

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> "CoordinatorProcessLock":
        if self.held:
            return self
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                os.fsync(handle.fileno())
            self._try_lock(handle)
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "token": self.token,
                    "process_start": _PROCESS_START_DIAGNOSTIC,
                },
                separators=(",", ":"),
            ).encode("ascii")
            handle.seek(0)
            handle.truncate(0)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self.held = True
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            self.held = False
            return
        self._handle = None
        self.held = False
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "CoordinatorProcessLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
