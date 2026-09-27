"""Fixed builder resources. The frozen builder's signature binds this manifest."""
import hashlib
import base64
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID

from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT
from ksat.public_trust import strict_json
from ksat.windows_authenticode import verify_authenticode

CLIENT_VERSION = "2.1.1"
REQUIRED_FILES = frozenset({"KSATClient.exe", "KSATClientUpdater.exe", "KSATClientInstallGuard.exe",
                            "publisher.cer", "update-release-public.json", "KSATClient.iss"})
_PRIVATE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def pyinstaller_payload_manifest(path: Path) -> dict[str, str]:
    # Reuse the release gate that recursively checks CArchive, PYZ and nested ZIPs.
    from ksat.archive_inspection import pyinstaller_payload_manifest as inspect
    return inspect(path)


@dataclass(frozen=True)
class ApprovedPayload:
    root: Path
    client_version: str
    files: dict[str, str]
    publisher: str
    thumbprint: str


def _plain_file(path: Path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Payload links and reparse points are not allowed.")


def load_approved_payload(root: Path) -> ApprovedPayload:
    root = Path(root)
    if root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("Payload directory must not be a link.")
    root = root.resolve(strict=True)
    path = root / "payload-manifest.json"
    _plain_file(path)
    manifest = strict_json(path.read_bytes(), 64 * 1024)
    expected = {"format_version", "client_version", "files", "publisher", "thumbprint"}
    if (not isinstance(manifest, dict) or set(manifest) != expected
            or type(manifest["format_version"]) is not int or manifest["format_version"] != 1):
        raise ValueError("Approved payload manifest is invalid.")
    payload = ApprovedPayload(root, manifest["client_version"], manifest["files"],
                              manifest["publisher"], manifest["thumbprint"])
    verify_payload(payload)
    return payload


def verify_payload(payload: ApprovedPayload) -> None:
    if (payload.client_version != CLIENT_VERSION or payload.publisher != PUBLISHER
            or payload.thumbprint != THUMBPRINT or not isinstance(payload.files, dict)
            or set(payload.files) != REQUIRED_FILES):
        raise ValueError("Payload version, identity or file allowlist is not approved.")
    if {p.name for p in payload.root.iterdir()} != REQUIRED_FILES | {"payload-manifest.json"}:
        raise ValueError("Unexpected files in the approved payload.")
    for name, expected_hash in payload.files.items():
        path = payload.root / name
        _plain_file(path)
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("Invalid payload fingerprint.")
        digest = hashlib.sha256()
        tail = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if _PRIVATE.search(tail + chunk):
                    raise ValueError("Private-key material must not be packaged.")
                tail = chunk[-128:]
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError("Approved payload bytes have changed.")
        if path.suffix == ".exe":
            identity = verify_authenticode(path, payload.publisher)
            if identity.thumbprint != payload.thumbprint:
                raise ValueError("Payload executable has the wrong signer.")
            archive = pyinstaller_payload_manifest(path)
            if name in {"KSATClient.exe", "KSATClientUpdater.exe"}:
                expected_key = payload.files["update-release-public.json"]
                keys = [digest for item, digest in archive.items()
                        if item.replace("\\", "/").endswith((":update-release-public.json", "/update-release-public.json"))]
                if keys != [expected_key]:
                    raise ValueError("Executable update verification key is missing or inconsistent.")
    try:
        certificate = x509.load_der_x509_certificate((payload.root / "publisher.cer").read_bytes())
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if (certificate.fingerprint(hashes.SHA1()).hex().upper() != payload.thumbprint
                or certificate.subject.rfc4514_string() != payload.publisher
                or ExtendedKeyUsageOID.CODE_SIGNING not in usage):
            raise ValueError
        key = strict_json((payload.root / "update-release-public.json").read_bytes(), 4096)
        if (not isinstance(key, dict) or set(key) != {"format_version", "update_signing_public_key_b64"}
                or type(key["format_version"]) is not int or key["format_version"] != 1
                or len(base64.b64decode(key["update_signing_public_key_b64"], validate=True)) != 32):
            raise ValueError
    except (ValueError, TypeError, x509.ExtensionNotFound) as error:
        raise ValueError("Payload public certificate or update verification key is invalid.") from error
