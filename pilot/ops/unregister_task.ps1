<#
.SYNOPSIS
  Unregister the degeneracy_v3 pilot window task.

.DESCRIPTION
  Removes the scheduled task so no new window processes are launched. Does NOT touch any window
  process that is already running (see runbook.md "Stop / kill" for how to end a live window and how
  the reconcile-first startup keeps positions safe across a kill). ASCII-only, PS 5.1 compatible.
  -DryRun prints what it WOULD remove and exits without touching Task Scheduler.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyV3Pilot",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$commandLine = "Unregister-ScheduledTask -TaskName '" + $TaskName + "' -Confirm:$false"
Write-Output ("[unregister_task] task    : " + $TaskName)
Write-Output ("[unregister_task] command : " + $commandLine)

if ($DryRun) {
    Write-Output "[unregister_task] DRY RUN -- nothing removed."
    return
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Output ("[unregister_task] no task named '" + $TaskName + "' found; nothing to do.")
    return
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output ("[unregister_task] removed '" + $TaskName + "'.")
