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

# Автозапуск при вході користувача (верстатний ПК працює під одним акаунтом).
# ВАЖЛИВО: `-serve` — без нього exe відкриває меню налаштувань, а не сервер.
schtasks /create /tn $task /tr "`"$exe`" -serve" /sc onlogon /rl highest /f | Out-Null
Write-Host "Завдання '$task' зареєстровано (автозапуск при вході)."

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
