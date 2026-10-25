; Inno Setup script of the Windows installer (Inno Setup 6).
;
; Installs the PyInstaller folder (dist\vinyl-check, built by packaging\vinyl-check.spec) for the current user without
; administrator rights (or for all users, if chosen in the first dialog), with a Start menu entry, an optional desktop
; icon and an uninstaller (Settings → Apps). Recordings, database and configuration live in %LOCALAPPDATA%\vinyl-check
; and are kept when vinyl-check is uninstalled.
;
; Build from the repository root:
;   iscc /DAppVersion=1.0.1 packaging\windows\vinyl-check.iss
; Optional: /DAppDir=<PyInstaller folder> /DOutputDir=<folder> /DIconFile=<.ico> (defaults: the PyInstaller defaults).
; Output: vinyl-check-<version>-windows-x64-setup.exe

#ifndef AppVersion
  #error Define the version: iscc /DAppVersion=x.y.z
#endif
#ifndef AppDir
  #define AppDir "..\..\dist\vinyl-check"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\vinyl-check\icons\vinyl-check.ico"
#endif

[Setup]
; The AppId identifies the installation for updates and the uninstaller: never change it
AppId={{01A9822F-5B56-4321-A32C-DDA1FFD43D6E}
AppName=vinyl-check
AppVersion={#AppVersion}
AppVerName=vinyl-check {#AppVersion}
AppPublisher=Michael Pietzsch
AppPublisherURL=https://github.com/michaelpietzsch/vinyl-check
AppSupportURL=https://github.com/michaelpietzsch/vinyl-check/issues
AppUpdatesURL=https://github.com/michaelpietzsch/vinyl-check/releases
AppCopyright=Copyright (c) 2026 Michael Pietzsch
DefaultDirName={autopf}\vinyl-check
DefaultGroupName=vinyl-check
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=vinyl-check-{#AppVersion}-windows-x64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\vinyl-check.exe
UninstallDisplayName=vinyl-check
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Close a running vinyl-check before files are replaced (update) or removed (uninstall)
CloseApplications=yes
; Version resource of the installer itself: code signing checks product name and version
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=vinyl-check
VersionInfoProductVersion={#AppVersion}.0
VersionInfoProductTextVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoDescription=vinyl-check setup
VersionInfoCompany=Michael Pietzsch
VersionInfoCopyright=Copyright (c) 2026 Michael Pietzsch

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The license texts also next to vinyl-check.exe, where people look for them
Source: "{#AppDir}\_internal\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#AppDir}\_internal\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\vinyl-check"; Filename: "{app}\vinyl-check.exe"
Name: "{autodesktop}\vinyl-check"; Filename: "{app}\vinyl-check.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\vinyl-check.exe"; Description: "{cm:LaunchProgram,vinyl-check}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the program folder (files written there at run time, e.g. byte code caches); the user's data stay
Type: filesandordirs; Name: "{app}"
