"""Transactional SQLite persistence for the managed lab client."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from pydantic import BaseModel, ValidationError

from ksat.protocol import (
    IntegrityEvent,
    SignedAttemptTicket,
    SignedResponseBundle,
    SubmissionReceipt,
    canonical_json,
)
from ksat.sqlite import connect_sqlite


_T = TypeVar("_T", bound=BaseModel)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OPTION = re.compile(r"[A-E]\Z")
_ACTIVE_STATES = ("in_progress", "sealed_pending")


class AttemptSealedError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalAttemptRecord:
    attempt_id: str
    release_id: str
    state: str
    deadline: datetime
    question_order: tuple[int, ...]
    responses: dict[int, str | None]
    remaining_seconds: int
    sealed_at: datetime | None
    receipt: SubmissionReceipt | None
    ticket: SignedAttemptTicket
    last_wall_time: datetime


@dataclass(frozen=True)
class PendingSubmission:
    attempt_id: str
    bundle: SignedResponseBundle
    retry_count: int
    next_attempt_at: datetime
    last_error: str | None


@dataclass(frozen=True)
class _ValidatedAttemptSnapshot:
    record: LocalAttemptRecord
    sealed_bundle: SignedResponseBundle | None
    receipt_json: str | None
    outbox_json: str | None


def _aware(value: datetime, label: str = "Timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value


def _iso(value: datetime, label: str = "Timestamp") -> str:
    return _aware(value, label).astimezone(timezone.utc).isoformat()


def _parse_time(value: object, message: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(message)
    try:
        parsed = datetime.fromisoformat(value)
        return _aware(parsed).astimezone(timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid.")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} is invalid.") from error
    if str(parsed) != value:
        raise ValueError(f"{label} is invalid.")
    return value


def _b64(value: object, length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid.")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError(f"{label} is invalid.") from error
    if len(decoded) != length or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"{label} is invalid.")
    return decoded


def _validate_finite_and_aware(value: object) -> None:
    if isinstance(value, datetime):
        _aware(value)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Persisted numeric values must be finite.")
    elif isinstance(value, dict):
        for nested in value.values():
            _validate_finite_and_aware(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_finite_and_aware(nested)
    elif isinstance(value, BaseModel):
        _validate_finite_and_aware(value.model_dump())


def _duplicates_rejected(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _strict_json_object(raw: str, message: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_duplicates_rejected,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON.")),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(message) from error
    if not isinstance(value, dict):
        raise ValueError(message)
    if canonical_json(value).decode("utf-8") != raw:
        raise ValueError(message)
    return value


def _model_json(value: _T, expected: type[_T]) -> tuple[_T, str]:
    if not isinstance(value, expected):
        raise ValueError(f"{expected.__name__} is invalid.")
    _validate_finite_and_aware(value)
    raw = canonical_json(value).decode("utf-8")
    _strict_json_object(raw, f"{expected.__name__} is invalid.")
    try:
        validated = expected.model_validate_json(raw, strict=True)
    except ValidationError as error:
        raise ValueError(f"{expected.__name__} is invalid.") from error
    return validated, raw


def _stored_model(raw: object, expected: type[_T], message: str) -> _T:
    if not isinstance(raw, str):
        raise ValueError(message)
    _strict_json_object(raw, message)
    try:
        value = expected.model_validate_json(raw, strict=True)
        _validate_finite_and_aware(value)
        return value
    except (ValidationError, ValueError) as error:
        raise ValueError(message) from error


def _validate_ticket(ticket: SignedAttemptTicket) -> tuple[SignedAttemptTicket, str]:
    ticket, raw = _model_json(ticket, SignedAttemptTicket)
    value = ticket.ticket
    _uuid(value.attempt_id, "Attempt identifier")
    _uuid(value.device_id, "Device identifier")
    _uuid(value.release_id, "Release identifier")
    if not isinstance(value.student_id, str) or not value.student_id.strip():
        raise ValueError("Student identifier is invalid.")
    if not _HASH.fullmatch(value.content_hash):
        raise ValueError("Content hash is invalid.")
    _aware(value.started_at, "Attempt start")
    _aware(value.deadline, "Attempt deadline")
    if value.deadline <= value.started_at:
        raise ValueError("Attempt deadline must follow its start.")
    _b64(value.order_seed_b64, 32, "Question-order seed")
    _b64(value.content_key_b64, 32, "Content key")
    _b64(ticket.signature_b64, 64, "Attempt ticket signature")
    return ticket, raw


def _validate_receipt(receipt: SubmissionReceipt) -> tuple[SubmissionReceipt, str]:
    receipt, raw = _model_json(receipt, SubmissionReceipt)
    _uuid(receipt.attempt_id, "Attempt identifier")
    _aware(receipt.accepted_at, "Receipt acceptance time")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (receipt.score, receipt.total_questions, receipt.attempted, receipt.violations)
    ):
        raise ValueError("Submission receipt counts are invalid.")
    if not (receipt.score <= receipt.attempted <= receipt.total_questions):
        raise ValueError("Submission receipt counts are invalid.")
    if not math.isfinite(receipt.percentage):
        raise ValueError("Persisted numeric values must be finite.")
    expected_percentage = (
        round(receipt.score / receipt.total_questions * 100, 1)
        if receipt.total_questions
        else 0.0
    )
    if not 0.0 <= receipt.percentage <= 100.0 or receipt.percentage != expected_percentage:
        raise ValueError("Submission receipt percentage is invalid.")
    return receipt, raw


def _validate_bundle(bundle: SignedResponseBundle) -> tuple[SignedResponseBundle, str]:
    bundle, raw = _model_json(bundle, SignedResponseBundle)
    _validate_ticket(bundle.bundle.ticket)
    _uuid(bundle.bundle.ticket.ticket.attempt_id, "Attempt identifier")
    if not _HASH.fullmatch(bundle.bundle.content_hash):
        raise ValueError("Content hash is invalid.")
    _aware(bundle.bundle.sealed_at, "Seal time")
    signature = _b64(bundle.device_signature_b64, 64, "Device signature")
    if not any(signature):
        raise ValueError("Device signature is invalid.")
    seen = set()
    for response in bundle.bundle.responses:
        if (
            not isinstance(response.question_id, int)
            or isinstance(response.question_id, bool)
            or response.question_id <= 0
            or response.question_id in seen
        ):
            raise ValueError("Bundle responses are invalid.")
        seen.add(response.question_id)
        if response.selected_answer is not None and not _OPTION.fullmatch(response.selected_answer):
            raise ValueError("Bundle responses are invalid.")
    for event in bundle.bundle.integrity_events:
        if not event.event_type.strip():
            raise ValueError("Integrity event is invalid.")
        _aware(event.occurred_at, "Integrity event time")
    return bundle, raw


def _validate_receipt_for_sealed_attempt(
    receipt: SubmissionReceipt,
    question_order: tuple[int, ...],
    bundle: SignedResponseBundle,
    message: str,
) -> None:
    if (
        receipt.total_questions != len(question_order)
        or receipt.attempted
        != sum(item.selected_answer is not None for item in bundle.bundle.responses)
        or receipt.violations != len(bundle.bundle.integrity_events)
    ):
        raise ValueError(message)


class ClientStore:
    def __init__(
        self,
        database_path: Path,
        *,
        connection_factory: Callable[[Path], sqlite3.Connection] = connect_sqlite,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = connection_factory(self.database_path)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._migrate()
        except BaseException:
            self.connection.close()
            self._closed = True
            raise

    def _migrate(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cached_content_packs (
                  release_id TEXT PRIMARY KEY,
                  content_hash TEXT NOT NULL,
                  pack_path TEXT NOT NULL,
                  verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
                  cached_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_attempts (
                  attempt_id TEXT PRIMARY KEY,
                  student_id TEXT NOT NULL,
                  release_id TEXT NOT NULL,
                  ticket_json TEXT NOT NULL,
                  question_order_json TEXT NOT NULL,
                  state TEXT NOT NULL CHECK (state IN ('in_progress', 'sealed_pending', 'acknowledged')),
                  deadline TEXT NOT NULL,
                  remaining_seconds INTEGER NOT NULL CHECK (remaining_seconds >= 0),
                  last_wall_time TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  sealed_at TEXT,
                  sealed_bundle_json TEXT,
                  receipt_json TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_student_release
                  ON local_attempts(student_id, release_id)
                  WHERE state IN ('in_progress', 'sealed_pending');
                CREATE TABLE IF NOT EXISTS local_responses (
                  attempt_id TEXT NOT NULL REFERENCES local_attempts(attempt_id) ON DELETE CASCADE,
                  question_id INTEGER NOT NULL,
                  selected_answer TEXT,
                  saved_at TEXT NOT NULL,
                  PRIMARY KEY(attempt_id, question_id)
                );
                CREATE TABLE IF NOT EXISTS local_integrity_events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  attempt_id TEXT NOT NULL REFERENCES local_attempts(attempt_id) ON DELETE CASCADE,
                  event_type TEXT NOT NULL,
                  occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submission_outbox (
                  attempt_id TEXT PRIMARY KEY REFERENCES local_attempts(attempt_id) ON DELETE CASCADE,
                  bundle_json TEXT NOT NULL,
                  retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
                  next_attempt_at TEXT NOT NULL,
                  last_error TEXT,
                  created_at TEXT NOT NULL
                );
                """
            )
            self.connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Client store is closed.")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self.connection.execute("BEGIN")
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self.connection.in_transaction:
                    self.connection.rollback()
                self.connection.close()
                self._closed = True

    def __enter__(self) -> ClientStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def cache_pack(
        self,
        release_id: str,
        content_hash: str,
        pack_path: Path,
        *,
        verified: bool,
        cached_at: datetime | None = None,
    ) -> None:
        _uuid(release_id, "Release identifier")
        if not _HASH.fullmatch(content_hash):
            raise ValueError("Content hash is invalid.")
        if type(verified) is not bool:
            raise ValueError("Content verification state is invalid.")
        normalized_path = str(Path(pack_path).resolve())
        timestamp = _iso(cached_at or datetime.now(timezone.utc), "Cache time")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT content_hash, pack_path, verified FROM cached_content_packs WHERE release_id=?",
                (release_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO cached_content_packs VALUES (?, ?, ?, ?, ?)",
                    (release_id, content_hash, normalized_path, int(verified), timestamp),
                )
            elif existing["content_hash"] != content_hash or existing["pack_path"] != normalized_path:
                raise ValueError("Cached content pack conflicts with the existing release record.")
            elif verified and not existing["verified"]:
                connection.execute(
                    "UPDATE cached_content_packs SET verified=1, cached_at=? WHERE release_id=?",
                    (timestamp, release_id),
                )

    def verified_pack(
        self,
        release_id: str,
        content_hash: str | None = None,
        pack_path: Path | None = None,
    ) -> Path | None:
        _uuid(release_id, "Release identifier")
        with self._lock:
            self._ensure_open()
            row = self.connection.execute(
                """SELECT content_hash, pack_path, verified, cached_at
                   FROM cached_content_packs WHERE release_id=?""",
                (release_id,),
            ).fetchone()
        if row is None or row["verified"] != 1:
            return None
        message = "Stored content pack record is invalid."
        try:
            if (
                not isinstance(row["content_hash"], str)
                or not _HASH.fullmatch(row["content_hash"])
                or not isinstance(row["pack_path"], str)
                or not row["pack_path"]
                or not Path(row["pack_path"]).is_absolute()
                or type(row["verified"]) is not int
            ):
                raise ValueError(message)
            _parse_time(row["cached_at"], message)
        except (TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error) == message:
                raise
            raise ValueError(message) from error
        if content_hash is not None and row["content_hash"] != content_hash:
            return None
        if pack_path is not None and row["pack_path"] != str(Path(pack_path).resolve()):
            return None
        return Path(row["pack_path"])

    def create_attempt(
        self,
        ticket: SignedAttemptTicket,
        question_order: list[int] | tuple[int, ...],
        *,
        remaining_seconds: int | None = None,
        created_at: datetime | None = None,
        last_wall_time: datetime | None = None,
    ) -> LocalAttemptRecord:
        ticket, ticket_json = _validate_ticket(ticket)
        if (
            not isinstance(question_order, (list, tuple))
            or not question_order
            or any(type(item) is not int or item <= 0 for item in question_order)
            or len(set(question_order)) != len(question_order)
        ):
            raise ValueError("Question order is invalid.")
        order_json = canonical_json({"order": list(question_order)}).decode("utf-8")
        attempt = ticket.ticket
        default_remaining = max(0, int((attempt.deadline - attempt.started_at).total_seconds()))
        remaining = default_remaining if remaining_seconds is None else remaining_seconds
        if type(remaining) is not int or remaining < 0:
            raise ValueError("Remaining time is invalid.")
        created = created_at or attempt.started_at
        wall = last_wall_time or created
        created_iso = _iso(created, "Attempt creation time")
        wall_iso = _iso(wall, "Wall checkpoint")
        deadline_iso = _iso(attempt.deadline, "Attempt deadline")
        with self._transaction() as connection:
            cached = connection.execute(
                """SELECT 1 FROM cached_content_packs
                   WHERE release_id=? AND content_hash=? AND verified=1""",
                (attempt.release_id, attempt.content_hash),
            ).fetchone()
            if cached is None:
                raise ValueError("Attempt requires the exact verified content pack.")
            existing = connection.execute(
                """SELECT ticket_json, question_order_json, deadline,
                          remaining_seconds, last_wall_time, created_at
                   FROM local_attempts WHERE attempt_id=?""",
                (attempt.attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["ticket_json"] != ticket_json
                    or existing["question_order_json"] != order_json
                    or existing["deadline"] != deadline_iso
                    or existing["remaining_seconds"] != remaining
                    or existing["last_wall_time"] != wall_iso
                    or existing["created_at"] != created_iso
                ):
                    raise ValueError(
                        "Attempt identifier conflicts with the stored creation parameters."
                    )
            else:
                active = connection.execute(
                    """SELECT attempt_id FROM local_attempts
                       WHERE state IN ('in_progress', 'sealed_pending')
                       ORDER BY created_at, attempt_id LIMIT 1"""
                ).fetchone()
                if active is not None:
                    raise ValueError(
                        "Another local assessment attempt is already active."
                    )
                try:
                    connection.execute(
                        """INSERT INTO local_attempts
                           (attempt_id, student_id, release_id, ticket_json, question_order_json,
                            state, deadline, remaining_seconds, last_wall_time, created_at)
                           VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?, ?, ?)""",
                        (
                            attempt.attempt_id,
                            attempt.student_id,
                            attempt.release_id,
                            ticket_json,
                            order_json,
                            deadline_iso,
                            remaining,
                            wall_iso,
                            created_iso,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValueError(
                        "Another local assessment attempt is already active."
                    ) from error
        return self.load_attempt(attempt.attempt_id)

    @staticmethod
    def _question_order(raw: object, message: str) -> tuple[int, ...]:
        if not isinstance(raw, str):
            raise ValueError(message)
        value = _strict_json_object(raw, message)
        order = value.get("order") if set(value) == {"order"} else None
        if (
            not isinstance(order, list)
            or not order
            or any(type(item) is not int or item <= 0 for item in order)
            or len(set(order)) != len(order)
        ):
            raise ValueError(message)
        return tuple(order)

    def _validated_attempt_snapshot(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        message: str,
    ) -> _ValidatedAttemptSnapshot:
        row = connection.execute(
            "SELECT * FROM local_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown local attempt: {attempt_id}")
        responses = connection.execute(
            """SELECT question_id, selected_answer, saved_at
               FROM local_responses WHERE attempt_id=?""",
            (attempt_id,),
        ).fetchall()
        event_rows = connection.execute(
            """SELECT event_type, occurred_at FROM local_integrity_events
               WHERE attempt_id=? ORDER BY event_id""",
            (attempt_id,),
        ).fetchall()
        outbox = connection.execute(
            "SELECT bundle_json FROM submission_outbox WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        outbox_json = None if outbox is None else outbox["bundle_json"]
        try:
            ticket = _stored_model(row["ticket_json"], SignedAttemptTicket, message)
            _validate_ticket(ticket)
            order = self._question_order(row["question_order_json"], message)
            deadline = _parse_time(row["deadline"], message)
            last_wall = _parse_time(row["last_wall_time"], message)
            _parse_time(row["created_at"], message)
            sealed_at = None if row["sealed_at"] is None else _parse_time(row["sealed_at"], message)
            sealed_bundle = None
            if row["sealed_bundle_json"] is not None:
                sealed_bundle = _stored_model(
                    row["sealed_bundle_json"], SignedResponseBundle, message
                )
                _validate_bundle(sealed_bundle)
            receipt = None
            if row["receipt_json"] is not None:
                receipt = _stored_model(row["receipt_json"], SubmissionReceipt, message)
                _validate_receipt(receipt)
            if (
                row["attempt_id"] != ticket.ticket.attempt_id
                or row["release_id"] != ticket.ticket.release_id
                or row["student_id"] != ticket.ticket.student_id
                or deadline != ticket.ticket.deadline.astimezone(timezone.utc)
                or row["state"] not in (*_ACTIVE_STATES, "acknowledged")
                or type(row["remaining_seconds"]) is not int
                or row["remaining_seconds"] < 0
                or (row["state"] == "in_progress" and sealed_at is not None)
                or (row["state"] != "in_progress" and sealed_at is None)
                or (row["state"] == "in_progress") != (sealed_bundle is None)
                or (
                    sealed_bundle is not None
                    and (
                        sealed_bundle.bundle.ticket != ticket
                        or sealed_bundle.bundle.ticket.ticket.attempt_id != row["attempt_id"]
                        or sealed_bundle.bundle.sealed_at != sealed_at
                    )
                )
                or (row["state"] == "acknowledged") != (receipt is not None)
                or (row["state"] == "sealed_pending") != (outbox is not None)
                or (
                    outbox is not None
                    and row["sealed_bundle_json"] != outbox_json
                )
            ):
                raise ValueError(message)
            response_map = {question_id: None for question_id in order}
            for response in responses:
                question_id = response["question_id"]
                selected = response["selected_answer"]
                if question_id not in response_map or (
                    selected is not None and not _OPTION.fullmatch(selected)
                ):
                    raise ValueError(message)
                _parse_time(response["saved_at"], message)
                response_map[question_id] = selected
            stored_events = []
            for event_row in event_rows:
                if (
                    not isinstance(event_row["event_type"], str)
                    or not event_row["event_type"].strip()
                    or len(event_row["event_type"]) > 200
                ):
                    raise ValueError(message)
                stored_events.append(
                    IntegrityEvent(
                        event_type=event_row["event_type"],
                        occurred_at=_parse_time(event_row["occurred_at"], message),
                    )
                )
            if sealed_bundle is not None and (
                sealed_bundle.bundle.content_hash != ticket.ticket.content_hash
                or [item.question_id for item in sealed_bundle.bundle.responses] != list(order)
                or {
                    item.question_id: item.selected_answer
                    for item in sealed_bundle.bundle.responses
                }
                != response_map
                or sealed_bundle.bundle.integrity_events != stored_events
                or sealed_at is None
                or sealed_at < ticket.ticket.started_at
                or (
                    receipt is not None
                    and receipt.accepted_at < sealed_at
                )
            ):
                raise ValueError(message)
            if receipt is not None:
                _validate_receipt_for_sealed_attempt(
                    receipt, order, sealed_bundle, message
                )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error) == message:
                raise
            raise ValueError(message) from error
        return _ValidatedAttemptSnapshot(
            record=LocalAttemptRecord(
                attempt_id=row["attempt_id"],
                release_id=row["release_id"],
                state=row["state"],
                deadline=deadline,
                question_order=order,
                responses=response_map,
                remaining_seconds=row["remaining_seconds"],
                sealed_at=sealed_at,
                receipt=receipt,
                ticket=ticket,
                last_wall_time=last_wall,
            ),
            sealed_bundle=sealed_bundle,
            receipt_json=row["receipt_json"],
            outbox_json=outbox_json,
        )

    def load_attempt(self, attempt_id: str) -> LocalAttemptRecord:
        with self._read_transaction() as connection:
            snapshot = self._validated_attempt_snapshot(
                connection, attempt_id, "Stored attempt data is invalid."
            )
        return snapshot.record

    def active_attempt(
        self, *, student_id: str | None = None, release_id: str | None = None
    ) -> LocalAttemptRecord | None:
        clauses = ["state IN ('in_progress', 'sealed_pending')"]
        values: list[str] = []
        if student_id is not None:
            clauses.append("student_id=?")
            values.append(student_id)
        if release_id is not None:
            clauses.append("release_id=?")
            values.append(release_id)
        with self._lock:
            self._ensure_open()
            rows = self.connection.execute(
                f"SELECT attempt_id FROM local_attempts WHERE {' AND '.join(clauses)} ORDER BY created_at, attempt_id",
                values,
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("Multiple active attempts require recovery intervention.")
        return None if not rows else self.load_attempt(rows[0]["attempt_id"])

    @staticmethod
    def _editable(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT state, question_order_json FROM local_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown local attempt: {attempt_id}")
        if row["state"] != "in_progress":
            raise AttemptSealedError("The attempt is sealed and cannot be changed.")
        return row

    def save_answer(
        self, attempt_id: str, question_id: int, selected_answer: str | None, *, saved_at: datetime
    ) -> None:
        if type(question_id) is not int or question_id <= 0:
            raise ValueError("Question identifier is invalid.")
        if selected_answer is not None and (
            not isinstance(selected_answer, str) or not _OPTION.fullmatch(selected_answer)
        ):
            raise ValueError("Selected answer is invalid.")
        saved_iso = _iso(saved_at, "Answer save time")
        with self._transaction() as connection:
            row = self._editable(connection, attempt_id)
            if question_id not in self._question_order(
                row["question_order_json"], "Stored attempt data is invalid."
            ):
                raise ValueError("Question is not part of the local attempt.")
            connection.execute(
                """INSERT INTO local_responses
                   (attempt_id, question_id, selected_answer, saved_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(attempt_id, question_id) DO UPDATE SET
                     selected_answer=excluded.selected_answer, saved_at=excluded.saved_at""",
                (attempt_id, question_id, selected_answer, saved_iso),
            )

    def record_integrity_event(
        self, attempt_id: str, event_type: str, *, occurred_at: datetime
    ) -> int:
        if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 200:
            raise ValueError("Integrity event type is invalid.")
        occurred_iso = _iso(occurred_at, "Integrity event time")
        with self._transaction() as connection:
            self._editable(connection, attempt_id)
            cursor = connection.execute(
                "INSERT INTO local_integrity_events (attempt_id, event_type, occurred_at) VALUES (?, ?, ?)",
                (attempt_id, event_type, occurred_iso),
            )
            return int(cursor.lastrowid)

    def integrity_events(self, attempt_id: str) -> tuple[IntegrityEvent, ...]:
        with self._read_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM local_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Unknown local attempt: {attempt_id}")
            rows = connection.execute(
                """SELECT event_type, occurred_at FROM local_integrity_events
                   WHERE attempt_id=? ORDER BY event_id""",
                (attempt_id,),
            ).fetchall()
        try:
            return tuple(
                IntegrityEvent(
                    event_type=row["event_type"],
                    occurred_at=_parse_time(row["occurred_at"], "Stored integrity event is invalid."),
                )
                for row in rows
            )
        except (ValidationError, ValueError) as error:
            raise ValueError("Stored integrity event is invalid.") from error

    def update_timer_checkpoint(
        self,
        attempt_id: str,
        remaining_seconds: int,
        *,
        last_wall_time: datetime,
    ) -> None:
        if type(remaining_seconds) is not int or remaining_seconds < 0:
            raise ValueError("Remaining time is invalid.")
        wall_iso = _iso(last_wall_time, "Wall checkpoint")
        with self._transaction() as connection:
            row = self._editable(connection, attempt_id)
            current = connection.execute(
                "SELECT remaining_seconds FROM local_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()["remaining_seconds"]
            if remaining_seconds > current:
                raise ValueError("Remaining time cannot increase.")
            connection.execute(
                """UPDATE local_attempts SET remaining_seconds=?, last_wall_time=?
                   WHERE attempt_id=?""",
                (remaining_seconds, wall_iso, attempt_id),
            )

    def seal_attempt(
        self,
        attempt_id: str,
        bundle: SignedResponseBundle,
        *,
        sealed_at: datetime,
    ) -> LocalAttemptRecord:
        sealed_iso = _iso(sealed_at, "Seal time")
        bundle, bundle_json = _validate_bundle(bundle)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM local_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown local attempt: {attempt_id}")
            if row["state"] != "in_progress":
                if row["sealed_at"] == sealed_iso and row["sealed_bundle_json"] == bundle_json:
                    pass
                else:
                    raise ValueError("Sealed attempt conflicts with the stored submission bundle.")
            else:
                ticket = _stored_model(
                    row["ticket_json"], SignedAttemptTicket, "Stored attempt data is invalid."
                )
                if sealed_at < ticket.ticket.started_at:
                    raise ValueError("Seal time cannot precede the attempt start.")
                order = self._question_order(
                    row["question_order_json"], "Stored attempt data is invalid."
                )
                saved_rows = connection.execute(
                    "SELECT question_id, selected_answer FROM local_responses WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchall()
                saved = {item: None for item in order}
                for saved_row in saved_rows:
                    if saved_row["question_id"] not in saved:
                        raise ValueError("Stored attempt data is invalid.")
                    saved[saved_row["question_id"]] = saved_row["selected_answer"]
                events = connection.execute(
                    """SELECT event_type, occurred_at FROM local_integrity_events
                       WHERE attempt_id=? ORDER BY event_id""",
                    (attempt_id,),
                ).fetchall()
                expected_events = [
                    IntegrityEvent(event_type=item["event_type"], occurred_at=_parse_time(
                        item["occurred_at"], "Stored integrity event is invalid."
                    ))
                    for item in events
                ]
                if (
                    bundle.bundle.ticket != ticket
                    or bundle.bundle.ticket.ticket.attempt_id != attempt_id
                    or bundle.bundle.content_hash != ticket.ticket.content_hash
                    or bundle.bundle.sealed_at != sealed_at
                    or [item.question_id for item in bundle.bundle.responses] != list(order)
                    or {item.question_id: item.selected_answer for item in bundle.bundle.responses} != saved
                    or bundle.bundle.integrity_events != expected_events
                ):
                    raise ValueError("Submission bundle does not match the stored attempt.")
                connection.execute(
                    """UPDATE local_attempts
                       SET state='sealed_pending', sealed_at=?, sealed_bundle_json=?
                       WHERE attempt_id=?""",
                    (sealed_iso, bundle_json, attempt_id),
                )
                connection.execute(
                    """INSERT INTO submission_outbox
                       (attempt_id, bundle_json, retry_count, next_attempt_at, last_error, created_at)
                       VALUES (?, ?, 0, ?, NULL, ?)""",
                    (attempt_id, bundle_json, sealed_iso, sealed_iso),
                )
        return self.load_attempt(attempt_id)

    def pending_submissions(
        self, *, due_at: datetime | None = None
    ) -> list[PendingSubmission]:
        values: tuple[str, ...] = ()
        where = ""
        if due_at is not None:
            where = "WHERE o.next_attempt_at<=?"
            values = (_iso(due_at, "Outbox due time"),)
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""SELECT o.attempt_id, o.bundle_json, o.retry_count,
                           o.next_attempt_at, o.last_error, o.created_at
                    FROM submission_outbox AS o
                    {where}
                    ORDER BY o.next_attempt_at, o.created_at, o.attempt_id""",
                values,
            ).fetchall()
            pending = []
            for row in rows:
                message = "Stored submission outbox data is invalid."
                try:
                    snapshot = self._validated_attempt_snapshot(
                        connection, row["attempt_id"], message
                    )
                    bundle = snapshot.sealed_bundle
                    next_attempt = _parse_time(row["next_attempt_at"], message)
                    _parse_time(row["created_at"], message)
                    if (
                        snapshot.record.state != "sealed_pending"
                        or bundle is None
                        or snapshot.outbox_json != row["bundle_json"]
                        or type(row["retry_count"]) is not int
                        or row["retry_count"] < 0
                        or (
                            row["last_error"] is not None
                            and (
                                not isinstance(row["last_error"], str)
                                or len(row["last_error"]) > 2000
                            )
                        )
                    ):
                        raise ValueError(message)
                except (KeyError, TypeError, ValueError) as error:
                    if isinstance(error, ValueError) and str(error) == message:
                        raise
                    raise ValueError(message) from error
                pending.append(
                    PendingSubmission(
                        attempt_id=row["attempt_id"],
                        bundle=bundle,
                        retry_count=row["retry_count"],
                        next_attempt_at=next_attempt,
                        last_error=row["last_error"],
                    )
                )
        return pending

    def record_retry(
        self,
        attempt_id: str,
        *,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> PendingSubmission:
        next_iso = _iso(next_attempt_at, "Retry time")
        if last_error is not None and (
            not isinstance(last_error, str) or len(last_error) > 2000
        ):
            raise ValueError("Retry error is invalid.")
        with self._transaction() as connection:
            state = connection.execute(
                "SELECT state FROM local_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if state is None:
                raise KeyError(f"Unknown local attempt: {attempt_id}")
            if state["state"] != "sealed_pending":
                raise ValueError("Only a pending sealed attempt can be retried.")
            cursor = connection.execute(
                """UPDATE submission_outbox
                   SET retry_count=retry_count+1, next_attempt_at=?, last_error=?
                   WHERE attempt_id=?""",
                (next_iso, last_error, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Sealed attempt is missing its submission outbox record.")
        return next(item for item in self.pending_submissions() if item.attempt_id == attempt_id)

    def acknowledge(self, attempt_id: str, receipt: SubmissionReceipt) -> LocalAttemptRecord:
        receipt, receipt_json = _validate_receipt(receipt)
        if receipt.attempt_id != attempt_id:
            raise ValueError("Acknowledgment attempt identifier does not match.")
        with self._transaction() as connection:
            snapshot = self._validated_attempt_snapshot(
                connection, attempt_id, "Stored attempt data is invalid."
            )
            record = snapshot.record
            sealed_bundle = snapshot.sealed_bundle
            if sealed_bundle is None:
                raise ValueError("Stored attempt data is invalid.")
            _validate_receipt_for_sealed_attempt(
                receipt,
                record.question_order,
                sealed_bundle,
                "Receipt does not match the sealed attempt.",
            )
            if record.state == "acknowledged":
                if snapshot.receipt_json != receipt_json:
                    raise ValueError("Acknowledgment conflicts with the stored receipt.")
                acknowledged = record
            elif record.state != "sealed_pending":
                raise ValueError("Only a pending sealed attempt can be acknowledged.")
            else:
                if record.sealed_at is None:
                    raise ValueError("Stored attempt data is invalid.")
                if receipt.accepted_at < record.sealed_at:
                    raise ValueError("Receipt acceptance time cannot precede the seal time.")
                connection.execute(
                    """UPDATE local_attempts SET state='acknowledged', receipt_json=?
                       WHERE attempt_id=?""",
                    (receipt_json, attempt_id),
                )
                connection.execute(
                    "DELETE FROM submission_outbox WHERE attempt_id=?", (attempt_id,)
                )
                acknowledged = replace(record, state="acknowledged", receipt=receipt)
        return acknowledged
