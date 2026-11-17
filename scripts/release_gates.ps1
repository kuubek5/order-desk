<#
.SYNOPSIS
  Ворота релізу KuubMill однією командою — ті самі перевірки, що в CI, у тому ж порядку.

.DESCRIPTION
  Написано після чотирьох релізів, зіпсованих пропущеною перевіркою: 0.13.11 упав на лінтері
  при зелених тестах, 0.21.50 і 0.21.51 пішли з новою помилкою типів (дивились лише на
  Release, а червонів Tests), перед 0.21.117 не було повного прогону.

  Режими:
    pre    ДО підняття версії: чи написано журнал змін і що саме поїде на прод.
    gates  ПІСЛЯ підняття версії, перед тегом: лінтер, типи, версії, журнал, JS, повний прогін.
           Код виходу 0 — лише коли пройшло ВСЕ. З -Quick повний прогін пропускається,
           і код виходу 2: до тега не готово.
    watch  ПІСЛЯ пушу тега: чекає ОБИДВА прогони CI (Tests і Release) і звіряє файли релізу.

  Скрипт нічого не комітить, не ставить тегів і не пушить.

.EXAMPLE
  pwsh scripts/release_gates.ps1 -Mode pre -Prod 0.21.143
  pwsh scripts/release_gates.ps1 -Mode gates
  pwsh scripts/release_gates.ps1 -Mode watch -Tag v0.21.146
#>
param(
    [ValidateSet('pre', 'gates', 'watch', 'changelog')]
    [string]$Mode = 'gates',
    # Версія на проді (kmill_health): від неї рахується «що поїде». Порожньо — від останнього тега.
    [string]$Prod = '',
    [string]$Tag = '',
    # Пропустити повний прогін тестів (лише для проміжної перевірки; до тега не готово).
    [switch]$Quick,
    [int]$WatchMinutes = 30,
    # Для тестів самого скрипта.
    [string]$ChangelogPath = '',
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $repo '.venv\Scripts\python.exe'
$codeRepo = 'kuubek5/order-desk'
$releaseRepo = 'kuubek5/order-desk-releases'
$unreleased = 'Незалежно від версії'
$results = New-Object 'System.Collections.Generic.List[object]'

function Say([string]$msg) { Write-Host $msg }

function Add-Result([string]$name, [bool]$ok, [string]$detail) {
    $results.Add([pscustomobject]@{ Name = $name; Ok = $ok; Detail = $detail })
    $mark = if ($ok) { 'OK    ' } else { 'ПРОВАЛ' }
    Say ("[{0}] {1}{2}" -f $mark, $name, $(if ($detail) { " - $detail" } else { '' }))
}

# Зовнішня команда: вивід рядками + код виходу. stderr зливається у вивід.
function Invoke-Tool([string]$exe, [string[]]$arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @arguments 2>&1 | ForEach-Object { "$_" }
        return @{ Code = $LASTEXITCODE; Lines = @($out) }
    }
    finally { $ErrorActionPreference = $prev }
}

function Get-AppVersion {
    if ($Version) { return $Version }
    $text = [IO.File]::ReadAllText((Join-Path $repo 'app\__version__.py'), [Text.Encoding]::UTF8)
    $m = [regex]::Match($text, '(?m)^VERSION\s*=\s*"([^"]+)"')
    if (-not $m.Success) { throw 'Не знайшов VERSION у app/__version__.py' }
    return $m.Groups[1].Value
}

# Скільки пунктів («- …») у розділі журналу з таким заголовком. -1 — розділу немає.
function Get-ChangelogItems([string]$title) {
    $path = if ($ChangelogPath) { $ChangelogPath } else { Join-Path $repo 'CHANGELOG.md' }
    $lines = [IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8)
    $inside = $false
    $count = -1
    foreach ($line in $lines) {
        if ($line -match '^##\s*\[([^\]]+)\]') {
            if ($inside) { break }
            if ($Matches[1] -eq $title) { $inside = $true; $count = 0 }
            continue
        }
        if ($inside -and $line -match '^\s*-\s+\S') { $count++ }
    }
    return $count
}

function Get-BaseTag {
    if ($Prod) { return 'v' + $Prod.TrimStart('v') }
    $r = Invoke-Tool 'git' @('-C', $repo, 'describe', '--tags', '--abbrev=0', '--match', 'v*')
    if ($r.Code -ne 0) { throw 'Не знайшов жодного тега v*' }
    return $r.Lines[0].Trim()
}

function Show-WhatShips {
    $base = Get-BaseTag
    $r = Invoke-Tool 'git' @('-C', $repo, 'log', '--oneline', "$base..HEAD")
    if ($r.Code -ne 0) { Add-Result "Що поїде від $base" $false 'git log не вдався (такого тега немає?)'; return }
    Say "Що поїде на прод від $base (комітів: $($r.Lines.Count)):"
    foreach ($l in $r.Lines) { Say "    $l" }
    $body = Invoke-Tool 'git' @('-C', $repo, 'log', '--format=%h %B', "$base..HEAD")
    $unverified = @($body.Lines | Where-Object { $_ -match 'НЕ ПЕРЕВІРЕНО' })
    if ($unverified.Count -gt 0) {
        Add-Result 'Неперевірене в комітах' $false ("у повідомленнях комітів є «НЕ ПЕРЕВІРЕНО» ({0}) - перевірити або назвати власнику" -f $unverified.Count)
    }
    else { Add-Result 'Неперевірене в комітах' $true '' }
}

function Test-Ruff {
    $files = (Invoke-Tool 'git' @('-C', $repo, 'ls-files', '*.py')).Lines | Where-Object { $_ }
    $bad = New-Object 'System.Collections.Generic.List[string]'
    # Лише файли репозиторію - як у CI на чистому checkout; чернетки поза git не рахуються.
    for ($i = 0; $i -lt $files.Count; $i += 150) {
        $chunk = @($files[$i..([Math]::Min($i + 149, $files.Count - 1))])
        $r = Invoke-Tool $python (@('-m', 'ruff', 'check', '--quiet') + $chunk)
        if ($r.Code -ne 0) { foreach ($l in $r.Lines) { $bad.Add($l) } }
    }
    if ($bad.Count -gt 0) { $bad | Select-Object -First 30 | ForEach-Object { Say "    $_" } }
    Add-Result 'Лінтер (ruff)' ($bad.Count -eq 0) $(if ($bad.Count) { "зауваг: див. вище" } else { "$($files.Count) файлів" })
}

function Test-Mypy {
    $r = Invoke-Tool $python @('-m', 'mypy', 'app', '--ignore-missing-imports')
    $found = $null
    foreach ($l in $r.Lines) { if ($l -match 'Found (\d+) error') { $found = [int]$Matches[1] } }
    if ($null -eq $found) {
        if ($r.Code -ne 0) { Add-Result 'Типи (mypy)' $false "mypy не дав числа й завершився з кодом $($r.Code)"; return }
        $found = 0
    }
    $baseline = [int](([IO.File]::ReadAllText((Join-Path $repo '.mypy-baseline'))).Trim())
    if ($found -gt $baseline) {
        $r.Lines | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
        Add-Result 'Типи (mypy)' $false "помилок $found, планка $baseline"
    }
    elseif ($found -lt $baseline) { Add-Result 'Типи (mypy)' $true "помилок $found, планка $baseline - оновити .mypy-baseline" }
    else { Add-Result 'Типи (mypy)' $true "помилок $found = планка" }
}

function Test-Changelog([string]$title, [string]$label) {
    $n = Get-ChangelogItems $title
    if ($n -lt 0) { Add-Result $label $false "розділу [$title] у CHANGELOG.md немає" }
    elseif ($n -eq 0) { Add-Result $label $false "розділ [$title] порожній" }
    else { Add-Result $label $true "пунктів: $n" }
}

function Test-Js {
    $base = Get-BaseTag
    $r = Invoke-Tool 'git' @('-C', $repo, 'diff', '--name-only', "$base..HEAD", '--', '*.js')
    $work = Invoke-Tool 'git' @('-C', $repo, 'diff', '--name-only', 'HEAD', '--', '*.js')
    $files = @(($r.Lines + $work.Lines) | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $repo $_)) } | Sort-Object -Unique)
    if ($files.Count -eq 0) { Add-Result 'JS (node --check)' $true 'змінених файлів немає'; return }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Add-Result 'JS (node --check)' $false "node не знайдено, а змінено $($files.Count) файлів"; return }
    $bad = 0
    foreach ($f in $files) {
        $c = Invoke-Tool 'node' @('--check', (Join-Path $repo $f))
        if ($c.Code -ne 0) { $bad++; Say "    $f"; $c.Lines | Select-Object -First 5 | ForEach-Object { Say "      $_" } }
    }
    Add-Result 'JS (node --check)' ($bad -eq 0) "файлів: $($files.Count), з помилкою: $bad"
}

function Test-Pytest([string]$label, [string[]]$arguments) {
    $r = Invoke-Tool $python (@('-X', 'utf8', '-m', 'pytest', '-q', '-p', 'no:cacheprovider') + $arguments)
    $tail = @($r.Lines | Where-Object { $_ -match '\d+ (passed|failed|error)' } | Select-Object -Last 1)
    if ($r.Code -ne 0) { $r.Lines | Select-Object -Last 25 | ForEach-Object { Say "    $_" } }
    Add-Result $label ($r.Code -eq 0) $(if ($tail) { $tail[0].Trim('= ') } else { "код $($r.Code)" })
}

function Finish([int]$notReadyCode) {
    $failed = @($results | Where-Object { -not $_.Ok })
    Say ''
    if ($failed.Count -gt 0) {
        Say ("ПІДСУМОК: ЧЕРВОНЕ ({0}): {1}" -f $failed.Count, (($failed | ForEach-Object { $_.Name }) -join '; '))
        exit 1
    }
    if ($notReadyCode -ne 0) { Say 'ПІДСУМОК: пройдене зелене, але ДО ТЕГА НЕ ГОТОВО - повний прогін пропущено (-Quick).'; exit $notReadyCode }
    Say 'ПІДСУМОК: усе зелене.'
    exit 0
}

Set-Location $repo

if ($Mode -eq 'changelog') {
    Test-Changelog (Get-AppVersion) 'Журнал змін версії'
    Finish 0
}

if ($Mode -eq 'pre') {
    Say "Перед підняттям версії (зараз $(Get-AppVersion))."
    Show-WhatShips
    Test-Changelog $unreleased 'Журнал змін написано'
    Finish 0
}

if ($Mode -eq 'gates') {
    $ver = Get-AppVersion
    Say "Ворота релізу $ver."
    Show-WhatShips
    $tagged = (Invoke-Tool 'git' @('-C', $repo, 'tag', '-l', "v$ver")).Lines | Where-Object { $_ }
    $head = (Invoke-Tool 'git' @('-C', $repo, 'rev-parse', 'HEAD')).Lines[0]
    if ($tagged) {
        $tagSha = (Invoke-Tool 'git' @('-C', $repo, 'rev-list', '-n', '1', "v$ver")).Lines[0]
        Add-Result 'Версію піднято' ($tagSha -eq $head) "тег v$ver уже є$(if ($tagSha -ne $head) { ' і стоїть НЕ на HEAD - версію не піднято' })"
    }
    else { Add-Result 'Версію піднято' $true "тега v$ver ще немає" }
    $dirty = @((Invoke-Tool 'git' @('-C', $repo, 'status', '--porcelain', '--untracked-files=no')).Lines | Where-Object { $_ -and $_ -notmatch '\.claude/launch\.json$' })
    Add-Result 'Усе закомічено' ($dirty.Count -eq 0) $(if ($dirty.Count) { "незакомічених файлів: $($dirty.Count) - у тег вони не потраплять" } else { '' })
    # Новий файл, не доданий у git, не бачить ні лінтер (він іде по файлах репозиторію), ні тег.
    $loose = @((Invoke-Tool 'git' @('-C', $repo, 'status', '--porcelain', '--untracked-files=all')).Lines | Where-Object { $_ -match '^\?\? "?(app|tests|scripts|tools|migrations|agent|installer)/' -and $_ -notmatch '__pycache__' })
    Add-Result 'Нові файли додані в git' ($loose.Count -eq 0) $(if ($loose.Count) { "не додано: $($loose.Count), перший: $($loose[0].Substring(3))" } else { '' })
    Test-Changelog $ver 'Журнал змін версії'
    Test-Ruff
    Test-Mypy
    Test-Pytest 'Версії збігаються' @('tests/test_version_sync.py')
    Test-Js
    if ($Quick) { Finish 2 }
    Test-Pytest 'Повний прогін тестів' @('-n', 'auto', '--dist', 'loadfile')
    Finish 0
}

if ($Mode -eq 'watch') {
    if (-not $Tag) { $Tag = 'v' + (Get-AppVersion) }
    $ver = $Tag.TrimStart('v')
    $sha = (Invoke-Tool 'git' @('-C', $repo, 'rev-list', '-n', '1', $Tag)).Lines[0]
    Say "Чекаю CI для $Tag ($($sha.Substring(0, 7))): Tests і Release."
    $deadline = (Get-Date).AddMinutes($WatchMinutes)
    $runs = @()
    while ($true) {
        $r = Invoke-Tool 'gh' @('run', 'list', '--repo', $codeRepo, '--commit', $sha, '--limit', '20', '--json', 'workflowName,status,conclusion,headBranch,databaseId')
        if ($r.Code -eq 0) { $runs = @(($r.Lines -join "`n") | ConvertFrom-Json) }
        $names = @($runs | ForEach-Object { $_.workflowName })
        $pending = @($runs | Where-Object { $_.status -ne 'completed' })
        if (($names -contains 'Tests') -and ($names -contains 'Release') -and $pending.Count -eq 0) { break }
        if ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Seconds 30
    }
    foreach ($need in @('Tests', 'Release')) {
        $mine = @($runs | Where-Object { $_.workflowName -eq $need })
        if ($mine.Count -eq 0) { Add-Result "CI: $need" $false 'прогону немає'; continue }
        $bad = @($mine | Where-Object { $_.status -ne 'completed' -or $_.conclusion -ne 'success' })
        Add-Result "CI: $need" ($bad.Count -eq 0) (($mine | ForEach-Object { "$($_.headBranch): $($_.status)/$($_.conclusion)" }) -join '; ')
    }
    foreach ($other in @($runs | Where-Object { $_.workflowName -notin @('Tests', 'Release') })) {
        Add-Result "CI: $($other.workflowName)" ($other.conclusion -eq 'success') "$($other.status)/$($other.conclusion)"
    }
    $a = Invoke-Tool 'gh' @('release', 'view', $Tag, '--repo', $releaseRepo, '--json', 'assets', '--jq', '.assets[].name')
    $want = @("KuubMill-Setup-$ver.exe", "KuubMill-Setup-$ver.exe.sha256")
    $missing = @($want | Where-Object { $a.Lines -notcontains $_ })
    Add-Result 'Файли релізу' ($a.Code -eq 0 -and $missing.Count -eq 0) $(if ($missing.Count) { "немає: $($missing -join ', ')" } else { $want -join ', ' })
    Finish 0
}
