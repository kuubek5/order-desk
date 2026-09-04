# KMill Machine Agent — інсталятор автозапуску (Windows 7/8/10/11).
# Реєструє агента як завдання, що стартує при вході користувача, і запускає
# його зараз. Запускати від адміністратора з теки, де лежать exe + agent.json.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Використовує schtasks (є на всіх Win7-11) заради максимальної сумісності.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$exe  = Join-Path $here "kmill-agent.exe"
$cfg  = Join-Path $here "agent.json"
$task = "KMillAgent"

if (-not (Test-Path $exe)) { throw "Не знайдено $exe — спершу поклади kmill-agent.exe сюди." }
if (-not (Test-Path $cfg)) { throw "Не знайдено $cfg — створи agent.json (див. config.example.json)." }

$conf = Get-Content $cfg -Raw | ConvertFrom-Json
if (-not $conf.token -or $conf.token -like "*ЗАМІНИ*") {
    throw "У agent.json не задано справжній 'token'. Впиши довгий випадковий рядок."
}

# Прибрати старе завдання, якщо було (ідемпотентно).
schtasks /query /tn $task 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { schtasks /delete /tn $task /f | Out-Null }

# Автозапуск при вході + РЕСТАРТ ПРИ ЗБОЇ. Через XML, бо `schtasks` з рядка
# командного не вміє restart-on-failure, а він тут головний: якщо агент упаде
# (паніка, kill, збій, якого не спіймав внутрішній цикл перепідключення),
# планувальник підійме його сам за хвилину — верстат не лишиться «offline» до
# наступного логіну. Плюс StartWhenAvailable: якщо ПК був вимкнений у мить
# тригера, завдання надолужить старт. Схема 1.2 — Task Scheduler 2.0, є на
# Windows 7 і новіших.
$user = "$env:USERDOMAIN\$env:USERNAME"
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$exe</Command>
      <Arguments>-serve</Arguments>
      <WorkingDirectory>$here</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
# Task Scheduler чекає саме UTF-16 (як оголошено в XML). Тимчасовий файл — бо
# schtasks /xml читає з диска.
$xmlPath = Join-Path $env:TEMP "kmill_agent_task.xml"
$xml | Out-File -FilePath $xmlPath -Encoding Unicode
schtasks /create /tn $task /xml $xmlPath /f | Out-Null
Remove-Item $xmlPath -ErrorAction SilentlyContinue
Write-Host "Завдання '$task' зареєстровано (автозапуск при вході + рестарт при збої)."

# Стартувати зараз (не чекати наступного входу).
schtasks /run /tn $task | Out-Null
Start-Sleep -Seconds 2

try {
    $port = ($conf.bind -split ":")[-1]
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 5
    if ($r.Content.Trim() -eq "ok") { Write-Host "Агент запущено й відповідає на порту $port." }
    else { Write-Host "Агент стартував, але /healthz відповів несподівано: $($r.Content)" }
} catch {
    Write-Host "УВАГА: агент не відповів на /healthz — перевір kmill-agent.log поряд з exe."
}

Write-Host ""
Write-Host "Далі: у KMill → Налаштування → Верстати додай машину з адресою"
Write-Host "  http://<IP-цього-ПК>:$port  і тим самим токеном."
