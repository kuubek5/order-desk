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

$projectOutput = Join-Path $projectRoot "dist-installer"
New-Item -ItemType Directory -Path $projectOutput -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $installerOutput "OrderDesk-Setup-0.1.0.exe") -Destination $projectOutput -Force

Write-Host "Windows installer ready: $projectOutput\OrderDesk-Setup-0.1.0.exe"
