# Review -- PR #83 "V3.3 Phase H-A: host-shaped runtime"

- Branch: `feat/v33-phase-h`. Round 1 head `797f8a8` (base `origin/main b5059ee`); Round 2 head `956fb92`.
- Reviewer: Opus 4.8, worktree `dv3_wt_review` @ `review/v33-phase-h`.

## VERDICT: APPROVE (Round 1 was APPROVE WITH NITS; Round 2 clears the two substantive findings)

Behaviour-neutral for the live armed V3.2 with no env set (verified byte-identical both rounds). The
supervisor is dormant until Brad registers the task. No forbidden file touched.

---

# Round 1 (head 797f8a8) -- APPROVE WITH NITS

## What I verified (receipts)

- **Neutrality, computed both sides.** HEAD helpers with `DV3_DATA_DIR`/`DV3_PROXY_BASE` unset return
  (relative names): `journals_v32`, `logs_v32`, `ledger/v32_ledger.jsonl`, `ops/v32_mode.txt`,
  `ops` (day-guard dir), `ceremony/v32_falsifier.md`, `ops/journal_keep.txt`, proxy
  `http://127.0.0.1:8642`. `git show origin/main:` literals for `run_v32.py` and `v32/ledger.py` match
  exactly. `ProxyAuth(base_url=proxy_base_url)` with no flag/env == old `ProxyAuth()` default.
- No import cycle (`paths.py` imports only `os`). Only runtime writer under `ops/` is
  `stops._write_day_guard`, reached via `ops_dir` arg -> `run_v32` passes `ops_dir_v32()`.
  `box_report.py`/`run_window.py` keep `_PILOT_DIR/ops` (box path, out of Phase H-A scope).
- Boot sweep reuses `cancel_stale_open_orders`: only `KXBTC*`, skips foreign coids, `?exchange_index=`
  via `cancel_path` (09-14 fix present). `--dry-sweep` (`_DryDeleteWriter`) emits no DELETE. Rotation
  guards the current window (exclude-set + 30-min min-age; bounded 3 files / 60 s). Child cmd == old
  task; no PIPE. Signals forward->grace->kill; Windows CREATE_NEW_PROCESS_GROUP + CTRL_BREAK.
- PS 5.1 clean (real `-DryRun` returncode 0; no `&&`/ternary); S4U, ExecutionTimeLimit 0, RestartCount 3,
  battery flags, IgnoreNew, correct cwd, proxy action reads no `.env`, "never two" warning.

## Round-1 findings
1. **[NIT, strong]** Runbook copies the mode file at cutover but not the day-guard -- a mid-day
   `DV3_DATA_DIR` cutover after a latched S1/S2/S4 stop reads an empty data-dir guard -> a stopped day
   can re-arm. (`V33_RUNBOOK.md` step 2 / §6.)
2. **[QUESTION]** No max per-window child runtime -- a hung child stalls all future windows.
3. **[QUESTION]** Boot-sweep retry latency against a not-yet-ready proxy.
4. **[NIT, minor]** Vestigial `--log-dir`/`DEFAULT_LOG_DIR` in run_v32; child stderr co-mingles into
   `supervisor.scheduler.out`.

---

# Round 2 (head 956fb92, delta 797f8a8..956fb92) -- APPROVE

Delta: `run_v32.py` (+`_resolve_v32_guard_path`), `supervisor.py` (watchdog + boot-sweep readiness),
`V33_RUNBOOK.md`, build report, and 2 new test blocks. I reviewed only the delta.

## Finding #1 (day-guard cutover) -- RESOLVED
New `run_v32._resolve_v32_guard_path(utc_day)` (`run_v32.py:202-224`), used at BOTH live-arming-gate
sites (`:1919` guard read, `:2005` S1_LEGGED record). Verified:
- **Neutral, computed both sides.** With `DV3_DATA_DIR` unset, `data_dir() is None` -> returns
  `v32_day_guard_path(ops_dir_v32(), utc_day)`. Live check: got `...\pilot\ops\v32_stops_2026-09-22.json`
  == historic `v32_day_guard_path(os.path.join(_PILOT_DIR,"ops"), day)` -> **EQUAL True**. The loud
  `logger.warning` sits INSIDE the `data_dir() is not None` branch, so it **cannot fire in the no-env
  path** (the early `return primary` runs first).
- **Never raises.** Only `os.path.exists` (never raises) + pure string joins. Live check with
  `DV3_DATA_DIR=C:/no/such/dir/xyz` returns `...\ops\v32_stops_2026-09-22.json` without raising.
- **Guard-only fallback.** The checkout fallback is confined to this guard resolver; the mode file still
  routes through `mode_path_v32()` with no fallback (unchanged). Confirmed the resolver is never used for
  mode.
- **Day-string / filename format.** `utc_day = close_iso[:10]` = `YYYY-MM-DD`; both `primary` and
  `checkout` go through the same unchanged `v32_day_guard_path` -> `v32_stops_YYYY-MM-DD.json`, matching
  the existing on-disk convention exactly.
- **Coherence.** Fallback triggers only when `primary != checkout and not exists(primary) and
  exists(checkout)`. Once the data-dir guard exists it wins (no double-count); if `DV3_DATA_DIR` resolves
  to the checkout (`primary == checkout`) the branch is skipped. Reads and writes for the day stay on one
  authoritative file. Tests `test_guard_*` (4) cover no-env, fallback-to-checkout, data-present-wins,
  fresh-day-uses-data -- non-hollow.

## Finding #2 (watchdog) -- RESOLVED
`WATCHDOG_GRACE_S=120`; `_wait_child(child, watchdog_deadline)` where
`watchdog_deadline = _next_top_of_hour_epoch(start) + 120` (`supervisor.py:544`). Verified:
- **Cannot kill a healthy child before close+120 s.** The deadline is the child's window close (next :00
  after the ~:40 spawn) + 120 s; `run_v32`'s own deadline is close+10 s, so a healthy child always exits
  well before. Trip condition is `self._clock() >= watchdog_deadline`. Late in-band spawns (e.g. :59:30)
  still deadline at close+120. `test_healthy_child_never_trips_watchdog` confirms status `exited`.
- **Shutdown-signal priority.** In the loop, `if self._stop_requested:` is checked BEFORE the watchdog
  deadline check, so SIGTERM/SIGINT/SIGBREAK always wins and the supervisor exits (status `signaled`);
  the watchdog only continues to the next :40.
- **`status` field doesn't break parsers.** The window record adds `status` while KEEPING
  `signaled` (`status == "signaled"`). No code in the repo parses `supervisor.out`/the window record
  (grep clean), so the added field + the new `child_watchdog_killed` event line are backward-compatible.
- **No busy-loop.** Each iteration blocks in `child.wait(self._child_poll_s)` (1.0 s); poll interval
  unchanged. `test_child_watchdog_kills_hung_child_and_continues` confirms forward(SIGTERM)+kill+continue.

## Finding #3 (boot-sweep readiness) -- RESOLVED
`_proxy_ready` + `_boot_sweep_wait_ready` (`supervisor.py:226-270`), `BOOT_SWEEP_MAX_ATTEMPTS=4`,
`BOOT_SWEEP_RETRY_INTERVAL_S=2.0`. Verified:
- **GET /health only, read-only.** `requests.get(base.rstrip('/') + '/health', timeout=2)`, ready iff
  `< 500`; any transport error -> not ready. No writes, no key material.
- **Skipped in dry/shakedown (mode).** For a non-`--dry-sweep` call, `if not dry_sweep and mode !=
  "armed": return skipped` runs BEFORE `_boot_sweep_wait_ready`, so no proxy is probed or touched.
  `test_boot_sweep_skips_readiness_and_proxy_when_not_armed` asserts `probed == []`. (The explicit
  `--dry-sweep` inspection still probes + lists, read-only, as intended.)
- **Bounded.** At most 4 attempts, 3 × 2 s sleeps between them (no sleep after the last), then it
  proceeds regardless (the sweep is already fail-closed). Tests `test_boot_sweep_wait_ready_stops_when_ready`
  and `_gives_up_after_max_attempts` confirm the log/sleep counts.

## Finding #4 (minor) -- documented
Runbook §5 now notes the child's stdout/stderr lands in `supervisor.scheduler.out`. The vestigial
`--log-dir` remains (cosmetic; not this PR's job).

## Tests (Round 2 receipts)
- Full suite in this worktree: `1001 passed, 3 skipped, 2 errors`. The 2 errors + 2 of the skips are ALL
  `historical-data/` `FileNotFoundError` (`test_quintile.py` et al.) -- that corpus is absent in the
  review worktree; environmental, not a regression. This matches the expected `1005 passed, 1 skipped`
  modulo the missing corpus (1001 + 2 would-pass + 2 data-skips).
- Phase-H tests alone: `44 passed, 1 skipped` (the POSIX-only real-SIGTERM test skips on Windows).
- No forbidden file in `797f8a8..956fb92` (only report/runbook/run_v32/supervisor/tests).

## Round-2 nits (non-blocking)
- None that block. The `_proxy_ready` `requests` import is lazy (inside the function), so no new
  top-level dependency is imposed on run_v32's neutral import path.
