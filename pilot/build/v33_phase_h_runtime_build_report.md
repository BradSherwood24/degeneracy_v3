# V3.3 Phase H-A build report -- host-shaped runtime (supervisor + data dir + proxy base + rotation + task scripts + runbook)

> **Round 2 (PR #83 review) is at the bottom of this file** -- Findings 1-4 addressed; suite now
> `1005 passed, 1 skipped`.

Branch: `feat/v33-phase-h` (worktree `dv3_wt_v11`), based on `origin/main` b5059ee.
Scope: Phase H-A only. NOT touched (owned by other builders): proxy `.env` changes, `requirements.txt`,
`.python-version`, the GitHub Action (Phase H-B); `pilot/service/v33/` ladder core (L1). No V3.2 decision
logic, params sha pins, falsifier, or mode file changed.

## Behaviour-neutrality claim (the headline)

With **no env vars set**, every default path and behaviour is byte-identical to today:
- `python -m service.run_v32 --help` is BYTE-IDENTICAL before and after (diffed; no arg names or help
  text changed; new resolution is at argparse-default time, invisible to `--help`).
- Path constants verified equal to the exact historic literals with `DV3_DATA_DIR` unset (see
  `tests/test_paths.py::test_unset_matches_historic_literals` and
  `::test_run_v32_and_ledger_constants_unchanged_when_unset`):
  - `DEFAULT_JOURNAL_DIR == _PILOT_DIR/journals_v32`
  - `DEFAULT_LOG_DIR == _PILOT_DIR/logs_v32`
  - `DEFAULT_MODE_PATH == _PILOT_DIR/ops/v32_mode.txt`
  - `DEFAULT_V32_LEDGER_DIR == pilot/ledger`, `DEFAULT_V32_LEDGER_PATH == pilot/ledger/v32_ledger.jsonl`
  - `DEFAULT_FALSIFIER_PATH` unchanged (still a literal `_PILOT_DIR/ceremony/v32_falsifier.md`).
- Proxy base: `args.proxy_base or default_proxy_base()`; with flag unset and `DV3_PROXY_BASE` unset this
  is `http://127.0.0.1:8642` -- identical to the old `ProxyAuth()` default and the old
  `proxy_base_url = args.proxy_base or "http://127.0.0.1:8642"`.
- `proxy_auth.DEFAULT_PROXY_BASE` was intentionally LEFT as the literal `http://127.0.0.1:8642`; the env
  default is applied at the run_v32 / supervisor call sites, so the proxy module stays byte-neutral for
  every other importer. (Open question #1 below.)

The only behavioural changes are OFF by default and gated on env vars / the new supervisor process.

## What changed / new

New files:
- `pilot/service/paths.py` (124 lines) -- single helper for writable-path + proxy-base resolution.
  `data_dir()`, `journal_dir_v32()`, `log_dir_v32()`, `ledger_dir_v32()/ledger_path_v32()`,
  `ops_dir_v32()`, `mode_path_v32()`, `supervisor_log_path()`, `journal_keep_path()`,
  `falsifier_path_v32()`, `default_proxy_base()`. Blank env is treated as unset.
- `pilot/service/supervisor.py` (507 lines) -- the wake loop. Pure helpers `next_forty`,
  `in_launch_band`; `PopenChild` adapter; `_default_boot_sweep` (reuses
  `executor.cancel_stale_open_orders`), `_DryDeleteWriter` for `--dry-sweep`, `_default_rotate` (reuses
  `journal_io.rotate_closed_journals`); the `Supervisor` class with fully injectable clock/sleep/spawn/
  sweep/rotate/on_event; `build_parser`/`main`. Flags: `--once --now --dry-sweep --proxy-base
  --child-args ...`.
- `pilot/ops/register_supervisor_tasks.ps1` (124) + `unregister_supervisor_tasks.ps1` (42) -- two tasks
  (`DegeneracyProxy`, `DegeneracyV3_Supervisor`), AtStartup, LogonType S4U, restart 3x/1min, no battery
  restriction, `-ExecutionTimeLimit 0`, `-MultipleInstances IgnoreNew`; `-DryRun`; warns to unregister
  `DegeneracyV3_2`.
- `pilot/ops/V33_RUNBOOK.md` (161) -- local run, env vars, cutover in a :02-:33 window, verify, rollback,
  Render note, Brad's levers.
- Tests: `tests/test_paths.py` (8), `tests/test_supervisor.py` (25, incl. 1 Windows-skip),
  `tests/test_register_supervisor_tasks.py` (2).

Modified (routing only, neutral):
- `pilot/service/run_v32.py` -- imports from `service.paths`; the 3 writable path constants + 4 argparse
  defaults route through helpers; two `ops_dir = os.path.join(_PILOT_DIR, "ops")` -> `ops_dir_v32()`;
  proxy base resolution uses `default_proxy_base()`; dropped the now-unused `DEFAULT_V32_LEDGER_PATH`
  import (argparse `--ledger` now uses `ledger_path_v32()`).
- `pilot/service/v32/ledger.py` -- `DEFAULT_V32_LEDGER_DIR/PATH` route through `service.paths`.

## DV3_DATA_DIR routing (deliverable 2)

Writable locations enumerated by grepping `_PILOT_DIR`/writes in `service/`: journals, logs, ledger dir,
mode file, and the day-guard/stops JSON (`ops/v32_stops_YYYY-MM-DD.json`, written via
`stops._write_day_guard` which `os.makedirs` its dir). All now resolve under `$DV3_DATA_DIR` when set.
Read-only inputs stay in the checkout: `policy/v32_params.json`, `ceremony/v32_falsifier.md`,
`ops/journal_keep.txt` (the rotation keep-list). **Mode file decision (documented in the runbook):** when
`DV3_DATA_DIR` is set the mode file lives in the DATA DIR, full stop -- NO checkout fallback; Brad copies
it once at cutover.

## Supervisor semantics (deliverable 1)

- Boot: run the cancel sweep ONCE (armed-only real cancels; dry/no-proxy = logged no-op; `--dry-sweep`
  lists would-cancels via `_DryDeleteWriter`, reusing the exact sweep filtering).
- Wake: `next_forty` computes the next UTC :40; a late wake still in [:40,:60) runs the current hour; a
  wake overshot into [:00,:40) logs `skipped_late` and recomputes. `--now` skips the first sleep;
  `--once` runs exactly one window.
- Rotation at every wake BEFORE spawning: `rotate_closed_journals` on `journal_dir_v32()` with the same
  bounds run_window uses; current window's raw journal is protected by the closed-set exclude + 30-min
  min-age (verified in `test_default_rotate_skips_current_and_gzips_old`).
- Child: `python -m service.run_v32` from the pilot dir, env inherited (no baked-in mode/path flags),
  `--child-args` passthrough. One JSON log line per window (wake, pid, exit_code, duration, signaled,
  rotated count) to `supervisor_log_path()` + logger.
- Signals: SIGTERM/SIGINT/(Windows)SIGBREAK. Idle -> exit 0 promptly (interruptible sleep). Busy ->
  forward to child, wait up to `SIGTERM_GRACE_S=240` (< Render's 300), hard-kill on overstay, exit with
  the child's code. Windows child created with `CREATE_NEW_PROCESS_GROUP` so `CTRL_BREAK_EVENT` reaches
  it; POSIX forwards the received signal.

## Tests / verification (receipts)

- Full suite BEFORE (main state in this worktree): `961 passed in 26.44s`.
- Full suite AFTER: `996 passed, 1 skipped in 23.29s` (+35 tests; the 1 skip is the POSIX-only real-
  SIGTERM delivery test, skipped on Windows with a reason -- the only skip added).
- `python -m service.run_v32 --help` diff before/after: IDENTICAL (checked twice).
- `python -m compileall service tests`: clean. (pyflakes not installed on this box; compileall used.)
- Both PowerShell scripts run under real `powershell -File ... -DryRun` with returncode 0 (the tests
  execute them, so PS 5.1 syntax is validated, not just text-matched).
- Path env round-trip verified live (unset == historic literals; set == under data dir; keep-list +
  falsifier stay in checkout; proxy base unset/set/blank).

## Could not verify / open questions for the reviewer

1. **`proxy_auth.DEFAULT_PROXY_BASE` left literal.** RENDER plan 3f suggests also honouring the env in
   `proxy_auth.DEFAULT_PROXY_BASE`. I deliberately did NOT, to keep that module byte-neutral for all
   importers; the env is applied where run_v32/supervisor construct `ProxyAuth`. run_window.py still
   hard-codes `http://127.0.0.1:8642` (V3.2/box path, out of Phase H-A scope). Confirm this call-site
   approach is preferred over mutating the module constant.
2. **Real armed boot sweep and real signal on Linux were NOT exercised** (house law: no supervisor run
   against the real proxy with orders; no Linux host here). Covered by injected fakes + the POSIX stub-
   child test which is skipped on this Windows box. The armed cancel path itself is the existing,
   already-tested `cancel_stale_open_orders`.
3. **S4U vs Password logon** documented as a tradeoff in the script header; S4U chosen (no stored
   credential, localhost-only needs). If Brad later needs networked resources under his identity, switch
   to Password.
4. **`--child-args` uses `argparse.REMAINDER`** so it must be last on the command line (documented). No
   child args are passed by default (mode/paths come from the mode file / env), matching the old task.
5. The supervisor imports `executor`/`proxy_writer` lazily inside the sweep, and reads the mode file
   directly (replicating run_v32's tiny `resolve_v32_mode`, with `_VALID_MODES` mirrored) so the long-
   running process need not import run_v32's asyncio/websocket stack. If the reviewer prefers importing
   the canonical `resolve_v32_mode`, that is a one-line change (accepting the heavier import).

## Not done (by design / other builders)

`requirements.txt`, `.python-version`, proxy `.env`/`PROXY_HOST`/budget-path env, GitHub Action (Phase
H-B); `service/v33/` ladder (L1). No PR opened (orchestrator opens; Brad merges).

---

## Round 2 -- PR #83 review response (APPROVE WITH NITS)

Review at `pilot/build/v33_phase_h_runtime_review.md` (branch `review/v33-phase-h`). All four findings
addressed. Behaviour-neutrality preserved: `run_v32 --help` still BYTE-IDENTICAL to origin/main; with no
env set every default path/behaviour is unchanged (the new guard fallback is a no-op when
`DV3_DATA_DIR` is unset -- `data_dir() is None` returns the historic `ops_dir_v32()` path).

### Finding 1 [must fix] -- day-guard dropped on a mid-day DV3_DATA_DIR cutover
Fixed BOTH ways.
- (a) Runbook `V33_RUNBOOK.md` §4 step 2: the cutover copy now includes `copy ops\v32_stops_*.json
  "%DV3_DATA_DIR%\ops\"`, plus an explicit RULE: cut over to `DV3_DATA_DIR` only on a fresh UTC day or
  copy today's guard. Rollback §6 already covered copying the guard back.
- (b) Code safety in `run_v32._resolve_v32_guard_path(utc_day)` (new): when `DV3_DATA_DIR` is set and the
  data-dir guard for today is MISSING while the CHECKOUT `ops/v32_stops_<day>.json` EXISTS, it uses the
  checkout guard as authoritative for that day and logs a loud `[V32]` warning. Read-only-style fallback
  for the GUARD ONLY -- never the mode file. Both guard call sites in `run_v32` (arming gate + S1_LEGGED
  record) now route through it, so reads and writes stay consistent on the cutover day. With
  `DV3_DATA_DIR` unset it returns `v32_day_guard_path(ops_dir_v32(), utc_day)` == the historic
  `os.path.join(_PILOT_DIR,"ops")` path (byte-identical). Tests:
  `test_paths.py::test_guard_no_env_uses_ops_dir`, `::test_guard_fallback_to_checkout_when_data_missing`,
  `::test_guard_uses_data_when_data_guard_present`, `::test_guard_fresh_day_uses_data`.

### Finding 2 -- per-window watchdog for a hung child
`supervisor._wait_child` now takes a `watchdog_deadline` (= the child's window close `:00` +
`WATCHDOG_GRACE_S`, default 120 s; run_v32's own deadline is close + 10 s). If the child is still running
past it, the supervisor forwards SIGTERM/CTRL_BREAK, waits the SIGTERM grace, hard-kills, emits a
`child_watchdog_killed` event with the exit code, and CONTINUES to the next :40 (a shutdown signal still
takes priority and exits). `_wait_child` returns `(rc, status)` where status is `exited|signaled|
watchdog_killed`; the window record carries `status` (and keeps `signaled` for back-compat). Tests:
`test_supervisor.py::test_child_watchdog_kills_hung_child_and_continues` (stub child that never exits +
an injected clock advanced past the deadline), `::test_healthy_child_never_trips_watchdog`.

### Finding 3 -- boot-sweep proxy-readiness retry made explicit
New `supervisor._boot_sweep_wait_ready(proxy_base, *, max_attempts, retry_interval_s, ready_fn, sleep,
log)`: polls `GET /health` up to `BOOT_SWEEP_MAX_ATTEMPTS` (4) times `BOOT_SWEEP_RETRY_INTERVAL_S` (2 s)
apart, logging ONE line per attempt, returning as soon as ready. `_default_boot_sweep` calls it before
any proxy-touching sweep (skipped entirely when mode != armed and not `--dry-sweep` -> no proxy probed).
Params are exposed on `Supervisor` (`boot_sweep_max_attempts`, `boot_sweep_retry_interval_s`). This WRAPS
-- does not replace -- the existing bounded retry inside `cancel_stale_open_orders` -> `ProxyAuth.rest_get`.
Tests: `::test_boot_sweep_wait_ready_stops_when_ready`, `::test_boot_sweep_wait_ready_gives_up_after_max_attempts`,
`::test_boot_sweep_skips_readiness_and_proxy_when_not_armed`.

### Finding 4 -- runbook sentence on child stderr
`V33_RUNBOOK.md` §5: child stdout AND stderr are redirected by the task into
`logs_v32\supervisor.scheduler.out` (run_v32 writes no per-window log of its own), so a child
crash/traceback is found there, not in the JSON-only `supervisor.out`. `--log-dir`/`DEFAULT_LOG_DIR`
left as-is per the review (pre-existing, dead; not this PR's cleanup).

### Round 2 receipts
- Full suite: `1005 passed, 1 skipped in 28.86s` (Round 1 was 996 + 1; +9 tests). The 1 skip remains the
  POSIX-only real-SIGTERM test on Windows.
- `python -m compileall service tests`: clean. `run_v32 --help`: byte-identical to before (re-diffed).
- No forbidden files touched; no V3.2 params/falsifier/mode/decision changes; supervisor still dormant
  until Brad registers/runs it.
