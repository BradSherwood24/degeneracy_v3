# Pilot operator runbook (Phase 4)

Operator: Brad. This is the day-to-day control surface for the live auto-fire dual-contract pilot.
Everything here runs on the always-on machine where the signing proxy lives. House law is absolute:
**Brad alone sets `ALLOW_ORDERS` and flips to `armed`; the bridge and the builder never do.**

All commands run from the pilot directory:

```
cd C:\Users\Brads\Python_stuff\degeneracy_v3\pilot
```

---

## 1. Where everything lives

| Thing | Path | Notes |
|---|---|---|
| Window process | `service\run_window.py` (`python -m service.run_window`) | one process == one hourly window |
| Mode switch | `ops\mode.txt` | one word: `shakedown` / `dry` / `armed` (unknown -> `shakedown`) |
| Scheduler scripts | `ops\register_task.ps1`, `ops\unregister_task.ps1` | register/remove the hourly task |
| Frozen policy | `policy\policy_params.json` (sha `1b01fd98…3656`) | sha-checked at startup; drift -> stand down |
| Falsifier | `ceremony\falsifier.md` | must read exactly `STATUS: FROZEN` to arm |
| Per-window journals | `journals\<CLOSE>.jsonl` + `journals\summary.jsonl` | full replay record, flushed at close |
| Pilot ledger | `ledger\pilot_ledger.jsonl` | one summary line per window (append-only) |
| Logs | `logs\<CLOSE>.log`, `logs\scheduler.out` | per-window log + the scheduler stdout/stderr capture |

The paired-replay report and the promotion-gate check read ONLY the ledger + journals.

---

## 2. Start / stop / kill

### Start the hourly schedule
```
powershell -ExecutionPolicy Bypass -File ops\register_task.ps1            # register (uses mode.txt)
powershell -ExecutionPolicy Bypass -File ops\register_task.ps1 -DryRun    # preview, registers NOTHING
```
The task wakes one process at **UTC :40** every hour (20 min before each top-of-hour close). The mode
is NOT baked in — the action runs `run_window` with no `--mode`, so `mode.txt` is read at run time.

**DST caveat (confessed):** the trigger fires at a fixed local minute computed from the machine's UTC
offset at registration and repeats hourly. For whole-hour-offset timezones this keeps the UTC:40
alignment across DST with no action. **Re-run `register_task.ps1` after any timezone/DST-policy change**
(belt-and-suspenders) — it recomputes the minute. Only a fractional-hour offset change (essentially
only Lord Howe Island) actually requires it.

### Stop the schedule (no new windows launch)
```
powershell -ExecutionPolicy Bypass -File ops\unregister_task.ps1
```
This does NOT touch a window already running.

### Kill a live window process
A window is a plain `python -m service.run_window`. To end one:
```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*run_window*' } | Select-Object ProcessId, CommandLine
Stop-Process -Id <PID>
```
**Safety:** a kill loses only that window's in-memory journal (accepted cost). It never loses a
position — the NEXT window's **reconcile-first** startup reads exchange truth and, if it finds any
position in our series, **refuses to trade and stands down** (flatten is not automatic — see F4/F8).
An armed process that crashes freezes orders on the way down (Ctrl+C flushes cleanly).

---

## 3. Mode flips (the ladder)

Edit `ops\mode.txt` to one of `shakedown`, `dry`, `armed`. No re-registration needed — the next
window reads it.

- **shakedown** — WouldFire-only. No executor, no orders. Composes `ShakedownRecorder`.
- **dry** — WouldFire-only AND exercises the arming (S5) check for observability, orders frozen. The
  24h unattended cycle runs here. (Armed mode is NEVER routed through `ShakedownRecorder`.)
- **armed** — live FIRE. Only actually arms if the S5 gate passes (below); otherwise it DEGRADES to
  dry with a loud journal record and places no orders.

Fail-closed: an unknown/empty `mode.txt` runs `shakedown`.

---

## 4. Alarms and stops — what each means, what to do

**Alarms** (notify, keep running) — visible in the window log, the journal (`alarm` records), and the
ledger entry's `alarms` list; Kalshi's app notifies on fills.

| Code | Meaning | Action |
|---|---|---|
| **A1** slippage | a fill moved > 2¢ from the intended ask | note it; if repeated, watch bin-4 in the paired report (V2 RETIRE-SIM watch) |
| **A2** ladder deviation | the paired hourly ladder's step != the map ($100, 21:00Z=$250, Fri=$500) | strangle stands down for that window; sub-$1 continues. Confirm which generation paired (F2) |
| **A3** entry-rate | fired-signal rate outside [12%,55%] rolling 3d | check for a regime change (V3 VOID-REGIME watch); no automatic action |
| **A4** guard trips | ≥ 5 book stale/lag/silence trips in one UTC day | the day stands down; investigate the feed/proxy |

**Stops** (freeze orders; apply position policy; alert loudly) — journal `stop` records + ledger
`stops` list.

| Code | Meaning | Action |
|---|---|---|
| **S1** arithmetic (n=1) | a FILLED sub-$1 pair whose worst-case realized < 0 with ACTUAL fees | **halt the pilot.** Our fee model / fill price / pairing is wrong. Investigate before any re-arm |
| **S2** imbalance | the imbalance protocol could not restore 1:1 / 0:0 within its bounds | flatten policy applied (F8); check the leg that stranded; re-flat manually if needed |
| **S3** reconcile mismatch | a believed-fill not on the exchange, or any ledger-vs-exchange diff | **halt.** Trust the exchange. Reconcile by hand before re-arming |
| **S4** daily loss | daily realized loss reached the cap ($5.00) | orders frozen for the day; resume next UTC day after review |
| **S5** arming refusal | falsifier not FROZEN, policy sha bad, or /health caps absent at arming | never armed to begin with; fix the precondition (see arming checklist) |

Position policy on a stop (**PENDING-BRAD F8**, current defaults): a COMPLETE sub-$1 floor pair is
HELD to settlement (safe: it pays ≥ $1); unprotected exposure (strangle legs, unpaired flip overhang)
is FLATTENED via reduce_only IOC sells at the observed bid (a flatten with no bid is held + alerted,
never blind-sold).

---

## 5. The daily paired report (five bins)

Each live day, pull that day's tape, run the frozen sim over the same hours, and bin every
divergence. The comparison is done by script, never by eye:

```
python -m service.parity_report --manifest <manifest.json> --out-json report.json --out-text report.txt
```
Manifest schema is documented at the top of `service\parity_report.py` (tape path + per-window
close_time, journal path, high/low tickers, quintile, G, sigma, and optional fills path). The window
meta the manifest needs is exactly what `run_window` journals (`window_meta` + `quintile` records) and
what the ledger entry carries. Exit 0 == parity clean (no bin-1/bin-2 fire/no-fire disagreements).

Bins: 1 sim-only, 2 live-only, 3 both/no-fill (fillability — the pilot's reason to exist), 4
both/diff-price (slippage), 5 both/match, + the imbalance bin.

---

## 6. Ledger queries + promotion gates

```
python -m service.pilot_ledger day 2026-08-22      # windows on a UTC day
python -m service.pilot_ledger loss                # S4 running realized total (all days)
python -m service.pilot_ledger loss --day 2026-08-22
python -m service.pilot_ledger gate P1             # P1 gate (exit 0 = pass)
python -m service.pilot_ledger gate P2
python -m service.pilot_ledger summary             # windows + realized total + both gates
```

**P1 (1 pair -> 2 pairs)** — ALL must hold on the 1-pair rung: ≥ 10 fired signals; fill rate ≥ 60%;
zero unresolved imbalances; zero S1 violations; mean |slippage| ≤ 1¢ per side; ≥ 1 calendar day at
1 pair. Brad rules on the report — the gate is the floor, never an auto-promote.

**P2 (2 pairs -> readiness report for 5)** — on the 2-pair rung: ≥ 10 further fired incl. ≥ 5 sub-$1;
P1 conditions still holding; second-pair book-walk measured (reported per 2-pair window). This report
closes the pilot; sizing to 5 needs a new commission.

The gate booleans are COMPUTED from the ledger every time — the falsifier text governs; the thresholds
are mirrored in `pilot_ledger.py` and must be re-mirrored if a freeze re-pins one.

---

## 7. Arming checklist (Brad only — every box, in order)

1. **Falsifier FROZEN.** `ceremony\falsifier.md` line reads exactly `STATUS: FROZEN` (and the two
   DEFERRED F4/F8 items are resolved — see §9).
2. **Policy sha.** `policy\policy_params.json` canonical sha == `1b01fd98…3656` (a `run_window` start
   self-checks it; a mismatch stands the window down).
3. **Proxy restarted with orders enabled.** Restart the proxy (`python proxy.py` / `run.ps1`) with
   `ALLOW_ORDERS=true`. Confirm `GET http://127.0.0.1:8642/health` shows `orders_enabled: true` and a
   `caps` block (`max_contracts_per_order`, `ticker_prefixes`, `daily_order_budget`).
4. **Flat.** No open position in `KXBTC15M*` / `KXBTCD*` (reconcile-first will refuse otherwise).
5. **`mode.txt = armed`.**
6. Watch the first armed window's log + Kalshi app. If S5 refuses, `run_window` DEGRADES to dry
   (orders frozen) and journals why — fix the failing precondition and let the next window try.

To stand down instantly at any time: set `mode.txt = dry` (next window) or unregister the task.

---

## 8. The 24h unattended dry cycle (Phase-4 exit criterion)

Goal: prove every hourly window wakes, runs, journals, and exits 0 with **zero orders attempted**,
unattended, for 24 hours.

Procedure:
1. `mode.txt = dry`; register the task (`register_task.ps1`).
2. Leave it for 24h (the machine stays awake).
3. Verify from the ledger:
```
python -m service.pilot_ledger day <each UTC day covered>
```
Pass criteria, per window that had a discoverable pair:
- an entry exists in `ledger\pilot_ledger.jsonl` (woke + ran + journaled + finalized),
- `exit_code == 0`,
- `orders_attempted == 0`,
- `mode == "dry"` (or `stand_down == true` with a clean reason — a stood-down window still counts as
  a healthy no-op: missing hourly leg at 06–09 UTC, ladder deviation, NoQuintile-no-pair, etc.).

A quick scan for any bad exit:
```
python -c "import json;[print(r) for r in map(json.loads,open('ledger/pilot_ledger.jsonl')) if r.get('exit_code') or r.get('orders_attempted')]"
```
(prints nothing == clean.)

---

## 9. Still open — PENDING-BRAD (owed before Phase 5 arms)

- **F4 stop semantics — in-window crash supervisor.** A crash mid-window settles unmanaged before the
  next :40 wake. Today: reconcile-first at the next window refuses to trade on any inherited position
  (safe, but the position rides to settlement unmanaged). Decision owed: an in-window supervisor /
  restart, yes or no.
- **F8 stop semantics — hold vs flatten.** On a stop, HOLD a complete floor-protected sub-$1 pair to
  settlement vs flatten it. Current defaults: hold complete floor pairs, flatten unprotected exposure
  (reduce_only). Both flags live on `StopConfig`; Brad's ruling is owed.
- **F2 dual-generation discovery.** On a dual-listing hour (e.g. 2026-07-31 21:00Z), leg selection
  picks the smallest-window generation and validates THAT ladder against the map; a deviation stands
  the strangle down. Confirm this is the intended pairing rule (vs the map selecting the generation).
- **R5 budget-file fail-open.** The proxy's daily order budget persists to `order_budget.json`. If
  that file is unreadable/corrupt, confirm the intended behavior (the proxy recovers a corrupt file;
  the client-side Executor token budget is an independent second cap).

Live-verification items that only real traffic resolves: the WS payload field names/shapes, live
market status strings, whether `/markets` returns SETTLED trailing 15M markets by close-ts (sigma-hat
depends on it — otherwise every window degrades to sub-$1-only), the positions payload shape/sign, the
`average_fee_paid` semantics (total vs per-contract), and the live create-order path vs the proxy's
capped path.
