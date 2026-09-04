; KMill Agent — інсталятор для ПК верстата (Windows 7/8/10/11).
; Ставить kmill-agent.exe, робить ярлик «налаштування» в меню Пуск і одразу
; відкриває меню налаштувань (`-setup`), де оператор вписує токен/назву/монітор.
; Реєстрацію автозапуску (Task Scheduler) і правило брандмауера робить сам агент
; при збереженні — інсталятор лишається простим.
;
; Збирається так (CI або локально з Inno Setup 6):
;   ISCC.exe /DMyAppVersion=0.1.0 installer.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{B7B3F0C2-7C4E-4E2A-9A1E-KMILLAGENT01}}
AppName=KMill Agent
AppVersion={#MyAppVersion}
AppPublisher=KMill
DefaultDirName={autopf}\KMill Agent
DefaultGroupName=KMill Agent
DisableProgramGroupPage=yes
UninstallDisplayName=KMill Agent
OutputBaseFilename=KMillAgent-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern

[Languages]
Name: "ukr"; MessagesFile: "compiler:Default.isl"

[Code]
{ Прибити стару копію агента ПЕРЕД копіюванням файлів: інакше запущений
  kmill-agent.exe (-serve або старе -setup меню) тримає і сам файл, і порт
  8766, тож перевстановлення лишало б завислий старий процес зі старою
  сторінкою. Best-effort — ігноруємо код повернення. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/im kmill-agent.exe /f',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

[Files]
Source: "kmill-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\KMill Agent — налаштування"; Filename: "{app}\kmill-agent.exe"; Parameters: "-setup"
Name: "{group}\Видалити KMill Agent"; Filename: "{uninstallexe}"

[Run]
; ОДИН елевований прохід (інсталятор уже адмін): створює конфіг+токен, реєструє
; автозапуск, відкриває брандмауер і стартує агента. Без ручного «Зберегти», без
; окремого UAC. Записує crm-setup.txt із токеном і адресою для CRM.
Filename: "{app}\kmill-agent.exe"; Parameters: "-install"; StatusMsg: "Налаштування агента верстата…"; Flags: runhidden waituntilterminated
; Показати оператору токен і адресу, які треба вписати в KMill → Верстати.
Filename: "notepad.exe"; Parameters: """{app}\crm-setup.txt"""; Description: "Показати токен і адресу для CRM"; Flags: postinstall skipifsilent nowait
; За бажанням — відкрити меню налаштувань (назва/монітор). Меню тепер завжди
; доступне за http://127.0.0.1:8766, бо його віддає сам агент.
Filename: "{app}\kmill-agent.exe"; Parameters: "-setup"; Description: "Відкрити меню налаштувань агента"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Прибрати автозапуск і правило брандмауера, які створив агент.
Filename: "schtasks"; Parameters: "/delete /tn KMillAgent /f"; Flags: runhidden; RunOnceId: "DelTask"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=KMillAgent"; Flags: runhidden; RunOnceId: "DelFw"
Filename: "taskkill"; Parameters: "/im kmill-agent.exe /f"; Flags: runhidden; RunOnceId: "KillAgent"

[UninstallDelete]
; Другий автозапуск — ярлик в автозавантаженні (addStartupShortcut).
Type: files; Name: "{userstartup}\KMillAgent.lnk"
