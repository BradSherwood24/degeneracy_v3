<#
.SYNOPSIS
  Unregister the degeneracy_v3.2 (pump-fader) window task.

.DESCRIPTION
  Removes the scheduled task so no new V3.2 window processes are launched. Does NOT touch any window
  process that is already running. ASCII-only, PS 5.1 compatible. -DryRun prints what it WOULD remove
  and exits without touching Task Scheduler.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "DegeneracyV3_2",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$commandLine = "Unregister-ScheduledTask -TaskName '" + $TaskName + "' -Confirm:$false"
Write-Output ("[unregister_v32_task] task    : " + $TaskName)
Write-Output ("[unregister_v32_task] command : " + $commandLine)

if ($DryRun) {
    Write-Output "[unregister_v32_task] DRY RUN -- nothing removed."
    return
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Output ("[unregister_v32_task] no task named '" + $TaskName + "' found; nothing to do.")
    return
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output ("[unregister_v32_task] removed '" + $TaskName + "'.")
