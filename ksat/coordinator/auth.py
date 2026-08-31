"""Authentication primitives for managed coordinator clients."""

import base64
import binascii
import hashlib
import os
import secrets
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from ksat.protocol import DeviceEnrollmentReceipt, DeviceEnrollmentRequest, device_request_bytes


TOKEN_SALT = "ksat-client-session-v1"
CLIENT_SESSION_SECONDS = 43_200
DEVICE_TIMESTAMP_TOLERANCE_SECONDS = 300


class AuthenticationProblem(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


_NONCE_LOCK = threading.Lock()
_SEEN_NONCES: dict[tuple[str, str], datetime] = {}


def _problem(code: str, message: str, *, status_code: int) -> AuthenticationProblem:
    return AuthenticationProblem(code, message, status_code=status_code)


def _public_key(public_key_b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(public_key_b64.encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise _problem("invalid_device_key", "The device key is invalid.", status_code=400) from error
    if len(raw) != 32:
        raise _problem("invalid_device_key", "The device key is invalid.", status_code=400)
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as error:
        raise _problem("invalid_device_key", "The device key is invalid.", status_code=400) from error


def _load_client_session_secret(secret_path: Path) -> bytes | None:
    try:
        raw_secret = secret_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Coordinator client session secret is invalid.") from error
    if len(raw_secret) != 32:
        raise ValueError("Coordinator client session secret is invalid.")
    return raw_secret


def load_or_create_client_session_secret(secrets_dir: Path) -> str:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secrets_dir / "client-session.key"
    while (raw_secret := _load_client_session_secret(secret_path)) is None:
        candidate = os.urandom(32)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{secret_path.name}.", dir=secrets_dir
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(candidate)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            try:
                os.link(temporary_path, secret_path)
                raw_secret = candidate
            except FileExistsError:
                raw_secret = _load_client_session_secret(secret_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        if raw_secret is not None:
            break
    return base64.urlsafe_b64encode(raw_secret).decode("ascii")


def register_device(
    connection: sqlite3.Connection,
    request: DeviceEnrollmentRequest,
    *,
    expected_enrollment_code: str,
    coordinator_public_key_b64: str,
    now_iso: str,
) -> DeviceEnrollmentReceipt:
    supplied_code = request.enrollment_code if isinstance(request.enrollment_code, str) else ""
    expected_code = expected_enrollment_code if isinstance(expected_enrollment_code, str) else ""
    supplied_digest = hashlib.sha256(supplied_code.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected_code.encode("utf-8")).digest()
    if not secrets.compare_digest(supplied_digest, expected_digest):
        raise _problem("invalid_enrollment_code", "The enrollment code is invalid.", status_code=403)
    label = request.label.strip()
    if not label:
        raise _problem("invalid_device_key", "The device key is invalid.", status_code=400)
    _public_key(request.public_key_b64)
    device_id = str(uuid.uuid4())
    connection.execute(
        """INSERT INTO devices (device_id, label, public_key_b64, status, enrolled_at)
           VALUES (?, ?, ?, 'active', ?)""",
        (device_id, label, request.public_key_b64, now_iso),
    )
    return DeviceEnrollmentReceipt(
        device_id=device_id,
        coordinator_public_key_b64=coordinator_public_key_b64,
    )


def issue_student_access_token(secret: str, student_id: str, device_id: str) -> str:
    return URLSafeTimedSerializer(secret, salt=TOKEN_SALT).dumps(
        {"student_id": student_id, "device_id": device_id}
    )


def verify_student_access_token(
    secret: str, token: str, *, max_age_seconds: int = CLIENT_SESSION_SECONDS
) -> dict[str, str]:
    try:
        claims = URLSafeTimedSerializer(secret, salt=TOKEN_SALT).loads(
            token, max_age=max_age_seconds
        )
    except (BadData, SignatureExpired, TypeError, ValueError) as error:
        raise _problem("invalid_client_session", "The client session is invalid.", status_code=401) from error
    if (
        not isinstance(claims, dict)
        or set(claims) != {"student_id", "device_id"}
        or any(not isinstance(claims[name], str) or not claims[name].strip() for name in claims)
    ):
        raise _problem("invalid_client_session", "The client session is invalid.", status_code=401)
    return {"student_id": claims["student_id"], "device_id": claims["device_id"]}


def verify_device_request(
    connection: sqlite3.Connection,
    *,
    device_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature_b64: str,
    now_utc: datetime | None = None,
) -> str:
    device = connection.execute(
        "SELECT public_key_b64, status FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if device is None or device["status"] != "active":
        raise _problem("device_inactive", "The device is not active.", status_code=403)
    try:
        request_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if request_time.tzinfo is None:
            raise ValueError("Timestamp requires a timezone.")
        request_time = request_time.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as error:
        raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403) from error
    current_time = now_utc or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403)
    current_time = current_time.astimezone(timezone.utc)
    if abs((current_time - request_time).total_seconds()) > DEVICE_TIMESTAMP_TOLERANCE_SECONDS:
        raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403)
    if not isinstance(nonce, str) or not nonce.strip():
        raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403)
    try:
        signature = base64.b64decode(signature_b64.encode("ascii"), validate=True)
        _public_key(device["public_key_b64"]).verify(
            signature, device_request_bytes(method, path, body, timestamp, nonce)
        )
    except (AttributeError, UnicodeEncodeError, binascii.Error, InvalidSignature, ValueError) as error:
        if isinstance(error, AuthenticationProblem):
            raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403) from error
        raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403) from error
    replay_key = (device_id, nonce)
    with _NONCE_LOCK:
        cutoff = current_time - timedelta(seconds=DEVICE_TIMESTAMP_TOLERANCE_SECONDS)
        expired_keys = [
            key for key, accepted_at in _SEEN_NONCES.items() if accepted_at < cutoff
        ]
        for key in expired_keys:
            del _SEEN_NONCES[key]
        if replay_key in _SEEN_NONCES:
            raise _problem("invalid_device_key", "The device request proof is invalid.", status_code=403)
        _SEEN_NONCES[replay_key] = current_time
    return device_id
