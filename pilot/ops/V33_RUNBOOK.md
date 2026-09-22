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

## 8. Brad's levers (nothing here is Claude's to pull)

- Registering / unregistering any scheduled task (`register_supervisor_tasks.ps1`,
  `unregister_v32_task.ps1`, `register_v32_task.ps1`).
- Unregistering `DegeneracyV3_2` when the supervisor takes over (never two drivers).
- Setting `DV3_DATA_DIR` / `DV3_PROXY_BASE` (they change where the runtime reads/writes).
- Editing `v32_mode.txt` (shakedown/dry/armed).
- Copying the mode file / day-guards at cutover.
- Anything on Render (workspace, services, disk, env, deploy button).
