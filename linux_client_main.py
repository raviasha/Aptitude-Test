"""Installed Linux client: privileged setup and unprivileged systemd service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

from client_app import (
    ClientProcessLock, ClientConfigStore, ClientRuntimeConfigStore,
    _load_locked_production_services, _validate_production_trust,
    create_client_app, install_client_configuration,
)
from ksat.client.linux_identity import LinuxKeyProtector

PROGRAM_DATA = Path("/var/lib/ksat")
DATA_DIR = PROGRAM_DATA / "KSAT Client"
KEY_PATH = Path("/etc/ksat-client/device-wrap.key")
SERVICE = "ksat-client.service"
VERSION = "2.0.0+ubuntu2"


def require_root():
    if os.geteuid() != 0:
        raise PermissionError("Administrator authorization is required. Open KSAT Client Setup from Applications.")


def protected_directory(path: Path, uid: int, gid: int, mode: int):
    if path.is_symlink():
        raise ValueError("A client directory cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def configure(base_url: str, ca: Path, metadata: Path):
    require_root()
    # Validate a private snapshot before touching the live service or its state.
    # Reuse the shared validator, including CA/signing-key hash and URL checks.
    with tempfile.TemporaryDirectory(prefix="ksat-setup-") as directory:
        staging = Path(directory)
        for source, name, limit in ((ca, "ca.pem", 256 * 1024), (metadata, "public.json", 64 * 1024)):
            try:
                with Path(source).open("rb") as stream:
                    content = stream.read(limit + 1)
            except OSError as error:
                raise ValueError("Cannot read both coordinator public files. Select them again.") from error
            if not content or len(content) > limit:
                raise ValueError("Coordinator public trust file has an invalid size.")
            (staging / name).write_bytes(content)
        candidate = install_client_configuration(staging / "check", base_url=base_url,
                                                 ca_source=staging / "ca.pem", metadata_source=staging / "public.json")
        config_path = DATA_DIR / "client-config.json"
        if config_path.is_file():
            existing = ClientRuntimeConfigStore(ClientConfigStore(config_path), DATA_DIR / "coordinator-url.json").load()
            _validate_production_trust(existing)
            if (existing.coordinator_base_url != candidate.coordinator_base_url
                    or existing.coordinator_signing_public_key_b64 != candidate.coordinator_signing_public_key_b64
                    or Path(existing.trusted_ca_path).read_bytes() != (staging / "ca.pem").read_bytes()):
                raise ValueError("This PC is already configured for a different coordinator. Existing settings and attempts were kept. Ask your lab administrator before changing servers.")
            start_configured_service()
            return
        _configure_new_client(base_url, staging / "ca.pem", staging / "public.json")


def _configure_new_client(base_url: str, ca: Path, metadata: Path):
    subprocess.run(["systemctl", "stop", SERVICE], check=True)
    account = pwd.getpwnam("ksat-client")
    os.umask(0o027)
    protected_directory(PROGRAM_DATA, 0, account.pw_gid, 0o750)
    protected_directory(DATA_DIR, account.pw_uid, account.pw_gid, 0o750)
    for name in ("identity", "state", "packs"):
        protected_directory(DATA_DIR / name, account.pw_uid, account.pw_gid, 0o700)
    protected_directory(KEY_PATH.parent, 0, account.pw_gid, 0o750)
    with ClientProcessLock(DATA_DIR / "state"):
        if not os.path.lexists(KEY_PATH):
            if any((DATA_DIR / "identity").iterdir()) or (DATA_DIR / "state" / "client.sqlite3").exists():
                raise ValueError("Wrapping key is missing from an existing installation. Restore its matching backup.")
            descriptor = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(KEY_PATH, 0, account.pw_gid)
        LinuxKeyProtector(KEY_PATH)
        install_client_configuration(PROGRAM_DATA, base_url=base_url, ca_source=ca, metadata_source=metadata)
        finalize_setup_permissions(account)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)
    print("KSAT client configured. Open KSAT Lab Client from Applications.")


def finalize_setup_permissions(account):
    # Idempotent after config publication: power loss must not strand root-only
    # configuration/CA files or a root-owned lock. Never rewrite their contents.
    protected_directory(DATA_DIR / "trust", 0, account.pw_gid, 0o750)
    paths = [(DATA_DIR / "client-config.json", 0, 0o640),
             (DATA_DIR / "trust/coordinator-ca.pem", 0, 0o640),
             (KEY_PATH, 0, 0o640)]
    lock = DATA_DIR / "state/.client.lock"
    if os.path.lexists(lock):
        paths.append((lock, account.pw_uid, 0o600))
    for path, uid, mode in paths:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Client configuration must be a regular file.")
            os.fchown(descriptor, uid, account.pw_gid)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)


def validate_configuration():
    if not (DATA_DIR / "client-config.json").is_file():
        raise ValueError("This PC is not configured yet. Select the server address and both public files, then click Configure client.")
    config = ClientRuntimeConfigStore(ClientConfigStore(DATA_DIR / "client-config.json"), DATA_DIR / "coordinator-url.json").load()
    _validate_production_trust(config)
    if not KEY_PATH.is_file():
        raise ValueError("Wrapping key is missing from an existing installation. Restore its matching backup.")
    LinuxKeyProtector(KEY_PATH)


def start_configured_service():
    require_root()
    validate_configuration()
    finalize_setup_permissions(pwd.getpwnam("ksat-client"))
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)


def load_services():
    account = pwd.getpwnam("ksat-client")
    if os.geteuid() != account.pw_uid:
        raise PermissionError("The client service must run as ksat-client.")
    protector = LinuxKeyProtector(KEY_PATH)
    lock = ClientProcessLock(DATA_DIR / "state").acquire()
    try:
        return _load_locked_production_services(DATA_DIR, lock, identity_protector=protector)
    except BaseException:
        lock.release()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="KSAT Ubuntu lab client")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--version", action="store_true")
    operation.add_argument("--configure", action="store_true")
    operation.add_argument("--validate-config", action="store_true")
    operation.add_argument("--start-service", action="store_true")
    operation.add_argument("--service", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args(argv)
    if args.version:
        print(json.dumps({"version": VERSION, "platform": "linux", "protocol": 1}))
        return 0
    if args.configure:
        if not args.base_url or not args.ca or not args.metadata:
            parser.error("--configure requires --base-url, --ca and --metadata")
        configure(args.base_url, args.ca, args.metadata)
    elif args.validate_config:
        validate_configuration()
        print("Configuration and Linux device wrapping key are valid.")
    elif args.start_service:
        start_configured_service()
    else:
        import uvicorn
        services = load_services()
        uvicorn.run(create_client_app(services), host="127.0.0.1", port=8010, log_level="warning")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"KSAT: {error}", file=sys.stderr)
        sys.exit(1)
