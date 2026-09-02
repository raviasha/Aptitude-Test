"""Cross-platform exclusive ownership for one coordinator data directory."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


class CoordinatorLockHeld(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class CoordinatorProcessLock:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve(strict=True)
        self.path = self.data_dir / ".coordinator.lock"
        self.token = secrets.token_hex(16)
        self.held = False

    def acquire(self) -> "CoordinatorProcessLock":
        payload = json.dumps(
            {"pid": os.getpid(), "token": self.token}, separators=(",", ":")
        ).encode("ascii")
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                try:
                    owner = json.loads(self.path.read_text(encoding="ascii"))
                    pid = int(owner["pid"])
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    pid = -1
                if _pid_alive(pid):
                    raise CoordinatorLockHeld(
                        "The coordinator data directory is already in use."
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.held = True
            return self
        raise CoordinatorLockHeld("The coordinator data directory lock is busy.")

    def release(self) -> None:
        if not self.held:
            return
        try:
            owner = json.loads(self.path.read_text(encoding="ascii"))
            if owner.get("token") == self.token and int(owner.get("pid", -1)) == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self.held = False

    def __enter__(self) -> "CoordinatorProcessLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
