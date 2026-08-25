<#
.SYNOPSIS
  Register the degeneracy_v3 pilot window task in Windows Task Scheduler.

.DESCRIPTION
  Wakes one window process at UTC :40 every hour (20 minutes before each top-of-hour close).
  The MODE is NOT baked into the registration: the action runs `run_window` with no --mode, so
  run_window reads pilot/ops/mode.txt at RUN time. Brad flips shakedown/dry/armed by editing
  mode.txt -- NO re-registration needed.

  DST APPROACH (chosen + confessed): the trigger fires at a FIXED LOCAL MINUTE computed once at
  registration from the machine's current UTC offset, and REPEATS every 1 hour. Because an hourly
  repeat at a fixed minute keeps firing at that minute of every local hour, and real DST shifts are
  whole hours, the UTC :40 alignment is preserved across a DST change for every whole-hour-offset
  timezone WITHOUT re-registration. The one caveat: a timezone whose UTC offset changes by a
  fractional-hour amount (essentially only Lord Howe Island's 30-min DST) would move the :40-UTC
  minute -> re-run this script after such a change. Belt-and-suspenders: re-run register_task.ps1
  after ANY timezone/DST-policy change and the minute is recomputed. (The single DST-transition hour
  can fire once early/late; the window simply stands down if no leg is discoverable.)

  ASCII-only, Windows PowerShell 5.1 compatible. -DryRun prints the command it WOULD register and
  exits WITHOUT touching Task Scheduler (used by the test suite; NEVER auto-register in tests).
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyV3Pilot",
    [string]$PythonExe = "",
    [string]$WorkingDir = "",
    [string]$LogDir = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- resolve paths (default to this repo layout) ---
$OpsDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PilotDir = Split-Path -Parent $OpsDir
if ([string]::IsNullOrEmpty($WorkingDir)) { $WorkingDir = $PilotDir }
if ([string]::IsNullOrEmpty($LogDir))     { $LogDir = Join-Path $PilotDir "logs" }
if ([string]::IsNullOrEmpty($PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $PythonExe = $cmd.Source } else { $PythonExe = "python" }
}

# --- compute the fixed local minute that corresponds to UTC :40 right now ---
$offset        = [System.TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now)
$offsetMinutes = [int]$offset.TotalMinutes
$localMinute   = ((40 + $offsetMinutes) % 60 + 60) % 60

# --- StartBoundary: the next occurrence of that minute, then repeat hourly ---
$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes($localMinute)
if ($start -le $now) { $start = $start.AddHours(1) }

# --- action: run_window with NO --mode (mode.txt governs); redirect stdout+stderr to a log ---
$logFile = Join-Path $LogDir "scheduler.out"
$actionArg = '/c "' + '"' + $PythonExe + '"' + ' -m service.run_window >> "' + $logFile + '" 2>&1"'

# --- assemble a one-line, well-formed description of the registration command ---
$commandLine = ("Register-ScheduledTask -TaskName '" + $TaskName + "'" +
    " -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument " + "'" + $actionArg + "'" +
    " -WorkingDirectory '" + $WorkingDir + "')" +
    " -Trigger (New-ScheduledTaskTrigger -Once -At '" + $start.ToString("s") + "'" +
    " -RepetitionInterval (New-TimeSpan -Hours 1))" +
    " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries" +
    " -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew)")

Write-Output ("[register_task] task           : " + $TaskName)
Write-Output ("[register_task] python         : " + $PythonExe)
Write-Output ("[register_task] working dir    : " + $WorkingDir)
Write-Output ("[register_task] UTC offset min : " + $offsetMinutes)
Write-Output ("[register_task] local minute   : " + $localMinute + " (UTC :40 -> local :" + ("{0:D2}" -f $localMinute) + ")")
Write-Output ("[register_task] first fire     : " + $start.ToString("s") + " (repeats every 1h)")
Write-Output ("[register_task] log file       : " + $logFile)
Write-Output ("[register_task] command        : " + $commandLine)

if ($DryRun) {
    Write-Output "[register_task] DRY RUN -- nothing registered."
    return
}

# The scheduled action redirects stdout/stderr via cmd.exe (>> "$logFile"). cmd.exe will NOT create
# the parent directory, so a missing logs\ makes the FIRST scheduled window fail to launch (the
# redirect target cannot be opened -- python never runs). Create it now, at registration.
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Write-Output ("[register_task] created log dir  : " + $LogDir)
}

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $actionArg -WorkingDirectory $WorkingDir
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
# -MultipleInstances IgnoreNew (F2-ops): pin single-instance -- if a prior window process is still
# running when the next :40 fire arrives (e.g. a hung poll/window that outlived its ~20min budget),
# the platform SKIPS the new instance rather than launching an overlapping second armed process. This
# removes the double-entry exposure the reconcile-first flat-read race (phase-4 finding 1) could not
# fully close, and pins the behavior instead of relying on the platform default.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output ("[register_task] registered '" + $TaskName + "'. Flip mode via " + (Join-Path $OpsDir "mode.txt") + " (no re-register).")
