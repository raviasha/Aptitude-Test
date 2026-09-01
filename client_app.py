"""Loopback-only FastAPI application for the installed KSAT lab client."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ksat.client.coordinator import (
    ContentVerificationError,
    CoordinatorClient,
    CoordinatorProblem,
)
from ksat.client.identity import DeviceIdentityStore
from ksat.client.outbox import OutboxWorker
from ksat.client.runtime import AssessmentRuntime, SystemClock
from ksat.client.store import AttemptSealedError, ClientStore


_ROOT = Path(__file__).resolve().parent
_CLIENT_STATIC = _ROOT / "static" / "client"
_SHARED_STATIC = _ROOT / "static"
_SAFE_HOSTS = frozenset({
    "127.0.0.1", "127.0.0.1:8010", "localhost", "localhost:8010",
    "[::1]", "[::1]:8010",
})
_SAFE_ORIGINS = frozenset({
    "http://127.0.0.1", "http://127.0.0.1:8010",
    "http://localhost", "http://localhost:8010",
    "http://[::1]", "http://[::1]:8010",
})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MAX_REQUEST_BYTES = 64 * 1024
_DIAGNOSTIC = re.compile(r"^KSAT-[A-Z0-9]{10}$")
_PUBLIC_ASSET_FILENAME = re.compile(r"^[0-9a-f]{64}\.(?:png|jpe?g|webp|svg)$")
_SEALED_MESSAGE = "Your answers are safe and will upload automatically."
_INTERVENTION_MESSAGE = (
    "The sealed submission needs Faculty attention. "
    "Your answers remain saved on this computer."
)
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
)
_KNOWN_PUBLIC_MESSAGES = {
    "coordinator_unavailable": "The assessment server is temporarily unavailable. Your saved work is safe.",
    "start_window_closed": "The 10-minute start window has closed. Ask Faculty for help.",
    "content_hash_mismatch": "The assessment download failed verification and will be downloaded again.",
    "device_inactive": "This lab computer is not registered. Ask Faculty or IT for help.",
    "corrupt_local_attempt": "Saved assessment data could not be verified. Do not close the application; ask Faculty for help.",
    "faculty_intervention_required": _INTERVENTION_MESSAGE,
}


class _StrictBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class EnrollmentBody(_StrictBody):
    label: str = Field(min_length=1, max_length=120)
    enrollment_code: str = Field(min_length=1, max_length=256)


class LoginBody(_StrictBody):
    student_id: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class PrefetchBody(_StrictBody):
    release_id: str = Field(min_length=36, max_length=36)


class ConfirmBody(_StrictBody):
    confirmed: bool


class AnswerBody(_StrictBody):
    answer: str | None = Field(default=None, max_length=1)


class ViolationBody(_StrictBody):
    event_type: str = Field(min_length=1, max_length=200)


class CoordinatorConfigurationBody(_StrictBody):
    base_url: str = Field(min_length=9, max_length=2048)
    confirmed: bool


def _normalize_coordinator_base_url(value: str) -> str:
    message = "Client configuration is invalid."
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(message)
    if any(character.isspace() for character in value) or "\\" in value:
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(message)
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(message) from error
    normalized_host = hostname.lower()
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if (
            len(normalized_host) > 253
            or not re.fullmatch(
                r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*",
                normalized_host,
            )
        ):
            raise ValueError(message)
        authority = normalized_host
    else:
        authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if port is not None:
        authority += f":{port}"
    return f"https://{authority}"


@dataclass(frozen=True)
class ClientConfig:
    coordinator_base_url: str
    trusted_ca_path: str
    coordinator_signing_public_key_b64: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "coordinator_base_url",
            _normalize_coordinator_base_url(self.coordinator_base_url),
        )
        try:
            ca_path = Path(self.trusted_ca_path).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("Client configuration is invalid.") from error
        if not ca_path.is_file():
            raise ValueError("Client configuration is invalid.")
        object.__setattr__(self, "trusted_ca_path", str(ca_path))
        try:
            public_key = base64.b64decode(
                self.coordinator_signing_public_key_b64.encode("ascii"), validate=True
            )
        except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise ValueError("Client configuration is invalid.") from error
        if len(public_key) != 32:
            raise ValueError("Client configuration is invalid.")

    def as_dict(self) -> dict[str, str]:
        return {
            "coordinator_base_url": self.coordinator_base_url,
            "trusted_ca_path": self.trusted_ca_path,
            "coordinator_signing_public_key_b64": self.coordinator_signing_public_key_b64,
        }


class ClientConfigStore:
    """Strict machine-wide configuration with atomic, durable replacement."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _decode(raw: bytes) -> ClientConfig:
        if len(raw) > 64 * 1024:
            raise ValueError("Client configuration is invalid.")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("Client configuration is invalid.") from error
        expected = {
            "coordinator_base_url", "trusted_ca_path",
            "coordinator_signing_public_key_b64",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or any(not isinstance(value[key], str) or not value[key] for key in expected)
        ):
            raise ValueError("Client configuration is invalid.")
        config = ClientConfig(**value)
        return config

    def load(self) -> ClientConfig:
        with self._lock:
            try:
                raw = self.path.read_bytes()
            except OSError as error:
                raise ValueError("Client configuration is invalid.") from error
            return self._decode(raw)

    def save(self, config: ClientConfig) -> ClientConfig:
        if not isinstance(config, ClientConfig):
            raise TypeError("Client configuration is invalid.")
        raw = json.dumps(
            config.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                previous = self.path.read_bytes()
            except FileNotFoundError:
                previous = None
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(temporary_name)
            published = False
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                published = True
                with self.path.open("rb+") as stream:
                    os.fsync(stream.fileno())
                reread = self.load()
                if reread != config:
                    raise ValueError("Client configuration is invalid.")
                return reread
            except BaseException:
                if published:
                    self._restore(previous)
                raise
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _restore(self, previous: bytes | None) -> None:
        if previous is None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.rollback.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(previous)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            with self.path.open("rb+") as stream:
                os.fsync(stream.fileno())
        except BaseException as error:
            raise OSError("Unable to restore the previous client configuration.") from error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def update_base_url(self, base_url: str) -> ClientConfig:
        normalized = _normalize_coordinator_base_url(base_url)
        with self._lock:
            current = self.load()
            return self.save(ClientConfig(
                coordinator_base_url=normalized,
                trusted_ca_path=current.trusted_ca_path,
                coordinator_signing_public_key_b64=current.coordinator_signing_public_key_b64,
            ))


@dataclass
class ClientServices:
    identity_store: DeviceIdentityStore
    store: ClientStore
    runtime: AssessmentRuntime | None
    coordinator: CoordinatorClient
    outbox: OutboxWorker
    cache_dir: Path | None = None
    owns_resources: bool = False
    background_prefetch: bool = False
    expected_coordinator_public_key_b64: str | None = None
    config_store: ClientConfigStore | None = None
    coordinator_factory: Any | None = None
    outbox_factory: Any | None = None


def _diagnostic_reference() -> str:
    return "KSAT-" + secrets.token_hex(5).upper()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _strict_uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ClientApiProblem("invalid_request", f"{label} is invalid.", 422) from error
    if str(parsed) != value:
        raise ClientApiProblem("invalid_request", f"{label} is invalid.", 422)
    return value


class ClientApiProblem(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        retryable: bool = False,
        diagnostic_reference: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.diagnostic_reference = diagnostic_reference


class _ClientContext:
    def __init__(self, services: ClientServices | None):
        self.services = services
        self.injected = services is not None
        self.csrf_token = secrets.token_urlsafe(32)
        self.catalog: dict[str, Any] = {}
        self.ready_releases: set[str] = set()
        self.startup_problem: dict[str, Any] | None = None
        self.identity = None
        self._prefetch_thread: threading.Thread | None = None
        self._prefetch_stop = threading.Event()
        self._prefetch_lock = threading.Lock()
        self._outbox_stop_called = False
        self._owned_resources_closed = False

    def initialize(self) -> None:
        if self.services is None:
            try:
                self.services = _load_production_services()
            except Exception:
                self.startup_problem = self.problem(
                    "client_configuration_invalid", status=500
                )
                return
        try:
            self.identity = self.services.identity_store.load_or_create()
            self._validate_expected_key()
            self._ensure_runtime()
            self.recover()
            if self.startup_problem is not None:
                return
            self.services.outbox.start()
            self._outbox_stop_called = False
            self.services.outbox.wake()
            if (
                self.services.background_prefetch
                and self.identity.device_id is not None
                and self.services.runtime is not None
            ):
                self._start_prefetch_thread()
        except Exception:
            if self.startup_problem is None:
                self.startup_problem = self.problem("client_startup_failed", status=500)
            self._stop_started_services()

    def shutdown(self) -> None:
        self._prefetch_stop.set()
        thread = self._prefetch_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)
        if self.services is None:
            return
        self._stop_outbox()
        self._close_owned_resources()

    def _stop_started_services(self) -> None:
        if self.services is None:
            return
        self._stop_outbox()
        self._close_owned_resources()

    def _stop_outbox(self) -> None:
        if self.services is None or self._outbox_stop_called:
            return
        self._outbox_stop_called = True
        try:
            self.services.outbox.stop()
        except Exception:
            pass

    def _close_owned_resources(self) -> None:
        if (
            self.services is None
            or not self.services.owns_resources
            or self._owned_resources_closed
        ):
            return
        self._owned_resources_closed = True
        try:
            self.services.coordinator.close()
        finally:
            self.services.store.close()

    def _validate_expected_key(self) -> None:
        expected = self.services.expected_coordinator_public_key_b64
        actual = getattr(self.identity, "coordinator_public_key_b64", None)
        if expected is not None and actual is not None and expected != actual:
            raise ValueError("Protected device enrollment does not match client configuration.")

    def _ensure_runtime(self) -> None:
        if self.services.runtime is None and getattr(self.identity, "device_id", None):
            self.services.runtime = AssessmentRuntime(
                self.services.store, self.identity, SystemClock()
            )

    def recover(self) -> Any:
        if self.services is None or self.services.runtime is None:
            return None
        try:
            return self.services.runtime.recover()
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            if self.startup_problem is None:
                self.startup_problem = self.problem("corrupt_local_attempt", status=409)
            return None

    def problem(self, code: str, *, status: int, retryable: bool = False) -> dict[str, Any]:
        message = _KNOWN_PUBLIC_MESSAGES.get(code, "The requested action could not be completed.")
        return {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "diagnostic_reference": _diagnostic_reference(),
            "status": status,
        }

    def prefetch(self, release_id: str | None = None) -> list[str]:
        if self.services is None or self.services.runtime is None:
            raise ClientApiProblem("device_inactive", _KNOWN_PUBLIC_MESSAGES["device_inactive"], 403)
        with self._prefetch_lock:
            entries = self.services.coordinator.prefetch_catalog()
            self.catalog = {entry.release_id: entry for entry in entries}
            self.ready_releases.intersection_update(self.catalog)
            targets = entries if release_id is None else [
                entry for entry in entries if entry.release_id == release_id
            ]
            if release_id is not None and not targets:
                raise ClientApiProblem("release_not_found", "Assessment release was not found.", 404)
            prepared: list[str] = []
            cache_dir = Path(self.services.cache_dir or Path.cwd() / "client-packs")
            cache_dir.mkdir(parents=True, exist_ok=True)
            for entry in targets:
                self.ready_releases.discard(entry.release_id)
                destination = cache_dir / entry.filename
                downloaded = self.services.coordinator.download_pack(entry, destination)
                try:
                    self.services.runtime.prepare(entry.descriptor, downloaded)
                except ValueError as error:
                    raise ClientApiProblem(
                        "content_hash_mismatch",
                        _KNOWN_PUBLIC_MESSAGES["content_hash_mismatch"],
                        409,
                    ) from error
                self.ready_releases.add(entry.release_id)
                prepared.append(entry.release_id)
            return prepared

    def _start_prefetch_thread(self) -> None:
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return

        def run() -> None:
            if self._prefetch_stop.is_set():
                return
            try:
                self.prefetch()
            except (CoordinatorProblem, ClientApiProblem, ValueError, OSError):
                return

        self._prefetch_stop.clear()
        self._prefetch_thread = threading.Thread(
            target=run, name="ksat-content-prefetch", daemon=True
        )
        self._prefetch_thread.start()


def _load_config(path: Path) -> dict[str, str]:
    return ClientConfigStore(path).load().as_dict()


def _load_production_services() -> ClientServices:
    program_data = os.environ.get("ProgramData")
    if not program_data:
        raise ValueError("ProgramData is unavailable.")
    data_dir = Path(program_data) / "KSAT Client"
    config_store = ClientConfigStore(data_dir / "client-config.json")
    config = config_store.load()
    identity_store = DeviceIdentityStore(data_dir)
    identity = identity_store.load_or_create()
    store = ClientStore(data_dir / "client.sqlite3")
    coordinator = None
    try:
        coordinator = CoordinatorClient(
            config.coordinator_base_url, config.trusted_ca_path, identity_store
        )
        runtime = None
        if identity.device_id is not None:
            if identity.coordinator_public_key_b64 != config.coordinator_signing_public_key_b64:
                raise ValueError("Protected device enrollment does not match client configuration.")
            runtime = AssessmentRuntime(store, identity, SystemClock())
        outbox = OutboxWorker(store, coordinator, SystemClock())
    except BaseException:
        try:
            if coordinator is not None:
                coordinator.close()
        finally:
            store.close()
        raise
    def coordinator_factory(base_url: str) -> CoordinatorClient:
        return CoordinatorClient(base_url, config.trusted_ca_path, identity_store)

    def outbox_factory(candidate: CoordinatorClient) -> OutboxWorker:
        return OutboxWorker(store, candidate, SystemClock())

    return ClientServices(
        identity_store=identity_store,
        store=store,
        runtime=runtime,
        coordinator=coordinator,
        outbox=outbox,
        cache_dir=data_dir / "packs",
        owns_resources=True,
        background_prefetch=True,
        expected_coordinator_public_key_b64=config.coordinator_signing_public_key_b64,
        config_store=config_store,
        coordinator_factory=coordinator_factory,
        outbox_factory=outbox_factory,
    )


def _public_problem(problem: dict[str, Any]) -> dict[str, Any]:
    return {key: problem[key] for key in (
        "code", "message", "retryable", "diagnostic_reference"
    )}


def _queue_for(context: _ClientContext, attempt_id: str) -> Any | None:
    services = context.services
    if services is None:
        return None
    pending = services.store.pending_submissions()
    return next((item for item in pending if item.attempt_id == attempt_id), None)


def _queue_payload(item: Any | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "retry_count": item.retry_count,
        "next_attempt_at": _jsonable(item.next_attempt_at),
        "last_error": (
            None if item.last_error is None
            else "The last upload attempt did not complete."
        ),
        "status": item.status,
    }


def _question_payload(question: Any) -> dict[str, Any]:
    stimulus = _jsonable(question.stimulus) if question.stimulus is not None else None
    display_media = _jsonable(question.display_media)
    return {
        "question_id": question.question_id,
        "category": question.category,
        "chapter": question.chapter,
        "difficulty": question.difficulty,
        "question_text": question.question_text,
        "options": dict(question.options),
        "stimulus": stimulus,
        "display_media": display_media,
    }


def _attempt_payload(context: _ClientContext, snapshot: Any, *, include_questions: bool) -> dict[str, Any]:
    state = snapshot.state
    queue = None
    message = None
    if state == "sealed_pending":
        queue_item = _queue_for(context, snapshot.attempt_id)
        queue = _queue_payload(queue_item)
        if queue_item is not None and queue_item.status == "faculty_intervention_required":
            state = "faculty_intervention_required"
            message = _INTERVENTION_MESSAGE
        else:
            message = _SEALED_MESSAGE
    elif state == "acknowledged":
        state = "acknowledged_result"
    payload = {
        "attempt_id": snapshot.attempt_id,
        "state": state,
        "question_order": list(snapshot.question_order),
        "responses": {str(key): value for key, value in snapshot.responses.items()},
        "remaining_seconds": snapshot.remaining_seconds,
        "violations": snapshot.violations,
        "queue": queue,
        "message": message,
    }
    if include_questions and context.services is not None and context.services.runtime is not None:
        payload["questions"] = [
            _question_payload(context.services.runtime.question(question_id))
            for question_id in snapshot.question_order
        ]
    if state == "acknowledged_result" and context.services is not None:
        record = context.services.store.load_attempt(snapshot.attempt_id)
        if record.receipt is None:
            raise ValueError("Acknowledged local attempt is missing its receipt.")
        payload["result"] = _jsonable(record.receipt)
    return payload


def _map_coordinator_problem(error: CoordinatorProblem) -> ClientApiProblem:
    if isinstance(error, ContentVerificationError) or error.code == "content_verification_failed":
        return ClientApiProblem(
            "content_hash_mismatch",
            _KNOWN_PUBLIC_MESSAGES["content_hash_mismatch"],
            409,
            diagnostic_reference=_diagnostic_reference(),
        )
    status = error.status_code or (503 if error.retryable else 409)
    message = _KNOWN_PUBLIC_MESSAGES.get(error.code, "The requested action could not be completed.")
    return ClientApiProblem(
        error.code, message, status, retryable=error.retryable,
        diagnostic_reference=_diagnostic_reference(),
    )


def create_client_app(services: ClientServices | None = None) -> FastAPI:
    context = _ClientContext(services)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        context.initialize()
        try:
            yield
        finally:
            context.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.client_context = context

    @app.middleware("http")
    async def loopback_boundary(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in _SAFE_HOSTS:
            response = JSONResponse(
                {"problem": {
                    "code": "invalid_loopback_host",
                    "message": "The local client address is invalid.",
                    "retryable": False,
                    "diagnostic_reference": _diagnostic_reference(),
                }},
                status_code=400,
            )
        elif request.method in _MUTATING_METHODS and (
            request.headers.get("origin", "").lower() not in _SAFE_ORIGINS
            or not secrets.compare_digest(
                request.headers.get("x-ksat-csrf", ""), context.csrf_token
            )
        ):
            response = JSONResponse(
                {"problem": {
                    "code": "cross_origin_request_rejected",
                    "message": "The local request could not be verified.",
                    "retryable": False,
                    "diagnostic_reference": _diagnostic_reference(),
                }},
                status_code=403,
            )
        else:
            if request.method in _MUTATING_METHODS:
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    response = JSONResponse(
                        {"problem": {
                            "code": "invalid_request",
                            "message": "A JSON request body is required.",
                            "retryable": False,
                            "diagnostic_reference": _diagnostic_reference(),
                        }}, status_code=415,
                    )
                else:
                    body = await request.body()
                    if len(body) > _MAX_REQUEST_BYTES:
                        response = JSONResponse(
                            {"problem": {
                                "code": "request_too_large",
                                "message": "The local request is too large.",
                                "retryable": False,
                                "diagnostic_reference": _diagnostic_reference(),
                            }}, status_code=413,
                        )
                    else:
                        response = await call_next(request)
            else:
                response = await call_next(request)
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ClientApiProblem)
    async def client_problem_handler(_request: Request, error: ClientApiProblem):
        reference = error.diagnostic_reference or _diagnostic_reference()
        return JSONResponse({"problem": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "diagnostic_reference": reference,
        }}, status_code=error.status_code)

    @app.exception_handler(CoordinatorProblem)
    async def coordinator_problem_handler(_request: Request, error: CoordinatorProblem):
        mapped = _map_coordinator_problem(error)
        return await client_problem_handler(_request, mapped)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, error: RequestValidationError):
        malformed = any(item.get("type") == "json_invalid" for item in error.errors())
        return JSONResponse({"problem": {
            "code": "invalid_json" if malformed else "invalid_request",
            "message": "The JSON request is invalid." if malformed else "The request fields are invalid.",
            "retryable": False,
            "diagnostic_reference": _diagnostic_reference(),
        }}, status_code=400 if malformed else 422)

    @app.exception_handler(Exception)
    async def opaque_error_handler(_request: Request, _error: Exception):
        return JSONResponse({"problem": {
            "code": "local_action_failed",
            "message": "The requested action could not be completed.",
            "retryable": False,
            "diagnostic_reference": _diagnostic_reference(),
        }}, status_code=500)

    def require_services() -> ClientServices:
        if context.services is None:
            raise ClientApiProblem("client_configuration_invalid", "The requested action could not be completed.", 500)
        return context.services

    def current_snapshot() -> Any | None:
        if context.startup_problem is not None:
            return None
        current = require_services()
        if current.runtime is None:
            return None
        try:
            return current.runtime.snapshot()
        except RuntimeError:
            return None
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            if context.startup_problem is None:
                context.startup_problem = context.problem("corrupt_local_attempt", status=409)
            return None

    def require_attempt(attempt_id: str, *, editable: bool = False) -> Any:
        _strict_uuid(attempt_id, "Attempt identifier")
        snapshot = current_snapshot()
        if snapshot is None or snapshot.attempt_id != attempt_id:
            raise ClientApiProblem(
                "attempt_mismatch",
                "The requested attempt is not the active local attempt.",
                409,
            )
        if editable and snapshot.state != "in_progress":
            raise ClientApiProblem("attempt_sealed", _SEALED_MESSAGE, 409)
        return snapshot

    @app.get("/")
    async def index():
        source = (_CLIENT_STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(source.replace("__KSAT_CSRF_TOKEN__", context.csrf_token))

    @app.get("/client/app.js")
    async def client_script():
        return FileResponse(_CLIENT_STATIC / "app.js", media_type="text/javascript")

    @app.get("/client/styles.css")
    async def client_styles():
        return FileResponse(_CLIENT_STATIC / "styles.css", media_type="text/css")

    @app.get("/branding.css")
    async def branding_styles():
        return FileResponse(_SHARED_STATIC / "branding.css", media_type="text/css")

    @app.get("/math.css")
    async def math_styles():
        return FileResponse(_SHARED_STATIC / "math.css", media_type="text/css")

    @app.get("/branding/{filename}")
    async def branding_asset(filename: str):
        if filename not in {
            "aiml-logo.png", "campus-login.jpg", "ksat-logo.png",
            "ksit-logo.png", "silver-jubilee-logo.png",
        }:
            raise ClientApiProblem("asset_not_found", "The requested asset was not found.", 404)
        return FileResponse(_SHARED_STATIC / "branding" / filename)

    @app.get("/api/state")
    async def state():
        if context.startup_problem is not None:
            return {
                "state": "faculty_intervention_required",
                "enrolled": bool(getattr(context.identity, "device_id", None)),
                "student": None,
                "attempt": None,
                "problem": _public_problem(context.startup_problem),
            }
        current = require_services()
        snapshot = current_snapshot()
        if snapshot is not None:
            attempt = _attempt_payload(context, snapshot, include_questions=False)
            return {
                "state": attempt["state"],
                "enrolled": True,
                "student": _student_payload(current.coordinator.session),
                "attempt": attempt,
                "problem": None,
            }
        enrolled = bool(getattr(context.identity, "device_id", None))
        session = current.coordinator.session
        return {
            "state": "waiting_or_ready" if session else ("login" if enrolled else "device_setup"),
            "enrolled": enrolled,
            "student": _student_payload(session),
            "attempt": None,
            "problem": None,
        }

    @app.get("/api/attempts/{attempt_id}/assets/{filename}")
    async def public_attempt_asset(attempt_id: str, filename: str):
        require_attempt(attempt_id)
        if _PUBLIC_ASSET_FILENAME.fullmatch(filename) is None:
            raise ClientApiProblem(
                "asset_not_found", "The requested asset was not found.", 404
            )
        reference = f"assets/{filename}"
        try:
            asset = require_services().runtime.public_asset(attempt_id, reference)
        except (KeyError, ValueError, TypeError):
            raise ClientApiProblem(
                "asset_not_found", "The requested asset was not found.", 404
            ) from None
        return Response(
            content=asset.content,
            media_type=asset.media_type,
            headers={
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": "inline",
            },
        )

    @app.post("/api/device/enroll")
    async def enroll(body: EnrollmentBody):
        current = require_services()
        receipt = current.coordinator.enroll(body.label.strip(), body.enrollment_code)
        context.identity = current.identity_store.load_or_create()
        context._validate_expected_key()
        context._ensure_runtime()
        if current.background_prefetch and current.runtime is not None:
            context._start_prefetch_thread()
        return {"state": "login", "device_id": receipt.device_id}

    @app.post("/api/device/coordinator")
    async def configure_coordinator(body: CoordinatorConfigurationBody):
        if not body.confirmed:
            raise ClientApiProblem(
                "confirmation_required", "Configuration confirmation is required.", 422
            )
        try:
            normalized = _normalize_coordinator_base_url(body.base_url)
        except ValueError as error:
            raise ClientApiProblem(
                "invalid_coordinator_configuration",
                "The coordinator address is invalid.",
                422,
            ) from error
        current = require_services()
        if (
            current.config_store is None
            or not callable(current.coordinator_factory)
            or not callable(current.outbox_factory)
        ):
            raise ClientApiProblem(
                "coordinator_configuration_unavailable",
                "Coordinator configuration is managed by IT on this computer.",
                403,
            )
        try:
            active = current.store.active_attempt()
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
            raise ClientApiProblem(
                "corrupt_local_attempt",
                _KNOWN_PUBLIC_MESSAGES["corrupt_local_attempt"],
                409,
            ) from error
        if active is not None:
            raise ClientApiProblem(
                "active_attempt_configuration_locked",
                "The coordinator address cannot change while saved assessment work is active.",
                409,
            )
        candidate = None
        candidate_outbox = None
        try:
            candidate = current.coordinator_factory(normalized)
            if getattr(context.identity, "device_id", None) is not None:
                # A signed catalog round trip validates TLS, the registered device,
                # and the expected coordinator signing key before persistence.
                candidate.prefetch_catalog()
            candidate_outbox = current.outbox_factory(candidate)
            candidate_outbox.start()
            current.config_store.update_base_url(normalized)
        except CoordinatorProblem as error:
            if candidate_outbox is not None:
                candidate_outbox.stop()
            if candidate is not None:
                candidate.close()
            raise _map_coordinator_problem(error) from error
        except (OSError, ValueError, TypeError) as error:
            if candidate_outbox is not None:
                candidate_outbox.stop()
            if candidate is not None:
                candidate.close()
            raise ClientApiProblem(
                "coordinator_configuration_update_failed",
                "The coordinator address could not be saved; the previous configuration is still active.",
                500,
            ) from error

        previous_coordinator = current.coordinator
        previous_outbox = current.outbox
        try:
            previous_coordinator.logout()
        finally:
            previous_outbox.stop()
        current.coordinator = candidate
        current.outbox = candidate_outbox
        context._outbox_stop_called = False
        context.catalog.clear()
        context.ready_releases.clear()
        candidate_outbox.wake()
        if current.owns_resources:
            previous_coordinator.close()
        return {"state": "login", "configuration": "updated"}

    @app.post("/api/login")
    async def login(body: LoginBody):
        current = require_services()
        session = current.coordinator.login(body.student_id, body.password)
        return {"state": "waiting_or_ready", "student": _student_payload(session)}

    @app.post("/api/logout")
    async def logout(body: ConfirmBody):
        if not body.confirmed:
            raise ClientApiProblem("confirmation_required", "Logout confirmation is required.", 422)
        current = require_services()
        current.coordinator.logout()
        snapshot = current_snapshot()
        return {"state": _attempt_payload(context, snapshot, include_questions=False)["state"] if snapshot else "login"}

    @app.post("/api/content/prefetch")
    async def prefetch(body: PrefetchBody):
        release_id = _strict_uuid(body.release_id, "Release identifier")
        try:
            prepared = context.prefetch(release_id)
        except CoordinatorProblem as error:
            raise _map_coordinator_problem(error) from error
        return {"state": "ready", "release_id": prepared[0]}

    @app.get("/api/assessments")
    async def assessments():
        current = require_services()
        rows = current.coordinator.assessments()
        return {
            "assessments": [
                _assessment_payload(row, current.store, context) for row in rows
            ]
        }

    @app.post("/api/assessments/{release_id}/start")
    async def start(release_id: str, body: ConfirmBody):
        release_id = _strict_uuid(release_id, "Release identifier")
        if not body.confirmed:
            raise ClientApiProblem("confirmation_required", "Start confirmation is required.", 422)
        current = require_services()
        if current.coordinator.session is None:
            raise ClientApiProblem("client_session_required", "Student login is required.", 401)
        entry = context.catalog.get(release_id)
        if entry is None or release_id not in context.ready_releases:
            raise ClientApiProblem("content_not_ready", "Assessment content is not ready.", 409)
        if current.runtime is None:
            raise ClientApiProblem("device_inactive", _KNOWN_PUBLIC_MESSAGES["device_inactive"], 403)
        response = current.coordinator.start_attempt(release_id, entry.content_hash)
        snapshot = current.runtime.start(response, student_id=current.coordinator.session.student_id)
        return _attempt_payload(context, snapshot, include_questions=True)

    @app.get("/api/attempts/active")
    async def active_attempt():
        snapshot = current_snapshot()
        if snapshot is None:
            return JSONResponse({"state": "none", "attempt": None}, status_code=404)
        return {"state": _attempt_payload(context, snapshot, include_questions=False)["state"], "attempt": _attempt_payload(context, snapshot, include_questions=True)}

    @app.get("/api/attempts/{attempt_id}")
    async def attempt(attempt_id: str):
        return _attempt_payload(context, require_attempt(attempt_id), include_questions=True)

    @app.put("/api/attempts/{attempt_id}/responses/{question_id}")
    async def answer(attempt_id: str, question_id: int, body: AnswerBody):
        require_attempt(attempt_id, editable=True)
        current = require_services()
        try:
            snapshot = current.runtime.answer(question_id, body.answer)
        except AttemptSealedError as error:
            raise ClientApiProblem("attempt_sealed", _SEALED_MESSAGE, 409) from error
        return {
            "attempt_id": snapshot.attempt_id,
            "state": snapshot.state,
            "question_id": question_id,
            "selected_answer": snapshot.responses.get(question_id),
            "remaining_seconds": snapshot.remaining_seconds,
        }

    @app.post("/api/attempts/{attempt_id}/violations")
    async def violation(attempt_id: str, body: ViolationBody):
        require_attempt(attempt_id, editable=True)
        current = require_services()
        try:
            snapshot = current.runtime.record_violation(body.event_type.strip())
        except AttemptSealedError as error:
            raise ClientApiProblem("attempt_sealed", _SEALED_MESSAGE, 409) from error
        return {
            "attempt_id": snapshot.attempt_id,
            "state": snapshot.state,
            "violations": snapshot.violations,
            "remaining_seconds": snapshot.remaining_seconds,
        }

    @app.post("/api/attempts/{attempt_id}/submit", status_code=202)
    async def submit(attempt_id: str, body: ConfirmBody):
        require_attempt(attempt_id)
        if not body.confirmed:
            raise ClientApiProblem("confirmation_required", "Submit confirmation is required.", 422)
        current = require_services()
        snapshot = current.runtime.submit()
        if snapshot.attempt_id != attempt_id:
            raise ClientApiProblem("attempt_mismatch", "The requested attempt is not active.", 409)
        current.outbox.wake()
        return _attempt_payload(context, snapshot, include_questions=False)

    @app.get("/api/attempts/{attempt_id}/result")
    async def result(attempt_id: str):
        _strict_uuid(attempt_id, "Attempt identifier")
        current = require_services()
        try:
            record = current.store.load_attempt(attempt_id)
        except KeyError as error:
            raise ClientApiProblem("attempt_not_found", "The local attempt was not found.", 404) from error
        if record.state == "acknowledged" and record.receipt is not None:
            return {"state": "acknowledged_result", "result": _jsonable(record.receipt)}
        if record.state == "sealed_pending":
            snapshot = current.runtime.snapshot()
            return JSONResponse(
                _attempt_payload(context, snapshot, include_questions=False), status_code=202
            )
        raise ClientApiProblem("result_not_ready", "The authoritative result is not ready.", 409)

    return app


def _student_payload(session: Any | None) -> dict[str, str] | None:
    if session is None:
        return None
    return {"student_id": session.student_id, "student_name": session.student_name}


def _assessment_payload(
    row: Any, store: ClientStore, context: _ClientContext
) -> dict[str, Any]:
    payload = {
        "release_id": row.release_id,
        "test_id": row.test_id,
        "test_name": row.test_name,
        "duration_seconds": row.duration_seconds,
        "launch_closes_at": _jsonable(row.launch_closes_at),
        "attempt_id": row.attempt_id,
        "attempt_deadline": _jsonable(row.attempt_deadline),
        "content_ready": (
            row.release_id in context.ready_releases
            and store.verified_pack(row.release_id, row.content_hash) is not None
        ),
    }
    return payload


app = create_client_app()


def main() -> None:
    import uvicorn

    uvicorn.run(create_client_app(), host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
