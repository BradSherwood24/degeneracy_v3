# run_v33_eth_dry.ps1 -- laptop wake-loop for the V3.3 ETH DRY pump-fader logger.
#
# Wakes each UTC hour at :40 and runs ONE dry ETH window (`python -m service.run_v33_eth`) for the
# upcoming :00 close, exactly as the BTC supervisor wakes run_v33. DRY BY CONSTRUCTION: run_v33_eth never
# arms and builds no write-capable executor; the laptop proxy is read-only as well (belt). Records nothing
# but market data + dry_sim fills + a labelled ETH ledger row.
#
# House law: `python` only (never python3/py on this box). All Kalshi access is via the local proxy; this
# script touches no key material. Console output is captured as UTF-8 via `Out-File -Encoding utf8` (NEVER
# PowerShell `*>>`, which writes UTF-16). The per-window journals/ledger are written by run_v33_eth itself
# under `journals_v33_eth/` + `ledger/v33_eth_ledger.jsonl` (relocated by DV3_DATA_DIR if set).
#
# Start it via Task Scheduler (see ops/V33_ETH_DRY_RUNBOOK.md); it loops until the task is stopped. One run
# per close: after a window returns (~:00:10) the next iteration computes THIS hour's :40 and sleeps, so a
# stand-down that returns early inside the band does not re-spawn for the same close.

$ErrorActionPreference = "Continue"

# pilot/ is the parent of ops/ -- run from there so `python -m service.run_v33_eth` resolves.
$PilotDir = Split-Path -Parent $PSScriptRoot
Set-Location $PilotDir

# Console log (UTF-8). The structured per-window record lives in the ETH ledger; this is the ops tail.
$LogDir = Join-Path $PilotDir "logs_v33_eth"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
$Log = Join-Path $LogDir "run_v33_eth_dry.out"

function Write-Log([string]$msg) {
    $ts = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    "$ts  $msg" | Out-File -FilePath $Log -Append -Encoding utf8
}

Write-Log "boot run_v33_eth_dry loop (pilot=$PilotDir, DV3_DATA_DIR=$($env:DV3_DATA_DIR))"

while ($true) {
    # next UTC :40 at or after now.
    $now  = [DateTime]::UtcNow
    $wake = $now.Date.AddHours($now.Hour).AddMinutes(40)
    if ($now -gt $wake) { $wake = $wake.AddHours(1) }
    $sleepS = ($wake - [DateTime]::UtcNow).TotalSeconds
    if ($sleepS -gt 0) {
        Write-Log ("sleep {0:N0}s -> wake {1:yyyy-MM-ddTHH:mm:ssZ}" -f $sleepS, $wake)
        Start-Sleep -Seconds $sleepS
    }

    Write-Log "launching ETH dry window (targets next :00)"
    # 2>&1 merges stderr (where Python logging writes) into stdout; Out-File writes UTF-8.
    & python -m service.run_v33_eth 2>&1 | Out-File -FilePath $Log -Append -Encoding utf8
    Write-Log "ETH dry window returned (exit=$LASTEXITCODE)"
    # loop: the next iteration computes this hour's :40 (the window ran ~:40->:00), so one run per close.
}
