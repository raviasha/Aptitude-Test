#define AppName "KSAT"
#define AppVersion "1.2.2"
#define AppPublisher "College Assessment Lab"
#define AppExeName "KSATServer.exe"

[Setup]
AppId={{AA14D31A-7D94-49E4-9A36-50CBE59C8D61}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\KSAT
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=KSAT-Setup-1.2.2
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\KSATServer.exe"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{autodesktop}\Aptitude Lab.lnk"

[Dirs]
Name: "{commonappdata}\Aptitude Lab"; Permissions: users-modify
Name: "{commonappdata}\Aptitude Lab\Question Banks"; Permissions: users-modify

[Icons]
Name: "{autodesktop}\KSAT"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "Start the KSAT server"
Name: "{group}\KSAT"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Aptitude Lab LAN"" dir=in action=allow protocol=TCP localport=8000 profile=private"; Flags: runhidden
Filename: "{app}\{#AppExeName}"; Description: "Launch KSAT now"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AptitudeLabServer.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM KSATServer.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Aptitude Lab LAN"" protocol=TCP localport=8000"; Flags: runhidden
