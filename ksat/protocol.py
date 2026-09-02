"""Versioned wire models and deterministic protocol helpers."""

import base64
import hashlib
import html
import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Any, Literal, Sequence
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROTOCOL_VERSION = 1
PACK_FORMAT_VERSION = 1
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
_CODE_TAG = re.compile(r"<\s*(?P<closing>/?)\s*code\b[^>]*>", re.IGNORECASE)
_MATH_FLOOR_DIVISION_CLASS_TOKEN = re.compile(
    re.escape(MATH_FLOOR_DIVISION_CLASS), re.IGNORECASE
)
_ORDINARY_CODE_CLOSE = re.compile(r"</\s*code\s*>\Z", re.IGNORECASE)
_MAX_MARKUP_DECODE_ROUNDS = 8


def _markup_structure_signature(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(match.group(0) for match in _CODE_TAG.finditer(value)),
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
    for token in _CODE_TAG.finditer(fragment):
        raw = token.group(0)
        if token.group("closing"):
            if not stack:
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            kind, start, body_start = stack.pop()
            if kind == "math":
                if raw != MATH_FLOOR_DIVISION_CLOSE:
                    raise ValueError(MATH_FLOOR_DIVISION_ERROR)
                canonical = canonicalize_math_floor_division_expression(
                    fragment[body_start:token.start()]
                )
                replacements.append((
                    start,
                    token.end(),
                    f"{MATH_FLOOR_DIVISION_OPEN}{canonical}{MATH_FLOOR_DIVISION_CLOSE}",
                ))
            elif _ORDINARY_CODE_CLOSE.fullmatch(raw) is None or _contains_normalized_double_slash(
                fragment[body_start:token.start()]
            ):
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            continue

        if stack:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if raw == MATH_FLOOR_DIVISION_OPEN:
            math_element_count += 1
            stack.append(("math", token.start(), token.end()))
            continue
        if _MATH_FLOOR_DIVISION_CLASS_TOKEN.search(raw) or raw.rstrip().endswith("/>"):
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        stack.append(("code", token.start(), token.end()))

    class_occurrences = tuple(_MATH_FLOOR_DIVISION_CLASS_TOKEN.finditer(fragment))
    if (
        stack
        or len(class_occurrences) != math_element_count
        or any(match.group(0) != MATH_FLOOR_DIVISION_CLASS for match in class_occurrences)
    ):
        raise ValueError(MATH_FLOOR_DIVISION_ERROR)
    for start, end, replacement in reversed(replacements):
        fragment = fragment[:start] + replacement + fragment[end:]
    return fragment


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
    pack_format_version: int = PACK_FORMAT_VERSION
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
