# dev_restart.ps1 — Один крок замість ритуалу «знайди PID → вбий → запусти → curl-poll».
#
# Навіщо: WatchFiles не перезавантажує .py на цьому setup, тож після кожної правки
# Python-коду dev-сервер треба рестартувати вручну. Ця рутина в минулих сесіях
# з'їла 80+ запусків + 36 вбивств + 78 curl — див. SESSIONS_DIAGNOSTICS.md, кластер 1.
#
# Використання:
#   .\dev_restart.ps1                      # рестарт uvicorn app.web на 8002 (проєктна БД)
#   .\dev_restart.ps1 -Launch "C:\...\dev_run.py"   # рестарт через кастомний лаунчер (ізольована dev-БД)
#   .\dev_restart.ps1 -Port 8002
#
# Повертає "UP <code>" або друкує хвіст логу помилки й виходить з кодом 1.

param(
    [int]$Port = 8002,
    [string]$Launch = "",
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$logFile = Join-Path $env:TEMP "orderdesk_dev.log"
$errFile = Join-Path $env:TEMP "orderdesk_dev.err.log"

Write-Host "[1/3] Зупиняю процеси на порту $Port ..."
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    $conns.OwningProcess | Sort-Object -Unique | ForEach-Object {
        try { Stop-Process -Id $_ -Force -ErrorAction Stop; Write-Host "  вбито PID $_" } catch {}
    }
}
# Прибрати reloader-дітей uvicorn/dev_run, що могли лишитись
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*uvicorn*$Port*" -or $_.CommandLine -like "*dev_run*" -or $_.CommandLine -like "*multiprocessing*" } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 2

Write-Host "[2/3] Запускаю сервер ..."
$py = Join-Path $projectRoot ".venv\Scripts\python.exe"
if ($Launch -ne "") {
    Start-Process -FilePath $py -ArgumentList $Launch -RedirectStandardOutput $logFile -RedirectStandardError $errFile -WindowStyle Hidden
} else {
    Start-Process -FilePath $py -ArgumentList @("-m","uvicorn","app.web:app","--reload","--port","$Port","--host","127.0.0.1") `
        -WorkingDirectory $projectRoot -RedirectStandardOutput $logFile -RedirectStandardError $errFile -WindowStyle Hidden
}

Write-Host "[3/3] Чекаю health ..."
for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
    Start-Sleep -Seconds 1
    $code = (curl.exe -s -o NUL -w "%{http_code}" --max-time 3 --noproxy '*' "http://127.0.0.1:$Port/" 2>$null)
    if ($code -and $code -ne "000") {
        Write-Host "UP $code  (лог: $logFile)"
        exit 0
    }
}

Write-Host "НЕ ПІДНЯВСЯ за ${TimeoutSeconds}с. Хвіст логів:"
if (Test-Path $errFile) { Get-Content $errFile -Tail 25 }
if (Test-Path $logFile) { Get-Content $logFile -Tail 10 }
exit 1
