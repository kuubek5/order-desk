#define MyAppName "KuubMill"
; Must be bumped by hand together with app/__version__.py::VERSION at every
; release — tests/test_version_sync.py fails the suite if these two drift.
#define MyAppVersion "0.5.1"
#define MyAppExeName "KuubMill.exe"
#ifndef BuildRoot
  #define BuildRoot "..\dist\KuubMill"
#endif
#ifndef OutputRoot
  #define OutputRoot "..\dist-installer"
#endif

[Setup]
AppId={{D2C25C62-8303-4D8F-A525-8B45E4059B88}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\KuubMill
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir={#OutputRoot}
OutputBaseFilename=KuubMill-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\kuubmill.ico

[Files]
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\KuubMill"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"
Name: "{autodesktop}\KuubMill"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; Flags: checkedonce
Name: "autostart"; Description: "Запускати KuubMill при вході у Windows"; Flags: checkedonce

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "KuubMill"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Interactive installs relaunch via this postinstall entry (skipped under
; /VERYSILENT). The silent auto-update path can't relaunch from here — a
; headless/service context has no desktop session for the launch — so the
; in-app updater handles its own relaunch via a watchdog script (see
; app/update_check.py::launch_silent_install).
Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"; Description: "Запустити KuubMill"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--shutdown"; Flags: runhidden waituntilterminated; RunOnceId: "StopOrderDesk"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExistingExe: String;
  ResultCode: Integer;
begin
  Result := '';
  ExistingExe := ExpandConstant('{app}\{#MyAppExeName}');
  if not FileExists(ExistingExe) then
    Exit;

  { Ask the running copy to stop through its named Windows event before
    Restart Manager checks which application locks the installation files. }
  if not Exec(ExistingExe, '--shutdown', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Не вдалося зупинити KuubMill перед оновленням.';
    Exit;
  end;

  if ResultCode <> 0 then
    Result := 'KuubMill не завершив роботу вчасно. Закрийте програму та повторіть спробу.';
end;
