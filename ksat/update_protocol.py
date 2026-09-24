"""Strict, signed Windows lab-client update bundle protocol."""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

from pydantic import Field, field_validator, model_validator

from ksat.crypto import verify_bytes
from ksat.protocol import ProtocolModel, canonical_json


MAX_INSTALLER_BYTES = 250_000_000
MAX_UNCOMPRESSED_BYTES = 260_000_000
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := _VERSION.fullmatch(value)) is None:
        raise ValueError("Client version must contain three numeric components.")
    return tuple(int(part) for part in match.groups())


def compare_versions(left: str, right: str) -> int:
    first, second = _version_tuple(left), _version_tuple(right)
    return (first > second) - (first < second)


def load_update_public_key(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw, object_pairs_hook=_duplicates_rejected)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Update signing public-key metadata is invalid.") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "update_signing_public_key_b64"}
        or value.get("format_version") != 1
        or canonical_json(value) != raw
    ):
        raise ValueError("Update signing public-key metadata is invalid.")
    public_key = value["update_signing_public_key_b64"]
    try:
        decoded = base64.b64decode(public_key.encode("ascii"), validate=True)
    except (AttributeError, UnicodeError, binascii.Error, ValueError) as error:
        raise ValueError("Update signing public-key metadata is invalid.") from error
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != public_key:
        raise ValueError("Update signing public-key metadata is invalid.")
    return public_key


class ClientUpdateManifest(ProtocolModel):
    format_version: Literal[1]
    release_id: str
    client_version: str
    minimum_source_version: str
    target_os: Literal["windows"]
    target_architecture: Literal["x86_64"]
    installer_filename: str
    installer_size: int = Field(gt=0, le=MAX_INSTALLER_BYTES)
    installer_sha256: str
    authenticode_publisher: str
    published_at: datetime
    release_notes: str = Field(min_length=1, max_length=4000)
    health_check_timeout_seconds: int = Field(ge=15, le=300)

    @field_validator("release_id")
    @classmethod
    def valid_release_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as error:
            raise ValueError("Release identifier is invalid.") from error
        if str(parsed) != value:
            raise ValueError("Release identifier is invalid.")
        return value

    @field_validator("client_version", "minimum_source_version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        _version_tuple(value)
        return value

    @field_validator("installer_filename")
    @classmethod
    def valid_installer_filename(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value != path.name
            or path.is_absolute()
            or "\\" in value
            or not value.lower().endswith(".exe")
        ):
            raise ValueError("Installer filename is unsafe.")
        return value

    @field_validator("installer_sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError("Installer SHA-256 is invalid.")
        return value

    @field_validator("authenticode_publisher")
    @classmethod
    def valid_publisher(cls, value: str) -> str:
        if not isinstance(value, str) or value != value.strip() or not value:
            raise ValueError("Authenticode publisher is invalid.")
        return value

    @field_validator("published_at")
    @classmethod
    def aware_publication_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Publication time must include a time zone.")
        return value

    @model_validator(mode="after")
    def target_is_newer_than_minimum(self):
        if compare_versions(self.client_version, self.minimum_source_version) <= 0:
            raise ValueError("Client update target must be newer than its minimum source version.")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedClientUpdate:
    manifest: ClientUpdateManifest
    bundle_path: Path
    bundle_sha256: str
    bundle_size: int
    authenticode_identity: Any


def _duplicates_rejected(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _strict_manifest(raw: bytes) -> ClientUpdateManifest:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_duplicates_rejected,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Non-finite JSON.")),
        )
        if not isinstance(value, dict):
            raise ValueError("Manifest is not an object.")
        manifest = ClientUpdateManifest.model_validate_json(raw, strict=True)
    except Exception as error:
        raise ValueError("Client update manifest is invalid.") from error
    if canonical_json(manifest) != raw:
        raise ValueError("Client update manifest is not canonical.")
    return manifest


def parse_client_update(
    path: Path,
    public_key_b64: str,
    authenticode_verifier: Callable[[Path, str], Any],
) -> VerifiedClientUpdate:
    bundle_path = Path(path)
    if bundle_path.suffix != ".ksat-client-update" or not bundle_path.is_file():
        raise ValueError("Client update bundle path is invalid.")
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            infos = archive.infolist()
            if len(infos) != 3 or len({info.filename for info in infos}) != 3:
                raise ValueError("Client update bundle must contain exactly three unique files.")
            if any(info.is_dir() or info.flag_bits & 0x1 for info in infos):
                raise ValueError("Client update bundle contains an invalid member.")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Client update bundle is too large.")
            if archive.testzip() is not None:
                raise ValueError("Client update bundle is corrupt.")
            if infos[0].filename != "manifest.json" or infos[-1].filename != "manifest.sig":
                raise ValueError("Client update bundle order is not canonical.")
            manifest_raw = archive.read(infos[0])
            manifest = _strict_manifest(manifest_raw)
            expected_names = ["manifest.json", manifest.installer_filename, "manifest.sig"]
            if [info.filename for info in infos] != expected_names:
                raise ValueError("Client update bundle members do not match the manifest.")
            try:
                signature = archive.read("manifest.sig").decode("ascii")
            except UnicodeError as error:
                raise ValueError("Client update manifest signature is invalid.") from error
            verify_bytes(public_key_b64, manifest_raw, signature)
            installer = archive.read(manifest.installer_filename)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("Client update bundle is invalid.") from error
    if len(installer) != manifest.installer_size:
        raise ValueError("Client update installer size does not match the manifest.")
    if hashlib.sha256(installer).hexdigest() != manifest.installer_sha256:
        raise ValueError("Client update installer digest does not match the manifest.")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="ksat-update-", suffix=".exe", delete=False) as temporary:
            temporary.write(installer)
            temporary.flush()
            temporary_path = Path(temporary.name)
        identity = authenticode_verifier(temporary_path, manifest.authenticode_publisher)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    bundle_bytes = bundle_path.read_bytes()
    return VerifiedClientUpdate(
        manifest=manifest,
        bundle_path=bundle_path.resolve(),
        bundle_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        bundle_size=len(bundle_bytes),
        authenticode_identity=identity,
    )
