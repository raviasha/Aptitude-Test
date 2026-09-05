"""Versioned wire models and deterministic protocol helpers."""

import base64
import hashlib
import html
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Literal, Sequence
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROTOCOL_VERSION = 1
CURRENT_PACK_FORMAT_VERSION = 2
PACK_FORMAT_VERSION = CURRENT_PACK_FORMAT_VERSION
SUPPORTED_PACK_FORMAT_VERSIONS = frozenset({1, CURRENT_PACK_FORMAT_VERSION})
SHUFFLE_ALGORITHM = "sha256-rank-v1"
MATH_FLOOR_DIVISION_CLASS = "math-floor-division"
MATH_FLOOR_DIVISION_OPEN = '<code class="math-floor-division">'
MATH_FLOOR_DIVISION_CLOSE = "</code>"
MATH_FLOOR_DIVISION_ERROR = (
    "Question HTML floor division must use exact explicit math markup "
    '<code class="math-floor-division">LEFT // RIGHT</code> with simple operands.'
)

_MATH_OPERAND = r"(?:[A-Za-z_][A-Za-z0-9_]*|(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)"
_RAW_MATH_FLOOR_DIVISION = re.compile(rf"({_MATH_OPERAND}) // ({_MATH_OPERAND})\Z")
_CANONICAL_MATH_FLOOR_DIVISION = re.compile(
    rf"⌊({_MATH_OPERAND}) ÷ ({_MATH_OPERAND})⌋\Z"
)
_MATH_FLOOR_DIVISION_CLASS_TOKEN = re.compile(
    re.escape(MATH_FLOOR_DIVISION_CLASS), re.IGNORECASE
)
_ORDINARY_CODE_CLOSE = re.compile(r"</\s*code\s*>\Z", re.IGNORECASE)
_MAX_MARKUP_DECODE_ROUNDS = 8


@dataclass(frozen=True, slots=True)
class _CodeTag:
    raw: str
    start: int
    end: int
    closing: bool


def _iter_code_tags(value: str) -> Iterator[_CodeTag]:
    """Tokenize code tags in one forward pass and reject an open-ended tag."""

    length = len(value)
    position = 0
    while position < length:
        start = value.find("<", position)
        if start < 0:
            return
        cursor = start + 1
        while cursor < length and value[cursor].isspace():
            cursor += 1
        closing = cursor < length and value[cursor] == "/"
        if closing:
            cursor += 1
            while cursor < length and value[cursor].isspace():
                cursor += 1
        name_end = cursor + 4
        if (
            name_end > length
            or value[cursor:name_end].casefold() != "code"
            or (
                name_end < length
                and (value[name_end] == "_" or value[name_end].isalnum())
            )
        ):
            position = start + 1
            continue
        tag_end = value.find(">", name_end)
        if tag_end < 0:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        tag_end += 1
        yield _CodeTag(value[start:tag_end], start, tag_end, closing)
        position = tag_end


def _markup_structure_signature(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(token.raw for token in _iter_code_tags(value)),
        tuple(match.group(0) for match in _MATH_FLOOR_DIVISION_CLASS_TOKEN.finditer(value)),
    )


def _compact_markup_marker(value: str, marker: str) -> str:
    separated = re.compile(r"\s*".join(map(re.escape, marker)), re.IGNORECASE)
    return separated.sub(
        lambda match: "".join(character for character in match.group(0) if not character.isspace()),
        value,
    )


def _normalize_markup_detection(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    normalized = _compact_markup_marker(normalized, "code")
    return _compact_markup_marker(normalized, MATH_FLOOR_DIVISION_CLASS)


def _validate_stable_markup_structure(fragment: str) -> None:
    raw_signature = _markup_structure_signature(fragment)
    decoded = fragment
    for _ in range(_MAX_MARKUP_DECODE_ROUNDS):
        normalized = _normalize_markup_detection(decoded)
        if _markup_structure_signature(normalized) != raw_signature:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        # Entity and percent decoding are non-expanding relative to their encoded source.
        expanded = html.unescape(unquote(decoded))
        if _markup_structure_signature(expanded) != raw_signature:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if expanded == decoded:
            return
        decoded = expanded
    raise ValueError(MATH_FLOOR_DIVISION_ERROR)


def _contains_normalized_double_slash(value: str) -> bool:
    decoded = value
    for _ in range(8):
        expanded = html.unescape(unquote(decoded))
        if expanded == decoded:
            break
        decoded = expanded
    compact = "".join(
        character
        for character in decoded
        if not character.isspace()
        and not unicodedata.category(character).startswith("C")
    )
    return "//" in compact


def canonicalize_math_floor_division_expression(value: str) -> str:
    """Return the sole canonical rendering of a supported floor-division expression."""
    match = _RAW_MATH_FLOOR_DIVISION.fullmatch(value)
    if match is None:
        match = _CANONICAL_MATH_FLOOR_DIVISION.fullmatch(value)
    if match is None or any(len(operand) > 64 for operand in match.groups()):
        raise ValueError(MATH_FLOOR_DIVISION_ERROR)
    left, right = match.groups()
    return f"⌊{left} ÷ {right}⌋"


def canonicalize_math_floor_division_markup(fragment: str) -> str:
    """Validate exact explicit-math source and replace expressions with canonical Unicode."""
    _validate_stable_markup_structure(fragment)

    stack: list[tuple[str, int, int]] = []
    replacements: list[tuple[int, int, str]] = []
    math_element_count = 0
    for token in _iter_code_tags(fragment):
        raw = token.raw
        if token.closing:
            if not stack:
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            kind, start, body_start = stack.pop()
            if kind == "math":
                if raw != MATH_FLOOR_DIVISION_CLOSE:
                    raise ValueError(MATH_FLOOR_DIVISION_ERROR)
                canonical = canonicalize_math_floor_division_expression(
                    fragment[body_start:token.start]
                )
                replacements.append((
                    start,
                    token.end,
                    f"{MATH_FLOOR_DIVISION_OPEN}{canonical}{MATH_FLOOR_DIVISION_CLOSE}",
                ))
            elif _ORDINARY_CODE_CLOSE.fullmatch(raw) is None or _contains_normalized_double_slash(
                fragment[body_start:token.start]
            ):
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            continue

        if stack:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if raw == MATH_FLOOR_DIVISION_OPEN:
            math_element_count += 1
            stack.append(("math", token.start, token.end))
            continue
        if _MATH_FLOOR_DIVISION_CLASS_TOKEN.search(raw) or raw.rstrip().endswith("/>"):
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        stack.append(("code", token.start, token.end))

    class_occurrences = tuple(_MATH_FLOOR_DIVISION_CLASS_TOKEN.finditer(fragment))
    if (
        stack
        or len(class_occurrences) != math_element_count
        or any(match.group(0) != MATH_FLOOR_DIVISION_CLASS for match in class_occurrences)
    ):
        raise ValueError(MATH_FLOOR_DIVISION_ERROR)
    if not replacements:
        return fragment
    canonical_parts: list[str] = []
    previous_end = 0
    for start, end, replacement in replacements:
        canonical_parts.extend((fragment[previous_end:start], replacement))
        previous_end = end
    canonical_parts.append(fragment[previous_end:])
    return "".join(canonical_parts)


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceEnrollmentRequest(BaseModel):
    label: str
    public_key_b64: str
    enrollment_code: str


class DeviceEnrollmentReceipt(BaseModel):
    device_id: str
    coordinator_public_key_b64: str


class ClientLoginRequest(BaseModel):
    student_id: str
    password: str
    device_id: str


class ClientSession(BaseModel):
    access_token: str
    student_id: str
    student_name: str
    device_id: str
    expires_in_seconds: int = 43_200


_PUBLIC_ASSET_URL = re.compile(r"assets/[0-9a-f]{64}\.(?:png|jpe?g|webp|svg)\Z")
_PUBLIC_OPTION_KEYS = frozenset(("A", "B", "C", "D", "E"))
PublicScalar = str | int | float | bool | None
_PUBLIC_PRIVATE_MARKERS = ("answer", "correct", "feedback", "score", "solution", "explanation")
_MAX_PUBLIC_STRUCTURE_DEPTH = 8
_MAX_PUBLIC_COLLECTION_ITEMS = 10_000
_MAX_PUBLIC_STRUCTURE_NODES = 50_000


def _normalized_public_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _validate_public_structure(value: Any) -> None:
    budget = [_MAX_PUBLIC_STRUCTURE_NODES]

    def visit(item: Any, depth: int) -> None:
        budget[0] -= 1
        if budget[0] < 0 or depth > _MAX_PUBLIC_STRUCTURE_DEPTH:
            raise ValueError("Public structured stimulus is too deeply nested or large.")
        if isinstance(item, dict):
            if len(item) > _MAX_PUBLIC_COLLECTION_ITEMS:
                raise ValueError("Public structured stimulus collection is too large.")
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError("Public structured stimulus keys must be strings.")
                normalized = _normalized_public_key(key)
                if any(marker in normalized for marker in _PUBLIC_PRIVATE_MARKERS):
                    raise ValueError("Private assessment fields are not allowed in public stimuli.")
                visit(nested, depth + 1)
            return
        if isinstance(item, list):
            if len(item) > _MAX_PUBLIC_COLLECTION_ITEMS:
                raise ValueError("Public structured stimulus collection is too large.")
            for nested in item:
                visit(nested, depth + 1)
            return
        if item is None or isinstance(item, (str, int, bool)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        raise ValueError("Public structured stimulus contains an unsupported value.")

    visit(value, 0)


class PublicMediaItem(ProtocolModel):
    url: str
    alt_text: str
    width: int = Field(gt=0, le=10_000)
    height: int = Field(gt=0, le=10_000)

    @field_validator("url")
    @classmethod
    def embedded_url_only(cls, value: str) -> str:
        if not _PUBLIC_ASSET_URL.fullmatch(value):
            raise ValueError("Public media URLs must name embedded pack assets.")
        return value


class PublicDisplayMedia(ProtocolModel):
    question: PublicMediaItem | None = None
    options: dict[str, PublicMediaItem] = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def option_media_keys(cls, value: dict[str, PublicMediaItem]) -> dict[str, PublicMediaItem]:
        if set(value) - _PUBLIC_OPTION_KEYS:
            raise ValueError("Public option media must use option keys A-E.")
        return value


class PublicImageStimulus(ProtocolModel):
    id: str
    type: Literal["image"]
    title: str = ""
    alt_text: str = ""
    url: str

    @field_validator("url")
    @classmethod
    def embedded_url_only(cls, value: str) -> str:
        if not _PUBLIC_ASSET_URL.fullmatch(value):
            raise ValueError("Public stimulus URLs must name embedded pack assets.")
        return value


class PublicChartSeries(ProtocolModel):
    model_config = ConfigDict(extra="allow")

    name: str = ""
    label: str = ""
    values: list[PublicScalar] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def bounded_public_content(cls, value: Any) -> Any:
        _validate_public_structure(value)
        return value


class PublicChartContent(ProtocolModel):
    model_config = ConfigDict(extra="allow")

    chart_type: Literal["bar", "line"] | None = None
    kind: Literal["bar", "line"] | None = None
    labels: list[PublicScalar] | None = None
    series: list[PublicChartSeries] | None = None
    values: list[PublicScalar] | None = None

    @model_validator(mode="before")
    @classmethod
    def bounded_public_content(cls, value: Any) -> Any:
        _validate_public_structure(value)
        return value


class PublicChartStimulus(ProtocolModel):
    id: str
    type: Literal["chart"]
    title: str = ""
    alt_text: str = ""
    content: PublicChartContent


class PublicTableContent(ProtocolModel):
    model_config = ConfigDict(extra="allow")

    columns: list[PublicScalar] | None = None
    rows: list[list[PublicScalar]] | None = None

    @model_validator(mode="before")
    @classmethod
    def bounded_public_content(cls, value: Any) -> Any:
        _validate_public_structure(value)
        return value


class PublicTableStimulus(ProtocolModel):
    id: str
    type: Literal["table"]
    title: str = ""
    alt_text: str = ""
    content: PublicTableContent


PublicStimulus = PublicImageStimulus | PublicChartStimulus | PublicTableStimulus


class PublicQuestion(ProtocolModel):
    question_id: int
    source_key: str
    category: str
    chapter: str
    difficulty: str
    question_text: str
    question_html: str = ""
    options: dict[str, str]
    stimulus: PublicStimulus | None = None
    display_media: PublicDisplayMedia = Field(default_factory=PublicDisplayMedia)

    @field_validator("options")
    @classmethod
    def public_option_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) not in (set("ABCD"), set("ABCDE")) or any(not item.strip() for item in value.values()):
            raise ValueError("Public questions require non-empty A-D options with optional E.")
        return value


AnswerKey = Literal["A", "B", "C", "D", "E"]


def _canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID.") from exc
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID.")
    return value


def _sha256_hex(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Content hash must be a lowercase SHA-256 value.")
    return value


def _base64_key(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Review keys must be valid base64.") from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("Review keys must encode exactly 32 bytes.")
    return value


class FrozenReviewQuestion(ProtocolModel):
    question_id: int = Field(gt=0)
    correct_answer: AnswerKey
    solution_steps: list[str] = Field(min_length=1, max_length=100)

    @field_validator("solution_steps")
    @classmethod
    def non_blank_steps(cls, value: list[str]) -> list[str]:
        if any(not step.strip() or len(step) > 10_000 for step in value):
            raise ValueError("Review solution steps must be non-empty bounded strings.")
        return value


class ReviewContent(ProtocolModel):
    questions: list[FrozenReviewQuestion] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_questions(self) -> "ReviewContent":
        identifiers = [item.question_id for item in self.questions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Review question identifiers must be unique.")
        return self


class ReviewResponseEntry(ProtocolModel):
    question_id: int = Field(gt=0)
    question_order: int = Field(ge=0)
    selected_answer: AnswerKey | None = None


class CompletedAssessmentSummary(ProtocolModel):
    attempt_id: str
    release_id: str
    test_id: int = Field(gt=0)
    test_name: str = Field(min_length=1, max_length=200)
    accepted_at: datetime
    score: int = Field(ge=0)
    total_questions: int = Field(gt=0, le=500)
    percentage: float = Field(ge=0, le=100)
    review_state: Literal["waiting", "available", "unavailable"]

    @field_validator("attempt_id")
    @classmethod
    def valid_attempt_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Attempt identifier")

    @field_validator("release_id")
    @classmethod
    def valid_release_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Release identifier")

    @field_validator("accepted_at")
    @classmethod
    def aware_accepted_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Accepted time must include a timezone.")
        return value

    @field_validator("percentage")
    @classmethod
    def finite_percentage(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Percentage must be finite.")
        return value


class AssessmentReviewGrant(ProtocolModel):
    attempt_id: str
    student_id: str = Field(min_length=1, max_length=100)
    release_id: str
    content_hash: str
    content_key_b64: str
    review_key_b64: str
    responses: list[ReviewResponseEntry] = Field(min_length=1, max_length=500)

    @field_validator("attempt_id")
    @classmethod
    def valid_attempt_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Attempt identifier")

    @field_validator("release_id")
    @classmethod
    def valid_release_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Release identifier")

    @field_validator("content_hash")
    @classmethod
    def valid_content_hash(cls, value: str) -> str:
        return _sha256_hex(value)

    @field_validator("content_key_b64", "review_key_b64")
    @classmethod
    def valid_keys(cls, value: str) -> str:
        return _base64_key(value)

    @model_validator(mode="after")
    def ordered_unique_responses(self) -> "AssessmentReviewGrant":
        identifiers = [item.question_id for item in self.responses]
        orders = [item.question_order for item in self.responses]
        if len(identifiers) != len(set(identifiers)) or sorted(orders) != list(range(len(orders))):
            raise ValueError("Review responses must have unique questions and contiguous order.")
        return self


class ReviewedQuestion(ProtocolModel):
    question: PublicQuestion
    selected_answer: AnswerKey | None = None
    correct_answer: AnswerKey
    solution_steps: list[str] = Field(min_length=1, max_length=100)

    @field_validator("solution_steps")
    @classmethod
    def non_blank_steps(cls, value: list[str]) -> list[str]:
        if any(not step.strip() or len(step) > 10_000 for step in value):
            raise ValueError("Review solution steps must be non-empty bounded strings.")
        return value


class AssessmentReview(ProtocolModel):
    attempt_id: str
    release_id: str
    test_name: str = Field(min_length=1, max_length=200)
    questions: list[ReviewedQuestion] = Field(min_length=1, max_length=500)

    @field_validator("attempt_id")
    @classmethod
    def valid_attempt_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Attempt identifier")

    @field_validator("release_id")
    @classmethod
    def valid_release_id(cls, value: str) -> str:
        return _canonical_uuid(value, "Release identifier")


class ReleaseSummary(ProtocolModel):
    release_id: str
    test_id: int
    state: str
    duration_seconds: int
    canonical_question_ids: list[int]
    content_pack_filename: str
    content_hash: str
    content_signature_b64: str
    wrapped_content_key_b64: str


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
    pack_format_version: int = CURRENT_PACK_FORMAT_VERSION
    release_id: str
    test_id: int
    test_name: str
    duration_seconds: int
    canonical_question_ids: list[int]
    asset_names: list[str] = Field(default_factory=list)


class PublicReleaseDescriptor(ProtocolModel):
    """Public signed release metadata safe to send before attempt start."""

    release_id: str
    test_id: int
    state: str
    duration_seconds: int
    canonical_question_ids: list[int]
    content_pack_filename: str
    content_hash: str
    content_signature_b64: str
    manifest: ReleaseManifest


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
    duration_extension_seconds: int = Field(
        default=0, ge=0, le=86_400, exclude_if=lambda value: value == 0
    )
    shuffle_algorithm: str = SHUFFLE_ALGORITHM


class SignedAttemptTicket(ProtocolModel):
    ticket: AttemptTicket
    signature_b64: str


class AttemptStartResponse(ProtocolModel):
    ticket: SignedAttemptTicket
    canonical_question_ids: list[int]
    server_time: datetime


class AttemptDeadlineUpdate(ProtocolModel):
    """Answer-free, signed authorization to extend one active local timer."""

    protocol_version: int = PROTOCOL_VERSION
    attempt_id: str
    release_id: str
    device_id: str
    base_deadline: datetime
    prior_deadline: datetime
    deadline: datetime
    cumulative_extension_seconds: int = Field(ge=1, le=86_400)
    revision: int = Field(ge=1)
    issued_at: datetime


class SignedAttemptDeadlineUpdate(ProtocolModel):
    update: AttemptDeadlineUpdate
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
