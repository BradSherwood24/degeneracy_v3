<#
.SYNOPSIS
  Unregister the V3.3 host-shaped runtime tasks (DegeneracyProxy + DegeneracyV3_Supervisor).

.DESCRIPTION
  Removes both scheduled tasks so no new proxy / supervisor processes are launched. Does NOT touch any
  process already running (stop those manually if needed). ASCII-only, PS 5.1 compatible. -DryRun
  prints what it WOULD remove and exits without touching Task Scheduler.

  NOTE: rolling BACK to the per-hour driver means re-registering DegeneracyV3_2
  (ops\register_v32_task.ps1) AFTER this -- never run both drivers at once.
#>
[CmdletBinding()]
param(
    [string]$ProxyTaskName      = "DegeneracyProxy",
    [string]$SupervisorTaskName = "DegeneracyV3_Supervisor",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$proxyCommandLine = "Unregister-ScheduledTask -TaskName '" + $ProxyTaskName + "' -Confirm:$false"
$superCommandLine = "Unregister-ScheduledTask -TaskName '" + $SupervisorTaskName + "' -Confirm:$false"
Write-Output ("[unregister_supervisor] proxy task      : " + $ProxyTaskName)
Write-Output ("[unregister_supervisor] supervisor task : " + $SupervisorTaskName)
Write-Output ("[unregister_supervisor] proxy command   : " + $proxyCommandLine)
Write-Output ("[unregister_supervisor] super command   : " + $superCommandLine)

if ($DryRun) {
    Write-Output "[unregister_supervisor] DRY RUN -- nothing removed."
    return
}

foreach ($name in @($SupervisorTaskName, $ProxyTaskName)) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output ("[unregister_supervisor] no task named '" + $name + "' found; nothing to do.")
        continue
    }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Output ("[unregister_supervisor] removed '" + $name + "'.")
}
