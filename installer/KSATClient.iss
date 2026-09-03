#define AppName "KSAT Lab Client"
#define AppVersion "2.0.0"
#define AppPublisher "College Assessment Lab"
#define AppExeName "KSATClient.exe"

[Setup]
AppId={{F08E1406-AD96-445E-9940-5D54AC2181AE}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
UninstallDisplayName={#AppName} {#AppVersion}
DefaultDirName={autopf}\KSAT Client
DefaultGroupName=KSAT
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=KSATClientSetup-2.0.0
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
Source: "..\dist\KSATClient.exe"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\KSAT Client"; Permissions: admins-full system-full users-readexec
Name: "{commonappdata}\KSAT Client\trust"; Permissions: admins-full system-full users-readexec
Name: "{commonappdata}\KSAT Client\identity"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Client\state"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Client\packs"; Permissions: admins-full system-full

[Icons]
Name: "{group}\KSAT Lab Client"; Filename: "{app}\{#AppExeName}"; Parameters: "--open-client"; WorkingDir: "{app}"
Name: "{autodesktop}\KSAT Lab Client"; Filename: "{app}\{#AppExeName}"; Parameters: "--open-client"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--open-client"; Description: "Open KSAT Lab Client"; Flags: nowait postinstall skipifsilent

[Code]
var
  UrlPage: TInputQueryWizardPage;
  TrustPage: TInputFileWizardPage;

function ConfigPath(): String;
begin Result := ExpandConstant('{commonappdata}\KSAT Client\client-config.json'); end;
function OwnedCaMarker(): String;
begin Result := ExpandConstant('{commonappdata}\KSAT Client\trust\installer-owned-root-ca.json'); end;
function InstalledCaPath(): String;
begin Result := ExpandConstant('{commonappdata}\KSAT Client\trust\coordinator-ca.pem'); end;
function ExistingConfiguration(): Boolean;
begin Result := FileExists(ConfigPath()); end;

procedure StopClientService(); forward;

procedure InitializeWizard();
begin
  UrlPage := CreateInputQueryPage(wpSelectDir, 'Coordinator address',
    'Enter the coordinator HTTPS address.',
    'This value is saved on the computer. Later changes require an Administrator.');
  UrlPage.Add('HTTPS URL:', False); UrlPage.Values[0] := ExpandConstant('{param:COORDINATORURL|}');
  TrustPage := CreateInputFilePage(UrlPage.ID, 'Coordinator public trust',
    'Select both files exported by the faculty coordinator.',
    'The installer validates the CA hash and protocol-signing key before saving configuration.');
  TrustPage.Add('Coordinator CA file:', 'PEM files|*.pem|All files|*.*', '.pem');
  TrustPage.Add('Coordinator metadata file:', 'JSON files|*.json|All files|*.*', '.json');
  TrustPage.Values[0] := ExpandConstant('{param:CAFILE|}');
  TrustPage.Values[1] := ExpandConstant('{param:METADATAFILE|}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := ExistingConfiguration() and ((PageID = UrlPage.ID) or (PageID = TrustPage.ID));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = UrlPage.ID) and (Pos('https://', Lowercase(Trim(UrlPage.Values[0]))) <> 1) then
  begin MsgBox('Enter an HTTPS coordinator URL.', mbError, MB_OK); Result := False; end;
  if (CurPageID = TrustPage.ID) and
     ((not FileExists(TrustPage.Values[0])) or (not FileExists(TrustPage.Values[1]))) then
  begin MsgBox('Select the coordinator-ca.pem and coordinator-public.json files.', mbError, MB_OK); Result := False; end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if WizardSilent() and (not ExistingConfiguration()) and
    ((Trim(UrlPage.Values[0]) = '') or (not FileExists(TrustPage.Values[0])) or
     (not FileExists(TrustPage.Values[1]))) then
    Result := 'A first silent install requires /COORDINATORURL=, /CAFILE=, and /METADATAFILE=.';
  if Result = '' then StopClientService();
end;

procedure ProtectAuthorityDirectory(PathName: String);
var ResultCode: Integer; Parameters: String;
begin
  Parameters := '"' + PathName + '" /inheritance:r /grant:r ' +
    '"*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" ' +
    '"NT SERVICE\KSATLabClientAuthority:(OI)(CI)M"';
  if (not Exec(ExpandConstant('{sys}\icacls.exe'), Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The LocalSystem client-service data permissions could not be applied.');
end;

function ServiceExists(): Boolean;
var ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query "KSATLabClientAuthority"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure StopClientService();
var ResultCode: Integer;
begin
  if ServiceExists() then
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop "KSATLabClientAuthority"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure ConfigureClientService();
var ResultCode: Integer; Parameters, ImagePath: String;
begin
  ImagePath := ExpandConstant('{app}\{#AppExeName}');
  if ServiceExists() then
    Parameters := 'config "KSATLabClientAuthority" binPath= "\"' + ImagePath +
      '\" --windows-service" start= auto obj= LocalSystem DisplayName= "KSAT Lab Client Authority"'
  else
    Parameters := 'create "KSATLabClientAuthority" binPath= "\"' + ImagePath +
      '\" --windows-service" start= auto obj= LocalSystem DisplayName= "KSAT Lab Client Authority"';
  if (not Exec(ExpandConstant('{sys}\sc.exe'), Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The LocalSystem client service could not be configured.');
  if (not Exec(ExpandConstant('{sys}\sc.exe'), 'sidtype "KSATLabClientAuthority" unrestricted',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The client service SID could not be enabled.');
end;

procedure StartClientService();
var ResultCode: Integer;
begin
  if (not Exec(ExpandConstant('{sys}\sc.exe'), 'start "KSATLabClientAuthority"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The LocalSystem client service could not be started.');
end;

procedure DeleteClientService();
var ResultCode: Integer;
begin
  StopClientService();
  if ServiceExists() and
    ((not Exec(ExpandConstant('{sys}\sc.exe'), 'delete "KSATLabClientAuthority"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0)) then
    RaiseException('The LocalSystem client service could not be removed.');
end;

procedure EnsureRootCa();
var ResultCode: Integer; Script, Parameters: String;
begin
  if not FileExists(InstalledCaPath()) then RaiseException('The installed coordinator CA file is missing.');
  Script :=
    '$ErrorActionPreference=''Stop'';' +
    '$p=''' + InstalledCaPath() + ''';$m=''' + OwnedCaMarker() + ''';' +
    '$c=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($p);' +
    '$t=$c.Thumbprint.ToUpperInvariant();$owned=@();' +
    'if(Test-Path -LiteralPath $m){$raw=Get-Content -Raw -LiteralPath $m;' +
    '$j=$raw|ConvertFrom-Json;' +
    '$names=@($j.PSObject.Properties.Name);' +
    'if(($names.Count-ne 1)-or($names[0]-ne ''Thumbprints'')){throw ''invalid CA ownership marker''};' +
    'if(-not($j.Thumbprints-is [System.Array])){throw ''invalid CA ownership marker''};' +
    'if(@($j.Thumbprints|Where-Object{$_-isnot [string]}).Count-ne 0){throw ''invalid CA ownership marker''};' +
    '$owned=@($j.Thumbprints|ForEach-Object{$_.ToString().ToUpperInvariant()});' +
    'if((@($owned|Where-Object{$_-notmatch ''^[0-9A-F]{40}$''}).Count-ne 0)-or' +
    '(@($owned|Sort-Object -Unique).Count-ne $owned.Count)){throw ''invalid CA ownership marker''};' +
    '$canonical=@{Thumbprints=@($owned)}|ConvertTo-Json -Compress;' +
    'if($raw-cne $canonical){throw ''invalid CA ownership marker''}};' +
    '$s=New-Object System.Security.Cryptography.X509Certificates.X509Store(''Root'',''LocalMachine'');' +
    '$added=$false;try{$s.Open(''ReadWrite'');' +
    '$found=@($s.Certificates|Where-Object{$_.Thumbprint.ToUpperInvariant()-eq $t}).Count-gt 0;' +
    'if(-not $found){$s.Add($c);$added=$true};' +
    '$verified=@($s.Certificates|Where-Object{$_.Thumbprint.ToUpperInvariant()-eq $t}).Count-gt 0;' +
    'if(-not $verified){throw ''CA store verification failed''};' +
    'if($added -and $owned -notcontains $t){$owned+=@($t)};' +
    'if($added){$tmp=$m+''.''+[Guid]::NewGuid().ToString(''N'')+''.tmp'';' +
    '@{Thumbprints=@($owned|Sort-Object -Unique)}|ConvertTo-Json -Compress|' +
    'Set-Content -LiteralPath $tmp -Encoding Ascii -NoNewline;' +
    'Move-Item -LiteralPath $tmp -Destination $m -Force}}' +
    'catch{if($added){@($s.Certificates|Where-Object{$_.Thumbprint.ToUpperInvariant()-eq $t})|' +
    'ForEach-Object{$s.Remove($_)}};throw}finally{$s.Close()}';
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The coordinator CA could not be installed or verified in Local Machine Trusted Root.');
end;

procedure RemoveOwnedRootCas();
var ResultCode: Integer; Script, Parameters: String;
begin
  if not FileExists(OwnedCaMarker()) then exit;
  Script :=
    '$ErrorActionPreference=''Stop'';$m=''' + OwnedCaMarker() + ''';' +
    '$raw=Get-Content -Raw -LiteralPath $m;$j=$raw|ConvertFrom-Json;' +
    '$names=@($j.PSObject.Properties.Name);' +
    'if(($names.Count-ne 1)-or($names[0]-ne ''Thumbprints'')){throw ''invalid CA ownership marker''};' +
    'if(-not($j.Thumbprints-is [System.Array])){throw ''invalid CA ownership marker''};' +
    'if(@($j.Thumbprints|Where-Object{$_-isnot [string]}).Count-ne 0){throw ''invalid CA ownership marker''};' +
    '$owned=@($j.Thumbprints|ForEach-Object{$_.ToString().ToUpperInvariant()});' +
    'if((@($owned|Where-Object{$_-notmatch ''^[0-9A-F]{40}$''}).Count-ne 0)-or' +
    '(@($owned|Sort-Object -Unique).Count-ne $owned.Count)){throw ''invalid CA ownership marker''};' +
    '$canonical=@{Thumbprints=@($owned)}|ConvertTo-Json -Compress;' +
    'if($raw-cne $canonical){throw ''invalid CA ownership marker''};' +
    '$s=New-Object System.Security.Cryptography.X509Certificates.X509Store(''Root'',''LocalMachine'');' +
    'try{$s.Open(''ReadWrite'');foreach($t in $owned){' +
    '@($s.Certificates|Where-Object{$_.Thumbprint.ToUpperInvariant()-eq $t})|' +
    'ForEach-Object{$s.Remove($_)}}}finally{$s.Close()};Remove-Item -LiteralPath $m -Force';
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"';
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The installer-owned coordinator CA could not be removed.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer; Parameters: String;
begin
  if CurStep = ssPostInstall then begin
    StopClientService();
    if not ExistingConfiguration() then begin
      Parameters := '--install-config --base-url "' + UrlPage.Values[0] +
        '" --ca "' + TrustPage.Values[0] + '" --metadata "' + TrustPage.Values[1] + '"';
      if (not Exec(ExpandConstant('{app}\{#AppExeName}'), Parameters,
        ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
        RaiseException('The coordinator trust bundle could not be validated and saved.');
    end;
    if (not Exec(ExpandConstant('{app}\{#AppExeName}'), '--validate-config',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      RaiseException('The installed coordinator trust configuration is invalid.');
    EnsureRootCa();
    ConfigureClientService();
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\identity'));
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\state'));
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\packs'));
    StartClientService();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    DeleteClientService();
    RemoveOwnedRootCas();
  end;
end;
