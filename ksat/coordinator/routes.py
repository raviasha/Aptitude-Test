"""Installed-client coordinator API routes."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

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
)
from ksat.coordinator.releases import load_release_manifest
from ksat.protocol import (
    AttemptStartResponse,
    ClientLoginRequest,
    ClientSession,
    DeviceEnrollmentReceipt,
    DeviceEnrollmentRequest,
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
    root = (config.data_dir / "Assessment Releases").resolve()
    try:
        candidate = (root / filename).resolve(strict=True)
    except OSError as error:
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.") from error
    if Path(filename).name != filename or not candidate.is_relative_to(root) or not candidate.is_file():
        raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
    return candidate


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
            try:
                load_release_manifest(
                    connection,
                    item["release_id"],
                    pack_dir=config.data_dir / "Assessment Releases",
                    signing_public_key_b64=config.signing_public_key_b64,
                    pack_master_key=config.pack_master_key,
                )
                item["byte_size"] = _pack_path(config, item["filename"]).stat().st_size
            except (KeyError, OSError, ValueError) as error:
                raise AttemptProblem(
                    "content_not_ready", "Assessment content is not ready."
                ) from error
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
            """SELECT content_pack_filename, content_hash
               FROM assessment_releases WHERE release_id = ?""",
            (release_id,),
        ).fetchone()
        if row is None:
            raise AttemptProblem("content_not_ready", "Assessment content is not ready.")
        try:
            load_release_manifest(
                connection,
                release_id,
                pack_dir=config.data_dir / "Assessment Releases",
                signing_public_key_b64=config.signing_public_key_b64,
                pack_master_key=config.pack_master_key,
            )
            path = _pack_path(config, row["content_pack_filename"])
        except (KeyError, OSError, ValueError) as error:
            raise AttemptProblem("content_not_ready", "Assessment content is not ready.") from error
        headers = {
            "ETag": f'"{row["content_hash"]}"',
            "Cache-Control": "private, immutable",
        }
        if _etag_matches(request.headers.get("If-None-Match", ""), row["content_hash"]):
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
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
