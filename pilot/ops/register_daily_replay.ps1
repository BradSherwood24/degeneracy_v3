<#
.SYNOPSIS
  Register the DAILY REPLAY + TRIPWIRE as a Windows scheduled task (DegeneracyReplayDaily).

.DESCRIPTION
  Runs "python tools\daily_replay.py" from the REPO ROOT once a morning. The runner is OFFLINE
  (re-runs the V3.2 pump-fader over the ms journals in pilot\journals_v32\*.jsonl.gz, never the
  proxy), incrementally replays only the new windows, merges them into the rolling fills history, and
  writes the falsifier TRIPWIRE (pilot\ops\replay_tripwire.txt / .json). It only REPORTS -- standing
  V3.2 down remains Brad's lever.

  SCHEDULE -- 10:10 UTC daily.
    Task Scheduler triggers fire on LOCAL wall-clock, so this script converts 10:10 UTC to local time
    AT REGISTRATION and registers a daily trigger at that local time. The box is currently UTC-4
    (EDT), so 10:10 UTC -> 06:10 local. (10:10 UTC is chosen so the previous UTC day's :40 windows
    have all closed and rotated to .gz.)
    CAVEAT: a daily trigger keeps its local time across a DST change, so the effective UTC firing time
    shifts by one hour when the box's offset changes (EDT<->EST). Re-run this script after a DST change
    to re-pin 10:10 UTC. The script prints both the UTC target and the computed local time.

  A first run after a long gap is bounded by the runner's own --max-windows guard (default 60), so the
  task cannot run for hours unattended; the remaining windows are picked up the next morning.

  Task settings:
    * -Daily trigger at the computed local time;
    * -StartWhenAvailable (a missed run -- box asleep at the trigger -- fires when the box is next up);
    * -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries (battery never holds it back or stops it);
    * -ExecutionTimeLimit 2h (a stuck run is killed; a normal daily increment is ~20 min);
    * -MultipleInstances IgnoreNew (a still-running instance is never doubled).

  LOGON TYPE (-LogonType, default Interactive):
    Interactive -- runs when the user is logged on; needs NO admin shell to register. This is the
      default and the recommended choice (the runner only touches local disk).
    S4U -- "run whether the user is logged on or not" WITHOUT a stored password; registering an S4U
      task needs an ADMIN PowerShell. Choose this only if the replay must run while logged off.

  ASCII-only, Windows PowerShell 5.1 compatible. -DryRun prints the command it WOULD register and
  exits WITHOUT touching Task Scheduler (used by the test suite; NEVER auto-register in tests). Never
  run this without -DryRun unless Brad intends to schedule the task.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyReplayDaily",
    [ValidateSet("Interactive", "S4U")]
    [string]$LogonType = "Interactive",
    [string]$PythonExe = "",
    [string]$RepoRoot  = "",
    [string]$LogDir    = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- resolve paths (default to this repo layout: ops -> pilot -> repo root) ---
$OpsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PilotDir = Split-Path -Parent $OpsDir
if ([string]::IsNullOrEmpty($RepoRoot)) { $RepoRoot = Split-Path -Parent $PilotDir }
if ([string]::IsNullOrEmpty($LogDir))   { $LogDir = Join-Path $RepoRoot "sim\out\v32_replay\daily" }
if ([string]::IsNullOrEmpty($PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $PythonExe = $cmd.Source } else { $PythonExe = "python" }
}

$UserId = "$env:USERDOMAIN\$env:USERNAME"

# --- 10:10 UTC -> local, computed at registration time ---
$utcTarget   = (Get-Date).ToUniversalTime().Date.AddHours(10).AddMinutes(10)   # today 10:10:00 UTC
$localTarget = $utcTarget.ToLocalTime()
$utcStr   = $utcTarget.ToString("HH:mm") + " UTC"
$localStr = $localTarget.ToString("HH:mm") + " local"

# --- action: cmd.exe redirect so stdout+stderr land in scheduler.out (cmd will NOT create the dir) ---
$schedLog = Join-Path $LogDir "scheduler.out"
$runArg = '/c "' + '"' + $PythonExe + '"' + ' tools\daily_replay.py >> "' + $schedLog + '" 2>&1"'

$settingsExpr = ("New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries" +
    " -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)" +
    " -MultipleInstances IgnoreNew")
if ($LogonType -eq "S4U") {
    $principalExpr = ("New-ScheduledTaskPrincipal -UserId '" + $UserId + "' -LogonType S4U -RunLevel Limited")
} else {
    $principalExpr = ("New-ScheduledTaskPrincipal -UserId '" + $UserId + "' -LogonType Interactive")
}
$triggerExpr = ("New-ScheduledTaskTrigger -Daily -At '" + $localTarget.ToString("HH:mm:ss") + "'")

$commandLine = ("Register-ScheduledTask -TaskName '" + $TaskName + "'" +
    " -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '" + $runArg + "'" +
    " -WorkingDirectory '" + $RepoRoot + "')" +
    " -Trigger (" + $triggerExpr + ")" +
    " -Principal (" + $principalExpr + ")" +
    " -Settings (" + $settingsExpr + ")")

Write-Output ("[register_daily_replay] task name    : " + $TaskName)
Write-Output ("[register_daily_replay] python       : " + $PythonExe)
Write-Output ("[register_daily_replay] repo root    : " + $RepoRoot)
Write-Output ("[register_daily_replay] command      : python tools\daily_replay.py")
Write-Output ("[register_daily_replay] schedule     : 10:10 UTC daily -> " + $utcStr + " = " + $localStr + " (box currently UTC-4)")
Write-Output ("[register_daily_replay] logon type   : " + $LogonType)
Write-Output ("[register_daily_replay] scheduler log: " + $schedLog)
Write-Output ("[register_daily_replay] register cmd : " + $commandLine)
if ($LogonType -eq "S4U") {
    Write-Output ("[register_daily_replay] NOTE: S4U registration needs an ADMIN PowerShell.")
}

if ($DryRun) {
    Write-Output "[register_daily_replay] DRY RUN -- nothing registered."
    return
}

# cmd.exe redirect (>> "log") cannot create the parent dir; make it now so the first run launches.
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
if ($LogonType -eq "S4U") {
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive
}
$trigger = New-ScheduledTaskTrigger -Daily -At $localTarget
$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $runArg -WorkingDirectory $RepoRoot

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output ("[register_daily_replay] registered '" + $TaskName + "' (fires " + $localStr + " = " + $utcStr + ").")
