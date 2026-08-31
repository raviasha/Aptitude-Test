"""Immutable, answer-free assessment release packaging."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ksat.crypto import decrypt_pack, encrypt_pack, sha256_hex, sign_json
from ksat.protocol import PublicQuestion, ReleaseManifest, ReleaseSummary, canonical_json


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SAFE_ASSET_NAME = re.compile(r"assets/[0-9a-f]{64}\.(?:png|jpe?g|webp|svg)\Z")
_PRIVATE_KEY_PARTS = ("answer", "solution", "explanation")
_PRIVATE_KEYS = frozenset(("correct", "iscorrect", "feedback", "score"))
_PRIVATE_HTML_MARKER = re.compile(
    r"(?:data-[\w:-]*(?:answer|correct|solution|explanation)|"
    r"(?:id|class)\s*=\s*[\"'][^\"']*(?:answer|correct|solution|explanation)|"
    r"correct[-_ ]?answer|answer[-_ ]?key|solution[-_ ]?(?:step|media)|"
    r"option[-_ ]?explanation)",
    re.IGNORECASE,
)


def wrap_release_content_key(master_key: bytes, release_id: str, content_key: bytes) -> str:
    """Wrap a release key with fresh AES-GCM encryption bound to its release ID."""
    return base64.b64encode(encrypt_pack(master_key, release_id, content_key)).decode("ascii")


def unwrap_release_content_key(master_key: bytes, release_id: str, wrapped_b64: str) -> bytes:
    try:
        wrapped = base64.b64decode(wrapped_b64.encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError("Wrapped release content key is invalid.") from error
    content_key = decrypt_pack(master_key, release_id, wrapped)
    if len(content_key) != 32:
        raise ValueError("Wrapped release content key is invalid.")
    return content_key


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _assert_public_content(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _PRIVATE_KEYS or any(part in normalized for part in _PRIVATE_KEY_PARTS):
                raise ValueError(f"Private assessment material is not allowed in content packs: {key}")
            if normalized == "questionhtml" and isinstance(item, str) and _PRIVATE_HTML_MARKER.search(item):
                raise ValueError("Private assessment material is not allowed in question HTML.")
            _assert_public_content(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_public_content(item)
        return
    if isinstance(value, str) and (
        "/api/question-banks/" in value or "/api/question-assets/" in value
    ):
        raise ValueError("Coordinator media URLs are not allowed in content packs.")


def _public_question_payloads(
    selected_questions: Sequence[PublicQuestion | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    questions = [
        item if isinstance(item, PublicQuestion) else PublicQuestion.model_validate(item)
        for item in selected_questions
    ]
    question_ids = [item.question_id for item in questions]
    if not question_ids or len(question_ids) != len(set(question_ids)):
        raise ValueError("Assessment releases require unique questions.")
    payloads = [item.model_dump(mode="json", exclude_none=True) for item in questions]
    payloads.sort(key=lambda item: item["question_id"])
    _assert_public_content(payloads)
    return payloads


def _validated_assets(assets: Mapping[str, bytes]) -> list[tuple[str, bytes]]:
    validated: list[tuple[str, bytes]] = []
    for name, content in assets.items():
        if not isinstance(name, str) or not _SAFE_ASSET_NAME.fullmatch(name):
            raise ValueError(f"Assessment asset name is unsafe: {name!r}")
        if not isinstance(content, bytes):
            raise ValueError(f"Assessment asset must contain bytes: {name}")
        expected_digest = name.split("/", 1)[1].split(".", 1)[0]
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ValueError(f"Assessment asset hash does not match its name: {name}")
        validated.append((name, content))
    validated.sort(key=lambda item: item[0])
    return validated


def _referenced_asset_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_referenced_asset_names(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_referenced_asset_names(item) for item in value), set())
    if isinstance(value, str) and value.startswith("assets/"):
        return {value}
    return set()


def _zip_entry(name: str, content: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info, content


def _build_pack_payload(
    manifest: ReleaseManifest,
    question_payloads: list[dict[str, Any]],
    assets: list[tuple[str, bytes]],
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in (
            ("manifest.json", canonical_json(manifest)),
            ("questions.json", canonical_json(question_payloads)),
            *assets,
        ):
            info, value = _zip_entry(name, content)
            archive.writestr(info, value)
    return stream.getvalue()


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _release_row(connection: sqlite3.Connection, *, release_id: str | None = None, test_id: int | None = None):
    if release_id is not None:
        return connection.execute(
            "SELECT * FROM assessment_releases WHERE release_id = ?", (release_id,)
        ).fetchone()
    return connection.execute(
        "SELECT * FROM assessment_releases WHERE test_id = ?", (test_id,)
    ).fetchone()


def _summary_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> ReleaseSummary:
    question_ids = [
        item["question_id"]
        for item in connection.execute(
            "SELECT question_id FROM release_questions WHERE release_id = ? ORDER BY canonical_order",
            (row["release_id"],),
        ).fetchall()
    ]
    try:
        manifest = ReleaseManifest.model_validate_json(row["manifest_json"])
    except Exception as error:
        raise ValueError("Stored assessment release manifest is invalid.") from error
    if manifest.canonical_question_ids != question_ids:
        raise ValueError("Stored assessment release question order is inconsistent.")
    return ReleaseSummary(
        release_id=row["release_id"],
        test_id=row["test_id"],
        state=row["state"],
        duration_seconds=row["duration_seconds"],
        canonical_question_ids=question_ids,
        content_pack_filename=row["content_pack_filename"],
        content_hash=row["content_hash"],
        content_signature_b64=row["content_signature_b64"],
        wrapped_content_key_b64=row["wrapped_content_key_b64"],
    )


def load_release_manifest(connection: sqlite3.Connection, release_id: str) -> ReleaseSummary:
    row = _release_row(connection, release_id=release_id)
    if row is None:
        raise KeyError(f"Assessment release not found: {release_id}")
    return _summary_from_row(connection, row)


def prepare_release(
    connection: sqlite3.Connection,
    *,
    test_id: int,
    selected_questions: Sequence[PublicQuestion | Mapping[str, Any]],
    assets: Mapping[str, bytes],
    pack_dir: Path,
    signing_private_key_b64: str,
    pack_master_key: bytes,
    now_iso: str,
) -> ReleaseSummary:
    """Create one immutable release for a test, or return its existing release."""
    existing = _release_row(connection, test_id=test_id)
    if existing is not None:
        return _summary_from_row(connection, existing)

    test = connection.execute(
        "SELECT test_id, test_name FROM tests WHERE test_id = ?", (test_id,)
    ).fetchone()
    if test is None:
        raise KeyError(f"Assessment test not found: {test_id}")

    question_payloads = _public_question_payloads(selected_questions)
    validated_assets = _validated_assets(assets)
    asset_names = [name for name, _ in validated_assets]
    missing_assets = _referenced_asset_names(question_payloads) - set(asset_names)
    if missing_assets:
        raise ValueError("Assessment questions reference missing assets: " + ", ".join(sorted(missing_assets)))

    release_id = str(uuid.uuid4())
    canonical_question_ids = [item["question_id"] for item in question_payloads]
    duration_seconds = len(canonical_question_ids) * 60
    manifest = ReleaseManifest(
        release_id=release_id,
        test_id=test_id,
        test_name=test["test_name"],
        duration_seconds=duration_seconds,
        canonical_question_ids=canonical_question_ids,
        asset_names=asset_names,
    )
    plaintext = _build_pack_payload(manifest, question_payloads, validated_assets)
    content_key = os.urandom(32)
    encrypted = encrypt_pack(content_key, release_id, plaintext)
    content_hash = sha256_hex(encrypted)
    manifest_payload = manifest.model_dump(mode="json")
    content_signature_b64 = sign_json(
        signing_private_key_b64,
        {"release_id": release_id, "content_hash": content_hash, "manifest": manifest_payload},
    )
    wrapped_content_key_b64 = wrap_release_content_key(pack_master_key, release_id, content_key)
    content_pack_filename = f"{release_id}.ksatpack"
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    pack_path = pack_dir / content_pack_filename
    _atomic_write(pack_path, encrypted)

    connection.execute("SAVEPOINT prepare_assessment_release")
    try:
        connection.execute(
            """INSERT INTO assessment_releases
               (release_id, test_id, state, duration_seconds, manifest_json,
                content_pack_filename, content_hash, content_signature_b64,
                wrapped_content_key_b64, created_at)
               VALUES (?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?)""",
            (
                release_id,
                test_id,
                duration_seconds,
                canonical_json(manifest).decode("utf-8"),
                content_pack_filename,
                content_hash,
                content_signature_b64,
                wrapped_content_key_b64,
                now_iso,
            ),
        )
        connection.executemany(
            "INSERT INTO release_questions (release_id, question_id, canonical_order) VALUES (?, ?, ?)",
            [
                (release_id, question_id, canonical_order)
                for canonical_order, question_id in enumerate(canonical_question_ids)
            ],
        )
        connection.execute(
            "UPDATE tests SET release_id = ? WHERE test_id = ?", (release_id, test_id)
        )
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT prepare_assessment_release")
        connection.execute("RELEASE SAVEPOINT prepare_assessment_release")
        pack_path.unlink(missing_ok=True)
        raise
    connection.execute("RELEASE SAVEPOINT prepare_assessment_release")
    return load_release_manifest(connection, release_id)


__all__ = [
    "load_release_manifest",
    "prepare_release",
    "unwrap_release_content_key",
    "wrap_release_content_key",
]
