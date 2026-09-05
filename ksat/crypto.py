"""Cryptographic primitives for the distributed assessment protocol."""

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from ksat.protocol import PROTOCOL_VERSION, canonical_json


_RAW_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_ENROLLMENT_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{24}\Z")


@dataclass(frozen=True)
class CoordinatorKeyring:
    signing_private_key_b64: str
    signing_public_key_b64: str
    pack_master_key: bytes
    enrollment_code: str


def _decode_base64(value: str, message: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ValueError(message) from error


def _private_key_from_b64(private_key_b64: str) -> Ed25519PrivateKey:
    raw = _decode_base64(private_key_b64, "Invalid Ed25519 private key.")
    if len(raw) != _RAW_KEY_LENGTH:
        raise ValueError("Invalid Ed25519 private key.")
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as error:
        raise ValueError("Invalid Ed25519 private key.") from error


def _public_key_from_b64(public_key_b64: str) -> Ed25519PublicKey:
    raw = _decode_base64(public_key_b64, "Invalid Ed25519 public key.")
    if len(raw) != _RAW_KEY_LENGTH:
        raise ValueError("Invalid Ed25519 public key.")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as error:
        raise ValueError("Invalid Ed25519 public key.") from error


def _validate_aes_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != _RAW_KEY_LENGTH:
        raise ValueError("AES-GCM key must be 32 bytes.")


def generate_ed25519_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private_raw).decode("ascii"), base64.b64encode(public_raw).decode("ascii")


def sign_json(private_key_b64: str, value: Any) -> str:
    signature = _private_key_from_b64(private_key_b64).sign(canonical_json(value))
    return base64.b64encode(signature).decode("ascii")


def verify_json(public_key_b64: str, value: Any, signature_b64: str) -> None:
    public_key = _public_key_from_b64(public_key_b64)
    signature = _decode_base64(signature_b64, "Invalid Ed25519 signature.")
    try:
        public_key.verify(signature, canonical_json(value))
    except InvalidSignature as error:
        raise ValueError("Invalid Ed25519 signature.") from error


def encrypt_pack(key: bytes, release_id: str, plaintext: bytes) -> bytes:
    _validate_aes_key(key)
    nonce = os.urandom(_NONCE_LENGTH)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, release_id.encode("utf-8"))
    return nonce + ciphertext


def decrypt_pack(key: bytes, release_id: str, envelope: bytes) -> bytes:
    _validate_aes_key(key)
    if not isinstance(envelope, bytes) or len(envelope) <= _NONCE_LENGTH:
        raise ValueError("Encrypted pack is invalid.")
    nonce, ciphertext = envelope[:_NONCE_LENGTH], envelope[_NONCE_LENGTH:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, release_id.encode("utf-8"))
    except InvalidTag as error:
        raise ValueError("Encrypted pack authentication failed.") from error


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(value)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _load_signing_private_key(path: Path) -> Ed25519PrivateKey | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("Coordinator signing key is invalid.") from error
    if len(raw) != _RAW_KEY_LENGTH:
        raise ValueError("Coordinator signing key is invalid.")
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as error:
        raise ValueError("Coordinator signing key is invalid.") from error


def _load_pack_master_key(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        key = path.read_bytes()
    except OSError as error:
        raise ValueError("Coordinator pack master key is invalid.") from error
    if len(key) != _RAW_KEY_LENGTH:
        raise ValueError("Coordinator pack master key is invalid.")
    return key


def _load_enrollment_code(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        code = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise ValueError("Coordinator enrollment code is invalid.") from error
    if not _ENROLLMENT_CODE_PATTERN.fullmatch(code):
        raise ValueError("Coordinator enrollment code is invalid.")
    return code


def _write_public_metadata(path: Path, public_key_b64: str) -> None:
    metadata = {
        "protocol_version": PROTOCOL_VERSION,
        "signing_public_key_b64": public_key_b64,
    }
    _atomic_write(path, json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_or_create_coordinator_keyring(secrets_dir: Path) -> CoordinatorKeyring:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    signing_key_path = secrets_dir / "protocol-signing.key"
    pack_master_path = secrets_dir / "pack-master.key"
    enrollment_code_path = secrets_dir / "enrollment.code"

    signing_private_key = _load_signing_private_key(signing_key_path)
    if signing_private_key is None:
        signing_private_key = Ed25519PrivateKey.generate()
        _atomic_write(
            signing_key_path,
            signing_private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        )

    pack_master_key = _load_pack_master_key(pack_master_path)
    if pack_master_key is None:
        pack_master_key = os.urandom(_RAW_KEY_LENGTH)
        _atomic_write(pack_master_path, pack_master_key)

    enrollment_code = _load_enrollment_code(enrollment_code_path)
    if enrollment_code is None:
        enrollment_code = secrets.token_urlsafe(18)
        _atomic_write(enrollment_code_path, enrollment_code.encode("ascii"))

    signing_private_key_b64 = base64.b64encode(
        signing_private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    ).decode("ascii")
    signing_public_key_b64 = base64.b64encode(
        signing_private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    _write_public_metadata(secrets_dir / "protocol-public.json", signing_public_key_b64)
    return CoordinatorKeyring(
        signing_private_key_b64=signing_private_key_b64,
        signing_public_key_b64=signing_public_key_b64,
        pack_master_key=pack_master_key,
        enrollment_code=enrollment_code,
    )
