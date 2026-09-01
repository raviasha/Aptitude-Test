"""Immutable, answer-free assessment release packaging."""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence
from urllib.parse import unquote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ksat.crypto import decrypt_pack, encrypt_pack, sha256_hex, sign_json, verify_json
from ksat.protocol import (
    MATH_FLOOR_DIVISION_CLASS,
    MATH_FLOOR_DIVISION_ERROR,
    PACK_FORMAT_VERSION,
    PROTOCOL_VERSION,
    PublicQuestion,
    ReleaseManifest,
    ReleaseSummary,
    canonicalize_math_floor_division_expression,
    canonicalize_math_floor_division_markup,
    canonical_json,
)


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_PACK_NONCE_BYTES = 12
_PACK_TAG_BYTES = 16
_PACK_VALIDATION_CHUNK_BYTES = 1024 * 1024
_PACK_VALIDATION_SPOOL_MEMORY_BYTES = 1024 * 1024
_SAFE_ASSET_NAME = re.compile(r"assets/[0-9a-f]{64}\.(?:png|jpe?g|webp|svg)\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_MARKERS = ("answer", "correct", "feedback", "score", "solution", "explanation")
_WRAPPED_KEY_ENVELOPE_BYTES = 12 + 32 + 16
_DOUBLE_SLASH_MESSAGE = (
    "Public assessment content contains ambiguous external URL // syntax; use explicit math markup "
    '<code class="math-floor-division">LEFT // RIGHT</code> in question_html for floor division.'
)


def _require_key(value: bytes, label: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{label} must be exactly 32 bytes.")
    return value


def _require_stored_release_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Stored assessment release ID is invalid.") from error
    if str(parsed) != value:
        raise ValueError("Stored assessment release ID is invalid.")
    return value


def _strict_base64(value: Any, *, length: int, message: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError(message) from error
    if len(decoded) != length:
        raise ValueError(message)
    return decoded


def wrap_release_content_key(master_key: bytes, release_id: str, content_key: bytes) -> str:
    master_key = _require_key(master_key, "Pack master key")
    content_key = _require_key(content_key, "Release content key")
    return base64.b64encode(encrypt_pack(master_key, release_id, content_key)).decode("ascii")


def unwrap_release_content_key(master_key: bytes, release_id: str, wrapped_b64: str) -> bytes:
    master_key = _require_key(master_key, "Pack master key")
    wrapped = _strict_base64(
        wrapped_b64,
        length=_WRAPPED_KEY_ENVELOPE_BYTES,
        message="Wrapped release content key is invalid.",
    )
    try:
        content_key = decrypt_pack(master_key, release_id, wrapped)
    except ValueError as error:
        raise ValueError("Wrapped release content key is invalid.") from error
    return _require_key(content_key, "Release content key")


def _private_marker(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _decode_public_text(value).lower())
    return any(marker in normalized for marker in _PRIVATE_MARKERS)


def _decode_public_text(value: str) -> str:
    decoded = value
    for _ in range(8):
        expanded = html.unescape(unquote(decoded))
        if expanded == decoded:
            break
        decoded = expanded
    return "".join(character for character in decoded if unicodedata.category(character) != "Cf")


def _url_compact(value: str) -> str:
    return "".join(
        character
        for character in _decode_public_text(value).casefold()
        if not character.isspace() and not unicodedata.category(character).startswith("C")
    )


def _contains_ambiguous_double_slash(value: str) -> bool:
    decoded = _decode_public_text(value).replace("\\", "/")
    compact = "".join(
        character
        for character in decoded
        if not character.isspace() and not unicodedata.category(character).startswith("C")
    )
    return "//" in compact


def _url_field_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", _decode_public_text(value).casefold())
    return normalized in {"asset", "assetfilename", "file", "href", "src", "url", "uri"} or normalized.endswith(
        ("href", "src", "url", "uri")
    )


def _validate_public_string(value: str, *, url_field: bool = False) -> None:
    if url_field and not _SAFE_ASSET_NAME.fullmatch(value):
        raise ValueError("Public URL fields must reference canonical embedded assets.")
    compact = _url_compact(value)
    if (
        re.search(r"(?:https?|ftp|javascript|vbscript):", compact)
        or re.search(r"[a-z][a-z0-9+.-]{1,63}:(?://|\\\\)", compact)
        or re.search(
            r"(?:mailto|blob|filesystem|about|chrome|resource|view-source):", compact
        )
        or re.search(r"file:/", compact)
        or re.search(r"tel:\+?[0-9]", compact)
        or re.search(
            r"(?<![a-z0-9])data:(?:[a-z0-9.+-]+/[a-z0-9.+-]+)?(?:;[a-z0-9=.+-]+)*,",
            compact,
        )
    ):
        raise ValueError("Public assessment content contains an active or external URL.")
    route_text = compact.replace("\\", "/")
    if re.search(r"(?:^|/)api/", route_text) or "question-assets" in route_text:
        raise ValueError("Public assessment content contains a coordinator URL.")
    if _contains_ambiguous_double_slash(value):
        raise ValueError(_DOUBLE_SLASH_MESSAGE)


def _validate_public_payload(value: Any, *, field_name: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _private_marker(key):
                raise ValueError("Private assessment fields are not allowed in public content.")
            _validate_public_string(key)
            if _url_field_name(key) and not isinstance(item, str):
                raise ValueError("Public URL fields must reference canonical embedded assets.")
            _validate_public_payload(item, field_name=key)
        return
    if isinstance(value, list):
        for item in value:
            _validate_public_payload(item, field_name=field_name)
        return
    if isinstance(value, str):
        if field_name == "question_html":
            _validate_sanitized_question_html(value)
        else:
            _validate_public_string(value, url_field=_url_field_name(field_name))


class _PublicHTMLSanitizer(HTMLParser):
    """Preserve static question formatting/SVG while dropping private or active markup."""

    SAFE_TAGS = {
        "p", "div", "span", "strong", "em", "b", "i", "small", "sub", "sup", "br", "hr",
        "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody",
        "tfoot", "tr", "th", "td", "figure", "figcaption", "svg", "g", "path", "rect", "circle",
        "line", "polyline", "polygon", "text", "ellipse", "defs", "lineargradient", "stop", "title",
        "desc", "code",
    }
    BLOCKED_TAGS = {
        "script", "style", "iframe", "object", "embed", "link", "meta", "base", "form", "input",
        "button", "textarea", "select", "option", "foreignobject",
    }
    VOID_TAGS = {"br", "hr"}
    GLOBAL_ATTRS = {"class", "title", "role", "aria-label"}
    URL_ATTRS = {"href", "src", "xlink:href", "action", "formaction", "poster"}
    VISUAL_ATTRS = {
        "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "fill",
        "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "opacity",
        "text-anchor", "font-size", "font-family", "font-weight", "transform", "dominant-baseline",
        "viewbox", "preserveaspectratio", "width", "height", "xmlns", "colspan", "rowspan", "scope",
    }
    ATTR_CASE = {"viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []
        self.drop_depth = 0
        self.open_tags: list[str] = []
        self.math_code_parts: list[str] | None = None

    def _is_exact_math_code(self, attrs: list[tuple[str, str | None]]) -> bool:
        return (
            len(attrs) == 1
            and attrs[0] == ("class", MATH_FLOOR_DIVISION_CLASS)
            and self.get_starttag_text()
            == '<code class="math-floor-division">'
        )

    @staticmethod
    def _uses_math_class(attrs: list[tuple[str, str | None]]) -> bool:
        return any(
            name.lower() == "class"
            and value is not None
            and MATH_FLOOR_DIVISION_CLASS in value.split()
            for name, value in attrs
        )

    def _finish_math_code(self) -> None:
        if self.math_code_parts is None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        value = "".join(self.math_code_parts)
        canonical = canonicalize_math_floor_division_expression(value)
        self.output.append(
            f'<code class="{MATH_FLOOR_DIVISION_CLASS}">'
            f"{canonical}</code>"
        )
        self.math_code_parts = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag == "code":
            if self._is_exact_math_code(attrs):
                self.math_code_parts = []
                return
            if self._uses_math_class(attrs):
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        for name, value in attrs:
            if value is None:
                continue
            normalized_name = name.lower()
            lowered = _decode_public_text(value).strip().lower()
            if normalized_name == "xmlns" and lowered == "http://www.w3.org/2000/svg":
                continue
            if normalized_name in self.URL_ATTRS and not _SAFE_ASSET_NAME.fullmatch(value):
                raise ValueError("Question HTML contains an external or coordinator URL.")
            _validate_public_string(value, url_field=normalized_name in self.URL_ATTRS)
        if tag in self.BLOCKED_TAGS:
            self.drop_depth = 1
            return
        if tag not in self.SAFE_TAGS:
            return
        for name, value in attrs:
            if _private_marker(name) or (value is not None and _private_marker(value)):
                self.drop_depth = 1
                return
        safe_attrs: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name not in self.GLOBAL_ATTRS | self.VISUAL_ATTRS or value is None:
                continue
            lowered = value.strip().lower()
            if name == "xmlns":
                if lowered != "http://www.w3.org/2000/svg":
                    raise ValueError("Question HTML contains an external URL.")
            safe_attrs.append(f' {self.ATTR_CASE.get(name, name)}="{html.escape(value, quote=True)}"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")
        if tag not in self.VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.math_code_parts is not None:
            if tag != "code":
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            self._finish_math_code()
            return
        if self.drop_depth:
            self.drop_depth -= 1
            return
        if tag in self.SAFE_TAGS and tag in self.open_tags:
            self.output.append(f"</{tag}>")
            self.open_tags.remove(tag)

    def handle_data(self, data: str) -> None:
        if self.math_code_parts is not None:
            self.math_code_parts.append(data)
            if sum(map(len, self.math_code_parts)) > 132:
                raise ValueError(MATH_FLOOR_DIVISION_ERROR)
            return
        if not self.drop_depth:
            self.output.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if not self.drop_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        if not self.drop_depth:
            self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)

    def handle_pi(self, data: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)

    def handle_decl(self, decl: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)

    def unknown_decl(self, data: str) -> None:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)

    def result(self) -> str:
        if self.math_code_parts is not None:
            raise ValueError(MATH_FLOOR_DIVISION_ERROR)
        return "".join(self.output).strip()


def _sanitize_question_html(fragment: str) -> str:
    fragment = canonicalize_math_floor_division_markup(fragment)
    sanitizer = _PublicHTMLSanitizer()
    sanitizer.feed(fragment)
    sanitizer.close()
    return sanitizer.result()


class _SanitizedHTMLURLCollector(HTMLParser):
    """Collect URL-relevant sanitized HTML while omitting only canonical SVG xmlns."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value is None:
                continue
            if (
                tag.lower() == "svg"
                and name.lower() == "xmlns"
                and value == "http://www.w3.org/2000/svg"
            ):
                continue
            self.parts.append(value)

    handle_startendtag = handle_starttag

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def _validate_sanitized_question_html(fragment: str) -> None:
    collector = _SanitizedHTMLURLCollector()
    collector.feed(fragment)
    collector.close()
    _validate_public_string("".join(collector.parts))


def _public_question_payloads(
    selected_questions: Sequence[PublicQuestion | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    questions: list[PublicQuestion] = []
    for item in selected_questions:
        raw = (
            item.model_dump(mode="json", warnings=False)
            if isinstance(item, PublicQuestion)
            else dict(item)
        )
        question = PublicQuestion.model_validate(raw)
        sanitized = question.model_dump(mode="json")
        sanitized["question_html"] = _sanitize_question_html(question.question_html)
        questions.append(PublicQuestion.model_validate(sanitized))
    question_ids = [item.question_id for item in questions]
    if not question_ids or len(question_ids) != len(set(question_ids)):
        raise ValueError("Assessment releases require unique questions.")
    payloads = [item.model_dump(mode="json", exclude_none=True) for item in questions]
    payloads.sort(key=lambda item: item["question_id"])
    _validate_public_payload(payloads)
    return payloads


def _referenced_asset_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names: set[str] = set()
        for item in value.values():
            names.update(_referenced_asset_names(item))
        return names
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names.update(_referenced_asset_names(item))
        return names
    if isinstance(value, str) and value.startswith("assets/"):
        return {value}
    return set()


def _validated_assets(
    assets: Mapping[str, bytes], referenced_names: set[str]
) -> list[tuple[str, bytes]]:
    for name in assets:
        if not isinstance(name, str) or not _SAFE_ASSET_NAME.fullmatch(name):
            raise ValueError(f"Assessment asset name is unsafe: {name!r}")
    supplied_names = set(assets)
    if supplied_names != referenced_names:
        missing = sorted(referenced_names - supplied_names)
        extra = sorted(supplied_names - referenced_names)
        raise ValueError(
            "Assessment assets must exactly match final public references"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unreferenced: {', '.join(extra)}" if extra else "")
            + "."
        )
    validated: list[tuple[str, bytes]] = []
    for name, content in assets.items():
        if not isinstance(content, bytes):
            raise ValueError(f"Assessment asset must contain bytes: {name}")
        expected_digest = name.split("/", 1)[1].split(".", 1)[0]
        if hashlib.sha256(content).hexdigest() != expected_digest:
            raise ValueError(f"Assessment asset hash does not match its name: {name}")
        validated.append((name, content))
    validated.sort(key=lambda item: item[0])
    return validated


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


def _publish_exclusive(path: Path, content: bytes) -> bool:
    """Publish a complete fsynced file without replacing an existing destination."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() == content:
                return False
            raise FileExistsError(f"Assessment pack path already contains different content: {path.name}")
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
        return True
    finally:
        temporary_path.unlink(missing_ok=True)


def _release_row(
    connection: sqlite3.Connection, *, release_id: str | None = None, test_id: int | None = None
):
    if release_id is not None:
        return connection.execute(
            "SELECT * FROM assessment_releases WHERE release_id = ?", (release_id,)
        ).fetchone()
    return connection.execute(
        "SELECT * FROM assessment_releases WHERE test_id = ?", (test_id,)
    ).fetchone()


def _derive_public_key_b64(private_key_b64: str) -> str:
    raw = _strict_base64(private_key_b64, length=32, message="Invalid Ed25519 private key.")
    try:
        public = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
    except ValueError as error:
        raise ValueError("Invalid Ed25519 private key.") from error
    return base64.b64encode(public).decode("ascii")


class _DuplicateJSONKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = item
    return value


def _load_json_without_duplicate_keys(content: str | bytes, message: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
    except (_DuplicateJSONKey, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(message) from error


def _load_pack_json(content: bytes, label: str) -> Any:
    return _load_json_without_duplicate_keys(
        content, f"Stored assessment pack {label} JSON is invalid."
    )


def _stream_sha256(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while True:
        chunk = stream.read(_PACK_VALIDATION_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _decrypt_pack_stream(
    key: bytes, release_id: str, encrypted: BinaryIO
) -> BinaryIO:
    key = _require_key(key, "Release content key")
    plaintext = tempfile.SpooledTemporaryFile(
        max_size=_PACK_VALIDATION_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    try:
        encrypted.seek(0, os.SEEK_END)
        envelope_size = encrypted.tell()
        if envelope_size <= _PACK_NONCE_BYTES + _PACK_TAG_BYTES:
            raise ValueError("Stored assessment pack encryption is invalid.")
        encrypted.seek(0)
        nonce = encrypted.read(_PACK_NONCE_BYTES)
        encrypted.seek(-_PACK_TAG_BYTES, os.SEEK_END)
        tag = encrypted.read(_PACK_TAG_BYTES)
        if len(nonce) != _PACK_NONCE_BYTES or len(tag) != _PACK_TAG_BYTES:
            raise ValueError("Stored assessment pack encryption is invalid.")

        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(release_id.encode("utf-8"))
        encrypted.seek(_PACK_NONCE_BYTES)
        remaining = envelope_size - _PACK_NONCE_BYTES - _PACK_TAG_BYTES
        while remaining:
            chunk = encrypted.read(min(_PACK_VALIDATION_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("Stored assessment pack encryption is invalid.")
            remaining -= len(chunk)
            plaintext.write(decryptor.update(chunk))
        plaintext.write(decryptor.finalize())
        plaintext.seek(0)
        encrypted.seek(0)
        return plaintext
    except (InvalidTag, OSError, ValueError) as error:
        plaintext.close()
        raise ValueError("Stored assessment pack encryption is invalid.") from error


def _inspect_pack(
    plaintext: bytes | BinaryIO,
    manifest: ReleaseManifest,
    canonical_question_ids: list[int],
) -> None:
    source = io.BytesIO(plaintext) if isinstance(plaintext, bytes) else plaintext
    source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_names = ["manifest.json", "questions.json", *manifest.asset_names]
            if names != expected_names or len(names) != len(set(names)) or any(info.is_dir() for info in infos):
                raise ValueError("Stored assessment pack entries are invalid.")
            if any(info.date_time != _FIXED_ZIP_TIMESTAMP for info in infos):
                raise ValueError("Stored assessment pack timestamps are invalid.")
            packed_manifest = _load_pack_json(archive.read("manifest.json"), "manifest")
            packed_questions_json = archive.read("questions.json")
            packed_questions = _load_pack_json(packed_questions_json, "questions")
            for asset_name in manifest.asset_names:
                digest = asset_name.split("/", 1)[1].split(".", 1)[0]
                with archive.open(asset_name) as asset:
                    actual_digest = hashlib.file_digest(asset, "sha256").hexdigest()
                if actual_digest != digest:
                    raise ValueError("Stored assessment pack asset hash is invalid.")
    except (KeyError, zipfile.BadZipFile, OSError) as error:
        raise ValueError("Stored assessment pack is invalid.") from error
    if packed_manifest != manifest.model_dump(mode="json"):
        raise ValueError("Stored assessment pack manifest is inconsistent.")
    try:
        canonical_payloads = _public_question_payloads(packed_questions)
    except Exception as error:
        raise ValueError("Stored assessment pack questions are invalid.") from error
    if packed_questions_json != canonical_json(canonical_payloads):
        raise ValueError("Stored assessment pack questions JSON is not canonical public content.")
    if [item["question_id"] for item in packed_questions] != canonical_question_ids:
        raise ValueError("Stored assessment pack question linkage is inconsistent.")
    if _referenced_asset_names(canonical_payloads) != set(manifest.asset_names):
        raise ValueError("Stored assessment pack asset references are inconsistent.")


def _summary_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    expected_release_id: str,
    pack_dir: Path | None = None,
    signing_public_key_b64: str | None = None,
    pack_master_key: bytes | None = None,
    encrypted_pack_file: BinaryIO | None = None,
) -> ReleaseSummary:
    _require_stored_release_id(expected_release_id)
    if row["release_id"] != expected_release_id or row["state"] not in {"prepared", "launched"}:
        raise ValueError("Stored assessment release identity or state is invalid.")
    if row["content_pack_filename"] != f"{expected_release_id}.ksatpack":
        raise ValueError("Stored assessment release filename is invalid.")
    if not isinstance(row["content_hash"], str) or not _SHA256.fullmatch(row["content_hash"]):
        raise ValueError("Stored assessment release content hash is invalid.")
    _strict_base64(
        row["content_signature_b64"], length=64, message="Stored assessment release signature is invalid."
    )
    _strict_base64(
        row["wrapped_content_key_b64"],
        length=_WRAPPED_KEY_ENVELOPE_BYTES,
        message="Stored assessment wrapped content key is invalid.",
    )
    try:
        manifest = ReleaseManifest.model_validate(_load_json_without_duplicate_keys(
            row["manifest_json"], "Stored assessment release manifest is invalid."
        ))
    except Exception as error:
        raise ValueError("Stored assessment release manifest is invalid.") from error
    if (
        manifest.protocol_version != PROTOCOL_VERSION
        or manifest.pack_format_version != PACK_FORMAT_VERSION
    ):
        raise ValueError("Stored assessment release version is unsupported.")
    test = connection.execute(
        "SELECT test_id, test_name, release_id FROM tests WHERE test_id = ?", (row["test_id"],)
    ).fetchone()
    if (
        test is None
        or test["release_id"] != expected_release_id
        or manifest.release_id != expected_release_id
        or manifest.test_id != row["test_id"]
        or manifest.test_name != test["test_name"]
    ):
        raise ValueError("Stored assessment release/test linkage is invalid.")
    question_rows = connection.execute(
        "SELECT question_id, canonical_order FROM release_questions WHERE release_id = ? ORDER BY canonical_order",
        (expected_release_id,),
    ).fetchall()
    canonical_orders = [item["canonical_order"] for item in question_rows]
    question_ids = [item["question_id"] for item in question_rows]
    if (
        canonical_orders != list(range(len(question_rows)))
        or not question_ids
        or len(question_ids) != len(set(question_ids))
        or manifest.canonical_question_ids != question_ids
        or row["duration_seconds"] != manifest.duration_seconds
        or manifest.duration_seconds != len(question_ids) * 60
        or manifest.asset_names != sorted(set(manifest.asset_names))
        or any(not _SAFE_ASSET_NAME.fullmatch(name) for name in manifest.asset_names)
    ):
        raise ValueError("Stored assessment release manifest/linkage is inconsistent.")

    content_source_count = sum(
        value is not None for value in (pack_dir, encrypted_pack_file)
    )
    verification_requested = any(
        value is not None
        for value in (
            pack_dir,
            signing_public_key_b64,
            pack_master_key,
            encrypted_pack_file,
        )
    )
    if verification_requested and (
        content_source_count != 1
        or signing_public_key_b64 is None
        or pack_master_key is None
    ):
        raise ValueError("Complete release verification inputs are required.")
    if verification_requested:
        _strict_base64(
            signing_public_key_b64, length=32, message="Stored assessment signing public key is invalid."
        )
        pack_master_key = _require_key(pack_master_key, "Pack master key")
        owned_encrypted_file: BinaryIO | None = None
        try:
            if encrypted_pack_file is None:
                pack_root = Path(pack_dir).resolve(strict=True)
                pack_path = (pack_root / row["content_pack_filename"]).resolve(strict=True)
                if not pack_path.is_relative_to(pack_root) or not pack_path.is_file():
                    raise OSError("Stored assessment pack path is invalid.")
                owned_encrypted_file = pack_path.open("rb")
                encrypted_pack_file = owned_encrypted_file
            actual_hash = _stream_sha256(encrypted_pack_file)
        except (OSError, RuntimeError, ValueError) as error:
            if owned_encrypted_file is not None:
                owned_encrypted_file.close()
            raise ValueError("Stored assessment pack is missing.") from error
        if actual_hash != row["content_hash"]:
            if owned_encrypted_file is not None:
                owned_encrypted_file.close()
            raise ValueError("Stored assessment pack hash is invalid.")
        signature_value = {
            "release_id": expected_release_id,
            "content_hash": row["content_hash"],
            "manifest": manifest.model_dump(mode="json"),
        }
        try:
            verify_json(signing_public_key_b64, signature_value, row["content_signature_b64"])
        except ValueError as error:
            if owned_encrypted_file is not None:
                owned_encrypted_file.close()
            raise ValueError("Stored assessment release signature is invalid.") from error
        try:
            content_key = unwrap_release_content_key(
                pack_master_key, expected_release_id, row["wrapped_content_key_b64"]
            )
        except ValueError:
            if owned_encrypted_file is not None:
                owned_encrypted_file.close()
            raise
        plaintext_file: BinaryIO | None = None
        try:
            plaintext_file = _decrypt_pack_stream(
                content_key, expected_release_id, encrypted_pack_file
            )
            _inspect_pack(plaintext_file, manifest, question_ids)
        finally:
            if plaintext_file is not None:
                plaintext_file.close()
            if owned_encrypted_file is not None:
                owned_encrypted_file.close()
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


def load_release_manifest(
    connection: sqlite3.Connection,
    release_id: str,
    *,
    pack_dir: Path | None = None,
    signing_public_key_b64: str | None = None,
    pack_master_key: bytes | None = None,
    encrypted_pack_file: BinaryIO | None = None,
) -> ReleaseSummary:
    _require_stored_release_id(release_id)
    row = _release_row(connection, release_id=release_id)
    if row is None:
        raise KeyError(f"Assessment release not found: {release_id}")
    return _summary_from_row(
        connection,
        row,
        expected_release_id=release_id,
        pack_dir=pack_dir,
        signing_public_key_b64=signing_public_key_b64,
        pack_master_key=pack_master_key,
        encrypted_pack_file=encrypted_pack_file,
    )


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
    _created_artifact_paths: list[Path] | None = None,
) -> ReleaseSummary:
    pack_master_key = _require_key(pack_master_key, "Pack master key")
    signing_public_key_b64 = _derive_public_key_b64(signing_private_key_b64)
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    existing = _release_row(connection, test_id=test_id)
    if existing is not None:
        return _summary_from_row(
            connection,
            existing,
            expected_release_id=existing["release_id"],
            pack_dir=Path(pack_dir),
            signing_public_key_b64=signing_public_key_b64,
            pack_master_key=pack_master_key,
        )
    test = connection.execute(
        "SELECT test_id, test_name FROM tests WHERE test_id = ?", (test_id,)
    ).fetchone()
    if test is None:
        raise KeyError(f"Assessment test not found: {test_id}")

    question_payloads = _public_question_payloads(selected_questions)
    referenced_assets = _referenced_asset_names(question_payloads)
    validated_assets = _validated_assets(assets, referenced_assets)
    asset_names = [name for name, _ in validated_assets]
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
    content_key = _require_key(os.urandom(32), "Release content key")
    encrypted = encrypt_pack(content_key, release_id, plaintext)
    content_hash = sha256_hex(encrypted)
    content_signature_b64 = sign_json(
        signing_private_key_b64,
        {
            "release_id": release_id,
            "content_hash": content_hash,
            "manifest": manifest.model_dump(mode="json"),
        },
    )
    wrapped_content_key_b64 = wrap_release_content_key(pack_master_key, release_id, content_key)
    content_pack_filename = f"{release_id}.ksatpack"
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    pack_path = pack_dir / content_pack_filename
    created_pack = _publish_exclusive(pack_path, encrypted)
    if created_pack and _created_artifact_paths is not None:
        _created_artifact_paths.append(pack_path)

    def remove_owned_pack() -> None:
        if not created_pack:
            return
        pack_path.unlink(missing_ok=True)
        if _created_artifact_paths is not None:
            _created_artifact_paths.remove(pack_path)

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
        updated = connection.execute(
            "UPDATE tests SET release_id = ? WHERE test_id = ? AND release_id IS NULL",
            (release_id, test_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("Assessment test already has a release.")
    except sqlite3.IntegrityError:
        connection.execute("ROLLBACK TO SAVEPOINT prepare_assessment_release")
        connection.execute("RELEASE SAVEPOINT prepare_assessment_release")
        remove_owned_pack()
        winner = _release_row(connection, test_id=test_id)
        if winner is not None:
            return _summary_from_row(
                connection,
                winner,
                expected_release_id=winner["release_id"],
                pack_dir=pack_dir,
                signing_public_key_b64=signing_public_key_b64,
                pack_master_key=pack_master_key,
            )
        raise
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT prepare_assessment_release")
        connection.execute("RELEASE SAVEPOINT prepare_assessment_release")
        remove_owned_pack()
        raise
    connection.execute("RELEASE SAVEPOINT prepare_assessment_release")
    return load_release_manifest(
        connection,
        release_id,
        pack_dir=pack_dir,
        signing_public_key_b64=signing_public_key_b64,
        pack_master_key=pack_master_key,
    )


__all__ = [
    "load_release_manifest",
    "prepare_release",
    "unwrap_release_content_key",
    "wrap_release_content_key",
]
