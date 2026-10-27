import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from scripts import windows_release as release
from scripts.create_lab_signing_identity import create_lab_signing_identity


def test_generic_compile_stages_only_matching_public_publisher_before_compiler():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        layout = release.ReleaseLayout(root)
        layout.dist_dir.mkdir()
        config = release.create_ephemeral_test_signing_config(root / "identity",
            environ={"KSAT_RELEASE_TEST_SIGNING": "1"})

        def compiler(args, **kwargs):
            certificate = x509.load_der_x509_certificate((layout.dist_dir / "publisher.cer").read_bytes())
            assert certificate.fingerprint(hashes.SHA1()).hex().upper() == config.expected_thumbprint
            assert {p.name for p in layout.dist_dir.iterdir()} == {"publisher.cer"}
            if args[-1].endswith("KSATClient.iss"):
                assert "/DKSAT_BUNDLE_PUBLISHER_TRUST=0" in args
                assert "/DKSAT_PUBLISHER_THUMBPRINT=" + config.expected_thumbprint in args
            target = layout.client_installer if args[-1].endswith("KSATClient.iss") else layout.coordinator_installer
            target.write_bytes(b"MZ fixture")

        with patch.object(release.subprocess, "run", side_effect=compiler), \
             patch.object(release, "sign_and_verify_artifact"):
            release.compile_installers(root, Path("ISCC.exe"), config)
        assert layout.client_installer.read_bytes() == b"MZ fixture"


@pytest.mark.parametrize('lab', [False, True])
def test_generic_inspection_cannot_omit_guard_signature_or_inner_bytes(lab):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        layout = release.ReleaseLayout(root)
        layout.dist_dir.mkdir()
        layout.release_dir.mkdir()
        paths = [layout.coordinator_executable, layout.client_executable, layout.updater_executable,
            layout.coordinator_release_executable, layout.client_release_executable,
            layout.coordinator_installer, layout.client_installer, layout.dist_dir / "KSATClientInstallGuard.exe"]
        for path in paths: path.write_bytes(b"MZfixture")
        extractor = root / "innoextract.exe"
        extractor.write_bytes(b"tool")
        config = release.create_ephemeral_test_signing_config(root / "identity",
            environ={"KSAT_RELEASE_TEST_SIGNING": "1"})
        if lab:
            identity = root / 'lab-identity'
            create_lab_signing_identity(identity, years=5)
            config = release.lab_signing_config(pfx_path=identity/'KSATLabReleaseSigning.pfx',
                password_environment_name='TEST_PASSWORD', expected_publisher=release.LAB_SIGNING_PUBLISHER,
                environ={'TEST_PASSWORD':(identity/'pfx-password.txt').read_text().strip()})
        signatures, archives, extracted = [], [], []

        def archive(path):
            archives.append(path.name)
            return {"C:b:update-release-public.json": "digest"}

        def inspect(installer, executable, tool, additional_executables=(), additional_resources=()):
            if installer == layout.coordinator_installer:
                assert additional_resources == ((layout.dist_dir/'publisher.cer',
                    root/'scripts/install_coordinator_release_trust.ps1') if lab else ())
            if installer == layout.client_installer:
                extracted.extend(p.name for p in additional_executables)

        with patch.object(release, "verify_authenticode_signature", side_effect=lambda p, c: signatures.append(p.name)), \
             patch.object(release, "pyinstaller_payload_manifest", side_effect=archive), \
             patch.object(release, "_inspect_installer", side_effect=inspect):
            release.inspect_artifacts(root, Path("python.exe"), extractor, config)
        assert "KSATClientInstallGuard.exe" in signatures
        assert "KSATClientInstallGuard.exe" in archives
        assert "KSATClientInstallGuard.exe" in extracted
