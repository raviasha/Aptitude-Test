"""Run the real compiled installer logic, substituting only OS boundaries.

Changing legacy acceptance, live verification, rename/rollback, or uninstall
targets breaks these behavioral tests. No machine firewall is accessed.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import psutil

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = r"C:\Program Files\KSAT Coordinator\KSATCoordinator.exe"
LEGACY = "KSAT Faculty Coordinator 2.0.0"
CURRENT = "KSAT Faculty Coordinator 2.1.0"
RECORD = r'{"program":"C:\\Program Files\\KSAT Coordinator\\KSATCoordinator.exe","rule_name":"KSAT Faculty Coordinator 2.0.0","port":8443,"direction":"inbound","action":"allow","protocol":"tcp","profile":"private","enabled":true}'


def live_rule(name=LEGACY, port="8443", **changes):
    result = dict(DisplayName=name, Program=PROGRAM, LocalPort=port,
                  Direction="Inbound", Action="Allow", Protocol="TCP",
                  Profile="Private", Enabled="True")
    return result | changes


HARNESS = r'''
[Setup]
AppName=KSAT Firewall Test
AppVersion=1
DefaultDirName={tmp}\KSAT-Firewall-Test
PrivilegesRequired=lowest
Uninstallable=no
CreateAppDir=no
OutputBaseFilename=firewall-test
OutputDir=.
[Code]
const
  FirewallRuleStateAbsent = 0;
  FirewallRuleStateCompatible = 1;
  FirewallRuleStateConflict = 2;
function NativeMoveFileEx(Old, New: String; Flags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';
function TestMoveFileEx(Old, New: String; Flags: Cardinal): Boolean;
begin
  if FileExists(ExpandConstant('{param:ROOT}\fail-save')) then Result := False
  else Result := NativeMoveFileEx(Old, New, Flags);
end;
function TestExpandConstant(Value: String): String;
begin
  StringChangeEx(Value, '{app}', 'C:\Program Files\KSAT Coordinator', True);
  StringChangeEx(Value, '{commonappdata}', ExpandConstant('{param:ROOT}'), True);
  Result := ExpandConstant(Value);
end;
function TestExec(Filename, Parameters, WorkingDir: String; ShowCmd: Integer;
  Wait: TExecWait; var Code: Integer): Boolean;
var Kind, Args, RootDir: String;
begin
  RootDir := ExpandConstant('{param:ROOT}');
  if ExtractFileName(Filename) = 'netsh.exe' then Kind := 'netsh'
  else if ExtractFileName(Filename) = 'powershell.exe' then Kind := 'powershell'
  else RaiseException('Unexpected external tool');
  if not SaveStringToFile(RootDir + '\command.txt', Parameters, False) then
    RaiseException('Cannot save test command');
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{src}') + '\coordinator_firewall.ps1" -Root "' + RootDir + '" -Kind ' + Kind;
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Args, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;
__PRODUCTION__
function InitializeSetup(): Boolean;
var Outcome: String;
begin
  try
    if ExpandConstant('{param:MODE|configure}') = 'delete' then DeleteOwnedFirewall(False)
    else ConfigureOwnedFirewall(ExpandConstant('{param:PORT|8443}'));
    Outcome := 'ok';
  except
    Outcome := GetExceptionMessage;
  end;
  SaveStringToFile(ExpandConstant('{param:ROOT}\outcome.txt'), Outcome, False);
  Result := False;
end;
'''


@unittest.skipUnless(os.name == "nt" and os.environ.get("KSAT_ISCC"), "Inno compiler required")
class CoordinatorFirewallUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ksat-firewall-harness-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.build = Path(cls.temp.name)
        source = (ROOT / "installer/KSATCoordinator.iss").read_text("utf-8")
        # Select unchanged production function bodies, excluding unrelated UI.
        functions = source[source.index("function FirewallOwnerPath"):source.index("function JsonString")]
        functions += source[source.index("function OwnedFirewallMetadata"):source.index("procedure CurStepChanged")]
        functions = functions.replace("ExpandConstant(", "TestExpandConstant(")
        functions = functions.replace("Exec(", "TestExec(").replace("MoveFileEx(", "TestMoveFileEx(")
        defines = source[:source.index("[Setup]")]
        script = cls.build / "harness.iss"
        script.write_text(defines + HARNESS.replace("__PRODUCTION__", functions), encoding="utf-8")
        (cls.build / "coordinator_firewall.ps1").write_bytes((ROOT / "tests/fixtures/coordinator_firewall.ps1").read_bytes())
        run = subprocess.run([os.environ["KSAT_ISCC"], str(script)], capture_output=True, text=True, timeout=60)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)

    def run_case(self, record=RECORD, rules=None, *, port="8443", mode="configure", fail_save=False, fail_at=()):
        with tempfile.TemporaryDirectory(prefix="ksat-firewall-state-") as directory:
            root = Path(directory)
            marker = root / "KSAT Coordinator/firewall-owner.json"
            marker.parent.mkdir()
            if record is not None:
                marker.write_text(record, encoding="ascii")
            if fail_save:
                (root / "fail-save").touch()
            state = dict(rules=[live_rule()] if rules is None else rules,
                         mutations=0, fail_at=list(fail_at), commands=[])
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
            process = subprocess.Popen(
                [str(self.build / "firewall-test.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                 f"/ROOT={root}", f"/MODE={mode}", f"/PORT={port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                process.wait(timeout=90)
            finally:
                # An Inno launcher has a child process. On timeout clean up the
                # exact owned tree before TemporaryDirectory removes its files.
                if process.poll() is None:
                    try:
                        parent = psutil.Process(process.pid)
                        owned = parent.children(recursive=True) + [parent]
                    except psutil.NoSuchProcess:
                        owned = []
                    for child in reversed(owned):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    psutil.wait_procs(owned, timeout=5)
                    process.wait(timeout=5)
            outcome = (root / "outcome.txt").read_text("utf-8-sig")
            state = json.loads((root / "state.json").read_text("utf-8-sig"))
            saved = marker.read_text("utf-8-sig") if marker.exists() else None
            return outcome, state, saved

    def test_user_legacy_record_migrates_and_preserves_security_fields(self):
        outcome, state, saved = self.run_case()
        self.assertEqual("ok", outcome)
        self.assertEqual([live_rule(CURRENT)], state["rules"])
        self.assertEqual(RECORD.replace(LEGACY, CURRENT), saved)

    def test_current_reinstall_and_clean_install(self):
        for record, rules in ((RECORD.replace(LEGACY, CURRENT), [live_rule(CURRENT)]), (None, []), (None, [live_rule(CURRENT)])):
            with self.subTest(record=record, rules=rules):
                outcome, state, saved = self.run_case(record, rules)
                self.assertEqual("ok", outcome)
                self.assertEqual([live_rule(CURRENT)], state["rules"])
                self.assertEqual(RECORD.replace(LEGACY, CURRENT), saved)

    def test_legacy_collision_is_not_modified(self):
        rules = [live_rule(), live_rule(CURRENT)]
        outcome, state, saved = self.run_case(rules=rules)
        self.assertNotEqual("ok", outcome)
        self.assertEqual(0, state["mutations"])
        self.assertEqual(rules, state["rules"])
        self.assertEqual(RECORD, saved)

    def test_current_record_with_leftover_legacy_rule_is_not_modified(self):
        rules = [live_rule(port="9443"), live_rule(CURRENT)]
        record = RECORD.replace(LEGACY, CURRENT)
        outcome, state, saved = self.run_case(record, rules)
        self.assertNotEqual("ok", outcome)
        self.assertEqual(0, state["mutations"])
        self.assertEqual(rules, state["rules"])
        self.assertEqual(record, saved)

    def test_invalid_metadata_is_rejected_before_any_external_command(self):
        for bad in (RECORD.replace(LEGACY, "Unrelated"), RECORD.replace("KSATCoordinator.exe", "other.exe"),
                    RECORD.replace('"private"', '"public"'), RECORD.replace('"enabled":true', '"enabled":false'),
                    RECORD + "\n", RECORD.replace("8443", "0"), RECORD.replace("8443", "65536"),
                    RECORD.replace("8443", "99999999999999999999")):
            with self.subTest(record=bad):
                outcome, state, saved = self.run_case(bad)
                self.assertIn("ownership record is invalid", outcome)
                self.assertEqual([], state["commands"])
                self.assertEqual(bad, saved)

    def test_mismatched_live_rule_is_rejected_without_mutation(self):
        for rules in ([], [live_rule(), live_rule()], [live_rule(Program="Any")],
                      [live_rule(LocalPort="443")], [live_rule(Profile="Public")],
                      [live_rule(Direction="Outbound")], [live_rule(Action="Block")],
                      [live_rule(Protocol="UDP")], [live_rule(Enabled="False")]):
            with self.subTest(rules=rules):
                outcome, state, saved = self.run_case(rules=rules)
                self.assertIn("live firewall rule", outcome)
                self.assertEqual(0, state["mutations"])
                self.assertEqual(rules, state["rules"])
                self.assertEqual(RECORD, saved)

    def test_custom_and_boundary_ports_migrate(self):
        for port in ("1", "9443", "65535"):
            with self.subTest(port=port):
                record = RECORD.replace("8443", port)
                outcome, state, saved = self.run_case(record, [live_rule(port=port)], port=port)
                self.assertEqual("ok", outcome)
                self.assertEqual([live_rule(CURRENT, port)], state["rules"])
                self.assertEqual(record.replace(LEGACY, CURRENT), saved)

    def test_failed_save_rolls_back_name_and_changed_port(self):
        for name in (LEGACY, CURRENT):
            with self.subTest(name=name):
                record = RECORD.replace(LEGACY, name)
                outcome, state, saved = self.run_case(record, [live_rule(name)], port="9443", fail_save=True)
                self.assertIn("ownership record could not be saved", outcome)
                self.assertEqual([live_rule(name)], state["rules"])
                self.assertEqual(record, saved)

    def test_failed_initial_mutation_leaves_old_state(self):
        outcome, state, saved = self.run_case(fail_at=(1,))
        self.assertIn("could not be updated", outcome)
        self.assertEqual([live_rule()], state["rules"])
        self.assertEqual(RECORD, saved)

    def test_failed_rollback_reports_recovery_failure(self):
        outcome, state, saved = self.run_case(fail_save=True, fail_at=(2,))
        self.assertIn("rollback both failed", outcome)
        self.assertEqual([live_rule(CURRENT)], state["rules"])
        self.assertEqual(RECORD, saved)

    def test_clean_install_save_failure_removes_only_new_rule(self):
        unrelated = live_rule("Other application")
        outcome, state, saved = self.run_case(None, [unrelated], fail_save=True)
        self.assertIn("ownership record could not be saved", outcome)
        self.assertEqual([unrelated], state["rules"])
        self.assertIsNone(saved)

    def test_uninstall_removes_only_verified_recorded_rule(self):
        for name in (LEGACY, CURRENT):
            with self.subTest(name=name):
                unrelated = live_rule("Other application")
                outcome, state, saved = self.run_case(RECORD.replace(LEGACY, name), [live_rule(name), unrelated], mode="delete")
                self.assertEqual("ok", outcome)
                self.assertEqual([unrelated], state["rules"])
                self.assertIsNone(saved)

    def test_uninstall_refuses_changed_live_rule(self):
        rules = [live_rule(Program="Any")]
        outcome, state, saved = self.run_case(rules=rules, mode="delete")
        self.assertIn("live firewall rule", outcome)
        self.assertEqual(0, state["mutations"])
        self.assertEqual(rules, state["rules"])
        self.assertEqual(RECORD, saved)
