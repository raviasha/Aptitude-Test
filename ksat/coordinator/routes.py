"""Installed-client coordinator API routes."""

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from ksat.coordinator.auth import (
    CLIENT_SESSION_SECONDS,
    AuthenticationProblem,
    issue_student_access_token,
    register_device,
    verify_device_request,
    verify_student_access_token,
)
from ksat.coordinator.attempts import (
    AttemptProblem,
    issue_attempt_ticket,
    list_launched_assessments,
    list_prefetchable_releases,
    preflight_attempt_start,
)
from ksat.coordinator.releases import load_release_manifest
from ksat.coordinator.submissions import SubmissionProblem, validate_and_score
from ksat.protocol import (
    AttemptStartResponse,
    ClientLoginRequest,
    ClientSession,
    DeviceEnrollmentReceipt,
    DeviceEnrollmentRequest,
    SignedResponseBundle,
    SubmissionReceipt,
)
from ksat.sqlite import connect_sqlite


@dataclass
class CoordinatorConfig:
    db_path: Path
    data_dir: Path
    session_secret: str
    device_enrollment_code: str
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    submission_writer: Any | None = None


router = APIRouter(prefix="/api/client/v1")

_CONTENT_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PACK_COPY_CHUNK_BYTES = 1024 * 1024
_PACK_SPOOL_MEMORY_BYTES = 1024 * 1024
_MAX_PACK_SNAPSHOT_BYTES = 64 * 1024 * 1024


class AttemptStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str
    confirmed_content_hash: str


def _config(request: Request) -> CoordinatorConfig:
    return request.app.state.coordinator_config


def _raise_http(error: AuthenticationProblem) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail()) from error


def _raise_attempt_http(error: AttemptProblem) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail()) from error


def _raise_submission_http(error: SubmissionProblem) -> None:
    headers = {"Retry-After": "1"} if error.retryable and error.status_code == 503 else None
    raise HTTPException(
        status_code=error.status_code,
        detail=error.detail(),
        headers=headers,
    ) from error


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _verified_device(request: Request, connection) -> str:
    return verify_device_request(
        connection,
        device_id=request.headers.get("X-KSAT-Device", ""),
        method=request.method,
        path=request.url.path,
        body=await request.body(),
        timestamp=request.headers.get("X-KSAT-Timestamp", ""),
        nonce=request.headers.get("X-KSAT-Nonce", ""),
        signature_b64=request.headers.get("X-KSAT-Signature", ""),
    )


def _verified_student(request: Request, config: CoordinatorConfig, device_id: str) -> str:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise AuthenticationProblem(
            "invalid_client_session", "The client session is invalid.", status_code=401
        )
    claims = verify_student_access_token(config.session_secret, authorization[7:].strip())
    if claims["device_id"] != device_id:
        raise AuthenticationProblem(
            "invalid_client_session", "The client session is invalid.", status_code=403
        )
    return claims["student_id"]


def _pack_path(config: CoordinatorConfig, filename: str) -> Path:
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
    try:
        root = (config.data_dir / "Assessment Releases").resolve(strict=True)
        candidate = (root / filename).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.") from error
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
    return candidate


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _is_reparse_point(value: os.stat_result) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(value, "st_file_attributes", 0) & attribute)


def _open_pack_source(path: Path) -> BinaryIO:
    source: BinaryIO | None = None
    try:
        before = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
            raise OSError("Assessment pack path is not a regular file.")
        source = path.open("rb")
        opened = os.fstat(source.fileno())
        after = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or _is_reparse_point(after)
            or _file_identity(before) != _file_identity(opened)
            or _file_identity(after) != _file_identity(opened)
        ):
            raise OSError("Assessment pack changed while it was opened.")
        return source
    except Exception:
        if source is not None:
            source.close()
        raise


def _new_pack_snapshot() -> BinaryIO:
    return tempfile.SpooledTemporaryFile(
        max_size=_PACK_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )


def _copy_pack_to_snapshot(source: BinaryIO, snapshot: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    while True:
        chunk = source.read(_PACK_COPY_CHUNK_BYTES)
        if not chunk:
            break
        byte_size += len(chunk)
        if byte_size > _MAX_PACK_SNAPSHOT_BYTES:
            raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
        digest.update(chunk)
        snapshot.write(chunk)
    return digest.hexdigest(), byte_size


def _verified_pack_snapshot(
    connection,
    config: CoordinatorConfig,
    *,
    release_id: str,
    filename: str,
    expected_hash: str,
) -> tuple[BinaryIO, int]:
    snapshot: BinaryIO | None = None
    try:
        path = _pack_path(config, filename)
        snapshot = _new_pack_snapshot()
        with _open_pack_source(path) as source:
            actual_hash, byte_size = _copy_pack_to_snapshot(source, snapshot)
        if actual_hash != expected_hash:
            raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
        load_release_manifest(
            connection,
            release_id,
            signing_public_key_b64=config.signing_public_key_b64,
            pack_master_key=config.pack_master_key,
            encrypted_pack_file=snapshot,
        )
        snapshot.seek(0)
        return snapshot, byte_size
    except AttemptProblem:
        if snapshot is not None:
            snapshot.close()
        raise
    except (KeyError, OSError, ValueError) as error:
        if snapshot is not None:
            snapshot.close()
        raise AttemptProblem(
            "content_not_ready", "Assessment content is not ready."
        ) from error
    except Exception:
        if snapshot is not None:
            snapshot.close()
        raise


async def _stream_pack_snapshot(snapshot: BinaryIO) -> AsyncIterator[bytes]:
    while True:
        chunk = await run_in_threadpool(snapshot.read, _PACK_COPY_CHUNK_BYTES)
        if not chunk:
            break
        yield chunk


class _PackSnapshotResponse(StreamingResponse):
    def __init__(self, snapshot: BinaryIO, **kwargs: Any) -> None:
        self._snapshot = snapshot
        self._snapshot_closed = False
        super().__init__(_stream_pack_snapshot(snapshot), **kwargs)

    def _close_snapshot(self) -> None:
        if self._snapshot_closed:
            return
        self._snapshot_closed = True
        self._snapshot.close()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close_snapshot()


def _etag_matches(header_value: str, content_hash: str) -> bool:
    for raw_tag in header_value.split(","):
        tag = raw_tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag == "*" or tag == f'"{content_hash}"':
            return True
    return False


def _assert_encrypted_pack_ready(connection, config: CoordinatorConfig, release_id: str) -> None:
    row = connection.execute(
        """SELECT content_pack_filename, content_hash, state
           FROM assessment_releases WHERE release_id = ?""",
        (release_id,),
    ).fetchone()
    if row is None or row["state"] not in {"prepared", "launched"}:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
    path = _pack_path(config, row["content_pack_filename"])
    try:
        with path.open("rb") as pack_file:
            actual_hash = hashlib.file_digest(pack_file, "sha256").hexdigest()
    except OSError as error:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.") from error
    if actual_hash != row["content_hash"]:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.")


@router.post("/devices/enroll", response_model=DeviceEnrollmentReceipt)
def enroll_device(payload: DeviceEnrollmentRequest, request: Request) -> DeviceEnrollmentReceipt:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        receipt = register_device(
            connection,
            payload,
            expected_enrollment_code=config.device_enrollment_code,
            coordinator_public_key_b64=config.signing_public_key_b64,
            now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        connection.commit()
        return receipt
    except AuthenticationProblem as error:
        connection.rollback()
        _raise_http(error)
    finally:
        connection.close()


@router.post("/session", response_model=ClientSession)
async def create_client_session(payload: ClientLoginRequest, request: Request) -> ClientSession:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        device_id = request.headers.get("X-KSAT-Device", "")
        verified_device_id = verify_device_request(
            connection,
            device_id=device_id,
            method=request.method,
            path=request.url.path,
            body=await request.body(),
            timestamp=request.headers.get("X-KSAT-Timestamp", ""),
            nonce=request.headers.get("X-KSAT-Nonce", ""),
            signature_b64=request.headers.get("X-KSAT-Signature", ""),
        )
        if payload.device_id != verified_device_id:
            raise AuthenticationProblem(
                "invalid_device_key",
                "The device request proof is invalid.",
                status_code=403,
            )
        student_id = payload.student_id.strip().upper()
        student = connection.execute(
            "SELECT student_id, name, password_hash FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
        credentials_valid = False
        if student is not None:
            try:
                credentials_valid = bcrypt.checkpw(
                    payload.password.encode(), student["password_hash"].encode()
                )
            except (AttributeError, TypeError, ValueError):
                credentials_valid = False
        if not credentials_valid:
            raise AuthenticationProblem(
                "invalid_credentials", "The student credentials are invalid.", status_code=401
            )
        return ClientSession(
            access_token=issue_student_access_token(config.session_secret, student_id, verified_device_id),
            student_id=student_id,
            student_name=student["name"],
            device_id=verified_device_id,
            expires_in_seconds=CLIENT_SESSION_SECONDS,
        )
    except AuthenticationProblem as error:
        _raise_http(error)
    finally:
        connection.close()


@router.get("/releases")
async def releases_catalog(request: Request) -> dict[str, Any]:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        await _verified_device(request, connection)
        items = list_prefetchable_releases(connection)
        for item in items:
            snapshot, byte_size = _verified_pack_snapshot(
                connection,
                config,
                release_id=item["release_id"],
                filename=item["filename"],
                expected_hash=item["content_hash"],
            )
            snapshot.close()
            item["byte_size"] = byte_size
        return {"releases": items}
    except AuthenticationProblem as error:
        _raise_http(error)
    except AttemptProblem as error:
        _raise_attempt_http(error)
    finally:
        connection.close()


@router.get("/releases/{release_id}/pack")
async def release_pack(release_id: str, request: Request) -> Response:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        await _verified_device(request, connection)
        row = connection.execute(
            """SELECT content_pack_filename, content_hash, state
               FROM assessment_releases WHERE release_id = ?""",
            (release_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] not in {"prepared", "launched"}
            or not isinstance(row["content_hash"], str)
            or not _CONTENT_HASH.fullmatch(row["content_hash"])
        ):
            raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
        headers = {
            "ETag": f'"{row["content_hash"]}"',
            "Cache-Control": "private, immutable",
        }
        if _etag_matches(request.headers.get("If-None-Match", ""), row["content_hash"]):
            return Response(status_code=304, headers=headers)
        snapshot, byte_size = _verified_pack_snapshot(
            connection,
            config,
            release_id=release_id,
            filename=row["content_pack_filename"],
            expected_hash=row["content_hash"],
        )
        headers["Content-Length"] = str(byte_size)
        try:
            return _PackSnapshotResponse(
                snapshot,
                media_type="application/octet-stream",
                headers=headers,
            )
        except Exception:
            snapshot.close()
            raise
    except AuthenticationProblem as error:
        _raise_http(error)
    except AttemptProblem as error:
        _raise_attempt_http(error)
    finally:
        connection.close()


@router.get("/assessments")
async def launched_assessments(request: Request) -> dict[str, Any]:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        device_id = await _verified_device(request, connection)
        student_id = _verified_student(request, config, device_id)
        return {
            "assessments": list_launched_assessments(
                connection,
                student_id=student_id,
                device_id=device_id,
                now_utc=utc_now(),
            )
        }
    except AuthenticationProblem as error:
        _raise_http(error)
    except AttemptProblem as error:
        _raise_attempt_http(error)
    finally:
        connection.close()


@router.post("/attempts/start", response_model=AttemptStartResponse)
async def start_attempt(payload: AttemptStartRequest, request: Request) -> AttemptStartResponse:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        device_id = await _verified_device(request, connection)
        student_id = _verified_student(request, config, device_id)
        preflight_attempt_start(connection, payload.release_id)
        existing_attempt = connection.execute(
            """SELECT 1 FROM attempts
               WHERE release_id = ? AND student_id = ? LIMIT 1""",
            (payload.release_id, student_id),
        ).fetchone()
        if existing_attempt is None:
            _assert_encrypted_pack_ready(connection, config, payload.release_id)
        return issue_attempt_ticket(
            connection,
            release_id=payload.release_id,
            student_id=student_id,
            device_id=device_id,
            confirmed_content_hash=payload.confirmed_content_hash,
            signing_private_key_b64=config.signing_private_key_b64,
            pack_master_key=config.pack_master_key,
            now_utc=utc_now(),
        )
    except AuthenticationProblem as error:
        _raise_http(error)
    except AttemptProblem as error:
        _raise_attempt_http(error)
    finally:
        connection.close()


@router.post("/submissions", response_model=SubmissionReceipt)
async def submit_assessment(
    payload: SignedResponseBundle, request: Request
) -> SubmissionReceipt:
    config = _config(request)
    connection = connect_sqlite(config.db_path)
    try:
        device_id = await _verified_device(request, connection)
        if device_id != payload.bundle.ticket.ticket.device_id:
            raise SubmissionProblem(
                "device_identity_mismatch",
                "The request device does not match the attempt ticket.",
                status_code=403,
            )
        scored_or_receipt = validate_and_score(
            connection,
            payload,
            coordinator_public_key_b64=config.signing_public_key_b64,
            received_at=utc_now(),
        )
        if isinstance(scored_or_receipt, SubmissionReceipt):
            return scored_or_receipt
        if config.submission_writer is None:
            raise SubmissionProblem(
                "submission_writer_not_running",
                "The submission service is not running.",
                status_code=503,
                retryable=True,
            )
        return await run_in_threadpool(config.submission_writer.submit, scored_or_receipt)
    except AuthenticationProblem as error:
        _raise_http(error)
    except SubmissionProblem as error:
        _raise_submission_http(error)
    finally:
        connection.close()
