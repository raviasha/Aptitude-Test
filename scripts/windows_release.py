"""Build, smoke-test, inspect, and hash the Windows release products."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlsplit

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from ksat.protocol import canonical_json
from ksat.crypto import public_key_b64_from_private
from ksat.update_protocol import parse_client_update
from scripts.build_client_update import build_client_update


APP_VERSION = "2.1.0"
LAB_SIGNING_PUBLISHER = "CN=KSAT LAB RELEASE SIGNING"
_FORBIDDEN_INPUT_NAMES = {
    "aptitude.db",
    "client.sqlite3",
    "client-config.json",
    "device-key.bin",
    "protocol-signing.key",
    "pack-master.key",
    "enrollment.code",
    "client-session.key",
    "browser-session.key",
    "coordinator-ca.key.pem",
    "coordinator-server.key.pem",
}
_FORBIDDEN_PRIVATE_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
)
_CLIENT_STATIC_DEPLOYMENT_MARKERS = ("http://", "https://", "coordinator_base_url")
_TEST_SIGNING_SUBJECT = "CN=KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION"


class SigningConfigurationError(RuntimeError):
    pass


def write_update_public_key_resource(public_key: bytes | str, destination: Path) -> Path:
    if isinstance(public_key, str):
        try:
            raw = base64.b64decode(public_key.encode("ascii"), validate=True)
        except (UnicodeError, binascii.Error, ValueError) as error:
            raise ValueError("Update signing public key is invalid.") from error
    else:
        raw = bytes(public_key)
    if len(raw) != 32:
        raise ValueError("Update signing public key is invalid.")
    payload = canonical_json({
        "format_version": 1,
        "update_signing_public_key_b64": base64.b64encode(raw).decode("ascii"),
    })
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def load_release_update_public_key(path: Path) -> bytes:
    raw = Path(path).read_bytes()
    if len(raw) == 32:
        return raw
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Update signing public key file is invalid.") from error
    if len(decoded) != 32:
        raise ValueError("Update signing public key file is invalid.")
    return decoded


@dataclass(frozen=True)
class ReleaseSigningConfig:
    pfx_path: Path
    password: str = field(repr=False)
    expected_publisher: str
    expected_thumbprint: str
    timestamp_url: str | None
    test_identity: bool = False
    lab_identity: bool = False

    def __post_init__(self) -> None:
        path = Path(self.pfx_path).resolve()
        object.__setattr__(self, "pfx_path", path)
        if not path.is_file() or not self.password:
            raise SigningConfigurationError("A readable signing identity and secret are required.")
        if not self.expected_publisher or not self.expected_thumbprint:
            raise SigningConfigurationError("The signing publisher identity must be pinned.")
        if self.test_identity and self.lab_identity:
            raise SigningConfigurationError("A signing identity cannot use two release profiles.")
        if self.test_identity:
            if self.timestamp_url is not None or self.expected_publisher != _TEST_SIGNING_SUBJECT:
                raise SigningConfigurationError("The nonproduction signing identity is invalid.")
        elif self.lab_identity:
            if self.timestamp_url is not None or self.expected_publisher != LAB_SIGNING_PUBLISHER:
                raise SigningConfigurationError("The private-lab signing identity is invalid.")
        else:
            try:
                parsed = urlsplit(self.timestamp_url or "")
            except ValueError as error:
                raise SigningConfigurationError("An HTTPS timestamp service is required.") from error
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise SigningConfigurationError("An HTTPS timestamp service is required.")


def _certificate_identity(pfx_path: Path, password: str) -> tuple[str, str]:
    try:
        _key, certificate, _chain = pkcs12.load_key_and_certificates(
            Path(pfx_path).read_bytes(), password.encode("utf-8")
        )
    except (OSError, ValueError) as error:
        raise SigningConfigurationError("The signing identity could not be loaded.") from error
    if certificate is None:
        raise SigningConfigurationError("The signing identity contains no certificate.")
    return (
        certificate.subject.rfc4514_string(),
        certificate.fingerprint(hashes.SHA1()).hex().upper(),
    )


def lab_signing_config(
    *,
    pfx_path: Path | None,
    password_environment_name: str,
    expected_publisher: str | None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ReleaseSigningConfig:
    values = os.environ if environ is None else environ
    password = values.get(password_environment_name) if password_environment_name else None
    if pfx_path is None or not password or expected_publisher != LAB_SIGNING_PUBLISHER:
        raise SigningConfigurationError(
            "Lab signing requires the persistent PFX, its password, and the exact KSAT Lab publisher."
        )
    try:
        _key, certificate, _chain = pkcs12.load_key_and_certificates(
            Path(pfx_path).read_bytes(), password.encode("utf-8")
        )
    except (OSError, ValueError) as error:
        raise SigningConfigurationError("The lab signing identity could not be loaded.") from error
    if certificate is None or certificate.subject.rfc4514_string() != expected_publisher:
        raise SigningConfigurationError("The lab signing publisher does not match the pin.")
    now = datetime.now(timezone.utc)
    if (
        certificate.not_valid_before_utc > now
        or certificate.not_valid_after_utc < now + timedelta(days=365)
    ):
        raise SigningConfigurationError(
            "The lab signing certificate must remain valid for at least one year."
        )
    try:
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as error:
        raise SigningConfigurationError(
            "The lab signing certificate lacks code-signing usage."
        ) from error
    if ExtendedKeyUsageOID.CODE_SIGNING not in usage:
        raise SigningConfigurationError("The lab signing certificate lacks code-signing usage.")
    return ReleaseSigningConfig(
        pfx_path=Path(pfx_path),
        password=password,
        expected_publisher=expected_publisher,
        expected_thumbprint=certificate.fingerprint(hashes.SHA1()).hex().upper(),
        timestamp_url=None,
        lab_identity=True,
    )


def production_signing_config(
    *,
    pfx_path: Path | None,
    password_environment_name: str,
    expected_publisher: str | None,
    timestamp_url: str | None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ReleaseSigningConfig:
    values = os.environ if environ is None else environ
    password = values.get(password_environment_name) if password_environment_name else None
    if pfx_path is None or not password or not expected_publisher or not timestamp_url:
        raise SigningConfigurationError(
            "Production signing requires PFX, password environment variable, publisher, and timestamp URL."
        )
    subject, thumbprint = _certificate_identity(pfx_path, password)
    if subject != expected_publisher:
        raise SigningConfigurationError("The signing certificate publisher does not match the pin.")
    return ReleaseSigningConfig(
        pfx_path=Path(pfx_path),
        password=password,
        expected_publisher=expected_publisher,
        expected_thumbprint=thumbprint,
        timestamp_url=timestamp_url,
    )


def create_ephemeral_test_signing_config(
    directory: Path,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> ReleaseSigningConfig:
    values = os.environ if environ is None else environ
    if values.get("KSAT_RELEASE_TEST_SIGNING") != "1":
        raise SigningConfigurationError(
            "Ephemeral signing requires KSAT_RELEASE_TEST_SIGNING=1 and is never production-ready."
        )
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "KSAT TEST SIGNING IDENTITY - NOT FOR PRODUCTION")]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    password = hashlib.sha256(os.urandom(64)).hexdigest()
    pfx_path = directory / "ksat-ephemeral-test-signing.pfx"
    pfx_path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"KSAT ephemeral test signing",
            key,
            certificate,
            None,
            serialization.BestAvailableEncryption(password.encode("ascii")),
        )
    )
    return ReleaseSigningConfig(
        pfx_path=pfx_path,
        password=password,
        expected_publisher=_TEST_SIGNING_SUBJECT,
        expected_thumbprint=certificate.fingerprint(hashes.SHA1()).hex().upper(),
        timestamp_url=None,
        test_identity=True,
    )


def _require_signing(config: ReleaseSigningConfig | None) -> ReleaseSigningConfig:
    if not isinstance(config, ReleaseSigningConfig):
        raise SigningConfigurationError("Release artifact signing is required and cannot be skipped.")
    return config


def _run_signing_powershell(script: str, environment: dict[str, str]) -> dict[str, object]:
    powershell = shutil.which("powershell.exe") or str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown PowerShell failure").strip()
        raise ValueError(f"Authenticode signing command failed: {detail[-1200:]}") from error
    try:
        value = json.loads(completed.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Authenticode signing returned invalid verification evidence.") from error
    if not isinstance(value, dict):
        raise ValueError("Authenticode signing returned invalid verification evidence.")
    return value


_SIGN_SCRIPT = r"""
$ErrorActionPreference='Stop'
$secret=New-Object System.Security.SecureString
$env:KSAT_RELEASE_SIGNING_SECRET.ToCharArray()|ForEach-Object{$secret.AppendChar($_)}
$secret.MakeReadOnly()
$flags=[System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
$cert=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:KSAT_RELEASE_SIGNING_PFX,$secret,$flags)
$parameters=@{FilePath=$env:KSAT_RELEASE_SIGNING_FILE;Certificate=$cert;HashAlgorithm='SHA256'}
if($env:KSAT_RELEASE_TIMESTAMP_URL){$parameters.TimestampServer=$env:KSAT_RELEASE_TIMESTAMP_URL}
$signature=Set-AuthenticodeSignature @parameters
@{status=$signature.Status.ToString();status_message=$signature.StatusMessage}|ConvertTo-Json -Compress
"""

_VERIFY_SCRIPT = r"""
$ErrorActionPreference='Stop'
$signature=Get-AuthenticodeSignature -FilePath $env:KSAT_RELEASE_SIGNING_FILE
@{
 status=$signature.Status.ToString();
 signer_subject=if($signature.SignerCertificate){$signature.SignerCertificate.Subject}else{$null};
 signer_thumbprint=if($signature.SignerCertificate){$signature.SignerCertificate.Thumbprint}else{$null};
 timestamp_thumbprint=if($signature.TimeStamperCertificate){$signature.TimeStamperCertificate.Thumbprint}else{$null}
}|ConvertTo-Json -Compress
"""


def _signing_environment(path: Path, config: ReleaseSigningConfig) -> dict[str, str]:
    environment = os.environ.copy()
    system_root = Path(environment.get("SystemRoot", r"C:\Windows"))
    environment.update(
        {
            "KSAT_RELEASE_SIGNING_FILE": str(Path(path).resolve()),
            "KSAT_RELEASE_SIGNING_PFX": str(config.pfx_path),
            "KSAT_RELEASE_SIGNING_SECRET": config.password,
            "KSAT_RELEASE_TIMESTAMP_URL": config.timestamp_url or "",
            # Do not allow a user/bundled module to shadow the Windows security module.
            "PSModulePath": str(
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
            ),
        }
    )
    return environment


def verify_authenticode_signature(path: Path, config: ReleaseSigningConfig) -> dict[str, object]:
    config = _require_signing(config)
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    evidence = _run_signing_powershell(_VERIFY_SCRIPT, _signing_environment(path, config))
    accepted_statuses = (
        {"Valid", "UnknownError", "NotTrusted"}
        if config.test_identity or config.lab_identity
        else {"Valid"}
    )
    if (
        evidence.get("status") not in accepted_statuses
        or evidence.get("signer_subject") != config.expected_publisher
        or str(evidence.get("signer_thumbprint") or "").upper()
        != config.expected_thumbprint
        or not (config.test_identity or config.lab_identity)
        and not evidence.get("timestamp_thumbprint")
    ):
        raise ValueError(f"Authenticode signature verification failed: {path.name}")
    return evidence


def sign_and_verify_artifact(path: Path, config: ReleaseSigningConfig) -> dict[str, object]:
    config = _require_signing(config)
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(path)
    result = _run_signing_powershell(_SIGN_SCRIPT, _signing_environment(path, config))
    if result.get("status") in {"NotSigned", "HashMismatch"}:
        raise ValueError(f"Authenticode signing failed: {path.name}")
    return verify_authenticode_signature(path, config)


@dataclass(frozen=True)
class ReleaseLayout:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    @property
    def dist_dir(self) -> Path:
        return self.root / "dist"

    @property
    def release_dir(self) -> Path:
        return self.root / "release"

    @property
    def build_dir(self) -> Path:
        return self.root / "build" / "windows"

    @property
    def coordinator_executable(self) -> Path:
        return self.dist_dir / "KSATCoordinator.exe"

    @property
    def client_executable(self) -> Path:
        return self.dist_dir / "KSATClient.exe"

    @property
    def updater_executable(self) -> Path:
        return self.dist_dir / "KSATClientUpdater.exe"

    @property
    def coordinator_installer(self) -> Path:
        return self.release_dir / f"KSATCoordinatorSetup-{APP_VERSION}.exe"

    @property
    def client_installer(self) -> Path:
        return self.release_dir / f"KSATClientSetup-{APP_VERSION}.exe"

    @property
    def coordinator_release_executable(self) -> Path:
        return self.release_dir / f"KSATCoordinator-{APP_VERSION}.exe"

    @property
    def client_release_executable(self) -> Path:
        return self.release_dir / f"KSATClient-{APP_VERSION}.exe"

    @property
    def hash_manifest(self) -> Path:
        return self.release_dir / "SHA256SUMS.txt"

    @property
    def test_hash_manifest(self) -> Path:
        return self.release_dir / f"SHA256SUMS-{APP_VERSION}-TEST-ONLY.txt"

    @property
    def test_client_update(self) -> Path:
        return self.release_dir / (
            f"KSATClientUpdate-{APP_VERSION}-TEST-ONLY.ksat-client-update"
        )

    @property
    def client_update(self) -> Path:
        return self.release_dir / f"KSATClientUpdate-{APP_VERSION}.ksat-client-update"

    @property
    def lab_trust_certificate(self) -> Path:
        return self.release_dir / "KSATLabReleaseSigning.cer"

    @property
    def lab_trust_installer(self) -> Path:
        return self.release_dir / "Install-KSATLabReleaseTrust.ps1"


def create_test_client_update_bundle(
    layout: ReleaseLayout,
    private_key_file: Path,
    *,
    publisher: str,
    authenticode_verifier,
) -> Path:
    """Create the non-distributable bundle exercised by the acceptance build."""
    return build_client_update(
        installer=layout.client_installer,
        version=APP_VERSION,
        minimum_source_version="2.0.0",
        publisher=publisher,
        private_key_file=private_key_file,
        output=layout.test_client_update,
        release_notes="TEST ONLY - coordinator-managed update acceptance artifact",
        authenticode_verifier=authenticode_verifier,
    )


def create_lab_client_update_bundle(
    layout: ReleaseLayout,
    private_key_file: Path,
    *,
    publisher: str,
    authenticode_verifier,
) -> Path:
    """Create the signed bundle distributed by a private KSAT lab."""
    return build_client_update(
        installer=layout.client_installer,
        version=APP_VERSION,
        minimum_source_version="2.0.0",
        publisher=publisher,
        private_key_file=private_key_file,
        output=layout.client_update,
        release_notes="KSAT Lab Client 2.1 managed-update bootstrap",
        authenticode_verifier=authenticode_verifier,
    )


def export_lab_trust_certificate(
    signing: ReleaseSigningConfig, destination: Path
) -> Path:
    if not signing.lab_identity:
        raise SigningConfigurationError("Only a lab identity may export lab trust.")
    _key, certificate, _chain = pkcs12.load_key_and_certificates(
        signing.pfx_path.read_bytes(), signing.password.encode("utf-8")
    )
    if certificate is None:
        raise SigningConfigurationError("The lab signing identity contains no certificate.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    return destination


def _add_data(source: Path, destination: str) -> list[str]:
    return ["--add-data", f"{source}{os.pathsep}{destination}"]


def _common_pyinstaller(
    root: Path, python: Path, name: str, work_name: str
) -> list[str]:
    layout = ReleaseLayout(root)
    return [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        name,
        "--distpath",
        str(layout.dist_dir),
        "--workpath",
        str(layout.build_dir / work_name / "work"),
        "--specpath",
        str(layout.build_dir / work_name / "spec"),
        "--paths",
        str(root),
        "--version-file",
        str(layout.build_dir / f"{name}.version.txt"),
        "--collect-all",
        "fastapi",
        "--collect-all",
        "starlette",
        "--collect-all",
        "uvicorn",
        "--collect-all",
        "cryptography",
        "--collect-all",
        "httpx",
        "--collect-all",
        "psutil",
        "--collect-all",
        "bcrypt",
        "--collect-all",
        "multipart",
        "--collect-all",
        "jinja2",
        "--hidden-import",
        "anyio._backends._asyncio",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
    ]


def build_commands(root: Path, python: Path) -> list[list[str]]:
    root = Path(root).resolve()
    python = Path(python)
    coordinator = _common_pyinstaller(root, python, "KSATCoordinator", "coordinator")
    public_key_resource = ReleaseLayout(root).build_dir / "update-release-public.json"
    coordinator.extend(_add_data(public_key_resource, "."))
    coordinator.extend(_add_data(root / "static", "static"))
    coordinator.extend(_add_data(root / "templates", "templates"))
    coordinator.extend(
        ["--hidden-import", "app", "--uac-admin", str(root / "coordinator_main.py")]
    )

    client = _common_pyinstaller(root, python, "KSATClient", "client")
    client.extend(_add_data(public_key_resource, "."))
    client.extend(_add_data(root / "static" / "client", "static/client"))
    client.extend(_add_data(root / "static" / "branding", "static/branding"))
    client.extend(_add_data(root / "static" / "branding.css", "static"))
    client.extend(_add_data(root / "static" / "math.css", "static"))
    client.append(str(root / "client_app.py"))
    updater = _common_pyinstaller(root, python, "KSATClientUpdater", "updater")
    updater.extend(_add_data(public_key_resource, "."))
    updater.extend(["--uac-admin", str(root / "client_updater.py")])
    return [coordinator, client, updater]


def smoke_coordinator_command(
    root: Path, python: Path, smoke_root: Path
) -> list[str]:
    """Build the frozen coordinator without the release UAC manifest for CI smoke.

    The shipped coordinator intentionally requests elevation so it can protect its
    ProgramData secrets.  Windows refuses to start that image from a non-elevated
    automated test process (WinError 740).  This isolated probe uses the same
    entrypoint, imports, and assets; only its manifest and output location differ.
    """
    root = Path(root).resolve()
    layout = ReleaseLayout(root)
    smoke_root = Path(smoke_root).resolve()
    command = _common_pyinstaller(
        root, Path(python), "KSATCoordinatorSmoke", "smoke"
    )
    command[command.index("--distpath") + 1] = str(smoke_root / "dist")
    command[command.index("--workpath") + 1] = str(smoke_root / "work")
    command[command.index("--specpath") + 1] = str(smoke_root / "spec")
    command[command.index("--version-file") + 1] = str(
        layout.build_dir / "KSATCoordinator.version.txt"
    )
    command.extend(
        _add_data(layout.build_dir / "update-release-public.json", ".")
    )
    command.extend(_add_data(root / "static", "static"))
    command.extend(_add_data(root / "templates", "templates"))
    command.extend(["--hidden-import", "app"])
    command.append(str(root / "coordinator_main.py"))
    return command


def _version_resource(product_name: str, executable_name: str) -> str:
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(2,1,0,0), prodvers=(2,1,0,0), mask=0x3f,
    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'College Assessment Lab'),
    StringStruct('FileDescription', '{product_name}'),
    StringStruct('FileVersion', '{APP_VERSION}'),
    StringStruct('InternalName', '{executable_name}'),
    StringStruct('OriginalFilename', '{executable_name}'),
    StringStruct('ProductName', '{product_name}'),
    StringStruct('ProductVersion', '{APP_VERSION}')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def write_version_resources(layout: ReleaseLayout) -> None:
    layout.build_dir.mkdir(parents=True, exist_ok=True)
    (layout.build_dir / "KSATCoordinator.version.txt").write_text(
        _version_resource("KSAT Faculty Coordinator", "KSATCoordinator.exe"),
        encoding="utf-8",
    )
    (layout.build_dir / "KSATClient.version.txt").write_text(
        _version_resource("KSAT Lab Client", "KSATClient.exe"), encoding="utf-8"
    )
    (layout.build_dir / "KSATClientUpdater.version.txt").write_text(
        _version_resource("KSAT Lab Client Updater", "KSATClientUpdater.exe"), encoding="utf-8"
    )


def inspect_release_inputs(root: Path) -> None:
    root = Path(root).resolve()
    client_static = root / "static" / "client"
    if not client_static.is_dir():
        raise ValueError("Client static assets are missing.")
    for path in client_static.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").casefold()
        except UnicodeError as error:
            raise ValueError("Client static assets must be UTF-8 text.") from error
        if any(marker in text for marker in _CLIENT_STATIC_DEPLOYMENT_MARKERS):
            raise ValueError(
                f"Client static asset contains a deployment-specific value: {path.name}"
            )
    for forbidden in _FORBIDDEN_INPUT_NAMES:
        if (root / forbidden).exists():
            raise ValueError(f"Forbidden release input at repository root: {forbidden}")


def _safe_remove_tree(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents or resolved == resolved_root:
        raise ValueError("Refusing to clean outside the repository build directory.")
    if resolved.exists():
        shutil.rmtree(resolved)


def publish_signed_executables(
    layout: ReleaseLayout,
    signing: ReleaseSigningConfig,
    *,
    signer=sign_and_verify_artifact,
) -> None:
    signing = _require_signing(signing)
    for executable in (layout.coordinator_executable, layout.client_executable, layout.updater_executable):
        if not executable.is_file() or executable.stat().st_size <= 0:
            raise RuntimeError(f"Expected executable was not built: {executable}")
        signer(executable, signing)
    layout.release_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(layout.coordinator_executable, layout.coordinator_release_executable)
    shutil.copy2(layout.client_executable, layout.client_release_executable)


def build_executables(
    root: Path,
    python: Path,
    signing: ReleaseSigningConfig | None = None,
) -> ReleaseLayout:
    signing = _require_signing(signing)
    layout = ReleaseLayout(root)
    inspect_release_inputs(layout.root)
    layout.dist_dir.mkdir(parents=True, exist_ok=True)
    layout.build_dir.mkdir(parents=True, exist_ok=True)
    for name in ("coordinator", "client", "updater"):
        _safe_remove_tree(layout.build_dir / name, layout.root)
    for executable in (layout.coordinator_executable, layout.client_executable, layout.updater_executable):
        try:
            executable.unlink()
        except FileNotFoundError:
            pass
    write_version_resources(layout)
    for command in build_commands(layout.root, python):
        subprocess.run(command, cwd=layout.root, check=True)
    publish_signed_executables(layout, signing)
    return layout


def discover_iscc() -> Path | None:
    command = shutil.which("ISCC.exe") or shutil.which("ISCC")
    candidates = [
        Path(command) if command else None,
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(tempfile.gettempdir()) / "KSATBuildTools" / "InnoSetup673" / "ISCC.exe",
        Path(tempfile.gettempdir()) / "InnoSetup7" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    return None


def discover_innoextract() -> Path | None:
    command = shutil.which("innoextract.exe") or shutil.which("innoextract")
    configured = os.environ.get("INNOEXTRACT_EXE")
    candidates = [
        Path(configured) if configured else None,
        Path(command) if command else None,
        Path(tempfile.gettempdir()) / "KSATBuildTools" / "innoextract670" / "innoextract.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    return None


def compile_installers(
    root: Path,
    iscc: Path,
    signing: ReleaseSigningConfig | None = None,
) -> ReleaseLayout:
    signing = _require_signing(signing)
    layout = ReleaseLayout(root)
    layout.release_dir.mkdir(parents=True, exist_ok=True)
    for path in (layout.coordinator_installer, layout.client_installer):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for script in (
        layout.root / "installer" / "KSATCoordinator.iss",
        layout.root / "installer" / "KSATClient.iss",
    ):
        subprocess.run([str(iscc), str(script)], cwd=layout.root, check=True)
    for path in (layout.coordinator_installer, layout.client_installer):
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"Expected installer was not built: {path}")
        sign_and_verify_artifact(path, signing)
    return layout


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_json(url: str, *, context: ssl.SSLContext | None, timeout: float = 45.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=context, timeout=1.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}") from last_error


def _terminate_process(process: subprocess.Popen) -> str:
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for item in children:
            item.terminate()
        parent.terminate()
        _gone, alive = psutil.wait_procs([parent, *children], timeout=8)
        for item in alive:
            item.kill()
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    output, _ = process.communicate(timeout=10)
    return output or ""


def _assert_loopback_listener(process: subprocess.Popen, port: int) -> None:
    import psutil

    owner = psutil.Process(process.pid)
    pids = {owner.pid, *(child.pid for child in owner.children(recursive=True))}
    listeners = [
        item
        for item in psutil.net_connections(kind="tcp")
        if item.status == psutil.CONN_LISTEN
        and item.pid in pids
        and item.laddr
        and item.laddr.port == port
    ]
    if not listeners or any(item.laddr.ip != "127.0.0.1" for item in listeners):
        raise RuntimeError("The lab client did not bind exclusively to IPv4 loopback.")


def smoke_executables(
    root: Path,
    python: Path,
    signing: ReleaseSigningConfig | None = None,
) -> dict[str, object]:
    signing = _require_signing(signing)
    layout = ReleaseLayout(root)
    for path in (layout.coordinator_executable, layout.client_executable, layout.updater_executable):
        if not path.is_file():
            raise FileNotFoundError(path)
        verify_authenticode_signature(path, signing)
    write_version_resources(layout)
    with tempfile.TemporaryDirectory(prefix="ksat-release-smoke-") as directory:
        temporary = Path(directory)
        smoke_build = temporary / "frozen-build"
        subprocess.run(
            smoke_coordinator_command(layout.root, python, smoke_build),
            cwd=layout.root,
            check=True,
        )
        smoke_coordinator = smoke_build / "dist" / "KSATCoordinatorSmoke.exe"
        if not smoke_coordinator.is_file() or smoke_coordinator.stat().st_size <= 0:
            raise RuntimeError("Expected non-elevated coordinator smoke image was not built.")
        coordinator_payload_manifest(layout.coordinator_executable, smoke_coordinator)
        coordinator_data = temporary / "coordinator-data"
        coordinator_port = _free_port()
        coordinator_environment = os.environ.copy()
        coordinator_environment.update(
            {
                "KSAT_COORDINATOR_DATA_DIR": str(coordinator_data),
                "KSAT_COORDINATOR_HOSTNAME": "localhost",
                "KSAT_COORDINATOR_BIND_HOST": "127.0.0.1",
                "KSAT_COORDINATOR_PORT": str(coordinator_port),
                "KSAT_NONINTERACTIVE": "1",
            }
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        coordinator = subprocess.Popen(
            [str(smoke_coordinator)],
            cwd=layout.root,
            env=coordinator_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=flags,
        )
        coordinator_output = ""
        client_output = ""
        smoke_error: Exception | None = None
        try:
            ca_path = coordinator_data / "public" / "coordinator-ca.pem"
            public_path = coordinator_data / "public" / "coordinator-public.json"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not (ca_path.is_file() and public_path.is_file()):
                if coordinator.poll() is not None:
                    raise RuntimeError("Coordinator executable exited during startup.")
                time.sleep(0.2)
            ssl_context = ssl.create_default_context(cafile=str(ca_path))
            coordinator_state = _wait_json(
                f"https://localhost:{coordinator_port}/api/build", context=ssl_context
            )
            if coordinator_state != {"version": APP_VERSION}:
                raise RuntimeError("Coordinator executable returned an unexpected version.")
            metadata = json.loads(public_path.read_text("utf-8"))

            client_program_data = temporary / "client-program-data"
            client_data = client_program_data / "KSAT Client"
            client_data.mkdir(parents=True)
            for name in ("identity", "state", "packs"):
                (client_data / name).mkdir()
            installed_ca = client_data / "coordinator-ca.pem"
            shutil.copyfile(ca_path, installed_ca)
            config = {
                "coordinator_base_url": f"https://localhost:{coordinator_port}",
                "trusted_ca_path": str(installed_ca.resolve()),
                "coordinator_signing_public_key_b64": metadata[
                    "signing_public_key_b64"
                ],
            }
            (client_data / "client-config.json").write_text(
                json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            client_port = _free_port()
            client_environment = os.environ.copy()
            client_environment.update(
                {
                    "ProgramData": str(client_program_data),
                    "KSAT_SMOKE_TEST": "1",
                    "KSAT_CLIENT_PORT": str(client_port),
                }
            )
            client = subprocess.Popen(
                [str(layout.client_executable), "--service-console"],
                cwd=layout.root,
                env=client_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=flags,
            )
            try:
                client_state = _wait_json(
                    f"http://127.0.0.1:{client_port}/api/state", context=None
                )
                if (
                    client_state.get("state") != "device_setup"
                    or client_state.get("problem") is not None
                ):
                    raise RuntimeError("Lab client did not reach healthy device setup state.")
                _assert_loopback_listener(client, client_port)
            finally:
                client_output = _terminate_process(client)
        except Exception as error:
            smoke_error = error
        finally:
            coordinator_output = _terminate_process(coordinator)
        combined = f"{coordinator_output}\n{client_output}"
        if smoke_error is not None:
            raise RuntimeError(
                "Frozen executable smoke failed.\n"
                f"Coordinator output:\n{coordinator_output}\n"
                f"Client output:\n{client_output}"
            ) from smoke_error
        if "Traceback (most recent call last)" in combined or "ModuleNotFoundError" in combined:
            raise RuntimeError("Packaged executable reported an import or crypto startup error.")
        return {
            "coordinator_version": coordinator_state["version"],
            "client_state": client_state["state"],
            "coordinator_port": coordinator_port,
            "client_port": client_port,
            "client_loopback_only": True,
            "tls_verified": True,
            "uac_payload_equivalent": True,
        }


def _forbidden_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/").casefold()
    return any(
        marker in normalized
        for marker in (
            "tests/test_",
            "aptitude.db",
            "client.sqlite3",
            "client-config.json",
            "device-key.bin",
            "protocol-signing.key",
            "pack-master.key",
            "enrollment.code",
            "client-session.key",
            "browser-session.key",
            "coordinator-ca.key.pem",
            "coordinator-server.key.pem",
            ".ksatpack",
        )
    )


def _assert_payload_safe(name: str, data: bytes) -> None:
    if _forbidden_archive_name(name):
        raise ValueError(f"Archive contains forbidden entry: {name}")
    is_windows_binary = data.startswith(b"MZ")
    if data.startswith(b"SQLite format 3\x00") or (
        not is_windows_binary
        and any(marker in data for marker in _FORBIDDEN_PRIVATE_MARKERS)
    ):
        raise ValueError(f"Archive contains forbidden private/live content: {name}")


def pyinstaller_payload_manifest(executable: Path) -> dict[str, str]:
    """Hash every decompressed CArchive and nested PYZ entry."""
    from PyInstaller.archive.readers import CArchiveReader

    try:
        archive = CArchiveReader(str(Path(executable)))
    except Exception as error:
        raise ValueError(f"Unable to inspect PyInstaller archive: {Path(executable).name}") from error
    manifest: dict[str, str] = {}
    for name, entry in sorted(archive.toc.items()):
        _offset, _length, _uncompressed, _compressed, typecode = entry
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
                nested_data = nested.extract(nested_name, raw=True)
                if nested_data is None:
                    nested_data = b""
                _assert_payload_safe(nested_name, nested_data)
                manifest[f"PYZ:{nested_name}"] = hashlib.sha256(nested_data).hexdigest()
    return manifest


def coordinator_payload_manifest(
    release_executable: Path, smoke_executable: Path
) -> dict[str, str]:
    """Prove the UAC image and runnable probe have identical application payloads."""
    release_manifest = pyinstaller_payload_manifest(release_executable)
    smoke_manifest = pyinstaller_payload_manifest(smoke_executable)
    if release_manifest != smoke_manifest:
        missing = sorted(set(release_manifest) ^ set(smoke_manifest))[:10]
        changed = sorted(
            key
            for key in set(release_manifest) & set(smoke_manifest)
            if release_manifest[key] != smoke_manifest[key]
        )[:10]
        raise ValueError(
            f"Coordinator UAC/smoke payloads differ; entries={missing}, changed={changed}"
        )
    return release_manifest


def _manifest_has_update_key(manifest: dict[str, str]) -> bool:
    return any(
        name.replace("\\", "/").endswith(
            ("/update-release-public.json", ":update-release-public.json")
        )
        or name == "update-release-public.json"
        for name in manifest
    )


def _inspect_installer(
    installer: Path,
    expected_executable: Path,
    innoextract: Path,
    additional_executables: tuple[Path, ...] = (),
) -> None:
    with tempfile.TemporaryDirectory(prefix="ksat-inno-inspect-") as directory:
        destination = Path(directory)
        subprocess.run(
            [
                str(innoextract),
                "--extract",
                "--silent",
                "--output-dir",
                str(destination),
                str(installer),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        files = sorted(path for path in destination.rglob("*") if path.is_file())
        if not files:
            raise ValueError(f"Installer payload is empty: {installer.name}")
        matches = [path for path in files if path.name == expected_executable.name]
        if len(matches) != 1:
            raise ValueError(f"Installer payload executable is ambiguous: {installer.name}")
        if hashlib.sha256(matches[0].read_bytes()).digest() != hashlib.sha256(
            expected_executable.read_bytes()
        ).digest():
            raise ValueError(f"Installer executable differs from dist output: {installer.name}")
        for path in files:
            relative = path.relative_to(destination).as_posix()
            _assert_payload_safe(relative, path.read_bytes())
        pyinstaller_payload_manifest(matches[0])
        for expected in additional_executables:
            extra = [path for path in files if path.name == expected.name]
            if len(extra) != 1 or hashlib.sha256(extra[0].read_bytes()).digest() != hashlib.sha256(expected.read_bytes()).digest():
                raise ValueError(f"Installer executable differs from dist output: {expected.name}")
            pyinstaller_payload_manifest(extra[0])


def inspect_artifacts(
    root: Path,
    python: Path,
    innoextract: Path | None = None,
    signing: ReleaseSigningConfig | None = None,
) -> dict[str, int]:
    signing = _require_signing(signing)
    layout = ReleaseLayout(root)
    artifacts = [
        layout.coordinator_executable,
        layout.client_executable,
        layout.updater_executable,
        layout.coordinator_release_executable,
        layout.client_release_executable,
        layout.coordinator_installer,
        layout.client_installer,
    ]
    sizes: dict[str, int] = {}
    for path in artifacts:
        verify_authenticode_signature(path, signing)
        raw = path.read_bytes()
        if not raw.startswith(b"MZ"):
            raise ValueError(f"Artifact is not a Windows executable: {path.name}")
        if raw.startswith(b"SQLite format 3\x00"):
            raise ValueError(f"Artifact contains forbidden private/live material: {path.name}")
        sizes[path.name] = len(raw)
    if layout.coordinator_executable.read_bytes() != layout.coordinator_release_executable.read_bytes():
        raise ValueError("Coordinator dist/release executables differ.")
    if layout.client_executable.read_bytes() != layout.client_release_executable.read_bytes():
        raise ValueError("Client dist/release executables differ.")
    for executable in (layout.coordinator_executable, layout.client_executable, layout.updater_executable):
        manifest = pyinstaller_payload_manifest(executable)
        if not _manifest_has_update_key(manifest):
            raise ValueError(f"Public update verification key is missing: {executable.name}")
    extractor = Path(innoextract).resolve() if innoextract is not None else discover_innoextract()
    if extractor is None or not extractor.is_file():
        raise FileNotFoundError("innoextract is required for deep installer inspection.")
    _inspect_installer(layout.coordinator_installer, layout.coordinator_executable, extractor)
    _inspect_installer(
        layout.client_installer, layout.client_executable, extractor,
        (layout.updater_executable,),
    )
    update_bundle = (
        layout.test_client_update
        if layout.test_client_update.is_file()
        else layout.client_update
        if layout.client_update.is_file()
        else None
    )
    if update_bundle is not None:
        resource = json.loads(
            (layout.build_dir / "update-release-public.json").read_text("utf-8")
        )
        parse_client_update(
            update_bundle,
            resource["update_signing_public_key_b64"],
            lambda path, publisher: verify_authenticode_signature(path, signing),
        )
        sizes[update_bundle.name] = update_bundle.stat().st_size
    return sizes


def write_sha256s(paths: Iterable[Path], destination: Path) -> Path:
    ordered = sorted((Path(path) for path in paths), key=lambda path: path.name)
    lines = []
    for path in ordered:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(lines), encoding="ascii", newline="\n")
    return destination


def create_hash_manifest(
    layout: ReleaseLayout, signing: ReleaseSigningConfig | None = None
) -> Path:
    signing = _require_signing(signing)
    for artifact in (
        layout.coordinator_release_executable,
        layout.client_release_executable,
        layout.coordinator_installer,
        layout.client_installer,
    ):
        verify_authenticode_signature(artifact, signing)
    artifacts = [
            layout.coordinator_release_executable,
            layout.client_release_executable,
            layout.coordinator_installer,
            layout.client_installer,
        ]
    if layout.test_client_update.is_file():
        artifacts.append(layout.test_client_update)
    elif layout.client_update.is_file():
        artifacts.append(layout.client_update)
    if signing.lab_identity:
        if not layout.lab_trust_certificate.is_file():
            raise FileNotFoundError(layout.lab_trust_certificate)
        if not layout.lab_trust_installer.is_file():
            raise FileNotFoundError(layout.lab_trust_installer)
        artifacts.extend((layout.lab_trust_certificate, layout.lab_trust_installer))
    destination = layout.test_hash_manifest if signing.test_identity else layout.hash_manifest
    return write_sha256s(artifacts, destination)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("build-executables", "smoke", "compile-installers", "inspect", "hashes", "all"),
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--innoextract", type=Path)
    parser.add_argument("--signing-pfx", type=Path)
    parser.add_argument("--signing-password-env", default="KSAT_SIGNING_PFX_PASSWORD")
    parser.add_argument("--signing-publisher")
    parser.add_argument("--timestamp-url")
    parser.add_argument("--test-signing", action="store_true")
    parser.add_argument("--lab-signing", action="store_true")
    parser.add_argument("--update-signing-public-key", type=Path)
    parser.add_argument("--update-signing-private-key", type=Path)
    return parser.parse_args(argv)


def _run_release_command(
    arguments: argparse.Namespace,
    signing: ReleaseSigningConfig,
    *,
    test_update_private_key: Path | None = None,
    lab_update_private_key: Path | None = None,
) -> int:
    layout = ReleaseLayout(arguments.root)
    if signing.lab_identity:
        layout.test_client_update.unlink(missing_ok=True)
        layout.test_hash_manifest.unlink(missing_ok=True)
    elif signing.test_identity:
        layout.client_update.unlink(missing_ok=True)
    if arguments.command in {"build-executables", "all"}:
        layout = build_executables(layout.root, arguments.python, signing)
    if arguments.command in {"smoke", "all"}:
        evidence = smoke_executables(layout.root, arguments.python, signing)
        print(json.dumps(evidence, sort_keys=True))
    if arguments.command in {"compile-installers", "all"}:
        iscc = arguments.iscc or discover_iscc()
        if iscc is None:
            raise RuntimeError("Inno Setup compiler ISCC.exe was not found.")
        compile_installers(layout.root, iscc, signing)
    if arguments.command == "all" and test_update_private_key is not None:
        create_test_client_update_bundle(
            layout,
            test_update_private_key,
            publisher=signing.expected_publisher,
            authenticode_verifier=lambda path, publisher: verify_authenticode_signature(
                path, signing
            ),
        )
    if arguments.command == "all" and lab_update_private_key is not None:
        create_lab_client_update_bundle(
            layout,
            lab_update_private_key,
            publisher=signing.expected_publisher,
            authenticode_verifier=lambda path, publisher: verify_authenticode_signature(
                path, signing
            ),
        )
    if arguments.command in {"inspect", "all"}:
        sizes = inspect_artifacts(
            layout.root, arguments.python, arguments.innoextract, signing
        )
        print(json.dumps(sizes, sort_keys=True))
    if arguments.command in {"hashes", "all"}:
        print(create_hash_manifest(layout, signing))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse(argv)
    public_resource = ReleaseLayout(arguments.root).build_dir / "update-release-public.json"
    if arguments.test_signing and arguments.lab_signing:
        raise SigningConfigurationError("Choose either test signing or lab signing.")
    if arguments.test_signing:
        if any(
            value is not None
            for value in (
                arguments.signing_pfx,
                arguments.signing_publisher,
                arguments.timestamp_url,
            )
        ):
            raise SigningConfigurationError(
                "Test signing cannot be combined with a production signing identity."
            )
        with tempfile.TemporaryDirectory(prefix="ksat-test-signing-") as directory:
            signing = create_ephemeral_test_signing_config(Path(directory))
            test_private_key = Ed25519PrivateKey.generate()
            test_key = test_private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            write_update_public_key_resource(test_key, public_resource)
            private_key_file = Path(directory) / "update-signing-private.key"
            private_key_file.write_bytes(
                test_private_key.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            )
            return _run_release_command(
                arguments, signing, test_update_private_key=private_key_file
            )
    if arguments.lab_signing:
        if arguments.timestamp_url is not None:
            raise SigningConfigurationError("Private-lab signing does not use a timestamp service.")
        if arguments.update_signing_public_key is None:
            raise SigningConfigurationError("The lab update-signing public key is required.")
        if arguments.command == "all" and arguments.update_signing_private_key is None:
            raise SigningConfigurationError(
                "The lab update-signing private key is required to build the managed update bundle."
            )
        signing = lab_signing_config(
            pfx_path=arguments.signing_pfx,
            password_environment_name=arguments.signing_password_env,
            expected_publisher=arguments.signing_publisher,
        )
        write_update_public_key_resource(
            load_release_update_public_key(arguments.update_signing_public_key),
            public_resource,
        )
        layout = ReleaseLayout(arguments.root)
        export_lab_trust_certificate(signing, layout.lab_trust_certificate)
        shutil.copy2(
            layout.root / "scripts" / "install_lab_release_trust.ps1",
            layout.lab_trust_installer,
        )
        return _run_release_command(
            arguments,
            signing,
            lab_update_private_key=arguments.update_signing_private_key,
        )
    if arguments.update_signing_public_key is None:
        raise SigningConfigurationError("The institution update-signing public key is required.")
    write_update_public_key_resource(
        load_release_update_public_key(arguments.update_signing_public_key), public_resource
    )
    signing = production_signing_config(
        pfx_path=arguments.signing_pfx,
        password_environment_name=arguments.signing_password_env,
        expected_publisher=arguments.signing_publisher,
        timestamp_url=arguments.timestamp_url,
    )
    return _run_release_command(arguments, signing)


if __name__ == "__main__":
    raise SystemExit(main())
