"""Run real hashes/setup preparation; mock process launch and silent UI state. No trust writes."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not os.environ.get('KSAT_ISCC'), reason='Inno compiler required')

HARNESS = r'''
[Setup]
AppName=KSAT Trust Test
AppVersion=1
DefaultDirName={tmp}\KSAT-Trust-Test
PrivilegesRequired=lowest
Uninstallable=no
CreateAppDir=no
OutputBaseFilename=trust-test
OutputDir=.
[Files]
Source: "publisher.cer"; Flags: dontcopy
Source: "install_coordinator_release_trust.ps1"; Flags: dontcopy
[Code]
var HasExistingConfig: Boolean; HostPage, PortPage: TInputQueryWizardPage;
function TryParsePort(Value: String; var PortNumber: Integer): Boolean;
begin Result := True; end;
procedure TestExtract(Name: String);
begin
  ExtractTemporaryFile(Name);
  if ((ExpandConstant('{param:MODE}') = 'tamper') and (Name = 'publisher.cer')) or
     ((ExpandConstant('{param:MODE}') = 'tamper-helper') and (Name = 'install_coordinator_release_trust.ps1')) then
    SaveStringToFile(ExpandConstant('{tmp}\') + Name, 'tampered', True);
end;
function TestExec(Filename, Parameters, WorkingDir: String; ShowCmd: Integer;
  Wait: TExecWait; var Code: Integer): Boolean;
begin
  SaveStringToFile(ExpandConstant('{param:ROOT}\command.txt'), Filename + #13#10 + Parameters, False);
  Code := 0;
  if ExpandConstant('{param:MODE}') = 'failure' then Code := 1;
  Result := ExpandConstant('{param:MODE}') <> 'launch-failure';
end;
__PRODUCTION__
function InitializeSetup(): Boolean;
var Outcome: String; Restart: Boolean;
begin
  HasExistingConfig := ExpandConstant('{param:MODE}') = 'existing';
  Outcome := PrepareToInstall(Restart);
  SaveStringToFile(ExpandConstant('{param:ROOT}\outcome.txt'), Outcome, False);
  Result := False;
end;
'''


@pytest.fixture(scope='module', params=[True, False])
def compiled(request, tmp_path_factory):
    folder = tmp_path_factory.mktemp('coordinator-trust-setup')
    cert = ROOT/'release/KSATLabReleaseSigning.cer'
    helper = ROOT/'scripts/install_coordinator_release_trust.ps1'
    (folder/'publisher.cer').write_bytes(cert.read_bytes())
    (folder/helper.name).write_bytes(helper.read_bytes())
    source = (ROOT/'installer/KSATCoordinator.iss').read_text()
    functions = source[source.index('function EnsurePublisherTrust'):source.index('function OwnedFirewallMetadata')]
    functions = functions.replace('Exec(', 'TestExec(').replace('ExtractTemporaryFile(', 'TestExtract(').replace('WizardSilent()', 'False')
    defines = f'#define KSAT_BUNDLE_PUBLISHER_TRUST "{int(request.param)}"\n'
    defines += '#define KSAT_PUBLISHER_THUMBPRINT "13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908"\n'
    defines += f'#define PublisherHash "{hashlib.sha256(cert.read_bytes()).hexdigest()}"\n'
    defines += f'#define TrustHelperHash "{hashlib.sha256(helper.read_bytes()).hexdigest()}"\n'
    script = folder/'harness.iss'
    script.write_text(defines + HARNESS.replace('__PRODUCTION__', functions))
    run = subprocess.run([os.environ['KSAT_ISCC'], str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    return folder/'trust-test.exe', request.param


@pytest.mark.parametrize('mode', ['fresh', 'existing', 'failure', 'launch-failure', 'tamper', 'tamper-helper'])
def test_setup_preparation(compiled, tmp_path, mode):
    exe, enabled = compiled
    subprocess.run([str(exe), '/VERYSILENT', '/SUPPRESSMSGBOXES', f'/ROOT={tmp_path}', f'/MODE={mode}'], timeout=30)
    outcome = (tmp_path/'outcome.txt').read_text('utf-8-sig')
    command = tmp_path/'command.txt'
    if not enabled:
        assert outcome == '' and not command.exists()
    elif mode.startswith('tamper'):
        assert 'verification' in outcome and not command.exists()
    else:
        assert command.exists()
        assert '-ExpectedSha256' in command.read_text('utf-8-sig')
        assert (outcome == '') == (mode in ('fresh', 'existing'))
