<#
.SYNOPSIS
  Unregister the DAILY REPLAY + TRIPWIRE scheduled task (DegeneracyReplayDaily).

.DESCRIPTION
  Removes the scheduled task so no new daily replay runs are launched. Does NOT touch a run already in
  flight (let it finish, or stop it manually). The rolling history (sim\out\v32_replay\) and the last
  tripwire files (pilot\ops\replay_tripwire.*) are left in place. ASCII-only, PS 5.1 compatible.
  -DryRun prints what it WOULD remove and exits without touching Task Scheduler.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyReplayDaily",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$commandLine = "Unregister-ScheduledTask -TaskName '" + $TaskName + "' -Confirm:$false"
Write-Output ("[unregister_daily_replay] task name : " + $TaskName)
Write-Output ("[unregister_daily_replay] command   : " + $commandLine)

if ($DryRun) {
    Write-Output "[unregister_daily_replay] DRY RUN -- nothing removed."
    return
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Output ("[unregister_daily_replay] no task named '" + $TaskName + "' found; nothing to do.")
    return
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output ("[unregister_daily_replay] removed '" + $TaskName + "'.")
