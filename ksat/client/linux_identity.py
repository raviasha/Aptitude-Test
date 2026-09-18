"""Linux device-key wrapping backed by a root-owned, service-readable key.

Confidentiality relies on Linux account permissions, not TPM binding. Do not
clone this key or the client state to another lab computer.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class LinuxKeyProtector:
    PREFIX = b"KSAT-LINUX-1\0"

    def __init__(self, key_path: Path):
        descriptor = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) not in (0o600, 0o640)
                    or metadata.st_nlink != 1):
                raise PermissionError("Device wrapping key must be root-owned with mode 0600 or 0640.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                key = stream.read(33)
        finally:
            os.close(descriptor)
        if len(key) != 32:
            raise ValueError("Invalid Linux device wrapping key.")
        self.cipher = AESGCM(key)

    def protect(self, value: bytes) -> bytes:
        nonce = os.urandom(12)
        return self.PREFIX + nonce + self.cipher.encrypt(nonce, value, self.PREFIX)

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(self.PREFIX):
            raise ValueError("Invalid Linux device identity format.")
        payload = value[len(self.PREFIX):]
        return self.cipher.decrypt(payload[:12], payload[12:], self.PREFIX)
