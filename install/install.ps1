<#
.SYNOPSIS
    Installs the agent monitor on this laptop: hooks, sync, and optionally the tile.

.DESCRIPTION
    Four things happen, in this order, and each is skipped cleanly if it is
    already done so re-running is safe:

      1. the agent files are copied to %LOCALAPPDATA%\SystemMonitorWidget\agent
      2. requirements.txt is pip-installed for the current user
      3. team.json is written, which is what switches reporting on
      4. the six Claude Code hooks are registered, and a scheduled task is
         created to upload the counts (reporter) or run the tile at logon (full)

    Deliberately does NOT need administrator rights in reporter mode. Elevation
    on this project only ever buys fan speed and CPU temperature from the MSI
    embedded controller, which a reporter install has no use for -- and asking a
    colleague for admin to install telemetry is a good way to be told no.

    Settings are merged, never overwritten: hooks.py amends Claude Code's
    settings.json in place and leaves any hooks belonging to anything else
    alone, after backing the file up.

.PARAMETER Url
    The Supabase project URL, e.g. https://abcd.supabase.co. Taken from the
    team.json beside the bundle when omitted.

.PARAMETER AnonKey
    The project's anon key. Write-only by the policies in schema.sql.

.PARAMETER TeamId
    Which team's board this laptop reports to.

.PARAMETER Member
    How this laptop appears on the board. Defaults to the Windows user name.

.PARAMETER Mode
    reporter (default) installs hooks and sync only -- nothing on screen.
    full also installs the tile and the dashboard, and starts it at logon.

.PARAMETER Uninstall
    Remove the hooks, the scheduled task and team.json, and stop reporting.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install\install.ps1 `
      -Url "https://abcd.supabase.co" -AnonKey "eyJ..." -TeamId "acme" -Mode full
#>

[CmdletBinding()]
param(
    [string] $Url,
    [string] $AnonKey,
    [string] $TeamId,
    [string] $Member,
    [ValidateSet('reporter', 'full')]
    [string] $Mode = 'reporter',
    [bool]   $SharePrompts  = $true,
    [bool]   $ShareHealth   = $true,
    [bool]   $ShareSessions = $true,
    [int]    $SyncSeconds   = 60,
    [string] $PythonExe,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$SourceDir  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$StateDir   = Join-Path $env:LOCALAPPDATA 'SystemMonitorWidget'
$InstallDir = Join-Path $StateDir 'agent'
$TeamFile   = Join-Path $StateDir 'team.json'
$SyncTask   = 'AgentMonitorSync'
$TileTask   = 'SystemMonitorWidget'

function Write-Step { param($Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param($Text) Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn { param($Text) Write-Host "    $Text" -ForegroundColor Yellow }

# -- python ------------------------------------------------------------------

function Resolve-Python {
    if ($PythonExe) {
        if (-not (Test-Path $PythonExe)) { throw "No such interpreter: $PythonExe" }
        return $PythonExe
    }
    # `py -3` is preferred where it exists: it resolves whatever the machine's
    # registered 3.x is, which survives a Python upgrade that moves python.exe.
    foreach ($candidate in @('python', 'python3')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) {
            $version = & $found.Source --version 2>&1
            if ($version -match '3\.(\d+)' -and [int]$Matches[1] -ge 10) {
                return $found.Source
            }
            Write-Warn "$($found.Source) is $version -- needs 3.10 or newer"
        }
    }
    throw 'No suitable Python found on PATH. Install Python 3.10+ and re-run.'
}

# -- uninstall ---------------------------------------------------------------

if ($Uninstall) {
    Write-Step 'Removing the agent monitor'

    foreach ($task in @($SyncTask, $TileTask)) {
        $existing = schtasks /Query /TN $task 2>$null
        if ($LASTEXITCODE -eq 0) {
            schtasks /Delete /TN $task /F | Out-Null
            Write-Ok "scheduled task $task deleted"
        }
    }

    $hooksScript = Join-Path $InstallDir 'hooks.py'
    if (Test-Path $hooksScript) {
        try {
            $python = Resolve-Python
            & $python $hooksScript uninstall
            Write-Ok 'Claude Code hooks removed'
        } catch {
            Write-Warn "could not remove hooks automatically: $($_.Exception.Message)"
            Write-Warn 'run /hooks in Claude Code to clear them by hand'
        }
    }

    if (Test-Path $TeamFile) {
        Remove-Item $TeamFile -Force
        Write-Ok 'team.json deleted -- this laptop has stopped reporting'
    }

    Write-Host ''
    Write-Host 'Done. The local event log was left in place:' -ForegroundColor Gray
    Write-Host "  $StateDir" -ForegroundColor Gray
    Write-Host 'Delete that folder too if you want nothing left behind.' -ForegroundColor Gray
    exit 0
}

# -- settings ----------------------------------------------------------------

Write-Step 'Working out what to install'

# A team.json shipped with the bundle supplies anything not passed on the
# command line, so the usual case is running this with no arguments at all.
$bundled = Join-Path $SourceDir 'team.json'
if (Test-Path $bundled) {
    try {
        $defaults = Get-Content $bundled -Raw | ConvertFrom-Json
        if (-not $Url)     { $Url     = $defaults.url }
        if (-not $AnonKey) { $AnonKey = $defaults.anon_key }
        if (-not $TeamId)  { $TeamId  = $defaults.team_id }
        if (-not $PSBoundParameters.ContainsKey('SharePrompts')  -and $null -ne $defaults.share_prompts)  { $SharePrompts  = [bool]$defaults.share_prompts }
        if (-not $PSBoundParameters.ContainsKey('ShareHealth')   -and $null -ne $defaults.share_health)   { $ShareHealth   = [bool]$defaults.share_health }
        if (-not $PSBoundParameters.ContainsKey('ShareSessions') -and $null -ne $defaults.share_sessions) { $ShareSessions = [bool]$defaults.share_sessions }
        Write-Ok "settings read from $bundled"
    } catch {
        Write-Warn "could not parse $bundled -- $($_.Exception.Message)"
    }
}

if (-not $Member) { $Member = $env:USERNAME }

$missing = @()
if (-not $Url)     { $missing += 'Url' }
if (-not $AnonKey) { $missing += 'AnonKey' }
if (-not $TeamId)  { $missing += 'TeamId' }
if ($missing.Count -gt 0) {
    throw ("Missing: {0}. Pass them as parameters, or run this from a bundle that has team.json beside it." -f ($missing -join ', '))
}

$python = Resolve-Python
Write-Ok "python: $python"
Write-Ok "mode:   $Mode"
Write-Ok "member: $Member"
Write-Ok "team:   $TeamId"

# -- copy --------------------------------------------------------------------

Write-Step "Copying agent files to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Must stay in step with REPORTER_FILES in deploy.py. The store, observer and
# goal modules are required, not optional: the SessionStart hook runs
# `notify.py context`, and a missing import there fails silently.
$reporterFiles = @(
    'notify.py', 'events.py', 'agent_alert.py', 'identity.py',
    'cloud.py', 'sync.py', 'stats.py', 'hooks.py', 'sensors.py', 'msi_ec.py',
    'store.py', 'observe.py', 'goals.py', 'context.py', 'mcp_server.py'
)
$fullFiles = @(
    'widget.py', 'dashboard.py', 'charts.py', 'viz.py', 'webserver.py',
    'memory.py', 'deploy.py', 'run.vbs', 'run.bat', 'schema.sql'
)

$wanted = $reporterFiles
if ($Mode -eq 'full') { $wanted = $reporterFiles + $fullFiles }

$copied = 0
$absent = @()
foreach ($name in $wanted) {
    $from = Join-Path $SourceDir $name
    if (Test-Path $from) {
        Copy-Item $from (Join-Path $InstallDir $name) -Force
        $copied++
    } else {
        $absent += $name
    }
}
if ($Mode -eq 'full') {
    $webFrom = Join-Path $SourceDir 'web'
    if (Test-Path $webFrom) {
        Copy-Item $webFrom (Join-Path $InstallDir 'web') -Recurse -Force
    }
}
Write-Ok "$copied file(s) copied"
if ($absent.Count -gt 0) {
    Write-Warn ("not found in the source folder: {0}" -f ($absent -join ', '))
    if ($absent -contains 'notify.py' -or $absent -contains 'sync.py') {
        throw 'Core files are missing -- is this script being run from inside the project or a bundle?'
    }
}

# -- dependencies ------------------------------------------------------------

Write-Step 'Installing Python dependencies'
$requirements = Join-Path $SourceDir 'requirements.txt'
if (Test-Path $requirements) {
    # --user keeps this out of a system site-packages that may need admin.
    & $python -m pip install --user --quiet --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'pip reported a problem. Reporting may still work if psutil is already present.'
    } else {
        Write-Ok 'dependencies installed'
    }
} else {
    $packages = if ($Mode -eq 'full') { @('PySide6', 'psutil', 'pywin32') } else { @('psutil') }
    & $python -m pip install --user --quiet --disable-pip-version-check @packages
    Write-Ok ("installed {0}" -f ($packages -join ', '))
}

# -- team.json ---------------------------------------------------------------

Write-Step 'Writing team.json'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$team = [ordered]@{
    enabled        = $true
    url            = $Url.TrimEnd('/')
    anon_key       = $AnonKey
    team_id        = $TeamId
    member         = $Member
    share_tasks    = $true
    share_sessions = $ShareSessions
    share_health   = $ShareHealth
    share_prompts  = $SharePrompts
    sync_seconds   = $SyncSeconds
}
$team | ConvertTo-Json | Set-Content -Path $TeamFile -Encoding utf8
Write-Ok "$TeamFile"
Write-Ok ("sharing: tasks{0}{1}{2}" -f `
    $(if ($SharePrompts)  { ', prompt text' } else { '' }), `
    $(if ($ShareSessions) { ', live sessions' } else { '' }), `
    $(if ($ShareHealth)   { ', machine health' } else { '' }))

# -- hooks -------------------------------------------------------------------

Write-Step 'Registering the Claude Code hooks'
& $python (Join-Path $InstallDir 'hooks.py') install --python $python
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'hook registration reported a problem -- check with: python hooks.py status'
} else {
    Write-Ok 'six hooks wired into Claude Code'
}

# -- scheduled tasks ---------------------------------------------------------

Write-Step 'Scheduling the upload'

# A scheduled task rather than a background process: it survives a reboot, has
# no window, and shows up somewhere the person can find and disable it.
$syncScript = Join-Path $InstallDir 'sync.py'
$pythonw = $python -replace 'python\.exe$', 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$syncCommand = "`"$pythonw`" `"$syncScript`" push"

schtasks /Query /TN $SyncTask 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    schtasks /Delete /TN $SyncTask /F | Out-Null
}
# Minimum granularity for schtasks is one minute, which is also plenty: the
# hooks have already written everything locally, so a late upload costs nothing.
$minutes = [Math]::Max(1, [Math]::Round($SyncSeconds / 60))
schtasks /Create /TN $SyncTask /TR $syncCommand /SC MINUTE /MO $minutes /F | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "scheduled task $SyncTask runs every $minutes minute(s)"
} else {
    Write-Warn "could not create the scheduled task -- run it by hand with: python `"$syncScript`" daemon"
}

if ($Mode -eq 'full') {
    Write-Step 'Setting the tile to start at logon'
    $runVbs = Join-Path $InstallDir 'run.vbs'
    if (Test-Path $runVbs) {
        schtasks /Query /TN $TileTask 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $TileTask /F | Out-Null }
        schtasks /Create /TN $TileTask /TR "wscript.exe `"$runVbs`"" /SC ONLOGON /RL HIGHEST /F | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "scheduled task $TileTask runs at logon"
        } else {
            Write-Warn 'could not register the logon task -- needs admin for /RL HIGHEST'
            Write-Warn 'the tile still runs if you start run.vbs by hand'
        }
    }
}

# -- first report ------------------------------------------------------------

Write-Step 'Sending a first report'
& $python $syncScript push
& $python $syncScript status

Write-Host ''
Write-Host 'Installed.' -ForegroundColor Green
Write-Host ''
if ($Mode -eq 'full') {
    Write-Host 'Start the tile now with:' -ForegroundColor Gray
    Write-Host "  wscript.exe `"$(Join-Path $InstallDir 'run.vbs')`"" -ForegroundColor Gray
    Write-Host 'Or open just the dashboard:' -ForegroundColor Gray
    Write-Host "  `"$python`" `"$(Join-Path $InstallDir 'dashboard.py')`"" -ForegroundColor Gray
} else {
    Write-Host 'Nothing runs on screen. To see what is being reported:' -ForegroundColor Gray
    Write-Host "  `"$python`" `"$syncScript`" status" -ForegroundColor Gray
}
Write-Host ''
Write-Host 'To stop reporting at any time, delete:' -ForegroundColor Gray
Write-Host "  $TeamFile" -ForegroundColor Gray
Write-Host 'or run this script again with -Uninstall.' -ForegroundColor Gray
