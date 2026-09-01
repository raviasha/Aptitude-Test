"""Coordinator-owned validation, scoring, and serialized submission persistence."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ksat.crypto import sha256_hex, verify_json
from ksat.protocol import (
    IntegrityEvent,
    SignedResponseBundle,
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


def freeze_release_answer_state(connection: sqlite3.Connection, release_id: str) -> None:
    """Freeze coordinator-only answer and option metadata for a production release."""

    rows = connection.execute(
        """SELECT rq.question_id, q.options_json, q.option_a, q.option_b, q.option_c,
                  q.option_d, q.correct_answer, q.category, q.chapter
           FROM release_questions rq
           JOIN questions q ON q.question_id=rq.question_id
           WHERE rq.release_id=? ORDER BY rq.canonical_order""",
        (release_id,),
    ).fetchall()
    expected = connection.execute(
        "SELECT COUNT(*) FROM release_questions WHERE release_id=?", (release_id,)
    ).fetchone()[0]
    if not rows or len(rows) != expected:
        raise ValueError("Release questions are missing private answer state.")
    for row in rows:
        try:
            options = json.loads(row["options_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("Question options are invalid.") from error
        if not isinstance(options, dict) or set(options) not in (set("ABCD"), set("ABCDE")):
            options = {
                key: row[f"option_{key.lower()}"]
                for key in "ABCD"
                if row[f"option_{key.lower()}"] is not None
            }
        option_keys = sorted(options)
        if row["correct_answer"] not in option_keys:
            raise ValueError("Question answer is not among its options.")
        connection.execute(
            """UPDATE release_questions
               SET options_json=COALESCE(options_json, ?),
                   correct_answer=COALESCE(correct_answer, ?),
                   category=COALESCE(category, ?), chapter=COALESCE(chapter, ?)
               WHERE release_id=? AND question_id=?""",
            (
                json.dumps(option_keys, separators=(",", ":")),
                row["correct_answer"],
                row["category"],
                row["chapter"],
                release_id,
                row["question_id"],
            ),
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
    if (
        attempt["student_id"] != ticket.student_id
        or attempt["release_id"] != ticket.release_id
        or attempt["device_id"] != ticket.device_id
        or attempt["order_seed"] != ticket.order_seed_b64
        or not stored_ticket_matches
        or _utc(stored_started) != _utc(ticket.started_at)
        or _utc(stored_deadline) != _utc(ticket.deadline)
    ):
        raise _problem("attempt_identity_mismatch", "The submitted attempt identity is invalid.")

    existing = _stored_receipt(connection, ticket.attempt_id)
    if existing is not None:
        return existing

    release = connection.execute(
        "SELECT test_id, content_hash FROM assessment_releases WHERE release_id=?",
        (ticket.release_id,),
    ).fetchone()
    if release is None or release["test_id"] != attempt["test_id"]:
        raise _problem("release_identity_mismatch", "The assessment release identity is invalid.")
    if ticket.content_hash != release["content_hash"] or bundle.content_hash != release["content_hash"]:
        raise _problem("content_hash_mismatch", "The assessment content hash does not match.")

    frozen_rows = connection.execute(
        """SELECT question_id, canonical_order, options_json, correct_answer, category, chapter
           FROM release_questions WHERE release_id=? ORDER BY canonical_order""",
        (ticket.release_id,),
    ).fetchall()
    expected_ids = [row["question_id"] for row in frozen_rows]
    submitted_ids = [item.question_id for item in bundle.responses]
    if not expected_ids or len(submitted_ids) != len(set(submitted_ids)) or set(submitted_ids) != set(expected_ids):
        raise _problem(
            "invalid_question_set",
            "The submission must contain every frozen release question exactly once.",
        )

    sealed_at = _utc(bundle.sealed_at)
    started_at = _utc(ticket.started_at)
    deadline = _utc(ticket.deadline)
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
                if not item.future.done():
                    item.future.set_exception(problem)
                self._queue.task_done()
            with self._lock:
                active_future = self._active_future
            if active_future is not None and not active_future.done():
                active_future.set_exception(problem)
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

    def _run(self) -> None:
        try:
            connection = connect_sqlite(self.db_path)
        except Exception as error:
            problem = SubmissionProblem(
                "submission_failed",
                "The coordinator could not open the submission database; retry the sealed bundle.",
                status_code=503,
                retryable=True,
            )
            problem.__cause__ = error
            with self._lock:
                self._worker_failure = problem
                self._state = "failed"
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if not item.future.done():
                        item.future.set_exception(problem)
                    self._queue.task_done()
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
                    receipt = self._commit(connection, item.scored)
                except SubmissionProblem as error:
                    if not item.future.done():
                        item.future.set_exception(error)
                except Exception as error:
                    connection.rollback()
                    problem = SubmissionProblem(
                        "submission_failed",
                        "The coordinator could not persist the submission; retry the sealed bundle.",
                        status_code=503,
                        retryable=True,
                    )
                    problem.__cause__ = error
                    if not item.future.done():
                        item.future.set_exception(problem)
                else:
                    if not item.future.done():
                        item.future.set_result(receipt)
                finally:
                    with self._lock:
                        if self._active_future is item.future:
                            self._active_future = None
                    self._queue.task_done()
        finally:
            connection.close()
            with self._lock:
                if self._state == "stopping":
                    self._state = "stopped"

    @staticmethod
    def _commit(connection: sqlite3.Connection, scored: ScoredSubmission) -> SubmissionReceipt:
        connection.execute("BEGIN IMMEDIATE")
        try:
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
                    (
                        scored.attempt_id,
                        event.event_type,
                        _utc(event.occurred_at).isoformat(timespec="seconds"),
                    )
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
        except Exception:
            connection.rollback()
            raise


__all__ = [
    "freeze_release_answer_state",
    "ScoredSubmission",
    "SubmissionProblem",
    "SubmissionWriter",
    "validate_and_score",
]
