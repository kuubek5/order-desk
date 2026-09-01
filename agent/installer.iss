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

[Files]
Source: "kmill-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\KMill Agent — налаштування"; Filename: "{app}\kmill-agent.exe"; Parameters: "-setup"
Name: "{group}\Видалити KMill Agent"; Filename: "{uninstallexe}"

[Run]
; Одразу відкрити меню налаштувань після встановлення (інсталятор уже з правами
; адміна, тому агент зареєструє автозапуск і брандмауер без повторного UAC).
Filename: "{app}\kmill-agent.exe"; Parameters: "-setup"; Description: "Відкрити налаштування агента"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Прибрати автозапуск і правило брандмауера, які створив агент.
Filename: "schtasks"; Parameters: "/delete /tn KMillAgent /f"; Flags: runhidden; RunOnceId: "DelTask"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=KMillAgent"; Flags: runhidden; RunOnceId: "DelFw"
Filename: "taskkill"; Parameters: "/im kmill-agent.exe /f"; Flags: runhidden; RunOnceId: "KillAgent"
