# V3.2 dry-run runbook (Brad's first manual dry window)

Phase 2 sends NO orders. `armed` degrades to dry. This is the "a couple dry runs just to check
functionality" step. One command, one window, then read the report.

## Prerequisites

1. **Proxy up and signed.** `curl -s http://127.0.0.1:8642/health` returns 200 and (for market data)
   the socket is signed. Dry mode reads market data only — it never sends an order — so **no `.env`
   change is needed for dry** (`ALLOW_ORDERS` and `ORDER_TICKER_PREFIXES` matter only for Phase 3
   arming). If `/health` is 503 / the proxy is down, the window will re-dial and then flush an empty
   journal at close (a clean no-op).
2. **Clean tree, tests green.** From `pilot/`: `python -m pytest -q` (expect all green).
3. You are in `pilot/` and using `python` (never `python3`/`py` on this box).
4. **Create the mode lever.** `ops/v32_mode.txt` is git-IGNORED (machine-local, exactly like the
   box's `ops/mode.txt`) so your live flips never dirty the tree. It does NOT ship in the repo. If it
   is absent, `run_v32` fails CLOSED to `shakedown` (the no-orders rung — verified in code:
   `read_v32_mode_file` returns `""` on a missing file, which `resolve_v32_mode` maps to
   `shakedown`). To run a dry window either pass `--mode dry` (below) or create the file:
   ```
   echo dry > ops\v32_mode.txt        # or: shakedown | armed  (armed degrades to dry in Phase 2)
   ```
   The scheduled task passes no `--mode`, so it reads this file at run time — flip it with no
   re-registration. `--mode` on the command line always wins over the file.

## Run one dry window

From `pilot/`:

```
python -m service.run_v32 --mode dry
```

Default close = the next top-of-hour (`next_top_of_hour_iso`). To target a specific close:

```
python -m service.run_v32 --mode dry --close 2026-09-13T21:00:00Z
```

Launch it at or after :40 for the cleanest window (discovery at :40, WS dials at the connect gate =
close − 15 min − 5 s = :44:55). Launched earlier, it discovers immediately and then **waits** at the
connect gate before dialing.

## What to expect in the log within the first minute

* `[V32] <close>: N strikes (g gen), M buckets (g gen), mode=dry effective=dry` — discovery
  succeeded. If it prints a stand-down instead (`no KXBTC range buckets…`, `no KXBTCD strike
  ladder…`, or `bucket width 250 != … (21Z $250/$500 hour)`), the window exits 0 cleanly and writes a
  stand-down summary + ledger row — nothing is wrong; that hour is not a $100 hour or the markets are
  not listed yet.
* Then a quiet hold until the connect gate, then `Kalshi WS opened …` twice (two connections: strikes
  and buckets — see the build report for the topology rationale).
* Once book frames flow (inside T-15..T-5), the journal fills with `would_place_rest` /
  `would_cancel_rest` records (the FrozenExecutor's simulated place/replace cycle) and `v32_eval`
  heartbeats. **No order is sent** — every order-bearing record is a `would_*` twin.
* Shadow fills (the ideal fill rule running on the live tape) accrue silently into the state and are
  summarized in the ledger row at close.

## Where the journal / ledger land

* Raw WS frames + every decision record: `pilot/journals_v32/<close>.jsonl` (gzipped to `.jsonl.gz`
  at close; both are git-ignored runtime state).
* One summary line: `pilot/journals_v32/summary.jsonl`.
* One ledger row: `pilot/ledger/v32_ledger.jsonl` (git-ignored).
* Logs: `pilot/logs_v32/<close>.log` when run under the scheduler (see below); an interactive run
  prints to the console.

## Read the report

```
python -m service.v32.report            # all windows
python -m service.v32.report --days 1   # just today (UTC)
```

Per-window table: mode, spot bucket, replaces, would-places, shadow fill/lock per E, and the two
per-connection lags. Totals block: window count, would-places, replaces, stand-downs, late-fills,
and the mean shadow lock per E (the dry-run statistic — the sim's edge measured on live data).

## How to stop

`Ctrl+C` — the process flushes the streamed journal, gzips it crash-safely, writes the summary +
ledger row, and exits 0. (The journal is also flushed to the OS every 200 frames, so a hard kill
loses at most the last <200 frames.)

---

## Task registration (Brad-only lever — SEPARATE from a manual dry run)

To run V3.2 hourly on its own scheduled task (distinct from the box task `DegeneracyV3Pilot`):

```
# always dry-run first (prints the command, registers nothing):
powershell -NoProfile -ExecutionPolicy Bypass -File ops\register_v32_task.ps1 -DryRun

# then, to actually register (Brad's call):
powershell -NoProfile -ExecutionPolicy Bypass -File ops\register_v32_task.ps1
```

The task `DegeneracyV3_2` fires at UTC :40 hourly and runs `python -m service.run_v32` with **no
`--mode`**, so it reads `ops/v32_mode.txt` at run time — flip shakedown/dry/armed there with NO
re-registration. `-MultipleInstances IgnoreNew` skips a new fire if a prior window is still running.
Remove with `ops\unregister_v32_task.ps1` (also `-DryRun`-first). Registration is Brad's lever; the
test suite only ever runs these with `-DryRun`.
```
