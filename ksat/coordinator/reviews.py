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
    SealedAssessmentReviewGrant,
    SealedReviewRequest,
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
    return (
        "available"
        if row["review_released_at"] and not bool(row["launched"])
        else "waiting"
    )


def list_completed_assessments(
    connection: sqlite3.Connection, *, student_id: str
) -> list[CompletedAssessmentSummary]:
    rows = connection.execute(
        """SELECT a.attempt_id,a.release_id,a.test_id,t.test_name,t.launched,
                  t.review_released_at,
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
                  t.review_released_at,
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
    if bool(row["launched"]) or not row["review_released_at"]:
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


def issue_sealed_review_grant(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    student_id: str,
    device_id: str,
    bundle_hash: str,
    pack_master_key: bytes,
) -> SealedAssessmentReviewGrant:
    """Commit immutable answers before releasing keys for an upload-pending review."""
    SealedReviewRequest(bundle_hash=bundle_hash)
    if connection.in_transaction:
        raise ValueError("A sealed review grant requires its own transaction.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """SELECT a.attempt_id,a.student_id,a.release_id,a.status,a.review_seal_hash,
                      d.status AS device_status,t.launched,t.review_released_at,
                      ar.content_hash,ar.wrapped_content_key_b64,ar.wrapped_review_key_b64,
                      s.bundle_hash AS accepted_bundle_hash
               FROM attempts a
               JOIN students student ON student.student_id=a.student_id
               JOIN devices d ON d.device_id=a.device_id
               JOIN tests t ON t.test_id=a.test_id
               JOIN assessment_releases ar ON ar.release_id=a.release_id AND ar.test_id=a.test_id
               LEFT JOIN submissions s ON s.attempt_id=a.attempt_id
               WHERE a.attempt_id=? AND a.student_id=? AND a.device_id=?
                 AND a.status IN ('in_progress','submitted')""",
            (attempt_id, student_id, device_id),
        ).fetchone()
        if row is None or (row["status"] == "submitted" and row["accepted_bundle_hash"] is None):
            raise ReviewProblem("review_not_found", "The assessment review was not found.", status_code=404)
        if row["device_status"] != "active":
            raise ReviewProblem("device_inactive", "The device is not active.", status_code=403)
        if bool(row["launched"]) or not row["review_released_at"]:
            raise ReviewProblem(
                "review_not_released", "Review will be available after Faculty closes the assessment."
            )
        if not row["wrapped_review_key_b64"]:
            raise ReviewProblem("review_unavailable", "Detailed review is unavailable for this older assessment.")
        for committed in (row["review_seal_hash"], row["accepted_bundle_hash"]):
            if committed is not None and committed != bundle_hash:
                raise ReviewProblem(
                    "review_seal_conflict", "The sealed answers do not match the assessment's committed submission."
                )
        try:
            content_key = unwrap_release_content_key(
                pack_master_key, row["release_id"], row["wrapped_content_key_b64"]
            )
            review_key = unwrap_release_review_key(
                pack_master_key, row["release_id"], row["wrapped_review_key_b64"]
            )
            grant = SealedAssessmentReviewGrant(
                attempt_id=row["attempt_id"], student_id=row["student_id"],
                release_id=row["release_id"], content_hash=row["content_hash"],
                content_key_b64=base64.b64encode(content_key).decode("ascii"),
                review_key_b64=base64.b64encode(review_key).decode("ascii"), bundle_hash=bundle_hash,
            )
        except (ValueError, TypeError) as error:
            raise ReviewProblem(
                "review_unavailable", "Detailed review requires faculty intervention.", status_code=503
            ) from error
        connection.execute(
            "UPDATE attempts SET review_seal_hash=? WHERE attempt_id=?",
            (bundle_hash, attempt_id),
        )
        # The submission writer checks this same commitment in its own write
        # transaction, even for bundles validated before these keys were released.
        connection.commit()
        return grant
    except BaseException:
        connection.rollback()
        raise


__all__ = [
    "ReviewProblem",
    "issue_review_grant",
    "issue_sealed_review_grant",
    "list_completed_assessments",
]
