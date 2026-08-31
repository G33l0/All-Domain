; Inno Setup script for the Domain Atlas Windows installer.
;
;   1. pyinstaller packaging/domain-atlas.spec --noconfirm
;   2. iscc packaging\domain-atlas.iss
;
; Produces packaging/output/DomainAtlas-Setup-<version>.exe
;
; The default is a per-user install under Local AppData, which needs no
; administrator rights. Choosing "for all users" installs to Program Files.

#define AppName "Domain Atlas"
#define AppExeName "DomainAtlas.exe"
#define AppUserModelID "DomainAtlas.DomainAtlas.Desktop.3"
#define AppCliName "domain-atlas-cli.exe"
#define AppPublisher "Domain Atlas"
#define AppUrl "https://github.com/G33l0/All-Domain"
#ifndef AppVersion
  #define AppVersion "3.0.0"
#endif

[Setup]
AppId={{7C2F6C64-0F8E-4B4E-9E2E-2C7B4E1A9D31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=output
OutputBaseFilename=DomainAtlas-Setup-{#AppVersion}
SetupIconFile=..\assets\domain-atlas.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Default to a per-user install so no administrator prompt is needed; the user
; can still choose an all-users install from the first page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\DomainAtlas\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID must match the identifier the application sets at startup,
; otherwise a pinned shortcut and the running window become two taskbar buttons.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelID}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; AppUserModelID: "{#AppUserModelID}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Collected data lives in the user's profile and is deliberately left in place.
Type: dirifempty; Name: "{app}"
