<#
.SYNOPSIS
  Розвідка Sum3D рівня 1 — ЛИШЕ ЧИТАННЯ. Перевіряє, чи відповідає API Sum3dDll_x64.dll
  на ліцензованому ПК, і що саме він повертає по КОПІЇ одного проєкту .cam.
  Запускається ЛИШЕ вручну; KuubMill цей файл сам не виконує.

.DESCRIPTION
  Що робить, по черзі:
    1. Рахує відкриті вікна Sum3D/MillBox. Якщо вони є — працює лише з -AllowRunning; не займає
       останнє вільне місце ліцензії (SUM_MaxSession з GPSettings.ini), якщо не дано -UseLastSeat.
    2. Шукає теки Sum3D, перевіряє 64 біт і наявність потрібних функцій (DLL ще НЕ завантажується).
    3. -DryRun: на цьому зупиняється.
    4. Знімок теки Sum3D (розмір + час кожного файлу), резервна копія *.ini/*.cfg/*.arl/*.dat
       з кореня, експорт гілок реєстру Sum3D, список процесів.
    5. Копіює .cam у тимчасову теку; Sum3D працює лише з копією.
    6. Кожен крок (load, init, machines, strategy, space, curves) — окремий процес з тайм-аутом
       у СВОЇЙ робочій теці: падіння DLL чи вікно «ключ не знайдено» зупиняється, Sum3D не страждає.
    7. Нові знімки: що змінилось у теці Sum3D, у реєстрі, які процеси з'явились, чи ті самі вікна.
    8. Звіт report.json + report.txt у теці виводу.
  Нічого не пише у файли Sum3D сам; рахувати траєкторії й змінювати проєкти не вміє.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sum3d_probe.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\sum3d_probe.ps1 -AllowRunning -SumDir C:\iCAMV5smart -Cam "D:\CAM-work\...\проєкт.cam"
#>
param(
    [string]$SumDir = '',
    [string]$Cam = '',
    [string[]]$Steps = @('load', 'init', 'machines', 'strategy', 'space', 'curves'),
    [int]$TimeoutSec = 90,
    [string]$OutRoot = 'C:\sum3d_probe',
    # Скільки секунд дозволено обходити теку Sum3D для знімка (тека буває велика).
    [int]$SnapshotSec = 60,
    [switch]$DryRun,
    # Дозволити пробу, коли вікна Sum3D відкриті (звичайний стан у цеху).
    [Alias('Force')][switch]$AllowRunning,
    # Дозволити зайняти ОСТАННЄ вільне місце ліцензії (за замовчуванням ні).
    [switch]$UseLastSeat,
    # Запасний варіант, якщо init не проходить зі своєю текою: робоча тека = тека Sum3D.
    [switch]$CwdInSumDir
)

$ErrorActionPreference = 'Stop'
$KitVersion = 3
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir = Join-Path $OutRoot $stamp
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$logPath = Join-Path $outDir 'report.txt'
$report = [ordered]@{ kit_version = $KitVersion; started = (Get-Date).ToString('s'); computer = $env:COMPUTERNAME; dry_run = [bool]$DryRun }
$sumPattern = '^(camwinc|camwin|MillBox)$'
$configPattern = '\.(ini|cfg|cfgc|arl|dat|csv)$'
$regKeys = @('HKCU\Software\LAB', 'HKCU\Software\CIMsystem')

function Log([string]$msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Save-Report {
    $report.finished = (Get-Date).ToString('s')
    ($report | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath (Join-Path $outDir 'report.json') -Encoding UTF8
    Log "Звіт: $outDir"
}

# ---------- PE: розрядність і список експортів без завантаження DLL ----------
function Get-PeInfo([string]$path) {
    $d = [IO.File]::ReadAllBytes($path)
    $pe = [BitConverter]::ToInt32($d, 0x3C)
    $machine = [BitConverter]::ToUInt16($d, $pe + 4)
    $nsec = [BitConverter]::ToUInt16($d, $pe + 6)
    $optSize = [BitConverter]::ToUInt16($d, $pe + 20)
    $opt = $pe + 24
    $magic = [BitConverter]::ToUInt16($d, $opt)
    $dd = $opt + $(if ($magic -eq 0x10B) { 96 } else { 112 })
    $expRva = [BitConverter]::ToUInt32($d, $dd)
    $secs = @()
    for ($i = 0; $i -lt $nsec; $i++) {
        $s = $opt + $optSize + $i * 40
        $secs += , @([BitConverter]::ToUInt32($d, $s + 12), [Math]::Max([BitConverter]::ToUInt32($d, $s + 8), [BitConverter]::ToUInt32($d, $s + 16)), [BitConverter]::ToUInt32($d, $s + 20))
    }
    $toOff = {
        param($rva)
        foreach ($s in $secs) { if ($rva -ge $s[0] -and $rva -lt $s[0] + $s[1]) { return [int]($rva - $s[0] + $s[2]) } }
        return -1
    }
    $names = New-Object System.Collections.Generic.List[string]
    if ($expRva -ne 0) {
        $o = & $toOff $expRva
        $count = [BitConverter]::ToUInt32($d, $o + 24)
        $namesOff = & $toOff ([BitConverter]::ToUInt32($d, $o + 32))
        for ($i = 0; $i -lt $count; $i++) {
            $so = & $toOff ([BitConverter]::ToUInt32($d, $namesOff + 4 * $i))
            $e = $so; while ($d[$e] -ne 0) { $e++ }
            $names.Add([Text.Encoding]::ASCII.GetString($d, $so, $e - $so))
        }
    }
    [pscustomobject]@{ x64 = ($machine -eq 0x8664); exports = $names }
}

# Єдині функції Sum3D, які кличе проба. Усі лише читають.
$needed = [ordered]@{
    setSumDirectoryInst          = '?setSumDirectoryInst@@YAXPEBD@Z'
    initialize                   = '?initialize@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@XZ'
    getMachineNameList           = '?getMachineNameList@@YA_NPEADH@Z'
    getUsedStrategyName          = '?getUsedStrategyName@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBDPEAD@Z'
    getSpaceLeftOnRAW            = '?getSpaceLeftOnRAW@@YAHPEBD@Z'
    getSpaceLeftOnRAW_WithCurves = '?getSpaceLeftOnRAW_WithCurves@@YAHPEBDAEAHAEAPEAXAEAPEAN@Z'
}

# Знімок теки з обмеженням часу: корінь обходиться першим, тож налаштування потрапляють завжди.
function Get-Snapshot([string]$dir, [int]$budgetSec) {
    $files = @{}
    $dirs = New-Object 'System.Collections.Generic.HashSet[string]'
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($dir)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $truncated = $false
    while ($stack.Count -gt 0) {
        if ($sw.Elapsed.TotalSeconds -gt $budgetSec) { $truncated = $true; break }
        $d = $stack.Pop()
        try {
            foreach ($f in [IO.Directory]::GetFiles($d)) {
                $fi = New-Object IO.FileInfo $f
                $files[$f] = '{0}|{1}' -f $fi.Length, $fi.LastWriteTimeUtc.Ticks
            }
            [void]$dirs.Add($d)
            foreach ($s in [IO.Directory]::GetDirectories($d)) { $stack.Push($s) }
        }
        catch { }
    }
    return @{ files = $files; dirs = $dirs; truncated = $truncated }
}

# Експорт гілки реєстру у файл (reg export лише читає реєстр). $true, якщо гілка є.
function Export-Reg([string]$key, [string]$file) {
    & cmd.exe /c "reg export `"$key`" `"$file`" /y >nul 2>&1"
    return (Test-Path -LiteralPath $file)
}

# Копія файлу, який відкрита Sum3D може тримати (LAB_Lang.Ini, проєкт .cam): спільний доступ
# на читання, запис і видалення — не блокуємо Sum3D і не падаємо на «used by another process»
# (18.11.26: проба зупинилась на копії LAB_Lang.Ini ще до першого кроку). $false — не вдалось.
function Copy-Shared([string]$src, [string]$dst) {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $in = $null; $out = $null
    try {
        $in = New-Object IO.FileStream($src, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        $out = New-Object IO.FileStream($dst, [IO.FileMode]::Create, [IO.FileAccess]::Write)
        $in.CopyTo($out)
        return $true
    }
    catch { return $false }
    finally {
        if ($out) { $out.Dispose() }
        if ($in) { $in.Dispose() }
    }
}

function Get-SharedSha256Prefix([string]$path) {
    $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    $in = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash($in)) -replace '-', '').Substring(0, 16)
    }
    finally { $in.Dispose() }
}

function Read-RegSections([string]$file) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $file)) { return $map }
    $cur = ''
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        if ($line.StartsWith('[')) { $cur = $line; $map[$cur] = New-Object System.Collections.Generic.List[string] }
        elseif ($cur -and $line.Trim()) { $map[$cur].Add($line) }
    }
    return $map
}

function Get-SumProcesses {
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $sumPattern })
}

try {
    Log "Розвідка Sum3D, рівень 1 (лише читання), набір v$KitVersion. Тека звіту: $outDir"

    # 1. Які Sum3D/MillBox уже працюють
    $running = Get-SumProcesses
    $runningDirs = @()
    $report.running_before = @($running | ForEach-Object {
            $p = ''
            try { $p = $_.Path } catch { }
            if ($p) { $runningDirs += (Split-Path -Parent $p) }
            "$($_.ProcessName) pid=$($_.Id) $p".Trim()
        })
    Log ("Відкрито Sum3D/MillBox: $($running.Count)")
    if ($running -and -not $AllowRunning -and -not $DryRun) {
        Log 'СТОП: відкриті вікна Sum3D/MillBox. Щоб пробувати поряд з ними — додай -AllowRunning.'
        $report.stopped = 'sum3d running'; Save-Report; exit 2
    }

    # 2. Де Sum3D і чи є потрібні функції (у кожній знайденій теці)
    $candidates = @($SumDir) + $runningDirs + @('C:\iCAMV5smart', 'C:\SUM3D', 'C:\SUM3D\MillBox', 'D:\SUM', 'C:\sum', 'C:\sum\sum') | Where-Object { $_ }
    $seen = @{}
    $found = @()
    $report.candidates = @()
    foreach ($c in $candidates) {
        if (-not (Test-Path -LiteralPath $c)) { continue }
        $dirPath = (Resolve-Path -LiteralPath $c).Path.TrimEnd('\')
        if ($seen.ContainsKey($dirPath.ToLower())) { continue }
        $seen[$dirPath.ToLower()] = $true
        $info = [ordered]@{ dir = $dirPath; has_camwinc = (Test-Path -LiteralPath (Join-Path $dirPath 'camwinc.exe')); has_dll_x64 = $false }
        $p = Join-Path $dirPath 'Sum3dDll_x64.dll'
        if (Test-Path -LiteralPath $p) {
            $info.has_dll_x64 = $true
            $it = Get-Item -LiteralPath $p
            $pei = Get-PeInfo $p
            $info.dll_version = $it.VersionInfo.FileVersion
            $info.dll_modified = $it.LastWriteTime.ToString('s')
            $info.x64 = $pei.x64
            $info.export_count = $pei.exports.Count
            $info.missing_exports = @($needed.GetEnumerator() | Where-Object { $pei.exports -notcontains $_.Value } | ForEach-Object { $_.Key })
            $found += $dirPath
            Log ("Тека $dirPath : x64=$($pei.x64), функцій $($pei.exports.Count), бракує: " + $(if ($info.missing_exports) { $info.missing_exports -join ', ' } else { 'нічого' }))
        }
        elseif ($info.has_camwinc) { Log "Тека $dirPath : є camwinc.exe, але немає Sum3dDll_x64.dll (стара 32-бітна?) — для проби не годиться." }
        else { continue }
        $report.candidates += $info
    }
    $report.sum_dirs_with_dll = $found
    if (-not $found) { Log 'Sum3dDll_x64.dll не знайдено. Вкажи теку: -SumDir <тека Sum3D>.' }

    if ($SumDir) { $SumDir = (Resolve-Path -LiteralPath $SumDir).Path.TrimEnd('\') }
    elseif ($found.Count -eq 1) { $SumDir = $found[0] }
    else {
        if ($found.Count -gt 1) { Log 'Знайдено кілька тек Sum3D — для проби вкажи одну: -SumDir <тека>.' }
        $report.stopped = $(if ($DryRun) { 'dry run' } else { 'sumdir ambiguous' })
        Save-Report; exit $(if ($DryRun) { 0 } else { 2 })
    }
    $dll = Join-Path $SumDir 'Sum3dDll_x64.dll'
    if (-not (Test-Path -LiteralPath $dll)) { Log "Немає Sum3dDll_x64.dll у $SumDir"; $report.stopped = 'no dll'; Save-Report; exit 2 }
    $report.sum_dir = $SumDir
    $item = Get-Item -LiteralPath $dll
    $pe = Get-PeInfo $dll
    $missing = @($needed.GetEnumerator() | Where-Object { $pe.exports -notcontains $_.Value } | ForEach-Object { $_.Key })
    $report.dll = [ordered]@{ size = $item.Length; version = $item.VersionInfo.FileVersion; modified = $item.LastWriteTime.ToString('s'); x64 = $pe.x64; export_count = $pe.exports.Count; missing_exports = $missing }
    $camwinc = Join-Path $SumDir 'camwinc.exe'
    if (Test-Path -LiteralPath $camwinc) { $report.camwinc_version = (Get-Item -LiteralPath $camwinc).VersionInfo.FileVersion }
    $pe.exports | Set-Content -LiteralPath (Join-Path $outDir 'exports.txt') -Encoding UTF8
    Log "Обрано теку: $SumDir"
    if (-not $pe.x64) { Log 'DLL не 64-бітна — ця проба лише для x64.'; $report.stopped = 'not x64'; Save-Report; exit 2 }

    # .cam для проби: у звіт іде лише розмір і початок хешу, не шлях і не назва
    if ($Cam) {
        if (-not (Test-Path -LiteralPath $Cam)) { Log 'Файл з -Cam не знайдено.'; $report.stopped = 'no cam'; Save-Report; exit 2 }
        $report.cam_source_size = (Get-Item -LiteralPath $Cam).Length
        $report.cam_source_sha256_16 = Get-SharedSha256Prefix $Cam
    }
    else { Log 'Без -Cam кроки strategy/space/curves пропускаються.' }

    # Ліміт одночасних сесій Sum3D (GPSettings.ini [NEWTPO_SETTINGS] SUM_MaxSession)
    $maxSession = $null
    $gp = Join-Path $SumDir 'GPSettings.ini'
    if (Test-Path -LiteralPath $gp) {
        $m = Select-String -LiteralPath $gp -Pattern '^\s*SUM_MaxSession\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $maxSession = [int]$m.Matches[0].Groups[1].Value }
    }
    $camRunning = @($running | Where-Object { $_.ProcessName -match '^camwin' }).Count
    $report.license = [ordered]@{ max_session = $maxSession; camwinc_running = $camRunning }
    Log ("Ліміт сесій Sum3D: " + $(if ($null -ne $maxSession) { $maxSession } else { 'у GPSettings.ini не вказано' }) + ", зараз відкрито camwinc: $camRunning")

    if ($DryRun) { Log 'DryRun: DLL не завантажувалась. Готово.'; $report.stopped = 'dry run'; Save-Report; exit 0 }
    if ($missing) { Log 'Потрібних функцій бракує — версія інша. Пробу зупинено.'; $report.stopped = 'missing exports'; Save-Report; exit 2 }

    if ($null -ne $maxSession -and $maxSession -gt 0) {
        $free = $maxSession - $camRunning
        if ($free -le 0) { Log "СТОП: вільних місць ліцензії немає ($camRunning з $maxSession)."; $report.stopped = 'no free seat'; Save-Report; exit 2 }
        if ($free -eq 1 -and -not $UseLastSeat) {
            Log 'СТОП: вільне лише ОСТАННЄ місце ліцензії — оператор не зможе відкрити нове вікно. Закрий одне вікно або додай -UseLastSeat.'
            $report.stopped = 'last seat'; Save-Report; exit 2
        }
    }

    # 4. Знімки ДО: тека, налаштування, реєстр, процеси
    Log "Знімок теки Sum3D (до $SnapshotSec с)…"
    $before = Get-Snapshot $SumDir $SnapshotSec
    $report.snapshot = [ordered]@{ files_before = $before.files.Count; truncated_before = $before.truncated }
    if ($before.truncated) { Log '  знімок неповний (тека велика) — порівнюються лише обійдені підтеки; корінь обійдено.' }
    $backup = Join-Path $outDir 'backup_ini'
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $notCopied = @()
    Get-ChildItem -LiteralPath $SumDir -File -Force | Where-Object { $_.Extension -match '^\.(ini|cfg|arl|dat)$' } |
        ForEach-Object { if (-not (Copy-Shared $_.FullName (Join-Path $backup $_.Name))) { $notCopied += $_.Name } }
    $report.backup_not_copied = $notCopied
    Log "Резервна копія налаштувань: $backup ($(@(Get-ChildItem $backup).Count) файлів)"
    if ($notCopied) { Log "  не скопійовано (зайняті): $($notCopied -join ', ')" }
    $regDir = Join-Path $outDir 'registry'
    New-Item -ItemType Directory -Force -Path $regDir | Out-Null
    $regFound = @()
    for ($i = 0; $i -lt $regKeys.Count; $i++) {
        if (Export-Reg $regKeys[$i] (Join-Path $regDir "before_$i.reg")) { $regFound += $regKeys[$i] }
    }
    Log ("Гілки реєстру Sum3D збережено: " + $(if ($regFound) { $regFound -join '; ' } else { 'не знайдено жодної' }))
    $procBefore = @{}
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object { $procBefore[$_.Id] = $_.ProcessName }

    # 5. Копія .cam
    $workCam = ''
    if ($Cam) {
        $work = Join-Path $outDir 'work'
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $workCam = Join-Path $work 'probe.cam'
        if (-not (Copy-Shared $Cam $workCam)) { Log 'Проєкт .cam не вдалось скопіювати.'; $report.stopped = 'cam copy failed'; Save-Report; exit 2 }
        Log "Копія проєкту: $workCam"
    }

    # 6. Кроки, кожен в окремому процесі. Своя робоча тека: тимчасові файли DLL
    #    не змішуються з тими, що пишуть відкриті вікна Sum3D.
    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $stepScript = Join-Path $here 'sum3d_probe_step.ps1'
    $stepWork = Join-Path $outDir 'cwd'
    New-Item -ItemType Directory -Force -Path $stepWork | Out-Null
    $report.cwd_in_sum_dir = [bool]$CwdInSumDir
    $report.steps = @()
    foreach ($step in $Steps) {
        if (($step -in @('strategy', 'space', 'curves')) -and -not $workCam) { continue }
        Log "Крок $step…"
        $so = Join-Path $outDir "step_$step.out.txt"; $se = Join-Path $outDir "step_$step.err.txt"
        $argList = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$stepScript`"", '-SumDir', "`"$SumDir`"", '-Step', $step)
        if ($workCam) { $argList += @('-Cam', "`"$workCam`"") }
        if (-not $CwdInSumDir) { $argList += @('-WorkDir', "`"$stepWork`"") }
        $t0 = Get-Date
        $proc = Start-Process -FilePath $ps -ArgumentList $argList -PassThru -NoNewWindow -RedirectStandardOutput $so -RedirectStandardError $se
        $null = $proc.Handle   # інакше ExitCode після WaitForExit буває порожній
        $finished = $proc.WaitForExit($TimeoutSec * 1000)
        $entry = [ordered]@{ step = $step; seconds = [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1) }
        if (-not $finished) {
            try { $proc.Kill() } catch { }
            $entry.timeout = $true
            Log "  тайм-аут $TimeoutSec с — процес проби зупинено (можливо, вікно ліцензії чи зависання)."
        }
        else { $entry.exit_code = $proc.ExitCode }
        $raw = (Get-Content -LiteralPath $so -Raw -ErrorAction SilentlyContinue)
        if ($raw) {
            $line = ($raw -split "`r?`n" | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1)
            if ($line) { try { $entry.result = $line | ConvertFrom-Json } catch { $entry.raw = $raw } } else { $entry.raw = $raw }
        }
        $err = (Get-Content -LiteralPath $se -Raw -ErrorAction SilentlyContinue)
        if ($err) { $entry.stderr = $err.Substring(0, [Math]::Min(2000, $err.Length)) }
        $report.steps += $entry
        Log ("  результат: " + $(if ($entry.result) { ($entry.result | ConvertTo-Json -Compress) } elseif ($entry.raw) { $entry.raw.Trim() } else { 'немає виводу' }))
        $baseFailed = (($step -eq 'load' -or $step -eq 'init') -and (-not $entry.result -or -not $entry.result.ok))
        if ($baseFailed) { Log '  Базовий крок не пройшов — далі не йдемо.'; break }
    }

    # 7. Знімки ПІСЛЯ
    $after = Get-Snapshot $SumDir $SnapshotSec
    $report.snapshot.files_after = $after.files.Count
    $report.snapshot.truncated_after = $after.truncated
    $changed = @()
    foreach ($k in $after.files.Keys) {
        $rel = $k.Substring($SumDir.Length)
        if (-not $before.files.ContainsKey($k)) {
            if (-not $before.truncated -or $before.dirs.Contains([IO.Path]::GetDirectoryName($k))) { $changed += "НОВИЙ  $rel" }
        }
        elseif ($before.files[$k] -ne $after.files[$k]) { $changed += "ЗМІНЕНО  $rel" }
    }
    foreach ($k in $before.files.Keys) {
        if (-not $after.files.ContainsKey($k)) {
            if (-not $after.truncated -or $after.dirs.Contains([IO.Path]::GetDirectoryName($k))) { $changed += "ЗНИК  $($k.Substring($SumDir.Length))" }
        }
    }
    $cfgChanged = @($changed | Where-Object { $_ -match $configPattern })
    $otherChanged = @($changed | Where-Object { $_ -notmatch $configPattern })
    $report.sum_dir_changes_config = $cfgChanged
    $report.sum_dir_changes_other = $otherChanged
    if ($running) { Log 'Примітка: вікна Sum3D були відкриті — частину змін могли зробити вони, а не проба.' }
    if ($cfgChanged) { Log "Змінились НАЛАШТУВАННЯ Sum3D ($($cfgChanged.Count)) — копії до проби в backup_ini:"; $cfgChanged | ForEach-Object { Log "  $_" } }
    else { Log 'Налаштування Sum3D (ini/cfg/arl/dat/csv) не змінились.' }
    if ($otherChanged) { Log "Інші зміни в теці Sum3D ($($otherChanged.Count)):"; $otherChanged | Select-Object -First 40 | ForEach-Object { Log "  $_" } }

    # Реєстр: які розділи змінились (значень у звіт не пишемо; файли до/після лежать у registry\)
    $regChanged = @()
    for ($i = 0; $i -lt $regKeys.Count; $i++) {
        $bf = Join-Path $regDir "before_$i.reg"; $af = Join-Path $regDir "after_$i.reg"
        [void](Export-Reg $regKeys[$i] $af)
        $b = Read-RegSections $bf; $a = Read-RegSections $af
        foreach ($sec in @($b.Keys) + @($a.Keys) | Select-Object -Unique) {
            $bv = $(if ($b.ContainsKey($sec)) { $b[$sec] -join "`n" } else { $null })
            $av = $(if ($a.ContainsKey($sec)) { $a[$sec] -join "`n" } else { $null })
            if ($bv -ne $av) { $regChanged += $sec }
        }
    }
    $report.registry = [ordered]@{ keys_found = $regFound; changed_sections = $regChanged }
    if ($regChanged) { Log "Змінились розділи реєстру ($($regChanged.Count)) — стан до проби в registry\before_*.reg:"; $regChanged | Select-Object -First 30 | ForEach-Object { Log "  $_" } }
    else { Log 'Реєстр Sum3D не змінився.' }

    # Процеси: що з'явилось за час проби (можуть бути й сторонні програми)
    $newProc = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { -not $procBefore.ContainsKey($_.Id) } | ForEach-Object { "$($_.ProcessName) pid=$($_.Id)" })
    $report.new_processes = $newProc
    if ($newProc) { Log ("Нові процеси за час проби (не обов'язково від неї): " + ($newProc -join ', ')) }

    # Чи ті самі вікна Sum3D
    $left = Get-SumProcesses
    $report.running_after = @($left | ForEach-Object { "$($_.ProcessName) pid=$($_.Id)" })
    $beforeIds = @($running | ForEach-Object { $_.Id })
    $afterIds = @($left | ForEach-Object { $_.Id })
    $gone = @($beforeIds | Where-Object { $afterIds -notcontains $_ })
    $new = @($afterIds | Where-Object { $beforeIds -notcontains $_ })
    $report.sum3d_closed_during_probe = $gone
    $report.sum3d_started_during_probe = $new
    if ($gone) { Log "УВАГА: під час проби закрились Sum3D pid=$($gone -join ', ') (могли закрити оператори — уточни)." }
    if ($new) { Log "Під час проби відкрились нові Sum3D pid=$($new -join ', ')." }
    if (-not $gone -and -not $new) { Log 'Відкриті вікна Sum3D ті самі, що й до проби.' }

    Save-Report
    exit 0
}
catch {
    Log "ПОМИЛКА: $($_.Exception.Message)"
    $report.error = $_.Exception.Message
    Save-Report
    exit 1
}
