; Inno Setup script for OctoSlave Windows installer
; Requires: Inno Setup 6.x  (https://jrsoftware.org/isinfo.php)
;
; Build:
;   iscc installers\windows\installer.iss
;
; Output: dist\OctoSlave-Windows-Installer.exe

#define AppName      "OctoSlave"
; AppVersion is single-sourced from pyproject.toml. CI passes the real value
; with  ISCC /DAppVersion=0.3.0 . The fallback below is only used for manual
; local builds that don't override it.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppPublisher "David Kopecky"
#define AppURL       "https://octoslave.karamazov.website"
#define AppExeName   "ots.exe"
#define WizardExe    "OctoSlave-Setup.exe"

[Setup]
AppId={{A3F2C1D4-5E6B-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=OctoSlave-Windows-Installer-{#AppVersion}
; SetupIconFile requires .ico — omitted until an .ico asset is added to the repo
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; WizardSmallImageFile accepts .bmp or .png in Inno Setup 6, but skip for now
; WizardSmallImageFile=..\..\octoslave\web\static\logo.png
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
; Minimum Windows 10
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";    Description: "{cm:CreateDesktopIcon}";    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath";      Description: "Add 'ots' command to PATH";                                            GroupDescription: "Command-line access:"; Flags: checkedonce

[Files]
; CLI binary (ots.exe)
Source: "..\..\dist\ots.exe";              DestDir: "{app}"; Flags: ignoreversion
; First-run setup wizard
Source: "..\..\dist\OctoSlave-Setup.exe";  DestDir: "{app}"; Flags: ignoreversion
; Web launcher batch file (shortcut target)
Source: "web_launcher.bat";                DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu
Name: "{group}\OctoSlave Web UI";          Filename: "{app}\ots.exe";               Parameters: "web"; WorkingDir: "{userdocs}"; Comment: "Launch OctoSlave browser interface"
Name: "{group}\OctoSlave Terminal";        Filename: "{app}\ots.exe";               WorkingDir: "{userdocs}"; Comment: "Open OctoSlave interactive assistant"
Name: "{group}\Configure OctoSlave";       Filename: "{app}\OctoSlave-Setup.exe";   Comment: "Re-run OctoSlave setup wizard"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
; Desktop (optional)
Name: "{autodesktop}\OctoSlave Web UI";    Filename: "{app}\ots.exe";               Parameters: "web"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Registry]
; Add {app} to user PATH
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; \
  Check: NeedsAddPath(ExpandConstant('{app}')); \
  Tasks: addtopath; Flags: preservestringtype

[Run]
; After installation: launch the first-run setup wizard (windowed, no console)
Filename: "{app}\OctoSlave-Setup.exe"; \
  Description: "Configure OctoSlave (API key, backend, model)"; \
  Flags: nowait postinstall skipifsilent; \
  StatusMsg: "Launching setup wizard…"

[Code]
// Helper: check if {app} is already in PATH to avoid duplicate entries
function NeedsAddPath(Param: string): boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Param + ';', ';' + OrigPath + ';') = 0;
end;
