# V33_RUNBOOK -- the host-shaped runtime (Phase H, run locally now; Render later)

Phase H makes the pilot run the way a hosted service would: ONE long-running **supervisor** process
wakes ONE `run_v32` window per UTC :40, instead of a Windows scheduled task firing every hour. This is
behaviour-neutral for the live V3.2 -- with no env vars set, every path and behaviour is byte-identical
to today. The Render move itself is NOT in this phase (see `RENDER_MIGRATION_PLAN.md`); Phase H is
"run what we would run as a hosted service, locally."

Everything in the "Brad's levers" section is Brad's to pull. Claude never registers a task, never
unregisters `DegeneracyV3_2`, never sets an env var that changes live behaviour, never runs the proxy
with orders.

---

## 1. The two pieces

| piece | what it is | how it starts |
|---|---|---|
| `DegeneracyProxy` | the Kalshi signing proxy (unchanged code) at `127.0.0.1:8642` | `python proxy.py` in `degeneracy-proxy\` |
| `DegeneracyV3_Supervisor` | `python -m service.supervisor` from `pilot\` -- wakes run_v32 at each UTC :40 | the supervisor loop |

The supervisor REPLACES the per-hour `DegeneracyV3_2` task. It also does two things that task never did:
- **boot cancel sweep**: on start it cancels any stray resting `v32-*` KXBTC order a crash/kill left on
  the venue (armed mode only; a no-op with a log line in dry/no-proxy).
- **leftover-journal rotation**: at each wake it gzips closed raw journals from crashed/killed windows
  (the `rotate_closed_journals` call V3.2's `run_v32` never inherited -- today's orphan raw journals).

`run_v32` is unchanged: it is still the per-window process; the supervisor only wraps it.

---

## 2. Environment variables (the host levers)

| var | unset (today) | set (host-shaped) | effect |
|---|---|---|---|
| `DV3_DATA_DIR` | -- | e.g. `D:\dv3data` or Render `/var/dv3` | relocates ALL writable paths: `journals_v32\`, `logs_v32\`, `ledger\`, and the writable ops files (the mode file `v32_mode.txt` and the day-guard/stops JSON) under `$DV3_DATA_DIR\<same name>` |
| `DV3_PROXY_BASE` | `http://127.0.0.1:8642` | e.g. `http://degeneracy-proxy-xxxx:8642` | default proxy base for run_v32 and the supervisor's boot sweep when no `--proxy-base` flag is given |

Read-only INPUTS never move regardless of `DV3_DATA_DIR`: `policy/v32_params.json`,
`ceremony/v32_falsifier.md`, and the rotation keep-list `ops/journal_keep.txt` always resolve in the
code checkout.

**Mode-file convention (important):** when `DV3_DATA_DIR` is set, the mode file lives in the DATA DIR
(`$DV3_DATA_DIR\ops\v32_mode.txt`), full stop -- there is NO fall-back read of the checkout copy. So at
cutover Brad copies the current mode file into the data dir ONCE (step 4b below). This keeps a single
source of truth on the disk the supervisor owns.

Setting either var changes where the runtime reads/writes, so both are Brad's levers.

---

## 3. Run it locally by hand (no tasks) -- to see one window

From `pilot\`:

```
# behaviour-neutral: unset env -> writes under the checkout, proxy at 127.0.0.1:8642
python -m service.supervisor --once --now
```

- `--once` runs exactly one window then exits. `--now` skips the sleep and runs immediately.
- `--dry-sweep` makes the boot sweep LIST what it would cancel without cancelling (safe inspection).
- `--proxy-base URL` overrides the proxy base for the boot sweep.
- `--child-args ...` passes everything after it verbatim to `run_v32` (must be LAST).

To exercise the data dir locally without touching the checkout:

```
set DV3_DATA_DIR=D:\dv3data
set DV3_PROXY_BASE=http://127.0.0.1:8642
python -m service.supervisor --once --now
# journals/logs/ledger/ops now under D:\dv3data
```

(The live V3.2 must keep running armed through the V3.3 build -- do NOT run a second armed window driver
by hand while `DegeneracyV3_2` or the supervisor is scheduled and armed.)

---

## 4. Cutover: from `DegeneracyV3_2` to the supervisor (Brad, in a :02-:33 UTC window)

Do this ONLY between :02 and :33 UTC so no window is mid-flight. **Never two window drivers at once.**

1. **Preview** the registration (nothing is changed):
   ```
   powershell -ExecutionPolicy Bypass -File ops\register_supervisor_tasks.ps1 -DryRun
   ```
2. **Decide the data dir** (optional). If you want writable state off the checkout, set a machine/user
   env var `DV3_DATA_DIR` (System Properties -> Environment Variables) so BOTH tasks inherit it, then
   copy the current mode file AND today's day-guard/stops JSON into the data dir ONCE:
   ```
   mkdir "%DV3_DATA_DIR%\ops"
   copy ops\v32_mode.txt "%DV3_DATA_DIR%\ops\v32_mode.txt"
   copy ops\v32_stops_*.json "%DV3_DATA_DIR%\ops\"
   ```
   **RULE: cut over to `DV3_DATA_DIR` only on a FRESH UTC day, OR copy today's `v32_stops_*.json` as
   above.** The day-guard holds the current UTC day's latched S1/S2/S4 stops and legged-occurrence
   counts; if you point at an empty data dir mid-day WITHOUT copying it, a day that had already STOPPED
   could re-arm and fire again. (The mode file has NO checkout fallback and must be copied; the
   day-guard HAS a one-day safety fallback in code -- `run_v32` will use the checkout guard for today and
   log a loud line if the data-dir copy is missing -- but copying it is still the clean path.) If you
   skip `DV3_DATA_DIR` entirely, everything stays under the checkout exactly as today.
3. **Stop the old driver first** (so it cannot fire while the supervisor also runs):
   ```
   powershell -ExecutionPolicy Bypass -File ops\unregister_v32_task.ps1        # (drop -DryRun to apply)
   ```
4. **Register the two new tasks:**
   ```
   powershell -ExecutionPolicy Bypass -File ops\register_supervisor_tasks.ps1  # (drop -DryRun to apply)
   ```
   Both register with: trigger AT STARTUP, LogonType **S4U** (run whether logged on or not, no stored
   password), restart-on-failure 3x/1min, no battery restrictions, no execution time limit,
   MultipleInstances IgnoreNew.
5. **Start them now** (they otherwise wait for the next boot):
   ```
   Start-ScheduledTask -TaskName DegeneracyProxy
   Start-ScheduledTask -TaskName DegeneracyV3_Supervisor
   ```

`v32_mode.txt` still governs shakedown/dry/armed with no re-registration -- edit the file (in the data
dir if `DV3_DATA_DIR` is set, else `ops\v32_mode.txt`).

---

## 5. Verify one window ran

- Supervisor log: `logs_v32\supervisor.out` (or `$DV3_DATA_DIR\logs_v32\supervisor.out`) has one JSON
  line per window: `{"event":"window","wake":...,"pid":...,"exit_code":0,"duration_s":...,"status":...}`.
  A clean window is `exit_code":0`, `status":"exited"`. `"event":"boot_sweep"` shows the boot cancel
  result (with `boot_sweep proxy readiness attempt N/M` lines if the proxy was not up yet);
  `"event":"skipped_late"` means a wake overshot its window (rare; investigate suspend/clock);
  `"event":"child_watchdog_killed"` means a child overran its window close + 120 s and was killed (the
  supervisor then continued to the next :40).
- The child's own stdout AND stderr are redirected by the task into
  `logs_v32\supervisor.scheduler.out` -- run_v32 writes no per-window log file of its own, so a child
  crash/traceback is found there (not in `supervisor.out`, which carries only the supervisor's JSON).
- A new window journal appears under `journals_v32\` (or the data dir); old raw journals get `.jsonl.gz`.
- The V3.2 ledger row lands in `ledger\v32_ledger.jsonl` (or the data dir) as before; the V3.2 report
  and falsifier are unchanged.

---

## 6. Roll back to `DegeneracyV3_2`

In a :02-:33 window:
```
powershell -ExecutionPolicy Bypass -File ops\unregister_supervisor_tasks.ps1   # removes both new tasks
powershell -ExecutionPolicy Bypass -File ops\register_v32_task.ps1             # re-registers the hourly task
```
If you had set `DV3_DATA_DIR`, either clear it (so the hourly task reads/writes the checkout again) or
copy the live `v32_mode.txt` + latest day-guard back into the checkout `ops\`. Never leave both drivers
registered.

---

## 7. Render later

When V3.3 is ready to host, the move is only "create the services and set the env vars" -- no further
code. See `RENDER_MIGRATION_PLAN.md`: a private-service proxy + a background worker with a persistent
disk at `/var/dv3`, `DV3_DATA_DIR=/var/dv3`, `DV3_PROXY_BASE=http://<proxy-internal-host>:8642`, UTC
timezone, SIGTERM grace 300 s (the supervisor's is 240 s), deploys only in a :02-:33 window.

---

## 8. V3.3 dry ladder alongside V3.2 (Phase L2 -- the side-by-side watch)

Phase L2 adds a SECOND roster, `DegeneracyV3_3`, that runs the rolling K-rung ladder in DRY next to the
live V3.2. It **sends nothing**: it runs the full V3.3 core against the live feed, journals every order it
WOULD send, and SIMULATES ladder fills with the ideal rule (a spot-bucket YES-taker print at
`yes_price >= 1 - rung price` fills that rung; wings priced from the live strike book at fill time). Those
fills land in `ledger/v33_ledger.jsonl` as `dry_sim` -- clearly labelled, NEVER realised money. Brad's
words (2026-09-22): "run it along side V3.2 without it trading, then flip V3.3 to contracts 10 and V3.2 to
0. Just to watch and compare."

V3.3 keeps its own writable state, all routed through `service.paths` + `DV3_DATA_DIR` exactly like V3.2:

| thing | V3.2 | V3.3 |
|---|---|---|
| mode file | `ops/v32_mode.txt` | `ops/v33_mode.txt` (**absent -> dry**, never armed by default) |
| ledger | `ledger/v32_ledger.jsonl` | `ledger/v33_ledger.jsonl` |
| journals | `journals_v32/` | `journals_v33/` |
| logs | `logs_v32/` | `logs_v33/` |
| day guard | `ops/v32_stops_<day>.json` | `ops/v33_stops_<day>.json` |
| coid prefix | `v32-*` | `v33-*` (the sweep + venue read NEVER touch the other roster) |

### Run one V3.3 dry window by hand (sends nothing)
From `pilot\`:
```
python -m service.run_v33 --mode dry
```
(or via the supervisor: `python -m service.supervisor --roster v33 --once --now`). Confirm it sent nothing:
its journal (`journals_v33\<close>.jsonl.gz`) has only `would_place_rest`/`would_amend_rest`/
`would_cancel_rest`/`dry_sim_fill` records and no `place_rest`/`amend_rest`/`take_wings`; and the proxy
`GET /health` `orders_remaining_today` is unchanged before/after.

### Register the dry ladder as a third task (Brad, in a :02-:33 window)
```
powershell -ExecutionPolicy Bypass -File ops\register_supervisor_tasks.ps1 -WithV33 -DryRun   # preview
powershell -ExecutionPolicy Bypass -File ops\register_supervisor_tasks.ps1 -WithV33           # apply
```
`-WithV33` registers `DegeneracyV3_3` = `python -m service.supervisor --roster v33` (its own boot sweep is a
no-op in dry and, when armed later, sweeps ONLY `v33-*`). It is SEPARATE from the two base tasks; running
it alongside `DegeneracyV3_Supervisor` (V3.2) is expected -- the "never two drivers" rule is about two
drivers of the SAME roster, not the two different rosters.

### Read the comparison
```
python -m service.v33.report --days 3
```
prints the per-window LADDER block and a SIDE-BY-SIDE block (V3.2 realised/dry set lock vs V3.3
dry_sim/realised ladder lock, per hour both wrote a row, with running totals).

### The flip (Brad's hands only, in a :02-:33 window; NEVER between :38 and :59)
After the dry side-by-side proves V3.3 runs as expected (>= 2 days, PLAN_V33 step 4-5), arm V3.3 and stand
V3.2 down IN THE SAME WINDOW so only ONE roster is armed per bucket:
```
echo armed> ops\v33_mode.txt          # (or the DV3_DATA_DIR copy)
echo dry> ops\v32_mode.txt            # V3.2 stops placing; its falsifier closes with the Q4 line
```
**Before flipping to armed** (the arm gate is the L3 falsifier PLUS these, all CLEARED in code as of L2
Round 2):

- **MUST-FIX-1 wing chunking -- CLEARED.** The coalesced wing take is split into `ceil(count / cap)` IOC
  chunks of <= cap, so no oversized order is rejected and no filled rung is left naked. S5 now accepts a
  proxy `MAX_CONTRACTS_PER_ORDER` anywhere in `[lots_per_rung, K*lots_per_rung]` (= `[1, 11]`).
- **MUST-FIX-2 pre-place stall -- CLEARED.** The 0.5 s recheck now fires ONLY on a flagged anomaly; a
  healthy resting ladder proceeds on the first read (no per-create stall of the WS reader).
- **MUST-FIX-3 per-rung bucket -- CLEARED.** `RungFill` carries the bucket captured at fill time; the held
  bucket-NO leg + the settlement backfill price against the right market across a mid-window bucket change.
- **MUST-FIX-4 write pacing -- CLEARED.** A Basic-tier write-token pacer (100/s, create/amend 10, cancel 2)
  paces the 11-create ladder / chunked wing bursts; cancels + wing takes are priority (never queued).

**Brad's proxy levers (his hands only):**
- `MAX_CONTRACTS_PER_ORDER`: leave at **2** and the wings go as `ceil(K/2) = 6` IOC chunks per side; or
  raise to **11** and each wing goes as **1** order (fewer writes, but it also lifts the one-lot-per-rung
  guard, so weigh it). BOTH values arm (S5 accepts `[1, 11]`); the executor's `wing_cap` = `min(params
  max_contracts_per_order_hint = 11, the live proxy cap)`, read from `/health` at window start.
- The **amend cap** (`ops/proxy_amend_cap.md`, Brad's `.env` + restart): until applied, the roll runs
  cancel->create per order (the executor's default fallback) -- correct, just more orders; applying it flips
  the roll to amend-first with no code change.
- **`DAILY_ORDER_BUDGET` -> 8000** (PLAN_V33 Q6/step 1) so the ~200 orders/window + bucket-change re-places
  fit the day.

The V3.3 falsifier (`ceremony/v33_falsifier.md`, drafted in L3) MUST carry `STATUS: FROZEN` before S5 arms.

---

## 9. Brad's levers (nothing here is Claude's to pull)

- Registering / unregistering any scheduled task (`register_supervisor_tasks.ps1`,
  `unregister_v32_task.ps1`, `register_v32_task.ps1`).
- Unregistering `DegeneracyV3_2` when the supervisor takes over (never two drivers).
- Setting `DV3_DATA_DIR` / `DV3_PROXY_BASE` (they change where the runtime reads/writes).
- Editing `v32_mode.txt` (shakedown/dry/armed).
- Copying the mode file / day-guards at cutover.
- Anything on Render (workspace, services, disk, env, deploy button).
