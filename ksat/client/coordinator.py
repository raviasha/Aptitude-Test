"""Typed, authenticated HTTPS transport for the installed lab client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import ssl
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, ValidationError, model_validator

from ksat.client.identity import DeviceIdentity
from ksat.crypto import verify_json
from ksat.protocol import (
    AttemptStartResponse,
    ClientLoginRequest,
    ClientSession,
    DeviceEnrollmentReceipt,
    DeviceEnrollmentRequest,
    PACK_FORMAT_VERSION,
    PROTOCOL_VERSION,
    ProtocolModel,
    PublicReleaseDescriptor,
    SignedResponseBundle,
    SubmissionReceipt,
    canonical_json,
    device_request_bytes,
)


_API_PREFIX = "/api/client/v1"
_MAX_JSON_BYTES = 1024 * 1024
_MAX_PACK_BYTES = 64 * 1024 * 1024
_PACK_CHUNK_BYTES = 256 * 1024
_MAX_RETRY_AFTER_SECONDS = 300.0
_CONTENT_HASH_LENGTH = 64
_NO_RETRY_CODES = frozenset({
    "invalid_credentials",
    "invalid_client_session",
    "invalid_device_key",
    "device_inactive",
    "invalid_bundle_signature",
    "invalid_bundle_content",
    "invalid_ticket_signature",
    "invalid_attempt_ticket",
    "content_hash_mismatch",
    "release_answer_state_invalid",
    "device_identity_mismatch",
})


class CoordinatorProblem(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        retry_after: float | None = None,
        *,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.status_code = status_code


class ContentVerificationError(CoordinatorProblem):
    def __init__(self, message: str):
        super().__init__("content_verification_failed", message, False)


class ReleaseCatalogEntry(ProtocolModel):
    release_id: str
    filename: str
    content_hash: str
    pack_signature_b64: str
    byte_size: int = Field(gt=0, le=_MAX_PACK_BYTES)
    pack_format_version: int
    descriptor: PublicReleaseDescriptor

    @model_validator(mode="after")
    def linked_descriptor(self) -> "ReleaseCatalogEntry":
        descriptor = self.descriptor
        try:
            parsed_release_id = uuid.UUID(descriptor.release_id)
        except (AttributeError, ValueError) as error:
            raise ValueError("Coordinator release catalog linkage is invalid.") from error
        if (
            str(parsed_release_id) != descriptor.release_id
            or self.release_id != descriptor.release_id
            or self.filename != descriptor.content_pack_filename
            or self.content_hash != descriptor.content_hash
            or self.pack_signature_b64 != descriptor.content_signature_b64
            or self.pack_format_version != descriptor.manifest.pack_format_version
            or descriptor.manifest.protocol_version != PROTOCOL_VERSION
            or descriptor.manifest.pack_format_version != PACK_FORMAT_VERSION
            or descriptor.release_id != descriptor.manifest.release_id
            or descriptor.test_id != descriptor.manifest.test_id
            or descriptor.duration_seconds != descriptor.manifest.duration_seconds
            or descriptor.canonical_question_ids != descriptor.manifest.canonical_question_ids
            or descriptor.content_pack_filename != f"{descriptor.release_id}.ksatpack"
            or descriptor.state not in {"prepared", "launched"}
            or len(descriptor.content_hash) != _CONTENT_HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in descriptor.content_hash)
        ):
            raise ValueError("Coordinator release catalog linkage is invalid.")
        return self


class AssessmentSummary(ProtocolModel):
    release_id: str
    test_id: int
    test_name: str
    duration_seconds: int = Field(gt=0)
    content_hash: str
    launch_closes_at: datetime
    attempt_id: str | None
    attempt_deadline: datetime | None

    @model_validator(mode="after")
    def valid_summary(self) -> "AssessmentSummary":
        if (
            len(self.content_hash) != _CONTENT_HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in self.content_hash)
            or self.launch_closes_at.tzinfo is None
            or (self.attempt_deadline is not None and self.attempt_deadline.tzinfo is None)
        ):
            raise ValueError("Coordinator assessment summary is invalid.")
        try:
            uuid.UUID(self.release_id)
            if self.attempt_id is not None:
                uuid.UUID(self.attempt_id)
        except (AttributeError, ValueError) as error:
            raise ValueError("Coordinator assessment summary is invalid.") from error
        return self


def _duplicates_rejected(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate JSON key.")
        value[key] = item
    return value


def _decode_json(body: bytes, code: str) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_duplicates_rejected,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number.")),
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise CoordinatorProblem(code, "The coordinator returned an invalid response.", False) from error


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    seconds: float
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed.astimezone(timezone.utc) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def _raw_private_key(value: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError("Protected device identity is invalid.") from error
    if len(raw) != 32:
        raise ValueError("Protected device identity is invalid.")
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as error:
        raise ValueError("Protected device identity is invalid.") from error


def _is_reparse(value: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(value, "st_file_attributes", 0) & marker)


def _safe_existing_hash(path: Path) -> str:
    before = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ContentVerificationError("Assessment content destination is unsafe.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = os.stat(path, follow_symlinks=False)
    if (
        stat.S_ISLNK(after.st_mode)
        or _is_reparse(after)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ContentVerificationError("Assessment content destination changed while verified.")
    return digest


class CoordinatorClient:
    def __init__(
        self,
        base_url,
        ca_file,
        identity,
        *,
        transport=None,
        timeout_seconds: float = 10.0,
    ):
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Coordinator timeout must be a positive finite number.")
        raw_url = str(base_url).rstrip("/")
        try:
            url = httpx.URL(raw_url)
        except Exception as error:
            raise ValueError("Coordinator URL is invalid.") from error
        if (
            not url.host
            or bool(url.username)
            or bool(url.password)
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or (transport is None and url.scheme != "https")
            or (transport is not None and url.scheme not in {"http", "https"})
        ):
            raise ValueError("Coordinator URL is invalid or insecure.")

        self.base_url = raw_url
        self._identity_source = identity if hasattr(identity, "load_or_create") else None
        self._identity = identity.load_or_create() if self._identity_source is not None else identity
        if not isinstance(self._identity, DeviceIdentity):
            raise TypeError("Coordinator client requires a protected device identity.")
        self._session: ClientSession | None = None
        self._catalog_sizes: dict[tuple[str, str], int] = {}
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=timeout_seconds,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=timeout_seconds,
        )
        client_options = {
            "base_url": self.base_url,
            "timeout": timeout,
            "follow_redirects": False,
            "limits": httpx.Limits(max_connections=8, max_keepalive_connections=4),
        }
        if transport is None:
            ca_path = Path(ca_file)
            try:
                supplied_metadata = os.stat(ca_path.absolute(), follow_symlinks=False)
                resolved_ca = ca_path.resolve(strict=True)
                metadata = os.stat(resolved_ca, follow_symlinks=False)
            except (OSError, RuntimeError) as error:
                raise ValueError("Trusted coordinator CA file is unavailable.") from error
            if (
                stat.S_ISLNK(supplied_metadata.st_mode)
                or _is_reparse(supplied_metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise ValueError("Trusted coordinator CA file is unsafe.")
            client_options["verify"] = ssl.create_default_context(cafile=str(resolved_ca))
        else:
            client_options["transport"] = transport
        self._client = httpx.Client(**client_options)

    @property
    def session(self) -> ClientSession | None:
        return self._session

    def close(self) -> None:
        self._session = None
        self._client.close()

    def _current_identity(self, *, enrolled: bool) -> DeviceIdentity:
        identity = self._identity
        if enrolled and (identity.device_id is None or identity.coordinator_public_key_b64 is None):
            raise CoordinatorProblem("device_not_enrolled", "This lab computer is not enrolled.", False)
        return identity

    def _proof_headers(self, method: str, path: str, body: bytes, *, bearer: bool) -> dict[str, str]:
        identity = self._current_identity(enrolled=True)
        nonce = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        signed = device_request_bytes(method, path, body, timestamp, nonce)
        signature = _raw_private_key(identity.private_key_b64).sign(signed)
        headers = {
            "X-KSAT-Device": identity.device_id,
            "X-KSAT-Timestamp": timestamp,
            "X-KSAT-Nonce": nonce,
            "X-KSAT-Signature": base64.b64encode(signature).decode("ascii"),
        }
        if bearer:
            if self._session is None or not self._session.access_token:
                raise CoordinatorProblem("client_session_required", "Student login is required.", False)
            headers["Authorization"] = f"Bearer {self._session.access_token}"
        return headers

    def _raise_for_status(self, status: int, headers: httpx.Headers, body: bytes) -> None:
        if 300 <= status < 400:
            raise CoordinatorProblem(
                "coordinator_redirect_rejected", "Coordinator redirects are not allowed.", False,
                status_code=status,
            )
        if 200 <= status < 300:
            return
        try:
            payload = _decode_json(body, "invalid_coordinator_error")
            if not isinstance(payload, dict) or set(payload) != {"detail"}:
                raise ValueError
            detail = payload["detail"]
            if (
                not isinstance(detail, dict)
                or set(detail) != {"code", "message", "retryable"}
                or not isinstance(detail["code"], str)
                or not detail["code"].strip()
                or not isinstance(detail["message"], str)
                or not detail["message"].strip()
                or type(detail["retryable"]) is not bool
            ):
                raise ValueError
        except (CoordinatorProblem, ValueError, TypeError, KeyError) as error:
            raise CoordinatorProblem(
                "invalid_coordinator_error",
                "The coordinator returned an invalid error response.",
                status == 429 or status >= 500,
                status_code=status,
            ) from error
        code = detail["code"]
        permitted_retry = status == 429 or status >= 500 or code == "submission_busy"
        retryable = bool(
            permitted_retry
            and code not in _NO_RETRY_CODES
            and (detail["retryable"] or status == 429 or status >= 500)
        )
        retry_after = _parse_retry_after(headers.get("Retry-After"), datetime.now(timezone.utc))
        raise CoordinatorProblem(
            code, detail["message"], retryable, retry_after if retryable else None,
            status_code=status,
        )

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        signed: bool = True,
        bearer: bool = False,
        maximum: int = _MAX_JSON_BYTES,
    ) -> bytes:
        headers = {"Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/json"
        if signed:
            headers.update(self._proof_headers(method, path, body, bearer=bearer))
        try:
            with self._client.stream(method, path, content=body, headers=headers) as response:
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > maximum:
                        raise CoordinatorProblem(
                            "coordinator_response_too_large",
                            "The coordinator response exceeded its allowed size.",
                            False,
                        )
                    chunks.append(chunk)
                response_body = b"".join(chunks)
                self._raise_for_status(response.status_code, response.headers, response_body)
                return response_body
        except CoordinatorProblem:
            raise
        except httpx.TimeoutException as error:
            raise CoordinatorProblem(
                "coordinator_timeout", "The coordinator request timed out.", True
            ) from error
        except httpx.TransportError as error:
            raise CoordinatorProblem(
                "coordinator_unavailable", "The coordinator is unavailable.", True
            ) from error

    @staticmethod
    def _typed(model, body: bytes, *, exact_keys: set[str] | None = None):
        value = _decode_json(body, "invalid_coordinator_response")
        if exact_keys is not None and (not isinstance(value, dict) or set(value) != exact_keys):
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        try:
            return model.model_validate_json(body, strict=True)
        except (ValidationError, ValueError, TypeError) as error:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            ) from error

    def enroll(self, label: str, enrollment_code: str) -> DeviceEnrollmentReceipt:
        identity = self._current_identity(enrolled=False)
        if identity.device_id is not None or identity.coordinator_public_key_b64 is not None:
            raise CoordinatorProblem(
                "device_already_enrolled", "This lab computer is already enrolled.", False
            )
        payload = DeviceEnrollmentRequest(
            label=label, public_key_b64=identity.public_key_b64, enrollment_code=enrollment_code
        )
        body = canonical_json(payload)
        receipt = self._typed(
            DeviceEnrollmentReceipt,
            self._request_bytes("POST", f"{_API_PREFIX}/devices/enroll", body=body, signed=False),
            exact_keys={"device_id", "coordinator_public_key_b64"},
        )
        try:
            parsed_device_id = uuid.UUID(receipt.device_id)
            coordinator_key = base64.b64decode(
                receipt.coordinator_public_key_b64.encode("ascii"), validate=True
            )
        except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            ) from error
        if str(parsed_device_id) != receipt.device_id or len(coordinator_key) != 32:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        if self._identity_source is not None and hasattr(self._identity_source, "save_enrollment"):
            self._identity = self._identity_source.save_enrollment(
                receipt.device_id, receipt.coordinator_public_key_b64
            )
        else:
            self._identity = DeviceIdentity(
                identity.private_key_b64, identity.public_key_b64,
                receipt.device_id, receipt.coordinator_public_key_b64,
            )
        return receipt

    def login(self, student_id: str, password: str) -> ClientSession:
        self._session = None
        identity = self._current_identity(enrolled=True)
        payload = ClientLoginRequest(
            student_id=student_id, password=password, device_id=identity.device_id
        )
        body = canonical_json(payload)
        session = self._typed(
            ClientSession,
            self._request_bytes("POST", f"{_API_PREFIX}/session", body=body),
            exact_keys={
                "access_token", "student_id", "student_name", "device_id", "expires_in_seconds"
            },
        )
        if session.device_id != identity.device_id:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        if (
            not session.access_token.strip()
            or not session.student_id.strip()
            or not session.student_name.strip()
            or session.expires_in_seconds <= 0
        ):
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        self._session = session
        return session

    def logout(self) -> None:
        self._session = None

    def prefetch_catalog(self) -> list[ReleaseCatalogEntry]:
        body = self._request_bytes("GET", f"{_API_PREFIX}/releases")
        value = _decode_json(body, "invalid_coordinator_response")
        if not isinstance(value, dict) or set(value) != {"releases"} or not isinstance(value["releases"], list):
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        try:
            entries = [ReleaseCatalogEntry.model_validate(item, strict=True) for item in value["releases"]]
            if len({entry.release_id for entry in entries}) != len(entries):
                raise ValueError("Duplicate release.")
            identity = self._current_identity(enrolled=True)
            for entry in entries:
                verify_json(
                    identity.coordinator_public_key_b64,
                    {
                        "release_id": entry.descriptor.release_id,
                        "content_hash": entry.descriptor.content_hash,
                        "manifest": entry.descriptor.manifest.model_dump(mode="json"),
                    },
                    entry.descriptor.content_signature_b64,
                )
        except (ValidationError, ValueError, TypeError) as error:
            raise CoordinatorProblem(
                "invalid_release_catalog", "The coordinator release catalog is invalid.", False
            ) from error
        self._catalog_sizes = {
            (entry.release_id, entry.content_hash): entry.byte_size for entry in entries
        }
        return entries

    def assessments(self) -> list[AssessmentSummary]:
        body = self._request_bytes("GET", f"{_API_PREFIX}/assessments", bearer=True)
        value = _decode_json(body, "invalid_coordinator_response")
        if not isinstance(value, dict) or set(value) != {"assessments"} or not isinstance(value["assessments"], list):
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        try:
            assessments = [
                AssessmentSummary.model_validate_json(canonical_json(item), strict=True)
                for item in value["assessments"]
            ]
            if len({item.release_id for item in assessments}) != len(assessments):
                raise ValueError("Duplicate assessment release.")
            return assessments
        except (ValidationError, ValueError, TypeError) as error:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            ) from error

    def start_attempt(self, release_id: str, confirmed_content_hash: str) -> AttemptStartResponse:
        body = canonical_json({
            "release_id": release_id, "confirmed_content_hash": confirmed_content_hash,
        })
        response = self._typed(
            AttemptStartResponse,
            self._request_bytes(
                "POST", f"{_API_PREFIX}/attempts/start", body=body, bearer=True
            ),
            exact_keys={"ticket", "canonical_question_ids", "server_time"},
        )
        identity = self._current_identity(enrolled=True)
        try:
            verify_json(
                identity.coordinator_public_key_b64,
                response.ticket.ticket,
                response.ticket.signature_b64,
            )
        except ValueError as error:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            ) from error
        ticket = response.ticket.ticket
        if (
            ticket.device_id != identity.device_id
            or self._session is None
            or ticket.student_id != self._session.student_id
            or ticket.release_id != release_id
            or ticket.content_hash != confirmed_content_hash
            or response.canonical_question_ids != list(dict.fromkeys(response.canonical_question_ids))
        ):
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        return response

    def submit_bundle(self, bundle: SignedResponseBundle) -> SubmissionReceipt:
        if not isinstance(bundle, SignedResponseBundle):
            raise TypeError("Submission bundle is invalid.")
        body = canonical_json(bundle)
        receipt = self._typed(
            SubmissionReceipt,
            self._request_bytes("POST", f"{_API_PREFIX}/submissions", body=body),
            exact_keys={
                "attempt_id", "accepted_at", "score", "total_questions", "attempted",
                "percentage", "violations",
            },
        )
        if receipt.attempt_id != bundle.bundle.ticket.ticket.attempt_id:
            raise CoordinatorProblem(
                "invalid_coordinator_response", "The coordinator returned an invalid response.", False
            )
        return receipt

    def download_pack(
        self,
        descriptor: ReleaseCatalogEntry | PublicReleaseDescriptor,
        destination: Path,
    ) -> Path:
        if isinstance(descriptor, ReleaseCatalogEntry):
            entry = descriptor
            public = entry.descriptor
            expected_size = entry.byte_size
        elif isinstance(descriptor, PublicReleaseDescriptor):
            public = descriptor
            expected_size = self._catalog_sizes.get((public.release_id, public.content_hash))
            if expected_size is None:
                raise ContentVerificationError(
                    "Assessment content size is not available from the authenticated catalog."
                )
        else:
            raise TypeError("Assessment release descriptor is invalid.")
        destination = Path(destination)
        if ".." in destination.parts or destination.name != public.content_pack_filename:
            raise ContentVerificationError("Assessment content destination is invalid.")
        try:
            unresolved_parent = destination.parent.absolute()
            for candidate in (unresolved_parent, *unresolved_parent.parents):
                metadata = os.stat(candidate, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                    raise ContentVerificationError("Assessment content destination is unsafe.")
            parent = unresolved_parent.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ContentVerificationError("Assessment content destination is unavailable.") from error
        destination = parent / destination.name
        if os.path.lexists(destination):
            if _safe_existing_hash(destination) == public.content_hash:
                return destination
            raise ContentVerificationError("Existing assessment content does not match.")

        path = f"{_API_PREFIX}/releases/{public.release_id}/pack"
        headers = self._proof_headers("GET", path, b"", bearer=False)
        headers["Accept"] = "application/octet-stream"
        temporary: Path | None = None
        try:
            with self._client.stream("GET", path, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise CoordinatorProblem(
                        "coordinator_redirect_rejected", "Coordinator redirects are not allowed.", False,
                        status_code=response.status_code,
                    )
                if not (200 <= response.status_code < 300):
                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_JSON_BYTES:
                            raise CoordinatorProblem(
                                "coordinator_response_too_large",
                                "The coordinator response exceeded its allowed size.", False,
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    self._raise_for_status(response.status_code, response.headers, body)
                raw_length = response.headers.get("Content-Length")
                try:
                    declared = int(raw_length) if raw_length is not None else None
                except ValueError as error:
                    raise ContentVerificationError("Assessment content length is invalid.") from error
                if declared is not None and declared != expected_size:
                    raise ContentVerificationError("Assessment content length does not match its catalog.")
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.{uuid.uuid4()}.", suffix=".partial", dir=parent
                )
                temporary = Path(temporary_name)
                digest = hashlib.sha256()
                byte_count = 0
                with os.fdopen(file_descriptor, "wb") as stream:
                    for chunk in response.iter_bytes(chunk_size=_PACK_CHUNK_BYTES):
                        byte_count += len(chunk)
                        if byte_count > expected_size or byte_count > _MAX_PACK_BYTES:
                            raise ContentVerificationError("Assessment content exceeds its declared size.")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if byte_count != expected_size:
                    raise ContentVerificationError("Assessment content is truncated.")
                if digest.hexdigest() != public.content_hash:
                    raise ContentVerificationError("Assessment content hash does not match.")
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if _safe_existing_hash(destination) != public.content_hash:
                    raise ContentVerificationError("Existing assessment content does not match.")
            if destination.exists():
                with destination.open("rb+") as published:
                    os.fsync(published.fileno())
            self._sync_directory(parent)
            return destination
        except CoordinatorProblem:
            raise
        except httpx.TimeoutException as error:
            raise CoordinatorProblem(
                "coordinator_timeout", "The coordinator request timed out.", True
            ) from error
        except httpx.TransportError as error:
            raise CoordinatorProblem(
                "coordinator_unavailable", "The coordinator is unavailable.", True
            ) from error
        except OSError as error:
            raise ContentVerificationError("Assessment content could not be saved.") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "AssessmentSummary",
    "ContentVerificationError",
    "CoordinatorClient",
    "CoordinatorProblem",
    "ReleaseCatalogEntry",
]
