"""Build only the coordinator and verify frozen startup with stale LAN SANs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile

from scripts import windows_release as release
from ksat.coordinator.tls import load_or_create_coordinator_security


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-executable", action="store_true", help="Recheck an existing executable; source equivalence is still verified against a fresh smoke build")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    layout = release.ReleaseLayout(root)
    python = Path(sys.executable)
    release.inspect_release_inputs(root)
    layout.build_dir.mkdir(parents=True, exist_ok=True)
    release.write_version_resources(layout)
    with tempfile.TemporaryDirectory(prefix="ksat-dhcp-build-") as directory:
        temporary = Path(directory)
        signing = release.create_ephemeral_test_signing_config(temporary)
        if not args.reuse_executable:
            subprocess.run(release.build_commands(root, python)[0], cwd=root, check=True)
        release.sign_and_verify_artifact(layout.coordinator_executable, signing)
        shutil.copy2(layout.coordinator_executable, layout.coordinator_release_executable)
        compiler = release.discover_iscc()
        extractor = Path(os.environ["INNOEXTRACT_EXE"]) if os.environ.get("INNOEXTRACT_EXE") else release.discover_innoextract()
        if compiler is None or extractor is None:
            raise RuntimeError("Inno Setup and innoextract are required")
        subprocess.run([str(compiler), str(root / "installer/KSATCoordinator.iss")], check=True)
        release.sign_and_verify_artifact(layout.coordinator_installer, signing)
        release._inspect_installer(layout.coordinator_installer, layout.coordinator_executable, extractor)
        smoke_command = release.smoke_coordinator_command(root, python, temporary / "smoke")
        smoke_command.remove("--windowed")
        subprocess.run(smoke_command, cwd=root, check=True)
        smoke = temporary / "smoke/dist/KSATCoordinatorSmoke.exe"
        release.coordinator_payload_manifest(layout.coordinator_executable, smoke)
        console_smoke = smoke.with_name("KSATCoordinatorConsoleSmoke.exe")
        shutil.copy2(smoke, console_smoke)
        # Reuse analysis but rebuild with PyInstaller's real windowless bootloader.
        windowless_command = release.smoke_coordinator_command(root, python, temporary / "smoke")
        windowless_command.remove("--clean")
        subprocess.run(windowless_command, cwd=root, check=True)
        release.coordinator_payload_manifest(layout.coordinator_executable, smoke)
        data = temporary / "coordinator-data"
        port = release._free_port()
        security = load_or_create_coordinator_security(data, hostname="localhost", port=port, lan_ip_addresses=["192.168.254.254"])
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (data / "secrets").iterdir() if p.is_file()}
        environment = dict(os.environ, KSAT_COORDINATOR_DATA_DIR=str(data), KSAT_COORDINATOR_HOSTNAME="localhost", KSAT_COORDINATOR_PORT=str(port), KSAT_COORDINATOR_BIND_HOST="127.0.0.1", KSAT_NONINTERACTIVE="1")
        context = ssl.create_default_context(cafile=str(security.ca_certificate_path))
        for executable in (console_smoke, console_smoke, smoke, smoke):
            process = subprocess.Popen([str(executable)], cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                assert release._wait_json(f"https://localhost:{port}/api/build", context=context) == {"version": release.APP_VERSION}
            finally:
                output = release._terminate_process(process)
                print(output, flush=True)
            assert "Traceback" not in output, output
            after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (data / "secrets").iterdir() if p.is_file()}
            assert before == after, "Startup changed coordinator identity or certificates"
        release.write_sha256s([layout.coordinator_release_executable, layout.coordinator_installer], layout.release_dir / "COORDINATOR-DHCP-SHA256SUMS.txt")
        evidence = {"test_signed": True, "hostname_https_with_old_lan_certificate": "passed twice each in console and GUI subsystems", "security_files_preserved": True, "uac_smoke_payload_match": True, "installer_payload_verified": True, "client_binaries_rebuilt": False}
        (layout.release_dir / "coordinator-dhcp-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
