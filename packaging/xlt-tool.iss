; XLT Tool's Windows installer (Inno Setup 6), made by build-windows.ps1 from its folder build:
;   ISCC /DAppVersion=1.0.0 /DSourceDir=<...\XLT Tool> /DIconFile=<...ico> /DOutputDir=<dist> xlt-tool.iss
;
; It installs for the current user (no admin prompt), in %LOCALAPPDATA%\Programs\XLT Tool;
; "Install for all users" in the first page puts it in Program Files instead. A newer
; installer run over it replaces it in place. Uninstalling leaves the user's settings,
; library and save backups (%APPDATA%\xlt-tool) alone.

#ifndef AppVersion
  #error AppVersion is not set (/DAppVersion=...)
#endif
#ifndef SourceDir
  #error SourceDir is not set (/DSourceDir=...)
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef FileVersion
  ; The file version must be numbers only (1.1.0, not 1.1.0-beta).
  #define FileVersion AppVersion
#endif

[Setup]
; Never change AppId: it is how Windows (and winget) know a newer installer is the same app.
AppId={{4DDE8C6D-F024-4660-8A3C-E0FB36C65D34}
AppName=XLT Tool
AppVersion={#AppVersion}
AppVerName=XLT Tool {#AppVersion}
AppPublisher=XLT
AppPublisherURL=https://github.com/boubounokefalos/xlt-tool
AppSupportURL=https://github.com/boubounokefalos/xlt-tool/issues
AppUpdatesURL=https://github.com/boubounokefalos/xlt-tool/releases
VersionInfoVersion={#FileVersion}
VersionInfoDescription=XLT Tool installer
DefaultDirName={autopf}\XLT Tool
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
#ifdef IconFile
SetupIconFile={#IconFile}
#endif
UninstallDisplayIcon={app}\XLT Tool.exe
UninstallDisplayName=XLT Tool
; An open XLT Tool is asked to close before its files are replaced.
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=XLT_Tool-{#AppVersion}-windows-x64-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; A new version's PyInstaller folder replaces the old one whole: no stale libraries left beside it.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\bin"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\XLT Tool"; Filename: "{app}\XLT Tool.exe"
Name: "{autodesktop}\XLT Tool"; Filename: "{app}\XLT Tool.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\XLT Tool.exe"; Description: "{cm:LaunchProgram,XLT Tool}"; Flags: nowait postinstall skipifsilent
