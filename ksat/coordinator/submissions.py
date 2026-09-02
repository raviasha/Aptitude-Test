"""Coordinator-owned validation, scoring, and serialized submission persistence."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ksat.crypto import sha256_hex, verify_json
from ksat.protocol import (
    IntegrityEvent,
    SignedResponseBundle,
    SignedAttemptDeadlineUpdate,
    SubmissionReceipt,
    canonical_json,
)
from ksat.sqlite import connect_sqlite


class SubmissionProblem(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class ReleaseAnswerStateProblem(ValueError):
    code = "release_answer_state_invalid"

    def __init__(self, message: str = "The release private answer snapshot is incomplete or invalid.") -> None:
        super().__init__(message)
        self.message = message


INVALID_ANSWER_STATE = "answer_state_invalid"
_CANONICAL_OPTION_SETS = (tuple("ABCD"), tuple("ABCDE"))


@dataclass(frozen=True)
class ScoredSubmission:
    attempt_id: str
    student_id: str
    release_id: str
    sealed_at: str
    bundle_hash: str
    bundle_json: str
    responses: tuple[tuple[int, str | None, int, str, str], ...]
    score: int
    attempted: int
    total_questions: int
    percentage: float
    violations: tuple[IntegrityEvent, ...]


def _release_and_question_ids(
    connection: sqlite3.Connection, release_id: str
) -> tuple[sqlite3.Row, tuple[int, ...]]:
    release = connection.execute(
        "SELECT state, manifest_json FROM assessment_releases WHERE release_id=?",
        (release_id,),
    ).fetchone()
    if release is None:
        raise ReleaseAnswerStateProblem("The assessment release does not exist.")
    try:
        manifest = json.loads(release["manifest_json"])
        raw_ids = manifest["canonical_question_ids"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReleaseAnswerStateProblem("The release manifest question coverage is invalid.") from error
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(question_id, int) or isinstance(question_id, bool) for question_id in raw_ids)
        or len(raw_ids) != len(set(raw_ids))
    ):
        raise ReleaseAnswerStateProblem("The release manifest question coverage is invalid.")
    return release, tuple(raw_ids)


def validate_release_answer_state(
    connection: sqlite3.Connection, release_id: str
) -> tuple[sqlite3.Row, ...]:
    """Validate exact coverage and every private field used by scoring/results."""

    _release, question_ids = _release_and_question_ids(connection, release_id)
    rows = tuple(connection.execute(
        """SELECT question_id, canonical_order, options_json, correct_answer, category, chapter
           FROM release_questions WHERE release_id=? ORDER BY canonical_order""",
        (release_id,),
    ).fetchall())
    if len(rows) != len(question_ids):
        raise ReleaseAnswerStateProblem("The release private snapshot question coverage is incomplete.")
    for canonical_order, (expected_id, row) in enumerate(zip(question_ids, rows)):
        if row["question_id"] != expected_id or row["canonical_order"] != canonical_order:
            raise ReleaseAnswerStateProblem("The release private snapshot question coverage is invalid.")
        try:
            option_keys = json.loads(row["options_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ReleaseAnswerStateProblem("A release question has invalid allowed options.") from error
        if (
            not isinstance(option_keys, list)
            or tuple(option_keys) not in _CANONICAL_OPTION_SETS
            or row["correct_answer"] not in option_keys
            or not isinstance(row["category"], str)
            or not row["category"].strip()
            or not isinstance(row["chapter"], str)
            or not row["chapter"].strip()
        ):
            raise ReleaseAnswerStateProblem("A release question has incomplete private scoring metadata.")
    return rows


def _source_answer_snapshot(row: sqlite3.Row) -> tuple[str, str, str, str]:
    try:
        options = json.loads(row["options_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise ReleaseAnswerStateProblem("A source question has invalid options.") from error
    if (
        not isinstance(options, dict)
        or tuple(sorted(options)) not in _CANONICAL_OPTION_SETS
        or any(not isinstance(value, str) or not value.strip() for value in options.values())
    ):
        options = {
            key: row[f"option_{key.lower()}"]
            for key in "ABCD"
            if isinstance(row[f"option_{key.lower()}"], str)
            and row[f"option_{key.lower()}"].strip()
        }
    option_keys = tuple(sorted(options))
    if (
        option_keys not in _CANONICAL_OPTION_SETS
        or row["correct_answer"] not in option_keys
        or not isinstance(row["category"], str)
        or not row["category"].strip()
        or not isinstance(row["chapter"], str)
        or not row["chapter"].strip()
    ):
        raise ReleaseAnswerStateProblem("A source question has incomplete private scoring metadata.")
    return (
        json.dumps(option_keys, separators=(",", ":")),
        row["correct_answer"],
        row["category"],
        row["chapter"],
    )


def freeze_release_answer_state(connection: sqlite3.Connection, release_id: str) -> None:
    """Create a complete private snapshot, or validate an immutable existing one."""

    try:
        validate_release_answer_state(connection, release_id)
        return
    except ReleaseAnswerStateProblem:
        pass

    release, question_ids = _release_and_question_ids(connection, release_id)
    has_attempts = connection.execute(
        "SELECT 1 FROM attempts WHERE release_id=? LIMIT 1", (release_id,)
    ).fetchone()
    if release["state"] != "prepared" or has_attempts:
        raise ReleaseAnswerStateProblem(
            "The used or launched release has an incomplete private answer snapshot and cannot be repaired safely."
        )

    placeholders = ",".join("?" for _ in question_ids)
    source_rows = connection.execute(
        f"""SELECT question_id, options_json, option_a, option_b, option_c, option_d,
                   correct_answer, category, chapter
            FROM questions WHERE question_id IN ({placeholders})""",
        question_ids,
    ).fetchall()
    source_by_id = {row["question_id"]: row for row in source_rows}
    if set(source_by_id) != set(question_ids):
        raise ReleaseAnswerStateProblem(
            "The unused release cannot be repaired because a source question is missing."
        )
    frozen = [
        (release_id, question_id, order, *_source_answer_snapshot(source_by_id[question_id]))
        for order, question_id in enumerate(question_ids)
    ]

    connection.execute("SAVEPOINT freeze_release_answer_state")
    try:
        connection.execute("DELETE FROM release_questions WHERE release_id=?", (release_id,))
        connection.executemany(
            """INSERT INTO release_questions
               (release_id, question_id, canonical_order, options_json,
                correct_answer, category, chapter)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            frozen,
        )
        validate_release_answer_state(connection, release_id)
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT freeze_release_answer_state")
        connection.execute("RELEASE SAVEPOINT freeze_release_answer_state")
        raise
    connection.execute("RELEASE SAVEPOINT freeze_release_answer_state")


def migrate_release_answer_states(connection: sqlite3.Connection) -> None:
    """Repair only unused prepared releases; quarantine every unsafe partial snapshot."""

    release_ids = [
        row["release_id"]
        for row in connection.execute("SELECT release_id FROM assessment_releases").fetchall()
    ]
    for release_id in release_ids:
        try:
            freeze_release_answer_state(connection, release_id)
        except ReleaseAnswerStateProblem:
            connection.execute(
                "UPDATE assessment_releases SET state=? WHERE release_id=?",
                (INVALID_ANSWER_STATE, release_id),
            )


def _problem(code: str, message: str) -> SubmissionProblem:
    return SubmissionProblem(code, message)


def _utc(value: datetime, *, code: str = "invalid_timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _problem(code, "Submission timestamps must include a time zone.")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise _problem(code, "Submission timestamp is invalid.") from error


def _stored_receipt(connection: sqlite3.Connection, attempt_id: str) -> SubmissionReceipt | None:
    row = connection.execute(
        """SELECT s.receipt_json, s.accepted_at, a.score, a.total_questions,
                  a.attempted, a.percentage,
                  (SELECT COUNT(*) FROM exam_violations ev WHERE ev.attempt_id=a.attempt_id) AS violations
           FROM submissions s JOIN attempts a ON a.attempt_id=s.attempt_id
           WHERE s.attempt_id=?""",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return None
    if row["receipt_json"]:
        try:
            return SubmissionReceipt.model_validate_json(row["receipt_json"])
        except Exception as error:
            raise _problem("stored_submission_invalid", "The stored submission receipt is invalid.") from error
    return SubmissionReceipt(
        attempt_id=attempt_id,
        accepted_at=row["accepted_at"],
        score=row["score"],
        total_questions=row["total_questions"],
        attempted=row["attempted"],
        percentage=row["percentage"],
        violations=row["violations"],
    )


def validate_and_score(
    connection: sqlite3.Connection,
    signed_bundle: SignedResponseBundle,
    *,
    coordinator_public_key_b64: str,
    received_at: datetime,
) -> ScoredSubmission | SubmissionReceipt:
    """Authenticate identity first, then validate and score against frozen release state."""

    _utc(received_at)
    bundle = signed_bundle.bundle
    signed_ticket = bundle.ticket
    ticket = signed_ticket.ticket
    try:
        verify_json(coordinator_public_key_b64, ticket, signed_ticket.signature_b64)
    except ValueError as error:
        raise _problem("invalid_ticket_signature", "The attempt ticket signature is invalid.") from error

    device = connection.execute(
        "SELECT public_key_b64, status FROM devices WHERE device_id=?", (ticket.device_id,)
    ).fetchone()
    if device is None or device["status"] != "active":
        raise SubmissionProblem(
            "device_inactive", "The submitting device is not active.", status_code=403
        )
    try:
        verify_json(device["public_key_b64"], bundle, signed_bundle.device_signature_b64)
    except ValueError as error:
        raise _problem("invalid_bundle_signature", "The response bundle signature is invalid.") from error

    attempt = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id=?", (ticket.attempt_id,)
    ).fetchone()
    if attempt is None:
        raise _problem("attempt_identity_mismatch", "The submitted attempt identity is invalid.")
    try:
        stored_ticket_matches = attempt["ticket_json"] == canonical_json(signed_ticket).decode("utf-8")
        stored_started = datetime.fromisoformat(attempt["started_at"].replace("Z", "+00:00"))
        stored_deadline = datetime.fromisoformat(attempt["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise _problem("attempt_identity_mismatch", "The stored attempt identity is invalid.") from error
    effective_deadline = _utc(ticket.deadline)
    if int(attempt["deadline_revision"] or 0) > 0:
        try:
            deadline_update = SignedAttemptDeadlineUpdate.model_validate_json(
                attempt["deadline_update_json"], strict=True
            )
            verify_json(
                coordinator_public_key_b64,
                deadline_update.update,
                deadline_update.signature_b64,
            )
            if (
                deadline_update.update.attempt_id != attempt["attempt_id"]
                or deadline_update.update.release_id != attempt["release_id"]
                or deadline_update.update.device_id != attempt["device_id"]
                or deadline_update.update.revision != attempt["deadline_revision"]
                or _utc(deadline_update.update.base_deadline) != _utc(ticket.deadline)
                or _utc(deadline_update.update.deadline)
                   != _utc(ticket.deadline)
                      + timedelta(seconds=deadline_update.update.cumulative_extension_seconds)
                or _utc(deadline_update.update.deadline) != _utc(stored_deadline)
            ):
                raise ValueError
            effective_deadline = _utc(deadline_update.update.deadline)
        except Exception as error:
            raise _problem("attempt_identity_mismatch", "The stored attempt identity is invalid.") from error
    if (
        attempt["student_id"] != ticket.student_id
        or attempt["release_id"] != ticket.release_id
        or attempt["device_id"] != ticket.device_id
        or attempt["order_seed"] != ticket.order_seed_b64
        or not stored_ticket_matches
        or _utc(stored_started) != _utc(ticket.started_at)
        or _utc(stored_deadline) != effective_deadline
    ):
        raise _problem("attempt_identity_mismatch", "The submitted attempt identity is invalid.")

    existing = _stored_receipt(connection, ticket.attempt_id)
    if existing is not None:
        return existing

    release = connection.execute(
        "SELECT test_id, content_hash, state FROM assessment_releases WHERE release_id=?",
        (ticket.release_id,),
    ).fetchone()
    if release is None or release["test_id"] != attempt["test_id"]:
        raise _problem("release_identity_mismatch", "The assessment release identity is invalid.")
    if release["state"] == INVALID_ANSWER_STATE:
        raise _problem(
            "release_answer_state_invalid",
            "The release private answer snapshot is incomplete; Faculty must create a new assessment.",
        )
    if ticket.content_hash != release["content_hash"] or bundle.content_hash != release["content_hash"]:
        raise _problem("content_hash_mismatch", "The assessment content hash does not match.")

    try:
        frozen_rows = validate_release_answer_state(connection, ticket.release_id)
    except ReleaseAnswerStateProblem as error:
        raise _problem(
            "release_answer_state_invalid",
            "The release private answer snapshot is incomplete; Faculty must create a new assessment.",
        ) from error
    expected_ids = [row["question_id"] for row in frozen_rows]
    submitted_ids = [item.question_id for item in bundle.responses]
    if not expected_ids or len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != set(expected_ids):
        raise _problem(
            "invalid_question_set",
            "The submission must contain every frozen release question exactly once.",
        )

    sealed_at = _utc(bundle.sealed_at)
    started_at = _utc(ticket.started_at)
    deadline = effective_deadline
    if sealed_at < started_at:
        raise _problem("invalid_timestamp", "The submission seal timestamp is invalid.")
    if sealed_at > deadline + timedelta(seconds=5):
        raise _problem("deadline_exceeded", "The submission was sealed after the deadline grace period.")
    for event in bundle.integrity_events:
        occurred_at = _utc(event.occurred_at)
        if occurred_at < started_at or occurred_at > sealed_at:
            raise _problem("invalid_timestamp", "An integrity-event timestamp is outside the attempt.")

    response_by_id = {item.question_id: item.selected_answer for item in bundle.responses}
    scored_rows: list[tuple[int, str | None, int, str, str]] = []
    score = 0
    attempted = 0
    for row in frozen_rows:
        try:
            allowed = json.loads(row["options_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise _problem("release_answer_state_invalid", "The frozen release answer state is invalid.") from error
        if (
            not isinstance(allowed, list)
            or not allowed
            or len(allowed) != len(set(allowed))
            or any(not isinstance(item, str) for item in allowed)
            or row["correct_answer"] not in allowed
            or not row["category"]
            or not row["chapter"]
        ):
            raise _problem("release_answer_state_invalid", "The frozen release answer state is invalid.")
        selected = response_by_id[row["question_id"]]
        if selected is not None and selected not in allowed:
            raise _problem("invalid_response_option", "A selected answer is not valid for its question.")
        correct = int(selected is not None and selected == row["correct_answer"])
        attempted += int(selected is not None)
        score += correct
        scored_rows.append((row["question_id"], selected, correct, row["category"], row["chapter"]))

    total = len(scored_rows)
    percentage = round(score / total * 100, 1) if total else 0.0
    bundle_json = canonical_json(signed_bundle).decode("utf-8")
    return ScoredSubmission(
        attempt_id=ticket.attempt_id,
        student_id=ticket.student_id,
        release_id=ticket.release_id,
        sealed_at=sealed_at.isoformat(timespec="seconds"),
        bundle_hash=sha256_hex(bundle_json.encode("utf-8")),
        bundle_json=bundle_json,
        responses=tuple(scored_rows),
        score=score,
        attempted=attempted,
        total_questions=total,
        percentage=percentage,
        violations=tuple(bundle.integrity_events),
    )


@dataclass(frozen=True)
class _WorkItem:
    scored: ScoredSubmission
    future: Future[SubmissionReceipt]


class SubmissionWriter:
    def __init__(self, db_path: Path, *, max_pending: int = 200) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.db_path = Path(db_path)
        self.max_pending = max_pending
        self._queue: queue.Queue[_WorkItem] = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._state = "new"
        self._thread: threading.Thread | None = None
        self._active_future: Future[SubmissionReceipt] | None = None
        self._worker_failure: SubmissionProblem | None = None

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        with self._lock:
            if self._state == "running":
                return
            if self._state == "stopping" and self._thread is not None and self._thread.is_alive():
                raise SubmissionProblem(
                    "submission_writer_stopping",
                    "The submission service is stopping.",
                    status_code=503,
                    retryable=True,
                )
            if self._state in {"stopped", "failed"}:
                self._queue = queue.Queue(maxsize=self.max_pending)
            self._worker_failure = None
            self._state = "running"
            self._thread = threading.Thread(
                target=self._run,
                name="ksat-submission-writer",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        with self._lock:
            if self._state in {"new", "stopped", "failed"}:
                self._state = "stopped"
                return
            self._state = "stopping"
            thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        if thread is not None and thread.is_alive():
            problem = SubmissionProblem(
                "submission_writer_stopping",
                "The submission service stopped before queued work completed.",
                status_code=503,
                retryable=True,
            )
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._set_exception(item.future, problem)
                self._queue.task_done()
            with self._lock:
                active_future = self._active_future
            if active_future is not None:
                self._set_exception(active_future, problem)
            return
        with self._lock:
            self._state = "stopped"
            self._thread = None

    def submit(self, scored: ScoredSubmission, timeout_seconds: float = 15.0) -> SubmissionReceipt:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        future: Future[SubmissionReceipt] = Future()
        with self._lock:
            if self._worker_failure is not None:
                raise self._worker_failure
            if self._state != "running":
                raise SubmissionProblem(
                    "submission_writer_not_running",
                    "The submission service is not running.",
                    status_code=503,
                    retryable=True,
                )
            try:
                self._queue.put_nowait(_WorkItem(scored, future))
            except queue.Full as error:
                raise SubmissionProblem(
                    "submission_busy",
                    "The submission queue is full; the sealed work is safe to retry.",
                    status_code=503,
                    retryable=True,
                ) from error
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeout as error:
            raise SubmissionProblem(
                "submission_timeout",
                "The submission acknowledgment timed out; retry the sealed bundle.",
                status_code=503,
                retryable=True,
            ) from error

    @staticmethod
    def _set_exception(future: Future[SubmissionReceipt], error: BaseException) -> None:
        try:
            future.set_exception(error)
        except InvalidStateError:
            pass

    @staticmethod
    def _set_result(future: Future[SubmissionReceipt], receipt: SubmissionReceipt) -> None:
        try:
            future.set_result(receipt)
        except InvalidStateError:
            pass

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> bool:
        try:
            connection.rollback()
        except Exception:
            return False
        return True

    @staticmethod
    def _close(connection: sqlite3.Connection | None) -> None:
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _terminal_failure(self, problem: SubmissionProblem) -> None:
        with self._lock:
            self._worker_failure = problem
            self._state = "failed"
            active_future = self._active_future
        if active_future is not None:
            self._set_exception(active_future, problem)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._set_exception(item.future, problem)
            self._queue.task_done()

    @staticmethod
    def _failure(error: BaseException, message: str) -> SubmissionProblem:
        problem = SubmissionProblem(
            "submission_failed",
            message,
            status_code=503,
            retryable=True,
        )
        problem.__cause__ = error
        return problem

    def _open_connection(self) -> sqlite3.Connection:
        return connect_sqlite(self.db_path)

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection()
        except Exception as error:
            self._terminal_failure(self._failure(
                error,
                "The coordinator could not open the submission database; retry the sealed bundle.",
            ))
            return
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    with self._lock:
                        if self._state == "stopping":
                            break
                    continue
                try:
                    with self._lock:
                        self._active_future = item.future
                    assert connection is not None
                    receipt = self._commit(connection, item.scored)
                except SubmissionProblem as error:
                    rollback_succeeded = self._rollback(connection)
                    self._set_exception(item.future, error)
                    if not rollback_succeeded:
                        self._close(connection)
                        connection = None
                except Exception as error:
                    rollback_succeeded = self._rollback(connection)
                    problem = self._failure(
                        error,
                        "The coordinator could not persist the submission; retry the sealed bundle.",
                    )
                    self._set_exception(item.future, problem)
                    if not rollback_succeeded:
                        self._close(connection)
                        connection = None
                else:
                    self._set_result(item.future, receipt)
                finally:
                    with self._lock:
                        if self._active_future is item.future:
                            self._active_future = None
                    self._queue.task_done()
                if connection is None:
                    try:
                        connection = self._open_connection()
                    except Exception as error:
                        self._terminal_failure(self._failure(
                            error,
                            "The coordinator could not recover the submission database; retry the sealed bundle.",
                        ))
                        return
        except Exception as error:
            self._terminal_failure(self._failure(
                error,
                "The submission worker stopped unexpectedly; retry the sealed bundle.",
            ))
        finally:
            self._close(connection)
            with self._lock:
                if self._state == "stopping":
                    self._state = "stopped"

    @staticmethod
    def _commit(connection: sqlite3.Connection, scored: ScoredSubmission) -> SubmissionReceipt:
        connection.execute("BEGIN IMMEDIATE")
        existing = _stored_receipt(connection, scored.attempt_id)
        if existing is not None:
            connection.commit()
            return existing
        attempt = connection.execute(
            "SELECT status FROM attempts WHERE attempt_id=?", (scored.attempt_id,)
        ).fetchone()
        if attempt is None or attempt["status"] != "in_progress":
            raise _problem("attempt_not_submittable", "The attempt cannot be submitted.")
        connection.executemany(
            """INSERT INTO responses
               (attempt_id, question_id, selected_answer, correct, category, chapter, question_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (scored.attempt_id, question_id, selected, correct, category, chapter, order)
                for order, (question_id, selected, correct, category, chapter) in enumerate(scored.responses)
            ],
        )
        connection.executemany(
            "INSERT INTO exam_violations (attempt_id, violation_type, occurred_at) VALUES (?, ?, ?)",
            [
                [
                    scored.attempt_id,
                    event.event_type,
                    _utc(event.occurred_at).isoformat(timespec="seconds"),
                ]
                for event in scored.violations
            ],
        )
        accepted_at = datetime.now(timezone.utc)
        receipt = SubmissionReceipt(
            attempt_id=scored.attempt_id,
            accepted_at=accepted_at,
            score=scored.score,
            total_questions=scored.total_questions,
            attempted=scored.attempted,
            percentage=scored.percentage,
            violations=len(scored.violations),
        )
        updated = connection.execute(
            """UPDATE attempts
               SET submitted_at=?, status='submitted', attempted=?, correct=?, score=?,
                   percentage=?, sealed_at=?, submission_hash=?
               WHERE attempt_id=? AND status='in_progress'""",
            (
                accepted_at.isoformat(timespec="seconds"),
                scored.attempted,
                scored.score,
                scored.score,
                scored.percentage,
                scored.sealed_at,
                scored.bundle_hash,
                scored.attempt_id,
            ),
        )
        if updated.rowcount != 1:
            raise _problem("attempt_not_submittable", "The attempt cannot be submitted.")
        connection.execute(
            """INSERT INTO submissions
               (attempt_id, bundle_hash, bundle_json, accepted_at, receipt_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                scored.attempt_id,
                scored.bundle_hash,
                scored.bundle_json,
                accepted_at.isoformat(timespec="seconds"),
                canonical_json(receipt).decode("utf-8"),
            ),
        )
        connection.commit()
        return receipt


__all__ = [
    "freeze_release_answer_state",
    "migrate_release_answer_states",
    "validate_release_answer_state",
    "INVALID_ANSWER_STATE",
    "ReleaseAnswerStateProblem",
    "ScoredSubmission",
    "SubmissionProblem",
    "SubmissionWriter",
    "validate_and_score",
]
