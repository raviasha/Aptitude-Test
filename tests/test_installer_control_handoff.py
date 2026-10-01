"""Run the shipped Pascal/PowerShell handoff, without installing KSAT."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ISCC = shutil.which("ISCC.exe") or str(
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe"
)


@pytest.mark.skipif(os.name != "nt" or not Path(ISCC).is_file(),
                    reason="Requires Windows and Inno Setup")
@pytest.mark.parametrize("terminal", ["commit", "abort"])
def test_installer_can_replace_configure_with_terminal_command(tmp_path, terminal):
    # A null/empty backup-path marshaling bug in GuardCommand must fail this test.
    # Compile the actual shipped procedures, not a Python imitation of them.
    source = (ROOT / "installer/KSATClient.iss").read_text()
    procedures = []
    for name in ("PSQuote", "PowerShell", "GuardCommand"):
        match = re.search(r"^(?:function|procedure) " + name + r"\(.*?^end;",
                          source, re.MULTILINE | re.DOTALL)
        assert match is not None, name
        procedures.append(match.group())
    stage = tmp_path / "handoff with spaces"
    stage.mkdir()
    script = tmp_path / "probe.iss"
    script.write_text("""[Setup]
AppName=KSAT Handoff Regression Probe
AppVersion=1
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputBaseFilename=handoff-probe
OutputDir=.
[Code]
var Stage: String;
""" + "\n".join(procedures) + """
procedure InitializeWizard();
begin
  Stage := ExpandConstant('{param:PROBE}');
  try
    GuardCommand('configure');
    GuardCommand('""" + terminal + """');
    SaveStringToFile(Stage + '\\completed.txt', 'complete', False);
  except
    SaveStringToFile(Stage + '\\completed.txt', GetExceptionMessage(), False);
  end;
end;
""", encoding="utf-8")
    compile_result = subprocess.run([ISCC, str(script)], capture_output=True, text=True, timeout=60)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    result = subprocess.run([str(tmp_path / "handoff-probe.exe"), "/VERYSILENT",
        "/SUPPRESSMSGBOXES", "/NORESTART", f"/PROBE={stage}", f"/LOG={tmp_path / 'probe.log'}"],
        capture_output=True, timeout=60)
    log = (tmp_path / "probe.log").read_text(errors="replace")
    assert result.returncode == 0, log
    assert (stage / "completed.txt").read_text() == "complete"
    assert json.loads((stage / "control.json").read_text()) == {"action": terminal}
    assert not (stage / "control-next.json").exists()
