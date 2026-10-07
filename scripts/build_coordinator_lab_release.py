"""Build a dated Coordinator-only release using the existing private-lab identity.

Run as a module. The signing directory stays outside the repository.
"""
import argparse
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
    subprocess.run(release.build_commands(root, Path(sys.executable))[0], cwd=root, check=True)
    release.sign_and_verify_artifact(layout.coordinator_executable, signing)
    # Verify the same application payload through a non-elevated disposable smoke build.
    with tempfile.TemporaryDirectory(prefix='ksat-coordinator-smoke-') as folder:
        scratch = Path(folder)
        command = release.smoke_coordinator_command(root, Path(sys.executable), scratch / 'smoke')
        command.remove('--windowed')
        subprocess.run(command, cwd=root, check=True)
        smoke = scratch / 'smoke/dist/KSATCoordinatorSmoke.exe'
        release.coordinator_payload_manifest(layout.coordinator_executable, smoke)
        port = release._free_port()
        data = scratch / 'data'
        security = load_or_create_coordinator_security(data, hostname='localhost', port=port, lan_ip_addresses=['192.168.254.254'])
        environment = dict(os.environ, KSAT_COORDINATOR_DATA_DIR=str(data), KSAT_COORDINATOR_HOSTNAME='localhost', KSAT_COORDINATOR_PORT=str(port), KSAT_COORDINATOR_BIND_HOST='127.0.0.1', KSAT_NONINTERACTIVE='1')
        context = ssl.create_default_context(cafile=str(security.ca_certificate_path))
        process = subprocess.Popen([str(smoke)], cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            if release._wait_json(f'https://localhost:{port}/api/build', context=context) != {'version': release.APP_VERSION}:
                raise RuntimeError('Frozen Coordinator startup failed.')
        finally:
            logs = release._terminate_process(process)
        if 'Traceback' in logs:
            raise RuntimeError('Frozen Coordinator reported a traceback.')
    # Publish only after the executable has passed its startup/payload checks.
    output.mkdir(parents=True)
    executable = output / f'KSATCoordinator-{release.APP_VERSION}.exe'
    shutil.copy2(layout.coordinator_executable, executable)
    subprocess.run([str(compiler), '/O' + str(output), str(root / 'installer/KSATCoordinator.iss')], cwd=root, check=True)
    installer = output / f'KSATCoordinatorSetup-{release.APP_VERSION}.exe'
    release.sign_and_verify_artifact(installer, signing)
    release._inspect_installer(installer, layout.coordinator_executable, extractor)
    release.write_sha256s([executable, installer], output / 'SHA256SUMS.txt')
    evidence = {'version':release.APP_VERSION, 'publisher':signing.expected_publisher,
                'thumbprint':signing.expected_thumbprint, 'frozen_https_startup':'passed',
                'installer_payload':'verified', 'uac_payload_equivalence':'verified',
                'client_binaries_rebuilt':False}
    (output / 'verification.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
