#ifndef KSAT_LAB_MODE
  #define KSAT_LAB_MODE "0"
#endif
#ifndef KSAT_CLIENT_VERSION
  #define KSAT_CLIENT_VERSION "2.1.1"
#endif
#ifndef KSAT_BUNDLE_PUBLISHER_TRUST
  #define KSAT_BUNDLE_PUBLISHER_TRUST "1"
#endif
#ifndef KSAT_PUBLISHER_THUMBPRINT
  #define KSAT_PUBLISHER_THUMBPRINT "13AE2A6440C33E074FC9C99FB35E5A1CFD9BE908"
#endif
#ifndef KSAT_PAYLOAD_DIR
  #define KSAT_PAYLOAD_DIR SourcePath + "..\dist"
#endif
#ifndef KSAT_OUTPUT_DIR
  #define KSAT_OUTPUT_DIR SourcePath + "..\release"
#endif
#define AppVersion KSAT_CLIENT_VERSION
#define GuardHash GetSHA256OfFile(KSAT_PAYLOAD_DIR + "\KSATClientInstallGuard.exe")
#if Int(KSAT_BUNDLE_PUBLISHER_TRUST) == 1
  #define PublisherHash GetSHA256OfFile(KSAT_PAYLOAD_DIR + "\publisher.cer")
#endif

[Setup]
AppId={{F08E1406-AD96-445E-9940-5D54AC2181AE}
AppName=KSAT Lab Client
AppVersion={#AppVersion}
AppPublisher=College Assessment Lab
DefaultDirName={autopf}\KSAT Client
DefaultGroupName=KSAT
DisableProgramGroupPage=yes
OutputDir={#KSAT_OUTPUT_DIR}
OutputBaseFilename=KSATClientSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartIfNeededByRun=no
SetupLogging=yes

[Files]
Source: "{#KSAT_PAYLOAD_DIR}\KSATClient.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#KSAT_PAYLOAD_DIR}\KSATClientUpdater.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#KSAT_PAYLOAD_DIR}\KSATClientInstallGuard.exe"; Flags: dontcopy
#if Int(KSAT_BUNDLE_PUBLISHER_TRUST) == 1
Source: "{#KSAT_PAYLOAD_DIR}\publisher.cer"; Flags: dontcopy
#endif
#if Int(KSAT_LAB_MODE) == 1
Source: "{#KSAT_PAYLOAD_DIR}\lab-profile.json"; Flags: dontcopy
Source: "{#KSAT_PAYLOAD_DIR}\coordinator-ca.pem"; Flags: dontcopy
Source: "{#KSAT_PAYLOAD_DIR}\coordinator-public.json"; Flags: dontcopy
Source: "{#KSAT_PAYLOAD_DIR}\lab-summary.ini"; Flags: dontcopy
#endif

[Dirs]
Name: "{commonappdata}\KSAT Client"; Permissions: admins-full system-full users-readexec
Name: "{commonappdata}\KSAT Client\trust"; Permissions: admins-full system-full users-readexec
Name: "{commonappdata}\KSAT Client\identity"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Client\state"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Client\packs"; Permissions: admins-full system-full
Name: "{commonappdata}\KSAT Client\updates"; Permissions: admins-full system-full

[Icons]
Name: "{group}\KSAT Lab Client"; Filename: "{app}\KSATClient.exe"; Parameters: "--open-client"; WorkingDir: "{app}"
Name: "{autodesktop}\KSAT Lab Client"; Filename: "{app}\KSATClient.exe"; Parameters: "--open-client"; WorkingDir: "{app}"

[Run]
Filename: "{app}\KSATClient.exe"; Parameters: "--open-client"; Description: "Open KSAT Lab Client"; Flags: nowait postinstall skipifsilent

[Code]
var
  UrlPage: TInputQueryWizardPage;
  TrustPage: TInputFileWizardPage;
  Stage: String;
  GuardStarted, GuardCommitted, HadExistingConfiguration: Boolean;

function GetCurrentProcessId(): LongWord;
external 'GetCurrentProcessId@kernel32.dll stdcall';

function PSQuote(Value: String): String;
begin
  StringChangeEx(Value, '''', '''''', True);
  Result := '''' + Value + '''';
end;

procedure PowerShell(Script: String);
var Code: Integer;
begin
  if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "' + Script + '"', '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
    RaiseException('The administrator setup operation failed. See the setup log.');
end;

function ExistingConfiguration(): Boolean;
begin Result := FileExists(ExpandConstant('{commonappdata}\KSAT Client\client-config.json')); end;

procedure StageResource(Name, Hash: String);
begin
  ExtractTemporaryFile(Name);
  if not FileCopy(ExpandConstant('{tmp}\') + Name, Stage + '\' + Name, True) then
    RaiseException('Unable to stage installer resource.');
  if CompareText(GetSHA256OfFile(Stage + '\' + Name), Hash) <> 0 then
    RaiseException('Installer resource integrity check failed.');
end;

procedure InitializeWizard();
var Page: TOutputMsgWizardPage; Summary: String;
begin
  HadExistingConfiguration := ExistingConfiguration();
#if Int(KSAT_LAB_MODE) == 1
  ExtractTemporaryFile('lab-summary.ini');
  Summary := ExpandConstant('{tmp}\lab-summary.ini');
  Page := CreateOutputMsgPage(wpSelectDir, 'Your lab client', 'Ready for this lab',
    'Lab: ' + GetIniString('Lab', 'Name', '', Summary) + #13#10 +
    'Coordinator: ' + GetIniString('Lab', 'URL', '', Summary) + #13#10 +
    'Client version: {#AppVersion}' + #13#10#13#10 +
    'Setup will configure the connection and public certificate trust. Existing student records are preserved.');
#else
  UrlPage := CreateInputQueryPage(wpSelectDir, 'Coordinator address', 'Enter the coordinator HTTPS address.',
    'Later changes require an Administrator.');
  UrlPage.Add('HTTPS URL:', False); UrlPage.Values[0] := ExpandConstant('{param:COORDINATORURL|}');
  TrustPage := CreateInputFilePage(UrlPage.ID, 'Coordinator public trust', 'Select both coordinator export files.',
    'The CA and protocol signing key are validated before saving configuration.');
  TrustPage.Add('Coordinator CA file:', 'PEM files|*.pem|All files|*.*', '.pem');
  TrustPage.Add('Coordinator metadata file:', 'JSON files|*.json|All files|*.*', '.json');
  TrustPage.Values[0] := ExpandConstant('{param:CAFILE|}');
  TrustPage.Values[1] := ExpandConstant('{param:METADATAFILE|}');
#endif
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
#if Int(KSAT_LAB_MODE) == 0
  Result := ExistingConfiguration() and ((PageID = UrlPage.ID) or (PageID = TrustPage.ID));
#endif
end;

function GuardState(): String;
begin Result := GetIniString('Guard', 'State', '', Stage + '\status.ini'); end;

procedure WaitForGuard(Expected: String);
var N: Integer; State, Code: String;
begin
  for N := 1 to 1200 do begin
    State := GuardState();
    if State = Expected then exit;
    if (State = 'blocked') or (State = 'aborted') then begin
      Code := GetIniString('Guard', 'Diagnostic', '', Stage + '\status.ini');
      RaiseException('Installation stopped safely (' + Code + '). Finish tests and uploads first. ' +
        'For a running 2.1.0 client, ask the administrator to stop the KSAT service. ' +
        'If this is the wrong lab package, use the package for the existing coordinator.');
    end;
    Sleep(100);
  end;
  RaiseException('The installation safety check timed out. No assessment should be interrupted.');
end;

procedure GuardCommand(Action: String);
begin
  if not SaveStringToFile(Stage + '\control-next.json', '{"action":"' + Action + '"}', False) then
    RaiseException('Unable to control installation safety guard.');
  PowerShell('$s=' + PSQuote(Stage + '\control-next.json') + ';$d=' + PSQuote(Stage + '\control.json') +
    ';if([IO.File]::Exists($d)){[IO.File]::Replace($s,$d,$null)}else{[IO.File]::Move($s,$d)}');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer; Parent, Token, Script: String;
begin
  Result := '';
  try
#if Int(KSAT_LAB_MODE) == 0
    if (not ExistingConfiguration()) and ((Pos('https://', Lowercase(Trim(UrlPage.Values[0]))) <> 1) or
        (not FileExists(TrustPage.Values[0])) or (not FileExists(TrustPage.Values[1]))) then
      RaiseException('A first installation requires an HTTPS URL and both coordinator public files.');
#endif
    Parent := ExpandConstant('{commonappdata}\KSAT Installer Staging');
    Token := Lowercase(GetSHA256OfString(ExpandConstant('{tmp}') + IntToStr(GetCurrentProcessId())));
    Stage := Parent + '\' + Copy(Token, 1, 32);
    Script := '$ErrorActionPreference=''Stop'';' +
      'Import-Module (Join-Path $PSHOME ''Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1'');' +
      '$parent=' + PSQuote(Parent) + ';$stage=' + PSQuote(Stage) + ';' +
      'if(Test-Path -LiteralPath $stage){throw ''stage collision''};' +
      'foreach($p in @($parent,$stage)){' +
      '$cursor=$p;while($cursor){if((Test-Path -LiteralPath $cursor)-and((Get-Item -Force -LiteralPath $cursor).Attributes-band 1024)){throw ''reparse point''};$cursor=Split-Path -Parent $cursor};' +
      '[IO.Directory]::CreateDirectory($p)|Out-Null;' +
      '$acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);' +
      '$acl.SetOwner([Security.Principal.SecurityIdentifier]::new(''S-1-5-32-544''));' +
      'foreach($sid in @(''S-1-5-18'',''S-1-5-32-544'')){' +
      '$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),''FullControl'',''ContainerInherit,ObjectInherit'',''None'',''Allow''))};' +
      'Set-Acl -LiteralPath $p -AclObject $acl}';
    PowerShell(Script);
    StageResource('KSATClientInstallGuard.exe', '{#GuardHash}');
#if Int(KSAT_BUNDLE_PUBLISHER_TRUST) == 1
    StageResource('publisher.cer', '{#PublisherHash}');
#endif
#if Int(KSAT_LAB_MODE) == 1
    StageResource('lab-profile.json', '{#GetSHA256OfFile(KSAT_PAYLOAD_DIR + "\lab-profile.json")}');
    StageResource('coordinator-ca.pem', '{#GetSHA256OfFile(KSAT_PAYLOAD_DIR + "\coordinator-ca.pem")}');
    StageResource('coordinator-public.json', '{#GetSHA256OfFile(KSAT_PAYLOAD_DIR + "\coordinator-public.json")}');
#else
    if not ExistingConfiguration() then begin
      if not FileCopy(TrustPage.Values[0], Stage + '\coordinator-ca.pem', True) then RaiseException('CA copy failed.');
      if not FileCopy(TrustPage.Values[1], Stage + '\coordinator-public.json', True) then RaiseException('Metadata copy failed.');
      SaveStringToFile(Stage + '\coordinator-url.txt', UTF8Encode(UrlPage.Values[0]), False);
    end;
#endif
    PowerShell('[IO.File]::WriteAllText(' + PSQuote(Stage + '\install-context.json') +
      ',(@{installer=' + PSQuote(ExpandConstant('{srcexe}')) + '}|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))');
    if not Exec(Stage + '\KSATClientInstallGuard.exe', '--stage "' + Stage + '" --installer-pid ' +
      IntToStr(GetCurrentProcessId()), Stage, SW_HIDE, ewNoWait, Code) then RaiseException('Unable to start installation guard.');
    GuardStarted := True;
    WaitForGuard('ready');
  except
    Result := GetExceptionMessage();
  end;
end;

procedure ServiceCommand(Args: String);
var Code: Integer;
begin
  if (not Exec(ExpandConstant('{sys}\sc.exe'), Args, '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
    RaiseException('The KSAT client service could not be configured.');
end;

procedure ProtectAuthorityDirectory(PathName: String);
var Code: Integer;
begin
  if (not Exec(ExpandConstant('{sys}\icacls.exe'), '"' + PathName + '" /inheritance:r /grant:r ' +
    '"*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "NT SERVICE\KSATLabClientAuthority:(OI)(CI)M"',
    '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then RaiseException('Client data protection failed.');
  if (not Exec(ExpandConstant('{sys}\icacls.exe'), '"' + PathName + '" /setowner "*S-1-5-32-544"',
    '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then RaiseException('Client data ownership failed.');
end;

procedure SeedLastKnownGood();
begin
  PowerShell('$ErrorActionPreference=''Stop'';Import-Module (Join-Path $PSHOME ''Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1'');' +
    '$src=' + PSQuote(ExpandConstant('{srcexe}')) + ';$s=Get-AuthenticodeSignature -LiteralPath $src;' +
    'if($s.Status-ne ''Valid'' -or $s.SignerCertificate.Thumbprint-ne ''{#KSAT_PUBLISHER_THUMBPRINT}''){throw ''invalid installer signature''};' +
    '$d=' + PSQuote(ExpandConstant('{commonappdata}\KSAT Client\updates\last-known-good')) + ';' +
    '[IO.Directory]::CreateDirectory($d)|Out-Null;Copy-Item -LiteralPath $src -Destination ($d+''\KSATClientSetup-{#AppVersion}.exe'')');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer; ImagePath, Verb: String;
begin
  if CurStep = ssPostInstall then begin
    GuardCommand('configure'); WaitForGuard('configured');
    ImagePath := ExpandConstant('{app}\KSATClient.exe');
    if Exec(ExpandConstant('{sys}\sc.exe'), 'query KSATLabClientAuthority', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then Verb := 'config' else Verb := 'create';
    ServiceCommand(Verb + ' KSATLabClientAuthority binPath= "\"' + ImagePath + '\" --windows-service" start= auto obj= LocalSystem DisplayName= "KSAT Lab Client Authority"');
    ServiceCommand('sidtype KSATLabClientAuthority unrestricted');
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\identity'));
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\state'));
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\packs'));
    ProtectAuthorityDirectory(ExpandConstant('{commonappdata}\KSAT Client\updates'));
    if not HadExistingConfiguration then SeedLastKnownGood();
    GuardCommand('commit'); WaitForGuard('committed'); GuardCommitted := True;
    ServiceCommand('start KSATLabClientAuthority');
    PowerShell('$ErrorActionPreference=''Stop'';$ok=$false;for($i=0;$i-lt 60;$i++){try{' +
      '$r=Invoke-RestMethod -Uri ''http://127.0.0.1:8010/api/build'' -TimeoutSec 2;' +
      'if($r.version-eq ''{#AppVersion}''){$ok=$true;break}}catch{};Start-Sleep -Milliseconds 500};' +
      'if(-not $ok){throw ''client local health check failed''}');
    WizardForm.FinishedLabel.Caption := 'KSAT client is installed. Open KSAT to check coordinator connectivity. ' +
      'If the coordinator is offline, reconnect and retry; do not reinstall or remove student data.';
  end;
end;

procedure DeinitializeSetup();
begin
  if GuardStarted and (not GuardCommitted) then begin
    try GuardCommand('abort'); except Log('Unable to signal guard; parent-exit recovery will run.'); end;
  end;
  { Retain exact protected stage for parent-exit recovery and diagnostics. }
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Code: Integer;
begin
  if CurUninstallStep = usUninstall then begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop KSATLabClientAuthority', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Exec(ExpandConstant('{sys}\sc.exe'), 'delete KSATLabClientAuthority', '', SW_HIDE, ewWaitUntilTerminated, Code);
    { Preserve records and public trust which other KSAT products may share. }
  end;
end;
