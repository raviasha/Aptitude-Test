"""Build a dated Coordinator-only release using the existing private-lab identity.

Run as a module. The signing directory stays outside the repository.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.request

from scripts import windows_release as release
from ksat.coordinator.tls import load_or_create_coordinator_security


def verify_frozen_runtime_upgrades(executable: Path, scratch: Path) -> list[str]:
    """Exercise the installer's real --validate-config command on old settings."""
    verified = []
    for version in ('2.2.5', '2.2.6', '2.2.7'):
        data = scratch / ('upgrade-' + version)
        data.mkdir()
        value = dict(bind_host='0.0.0.0', hostname='localhost', interactive=False,
                     port=9443, version=version)
        original = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        config = data/'coordinator-runtime.json'
        config.write_bytes(original)
        load_or_create_coordinator_security(data, hostname='localhost', port=9443,
                                             lan_ip_addresses=['192.168.254.254'])
        database = data/'aptitude.db'
        connection = sqlite3.connect(database)
        try:
            connection.executescript("CREATE TABLE students(id TEXT, password_hash TEXT);"
                "INSERT INTO students VALUES ('fixture-student','fixture-password-hash');"
                "CREATE TABLE results(id TEXT, score INTEGER);"
                "INSERT INTO results VALUES ('fixture-attempt',17);")
        finally:
            connection.close()
        (data/'results.csv').write_bytes(b'attempt,score\nfixture-attempt,17\n')
        retained = {p.relative_to(data):p.read_bytes() for p in data.rglob('*')
                    if p.is_file() and p != config}
        environment = dict(os.environ, KSAT_COORDINATOR_DATA_DIR=str(data),
                           KSAT_NONINTERACTIVE='1')
        for _ in range(2):
            result = subprocess.run([str(executable), '--validate-config'], env=environment,
                capture_output=True, text=True, timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 0:
                raise RuntimeError(f'Frozen upgrade validation failed for {version}.')
            expected = json.dumps(dict(value, version=release.APP_VERSION),
                                  sort_keys=True, separators=(',', ':')).encode()
            if config.read_bytes() != expected:
                raise RuntimeError('Frozen upgrade changed saved network settings.')
            if (data/f'coordinator-runtime.pre-{release.APP_VERSION}.json.bak').read_bytes() != original:
                raise RuntimeError('Frozen upgrade did not retain the original configuration.')
            if any((data/name).read_bytes() != raw for name, raw in retained.items()):
                raise RuntimeError('Frozen upgrade changed retained records or security identity.')
        verified.append(version)
    return verified


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--signing-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--innoextract', type=Path, help='Compatible Inno 6.7 extractor')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Use a new release output directory.')
    signing_root = args.signing_directory.resolve()
    signing = release.lab_signing_config(
        pfx_path=signing_root / 'KSATLabReleaseSigning.pfx',
        password_environment_name='KSAT_BUILD_PASSWORD',
        expected_publisher=release.LAB_SIGNING_PUBLISHER,
        environ={'KSAT_BUILD_PASSWORD': (signing_root / 'pfx-password.txt').read_text().strip()},
    )
    certificate = root / 'release/KSATLabReleaseSigning.cer'
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    expected = x509.load_der_x509_certificate(certificate.read_bytes()).fingerprint(hashes.SHA1()).hex().upper()
    if signing.expected_thumbprint != expected:
        raise ValueError('Signing identity differs from the published lab identity.')
    compiler = release.discover_iscc()
    extractor = args.innoextract or release.discover_innoextract()
    if compiler is None or extractor is None:
        raise RuntimeError('Inno Setup and innoextract are required.')
    layout = release.ReleaseLayout(root)
    release.inspect_release_inputs(root)
    release.write_version_resources(layout)
    release.write_update_public_key_resource(
        release.load_release_update_public_key(signing_root / 'update-signing-public.key'),
        layout.build_dir / 'update-release-public.json',
    )
    # A fresh work directory avoids stale/read-only caches in synced workspaces.
    with tempfile.TemporaryDirectory(prefix='ksat-coordinator-build-') as folder:
        command = release.build_commands(root, Path(sys.executable))[0]
        command[command.index('--workpath') + 1] = str(Path(folder)/'work')
        command[command.index('--specpath') + 1] = str(Path(folder)/'spec')
        subprocess.run(command, cwd=root, check=True)
    release.sign_and_verify_artifact(layout.coordinator_executable, signing)
    # Verify the same application payload through a non-elevated disposable smoke build.
    with tempfile.TemporaryDirectory(prefix='ksat-coordinator-smoke-') as folder:
        scratch = Path(folder)
        command = release.smoke_coordinator_command(root, Path(sys.executable), scratch / 'smoke')
        command.remove('--windowed')
        subprocess.run(command, cwd=root, check=True)
        smoke = scratch / 'smoke/dist/KSATCoordinatorSmoke.exe'
        release.coordinator_payload_manifest(layout.coordinator_executable, smoke)
        upgrade_versions = verify_frozen_runtime_upgrades(smoke, scratch)
        port = release._free_port()
        data = scratch / 'data'
        security = load_or_create_coordinator_security(data, hostname='localhost', port=port, lan_ip_addresses=['192.168.254.254'])
        environment = dict(os.environ, KSAT_COORDINATOR_DATA_DIR=str(data), KSAT_COORDINATOR_HOSTNAME='localhost', KSAT_COORDINATOR_PORT=str(port), KSAT_COORDINATOR_BIND_HOST='127.0.0.1', KSAT_NONINTERACTIVE='1')
        context = ssl.create_default_context(cafile=str(security.ca_certificate_path))
        process = subprocess.Popen([str(smoke)], cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            if release._wait_json(f'https://localhost:{port}/api/coordinator-build', context=context) != {'version': release.APP_VERSION}:
                raise RuntimeError('Frozen Coordinator startup failed.')
            if release._wait_json(f'https://localhost:{port}/api/build', context=context) != {'version': '2.1.0'}:
                raise RuntimeError('Existing client compatibility handshake failed.')
            with urllib.request.urlopen(f'https://localhost:{port}/', context=context, timeout=10) as page:
                if f'Coordinator version {release.APP_VERSION}' not in page.read().decode('utf-8'):
                    raise RuntimeError('Frozen Coordinator version label missing.')
        finally:
            logs = release._terminate_process(process)
        if 'Traceback' in logs:
            raise RuntimeError('Frozen Coordinator reported a traceback.')
    # Publish only after the executable has passed its startup/payload checks.
    output.mkdir(parents=True)
    executable = output / f'KSATCoordinator-{release.APP_VERSION}.exe'
    shutil.copy2(layout.coordinator_executable, executable)
    definitions = release.prepare_coordinator_installer_trust(layout, signing)
    subprocess.run([str(compiler), *definitions, '/O' + str(output), str(root / 'installer/KSATCoordinator.iss')], cwd=root, check=True)
    installer = output / f'KSATCoordinatorSetup-{release.APP_VERSION}.exe'
    release.sign_and_verify_artifact(installer, signing)
    release._inspect_installer(installer, layout.coordinator_executable, extractor,
        additional_resources=(layout.dist_dir/'publisher.cer', root/'scripts/install_coordinator_release_trust.ps1'))
    release.write_sha256s([executable, installer], output / 'SHA256SUMS.txt')
    evidence = {'version':release.APP_VERSION, 'publisher':signing.expected_publisher,
                'thumbprint':signing.expected_thumbprint, 'frozen_https_startup':'passed',
                'installer_payload':'verified', 'uac_payload_equivalence':'verified',
                'frozen_runtime_upgrade_sources':upgrade_versions,
                'runtime_upgrade_retained_data':'database, CSV, network settings and security identity unchanged; original config backed up',
                'client_binaries_rebuilt':False}
    (output / 'verification.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
