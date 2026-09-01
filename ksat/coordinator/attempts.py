"""Pre-staged release discovery and atomic per-student attempt issuance."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ksat.coordinator.releases import unwrap_release_content_key
from ksat.crypto import sign_json
from ksat.protocol import (
    PACK_FORMAT_VERSION,
    AttemptStartResponse,
    AttemptTicket,
    SignedAttemptTicket,
    canonical_json,
)


class AttemptProblem(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": False}


def _problem(code: str, message: str, *, status_code: int = 409) -> AttemptProblem:
    return AttemptProblem(code, message, status_code=status_code)


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Coordinator time must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise _problem("content_not_ready", "Assessment content is not ready.") from error
    if parsed.tzinfo is None:
        raise _problem("content_not_ready", "Assessment content is not ready.")
    return parsed.astimezone(timezone.utc)


def list_prefetchable_releases(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT release_id, content_pack_filename, content_hash,
                  content_signature_b64, manifest_json
           FROM assessment_releases
           WHERE state IN ('prepared', 'launched')
           ORDER BY created_at, release_id"""
    ).fetchall()
    releases: list[dict[str, Any]] = []
    for row in rows:
        try:
            manifest = json.loads(row["manifest_json"])
            pack_format_version = int(manifest["pack_format_version"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _problem("content_not_ready", "Assessment content is not ready.") from error
        if pack_format_version != PACK_FORMAT_VERSION:
            raise _problem("content_not_ready", "Assessment content is not ready.")
        releases.append({
            "release_id": row["release_id"],
            "filename": row["content_pack_filename"],
            "content_hash": row["content_hash"],
            "pack_signature_b64": row["content_signature_b64"],
            "byte_size": None,
            "pack_format_version": pack_format_version,
        })
    return releases


def list_launched_assessments(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    device_id: str,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    now_utc = _as_utc(now_utc)
    rows = connection.execute(
        """SELECT r.release_id, r.test_id, t.test_name, r.duration_seconds,
                  r.content_hash, r.launch_opens_at, r.launch_closes_at,
                  t.launched,
                  a.attempt_id, a.device_id AS attempt_device_id,
                  a.status AS attempt_status, a.expires_at
           FROM assessment_releases r
           JOIN tests t ON t.test_id = r.test_id
           LEFT JOIN attempts a
             ON a.release_id = r.release_id AND a.student_id = ?
           WHERE r.state = 'launched'
             AND NOT EXISTS (
                 SELECT 1 FROM attempts submitted
                 WHERE submitted.release_id = r.release_id
                   AND submitted.student_id = ?
                   AND submitted.status = 'submitted'
             )
           ORDER BY r.launch_opens_at, r.release_id""",
        (student_id, student_id),
    ).fetchall()
    assessments: list[dict[str, Any]] = []
    for row in rows:
        opens = _parse_time(row["launch_opens_at"])
        closes = _parse_time(row["launch_closes_at"])
        deadline = _parse_time(row["expires_at"])
        can_start = bool(row["launched"] and opens and closes and opens <= now_utc <= closes)
        can_resume = bool(
            row["attempt_id"]
            and row["attempt_status"] == "in_progress"
            and row["attempt_device_id"] == device_id
            and deadline
            and now_utc <= deadline
        )
        if not (can_start or can_resume):
            continue
        assessments.append({
            "release_id": row["release_id"],
            "test_id": row["test_id"],
            "test_name": row["test_name"],
            "duration_seconds": row["duration_seconds"],
            "content_hash": row["content_hash"],
            "launch_closes_at": closes,
            "attempt_id": row["attempt_id"],
            "attempt_deadline": deadline,
        })
    return assessments


def _existing_attempt(
    connection: sqlite3.Connection, release_id: str, student_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM attempts
           WHERE release_id = ? AND student_id = ?
           ORDER BY started_at, attempt_id LIMIT 1""",
        (release_id, student_id),
    ).fetchone()


def _stored_start_response(
    connection: sqlite3.Connection,
    *,
    attempt: sqlite3.Row,
    release_id: str,
    student_id: str,
    device_id: str,
    now_utc: datetime,
) -> AttemptStartResponse:
    if attempt["status"] == "submitted":
        raise _problem("already_submitted", "This assessment was already submitted.")
    if attempt["device_id"] != device_id:
        raise _problem(
            "attempt_bound_to_other_device",
            "This attempt can resume only on the computer where it started.",
        )
    deadline = _parse_time(attempt["expires_at"])
    if attempt["status"] != "in_progress" or deadline is None or now_utc > deadline:
        raise _problem("start_window_closed", "The assessment start window is closed.")
    try:
        signed = SignedAttemptTicket.model_validate_json(attempt["ticket_json"])
    except Exception as error:
        raise _problem("content_not_ready", "The stored attempt ticket is invalid.") from error
    if (
        signed.ticket.attempt_id != attempt["attempt_id"]
        or signed.ticket.release_id != release_id
        or signed.ticket.student_id != student_id
        or signed.ticket.device_id != device_id
        or signed.ticket.deadline != deadline
    ):
        raise _problem("content_not_ready", "The stored attempt ticket is invalid.")
    question_ids = [
        row["question_id"]
        for row in connection.execute(
            """SELECT question_id FROM release_questions
               WHERE release_id = ? ORDER BY canonical_order""",
            (release_id,),
        ).fetchall()
    ]
    return AttemptStartResponse(
        ticket=signed,
        canonical_question_ids=question_ids,
        server_time=now_utc,
    )


def issue_attempt_ticket(
    connection: sqlite3.Connection,
    *,
    release_id: str,
    student_id: str,
    device_id: str,
    confirmed_content_hash: str,
    signing_private_key_b64: str,
    pack_master_key: bytes,
    now_utc: datetime,
) -> AttemptStartResponse:
    """Atomically issue one immutable attempt ticket, or return its exact stored value."""

    now_utc = _as_utc(now_utc)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        device = connection.execute(
            "SELECT status FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if device is None or device["status"] != "active":
            raise _problem("device_inactive", "The device is not active.", status_code=403)
        if connection.execute(
            "SELECT 1 FROM students WHERE student_id = ?", (student_id,)
        ).fetchone() is None:
            raise _problem("invalid_client_session", "The client session is invalid.", status_code=401)
        release = connection.execute(
            """SELECT r.*, t.launched AS test_launched
               FROM assessment_releases r
               JOIN tests t ON t.test_id = r.test_id
               WHERE r.release_id = ?""",
            (release_id,),
        ).fetchone()
        if release is None or release["state"] not in {"prepared", "launched"}:
            raise _problem("content_not_ready", "Assessment content is not ready.")
        if confirmed_content_hash != release["content_hash"]:
            raise _problem("content_hash_mismatch", "The cached assessment content does not match.")

        existing = _existing_attempt(connection, release_id, student_id)
        if existing is not None:
            response = _stored_start_response(
                connection,
                attempt=existing,
                release_id=release_id,
                student_id=student_id,
                device_id=device_id,
                now_utc=now_utc,
            )
            if owns_transaction:
                connection.commit()
            return response

        if connection.execute(
            """SELECT 1 FROM submissions s JOIN attempts a ON a.attempt_id = s.attempt_id
               WHERE a.release_id = ? AND a.student_id = ? LIMIT 1""",
            (release_id, student_id),
        ).fetchone():
            raise _problem("already_submitted", "This assessment was already submitted.")
        if release["state"] != "launched" or not release["test_launched"]:
            raise _problem("assessment_not_launched", "This assessment has not been launched.")
        opens = _parse_time(release["launch_opens_at"])
        closes = _parse_time(release["launch_closes_at"])
        if opens is None or closes is None:
            raise _problem("assessment_not_launched", "This assessment has not been launched.")
        if now_utc < opens or now_utc > closes:
            raise _problem("start_window_closed", "The assessment start window is closed.")

        question_ids = [
            row["question_id"]
            for row in connection.execute(
                """SELECT question_id FROM release_questions
                   WHERE release_id = ? ORDER BY canonical_order""",
                (release_id,),
            ).fetchall()
        ]
        if not question_ids:
            raise _problem("content_not_ready", "Assessment content is not ready.")
        try:
            content_key = unwrap_release_content_key(
                pack_master_key, release_id, release["wrapped_content_key_b64"]
            )
        except ValueError as error:
            raise _problem("content_not_ready", "Assessment content is not ready.") from error
        attempt_id = str(uuid.uuid4())
        order_seed_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
        deadline = now_utc + timedelta(seconds=release["duration_seconds"])
        ticket = AttemptTicket(
            attempt_id=attempt_id,
            student_id=student_id,
            device_id=device_id,
            release_id=release_id,
            content_hash=release["content_hash"],
            started_at=now_utc,
            deadline=deadline,
            order_seed_b64=order_seed_b64,
            content_key_b64=base64.b64encode(content_key).decode("ascii"),
        )
        signed = SignedAttemptTicket(
            ticket=ticket,
            signature_b64=sign_json(signing_private_key_b64, ticket),
        )
        ticket_json = canonical_json(signed).decode("utf-8")
        connection.execute(
            """INSERT INTO attempts
               (attempt_id, student_id, test_id, release_id, device_id, order_seed,
                ticket_json, started_at, status, total_questions, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)""",
            (
                attempt_id,
                student_id,
                release["test_id"],
                release_id,
                device_id,
                order_seed_b64,
                ticket_json,
                now_utc.isoformat(timespec="seconds"),
                len(question_ids),
                deadline.isoformat(timespec="seconds"),
            ),
        )
        response = AttemptStartResponse(
            ticket=signed,
            canonical_question_ids=question_ids,
            server_time=now_utc,
        )
        if owns_transaction:
            connection.commit()
        return response
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise
