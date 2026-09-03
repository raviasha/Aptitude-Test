"""Authentication primitives for managed coordinator clients."""

import base64
import binascii
import heapq
import hashlib
import os
import secrets
import sqlite3
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from ksat.protocol import DeviceEnrollmentReceipt, DeviceEnrollmentRequest, device_request_bytes


TOKEN_SALT = "ksat-client-session-v1"
CLIENT_SESSION_SECONDS = 43_200
DEVICE_TIMESTAMP_TOLERANCE_SECONDS = 300
DEVICE_NONCE_GLOBAL_LIMIT = 50_000
DEVICE_NONCE_PER_DEVICE_LIMIT = 2_048
DEVICE_REQUEST_RATE_LIMIT = 512
DEVICE_GLOBAL_REQUEST_RATE_LIMIT = 20_000
DEVICE_REQUEST_RATE_WINDOW_SECONDS = 60


class AuthenticationProblem(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class NonceReplayCache:
    """Bounded replay and authenticated-request rate tracking.

    Expiration uses a min-heap so each accepted nonce is inserted and removed once;
    request processing never scans the live cache.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int,
        global_limit: int,
        per_device_limit: int,
        rate_limit: int,
        rate_window_seconds: int,
        global_rate_limit: int = DEVICE_GLOBAL_REQUEST_RATE_LIMIT,
    ) -> None:
        limits = (
            ttl_seconds,
            global_limit,
            per_device_limit,
            rate_limit,
            rate_window_seconds,
            global_rate_limit,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("Nonce replay-cache limits must be positive integers.")
        if per_device_limit > global_limit:
            raise ValueError("The per-device nonce limit cannot exceed the global limit.")
        self.ttl_seconds = ttl_seconds
        self.global_limit = global_limit
        self.per_device_limit = per_device_limit
        self.rate_limit = rate_limit
        self.rate_window_seconds = rate_window_seconds
        self.global_rate_limit = global_rate_limit
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], float] = {}
        self._expiry_heap: list[tuple[float, int, tuple[str, str]]] = []
        self._device_counts: dict[str, int] = {}
        self._device_rates: dict[str, deque[float]] = {}
        self._global_rate: deque[tuple[float, str]] = deque()
        self._sequence = 0

    def _prune_expired(self, now: float) -> None:
        while self._expiry_heap and self._expiry_heap[0][0] < now:
            expires_at, _sequence, key = heapq.heappop(self._expiry_heap)
            if self._entries.get(key) != expires_at:
                continue
            del self._entries[key]
            device_id = key[0]
            remaining = self._device_counts[device_id] - 1
            if remaining:
                self._device_counts[device_id] = remaining
            else:
                del self._device_counts[device_id]

    def _prune_rate_window(self, cutoff: float) -> None:
        while self._global_rate and self._global_rate[0][0] < cutoff:
            accepted_at, device_id = self._global_rate.popleft()
            device_rate = self._device_rates.get(device_id)
            if device_rate and device_rate[0] == accepted_at:
                device_rate.popleft()
            if device_rate is not None and not device_rate:
                del self._device_rates[device_id]

    def record(self, device_id: str, nonce: str, *, now_utc: datetime) -> None:
        now = now_utc.timestamp()
        key = (device_id, nonce)
        with self._lock:
            self._prune_expired(now)
            cutoff = now - self.rate_window_seconds
            self._prune_rate_window(cutoff)
            device_rate = self._device_rates.setdefault(device_id, deque())
            if key in self._entries:
                if not device_rate:
                    self._device_rates.pop(device_id, None)
                raise _problem(
                    "invalid_device_key",
                    "The device request proof is invalid.",
                    status_code=403,
                )
            if len(device_rate) >= self.rate_limit or len(self._global_rate) >= self.global_rate_limit:
                if not device_rate:
                    self._device_rates.pop(device_id, None)
                raise AuthenticationProblem(
                    "device_request_rate_limited",
                    "The device request rate limit was reached.",
                    status_code=429,
                    retryable=True,
                )
            if (
                self._device_counts.get(device_id, 0) >= self.per_device_limit
                or len(self._entries) >= self.global_limit
            ):
                if not device_rate:
                    self._device_rates.pop(device_id, None)
                raise AuthenticationProblem(
                    "device_request_capacity",
                    "The device request replay capacity was reached.",
                    status_code=429,
                    retryable=True,
                )
            expires_at = now + self.ttl_seconds
            self._sequence += 1
            self._entries[key] = expires_at
            self._device_counts[device_id] = self._device_counts.get(device_id, 0) + 1
            heapq.heappush(self._expiry_heap, (expires_at, self._sequence, key))
            device_rate.append(now)
            self._global_rate.append((now, device_id))

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": len(self._entries),
                "devices": len(self._device_counts),
                "maximum_device_entries": max(self._device_counts.values(), default=0),
                "expiry_records": len(self._expiry_heap),
                "rate_events": len(self._global_rate),
                "rate_devices": len(self._device_rates),
            }


_NONCE_CACHE = NonceReplayCache(
    ttl_seconds=DEVICE_TIMESTAMP_TOLERANCE_SECONDS,
    global_limit=DEVICE_NONCE_GLOBAL_LIMIT,
    per_device_limit=DEVICE_NONCE_PER_DEVICE_LIMIT,
    rate_limit=DEVICE_REQUEST_RATE_LIMIT,
    rate_window_seconds=DEVICE_REQUEST_RATE_WINDOW_SECONDS,
)


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
    for attempt in range(2):
        try:
            raw_secret = secret_path.read_bytes()
            break
        except FileNotFoundError as error:
            if not os.path.lexists(secret_path):
                return None
            if attempt == 1:
                raise ValueError("Coordinator client session secret is invalid.") from error
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
    try:
        parsed_nonce = uuid.UUID(nonce) if type(nonce) is str and len(nonce) == 36 else None
    except (AttributeError, TypeError, ValueError):
        parsed_nonce = None
    if parsed_nonce is None or parsed_nonce.version != 4 or str(parsed_nonce) != nonce:
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
    _NONCE_CACHE.record(device_id, nonce, now_utc=current_time)
    return device_id
