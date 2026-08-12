#define MyAppName "Order Desk"
; Must be bumped by hand together with app/__version__.py::VERSION at every
; release — tests/test_version_sync.py fails the suite if these two drift.
#define MyAppVersion "0.1.8"
#define MyAppExeName "OrderDesk.exe"
#ifndef BuildRoot
  #define BuildRoot "..\dist\OrderDesk"
#endif
#ifndef OutputRoot
  #define OutputRoot "..\dist-installer"
#endif

[Setup]
AppId={{D2C25C62-8303-4D8F-A525-8B45E4059B88}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\OrderDesk
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir={#OutputRoot}
OutputBaseFilename=OrderDesk-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Order Desk"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"
Name: "{autodesktop}\Order Desk"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; Flags: checkedonce
Name: "autostart"; Description: "Запускати Order Desk при вході у Windows"; Flags: checkedonce

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OrderDesk"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Interactive installs relaunch via this postinstall entry (skipped under
; /VERYSILENT). The silent auto-update path can't relaunch from here — a
; headless/service context has no desktop session for the launch — so the
; in-app updater handles its own relaunch via a watchdog script (see
; app/update_check.py::launch_silent_install).
Filename: "{app}\{#MyAppExeName}"; Parameters: "--open-browser"; Description: "Запустити Order Desk"; Flags: nowait postinstall skipifsilent

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
    Result := 'Не вдалося зупинити Order Desk перед оновленням.';
    Exit;
  end;

  if ResultCode <> 0 then
    Result := 'Order Desk не завершив роботу вчасно. Закрийте програму та повторіть спробу.';
end;
