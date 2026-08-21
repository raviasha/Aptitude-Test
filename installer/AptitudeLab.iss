#define AppName "Aptitude Lab"
#define AppVersion "1.1.1"
#define AppPublisher "College Assessment Lab"
#define AppExeName "AptitudeLabServer.exe"

[Setup]
AppId={{89D710B6-1437-4FB7-AFF7-0895627E77E3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Aptitude Lab
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=Aptitude-Lab-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\AptitudeLabServer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\Aptitude Lab"; Permissions: users-modify
Name: "{commonappdata}\Aptitude Lab\Question Banks"; Permissions: users-modify

[Icons]
Name: "{autodesktop}\Aptitude Lab"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "Start the Aptitude Lab server"
Name: "{group}\Aptitude Lab"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Aptitude Lab LAN"" dir=in action=allow protocol=TCP localport=8000 profile=private"; Flags: runhidden
Filename: "{app}\{#AppExeName}"; Description: "Launch Aptitude Lab now"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AptitudeLabServer.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Aptitude Lab LAN"" protocol=TCP localport=8000"; Flags: runhidden
