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

  Рівень 2 (-Place, набір v4): ставить ОДИН STL на КОПІЮ диска .blk (importSTL → .obj →
  MB2Sum_MBobj2SUMcam → .cam) і лишає нові файли лише у своїй теці work. Оригінали STL і диска
  лише копіюються. Відомий слід: importSTL сама дописує importstl.log у теку Sum3D.

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
    [switch]$CwdInSumDir,
    # Рівень 2 (набір v4): поставити ОДИН STL на КОПІЮ диска .blk → .obj → .cam. Усе пишеться
    # лише в теку проби (work); оригінали STL і диска лише копіюються. Відкрити результат у
    # Sum3D і подивитись, де стала робота, — людина, руками.
    [switch]$Place,
    [string]$Stl = '',
    [string]$Blk = '',
    [string]$Machine = '',
    [string]$Material = '',
    [string]$Equipment = '',
    [double]$Shrink = 1.25,
    [int]$ObjectType = 9,
    # Друга копія STL, зсунута по X на стільки мм: чи переносить Sum3D роботу разом із координатами.
    [double]$ShiftX = 20
)

$ErrorActionPreference = 'Stop'
$KitVersion = 4
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

# Єдині функції Sum3D, які кличе проба. Рівень 1 — лише читають.
$needed = [ordered]@{
    setSumDirectoryInst          = '?setSumDirectoryInst@@YAXPEBD@Z'
    initialize                   = '?initialize@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@XZ'
    getMachineNameList           = '?getMachineNameList@@YA_NPEADH@Z'
    getUsedStrategyName          = '?getUsedStrategyName@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBDPEAD@Z'
    getSpaceLeftOnRAW            = '?getSpaceLeftOnRAW@@YAHPEBD@Z'
    getSpaceLeftOnRAW_WithCurves = '?getSpaceLeftOnRAW_WithCurves@@YAHPEBDAEAHAEAPEAXAEAPEAN@Z'
}
# Рівень 2 (-Place): список обладнання (читає) і два виклики, що пишуть — лише нові файли в
# теці проби (перевіряє й сам крок).
$neededPlace = [ordered]@{
    getEquipmentList = '?getEquipmentList@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEB_WPEADAEAH@Z'
    importSTL    = '?importSTL@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBD000HN_NNHNH11N11100@Z'
    MBobj2SUMcam = '?MB2Sum_MBobj2SUMcam@@YA_NPEBDPEAD@Z'
}
if ($Place) { foreach ($k in $neededPlace.Keys) { $needed[$k] = $neededPlace[$k] } }

# Копія STL, зсунута по X (двійковий або текстовий STL). $false — формат не впізнано.
function Write-ShiftedStl([string]$src, [string]$dst, [double]$dx) {
    $d = [IO.File]::ReadAllBytes($src)
    if ($d.Length -ge 84) {
        $n = [BitConverter]::ToUInt32($d, 80)
        if ($n -gt 0 -and 84 + $n * 50 -eq $d.Length) {
            for ($i = 0; $i -lt $n; $i++) {
                for ($v = 0; $v -lt 3; $v++) {
                    $o = 84 + $i * 50 + 12 + $v * 12
                    $x = [BitConverter]::ToSingle($d, $o) + $dx
                    [Array]::Copy([BitConverter]::GetBytes([single]$x), 0, $d, $o, 4)
                }
            }
            [IO.File]::WriteAllBytes($dst, $d)
            return $true
        }
    }
    $text = [Text.Encoding]::ASCII.GetString($d)
    if (-not $text.TrimStart().ToLower().StartsWith('solid')) { return $false }
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $out = [regex]::Replace($text, 'vertex\s+(\S+)', { param($m) 'vertex ' + ([double]::Parse($m.Groups[1].Value, $inv) + $dx).ToString('R', $inv) })
    [IO.File]::WriteAllText($dst, $out, [Text.Encoding]::ASCII)
    return $true
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
    $levelText = $(if ($Place) { 'рівень 2 (постановка на копію диска; пише лише у свою теку)' } else { 'рівень 1 (лише читання)' })
    Log "Розвідка Sum3D, $levelText, набір v$KitVersion. Тека звіту: $outDir"

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

    # 3a. Рівень 2: що ставимо й на який диск. Назва STL у звіт не йде (імена пацієнтів) —
    #     лише розмір і початок хешу; назва диска — так (це артикул, не людина).
    if ($Place) {
        if (-not $Stl) { $Stl = (Read-Host 'STL роботи (повний шлях; можна перетягнути файл у це вікно)').Trim('"', ' ') }
        if (-not $Blk) { $Blk = (Read-Host 'Диск .blk (повний шлях, напр. тека дисків\14\zr14_14-Monolith-A3-x101.blk)').Trim('"', ' ') }
        if (-not $Machine) { $Machine = (Read-Host 'Верстат як у списку Sum3D; досить частини назви (напр. Boris або Tolik)').Trim() }
        if (-not $Material) {
            $Material = (Read-Host 'Матеріал як у Sum3D; кілька варіантів через | (Enter = ZR|Zirconium)').Trim()
            if (-not $Material) { $Material = 'ZR|Zirconium' }
        }
        if (-not $PSBoundParameters.ContainsKey('Shrink')) {
            $answer = (Read-Host 'Усадка диска (Scaling Factor у вікні вибору диска, напр. 1.242). Enter = 1.25').Trim().Replace(',', '.')
            $parsed = 0.0
            if ($answer -and [double]::TryParse($answer, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -and $parsed -gt 0) { $Shrink = $parsed }
        }
        foreach ($pair in @(@('STL', $Stl), @('Диск', $Blk))) {
            if (-not $pair[1] -or -not (Test-Path -LiteralPath $pair[1])) { Log "$($pair[0]): файл не знайдено."; $report.stopped = 'place input missing'; Save-Report; exit 2 }
        }
        if (-not $Machine -or -not $Material) { Log 'Потрібні верстат і матеріал.'; $report.stopped = 'place input missing'; Save-Report; exit 2 }
        $report.place = [ordered]@{
            stl_size = (Get-Item -LiteralPath $Stl).Length; stl_sha256_16 = Get-SharedSha256Prefix $Stl
            blk = [IO.Path]::GetFileName($Blk); blk_size = (Get-Item -LiteralPath $Blk).Length
            machine = $Machine; material = $Material; material_used = ''; equipment = $Equipment
            shrink = $Shrink; object_type = $ObjectType; shift_x = $ShiftX
        }
        Log "Рівень 2: STL на копію диска $([IO.Path]::GetFileName($Blk)), верстат $Machine, матеріал $Material, усадка $Shrink."
        # Відомий слід (розбір DLL 22.11.26): importSTL сама дописує свій журнал importstl.log у теку,
        # де лежить Sum3dDll_x64.dll. Наявних файлів Sum3D це не змінює; набір файл не прибирає.
        Log "УВАГА: Sum3D створить або допише власний журнал $SumDir\importstl.log (текст: верстат, матеріал, шляхи копій). Інших змін у теці Sum3D не очікується — звіт покаже всі."
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
    $variants = @()
    if ($Place) {
        $work = Join-Path $outDir 'work'
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $workBlk = Join-Path $work ([IO.Path]::GetFileName($Blk))
        $workStl = Join-Path $work 'probe.stl'
        if (-not (Copy-Shared $Blk $workBlk) -or -not (Copy-Shared $Stl $workStl)) {
            Log 'STL чи диск не вдалось скопіювати.'; $report.stopped = 'place copy failed'; Save-Report; exit 2
        }
        $variants += , @('probe', $workStl)
        if ($ShiftX -ne 0) {
            $shifted = Join-Path $work 'probe_shift.stl'
            if (Write-ShiftedStl $workStl $shifted $ShiftX) { $variants += , @('probe_shift', $shifted) }
            else { Log 'Формат STL не впізнано — зсунуту копію пропущено.' }
        }
        Log "Копії для рівня 2: $work"
    }

    # 6. Кроки, кожен в окремому процесі. Своя робоча тека: тимчасові файли DLL
    #    не змішуються з тими, що пишуть відкриті вікна Sum3D.
    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $stepScript = Join-Path $here 'sum3d_probe_step.ps1'
    $stepWork = Join-Path $outDir 'cwd'
    New-Item -ItemType Directory -Force -Path $stepWork | Out-Null
    $report.cwd_in_sum_dir = [bool]$CwdInSumDir
    $report.steps = @()
    # Один крок в окремому процесі. $label — назва у звіті (у рівні 2 той самий крок іде двічі).
    function Invoke-ProbeStep([string]$step, [string]$label, [string]$cam, [string[]]$extra) {
        Log "Крок $label…"
        $so = Join-Path $outDir "step_$label.out.txt"; $se = Join-Path $outDir "step_$label.err.txt"
        $argList = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$stepScript`"", '-SumDir', "`"$SumDir`"", '-Step', $step)
        if ($cam) { $argList += @('-Cam', "`"$cam`"") }
        if (-not $CwdInSumDir) { $argList += @('-WorkDir', "`"$stepWork`"") }
        if ($extra) { $argList += $extra }
        $t0 = Get-Date
        $proc = Start-Process -FilePath $ps -ArgumentList $argList -PassThru -NoNewWindow -RedirectStandardOutput $so -RedirectStandardError $se
        $null = $proc.Handle   # інакше ExitCode після WaitForExit буває порожній
        $finished = $proc.WaitForExit($TimeoutSec * 1000)
        $entry = [ordered]@{ step = $label; seconds = [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1) }
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
        $script:report.steps += $entry
        Log ("  результат: " + $(if ($entry.result) { ($entry.result | ConvertTo-Json -Compress) } elseif ($entry.raw) { $entry.raw.Trim() } else { 'немає виводу' }))
        return $entry
    }
    function Test-StepOk($entry) { return [bool]($entry.result -and $entry.result.ok) }

    $baseOk = $true
    # Назви матеріалу, які пробуємо по черзі (яку з них чекає importSTL — з'ясовує сама проба).
    $materials = @($Material -split '\|' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($step in $(if ($Place) { @('load', 'init', 'machines', 'equipment') } else { $Steps })) {
        if (($step -in @('strategy', 'space', 'curves')) -and -not $workCam) { continue }
        $extra = @()
        if ($step -eq 'equipment') { $extra = @('-Machine', "`"$Machine`"") }
        $entry = Invoke-ProbeStep $step $step $workCam $extra
        $baseFailed = (($step -eq 'load' -or $step -eq 'init') -and -not (Test-StepOk $entry))
        if ($baseFailed) { Log '  Базовий крок не пройшов — далі не йдемо.'; $baseOk = $false; break }
        if ($Place -and $step -eq 'machines' -and (Test-StepOk $entry)) {
            # Верстат: точна назва зі списку Sum3D або єдина, що містить введене.
            $all = @($entry.result.machines_raw -split '\|' | Where-Object { $_ })
            $hit = @($all | Where-Object { $_ -ieq $Machine })
            if (-not $hit) { $hit = @($all | Where-Object { $_.ToLower().Contains($Machine.ToLower()) }) }
            if ($hit.Count -ne 1) {
                Log ("  СТОП: верстат «$Machine» у списку Sum3D " + $(if ($hit) { "підходить до кількох: $($hit -join '; ')" } else { 'не знайдено' }) + '. Повний список — у звіті, крок machines.')
                $report.stopped = 'place machine not found'; $baseOk = $false; break
            }
            $Machine = $hit[0]
            $report.place.machine = $Machine
            Log "  верстат для імпорту: $Machine"
        }
        if ($step -eq 'equipment' -and -not $Equipment -and (Test-StepOk $entry)) {
            # Перше обладнання з позначкою default: «default|<назва>|…»
            $parts = @($entry.result.equipment_raw -split '\|')
            $i = [Array]::IndexOf($parts, 'default')
            if ($i -ge 0 -and $i + 1 -lt $parts.Count) { $Equipment = $parts[$i + 1] }
            $report.place.equipment = $Equipment
            Log "  обладнання для імпорту: $(if ($Equipment) { $Equipment } else { 'не визначено (задай -Equipment)' })"
        }
    }

    # Рівень 2: кожна копія STL → .obj → .cam у теці проби, потім читаємо результат (curves).
    if ($Place -and $baseOk) {
        foreach ($v in $variants) {
            $outCam = Join-Path $work "$($v[0]).cam"
            $imp = $null; $obj = ''
            for ($m = 0; $m -lt $materials.Count; $m++) {
                # Кожна спроба — свій .obj: крок не пише поверх наявного файла.
                $suffix = $(if ($m -eq 0) { '' } else { "_m$($m + 1)" })
                $obj = Join-Path $work "$($v[0])$suffix.obj"
                $common = @('-Machine', "`"$Machine`"", '-Material', "`"$($materials[$m])`"", '-Equipment', "`"$Equipment`"",
                    '-Blk', "`"$workBlk`"", '-Stl', "`"$($v[1])`"", '-Obj', "`"$obj`"",
                    '-ObjectType', $ObjectType, '-Shrink', $Shrink.ToString([Globalization.CultureInfo]::InvariantCulture))
                $imp = Invoke-ProbeStep 'import' "import_$($v[0])$suffix" '' $common
                if (Test-StepOk $imp) {
                    # Назва спрацювала — далі лише вона.
                    $report.place.material_used = $materials[$m]
                    $materials = @($materials[$m])
                    break
                }
            }
            if (-not (Test-StepOk $imp)) { continue }
            $toCam = Invoke-ProbeStep 'tocam' "tocam_$($v[0])" $outCam @('-Obj', "`"$obj`"")
            if (Test-StepOk $toCam) { [void](Invoke-ProbeStep 'curves' "curves_$($v[0])" $outCam @()) }
        }
        Log "Готові проєкти (якщо вийшли): $work\*.cam — відкрий у Sum3D через Файл → Відкрити й подивись, де стала робота."
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
