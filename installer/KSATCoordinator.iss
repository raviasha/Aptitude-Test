#define AppName "KSAT Faculty Coordinator"
#define AppVersion "2.0.0"
#define AppPublisher "College Assessment Lab"
#define AppExeName "KSATCoordinator.exe"
#define FirewallRule "KSAT Faculty Coordinator 2.0.0"

[Setup]
AppId={{6A30525E-E010-4B63-B5E9-1AEFDF47A6DC}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
UninstallDisplayName={#AppName} {#AppVersion}
DefaultDirName={autopf}\KSAT Coordinator
DefaultGroupName=KSAT
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=KSATCoordinatorSetup-2.0.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartIfNeededByRun=no
SetupLogging=yes

[Files]
Source: "..\dist\KSATCoordinator.exe"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\KSAT Coordinator"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\secrets"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\backups"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\Assessment Releases"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\Question Assets"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\Question Banks"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Coordinator\public"; Permissions: admins-full system-full users-readexec

[Icons]
Name: "{group}\KSAT Faculty Coordinator"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\KSAT Faculty Coordinator"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch KSAT Faculty Coordinator"; Flags: nowait postinstall skipifsilent

[Code]
const
  FirewallRuleStateAbsent = 0;
  FirewallRuleStateCompatible = 1;
  FirewallRuleStateConflict = 2;

var
  HostPage: TInputQueryWizardPage;
  PortPage: TInputQueryWizardPage;
  ExistingHost: String;
  ExistingPort: String;
  HasExistingConfig: Boolean;

function MoveFileEx(lpExistingFileName, lpNewFileName: String;
  dwFlags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';

function ConfigPath(): String;
begin Result := ExpandConstant('{commonappdata}\KSAT Coordinator\coordinator-runtime.json'); end;
function FirewallOwnerPath(): String;
begin Result := ExpandConstant('{commonappdata}\KSAT Coordinator\firewall-owner.json'); end;

function JsonEscape(Value: String): String;
begin
  Result := Value;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

function IsDigits(Value: String): Boolean;
var I: Integer;
begin
  Result := Length(Value) > 0;
  for I := 1 to Length(Value) do
    if (Value[I] < '0') or (Value[I] > '9') then Result := False;
end;

function TryParsePort(Value: String; var PortNumber: Integer): Boolean;
begin
  Result := False;
  if not IsDigits(Value) then exit;
  try PortNumber := StrToInt(Value); except exit; end;
  Result := (PortNumber >= 1) and (PortNumber <= 65535);
end;

function JsonString(Raw, Key: String; var Value: String): Boolean;
var StartAt, EndAt: Integer; Prefix: String;
begin
  Result := False; Prefix := '"' + Key + '":"'; StartAt := Pos(Prefix, Raw);
  if StartAt = 0 then exit;
  StartAt := StartAt + Length(Prefix); EndAt := StartAt;
  while (EndAt <= Length(Raw)) and (Raw[EndAt] <> '"') do EndAt := EndAt + 1;
  if EndAt > Length(Raw) then exit;
  Value := Copy(Raw, StartAt, EndAt - StartAt);
  Result := (Value <> '') and (Pos('\', Value) = 0);
end;

function JsonPort(Raw: String; var Value: String): Boolean;
var StartAt, EndAt, PortNumber: Integer; Prefix: String;
begin
  Result := False; Prefix := '"port":'; StartAt := Pos(Prefix, Raw);
  if StartAt = 0 then exit;
  StartAt := StartAt + Length(Prefix); EndAt := StartAt;
  while (EndAt <= Length(Raw)) and (Raw[EndAt] >= '0') and (Raw[EndAt] <= '9') do
    EndAt := EndAt + 1;
  Value := Copy(Raw, StartAt, EndAt - StartAt);
  Result := TryParsePort(Value, PortNumber);
end;

procedure LoadExistingRuntime();
var Raw: AnsiString; Text: String;
begin
  HasExistingConfig := FileExists(ConfigPath());
  if not HasExistingConfig then exit;
  if not LoadStringFromFile(ConfigPath(), Raw) then
    RaiseException('The existing coordinator configuration could not be read.');
  Text := String(Raw);
  if (not JsonString(Text, 'hostname', ExistingHost)) or
     (not JsonPort(Text, ExistingPort)) then
    RaiseException('The existing coordinator configuration is invalid.');
end;

procedure InitializeWizard();
var HostValue, PortValue: String;
begin
  LoadExistingRuntime();
  HostValue := ExpandConstant('{param:HOSTNAME|}'); PortValue := ExpandConstant('{param:PORT|8443}');
  if HasExistingConfig then begin HostValue := ExistingHost; PortValue := ExistingPort; end;
  if HostValue = '' then HostValue := Lowercase(GetComputerNameString()) + '.local';
  HostPage := CreateInputQueryPage(wpSelectDir, 'Coordinator network name',
    'Enter the DNS name used by lab clients.', 'Upgrades preserve the installed hostname and port.');
  HostPage.Add('HTTPS hostname:', False); HostPage.Values[0] := HostValue;
  PortPage := CreateInputQueryPage(HostPage.ID, 'Coordinator HTTPS port',
    'Choose the private-network HTTPS port.', 'The default port is 8443.');
  PortPage.Add('TCP port:', False); PortPage.Values[0] := PortValue;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin Result := HasExistingConfig and ((PageID = HostPage.ID) or (PageID = PortPage.ID)); end;

function NextButtonClick(CurPageID: Integer): Boolean;
var PortNumber: Integer;
begin
  Result := True;
  if (CurPageID = HostPage.ID) and
     ((Trim(HostPage.Values[0]) = '') or (Pos(' ', HostPage.Values[0]) > 0) or
      (Pos('/', HostPage.Values[0]) > 0) or (Pos('\', HostPage.Values[0]) > 0)) then
  begin MsgBox('Enter a valid DNS hostname.', mbError, MB_OK); Result := False; end;
  if (CurPageID = PortPage.ID) and (not TryParsePort(PortPage.Values[0], PortNumber)) then
  begin MsgBox('Enter a TCP port from 1 through 65535.', mbError, MB_OK); Result := False; end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var PortNumber: Integer;
begin
  Result := '';
  if WizardSilent() and (not HasExistingConfig) and
     ((Trim(HostPage.Values[0]) = '') or (not TryParsePort(PortPage.Values[0], PortNumber))) then
    Result := 'A first silent install requires /HOSTNAME=<dns-name> and optional /PORT=<1-65535>.';
end;

function OwnedFirewallMetadata(PortValue: String): String;
begin
  Result := '{"program":"' + JsonEscape(ExpandConstant('{app}\{#AppExeName}')) +
    '","rule_name":"{#FirewallRule}","port":' + PortValue +
    ',"direction":"inbound","action":"allow","protocol":"tcp",' +
    '"profile":"private","enabled":true}';
end;

function OwnerPort(Raw: String; var StoredPort: String): Boolean;
var ExpectedPrefix, ExpectedSuffix: String; Dummy: Integer;
begin
  ExpectedPrefix := '{"program":"' + JsonEscape(ExpandConstant('{app}\{#AppExeName}')) +
    '","rule_name":"{#FirewallRule}","port":';
  ExpectedSuffix := ',"direction":"inbound","action":"allow",' +
    '"protocol":"tcp","profile":"private","enabled":true}';
  Result := (Pos(ExpectedPrefix, Raw) = 1) and
    (Copy(Raw, Length(Raw) - Length(ExpectedSuffix) + 1,
      Length(ExpectedSuffix)) = ExpectedSuffix);
  if not Result then exit;
  StoredPort := Copy(Raw, Length(ExpectedPrefix) + 1,
    Length(Raw) - Length(ExpectedPrefix) - Length(ExpectedSuffix));
  Result := TryParsePort(StoredPort, Dummy);
end;

procedure VerifyOwnedFirewall(StoredPort: String);
var Script, Parameters: String; ResultCode: Integer;
begin
  Script := '$ErrorActionPreference=''Stop'';' +
    '$name=''{#FirewallRule}'';$program=''' +
    ExpandConstant('{app}\{#AppExeName}') + ''';$port=''' + StoredPort + ''';' +
    '$rules=@(Get-NetFirewallRule -DisplayName $name -ErrorAction Stop);' +
    'if($rules.Count-ne 1){exit 41};$rule=$rules[0];' +
    '$apps=@($rule|Get-NetFirewallApplicationFilter -ErrorAction Stop);' +
    '$ports=@($rule|Get-NetFirewallPortFilter -ErrorAction Stop);' +
    'if(($apps.Count-ne 1)-or($ports.Count-ne 1)){exit 42};' +
    'if(($apps[0].Program-eq ''Any'')-or(-not [StringComparer]::OrdinalIgnoreCase.Equals(' +
    '[IO.Path]::GetFullPath($apps[0].Program),[IO.Path]::GetFullPath($program)))){exit 43};' +
    'if(($rule.DisplayName-cne $name)-or($rule.Direction.ToString()-cne ''Inbound'')-or' +
    '($rule.Action.ToString()-cne ''Allow'')-or($rule.Profile.ToString()-cne ''Private'')-or' +
    '($rule.Enabled.ToString()-cne ''True'')-or($ports[0].Protocol.ToString()-cne ''TCP'')-or' +
    '(@($ports[0].LocalPort).Count-ne 1)-or($ports[0].LocalPort.ToString()-cne $port)){exit 44}';
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The live firewall rule no longer matches the installer ownership record.');
end;

function ExistingFirewallRuleState(PortValue: String): Integer;
var Script, Parameters: String; ResultCode: Integer;
begin
  Result := FirewallRuleStateConflict;
  Script := '$ErrorActionPreference=''Stop'';try{' +
    '$name=''{#FirewallRule}'';$program=''' +
    ExpandConstant('{app}\{#AppExeName}') + ''';$port=''' + PortValue + ''';' +
    '$rules=@(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue);' +
    'if($rules.Count -eq 0){exit 0};if($rules.Count -ne 1){exit 2};' +
    '$rule=$rules[0];$apps=@($rule|Get-NetFirewallApplicationFilter -ErrorAction Stop);' +
    '$ports=@($rule|Get-NetFirewallPortFilter -ErrorAction Stop);' +
    'if(($apps.Count-ne 1)-or($ports.Count-ne 1)){exit 2};' +
    'if(($apps[0].Program-eq ''Any'')-or(-not [StringComparer]::OrdinalIgnoreCase.Equals(' +
    '[IO.Path]::GetFullPath($apps[0].Program),[IO.Path]::GetFullPath($program)))){exit 2};' +
    'if(($rule.DisplayName-cne $name)-or($rule.Direction.ToString()-cne ''Inbound'')-or' +
    '($rule.Action.ToString()-cne ''Allow'')-or($rule.Profile.ToString()-cne ''Private'')-or' +
    '($rule.Enabled.ToString()-cne ''True'')-or($ports[0].Protocol.ToString()-cne ''TCP'')-or' +
    '(@($ports[0].LocalPort).Count-ne 1)-or($ports[0].LocalPort.ToString()-cne $port)){exit 2};' +
    'exit 1}catch{exit 3}';
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then exit;
  if ResultCode = 0 then Result := FirewallRuleStateAbsent
  else if ResultCode = 1 then Result := FirewallRuleStateCompatible;
end;

procedure VerifyFirewallAbsent();
var Script, Parameters: String; ResultCode: Integer;
begin
  Script := '$r=@(Get-NetFirewallRule -DisplayName ''{#FirewallRule}'' ' +
    '-ErrorAction SilentlyContinue);if($r.Count-ne 0){exit 45}';
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('A firewall rule with the KSAT name exists but is not installer-owned.');
end;

procedure SaveFirewallMetadataAtomically(Value: String);
var Temporary: String;
begin
  Temporary := FirewallOwnerPath() + '.tmp';
  DeleteFile(Temporary);
  if (not SaveStringToFile(Temporary, Value, False)) or
     (not MoveFileEx(Temporary, FirewallOwnerPath(), 9)) then
  begin
    DeleteFile(Temporary);
    RaiseException('The firewall ownership record could not be saved.');
  end;
end;

procedure DeleteOwnedFirewall(ForUpgrade: Boolean);
var Raw: AnsiString; StoredPort, Parameters: String; ResultCode: Integer;
begin
  if not FileExists(FirewallOwnerPath()) then exit;
  if not LoadStringFromFile(FirewallOwnerPath(), Raw) then
    RaiseException('The firewall ownership record could not be read.');
  if not OwnerPort(String(Raw), StoredPort) then
    RaiseException('The firewall ownership record is invalid.');
  VerifyOwnedFirewall(StoredPort);
  Parameters := 'advfirewall firewall delete rule name="{#FirewallRule}" program="' +
    ExpandConstant('{app}\{#AppExeName}') + '"';
  if (not Exec(ExpandConstant('{sys}\netsh.exe'), Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The installer-owned coordinator firewall rule could not be removed.');
  VerifyFirewallAbsent();
  if not ForUpgrade then DeleteFile(FirewallOwnerPath());
end;

procedure ConfigureOwnedFirewall(PortValue: String);
var Parameters, PreviousMetadata, PreviousPort: String; Raw: AnsiString;
    ResultCode, RestoreCode, FirewallState: Integer; HadMarker: Boolean;
begin
  HadMarker := FileExists(FirewallOwnerPath()); PreviousMetadata := '';
  if HadMarker then begin
    if not LoadStringFromFile(FirewallOwnerPath(), Raw) then
      RaiseException('The firewall ownership record could not be read.');
    PreviousMetadata := String(Raw);
    if not OwnerPort(PreviousMetadata, PreviousPort) then
      RaiseException('The firewall ownership record is invalid.');
    VerifyOwnedFirewall(PreviousPort);
    Parameters := 'advfirewall firewall set rule name="{#FirewallRule}" program="' +
      ExpandConstant('{app}\{#AppExeName}') + '" new localport=' + PortValue +
      ' profile=private enable=yes';
    if (not Exec(ExpandConstant('{sys}\netsh.exe'), Parameters, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      RaiseException('The installer-owned coordinator firewall rule could not be updated.');
    VerifyOwnedFirewall(PortValue);
  end else begin
    FirewallState := ExistingFirewallRuleState(PortValue);
    if FirewallState = FirewallRuleStateConflict then
      RaiseException('A firewall rule with the KSAT name exists but is not compatible with this Coordinator.');
    if FirewallState = FirewallRuleStateCompatible then begin
      VerifyOwnedFirewall(PortValue);
    end else begin
    Parameters := 'advfirewall firewall add rule name="{#FirewallRule}" dir=in action=allow ' +
      'protocol=TCP localport=' + PortValue + ' profile=private program="' +
      ExpandConstant('{app}\{#AppExeName}') + '" enable=yes';
    if (not Exec(ExpandConstant('{sys}\netsh.exe'), Parameters, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      RaiseException('The private-profile coordinator firewall rule could not be created.');
    VerifyOwnedFirewall(PortValue);
    end;
  end;
  try
    SaveFirewallMetadataAtomically(OwnedFirewallMetadata(PortValue));
  except
  begin
    if HadMarker then begin
      Parameters := 'advfirewall firewall set rule name="{#FirewallRule}" program="' +
        ExpandConstant('{app}\{#AppExeName}') + '" new localport=' + PreviousPort +
        ' profile=private enable=yes';
      if (not Exec(ExpandConstant('{sys}\netsh.exe'), Parameters, '', SW_HIDE,
        ewWaitUntilTerminated, RestoreCode)) or (RestoreCode <> 0) then
        RaiseException('The firewall ownership record and rollback both failed.');
      VerifyOwnedFirewall(PreviousPort);
    end else if FirewallState = FirewallRuleStateAbsent then begin
      Parameters := 'advfirewall firewall delete rule name="{#FirewallRule}" program="' +
        ExpandConstant('{app}\{#AppExeName}') + '"';
      if (not Exec(ExpandConstant('{sys}\netsh.exe'), Parameters, '', SW_HIDE,
        ewWaitUntilTerminated, RestoreCode)) or (RestoreCode <> 0) then
        RaiseException('The new firewall rule was created but its ownership record and rollback both failed.');
    end;
    RaiseException('The firewall ownership record could not be saved.');
  end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer; Parameters: String;
begin
  if CurStep = ssPostInstall then begin
    if not HasExistingConfig then begin
      Parameters := '--install-config --hostname "' + HostPage.Values[0] + '" --port ' + PortPage.Values[0];
      if (not Exec(ExpandConstant('{app}\{#AppExeName}'), Parameters,
        ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
        RaiseException('The coordinator configuration could not be validated and saved.');
    end;
    if (not Exec(ExpandConstant('{app}\{#AppExeName}'), '--validate-config',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      RaiseException('The coordinator configuration is invalid.');
    ConfigureOwnedFirewall(PortPage.Values[0]);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin if CurUninstallStep = usUninstall then DeleteOwnedFirewall(False); end;
