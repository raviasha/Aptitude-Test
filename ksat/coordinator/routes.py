"""Installed-client coordinator API routes."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bcrypt
from fastapi import APIRouter, HTTPException, Request

from ksat.coordinator.auth import (
    CLIENT_SESSION_SECONDS,
    AuthenticationProblem,
    issue_student_access_token,
    register_device,
    verify_device_request,
)
from ksat.protocol import ClientLoginRequest, ClientSession, DeviceEnrollmentReceipt, DeviceEnrollmentRequest
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


def _config(request: Request) -> CoordinatorConfig:
    return request.app.state.coordinator_config


def _raise_http(error: AuthenticationProblem) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail()) from error


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
