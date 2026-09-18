"""Installed Linux client: privileged setup and unprivileged systemd service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

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
VERSION = "2.0.0+ubuntu1"


def require_root():
    if os.geteuid() != 0:
        raise PermissionError("Run client configuration with sudo.")


def protected_directory(path: Path, uid: int, gid: int, mode: int):
    if path.is_symlink():
        raise ValueError("A client directory cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def configure(base_url: str, ca: Path, metadata: Path):
    require_root()
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
        # Trust is private to the service and is used explicitly by its HTTPS client.
        # No global certificate-store modification is needed for the loopback UI.
        for path in (DATA_DIR / "trust",):
            protected_directory(path, 0, account.pw_gid, 0o750)
        for path in (DATA_DIR / "client-config.json", DATA_DIR / "trust" / "coordinator-ca.pem"):
            if path.is_symlink():
                raise ValueError("Client configuration cannot be a symbolic link.")
            os.chown(path, 0, account.pw_gid)
            os.chmod(path, 0o640)
    # The service must be able to take the lock initially made by root.
    os.chown(DATA_DIR / "state" / ".client.lock", account.pw_uid, account.pw_gid)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)
    print("KSAT client configured. Open KSAT Lab Client from Applications.")


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
        config = ClientRuntimeConfigStore(ClientConfigStore(DATA_DIR / "client-config.json"), DATA_DIR / "coordinator-url.json").load()
        _validate_production_trust(config)
        LinuxKeyProtector(KEY_PATH)
        print("Configuration and Linux device wrapping key are valid.")
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
