"""Versioned wire models and deterministic protocol helpers."""

import base64
import hashlib
import json
from datetime import datetime
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field


PROTOCOL_VERSION = 1
PACK_FORMAT_VERSION = 1
SHUFFLE_ALGORITHM = "sha256-rank-v1"


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def canonical_json(value: BaseModel | dict[str, Any]) -> bytes:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else (
        value.dict() if isinstance(value, BaseModel) else value
    )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def deterministic_question_order(
    question_ids: Sequence[int], seed_b64: str, algorithm: str = SHUFFLE_ALGORITHM
) -> list[int]:
    if algorithm != SHUFFLE_ALGORITHM:
        raise ValueError(f"Unsupported shuffle algorithm: {algorithm}")
    seed = base64.b64decode(seed_b64, validate=True)
    if len(seed) != 32 or len(set(question_ids)) != len(question_ids):
        raise ValueError("A 32-byte seed and unique question IDs are required.")
    return sorted(question_ids, key=lambda item: hashlib.sha256(seed + str(item).encode("ascii")).digest())


def device_request_bytes(method: str, path: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{body_hash}\n{timestamp}\n{nonce}".encode("utf-8")


class ReleaseManifest(ProtocolModel):
    protocol_version: int = PROTOCOL_VERSION
    pack_format_version: int = PACK_FORMAT_VERSION
    release_id: str
    test_id: int
    test_name: str
    duration_seconds: int
    canonical_question_ids: list[int]
    asset_names: list[str] = Field(default_factory=list)


class AttemptTicket(ProtocolModel):
    protocol_version: int = PROTOCOL_VERSION
    attempt_id: str
    student_id: str
    device_id: str
    release_id: str
    content_hash: str
    started_at: datetime
    deadline: datetime
    order_seed_b64: str
    content_key_b64: str
    shuffle_algorithm: str = SHUFFLE_ALGORITHM


class SignedAttemptTicket(ProtocolModel):
    ticket: AttemptTicket
    signature_b64: str


class ResponseEntry(ProtocolModel):
    question_id: int
    selected_answer: str | None


class IntegrityEvent(ProtocolModel):
    event_type: str
    occurred_at: datetime


class ResponseBundle(ProtocolModel):
    protocol_version: int = PROTOCOL_VERSION
    ticket: SignedAttemptTicket
    content_hash: str
    sealed_at: datetime
    responses: list[ResponseEntry]
    integrity_events: list[IntegrityEvent] = Field(default_factory=list)


class SignedResponseBundle(ProtocolModel):
    bundle: ResponseBundle
    device_signature_b64: str


class SubmissionReceipt(ProtocolModel):
    attempt_id: str
    accepted_at: datetime
    score: int
    total_questions: int
    attempted: int
    percentage: float
    violations: int


class ApiProblem(ProtocolModel):
    code: str
    message: str
    retryable: bool = False
