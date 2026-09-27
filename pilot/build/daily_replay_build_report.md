# Build report -- Daily Replay + Tripwire

Branch: `ops/daily-replay` (off origin/main ba1694f). Worktree: `C:\Users\Brads\Python_stuff\dv3_wt_v11`.
Brad, 2026-09-27: "lets get an automated process to run the replay every morning."

## What was built

An offline, incremental morning job that re-runs the V3.2 pump-fader over the ms journals and reports a
falsifier tripwire. It only REPORTS -- standing V3.2 down remains Brad's lever.

### New files
- `tools/daily_replay.py` -- the incremental runner. Manifest-driven selection (filename + size),
  merge/dedupe into rolling `fills_all.jsonl` / `per_window_all.jsonl`, trailing 7-day / 3-day
  tripwire, `--dry-run` / `--full` / `--max-windows` (default 60) / `--seed-from` / `--as-of`.
  `main()` never raises (logs + returns 1 on error). Exit codes 0 OK / 2 WATCH / 3 TRIP / 1 error.
- `pilot/ops/register_daily_replay.ps1` -- registers `DegeneracyReplayDaily`, daily 10:10 UTC (converted
  to local at registration; box UTC-4 -> 06:10 local), `-LogonType Interactive|S4U` (default
  Interactive), `-DryRun`, ExecutionTimeLimit 2h, MultipleInstances IgnoreNew, StartWhenAvailable,
  battery flags. Logs to `sim/out/v32_replay/daily/scheduler.out`.
- `pilot/ops/unregister_daily_replay.ps1` -- removes the task (`-DryRun` supported).
- `pilot/ops/DAILY_REPLAY.md` -- what it does, how to read the tripwire, verdict rules (+2c = the frozen
  V3.2 falsifier kill line), register/unregister, seed, and the "tripwire only reports" rule.
- `pilot/tests/test_daily_replay.py` -- 15 tests: filename<->close, manifest incremental selection +
  size-change re-select + live-.jsonl exclusion, max-windows guard, `--full`, fills/per_window
  merge-dedupe, tripwire OK/WATCH/TRIP + non-base-cell exclusion + wing-drift + percentile, seed import
  + idempotence, main() dry-run (no lab) and no-new-windows refresh, and the lab `--only`/`--files-from`
  + `_filter_paths` unit test.
- `pilot/tests/test_register_daily_replay.py` -- 3 ps1 `-DryRun` parse tests (mirrors
  `test_register_supervisor_tasks.py`).

### Changed files
- `sim/v32_replay/lab.py` -- backwards-compatible `--only` / `--files-from` subset option
  (`_wanted_basenames` + `_filter_paths`, both pure/unit-tested), and the main loop now SKIPS an
  unreadable/partial journal (no `window_meta` / read error) with a warning instead of aborting the
  whole run -- while still HARD-REFUSING sealed/holdout journals (`SealedDateRefusal` re-raised). This
  robustness serves the nightly runner's "never crash on one bad window" requirement.
- `sim/v32_replay/tests/test_replay.py` -- `test_cli_writes_outputs` now pins the head window with
  `--only`. This test was PRE-EXISTING BROKEN on main ba1694f: commit 09993f2 ("V3.2 hotfix: shard-aware
  cancels...") dropped partial incident fixtures into the shared `pilot/tests/fixtures/v32/` dir, and the
  test globs the whole dir. Confirmed the failure exists on unmodified `lab.py` (git-stash check). Fixed
  by targeting the intended full window.
- `.gitignore` -- ignore the three runtime tripwire artifacts (`pilot/ops/replay_tripwire.txt`,
  `replay_tripwire.json`, `replay_daily.log`), mirroring `pilot/ops/v32_mode.txt` etc.

## Test receipts

- `pilot/` full suite: **1282 passed, 1 skipped** (main was 1264 passed, 1 skipped; +18 from the two new
  test files).
- `sim/v32_replay/tests`: **23 passed** (was 22 passed + 1 pre-existing failure; the failure is now
  fixed).

## Behavior receipts (smoke, all in scratchpad -- no worktree artifacts)

End-to-end over the head fixture (`live_window_20260914T170000Z_head.jsonl.gz`):
- dry-run listed 1 window; real run invoked the lab subprocess, merged (+1 per_window), wrote the
  manifest (`{size:191623, replayed_at:...}`), tripwire, and log; the 2nd run correctly found no new
  windows and refreshed the tripwire.

Seed smoke on the orchestrator's scratchpad full run (`v32_replay_all/`: 635 fills, 245 windows;
journals dir left EMPTY so nothing in the live tree was read):
- `--as-of 2026-09-23`: 7-day n=99, mean +10.42c, 0 negative -> **OK**.
- `--as-of 2026-09-26`: 7-day n=81, mean +5.01c (still > kill line) but 3-day n=27, mean **-5.68c**,
  24/27 negative, wing drift p90 **+19.15c** -> **WATCH**. This matches the reported 2026-09-25/26 regime
  change (sweeps arriving with a 12-20c wing jump); the 3-day early warning fires days before the 7-day
  mean would cross the kill line.

## House law

Offline only (never the proxy). Reads only `*.jsonl.gz`. Sealed/holdout dates are hard-refused by the lab
(the runner never sets the acknowledge flag; skip logic re-raises `SealedDateRefusal`). No files in the
two open PRs' scope were touched (#93 record_window/run_v32/run_v33; #94 service/v33/*). Never pushed; no
PR opened (orchestrator does that after review).
