"""Build an offline-signed deterministic KSAT lab-client update bundle."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ksat.crypto import public_key_b64_from_private, sign_bytes
from ksat.protocol import canonical_json
from ksat.update_protocol import ClientUpdateManifest, compare_versions, parse_client_update
from ksat.windows_authenticode import verify_authenticode


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def build_client_update(
    *,
    installer: Path,
    version: str,
    minimum_source_version: str,
    publisher: str,
    private_key_file: Path,
    output: Path,
    release_notes: str,
    release_id: str | None = None,
    published_at: datetime | None = None,
    health_check_timeout_seconds: int = 60,
    authenticode_verifier: Callable = verify_authenticode,
) -> Path:
    installer = Path(installer)
    output = Path(output)
    if not installer.is_file():
        raise ValueError("Client installer does not exist.")
    if output.suffix != ".ksat-client-update":
        raise ValueError("Client update output must use .ksat-client-update.")
    if compare_versions(version, minimum_source_version) <= 0:
        raise ValueError("Client update target must be newer than its minimum source version.")
    raw_private = Path(private_key_file).read_bytes()
    if len(raw_private) != 32:
        raise ValueError("Update signing private key is invalid.")
    private_b64 = base64.b64encode(raw_private).decode("ascii")
    public_b64 = public_key_b64_from_private(private_b64)
    installer_bytes = installer.read_bytes()
    authenticode_verifier(installer, publisher)
    manifest = ClientUpdateManifest(
        format_version=1,
        release_id=release_id or str(uuid.uuid4()),
        client_version=version,
        minimum_source_version=minimum_source_version,
        target_os="windows",
        target_architecture="x86_64",
        installer_filename=installer.name,
        installer_size=len(installer_bytes),
        installer_sha256=hashlib.sha256(installer_bytes).hexdigest(),
        authenticode_publisher=publisher,
        published_at=published_at or datetime.now(timezone.utc),
        release_notes=release_notes,
        health_check_timeout_seconds=health_check_timeout_seconds,
    )
    manifest_bytes = canonical_json(manifest)
    signature = sign_bytes(private_b64, manifest_bytes).encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp.ksat-client-update")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            archive.writestr(_zip_info("manifest.json"), manifest_bytes)
            archive.writestr(_zip_info(manifest.installer_filename), installer_bytes)
            archive.writestr(_zip_info("manifest.sig"), signature)
        parse_client_update(temporary, public_b64, authenticode_verifier)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def write_public_key_metadata(path: Path, public_key_b64: str) -> None:
    value = {"format_version": 1, "update_signing_public_key_b64": public_key_b64}
    Path(path).write_bytes(canonical_json(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--minimum-source-version", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--private-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-notes", default="KSAT Lab Client update")
    args = parser.parse_args()
    output = build_client_update(
        installer=args.installer,
        version=args.version,
        minimum_source_version=args.minimum_source_version,
        publisher=args.publisher,
        private_key_file=args.private_key_file,
        output=args.output,
        release_notes=args.release_notes,
    )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(json.dumps({"bundle_sha256": digest, "path": str(output), "version": args.version}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
