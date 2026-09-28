"""Validate the frozen upgrade against disposable data, never a lab database.

Run with --build-probe to build the non-elevated smoke image. Its decompressed
application payload must exactly match the signed production Coordinator.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.windows_release import (ReleaseLayout, smoke_coordinator_command,
    coordinator_payload_manifest, _free_port, _wait_json, _terminate_process)
from ksat.public_trust import validate_public_bundle
from datetime import datetime, timezone


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-probe", action="store_true")
    args = parser.parse_args()
    build_root = ROOT / "build" / "coordinator-upgrade-smoke"
    if args.build_probe:
        command = smoke_coordinator_command(ROOT, Path(sys.executable), build_root)
        command.remove("--clean")
        subprocess.run(command, cwd=ROOT, check=True)
    probe = build_root / "dist" / "KSATCoordinatorSmoke.exe"
    coordinator_payload_manifest(ReleaseLayout(ROOT).coordinator_executable, probe)
    with tempfile.TemporaryDirectory(prefix="ksat-runtime-upgrade-smoke-") as folder:
        root = Path(folder)
        data = root / "KSAT Coordinator"
        data.mkdir()
        port = _free_port()
        config = data / "coordinator-runtime.json"
        current = dict(bind_host="127.0.0.1", hostname="localhost", interactive=False,
                       port=port, version="2.1.0")
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        config.write_bytes(encode(current))
        env = {k:v for k,v in os.environ.items() if not k.startswith("KSAT_")}
        env.update(ProgramData=str(root), KSAT_COORDINATOR_DATA_DIR=str(data))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        def start():
            process = subprocess.Popen([str(probe)], env=env, cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            try:
                ca = data / "public" / "coordinator-ca.pem"
                deadline = time.monotonic() + 60
                while not ca.is_file() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Frozen Coordinator exited before TLS startup.")
                    time.sleep(.2)
                state = _wait_json(f"https://localhost:{port}/api/build",
                                  context=ssl.create_default_context(cafile=str(ca)), timeout=60)
                assert state == {"version": "2.1.0"}, state
                return process
            except Exception:
                _terminate_process(process)
                raise

        def students():
            with closing(sqlite3.connect(data / "aptitude.db")) as db:
                return db.execute("SELECT * FROM students ORDER BY student_id").fetchall()

        # Initialize disposable real Coordinator state and student accounts.
        process = start()
        try:
            before_students = students()
        finally:
            _terminate_process(process)
        assert before_students, "Expected disposable sample student accounts."
        identities = {p.name:p.read_bytes() for p in (data / "secrets").iterdir()
                      if p.is_file() and not p.name.startswith(".")}
        ca_before = (data / "public" / "coordinator-ca.pem").read_bytes()
        metadata_path = data / "public" / "coordinator-public.json"
        metadata = json.loads(metadata_path.read_bytes())
        old_metadata = dict(metadata, version="2.0.0")
        metadata_path.write_bytes(encode(old_metadata))
        legacy = encode(dict(current, version="2.0.0"))
        config.write_bytes(legacy)
        db_before = (data / "aptitude.db").read_bytes()

        # This is the exact command invoked by Coordinator Setup on upgrade.
        subprocess.run([str(probe), "--validate-config"], env=env, cwd=ROOT,
                       creationflags=flags, check=True, timeout=60)
        assert config.read_bytes() == encode(current)
        assert (data / "coordinator-runtime.pre-2.1.0.json.bak").read_bytes() == legacy
        assert (data / "aptitude.db").read_bytes() == db_before
        process = start()
        try:
            assert students() == before_students
            assert (data / "public" / "coordinator-ca.pem").read_bytes() == ca_before
            assert {p.name:p.read_bytes() for p in (data / "secrets").iterdir()
                    if p.is_file() and not p.name.startswith(".")} == identities
            validate_public_bundle(metadata["coordinator_url"], ca_before,
                metadata_path.read_bytes(), now=datetime.now(timezone.utc))
        finally:
            _terminate_process(process)
    print("PASS: frozen installer-validation upgrade, HTTPS startup, student preservation, unchanged identity, builder-compatible public exports, identical release/probe payloads.")


if __name__ == "__main__":
    run()
