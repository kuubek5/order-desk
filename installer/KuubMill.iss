#define MyAppName "KuubMill"
; Must be bumped by hand together with app/__version__.py::VERSION at every
; release — tests/test_version_sync.py fails the suite if these two drift.
#define MyAppVersion "0.21.169"
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
; Оформлення майстра. Inno читає ЛИШЕ BMP — PNG чи JPEG компілятор не приймає,
; і промах тихий: збірка падає аж на етапі компіляції інсталятора.
; Список через кому — Inno сам бере потрібний файл під DPI екрана (100/125/150/200 %).
; Кадр: розжарені коронки в печі — той самий світ, що арт `furnace-crowns.jpg`
; у віджеті печей і на табло цеху, щоб інсталятор і застосунок були однією рукою.
WizardImageFile=..\assets\wizard-image-164x314.bmp,..\assets\wizard-image-192x386.bmp,..\assets\wizard-image-246x459.bmp,..\assets\wizard-image-328x628.bmp
WizardSmallImageFile=..\assets\wizard-small-55x58.bmp,..\assets\wizard-small-64x68.bmp,..\assets\wizard-small-83x80.bmp,..\assets\wizard-small-110x116.bmp

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
  KillCode: Integer;
  ResultCode2: Integer;
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

  { 17.11.26: сервер зупинився, а процес жив далі на завислому потоці
    (розрахунок підказки диска, 3 год). Restart Manager 30 с не міг його закрити,
    у тихому режимі відповів «Abort», і 0.21.147 відкотилась. Тому після чемного
    --shutdown усе, що лишилось від KuubMill.exe, закриваємо силоміць — лише
    наш exe, нічого чужого. База вже скопійована перед оновленням, а незакомічене
    SQLite відкотить само. Не зупинився й так — тоді вже повідомляємо. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#MyAppExeName}', '', SW_HIDE,
    ewWaitUntilTerminated, KillCode);
  { Друга спроба того ж вечора впала вже без зависання: Restart Manager бачив
    лише «Lab  Application» — процес, що живе між перезапусками й тримає наші
    файли. Тому закриваємо й усе інше, що ЗАПУЩЕНЕ з теки програми (за шляхом
    exe — лише наше), а не тільки KuubMill.exe за іменем. }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith(''' +
    ExpandConstant('{app}') + '\'', [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode2);
  Sleep(1500);

  if (ResultCode <> 0) and (KillCode <> 0) and (KillCode <> 128) then
    Result := 'KuubMill не завершив роботу вчасно. Закрийте програму та повторіть спробу.';
end;
