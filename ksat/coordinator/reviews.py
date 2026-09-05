"""Close-gated authorization for immutable student assessment reviews."""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime

from ksat.coordinator.releases import (
    unwrap_release_content_key,
    unwrap_release_review_key,
)
from ksat.protocol import (
    AssessmentReviewGrant,
    CompletedAssessmentSummary,
    ReviewResponseEntry,
)


class ReviewProblem(ValueError):
    def __init__(
        self, code: str, message: str, *, status_code: int = 409, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def _review_state(row: sqlite3.Row) -> str:
    if not row["wrapped_review_key_b64"]:
        return "unavailable"
    return "waiting" if bool(row["launched"]) else "available"


def list_completed_assessments(
    connection: sqlite3.Connection, *, student_id: str
) -> list[CompletedAssessmentSummary]:
    rows = connection.execute(
        """SELECT a.attempt_id,a.release_id,a.test_id,t.test_name,t.launched,
                  a.score,a.total_questions,a.percentage,s.accepted_at,
                  ar.wrapped_review_key_b64
           FROM attempts a
           JOIN submissions s ON s.attempt_id=a.attempt_id
           JOIN tests t ON t.test_id=a.test_id
           JOIN assessment_releases ar ON ar.release_id=a.release_id
           WHERE a.student_id=? AND a.status='submitted'
           ORDER BY s.accepted_at DESC,a.attempt_id DESC""",
        (student_id,),
    ).fetchall()
    return [
        CompletedAssessmentSummary(
            attempt_id=row["attempt_id"],
            release_id=row["release_id"],
            test_id=row["test_id"],
            test_name=row["test_name"],
            accepted_at=datetime.fromisoformat(row["accepted_at"].replace("Z", "+00:00")),
            score=row["score"],
            total_questions=row["total_questions"],
            percentage=row["percentage"],
            review_state=_review_state(row),
        )
        for row in rows
    ]


def issue_review_grant(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    student_id: str,
    pack_master_key: bytes,
) -> AssessmentReviewGrant:
    row = connection.execute(
        """SELECT a.attempt_id,a.student_id,a.release_id,a.status,t.launched,
                  ar.content_hash,ar.wrapped_content_key_b64,ar.wrapped_review_key_b64
           FROM attempts a
           JOIN submissions s ON s.attempt_id=a.attempt_id
           JOIN tests t ON t.test_id=a.test_id
           JOIN assessment_releases ar ON ar.release_id=a.release_id
           WHERE a.attempt_id=? AND a.student_id=? AND a.status='submitted'""",
        (attempt_id, student_id),
    ).fetchone()
    if row is None:
        raise ReviewProblem(
            "review_not_found", "The completed assessment review was not found.",
            status_code=404,
        )
    if bool(row["launched"]):
        raise ReviewProblem(
            "review_not_released",
            "Review will be available after Faculty closes the assessment.",
        )
    if not row["wrapped_review_key_b64"]:
        raise ReviewProblem(
            "review_unavailable",
            "Detailed review is unavailable for this older assessment.",
        )
    try:
        content_key = unwrap_release_content_key(
            pack_master_key, row["release_id"], row["wrapped_content_key_b64"]
        )
        review_key = unwrap_release_review_key(
            pack_master_key, row["release_id"], row["wrapped_review_key_b64"]
        )
    except ValueError as error:
        raise ReviewProblem(
            "review_unavailable",
            "Detailed review requires faculty intervention.",
            status_code=503,
        ) from error
    response_rows = connection.execute(
        """SELECT question_id,question_order,selected_answer
           FROM responses WHERE attempt_id=? ORDER BY question_order""",
        (attempt_id,),
    ).fetchall()
    try:
        return AssessmentReviewGrant(
            attempt_id=row["attempt_id"],
            student_id=row["student_id"],
            release_id=row["release_id"],
            content_hash=row["content_hash"],
            content_key_b64=base64.b64encode(content_key).decode("ascii"),
            review_key_b64=base64.b64encode(review_key).decode("ascii"),
            responses=[ReviewResponseEntry(**dict(item)) for item in response_rows],
        )
    except Exception as error:
        raise ReviewProblem(
            "review_unavailable",
            "Detailed review requires faculty intervention.",
            status_code=503,
        ) from error


__all__ = [
    "ReviewProblem",
    "issue_review_grant",
    "list_completed_assessments",
]
