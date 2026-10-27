"""Privileged entry point for installing one staged KSAT client update."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from ksat.client.install_guard import installed_version
from ksat.client.updater import HealthVerifier, Updater
from ksat.client.preservation import authenticated_store_ready
from ksat.update_protocol import load_update_public_key
from ksat.windows_authenticode import verify_authenticode


_BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def configuration_digest(data: Path) -> str:
    digest = hashlib.sha256()
    for name in ("client-config.json", "coordinator-url.json"):
        path = data / name
        digest.update(name.encode("ascii"))
        digest.update(path.read_bytes() if path.exists() else b"<absent>")
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
    from client_install_guard import require_protected_directory
    require_protected_directory(updates, allow_service=True)
    require_protected_directory(data / "state", allow_service=True)
    require_protected_directory(data / "identity", allow_service=True)
    def version_probe():
        with urllib.request.urlopen("http://127.0.0.1:8010/api/build",timeout=5) as response:
            import json
            return json.loads(response.read().decode("utf-8"))
    def store_probe():
        return authenticated_store_ready(data)
    version = installed_version(Path(program_data))
    if version is None:
        raise ValueError("The installed client version could not be established.")
    updater=Updater(
        updates,load_update_public_key(_BUNDLE_ROOT/"update-release-public.json"),verify_authenticode,
        WindowsServices(),WindowsInstallers(),HealthVerifier(version_probe,store_probe),
        installed_version=version, data_root=data,
    )
    return 0 if updater.run(args.request).success else 1


if __name__ == "__main__": raise SystemExit(main())
