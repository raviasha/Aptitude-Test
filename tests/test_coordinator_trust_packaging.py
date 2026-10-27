from dataclasses import replace
from pathlib import Path

import pytest

from scripts import windows_release as release
from scripts.create_lab_signing_identity import create_lab_signing_identity


def test_lab_trust_stages_only_selected_public_certificate(tmp_path):
    identity = tmp_path / 'identity'
    create_lab_signing_identity(identity, years=5)
    signing = release.lab_signing_config(
        pfx_path=identity/'KSATLabReleaseSigning.pfx', password_environment_name='TEST_PASSWORD',
        expected_publisher=release.LAB_SIGNING_PUBLISHER,
        environ={'TEST_PASSWORD': (identity/'pfx-password.txt').read_text().strip()})
    layout = release.ReleaseLayout(tmp_path/'workspace')
    definitions = release.prepare_coordinator_installer_trust(layout, signing)
    assert '/DKSAT_BUNDLE_PUBLISHER_TRUST=1' in definitions
    assert f'/DKSAT_PUBLISHER_THUMBPRINT={signing.expected_thumbprint}' in definitions
    assert (layout.dist_dir/'publisher.cer').read_bytes() == (identity/'KSATLabReleaseSigning.cer').read_bytes()
    assert {p.name for p in layout.dist_dir.iterdir()} == {'publisher.cer'}
    with pytest.raises(release.SigningConfigurationError):
        release.prepare_coordinator_installer_trust(layout, replace(signing, expected_thumbprint='0'*40))


def test_non_lab_build_does_not_stage_or_enable_trust(tmp_path):
    identity = tmp_path/'identity'
    identity.mkdir()
    signing = release.create_ephemeral_test_signing_config(identity, environ={'KSAT_RELEASE_TEST_SIGNING':'1'})
    layout = release.ReleaseLayout(tmp_path/'workspace')
    assert release.prepare_coordinator_installer_trust(layout, signing) == ['/DKSAT_BUNDLE_PUBLISHER_TRUST=0']
    assert not (layout.dist_dir/'publisher.cer').exists()


def test_setup_bootstrap_runs_before_file_replacement_and_preserves_shared_trust():
    source = (Path(__file__).resolve().parents[1]/'installer/KSATCoordinator.iss').read_text()
    prepare = source.split('function PrepareToInstall(', 1)[1].split('function OwnedFirewallMetadata', 1)[0]
    assert 'EnsurePublisherTrust()' in prepare
    uninstall = source.split('procedure CurUninstallStepChanged', 1)[1]
    assert 'Certificate' not in uninstall and 'PublisherTrust' not in uninstall
