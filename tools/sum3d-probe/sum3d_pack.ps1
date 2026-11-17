<#
.SYNOPSIS
  Пакет налаштувань Sum3D — ЛИШЕ ЧИТАННЯ. Збирає стратегії, архів фрез і словники виробника
  в один zip, щоб розбирати їх на іншому ПК. Запускається ЛИШЕ вручну; KuubMill його не виконує.

.DESCRIPTION
  Що бере (і більше нічого):
    - архіви *.arl і їхні датовані копії (Tools.arl, Tools.arl.250908122720, Machines.arl ...);
    - словники виробника за точною назвою (params_codes.txt, strategies_vocabulary.csv,
      CAMWIN.TAB, Reference *.csv, Strategy_release_notes.pdf);
    - файли .cfg / .cfgc / .txt / .csv з тек стратегій (ST, STRATEGIES, Strategy).
  Чого не бере ніколи: моделі (.stl), проєкти (.cam), програми, ключі й файли ліцензії,
  налаштування .ini, реєстр. Файли Sum3D відкриває лише на читання й не блокує їх, тому
  Sum3D може лишатись відкритою. У мережу нічого не надсилає, нічого не видаляє.
  Пише лише у свою теку виводу (за замовчуванням C:\sum3d_pack\<дата_час>).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sum3d_pack.ps1
  powershell -ExecutionPolicy Bypass -File .\sum3d_pack.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\sum3d_pack.ps1 -SumDir C:\iCAMV5smart
#>
param(
    # Теки Sum3D. Порожньо — усі знайдені зі звичних місць.
    [string[]]$SumDir = @(),
    [string]$OutRoot = 'C:\sum3d_pack',
    # Файл, більший за це, пропускається (і потрапляє у список пропущених).
    [int]$MaxFileMB = 200,
    # Межа на весь пакет до стиснення.
    [int]$MaxTotalMB = 3000,
    # Скільки секунд дозволено обходити одну теку Sum3D.
    [int]$WalkSec = 300,
    # Лише показати, що потрапило б у пакет; zip не створюється.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$PackVersion = 1
$knownDirs = @('C:\iCAMV5smart', 'C:\sum\sum')

# Що дозволено брати. Усе інше не береться.
$archivePattern = '(?i)\.arl(\.[A-Za-z0-9.]+)?$'
$exactNames = @(
    'params_codes.txt', 'strategies_vocabulary.csv', 'CAMWIN.TAB',
    'Reference Strategy Config.csv', 'Reference Tools Parameters.csv', 'Strategy_release_notes.pdf'
)
$strategyDirPattern = '(?i)^(ST|STRATEGIES|Strategy)$'
$strategyFilePattern = '(?i)\.(cfg|cfgc|txt|csv)$'
# Запобіжник поверх білого списку: ці файли не беруться, навіть якщо лежать у теці стратегій.
$neverPattern = '(?i)\.(stl|cam|ply|obj|dcm|xml|constructioninfo|ini|lic|key|dat|exe|dll|iso|nc|zip|rar|7z)$'

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$outDir = Join-Path $OutRoot $stamp
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$logPath = Join-Path $outDir 'pack_log.txt'

function Log([string]$msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-Wanted([string]$root, [string]$path) {
    $name = [IO.Path]::GetFileName($path)
    if ($name -match $neverPattern) { return $false }
    if ($name -match $archivePattern) { return $true }
    foreach ($exact in $exactNames) { if ($name -ieq $exact) { return $true } }
    if ($name -notmatch $strategyFilePattern) { return $false }
    $rel = $path.Substring($root.Length).TrimStart('\')
    $parts = $rel.Split('\')
    for ($i = 0; $i -lt $parts.Length - 1; $i++) {
        if ($parts[$i] -match $strategyDirPattern) { return $true }
    }
    return $false
}

# Обхід теки з обмеженням часу; посилання на інші теки (junction) не розкриваються.
function Find-Files([string]$root, [int]$budgetSec) {
    $found = New-Object 'System.Collections.Generic.List[string]'
    $stack = New-Object 'System.Collections.Generic.Stack[string]'
    $stack.Push($root)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $truncated = $false
    while ($stack.Count -gt 0) {
        if ($sw.Elapsed.TotalSeconds -gt $budgetSec) { $truncated = $true; break }
        $d = $stack.Pop()
        try {
            foreach ($f in [IO.Directory]::GetFiles($d)) {
                if (Test-Wanted $root $f) { $found.Add($f) }
            }
            foreach ($s in [IO.Directory]::GetDirectories($d)) {
                $attr = [IO.File]::GetAttributes($s)
                if (($attr -band [IO.FileAttributes]::ReparsePoint) -eq 0) { $stack.Push($s) }
            }
        }
        catch { }
    }
    return @{ files = $found; truncated = $truncated }
}

function Get-Label([string]$dir) {
    return ($dir.TrimEnd('\') -replace '[:\\/]+', '_').Trim('_')
}

Log "Пакет налаштувань Sum3D, версія $PackVersion. Лише читання."
$roots = @()
if ($SumDir.Count -gt 0) { $candidates = $SumDir } else { $candidates = $knownDirs }
foreach ($c in $candidates) {
    if ($c -and (Test-Path -LiteralPath $c -PathType Container)) { $roots += (Resolve-Path -LiteralPath $c).Path.TrimEnd('\') }
    else { Log "Теки немає: $c" }
}
if ($roots.Count -eq 0) {
    Log 'Жодної теки Sum3D не знайдено. Вкажи її: -SumDir C:\шлях'
    exit 1
}

$maxFile = [int64]$MaxFileMB * 1MB
$maxTotal = [int64]$MaxTotalMB * 1MB
$plan = New-Object 'System.Collections.Generic.List[object]'
$skipped = New-Object 'System.Collections.Generic.List[string]'
$total = [int64]0
foreach ($root in $roots) {
    Log "Шукаю в $root ..."
    $walk = Find-Files $root $WalkSec
    if ($walk.truncated) { Log "УВАГА: обхід $root зупинено за часом ($WalkSec с) - пакет неповний." ; $skipped.Add("$root : обхід зупинено за часом") }
    $label = Get-Label $root
    $count = 0
    foreach ($f in ($walk.files | Sort-Object)) {
        $fi = New-Object IO.FileInfo $f
        $rel = $f.Substring($root.Length).TrimStart('\')
        if ($fi.Length -gt $maxFile) { $skipped.Add("$label\$rel : завеликий ($([math]::Round($fi.Length / 1MB)) МБ)"); continue }
        if ($total + $fi.Length -gt $maxTotal) { $skipped.Add("$label\$rel : перевищено межу пакета $MaxTotalMB МБ"); continue }
        $total += $fi.Length
        $count++
        $plan.Add([pscustomobject]@{ Path = $f; Entry = ($label + '/' + ($rel -replace '\\', '/')); Size = $fi.Length; Time = $fi.LastWriteTime })
    }
    Log "  файлів: $count"
}

$manifest = New-Object 'System.Collections.Generic.List[string]'
$manifest.Add("Пакет налаштувань Sum3D, версія $PackVersion, $stamp")
$manifest.Add('Теки: ' + ($roots -join '; '))
$manifest.Add('розмір_байт' + "`t" + 'змінено' + "`t" + 'файл')
foreach ($p in $plan) {
    $line = "{0}`t{1}`t{2}" -f $p.Size, $p.Time.ToString('yyyy-MM-dd HH:mm:ss'), $p.Entry
    $manifest.Add($line)
}

if ($plan.Count -eq 0) {
    Log 'Нічого не знайдено: у цих теках немає ні архівів фрез, ні стратегій.'
    exit 1
}
Log ("Разом: {0} файлів, {1} МБ до стиснення." -f $plan.Count, [math]::Round($total / 1MB, 1))

if ($DryRun) {
    foreach ($s in $skipped) { Log "Пропущено: $s" }
    [IO.File]::WriteAllLines((Join-Path $outDir 'manifest.txt'), $manifest, (New-Object Text.UTF8Encoding $true))
    Log "DryRun: zip не створювався. Перелік: $outDir\manifest.txt"
    exit 0
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipPath = Join-Path $outDir ("sum3d_pack_$stamp.zip")
$zipStream = New-Object IO.FileStream($zipPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
$zip = New-Object IO.Compression.ZipArchive($zipStream, [IO.Compression.ZipArchiveMode]::Create)
$packed = 0
try {
    foreach ($p in $plan) {
        $in = $null
        try {
            # Лише читання, без блокування: Sum3D може тримати файл відкритим і писати в нього.
            $share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
            $in = New-Object IO.FileStream($p.Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
        }
        catch {
            $skipped.Add("$($p.Entry) : не прочитано ($($_.Exception.Message))")
            continue
        }
        try {
            $entry = $zip.CreateEntry($p.Entry, [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]$p.Time
            $out = $entry.Open()
            try { $in.CopyTo($out) } finally { $out.Dispose() }
            $packed++
        }
        finally { $in.Dispose() }
    }
    if ($skipped.Count -gt 0) {
        $manifest.Add('')
        $manifest.Add('ПРОПУЩЕНО:')
        foreach ($s in $skipped) { $manifest.Add($s) }
    }
    $entry = $zip.CreateEntry('manifest.txt', [IO.Compression.CompressionLevel]::Optimal)
    $writer = New-Object IO.StreamWriter($entry.Open(), (New-Object Text.UTF8Encoding $true))
    try { foreach ($line in $manifest) { $writer.WriteLine($line) } } finally { $writer.Dispose() }
}
finally {
    $zip.Dispose()
    $zipStream.Dispose()
}

[IO.File]::WriteAllLines((Join-Path $outDir 'manifest.txt'), $manifest, (New-Object Text.UTF8Encoding $true))
foreach ($s in $skipped) { Log "Пропущено: $s" }
$zipSize = (New-Object IO.FileInfo $zipPath).Length
Log ("Готово: {0} файлів у пакеті, zip {1} МБ." -f $packed, [math]::Round($zipSize / 1MB, 1))
Log "Архів: $zipPath"
exit 0
