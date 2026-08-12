$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$buildRoot = Join-Path $env:LOCALAPPDATA "OrderDeskBuild"
$buildVenv = Join-Path $buildRoot "venv"
$buildPython = Join-Path $buildVenv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $buildPython)) {
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    python -m venv $buildVenv
}

& $buildPython -m pip install --disable-pip-version-check -r requirements-build.txt
& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --workpath (Join-Path $buildRoot "work") `
    --distpath (Join-Path $buildRoot "dist") `
    OrderDesk.spec

$innoCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
)
$iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 не знайдено. Встановіть JRSoftware.InnoSetup через winget."
}

$installerOutput = Join-Path $buildRoot "installer"
New-Item -ItemType Directory -Path $installerOutput -Force | Out-Null
& $iscc `
    "/DBuildRoot=$(Join-Path $buildRoot 'dist\OrderDesk')" `
    "/DOutputRoot=$installerOutput" `
    (Join-Path $projectRoot "installer\OrderDesk.iss")

# Version comes from the single source of truth (app/__version__.py) so the
# installer filename never drifts from the code version — the exact drift that
# broke the v0.1.1 release build when this was hardcoded to 0.1.0.
$versionLine = Select-String -Path (Join-Path $projectRoot "app\__version__.py") -Pattern 'VERSION\s*=\s*"([^"]+)"'
$version = $versionLine.Matches[0].Groups[1].Value
$installerName = "OrderDesk-Setup-$version.exe"

$projectOutput = Join-Path $projectRoot "dist-installer"
New-Item -ItemType Directory -Path $projectOutput -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $installerOutput $installerName) -Destination $projectOutput -Force

Write-Host "Windows installer ready: $projectOutput\$installerName"
