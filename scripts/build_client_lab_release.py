"""Build the interactive client bootstrap and matching public-only lab builder."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request

from scripts import windows_release as release
from scripts import build_lab_package_builder as builder
from ksat.lab_builder.payload import REQUIRED_FILES, load_approved_payload
from ksat.lab_builder.signing import PUBLISHER, THUMBPRINT


def smoke_client(root, layout):
    """Real frozen client against an isolated source server; no installed services."""
    with tempfile.TemporaryDirectory(prefix='ksat-client-smoke-') as folder:
        scratch = Path(folder)
        server_data = scratch/'server'
        port = release._free_port()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        environment = dict(os.environ, KSAT_COORDINATOR_DATA_DIR=str(server_data),
            KSAT_COORDINATOR_HOSTNAME='localhost', KSAT_COORDINATOR_PORT=str(port),
            KSAT_COORDINATOR_BIND_HOST='127.0.0.1', KSAT_NONINTERACTIVE='1')
        server = subprocess.Popen([sys.executable, str(root/'coordinator_main.py')],
            cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=flags)
        client = None
        try:
            import time
            ca = server_data/'public/coordinator-ca.pem'
            deadline = time.monotonic()+45
            while not ca.exists() and time.monotonic()<deadline:
                if server.poll() is not None:
                    raise RuntimeError('Disposable source server exited.')
                time.sleep(.2)
            context = ssl.create_default_context(cafile=str(ca))
            assert release._wait_json(f'https://localhost:{port}/api/coordinator-build', context=context) == {'version': release.APP_VERSION}
            metadata = json.loads((server_data/'public/coordinator-public.json').read_bytes())
            program_data = scratch/'client-program-data'
            data = program_data/'KSAT Client'
            for name in ('identity', 'state', 'packs'):
                (data/name).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ca, data/'coordinator-ca.pem')
            (data/'client-config.json').write_text(json.dumps(dict(
                coordinator_base_url=f'https://localhost:{port}',
                trusted_ca_path=str(data/'coordinator-ca.pem'),
                coordinator_signing_public_key_b64=metadata['signing_public_key_b64'])), encoding='utf-8')
            client_port = release._free_port()
            client = subprocess.Popen([str(layout.client_executable), '--service-console'],
                cwd=root, env=dict(os.environ, ProgramData=str(program_data), KSAT_SMOKE_TEST='1',
                    KSAT_CLIENT_PORT=str(client_port)), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, creationflags=flags)
            state = release._wait_json(f'http://127.0.0.1:{client_port}/api/state', context=None)
            if state.get('state') != 'device_setup' or state.get('problem'):
                raise RuntimeError('Frozen client did not reach device setup.')
            if release._wait_json(f'http://127.0.0.1:{client_port}/api/build', context=None) != {'version': release.CLIENT_VERSION}:
                raise RuntimeError('Frozen client version mismatch.')
            with urllib.request.urlopen(f'http://127.0.0.1:{client_port}/', timeout=10) as page:
                if f'Client version {release.CLIENT_VERSION}' not in page.read().decode('utf-8'):
                    raise RuntimeError('Frozen client version label missing.')
            release._assert_loopback_listener(client, client_port)
        finally:
            outputs = [release._terminate_process(p) for p in (client, server) if p is not None]
        if any('Traceback' in output for output in outputs):
            raise RuntimeError('Frozen smoke reported an exception.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signing-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--innoextract', type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    keys = args.signing_directory.resolve()
    signing = release.lab_signing_config(pfx_path=keys/'KSATLabReleaseSigning.pfx',
        password_environment_name='KSAT_BUILD_PASSWORD', expected_publisher=PUBLISHER,
        environ={'KSAT_BUILD_PASSWORD': (keys/'pfx-password.txt').read_text().strip()})
    if signing.expected_thumbprint != THUMBPRINT:
        raise ValueError('Unexpected release identity.')
    layout = release.ReleaseLayout(root)
    release.inspect_release_inputs(root)
    release.write_version_resources(layout)
    release.write_update_public_key_resource(release.load_release_update_public_key(keys/'update-signing-public.key'),
        layout.build_dir/'update-release-public.json')
    commands = release.build_commands(root, Path(sys.executable))[1:]
    with tempfile.TemporaryDirectory(prefix='ksat-client-release-') as folder:
        scratch = Path(folder)
        def freeze(command):
            name = command[command.index('--name')+1]
            command[command.index('--workpath')+1] = str(scratch/name/'work')
            command[command.index('--specpath')+1] = str(scratch/name/'spec')
            with (output/f'{name}-build.log').open('w', encoding='utf-8') as log:
                subprocess.run(command, cwd=root, check=True, stdout=log, stderr=subprocess.STDOUT)
            return name
        with ThreadPoolExecutor(max_workers=2) as pool:
            for name in pool.map(freeze, commands):
                print('Built '+name, flush=True)
        executables = (layout.client_executable, layout.updater_executable, layout.guard_executable, layout.recovery_executable)
        for executable in executables:
            release.sign_and_verify_artifact(executable, signing)
            release.pyinstaller_payload_manifest(executable)
        smoke_client(root, layout)
        payload = scratch/'lab-payload'
        payload.mkdir()
        sources = {p.name:p for p in executables}
        sources.update({'publisher.cer': root/'release/KSATLabReleaseSigning.cer',
            'update-release-public.json': layout.build_dir/'update-release-public.json',
            'KSATClient.iss':root/'installer/KSATClient.iss'})
        if sources.keys() != REQUIRED_FILES:
            raise ValueError('Client payload allowlist mismatch.')
        for name, source in sources.items():
            shutil.copyfile(source, payload/name)
        manifest = dict(format_version=1, client_version=release.CLIENT_VERSION,
            publisher=PUBLISHER, thumbprint=THUMBPRINT,
            files={name:hashlib.sha256((payload/name).read_bytes()).hexdigest() for name in sorted(sources)})
        (payload/'payload-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        load_approved_payload(payload)
        subprocess.run([str(release.discover_iscc()), f'/DKSAT_PAYLOAD_DIR={payload}',
            f'/DKSAT_OUTPUT_DIR={output}', '/DKSAT_BUNDLE_PUBLISHER_TRUST=1',
            f'/DKSAT_PUBLISHER_THUMBPRINT={THUMBPRINT}', str(root/'installer/KSATClient.iss')], cwd=root, check=True)
        installer = output/f'KSATClientSetup-{release.CLIENT_VERSION}.exe'
        release.sign_and_verify_artifact(installer, signing)
        release._inspect_installer(installer, layout.client_executable, args.innoextract,
            (layout.updater_executable, layout.guard_executable, layout.recovery_executable))
        work = scratch/'builder'
        work.mkdir()
        (work/'builder.version.txt').write_text(builder.builder_version_resource(), encoding='utf-8')
        subprocess.run(builder.builder_command(root, Path(sys.executable), work, payload), cwd=root, check=True)
        built_builder = work/'dist'/f'KSATLabPackageBuilder-{release.CLIENT_VERSION}.exe'
        release.sign_and_verify_artifact(built_builder, signing)
        builder.verify_frozen_builder(built_builder, payload)
        builder_output = output/built_builder.name
        shutil.copyfile(built_builder, builder_output)
        artifacts = [installer, builder_output]
        managed_bundle = 'not published: interactive bootstrap required'
        if tuple(map(int, release.CLIENT_VERSION.split('.'))) > (2, 1, 7):
            bundle = release.build_client_update(
                installer=installer, version=release.CLIENT_VERSION,
                minimum_source_version='2.1.7', publisher=PUBLISHER,
                private_key_file=keys/'update-signing-private.key',
                output=output/f'KSATClientUpdate-{release.CLIENT_VERSION}.ksat-client-update',
                release_notes=f'KSAT Client {release.CLIENT_VERSION}: automatically archive obsolete identity-mismatch submissions and show the installed version',
                authenticode_verifier=lambda path, publisher: release.verify_authenticode_signature(path, signing))
            artifacts.append(bundle)
            managed_bundle = 'verified; minimum installed client 2.1.7'
        release.write_sha256s(artifacts, output/'SHA256SUMS.txt')
        evidence = dict(client_version=release.CLIENT_VERSION, publisher=PUBLISHER, thumbprint=THUMBPRINT,
            installer_payload='verified', builder_payload='verified', frozen_client_startup='passed',
            client_loopback_only=True, tls_verified=True, managed_bundle=managed_bundle,
            physical_lab_pilot='not yet performed')
        (output/'verification.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(evidence), flush=True)


if __name__ == '__main__':
    main()
