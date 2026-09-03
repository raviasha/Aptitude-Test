"""Network-independent assessment execution for a managed lab client."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ksat.client.identity import DeviceIdentity
from ksat.client.store import AttemptSealedError, ClientStore, LocalAttemptRecord
from ksat.crypto import decrypt_pack, sha256_hex, sign_json, verify_json
from ksat.protocol import (
    SignedAttemptDeadlineUpdate,
    PACK_FORMAT_VERSION,
    PROTOCOL_VERSION,
    AttemptStartResponse,
    PublicQuestion,
    PublicReleaseDescriptor,
    ReleaseManifest,
    ReleaseSummary,
    ResponseBundle,
    ResponseEntry,
    SignedResponseBundle,
    canonical_json,
    deterministic_question_order,
)


_FORBIDDEN_PUBLIC_KEYS = frozenset(
    ("correctanswer", "solution", "solutionsteps", "optionexplanations", "feedback")
)
_ASSET_NAME = re.compile(r"assets/([0-9a-f]{64})\.(?:png|jpe?g|webp|svg)\Z")
_PUBLIC_ASSET_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
_MAX_PUBLIC_ASSET_BYTES = 25 * 1024 * 1024
_MAX_PUBLIC_ASSETS_TOTAL_BYTES = 64 * 1024 * 1024


class Clock(Protocol):
    def utcnow(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    state: str
    question_order: tuple[int, ...]
    responses: dict[int, str | None]
    remaining_seconds: int
    violations: int
    current_question_id: int


@dataclass(frozen=True)
class PublicAsset:
    """Immutable, verified public media from the active attempt's signed pack."""

    reference: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class _PreparedPack:
    descriptor: PublicReleaseDescriptor
    path: Path


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _strict_json(raw: bytes, label: str) -> Any:
    def object_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate key in {label}.")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON.")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Assessment pack {label} is invalid.") from error


def _reject_answer_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("Assessment pack public keys must be strings.")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError("Assessment pack contains prohibited private metadata.")
            _reject_answer_metadata(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_answer_metadata(nested)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Assessment pack contains a non-finite value.")


class AssessmentRuntime:
    """Runs one local attempt without placing answer changes on the network path."""

    def __init__(
        self,
        store: ClientStore,
        identity: DeviceIdentity,
        clock: Clock | None = None,
    ):
        if not identity.device_id or not identity.coordinator_public_key_b64:
            raise ValueError("An enrolled device identity is required.")
        self.store = store
        self.identity = identity
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._prepared: dict[str, _PreparedPack] = {}
        self._attempt_id: str | None = None
        self._questions: dict[int, PublicQuestion] = {}
        self._assets: dict[str, PublicAsset] = {}
        self._anchor_monotonic: float | None = None
        self._anchor_remaining = 0.0
        self._anchor_trusted_wall: datetime | None = None
        self._last_checkpoint_monotonic: float | None = None
        self._last_observed_monotonic: float | None = None

    def prepare(
        self,
        release: PublicReleaseDescriptor | ReleaseSummary,
        manifest_or_path: ReleaseManifest | Path,
        pack_path: Path | None = None,
    ) -> Path:
        """Authenticate a complete encrypted artifact before marking it verified."""
        if isinstance(release, PublicReleaseDescriptor):
            if pack_path is not None or isinstance(manifest_or_path, ReleaseManifest):
                raise TypeError("Public descriptor preparation requires one pack path.")
            descriptor = release
            path = Path(manifest_or_path).resolve()
        elif isinstance(release, ReleaseSummary):
            if not isinstance(manifest_or_path, ReleaseManifest) or pack_path is None:
                raise TypeError("Legacy release preparation requires a manifest and pack path.")
            descriptor = PublicReleaseDescriptor(
                release_id=release.release_id,
                test_id=release.test_id,
                state=release.state,
                duration_seconds=release.duration_seconds,
                canonical_question_ids=release.canonical_question_ids,
                content_pack_filename=release.content_pack_filename,
                content_hash=release.content_hash,
                content_signature_b64=release.content_signature_b64,
                manifest=manifest_or_path,
            )
            path = Path(pack_path).resolve()
        else:
            raise TypeError("Assessment release descriptor is invalid.")
        manifest = descriptor.manifest
        if not path.is_file():
            raise ValueError("Assessment content pack is unavailable.")
        if (
            descriptor.release_id != manifest.release_id
            or manifest.protocol_version != PROTOCOL_VERSION
            or manifest.pack_format_version != PACK_FORMAT_VERSION
            or descriptor.test_id != manifest.test_id
            or descriptor.duration_seconds != manifest.duration_seconds
            or descriptor.canonical_question_ids != manifest.canonical_question_ids
            or descriptor.content_pack_filename != f"{descriptor.release_id}.ksatpack"
            or descriptor.state not in {"prepared", "launched"}
        ):
            raise ValueError("Assessment release metadata is inconsistent.")
        encrypted = path.read_bytes()
        if sha256_hex(encrypted) != descriptor.content_hash:
            raise ValueError("Assessment content hash does not match.")
        verify_json(
            self.identity.coordinator_public_key_b64,
            {
                "release_id": descriptor.release_id,
                "content_hash": descriptor.content_hash,
                "manifest": manifest.model_dump(mode="json"),
            },
            descriptor.content_signature_b64,
        )
        self.store.cache_pack(
            descriptor.release_id,
            descriptor.content_hash,
            path,
            verified=True,
            cached_at=_utc(self.clock.utcnow(), "Cache time"),
        )
        with self._lock:
            self._prepared[descriptor.release_id] = _PreparedPack(descriptor, path)
        return path

    def start(
        self,
        response: AttemptStartResponse,
        *,
        student_id: str,
    ) -> AttemptSnapshot:
        with self._lock:
            received_monotonic = self._monotonic()
            signed = response.ticket
            ticket = signed.ticket
            verify_json(
                self.identity.coordinator_public_key_b64,
                ticket,
                signed.signature_b64,
            )
            if (
                ticket.protocol_version != PROTOCOL_VERSION
                or ticket.device_id != self.identity.device_id
                or ticket.student_id != student_id
                or not isinstance(student_id, str)
                or not student_id.strip()
            ):
                raise ValueError("Attempt ticket binding does not match this session and device.")
            started = _utc(ticket.started_at, "Attempt start")
            deadline = _utc(ticket.deadline, "Attempt deadline")
            server_time = _utc(response.server_time, "Coordinator time")
            duration = (deadline - started).total_seconds()
            if (
                duration <= 0
                or not duration.is_integer()
                or not started <= server_time <= deadline
            ):
                raise ValueError("Attempt ticket timing is invalid.")
            prepared = self._prepared.get(ticket.release_id)
            pack_path = self.store.verified_pack(ticket.release_id, ticket.content_hash)
            if prepared is None or pack_path is None:
                raise ValueError("The exact verified assessment content is not prepared.")
            if (
                prepared.descriptor.content_hash != ticket.content_hash
                or prepared.descriptor.manifest.duration_seconds
                + ticket.duration_extension_seconds
                != int(duration)
                or response.canonical_question_ids
                != prepared.descriptor.manifest.canonical_question_ids
            ):
                raise ValueError("Attempt ticket does not match the prepared assessment release.")
            questions, manifest, assets = self._open_pack(
                pack_path,
                ticket.release_id,
                ticket.content_hash,
                ticket.content_key_b64,
            )
            if manifest != prepared.descriptor.manifest:
                raise ValueError("Assessment pack manifest does not match its signed metadata.")
            order = deterministic_question_order(
                manifest.canonical_question_ids,
                ticket.order_seed_b64,
                ticket.shuffle_algorithm,
            )
            wall_now = _utc(self.clock.utcnow(), "Local start time")
            activated_monotonic = self._monotonic()
            processing_elapsed = activated_monotonic - received_monotonic
            initial_remaining = min(
                max(0, math.floor(duration - processing_elapsed)),
                max(
                    0,
                    math.floor(
                        (deadline - server_time).total_seconds()
                        - processing_elapsed
                    ),
                ),
                max(0, math.floor((deadline - wall_now).total_seconds())),
            )
            record = self.store.start_or_resume_attempt(
                signed,
                order,
                remaining_seconds=initial_remaining,
                created_at=started,
                last_wall_time=wall_now,
            )
            if record.state != "in_progress":
                self._activate(record, questions, assets)
                return self._snapshot_record(record)
            if (
                self._attempt_id == record.attempt_id
                and self._anchor_monotonic is not None
            ):
                return self.snapshot()
            resumed_wall = _utc(self.clock.utcnow(), "Local start completion time")
            resumed_monotonic = self._monotonic()
            safe_remaining = min(
                record.remaining_seconds,
                max(
                    0,
                    math.floor(
                        record.remaining_seconds
                        - (resumed_monotonic - activated_monotonic)
                    ),
                ),
                max(0, math.floor((record.deadline - resumed_wall).total_seconds())),
            )
            if safe_remaining < record.remaining_seconds:
                self.store.update_timer_checkpoint(
                    record.attempt_id,
                    safe_remaining,
                    last_wall_time=resumed_wall,
                )
                record = self.store.load_attempt(record.attempt_id)
            self._activate(
                record,
                questions,
                assets,
                trusted_wall=record.deadline - timedelta(seconds=safe_remaining),
                anchor_monotonic=resumed_monotonic,
            )
            if safe_remaining == 0:
                return self._seal(record)
            return self._snapshot_record(record, remaining=safe_remaining)

    def answer(self, question_id: int, selected_answer: str | None) -> AttemptSnapshot:
        with self._lock:
            record = self._current_record()
            if record.state != "in_progress":
                raise AttemptSealedError("The attempt is sealed and cannot be changed.")
            question = self._questions.get(question_id)
            if question is None:
                raise ValueError("Question is not part of this assessment release.")
            if selected_answer is not None and selected_answer not in question.options:
                raise ValueError("Selected answer is not an option for this question.")
            remaining = self._remaining()
            if remaining == 0:
                self._seal(record)
                raise AttemptSealedError("The assessment time has expired.")
            now = self._trusted_now(record)
            self.store.update_timer_checkpoint(
                record.attempt_id, remaining, last_wall_time=now
            )
            self._last_checkpoint_monotonic = self._monotonic()
            self.store.save_answer(
                record.attempt_id, question_id, selected_answer, saved_at=now
            )
            return self._snapshot_record(self.store.load_attempt(record.attempt_id))

    def position(self, question_id: int) -> AttemptSnapshot:
        with self._lock:
            record = self._current_record()
            if record.state != "in_progress":
                raise AttemptSealedError("The attempt is sealed and cannot be changed.")
            if type(question_id) is not int or question_id not in record.question_order:
                raise ValueError("Question is not part of this assessment release.")
            remaining = self._remaining()
            if remaining == 0:
                self._seal(record)
                raise AttemptSealedError("The assessment time has expired.")
            now = self._trusted_now(record)
            self.store.update_timer_checkpoint(
                record.attempt_id, remaining, last_wall_time=now
            )
            self._last_checkpoint_monotonic = self._monotonic()
            self.store.save_position(record.attempt_id, question_id)
            return self._snapshot_record(self.store.load_attempt(record.attempt_id))

    def record_violation(self, event_type: str) -> AttemptSnapshot:
        with self._lock:
            record = self._current_record()
            if record.state != "in_progress":
                raise AttemptSealedError("The attempt is sealed and cannot be changed.")
            remaining = self._remaining()
            if remaining == 0:
                return self._seal(record)
            occurred_at = self._trusted_now(record)
            self.store.update_timer_checkpoint(
                record.attempt_id, remaining, last_wall_time=occurred_at
            )
            self._last_checkpoint_monotonic = self._monotonic()
            prior = self.store.integrity_events(record.attempt_id)
            matching = next(
                (event for event in reversed(prior) if event.event_type == event_type), None
            )
            if matching is None or (occurred_at - matching.occurred_at).total_seconds() >= 2:
                self.store.record_integrity_event(
                    record.attempt_id, event_type, occurred_at=occurred_at
                )
            return self._snapshot_record(self.store.load_attempt(record.attempt_id))

    def question(self, question_id: int) -> PublicQuestion:
        try:
            return self._questions[question_id]
        except KeyError as error:
            raise KeyError(f"Question is not loaded: {question_id}") from error

    def public_asset(self, attempt_id: str, reference: str) -> PublicAsset:
        """Return only verified public bytes bound to the exact active attempt."""
        with self._lock:
            if not isinstance(attempt_id, str) or attempt_id != self._attempt_id:
                raise ValueError("Public asset does not belong to the active attempt.")
            if not isinstance(reference, str) or _ASSET_NAME.fullmatch(reference) is None:
                raise ValueError("Public asset reference is invalid.")
            record = self._current_record()
            if record.attempt_id != attempt_id:
                raise ValueError("Public asset does not belong to the active attempt.")
            try:
                return self._assets[reference]
            except KeyError as error:
                raise KeyError("Public asset is not part of the active attempt.") from error

    def snapshot(self) -> AttemptSnapshot:
        with self._lock:
            record = self._current_record()
            if record.state == "in_progress":
                remaining = self._remaining()
                if remaining == 0:
                    return self._seal(record)
                if self._checkpoint_due():
                    now = self._trusted_now(record)
                    self.store.update_timer_checkpoint(
                        record.attempt_id, remaining, last_wall_time=now
                    )
                    self._last_checkpoint_monotonic = self._monotonic()
                    record = self.store.load_attempt(record.attempt_id)
                return self._snapshot_record(record, remaining=remaining)
            return self._snapshot_record(record)

    def tick(self) -> AttemptSnapshot:
        return self.snapshot()

    def submit(self) -> AttemptSnapshot:
        with self._lock:
            record = self._current_record()
            if record.state != "in_progress":
                return self._snapshot_record(record)
            return self._seal(record)

    def recover(self) -> AttemptSnapshot | None:
        with self._lock:
            record = self.store.active_attempt()
            if record is None:
                self._clear_active()
                return None
            ticket = record.ticket.ticket
            verify_json(
                self.identity.coordinator_public_key_b64,
                ticket,
                record.ticket.signature_b64,
            )
            if record.deadline_revision > 0:
                if record.deadline_update is None:
                    raise ValueError("Local attempt deadline authorization is invalid.")
                verify_json(
                    self.identity.coordinator_public_key_b64,
                    record.deadline_update.update,
                    record.deadline_update.signature_b64,
                )
            if ticket.device_id != self.identity.device_id:
                raise ValueError("Local attempt cannot be recovered on this device.")
            questions, manifest, assets = self._open_pack(
                self._verified_attempt_pack(record),
                ticket.release_id,
                ticket.content_hash,
                ticket.content_key_b64,
            )
            if (
                manifest.release_id != ticket.release_id
                or manifest.canonical_question_ids != list(questions)
                or manifest.duration_seconds
                + ticket.duration_extension_seconds
                != int((ticket.deadline - ticket.started_at).total_seconds())
                or record.question_order
                != tuple(
                    deterministic_question_order(
                        manifest.canonical_question_ids,
                        ticket.order_seed_b64,
                        ticket.shuffle_algorithm,
                    )
                )
            ):
                raise ValueError("Local attempt cannot be recovered on this device.")
            if record.state != "in_progress":
                self._activate(record, questions, assets)
                return self._snapshot_record(record)
            wall_now = _utc(self.clock.utcnow(), "Recovery time")
            wall_remaining = max(
                0, math.floor((record.deadline - wall_now).total_seconds())
            )
            safe_remaining = min(record.remaining_seconds, wall_remaining)
            if safe_remaining < record.remaining_seconds:
                self.store.update_timer_checkpoint(
                    record.attempt_id, safe_remaining, last_wall_time=wall_now
                )
                record = self.store.load_attempt(record.attempt_id)
            trusted_wall = record.deadline - timedelta(seconds=safe_remaining)
            self._activate(record, questions, assets, trusted_wall=trusted_wall)
            if safe_remaining == 0:
                return self._seal(record)
            return self._snapshot_record(record, remaining=safe_remaining)

    def apply_deadline_update(
        self, signed_update: SignedAttemptDeadlineUpdate
    ) -> AttemptSnapshot:
        """Apply one coordinator-signed monotonic timer extension locally."""

        with self._lock:
            record = self._current_record()
            if record.state != "in_progress":
                raise AttemptSealedError("Sealed attempts cannot be extended.")
            update = signed_update.update
            verify_json(
                self.identity.coordinator_public_key_b64,
                update,
                signed_update.signature_b64,
            )
            ticket = record.ticket.ticket
            if (
                update.protocol_version != PROTOCOL_VERSION
                or update.attempt_id != record.attempt_id
                or update.release_id != record.release_id
                or update.device_id != self.identity.device_id
                or update.device_id != ticket.device_id
                or _utc(update.base_deadline, "Deadline update base")
                   != _utc(ticket.deadline, "Attempt deadline")
                or _utc(update.deadline, "Deadline update deadline")
                   != _utc(ticket.deadline, "Attempt deadline")
                      + timedelta(seconds=update.cumulative_extension_seconds)
                or _utc(update.deadline, "Deadline update deadline") < record.deadline
            ):
                raise ValueError("Deadline update does not match this attempt and device.")
            if (
                record.deadline_revision == update.revision
                and record.deadline_update == signed_update
            ):
                return self.snapshot()
            remaining = self._remaining()
            if remaining == 0:
                return self._seal(record)
            trusted_now = self._trusted_now(record)
            updated = self.store.apply_deadline_update(
                record.attempt_id,
                signed_update,
                remaining_before=remaining,
                last_wall_time=trusted_now,
            )
            self._activate(updated, self._questions, self._assets, trusted_wall=trusted_now)
            return self._snapshot_record(updated, remaining=updated.remaining_seconds)

    def _open_pack(
        self,
        path: Path,
        release_id: str,
        content_hash: str,
        content_key_b64: str,
    ) -> tuple[dict[int, PublicQuestion], ReleaseManifest, dict[str, PublicAsset]]:
        encrypted = Path(path).read_bytes()
        if sha256_hex(encrypted) != content_hash:
            raise ValueError("Cached assessment content hash does not match.")
        try:
            key = base64.b64decode(content_key_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError("Attempt content key is invalid.") from error
        plaintext = decrypt_pack(key, release_id, encrypted)
        try:
            with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
                infos = archive.infolist()
                names = [item.filename for item in infos]
                if (
                    len(names) != len(set(names))
                    or any(item.is_dir() for item in infos)
                    or names[:2] != ["manifest.json", "questions.json"]
                ):
                    raise ValueError("Assessment pack entries are invalid.")
                manifest_raw = archive.read("manifest.json")
                questions_raw = archive.read("questions.json")
                manifest_value = _strict_json(manifest_raw, "manifest")
                questions_value = _strict_json(questions_raw, "questions")
                _reject_answer_metadata(questions_value)
                manifest = ReleaseManifest.model_validate(manifest_value, strict=True)
                if canonical_json(manifest) != manifest_raw:
                    raise ValueError("Assessment pack manifest is not canonical.")
                if not isinstance(questions_value, list):
                    raise ValueError("Assessment pack questions are invalid.")
                question_list = [
                    PublicQuestion.model_validate(item, strict=True)
                    for item in questions_value
                ]
                canonical_questions = canonical_json(
                    [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in question_list
                    ]
                )
                legacy_canonical_questions = canonical_json(
                    [item.model_dump(mode="json") for item in question_list]
                )
                if questions_raw not in {
                    canonical_questions,
                    legacy_canonical_questions,
                }:
                    raise ValueError("Assessment pack questions are not canonical.")
                if names != ["manifest.json", "questions.json", *manifest.asset_names]:
                    raise ValueError("Assessment pack entries do not match its manifest.")
                if manifest.asset_names != sorted(set(manifest.asset_names)):
                    raise ValueError("Assessment pack asset list is not canonical.")
                referenced_assets: set[str] = set()
                for question in question_list:
                    if question.stimulus is not None and question.stimulus.type == "image":
                        referenced_assets.add(question.stimulus.url)
                    if question.display_media.question is not None:
                        referenced_assets.add(question.display_media.question.url)
                    referenced_assets.update(
                        media.url for media in question.display_media.options.values()
                    )
                if referenced_assets != set(manifest.asset_names):
                    raise ValueError("Assessment pack asset references do not match its manifest.")
                assets: dict[str, PublicAsset] = {}
                total_asset_bytes = 0
                for name in manifest.asset_names:
                    match = _ASSET_NAME.fullmatch(name)
                    suffix = Path(name).suffix
                    info = archive.getinfo(name)
                    if (
                        match is None
                        or suffix not in _PUBLIC_ASSET_MIME
                        or info.file_size < 0
                        or info.file_size > _MAX_PUBLIC_ASSET_BYTES
                    ):
                        raise ValueError("Assessment pack asset is invalid.")
                    total_asset_bytes += info.file_size
                    if total_asset_bytes > _MAX_PUBLIC_ASSETS_TOTAL_BYTES:
                        raise ValueError("Assessment pack assets are too large.")
                    content = bytes(archive.read(name))
                    if len(content) != info.file_size or hashlib.sha256(content).hexdigest() != match.group(1):
                        raise ValueError("Assessment pack asset is invalid.")
                    assets[name] = PublicAsset(
                        reference=name,
                        media_type=_PUBLIC_ASSET_MIME[suffix],
                        content=content,
                    )
        except (KeyError, OSError, ValidationError, zipfile.BadZipFile) as error:
            raise ValueError("Assessment pack public content is invalid.") from error
        ids = [item.question_id for item in question_list]
        if (
            manifest.protocol_version != PROTOCOL_VERSION
            or manifest.pack_format_version != PACK_FORMAT_VERSION
            or manifest.release_id != release_id
            or ids != manifest.canonical_question_ids
            or not ids
            or len(ids) != len(set(ids))
            or any(type(item) is not int or item <= 0 for item in ids)
        ):
            raise ValueError("Assessment pack question linkage is invalid.")
        return {item.question_id: item for item in question_list}, manifest, assets

    def _verified_attempt_pack(self, record: LocalAttemptRecord) -> Path:
        path = self.store.verified_pack(
            record.ticket.ticket.release_id, record.ticket.ticket.content_hash
        )
        if path is None:
            raise ValueError("The verified assessment content is unavailable for recovery.")
        return path

    def _activate(
        self,
        record: LocalAttemptRecord,
        questions: dict[int, PublicQuestion],
        assets: dict[str, PublicAsset],
        *,
        trusted_wall: datetime | None = None,
        anchor_monotonic: float | None = None,
    ) -> None:
        self._validate_record_content(record, questions)
        now_mono = self._monotonic() if anchor_monotonic is None else anchor_monotonic
        self._attempt_id = record.attempt_id
        self._questions = dict(questions)
        self._assets = dict(assets)
        self._anchor_monotonic = now_mono
        self._anchor_remaining = float(record.remaining_seconds)
        self._anchor_trusted_wall = trusted_wall or (
            record.deadline - timedelta(seconds=record.remaining_seconds)
        )
        self._last_checkpoint_monotonic = now_mono

    @staticmethod
    def _validate_record_content(
        record: LocalAttemptRecord, questions: dict[int, PublicQuestion]
    ) -> None:
        if set(record.question_order) != set(questions):
            raise ValueError("Local attempt questions do not match the assessment content.")
        for question_id, selected_answer in record.responses.items():
            if question_id not in questions or (
                selected_answer is not None
                and selected_answer not in questions[question_id].options
            ):
                raise ValueError("Saved response does not match the assessment content.")

    def _clear_active(self) -> None:
        self._attempt_id = None
        self._questions = {}
        self._assets = {}
        self._anchor_monotonic = None
        self._anchor_trusted_wall = None
        self._last_checkpoint_monotonic = None
        self._last_observed_monotonic = None

    def _current_record(self) -> LocalAttemptRecord:
        if self._attempt_id is None:
            record = self.store.active_attempt()
            if record is None:
                raise RuntimeError("No local assessment attempt is active.")
            self._attempt_id = record.attempt_id
        record = self.store.load_attempt(self._attempt_id)
        if self._questions:
            self._validate_record_content(record, self._questions)
        return record

    def _monotonic(self) -> float:
        value = self.clock.monotonic()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ValueError("Monotonic clock returned an invalid value.")
        if (
            self._last_observed_monotonic is not None
            and value < self._last_observed_monotonic
        ):
            raise RuntimeError("Monotonic clock moved backwards.")
        self._last_observed_monotonic = float(value)
        return float(value)

    def _elapsed(self) -> float:
        if self._anchor_monotonic is None:
            raise RuntimeError("The assessment timer is not anchored.")
        return self._monotonic() - self._anchor_monotonic

    def _remaining(self) -> int:
        return max(0, math.floor(self._anchor_remaining - self._elapsed()))

    def _trusted_now(self, record: LocalAttemptRecord) -> datetime:
        if self._anchor_trusted_wall is None:
            raise RuntimeError("The assessment timer is not anchored.")
        return min(record.deadline, self._anchor_trusted_wall + timedelta(seconds=self._elapsed()))

    def _checkpoint_due(self) -> bool:
        return (
            self._last_checkpoint_monotonic is None
            or self._monotonic() - self._last_checkpoint_monotonic >= 10
        )

    def _snapshot_record(
        self, record: LocalAttemptRecord, *, remaining: int | None = None
    ) -> AttemptSnapshot:
        return AttemptSnapshot(
            attempt_id=record.attempt_id,
            state=record.state,
            question_order=record.question_order,
            responses=dict(record.responses),
            remaining_seconds=record.remaining_seconds if remaining is None else remaining,
            violations=len(self.store.integrity_events(record.attempt_id)),
            current_question_id=record.current_question_id,
        )

    def _seal(self, record: LocalAttemptRecord) -> AttemptSnapshot:
        latest = self.store.load_attempt(record.attempt_id)
        if latest.state != "in_progress":
            return self._snapshot_record(latest)
        remaining = self._remaining()
        now = self._trusted_now(latest)
        if remaining < latest.remaining_seconds:
            self.store.update_timer_checkpoint(
                latest.attempt_id, remaining, last_wall_time=now
            )
            self._last_checkpoint_monotonic = self._monotonic()
            latest = self.store.load_attempt(latest.attempt_id)
        sealed_at = self._trusted_now(latest)
        if remaining == 0:
            sealed_at = latest.deadline
        responses = [
            ResponseEntry(
                question_id=question_id,
                selected_answer=latest.responses[question_id],
            )
            for question_id in latest.question_order
        ]
        bundle = ResponseBundle(
            ticket=latest.ticket,
            content_hash=latest.ticket.ticket.content_hash,
            sealed_at=sealed_at,
            responses=responses,
            integrity_events=list(self.store.integrity_events(latest.attempt_id)),
        )
        signed = SignedResponseBundle(
            bundle=bundle,
            device_signature_b64=sign_json(self.identity.private_key_b64, bundle),
        )
        verify_json(
            self.identity.public_key_b64,
            bundle,
            signed.device_signature_b64,
        )
        try:
            sealed = self.store.seal_attempt(
                latest.attempt_id, signed, sealed_at=sealed_at
            )
        except ValueError:
            winner = self.store.load_attempt(latest.attempt_id)
            if winner.state == "in_progress":
                raise
            sealed = winner
        return self._snapshot_record(sealed)


__all__ = ["AssessmentRuntime", "AttemptSnapshot", "Clock", "PublicAsset", "SystemClock"]
