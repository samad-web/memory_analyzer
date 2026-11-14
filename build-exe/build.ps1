<#
.SYNOPSIS
    Build MemoryAnalyzerSetup.exe for the team.

.DESCRIPTION
    Three steps, in order:

      1. pip install -r build-exe/build-requirements.txt into the current
         Python, if PyInstaller is not already there. The install is marked
         --user by default so a shared machine does not need admin.

      2. pyinstaller build-exe\MemoryAnalyzer.spec --clean. Produces
         dist\MemoryAnalyzer\ with MemoryAnalyzer.exe and MemoryBridge.exe
         plus their support files.

      3. ISCC.exe build-exe\MemoryAnalyzerSetup.iss. Produces
         build-exe\Output\MemoryAnalyzerSetup.exe, the single file to hand
         to the team.

    Inno Setup is NOT installed by this script -- it is a one-time
    developer dependency. If missing, the script says where to get it and
    stops after the PyInstaller build (which is still useful on its own).

    Run from the repo root:

      powershell -ExecutionPolicy Bypass -File .\build-exe\build.ps1

.PARAMETER SkipPyInstall
    Skip step 1. Use when you have already set up the build environment.

.PARAMETER PyInstallerOnly
    Stop after step 2. Produces the dist/ tree but no installer.
#>

[CmdletBinding()]
param(
    [switch] $SkipPyInstall,
    [switch] $PyInstallerOnly
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BuildDir = Join-Path $Root 'build-exe'
$Spec = Join-Path $BuildDir 'MemoryAnalyzer.spec'
$IssScript = Join-Path $BuildDir 'MemoryAnalyzerSetup.iss'
$Dist = Join-Path $Root 'dist'
$Build = Join-Path $Root 'build'

function Write-Step { param($Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param($Text) Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn { param($Text) Write-Host "    $Text" -ForegroundColor Yellow }

# -- Step 1: build dependencies ------------------------------------------

if (-not $SkipPyInstall) {
    Write-Step 'Checking build dependencies'
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw 'python.exe not on PATH' }
    & $py -m pip install --user -r (Join-Path $BuildDir 'build-requirements.txt') | Out-Null
    Write-Ok 'PyInstaller + runtime deps installed'
}

# -- Step 2: freeze the two exes -----------------------------------------

Write-Step 'Freezing MemoryAnalyzer.exe and MemoryBridge.exe'

if (Test-Path $Dist)  { Remove-Item -Recurse -Force $Dist }
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }

Push-Location $Root
try {
    & python -m PyInstaller $Spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller exited $LASTEXITCODE" }
} finally {
    Pop-Location
}

$distApp = Join-Path $Dist 'Sirahlyzer'
if (-not (Test-Path (Join-Path $distApp 'Sirahlyzer.exe'))) {
    throw 'Sirahlyzer.exe not produced'
}
if (-not (Test-Path (Join-Path $distApp 'SirahlyzerBridge.exe'))) {
    throw 'SirahlyzerBridge.exe not produced'
}
$sizeMB = (Get-ChildItem $distApp -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Ok ('frozen tree: {0:N1} MB at {1}' -f $sizeMB, $distApp)

if ($PyInstallerOnly) {
    Write-Host "`nDone (PyInstaller only)." -ForegroundColor Green
    exit 0
}

# -- Step 3: wrap it in an Inno Setup installer --------------------------

Write-Step 'Wrapping the installer'
$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Warn 'Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php'
    Write-Warn 'and re-run with -SkipPyInstall to only wrap the existing dist/ tree.'
    exit 1
}

& $iscc $IssScript
if ($LASTEXITCODE -ne 0) { throw "ISCC exited $LASTEXITCODE" }

$installer = Join-Path $BuildDir 'Output\MemoryAnalyzerSetup.exe'
$installerMB = (Get-Item $installer).Length / 1MB
Write-Host ''
Write-Host ('Installer built: {0}' -f $installer) -ForegroundColor Green
Write-Host ('                 {0:N1} MB' -f $installerMB) -ForegroundColor Green
