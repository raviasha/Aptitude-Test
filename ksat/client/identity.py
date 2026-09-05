"""Stable, machine-protected client device identity."""

from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ksat.crypto import generate_ed25519_keypair


_IDENTITY_FORMAT_VERSION = 1
_RAW_KEY_LENGTH = 32
_PROTECTED_IDENTITY_ERROR = "Protected device identity is invalid."
_ENROLLMENT_ERROR = "Device enrollment is invalid."
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class SecretProtector(Protocol):
    def protect(self, value: bytes) -> bytes:
        """Return machine-protected bytes without displaying user interface."""

    def unprotect(self, value: bytes) -> bytes:
        """Return the plaintext represented by protected bytes."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDpapiProtector:
    """Protect secrets for all accounts on the current Windows computer."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    CRYPTPROTECT_LOCAL_MACHINE = 0x4
    FLAGS = CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE

    def __init__(self, crypt32=None, kernel32=None):
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is unavailable on this platform.")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("Windows DPAPI is unavailable on this platform.")
        self._crypt32 = crypt32 or loader("crypt32", use_last_error=True)
        self._kernel32 = kernel32 or loader("kernel32", use_last_error=True)
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            ctypes.c_wchar_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(ctypes.c_wchar_p),
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
        if not isinstance(value, bytes):
            raise TypeError("Device key material must be bytes.")
        buffer = ctypes.create_string_buffer(value, max(1, len(value)))
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return _DataBlob(len(value), pointer), buffer

    def _call(self, operation, value: bytes, error_message: str, *, unprotect: bool) -> bytes:
        input_blob, input_buffer = self._input_blob(value)
        output_blob = _DataBlob()
        description = ctypes.c_wchar_p()
        description_pointer = (
            ctypes.byref(description)
            if unprotect
            else None
        )
        try:
            succeeded = operation(
                ctypes.byref(input_blob),
                description_pointer,
                None,
                None,
                None,
                self.FLAGS,
                ctypes.byref(output_blob),
            )
            # Keep the input buffer alive until the native call has returned.
            del input_buffer
            if not succeeded:
                raise ValueError(error_message)
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
            if description.value is not None:
                self._kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))

    def protect(self, value: bytes) -> bytes:
        return self._call(
            self._crypt32.CryptProtectData,
            value,
            "Windows device key protection failed.",
            unprotect=False,
        )

    def unprotect(self, value: bytes) -> bytes:
        return self._call(
            self._crypt32.CryptUnprotectData,
            value,
            "Windows device key unprotection failed.",
            unprotect=True,
        )


@dataclass(frozen=True)
class DeviceIdentity:
    private_key_b64: str
    public_key_b64: str
    device_id: str | None = None
    coordinator_public_key_b64: str | None = None


def derive_state_integrity_key(identity: DeviceIdentity) -> bytes:
    """Derive a non-exported-purpose key from the protected device secret."""
    if not isinstance(identity, DeviceIdentity):
        raise ValueError(_PROTECTED_IDENTITY_ERROR)
    private_key = _raw_key(identity.private_key_b64)
    return hmac.new(
        private_key,
        b"KSAT client authenticated state journal v1",
        hashlib.sha256,
    ).digest()


def _default_protector() -> SecretProtector:
    if os.name != "nt":
        raise RuntimeError(
            "Device identity protection requires Windows DPAPI or an injected SecretProtector."
        )
    return WindowsDpapiProtector()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    lock_path = path.parent / f".{path.name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(path):
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
                os.fsync(lock_file.fileno())
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 30
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Timed out waiting for the device identity lock.")
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _strict_json(value: bytes) -> dict[str, object]:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError(_PROTECTED_IDENTITY_ERROR)
            result[key] = item
        return result

    try:
        text = value.decode("utf-8")
        decoded = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError(_PROTECTED_IDENTITY_ERROR) from error
    if not isinstance(decoded, dict):
        raise ValueError(_PROTECTED_IDENTITY_ERROR)
    canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if canonical != text:
        raise ValueError(_PROTECTED_IDENTITY_ERROR)
    return decoded


def _raw_key(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError(_PROTECTED_IDENTITY_ERROR)
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError(_PROTECTED_IDENTITY_ERROR) from error
    if len(raw) != _RAW_KEY_LENGTH or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError(_PROTECTED_IDENTITY_ERROR)
    return raw


def _validate_device_id(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise ValueError(message)
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(message) from error
    if str(parsed) != value:
        raise ValueError(message)
    return value


class DeviceIdentityStore:
    """Load or atomically create the one identity bound to this computer."""

    def __init__(self, data_dir: Path, protector: SecretProtector | None = None):
        self.protector = protector if protector is not None else _default_protector()
        self.data_dir = Path(data_dir)
        self.key_path = self.data_dir / "device-key.bin"

    @staticmethod
    def _payload(identity: DeviceIdentity) -> bytes:
        value = {
            "coordinator_public_key_b64": identity.coordinator_public_key_b64,
            "device_id": identity.device_id,
            "format_version": _IDENTITY_FORMAT_VERSION,
            "private_key_b64": identity.private_key_b64,
            "public_key_b64": identity.public_key_b64,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _decode(self, protected: bytes) -> DeviceIdentity:
        try:
            plaintext = self.protector.unprotect(protected)
        except Exception as error:
            raise ValueError(_PROTECTED_IDENTITY_ERROR) from error
        value = _strict_json(plaintext)
        if set(value) != {
            "coordinator_public_key_b64",
            "device_id",
            "format_version",
            "private_key_b64",
            "public_key_b64",
        } or value["format_version"] != _IDENTITY_FORMAT_VERSION:
            raise ValueError(_PROTECTED_IDENTITY_ERROR)
        private_raw = _raw_key(value["private_key_b64"])
        public_raw = _raw_key(value["public_key_b64"])
        try:
            private = Ed25519PrivateKey.from_private_bytes(private_raw)
            Ed25519PublicKey.from_public_bytes(public_raw)
            derived = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        except ValueError as error:
            raise ValueError(_PROTECTED_IDENTITY_ERROR) from error
        if derived != public_raw:
            raise ValueError(_PROTECTED_IDENTITY_ERROR)
        device_id = value["device_id"]
        coordinator_key = value["coordinator_public_key_b64"]
        if (device_id is None) != (coordinator_key is None):
            raise ValueError(_PROTECTED_IDENTITY_ERROR)
        if device_id is not None:
            _validate_device_id(device_id, _PROTECTED_IDENTITY_ERROR)
            _raw_key(coordinator_key)
        return DeviceIdentity(
            private_key_b64=value["private_key_b64"],
            public_key_b64=value["public_key_b64"],
            device_id=device_id,
            coordinator_public_key_b64=coordinator_key,
        )

    def _load_existing(self) -> DeviceIdentity | None:
        if not os.path.lexists(self.key_path):
            return None
        try:
            protected = self.key_path.read_bytes()
        except OSError as error:
            raise ValueError(_PROTECTED_IDENTITY_ERROR) from error
        return self._decode(protected)

    def _protected(self, identity: DeviceIdentity) -> bytes:
        try:
            protected = self.protector.protect(self._payload(identity))
        except Exception as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("Unable to protect device identity.") from error
        if not isinstance(protected, bytes) or not protected:
            raise ValueError("Unable to protect device identity.")
        return protected

    @staticmethod
    def _write_temporary(path: Path, protected: bytes) -> Path:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return temporary

    @staticmethod
    def _sync_file(path: Path) -> None:
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())

    def _publish_new(self, protected: bytes) -> None:
        temporary = self._write_temporary(self.key_path, protected)
        try:
            os.link(temporary, self.key_path)
            self._sync_file(self.key_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _replace(self, protected: bytes) -> None:
        temporary = self._write_temporary(self.key_path, protected)
        try:
            os.replace(temporary, self.key_path)
            self._sync_file(self.key_path)
        except OSError as error:
            raise OSError("Unable to save protected device identity.") from error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _recover_temporary(self) -> DeviceIdentity | None:
        candidates = sorted(self.data_dir.glob(f".{self.key_path.name}.*.tmp"))
        if not candidates:
            return None
        valid: list[tuple[Path, DeviceIdentity]] = []
        for candidate in candidates:
            try:
                valid.append((candidate, self._decode(candidate.read_bytes())))
            except (OSError, ValueError):
                continue
        if not valid:
            raise ValueError(_PROTECTED_IDENTITY_ERROR)
        candidate, identity = valid[0]
        try:
            os.link(candidate, self.key_path)
            self._sync_file(self.key_path)
        except FileExistsError:
            winner = self._load_existing()
            if winner is None:
                raise ValueError(_PROTECTED_IDENTITY_ERROR)
            return winner
        finally:
            for path in candidates:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return identity

    def load_or_create(self) -> DeviceIdentity:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.key_path):
            existing = self._load_existing()
            if existing is not None:
                return existing
            recovered = self._recover_temporary()
            if recovered is not None:
                return recovered
            private_key_b64, public_key_b64 = generate_ed25519_keypair()
            identity = DeviceIdentity(private_key_b64, public_key_b64)
            try:
                self._publish_new(self._protected(identity))
            except FileExistsError:
                winner = self._load_existing()
                if winner is None:
                    raise ValueError(_PROTECTED_IDENTITY_ERROR)
                return winner
            return identity

    def save_enrollment(
        self, device_id: str, coordinator_public_key_b64: str
    ) -> DeviceIdentity:
        try:
            valid_device_id = _validate_device_id(device_id, _ENROLLMENT_ERROR)
            _raw_key(coordinator_public_key_b64)
        except ValueError as error:
            raise ValueError(_ENROLLMENT_ERROR) from error
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.key_path):
            identity = self._load_existing()
            if identity is None:
                raise ValueError("Protected device identity is missing.")
            if identity.device_id is not None:
                if (
                    identity.device_id == valid_device_id
                    and identity.coordinator_public_key_b64 == coordinator_public_key_b64
                ):
                    return identity
                raise ValueError("Device enrollment conflicts with the stored identity.")
            enrolled = DeviceIdentity(
                private_key_b64=identity.private_key_b64,
                public_key_b64=identity.public_key_b64,
                device_id=valid_device_id,
                coordinator_public_key_b64=coordinator_public_key_b64,
            )
            self._replace(self._protected(enrolled))
            return enrolled
