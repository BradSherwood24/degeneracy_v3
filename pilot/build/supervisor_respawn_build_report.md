# Supervisor respawn hotfix -- build report

Branch: `fix/supervisor-respawn` (off `origin/main` c02b8d4). Builder: Opus 4.8. Offline, injected clocks.

## The bug (confirmed from live logs)

`Supervisor.run` (`pilot/service/supervisor.py`) wakes in the launch band `[:40:00, :60:00)`, spawns the
child window process, and waits for it. After the child exits with status `exited`, the loop returns to
the top; because `now` is still inside the band, `not in_launch_band(now)` is `False`, so it skips the
sleep branch and immediately spawns another child for the SAME close. At closes with no co-settling
$100 KXBTC range buckets (the 21:00Z close every day), `run_v33` stands down and exits 0 in ~0.6 s, so
the supervisor respawned it every ~0.6 s until :60. Measured spawns for the 21:00Z close: 1691 (09-23),
761 (09-25), 1110 (09-26), 353 today before the orchestrator killed it. Each spawn ran proxy discovery
GETs and appended a stand-down row to the V3.3 ledger (~3,500 duplicate 21:00Z rows accumulated).

## Fix (minimal)

One-run-per-close guard in `Supervisor.run`:

- New loop-local `last_close_run: float | None` (starts `None`). At spawn time it is set to
  `_next_top_of_hour_epoch(start)` -- the window close the child was launched for -- BEFORE `_wait_child`,
  so whatever status the child returns with (exited / watchdog_killed), that close is marked run.
- At the top of the loop, after the existing band/sleep block, a guard: if `in_launch_band(now)` AND
  `_next_top_of_hour_epoch(now) == last_close_run`, do NOT respawn -- emit
  `{"event": "sleep", "reason": "close_already_run", "close": <iso>, "wake": <iso>}` and sleep to
  `next_forty(now)` (the next hour's :40), exactly like the out-of-band path (same stop-aware
  `_sleep_until`, same `stop_idle` on interrupt), then `continue`.

Semantics preserved:
- `--now` (run_now) still forces the first window regardless of band (guard skipped on the run_now first
  pass; `last_close_run` is `None` there anyway).
- A genuine late first wake for a close that has NOT been run still runs once (`last_close_run` is `None`
  until the first spawn).
- `--once` unchanged (returns after one window before the guard can fire a second time).
- A child that exits early for ANY reason -- stand-down, crash, exit 1, watchdog kill -- gets exactly ONE
  run per close. Restart-on-failure remains the scheduled task's job, not the loop's.
- Launch-band arithmetic (`next_forty`, `in_launch_band`) and the watchdog are untouched.

Diff: `pilot/service/supervisor.py` +33/-2 (guard + `last_close_run` + docstring bullet 5).

## Tests

`pilot/tests/test_supervisor.py` (extends the existing fake-clock harness), 5 new tests:
- (a) `test_fast_standdown_in_band_runs_once_then_sleeps_close_already_run` -- child exits in ~0.6 s in
  band -> exactly ONE spawn, then a `sleep` event `reason=close_already_run`, `close=01:00:00Z`,
  `wake=01:40:00Z`, one `window` event.
- (b) `test_boot_inside_band_runs_current_close_once` -- boot at :45 (no --now) -> one window for the
  current close, no `close_already_run`.
- (c) `test_watchdog_killed_child_not_rerun_for_same_close` -- watchdog-killed child -> ONE spawn,
  `child_watchdog_killed` logged, window status `watchdog_killed`, no respawn for that close.
- (d) `test_normal_path_next_spawn_at_next_forty` -- child runs to close+, loop sleeps to the next :40 and
  spawns window 2 (spawns at 00:45:00Z and 01:40:00Z); no `close_already_run`.
- (e) `test_once_exits_after_one_window_with_guard` -- `--once` still exits after exactly one window.

Result: `python -m pytest tests/test_supervisor.py -q` -> 35 passed, 1 skipped (was 30 passed, 1 skipped;
the 1 skip is the POSIX-only real-SIGTERM test on Windows).

## Cleanup tool

`tools/dedupe_v33_ledger.py` -- rewrites `pilot/ledger/v33_ledger.jsonl` keeping, for each `close_time`,
only the LAST row (replaced in the first occurrence's slot so window order is preserved; rows without a
`close_time` pass through untouched, never deduped). It:
- writes a temp file in the same dir, fsyncs, then `os.replace` (atomic); keeps a `.bak` copy of the
  original first; serialises rows with the ledger writer's own `_json_default` (Decimal-aware, sort_keys).
- prints counts (`read / unique_close / removed / no_close_kept / written`).
- supports `--dry-run` (report only, no write, no time guard) and `--path`.
- REFUSES to run outside the safe UTC minute band `[:02, :33]` (a `run_v33` window may be mid-write;
  windows run :40 -> :00); `--force` overrides. Documented: run it between :02 and :33 UTC.
- idempotent (a second run over a clean file removes 0 and writes no `.bak`); missing file -> no-op;
  tolerates one truncated trailing line.
- NEVER touches the proxy or key material -- pure file rewrite. NOT run against the live ledger here.

Tests: `pilot/tests/test_dedupe_v33_ledger.py`, 11 tests (pure dedupe + slot preservation + idempotency;
safe-band boundaries; refuse-without-force; force override; dry-run no-write; real rewrite + backup on a
3,501-row synthetic file; already-clean no-bak; missing file no-op; truncated trailing line). All pass.

## Full suite

`python -m pytest -q` from the worktree `pilot/`: **1333 passed, 1 skipped** (main baseline 1317 passed,
1 skipped; +16 = 5 supervisor + 11 dedupe).

## Files
- `pilot/service/supervisor.py` (modified)
- `pilot/tests/test_supervisor.py` (modified)
- `tools/dedupe_v33_ledger.py` (new)
- `pilot/tests/test_dedupe_v33_ledger.py` (new)

## Round 2 (review of 95f3b75: APPROVE WITH NITS)

Reviewer verified the supervisor fix (old code: 1501 spawns in the guard test; new: 1). Two items addressed
as a second commit on `fix/supervisor-respawn`.

### F1 [HIGH, latent] -- backfill rows must never be collapsed; window-row conflicts must be refused

An ARMED window gets a WINDOW row (`run_v33.py` append at close) AND, later, a settlement BACKFILL row with
the SAME `close_time` (`build_v33_backfill_row`: `mode == "backfill"`, `backfill_of` set; carries only
settlement_results/payoff/floor_netted/realized_delta -- NO rung_fills/wing_batch_sets/ladder/lock_solved/
realized_lock). The reporter/falsifier read the window row (`report._is_window_row` = `mode != "backfill"`)
and the backfill separately. Round-1 keep-last would DESTROY the window row (latent only because the backfill
sweep skips `dry_sim` rows today).

Fix in `tools/dedupe_v33_ledger.py`:
- `_is_backfill(row)` (`mode == "backfill"` OR `backfill_of` present): such rows PASS THROUGH untouched, are
  never deduped and never used as a survivor.
- Collapse ONLY non-backfill rows: for each `close_time` keep the LAST non-backfill row in its
  first-occurrence slot. `dedupe_rows` now returns `(out, stats, conflicts)`; stats gains `backfill_kept`.
- Conflict guard: if a `close_time` has >= 2 non-backfill rows differing by more than `flushed_at` /
  `record_count` (`_IGNORE_ON_COMPARE`), `run` REFUSES with exit 2, writes nothing, and prints each
  offending close -- unless `--force` (which then keeps the last non-backfill row and logs the collapse).
  Exit codes are now: 0 ok/no-op, 1 time-band refusal, 2 window-row conflict refusal.
- Docstring rewritten to state the backfill contract and the conflict refusal.

New dedupe tests (`pilot/tests/test_dedupe_v33_ledger.py`, +4; existing direct-call tests updated to the
3-tuple + `backfill_kept`):
- `test_window_row_and_later_backfill_both_kept_order_preserved` -- window + later backfill (same close) ->
  BOTH kept, order [armed, dry, backfill], removed=0, backfill_kept=1.
- `test_backfill_never_used_as_survivor_for_standdown_storm` -- 3500 stand-downs + 1 backfill -> written=2
  ([dry, backfill]), removed=3499, backfill_kept=1.
- `test_conflict_two_different_window_rows_refused` -- two window rows differing in realized_lock -> exit 2,
  file untouched, no .bak, offending close printed; then `--force` collapses to the LAST (realized_lock 9.99)
  and writes the .bak.
- `test_no_conflict_when_only_flushed_at_and_record_count_differ` -- rows differing only in those two fields
  are NOT a conflict (collapse to 1).
CLI smoke-tested: a synthetic 2-window + backfill file returns exit 2, keeps the backfill, writes nothing.

### N2 -- strengthen the supervisor tests

Added `test_large_watchdog_grace_kill_in_next_band_runs_new_close_once`: `watchdog_grace_s=2500` so the kill
deadline (01:00 close + 2500 s = 01:41:40) lands INSIDE the 02:00 window's band. Asserts exactly TWO spawns
(00:45:00Z for close 01:00, watchdog-killed; 01:41:40Z for the NEW close 02:00, exited), `child_watchdog_killed`
logged, and NO `close_already_run` (different closes -> the guard keys on close epoch and does not suppress a
legitimate next-hour entry, nor re-run the killed close). This test and `test_normal_path_next_spawn_at_next_forty`
/ `test_fast_standdown_in_band_runs_once_then_sleeps_close_already_run` each advance the fake clock inside the
injected sleep (N2's "at least one" satisfied, now several).

### Round 2 counts
- `python -m pytest tests/test_supervisor.py -q`: 36 passed, 1 skipped (Round 1: 35+1).
- `python -m pytest tests/test_dedupe_v33_ledger.py -q`: 15 passed (Round 1: 11).
- Full suite from worktree `pilot/` (`python -m pytest -q`): **1338 passed, 1 skipped** (Round 1: 1333+1; +5 =
  1 supervisor + 4 dedupe). ASCII-only confirmed on all changed files.
