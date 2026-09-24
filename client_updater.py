"""Privileged entry point for installing one staged KSAT client update."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from ksat.client.identity import DeviceIdentityStore, derive_state_integrity_key
from ksat.client.store import ClientStore
from ksat.client.updater import HealthVerifier, Updater
from ksat.update_protocol import load_update_public_key
from ksat.windows_authenticode import verify_authenticode


_BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    paths = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    for item in paths:
        digest.update(item.relative_to(path.parent).as_posix().encode("utf-8")); digest.update(item.read_bytes())
    return digest.hexdigest()


class WindowsServices:
    def _run(self, action: str, name: str, timeout: int) -> None:
        subprocess.run(["sc.exe", action, name], check=True, timeout=timeout, capture_output=True,
                       creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    def stop(self, name: str, timeout: int) -> None: self._run("stop",name,timeout)
    def start(self, name: str, timeout: int) -> None: self._run("start",name,timeout)


class WindowsInstallers:
    def run(self, path: Path, arguments: list[str], timeout: int) -> None:
        subprocess.run([str(path),*arguments],check=True,timeout=timeout,
                       creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))


def main(argv=None) -> int:
    import argparse
    parser=argparse.ArgumentParser(prog="KSATClientUpdater")
    parser.add_argument("--request",type=Path,required=True)
    args=parser.parse_args(argv)
    program_data=os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA")
    if not program_data: raise ValueError("ProgramData is unavailable.")
    data=Path(program_data).resolve()/"KSAT Client"; updates=data/"updates"
    identity_path=data/"identity"; config_path=data/"client-config.json"; state_path=data/"state"
    database_path=state_path/"client.sqlite3"
    before=(_digest(identity_path),_digest(config_path),_digest(database_path))
    def version_probe():
        with urllib.request.urlopen("http://127.0.0.1:8765/api/build",timeout=5) as response:
            import json
            return json.loads(response.read().decode("utf-8"))
    def store_probe():
        identity=DeviceIdentityStore(identity_path).load_or_create()
        store=ClientStore(state_path/"client.sqlite3",integrity_key=derive_state_integrity_key(identity),integrity_anchor_path=identity_path/"state-anchor.json")
        try: store.migration_summary(); return True
        finally: store.close()
    updater=Updater(
        updates,load_update_public_key(_BUNDLE_ROOT/"update-release-public.json"),verify_authenticode,
        WindowsServices(),WindowsInstallers(),HealthVerifier(version_probe,store_probe),
        installed_version="2.1.0",identity_digest=before[0],config_digest=before[1],state_digest=before[2],
        current_digest_probe=lambda:(_digest(identity_path),_digest(config_path),_digest(database_path)),
    )
    return 0 if updater.run(args.request).success else 1


if __name__ == "__main__": raise SystemExit(main())
