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
