"""Build, smoke-test, inspect, and hash the two Windows release products."""

from __future__ import annotations

import argparse
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

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


APP_VERSION = "2.0.0"
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


@dataclass(frozen=True)
class ReleaseSigningConfig:
    pfx_path: Path
    password: str = field(repr=False)
    expected_publisher: str
    expected_thumbprint: str
    timestamp_url: str | None
    test_identity: bool = False

    def __post_init__(self) -> None:
        path = Path(self.pfx_path).resolve()
        object.__setattr__(self, "pfx_path", path)
        if not path.is_file() or not self.password:
            raise SigningConfigurationError("A readable signing identity and secret are required.")
        if not self.expected_publisher or not self.expected_thumbprint:
            raise SigningConfigurationError("The signing publisher identity must be pinned.")
        if self.test_identity:
            if self.timestamp_url is not None or self.expected_publisher != _TEST_SIGNING_SUBJECT:
                raise SigningConfigurationError("The nonproduction signing identity is invalid.")
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
    accepted_statuses = {"Valid", "UnknownError", "NotTrusted"} if config.test_identity else {"Valid"}
    if (
        evidence.get("status") not in accepted_statuses
        or evidence.get("signer_subject") != config.expected_publisher
        or str(evidence.get("signer_thumbprint") or "").upper()
        != config.expected_thumbprint
        or not config.test_identity
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
    coordinator.extend(_add_data(root / "static", "static"))
    coordinator.extend(_add_data(root / "templates", "templates"))
    coordinator.extend(
        ["--hidden-import", "app", "--uac-admin", str(root / "coordinator_main.py")]
    )

    client = _common_pyinstaller(root, python, "KSATClient", "client")
    client.extend(_add_data(root / "static" / "client", "static/client"))
    client.extend(_add_data(root / "static" / "branding", "static/branding"))
    client.extend(_add_data(root / "static" / "branding.css", "static"))
    client.extend(_add_data(root / "static" / "math.css", "static"))
    client.append(str(root / "client_app.py"))
    return [coordinator, client]


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
    command.extend(_add_data(root / "static", "static"))
    command.extend(_add_data(root / "templates", "templates"))
    command.extend(["--hidden-import", "app"])
    command.append(str(root / "coordinator_main.py"))
    return command


def _version_resource(product_name: str, executable_name: str) -> str:
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(2,0,0,0), prodvers=(2,0,0,0), mask=0x3f,
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
    for executable in (layout.coordinator_executable, layout.client_executable):
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
    for name in ("coordinator", "client"):
        _safe_remove_tree(layout.build_dir / name, layout.root)
    for executable in (layout.coordinator_executable, layout.client_executable):
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
    for path in (layout.coordinator_executable, layout.client_executable):
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
    if data.startswith(b"SQLite format 3\x00") or any(
        marker in data for marker in _FORBIDDEN_PRIVATE_MARKERS
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


def _inspect_installer(
    installer: Path,
    expected_executable: Path,
    innoextract: Path,
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
        if raw.startswith(b"SQLite format 3\x00") or any(
            marker in raw for marker in _FORBIDDEN_PRIVATE_MARKERS
        ):
            raise ValueError(f"Artifact contains forbidden private/live material: {path.name}")
        sizes[path.name] = len(raw)
    if layout.coordinator_executable.read_bytes() != layout.coordinator_release_executable.read_bytes():
        raise ValueError("Coordinator dist/release executables differ.")
    if layout.client_executable.read_bytes() != layout.client_release_executable.read_bytes():
        raise ValueError("Client dist/release executables differ.")
    for executable in (layout.coordinator_executable, layout.client_executable):
        pyinstaller_payload_manifest(executable)
    extractor = Path(innoextract).resolve() if innoextract is not None else discover_innoextract()
    if extractor is None or not extractor.is_file():
        raise FileNotFoundError("innoextract is required for deep installer inspection.")
    _inspect_installer(layout.coordinator_installer, layout.coordinator_executable, extractor)
    _inspect_installer(layout.client_installer, layout.client_executable, extractor)
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
    return write_sha256s(
        [
            layout.coordinator_release_executable,
            layout.client_release_executable,
            layout.coordinator_installer,
            layout.client_installer,
        ],
        layout.hash_manifest,
    )


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
    return parser.parse_args(argv)


def _run_release_command(arguments: argparse.Namespace, signing: ReleaseSigningConfig) -> int:
    layout = ReleaseLayout(arguments.root)
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
            return _run_release_command(arguments, signing)
    signing = production_signing_config(
        pfx_path=arguments.signing_pfx,
        password_environment_name=arguments.signing_password_env,
        expected_publisher=arguments.signing_publisher,
        timestamp_url=arguments.timestamp_url,
    )
    return _run_release_command(arguments, signing)


if __name__ == "__main__":
    raise SystemExit(main())
