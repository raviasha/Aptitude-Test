"""Execute real trust validation; replace only machine certificate-store commands."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/install_coordinator_release_trust.ps1'
POWERSHELL = shutil.which('powershell.exe')
pytestmark = pytest.mark.skipif(os.name != 'nt' or not POWERSHELL, reason='Windows PowerShell required')


def make_certificate(path, *, subject='KSAT LAB RELEASE SIGNING', eku=True, start=-1, end=2):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=start)).not_valid_after(now + timedelta(days=end)))
    if eku:
        usage = ExtendedKeyUsageOID.CODE_SIGNING if eku is True else ExtendedKeyUsageOID.SERVER_AUTH
        cert = cert.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    signed = cert.sign(key, hashes.SHA256())
    path.write_bytes(signed.public_bytes(serialization.Encoding.DER))
    return path


def run_helper(tmp_path, cert=None, *, propagate=False, **changes):
    assert HELPER.is_file(), 'Coordinator publisher trust helper is missing'
    cert = cert or ROOT / 'release/KSATLabReleaseSigning.cer'
    parsed = x509.load_der_x509_certificate(cert.read_bytes())
    fixture = dict(certificate=str(cert), thumbprint=parsed.fingerprint(hashes.SHA1()).hex().upper(),
                   sha256=hashlib.sha256(cert.read_bytes()).hexdigest(), existing=[], fail_read='',
                   fail_import='', missing_after_import='') | changes
    path = tmp_path / 'fixture.json'
    path.write_text(json.dumps(fixture), encoding='utf-8')
    run = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                          '-File', str(ROOT/'tests/fixtures/coordinator_release_trust.ps1'),
                          '-Helper', str(HELPER), '-Fixture', str(path)] + (['-Propagate'] if propagate else []), capture_output=True,
                         text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if propagate:
        return run
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize('existing,want', [([], ['Root','TrustedPublisher']),
    (['Root'], ['TrustedPublisher']), (['TrustedPublisher'], ['Root']), (['Root','TrustedPublisher'], [])])
def test_installs_only_missing_machine_publisher_trust(tmp_path, existing, want):
    result = run_helper(tmp_path, existing=existing)
    assert result['outcome'] == 'ok', result
    assert result['imports'] == want
    assert result['root_count'] == result['publisher_count'] == 1


@pytest.mark.parametrize('changes,diagnostic', [({'thumbprint':'0'*40}, 'thumbprint'), ({'sha256':'0'*64}, 'SHA-256')])
def test_wrong_resource_binding_rejected_before_store_access(tmp_path, changes, diagnostic):
    result = run_helper(tmp_path, **changes)
    assert result['outcome'] == 'error' and diagnostic.lower() in result['message'].lower(), result
    assert result['imports'] == result['reads'] == []


@pytest.mark.parametrize('kwargs,diagnostic', [({'subject':'Wrong Publisher'}, 'publisher'),
    ({'eku':False}, 'code-signing'), ({'eku':'server'}, 'code-signing'),
    ({'start':-3,'end':-1}, 'valid'), ({'start':1,'end':3}, 'valid')])
def test_unapproved_certificate_rejected_before_store_access(tmp_path, kwargs, diagnostic):
    result = run_helper(tmp_path, make_certificate(tmp_path/'test.cer', **kwargs))
    assert result['outcome'] == 'error' and diagnostic in result['message'].lower(), result
    assert result['imports'] == result['reads'] == []


def test_import_failure_preserves_approved_partial_trust_and_retry_completes(tmp_path):
    first = run_helper(tmp_path, fail_import='TrustedPublisher')
    assert first['outcome'] == 'error' and 'denied' in first['message'], first
    assert first['root_count'] == 1 and first['publisher_count'] == 0
    retry = run_helper(tmp_path, existing=['Root'])
    assert retry['outcome'] == 'ok' and retry['imports'] == ['TrustedPublisher'], retry


def test_import_must_be_verified_not_merely_report_success(tmp_path):
    result = run_helper(tmp_path, missing_after_import='Root')
    assert result['outcome'] == 'error' and 'verify' in result['message'].lower(), result
    assert result['imports'] == ['Root']


def test_store_access_denied_is_not_treated_as_missing_certificate(tmp_path):
    result = run_helper(tmp_path, fail_read='Root')
    assert result['outcome'] == 'error' and 'denied' in result['message'], result
    assert result['imports'] == []


@pytest.mark.parametrize('changes,diagnostic', [({'sha256':'0'*64}, 'SHA-256'),
    ({'fail_import':'Root'}, 'denied')])
def test_rejection_propagates_to_nonzero_powershell_process_exit(tmp_path, changes, diagnostic):
    run = run_helper(tmp_path, propagate=True, **changes)
    assert run.returncode != 0
    assert diagnostic in run.stderr
