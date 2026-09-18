"""Build one self-contained Debian installer on the named Ubuntu release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("18.04", "22.04"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True, help="Git base commit, supplied by the host for WSL worktrees")
    args = parser.parse_args()
    release = dict(line.strip().split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != args.target:
        parser.error("Build on the exact target Ubuntu release.")
    if platform.machine() != "x86_64":
        parser.error("Only amd64 lab PCs are supported by this build.")
    if sys.version_info < (3, 11) or sqlite3.sqlite_version_info < (3, 35):
        parser.error("Build requires Python 3.11+ and SQLite 3.35+; do not bundle Ubuntu 18.04's system SQLite.")
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Fresh staging avoids mixing assets with an earlier build or live data.
    with tempfile.TemporaryDirectory(prefix="ksat-linux-build-") as temporary:
        work = Path(temporary)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
            "--name", "KSATClient", "--distpath", str(work / "dist"),
            "--workpath", str(work / "work"), "--specpath", str(work),
            "--paths", str(root), "--collect-all", "uvicorn",
            "--add-data", f"{root / 'static' / 'client'}:static/client",
            "--add-data", f"{root / 'static' / 'branding'}:static/branding",
            "--add-data", f"{root / 'static' / 'branding.css'}:static",
            "--add-data", f"{root / 'static' / 'math.css'}:static",
            str(root / "linux_client_main.py"),
        ], check=True, cwd=root)
        executable = work / "dist" / "KSATClient" / "KSATClient"
        subprocess.run([str(executable), "--version"], check=True)
        package = work / "package"
        (package / "opt").mkdir(parents=True)
        shutil.copytree(executable.parent, package / "opt" / "ksat-client")
        for source, destination in (
            ("ksat-client.service", "lib/systemd/system/ksat-client.service"),
            ("ksat-client.desktop", "usr/share/applications/ksat-client.desktop"),
            ("ksat-client-setup.desktop", "usr/share/applications/ksat-client-setup.desktop"),
            ("setup_gui.py", "opt/ksat-client/setup_gui.py"),
            ("org.ksat.client.policy", "usr/share/polkit-1/actions/org.ksat.client.policy"),
        ):
            target = package / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((root / "installer/linux" / source).read_text(), newline="\n")
            target.chmod(0o644)
        metadata = package / "DEBIAN"
        metadata.mkdir()
        for name in ("preinst", "postinst", "prerm", "postrm"):
            target = metadata / name
            target.write_text((root / "installer/linux" / name).read_text(), newline="\n")
            target.chmod(0o755)
        version = "2.0.0+ubuntu2." + args.target.replace(".", "")
        (metadata / "control").write_text(
            f"Package: ksat-client\nVersion: {version}\nArchitecture: amd64\n"
            "Maintainer: KSAT Lab\nSection: education\nPriority: optional\n"
            f"Depends: libc6 (>= {'2.27' if args.target == '18.04' else '2.35'}), libgcc1, zlib1g, systemd, adduser, xdg-utils, python3, python3-gi, gir1.2-gtk-3.0, policykit-1\n"
            "Description: KSAT lab client for a Windows faculty coordinator\n"
            " Includes the assessment runtime and a graphical first-run setup wizard.\n"
        )
        dependencies = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
        provenance = {"target": args.target, "architecture": "amd64", "python": sys.version, "sqlite": sqlite3.sqlite_version,
                      "dependencies": dependencies.splitlines(), "status": "TEST BUILD",
                      "source_revision": args.source_revision,
                      "source_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in sorted([root / "linux_client_main.py", root / "client_app.py", Path(__file__).resolve()]
                                                        + list((root / "ksat").rglob("*.py"))
                                                        + list((root / "installer/linux").glob("*"))) if p.is_file()}}
        (package / "opt/ksat-client/build-info.json").write_text(json.dumps(provenance, indent=2))
        artifact = output / f"KSATClient-Ubuntu-{args.target}-amd64.deb"
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(package), str(artifact)], check=True)
        (output / (artifact.name + ".sha256")).write_text(hashlib.sha256(artifact.read_bytes()).hexdigest() + "  " + artifact.name + "\n")
        (output / (artifact.name + ".build-info.json")).write_text(json.dumps(provenance, indent=2))
        print(artifact)


if __name__ == "__main__":
    main()
