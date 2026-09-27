"""Read-only frozen archive inspection, safe to include in the lab builder."""
import hashlib
import io
import re
import zipfile
from pathlib import Path


def _forbidden_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/").casefold()
    return any(marker in normalized for marker in (
        "tests/test_", "aptitude.db", "client.sqlite3", "client-config.json",
        "device-key.bin", "protocol-signing.key", "pack-master.key", "enrollment.code",
        "client-session.key", "browser-session.key", "coordinator-ca.key.pem",
        "coordinator-server.key.pem", ".ksatpack", ".pfx", "pfx-password.txt",
    ))


def _assert_payload_safe(name: str, data: bytes) -> None:
    if _forbidden_archive_name(name):
        raise ValueError(f"Archive contains forbidden entry: {name}")
    # The pattern is not itself a PEM header when frozen into this module.
    if data.startswith(b"SQLite format 3\x00") or (
        not data.startswith(b"MZ")
        and re.search(rb"-----BEGIN (?:RSA )?PRIVATE KEY-----", data)
    ):
        raise ValueError(f"Archive contains forbidden private/live content: {name}")


def pyinstaller_payload_manifest(executable: Path) -> dict[str, str]:
    """Hash decompressed CArchive, nested ZIP and PYZ entries; reject private state."""
    from PyInstaller.archive.readers import CArchiveReader
    try:
        archive = CArchiveReader(str(Path(executable)))
    except Exception as error:
        raise ValueError(f"Unable to inspect PyInstaller archive: {Path(executable).name}") from error
    manifest: dict[str, str] = {}
    for name, entry in sorted(archive.toc.items()):
        typecode = entry[-1]
        data = archive.extract(name)
        _assert_payload_safe(name, data)
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as nested_zip:
                for zip_name in sorted(nested_zip.namelist()):
                    zip_data = nested_zip.read(zip_name)
                    _assert_payload_safe(zip_name, zip_data)
                    manifest[f"ZIP:{name}:{zip_name}"] = hashlib.sha256(zip_data).hexdigest()
        else:
            manifest[f"C:{typecode}:{name}"] = hashlib.sha256(data).hexdigest()
        if typecode == "z":
            nested = archive.open_embedded_archive(name)
            for nested_name in sorted(nested.toc):
                nested_data = nested.extract(nested_name, raw=True) or b""
                _assert_payload_safe(nested_name, nested_data)
                manifest[f"PYZ:{nested_name}"] = hashlib.sha256(nested_data).hexdigest()
    return manifest
