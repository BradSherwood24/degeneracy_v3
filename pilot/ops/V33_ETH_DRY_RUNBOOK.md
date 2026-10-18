# V3.3 ETH DRY logger — runbook

Status: **DRY ONLY, BY CONSTRUCTION.** Runs the V3.3 ladder pump-fader against the ETH hourly markets
(`KXETH` range + `KXETHD` strike) in DRY to measure whether ETH has edge. It never arms, builds no
write-capable executor, and the laptop proxy is read-only. Nothing here places an order.

## What it is

`python -m service.run_v33_eth` runs ONE dry ETH window (mirrors `run_v33` for BTC): it discovers the
co-settling ETH range buckets + strike ladder, runs the SAME `decide_v33` core (via `V33Driver`), journals
WOULD_* intents, simulates ladder fills with the ideal rule (`dry_sim`), and appends a labelled per-window
row to `ledger/v33_eth_ledger.jsonl`. The laptop loop `ops/run_v33_eth_dry.ps1` wakes each UTC :40 and runs
one window for the upcoming :00.

Own writable files (separate from BTC V3.2/V3.3; relocated by `DV3_DATA_DIR` if set):
- journals: `journals_v33_eth/`
- ledger:   `ledger/v33_eth_ledger.jsonl`
- logs:     `logs_v33_eth/` (incl. `run_v33_eth_dry.out`, the loop's UTF-8 console tail)
- mode file (optional): `ops/v33_eth_mode.txt` — **you do not need to create it**; missing → dry. Even
  writing `armed` into it keeps the window DRY (the entry point refuses armed and journals the refusal).

## ETH geometry (verified via read-only proxy GETs, 2026-10-17)

- `KXETH` (range) + `KXETHD` (strike): both `exchange_index 2`, CF Benchmarks, hourly, fee `quadratic`
  multiplier 1 — the **same exact-fee law as BTC** (`0.07·p·(1-p)`, maker 0), so the money math is unchanged.
- Bucket width **$5** (BTC: $100); strike spacing **$5**; grids share an origin, so the 3-leg set pays
  **$2 everywhere** (298/298 buckets), identical structure to BTC with `Su = Sd + 5`.
- Strike ticker `KXETHD-<tag>-T<threshold.99>` (e.g. `T1704.99` → floor 1705): the `.99` sub-tick
  convention and `round(x+0.01)` parse are **identical to BTC** — only the prefix differs.
- The wide **21:00Z daily** buckets are **$40**-wide; the width filter + observed-width check stand those
  windows down automatically (ETH $5 hourly buckets only), same as BTC's 21Z stand-down.

## Dry enforcement (three ways)

1. **Entry point refuses armed.** `eth_effective_mode()` maps every requested mode to `dry` and never
   returns `armed`; a request (CLI `--mode armed` or the mode file) is journaled `armed_refused_dry_only`
   and the window runs DRY anyway.
2. **No write-capable executor is ever constructed.** The only executor built is the dry `FrozenExecutor`;
   the module imports no `ProxyWriter` / `V33LiveExecutor` and never calls `build_executor_v33` (AST test).
3. **Operational belt (noted, not relied on).** The laptop proxy is read-only (`orders_enabled: false`, and
   its order allowlist is KXBTC-only), so an ETH order could not be placed even if one were attempted.

## Start / stop (Task Scheduler) — RUN THESE YOURSELF

Register a task that runs the loop at logon (adjust the path if your checkout differs):

```powershell
$ps1 = "C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ops\run_v33_eth_dry.ps1"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "DegeneracyV33_ETH_Dry" -Action $action -Trigger $trigger `
    -Settings $settings -Description "V3.3 ETH DRY pump-fader logger (reads only; never trades)"
```

Start it now without waiting for a logon:
```powershell
Start-ScheduledTask -TaskName "DegeneracyV33_ETH_Dry"
```

Stop / remove:
```powershell
Stop-ScheduledTask -TaskName "DegeneracyV33_ETH_Dry"
Unregister-ScheduledTask -TaskName "DegeneracyV33_ETH_Dry" -Confirm:$false
```

Watch the tail:
```powershell
Get-Content "C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\logs_v33_eth\run_v33_eth_dry.out" -Wait -Tail 40
```

Run a single window by hand (one-off check, no task):
```powershell
cd C:\Users\Brads\Python_stuff\degeneracy_v3\pilot
python -m service.run_v33_eth            # targets the next :00
```

## Report

```powershell
cd C:\Users\Brads\Python_stuff\degeneracy_v3\pilot
python -m service.v33.report --ledger ledger\v33_eth_ledger.jsonl --v32-ledger ledger\v33_eth_ledger.jsonl --label DegeneracyV3_3_ETH
```

Notes on the report (BTC-specific pieces):
- The per-window table + the gate/scoreboard read the ETH ledger via `--ledger` and the rows self-label
  (`roster: DegeneracyV3_3_ETH`, `series: KXETH`).
- The **side-by-side section compares against `--v32-ledger` (the BTC live roster)** — that cross-roster
  compare is meaningless for ETH dry (ETH dry vs BTC live). Point `--v32-ledger` at the ETH ledger itself
  (as above) so the side-by-side degenerates harmlessly, and read the per-window + scoreboard sections.
- The flip-gate table (S1..S5 / capture ratio) is written for the BTC *armed* decision; ETH never arms, so
  it is informational only here.

## Disk

- Laptop free space measured 2026-10-17: **~61.5 GB** (of a 1 TB volume). Plenty of headroom.
- ETH records ~300 strikes + ~300 buckets per window (vs BTC's ~188 + ~180). The 2026-09-22 scan estimated
  ~**8–15 MB gz/window** for the full ETH pair; with the current thinner ETH tape it will likely be less.
  **Pin the real figure from the first window's `.jsonl.gz` size** and prune `journals_v33_eth/` as needed
  (the BTC journal-rotation keep-list does not cover this dir). At ~10 MB/window that is ~**240 MB/day**.
