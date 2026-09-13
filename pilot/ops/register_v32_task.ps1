<#
.SYNOPSIS
  Register the degeneracy_v3.2 (pump-fader) window task in Windows Task Scheduler.

.DESCRIPTION
  Wakes one V3.2 window process at UTC :40 every hour (20 minutes before each top-of-hour close).
  The MODE is NOT baked into the registration: the action runs run_v32 with no --mode, so run_v32
  reads pilot/ops/v32_mode.txt at RUN time. Brad flips shakedown/dry/armed by editing v32_mode.txt --
  NO re-registration needed. (In Phase 2 'armed' degrades to dry: there is no order path yet.)

  This is a COPY of register_task.ps1 with a distinct TaskName (DegeneracyV3_2), a distinct log dir
  (logs_v32), and the run_v32 entry point -- so V3.2 and v1.1 (the box) register as separate tasks and
  never collide. DST approach is identical: a fixed local minute computed once from the current UTC
  offset, repeating hourly, preserves UTC :40 across whole-hour DST shifts without re-registration
  (re-run this after any fractional-hour offset change; see register_task.ps1 for the full note).

  ASCII-only, Windows PowerShell 5.1 compatible. -DryRun prints the command it WOULD register and
  exits WITHOUT touching Task Scheduler (used by the test suite; NEVER auto-register in tests). Never
  run this without -DryRun unless Brad intends to schedule the task.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyV3_2",
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
if ([string]::IsNullOrEmpty($LogDir))     { $LogDir = Join-Path $PilotDir "logs_v32" }
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

# --- action: run_v32 with NO --mode (v32_mode.txt governs); redirect stdout+stderr to a log ---
$logFile = Join-Path $LogDir "scheduler.out"
$actionArg = '/c "' + '"' + $PythonExe + '"' + ' -m service.run_v32 >> "' + $logFile + '" 2>&1"'

# --- assemble a one-line, well-formed description of the registration command ---
$commandLine = ("Register-ScheduledTask -TaskName '" + $TaskName + "'" +
    " -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument " + "'" + $actionArg + "'" +
    " -WorkingDirectory '" + $WorkingDir + "')" +
    " -Trigger (New-ScheduledTaskTrigger -Once -At '" + $start.ToString("s") + "'" +
    " -RepetitionInterval (New-TimeSpan -Hours 1))" +
    " -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries" +
    " -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew)")

Write-Output ("[register_v32_task] task           : " + $TaskName)
Write-Output ("[register_v32_task] python         : " + $PythonExe)
Write-Output ("[register_v32_task] working dir    : " + $WorkingDir)
Write-Output ("[register_v32_task] UTC offset min : " + $offsetMinutes)
Write-Output ("[register_v32_task] local minute   : " + $localMinute + " (UTC :40 -> local :" + ("{0:D2}" -f $localMinute) + ")")
Write-Output ("[register_v32_task] first fire     : " + $start.ToString("s") + " (repeats every 1h)")
Write-Output ("[register_v32_task] log file       : " + $logFile)
Write-Output ("[register_v32_task] command        : " + $commandLine)

if ($DryRun) {
    Write-Output "[register_v32_task] DRY RUN -- nothing registered."
    return
}

# The scheduled action redirects stdout/stderr via cmd.exe (>> "$logFile"). cmd.exe will NOT create
# the parent directory, so a missing logs_v32\ makes the FIRST scheduled window fail to launch (the
# redirect target cannot be opened -- python never runs). Create it now, at registration.
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Write-Output ("[register_v32_task] created log dir  : " + $LogDir)
}

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $actionArg -WorkingDirectory $WorkingDir
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
# -MultipleInstances IgnoreNew: pin single-instance -- if a prior V3.2 window process is still running
# when the next :40 fire arrives, the platform SKIPS the new instance rather than launching an
# overlapping second process. Pins the behavior instead of relying on the platform default.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output ("[register_v32_task] registered '" + $TaskName + "'. Flip mode via " + (Join-Path $OpsDir "v32_mode.txt") + " (no re-register).")
