; ─────────────────────────────────────────────────────────────────────────────
;  WP Downloader — Windows installer (Inno Setup 6+)
;
;  Cele:
;   • Instaluje do %LOCALAPPDATA%\Programs\WP_Downloader\ — user-scope,
;     BEZ promptu UAC, działa nawet na zarządzanych laptopach służbowych.
;   • Tworzy skróty w menu Start + (opcjonalnie) na pulpicie.
;   • SmartScreen pierwsze uruchomienie pokaże "Nieznany wydawca", ale po
;     "Więcej informacji → Uruchom mimo to" hash trafia do reputation cache
;     i kolejne uruchomienia są ciche.
;   • Build runuje w CI z `dist/WP_Downloader/` jako źródłem.
; ─────────────────────────────────────────────────────────────────────────────

#define MyAppName "WP Downloader"
#define MyAppVersion "1.0"
#define MyAppPublisher "geroo94"
#define MyAppURL "https://github.com/geroo94/wp-downloader"
#define MyAppExeName "WP_Downloader.exe"

[Setup]
AppId={{8F4F1E4A-7A2B-4B5C-9D8E-1F2A3B4C5D6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\WP_Downloader
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\Output
OutputBaseFilename=WP_Downloader_Setup
SetupIconFile=..\static\wp_logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Utwórz skrót na pulpicie"; GroupDescription: "Dodatkowe ikony:"; Flags: unchecked

[Files]
Source: "..\dist\WP_Downloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Odinstaluj {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Uruchom {#MyAppName}"; Flags: nowait postinstall skipifsilent
