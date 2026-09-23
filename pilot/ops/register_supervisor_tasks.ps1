<#
.SYNOPSIS
  Register the V3.3 host-shaped runtime as TWO Windows scheduled tasks: the Kalshi signing proxy and
  the degeneracy_v3 supervisor. This is the LOCAL stand-in for what Render's two services will run.

.DESCRIPTION
  Task 1  DegeneracyProxy           -> runs the signing proxy (python proxy.py in degeneracy-proxy\).
  Task 2  DegeneracyV3_Supervisor   -> runs "python -m service.supervisor" from pilot\; the supervisor
                                       wakes ONE run_v32 window per UTC :40 (replacing the per-hour
                                       DegeneracyV3_2 task), does the boot cancel sweep and the
                                       leftover-journal rotation, and is SIGTERM-aware.

  Both tasks:
    * trigger AT STARTUP and "run whether the user is logged on or not";
    * restart on failure (3 tries, 1 minute apart);
    * -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries (never held back or stopped by battery);
    * -ExecutionTimeLimit 0 (never auto-killed -- these are long-running processes);
    * -MultipleInstances IgnoreNew (a still-running instance is never doubled).

  LOGON TYPE -- tradeoff (documented):
    -LogonType S4U ("run whether user is logged on or not" WITHOUT storing a password). S4U runs the
    task non-interactively in the user's own context with NO stored credential, so nothing secret is
    kept in Task Scheduler. The cost: an S4U task cannot reach resources that need the user's password
    (mapped network drives, DPAPI creds unlockable only with the password). Our processes only touch
    localhost (the proxy at 127.0.0.1:8642) and local disk, so S4U is the right, password-free choice.
    The alternative, -LogonType Password (or -User/-Password), stores the account password with the
    task; rejected here to avoid holding a credential. Choose Password only if you later need networked
    resources under the user's identity.

  IMPORTANT -- never two window drivers at once. When the supervisor takes over, the old per-hour
  task DegeneracyV3_2 MUST be unregistered by Brad (ops\unregister_v32_task.ps1). Running BOTH would
  launch two run_v32 windows per hour against the same account. This script only WARNS; Brad pulls
  that lever.

  ASCII-only, Windows PowerShell 5.1 compatible. -DryRun prints the commands it WOULD register and
  exits WITHOUT touching Task Scheduler (used by the test suite; NEVER auto-register in tests). Never
  run this without -DryRun unless Brad intends to schedule the tasks.
#>
[CmdletBinding()]
param(
    [string]$ProxyTaskName      = "DegeneracyProxy",
    [string]$SupervisorTaskName = "DegeneracyV3_Supervisor",
    [string]$PythonExe = "",
    [string]$PilotDir  = "",
    [string]$ProxyDir  = "",
    [string]$LogDir    = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- resolve paths (default to this repo layout) ---
$OpsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrEmpty($PilotDir)) { $PilotDir = Split-Path -Parent $OpsDir }
$RepoRoot = Split-Path -Parent $PilotDir
$RepoParent = Split-Path -Parent $RepoRoot
if ([string]::IsNullOrEmpty($ProxyDir)) { $ProxyDir = Join-Path $RepoParent "degeneracy-proxy" }
if ([string]::IsNullOrEmpty($LogDir))   { $LogDir = Join-Path $PilotDir "logs_v32" }
if ([string]::IsNullOrEmpty($PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $PythonExe = $cmd.Source } else { $PythonExe = "python" }
}

$UserId = "$env:USERDOMAIN\$env:USERNAME"

# --- actions (cmd.exe redirect so stdout+stderr land in a log; cmd.exe will NOT create the dir) ---
$proxyLog = Join-Path $ProxyDir "proxy.out.log"
$superLog = Join-Path $LogDir "supervisor.scheduler.out"
$proxyArg = '/c "' + '"' + $PythonExe + '"' + ' proxy.py >> "' + $proxyLog + '" 2>&1"'
$superArg = '/c "' + '"' + $PythonExe + '"' + ' -m service.supervisor >> "' + $superLog + '" 2>&1"'

# --- one-line, well-formed descriptions of the two registration commands ---
$settingsExpr = ("New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries" +
    " -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)" +
    " -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew")
$principalExpr = ("New-ScheduledTaskPrincipal -UserId '" + $UserId + "' -LogonType S4U -RunLevel Limited")

$proxyCommandLine = ("Register-ScheduledTask -TaskName '" + $ProxyTaskName + "'" +
    " -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '" + $proxyArg + "'" +
    " -WorkingDirectory '" + $ProxyDir + "')" +
    " -Trigger (New-ScheduledTaskTrigger -AtStartup)" +
    " -Principal (" + $principalExpr + ")" +
    " -Settings (" + $settingsExpr + ")")

$superCommandLine = ("Register-ScheduledTask -TaskName '" + $SupervisorTaskName + "'" +
    " -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '" + $superArg + "'" +
    " -WorkingDirectory '" + $PilotDir + "')" +
    " -Trigger (New-ScheduledTaskTrigger -AtStartup)" +
    " -Principal (" + $principalExpr + ")" +
    " -Settings (" + $settingsExpr + ")")

Write-Output ("[register_supervisor] proxy task      : " + $ProxyTaskName)
Write-Output ("[register_supervisor] supervisor task : " + $SupervisorTaskName)
Write-Output ("[register_supervisor] python          : " + $PythonExe)
Write-Output ("[register_supervisor] pilot dir       : " + $PilotDir)
Write-Output ("[register_supervisor] proxy dir       : " + $ProxyDir)
Write-Output ("[register_supervisor] logon type      : S4U (run whether logged on or not; no stored password)")
Write-Output ("[register_supervisor] proxy log       : " + $proxyLog)
Write-Output ("[register_supervisor] supervisor log  : " + $superLog)
Write-Output ("[register_supervisor] proxy command   : " + $proxyCommandLine)
Write-Output ("[register_supervisor] super command   : " + $superCommandLine)
Write-Output ("[register_supervisor] WARNING: unregister DegeneracyV3_2 before the supervisor drives windows -- never two.")

if ($DryRun) {
    Write-Output "[register_supervisor] DRY RUN -- nothing registered."
    return
}

# cmd.exe redirects (>> "log") cannot create the parent dir; make both now so the first run launches.
if (-not (Test-Path $LogDir))   { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
if (-not (Test-Path $ProxyDir)) { throw "proxy dir not found: $ProxyDir" }

$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
$trigger   = New-ScheduledTaskTrigger -AtStartup

$proxyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $proxyArg -WorkingDirectory $ProxyDir
Register-ScheduledTask -TaskName $ProxyTaskName -Action $proxyAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output ("[register_supervisor] registered '" + $ProxyTaskName + "'.")

$superAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $superArg -WorkingDirectory $PilotDir
Register-ScheduledTask -TaskName $SupervisorTaskName -Action $superAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output ("[register_supervisor] registered '" + $SupervisorTaskName + "'.")
Write-Output ("[register_supervisor] REMINDER: run ops\unregister_v32_task.ps1 so DegeneracyV3_2 no longer fires.")
