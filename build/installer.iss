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
;
;  VC++ Redistributable (opcjonalny prerequisite, patrz [Code] niżej):
;   • Instalowany TYLKO gdy faktycznie brakuje (sonda rejestru) — większość
;     Windows 10/11 już go ma z Windows Update, więc krok zwykle jest no-op.
;   • Sam ten JEDEN krok wymaga elevacji (WinSxS/System32 — nieodłączna
;     właściwość VC++ Redist, nie do obejścia) — ShellExec('runas', ...)
;     każe WINDOWSOWI pokazać jego WŁASNY, widoczny prompt UAC tylko dla
;     tego kroku. Reszta instalatora (PrivilegesRequired=lowest) zostaje
;     w pełni bez adminarights — nic się tu nie zmienia w tym względzie.
;   • Plik vc_redist.x64.exe jest dołączany WARUNKOWO (external +
;     skipifsourcedoesntexist) — jeśli CI/build lokalny go nie pobrał do
;     build\vc_redist.x64.exe, krok cicho się pomija (bez błędu kompilacji
;     .iss), a user i tak może dociągnąć go później przez WP Environment
;     Checker → "Instaluj brakujące komponenty".
;
;  Node.js CELOWO nie jest tu bundlowany jako obowiązkowy prerequisite:
;   aplikacja używa WŁASNEGO, dołączonego Deno do JS Challenge YouTube
;   (patrz yt_dlp_worker.py:_detect_js_runtime) — Node.js nie jest wymagany
;   do działania. Dostępny jako czysto opcjonalna instalacja przez
;   WP Environment Checker, dla userów którzy z innych powodów go chcą.
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
; Dołączany WARUNKOWO (patrz komentarz przy [Setup] wyżej) — dontcopy: NIE
; trafia do {app}, tylko do wewnętrznego payloadu instalatora, wyciągany na
; żądanie w [Code] TYLKO jeśli VC++ Redist faktycznie brakuje w systemie.
Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: dontcopy external skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Odinstaluj {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Uruchom {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function IsVCRedistInstalled(): Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64', 'Installed', Installed)
    and (Installed = 1);
end;

procedure InstallVCRedistIfMissing();
var
  ResultCode: Integer;
  TmpFile: String;
begin
  if IsVCRedistInstalled() then
  begin
    Log('VC++ Redistributable: już zainstalowany, pomijam.');
    Exit;
  end;
  TmpFile := ExpandConstant('{tmp}\vc_redist.x64.exe');
  try
    ExtractTemporaryFile('vc_redist.x64.exe');
  except
    Log('VC++ Redistributable: brak dołączonego instalatora w tym buildzie — pomijam ' +
        '(WP Environment Checker → "Instaluj brakujące komponenty" dociągnie go później).');
    Exit;
  end;
  // 'runas': WŁASNY, oddzielny prompt UAC TYLKO dla tego kroku — reszta
  // instalatora (PrivilegesRequired=lowest, patrz [Setup]) zostaje bez
  // adminarights, zgodnie z pierwotnym zamysłem dla laptopów firmowych.
  if ShellExec('runas', TmpFile, '/install /quiet /norestart', '',
               SW_SHOW, ewWaitUntilTerminated, ResultCode) then
    Log('VC++ Redistributable: instalator uruchomiony, kod wyjścia=' + IntToStr(ResultCode))
  else
    Log('VC++ Redistributable: nie udało się uruchomić instalatora (UAC odrzucone?).');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallVCRedistIfMissing();
end;
