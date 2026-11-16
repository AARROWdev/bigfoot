; Bigfoot installer (Inno Setup 6). Built by build.ps1, which passes /DAppVersion=<VERSION from bigfoot.py>.
; Packs the PyInstaller one-folder build in build\dist\Bigfoot.

#ifndef AppVersion
  #error Build with build.ps1 (it passes /DAppVersion)
#endif

[Setup]
AppId={{3484C9CA-B551-4A47-A264-7723F3745472}
AppName=Bigfoot
AppVersion={#AppVersion}
AppVerName=Bigfoot {#AppVersion}
AppPublisher=AARROWdev
AppPublisherURL=https://github.com/AARROWdev/bigfoot
DefaultDirName={autopf}\Bigfoot
DefaultGroupName=Bigfoot
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=force
RestartApplications=no
SetupIconFile=..\bigfoot.ico
UninstallDisplayIcon={app}\Bigfoot.exe
UninstallDisplayName=Bigfoot
OutputDir=..\dist
OutputBaseFilename=BigfootSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\build\dist\Bigfoot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Bigfoot"; Filename: "{app}\Bigfoot.exe"
Name: "{autodesktop}\Bigfoot"; Filename: "{app}\Bigfoot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Bigfoot.exe"; Description: "{cm:LaunchProgram,Bigfoot}"; Flags: nowait postinstall skipifsilent

[Code]
// Bigfoot holds "BigfootAppMutex" (and the Global\ one, seen from other sessions) while it runs. Setup closes
// it (CloseApplications), but the uninstaller doesn't, so it refuses to start until Bigfoot is closed instead
// of leaving a half-removed folder.
function InitializeUninstall(): Boolean;
begin
  Result := True;
  while Result and CheckForMutexes('BigfootAppMutex,Global\BigfootAppMutex') do
    if UninstallSilent or (MsgBox('Bigfoot is running. Close it, then click OK to uninstall.',
                                  mbError, MB_OKCANCEL) <> IDOK) then
      Result := False;
end;
