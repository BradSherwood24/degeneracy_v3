# Daily Replay + Tripwire -- Opus 4.8 review

Reviewer: Opus 4.8 (delegated). Worktree: C:\Users\Brads\Python_stuff\dv3_wt_review
Target: git diff ba1694f d7afce3 (branch ops/daily-replay, detached at d7afce3).
Date: 2026-09-27.

## VERDICT: APPROVE WITH NITS

The math is correct and reproduces the expected tripwire numbers to the digit; house law
(offline, only *.jsonl.gz, sealed/holdout hard-refused, no proxy, nothing touches the live
tree) holds; both suites are green. Findings below are nits and documented limitations, none
blocking. No correctness bug found.

--------------------------------------------------------------------------------
## 1. Tripwire math on the real seed data -- CONFIRMED

Seed (READ-ONLY): ...\scratchpad\v32_replay_all\ (fills 635 rows, per_window 245 rows).
Base cell = model=="lag", E=="0.10", lock_c not null -> 172 base fills, span 2026-09-14..-26.

Independently recomputed AND cross-checked against the actual module (tools.daily_replay):

  as_of 2026-09-26:
    7-day n=81  mean=+5.014c  median=+9.40c  neg=24/81  drift_p90=+19.15c
    3-day n=27  mean=-5.683c  median=-6.56c  neg=24/27
    -> VERDICT WATCH (3d n>=8 and mean < +2.0c; 7d mean +5.01c still > kill), exit 2.

  as_of 2026-09-23:
    7-day n=99  mean=+10.421c  neg=0/99
    3-day n=9   mean=+9.172c   neg=0/9
    -> VERDICT OK (both means > +2.0c), exit 0.

Both match the expected numbers in the task brief (7d n=81 ~+5.0c; 3d n=27 ~-5.7c, 24 neg ->
WATCH; 09-23 OK). A `--seed-from` run (module output paths redirected to scratch, journals dir
EMPTY -- never pointed at live journals) reproduced verdict=WATCH, rc=2, 7d n=81/+5.01c,
3d n=27/-5.68c, fills_all=635, and correctly marked 0 journals done (empty dir -> no basenames
match the seeded closes).

Drift formula: `_wing_drift_c` (daily_replay.py:230-239) = (W_completion - (2 - BASE_E - n)) * 100.
Spot-checked: W=1.3306, n=0.54, E=0.10 -> module and manual both -2.94c. Uses BASE_E (0.10)
constant, which is exactly right because `_base_fills` already restricts to E==0.10. Correct.

Day bucketing: `_to_date(f["close"])` = date.fromisoformat(close[:10]) -- i.e. the UTC close
date. Trailing windows are [as_of-6, as_of] and [as_of-2, as_of] inclusive (compute_tripwire:275).
Confirmed by test_tripwire_trip_7day_kill (per_day_last7 == 2026-09-21..27).

--------------------------------------------------------------------------------
## 2. Manifest / incremental logic

- Grows (size change): re-selected. select_new (daily_replay.py:137) treats basename with a
  different size as new. Covered by test_select_new_incremental_and_size_change. GOOD.
- Deleted after being marked done: list_gz_journals omits it -> never re-run, no error; stale
  manifest entry is harmless. Deleted between select and run: lab reports it "missing" and
  continues; mark_done getsize -> OSError -> size=None recorded (re-selected if it returns). GOOD.
- Mid-write / rotation: rotation is at :40, the runner fires 10:10 UTC, so the just-closed
  window is still a live `.jsonl` (ignored -- only `*.jsonl.gz` are listed) and its `.gz`
  appears at 10:40 -> picked up the NEXT day. Confirmed. If a `.gz` is genuinely mid-write at
  scan time, gzip read raises -> run_window raises -> caught by the generic except -> SKIP
  (nothing crashes); it is later re-selected when its final size differs. GOOD.
- --max-windows: eligible[:N], oldest first; each run marks its slice done so the next run
  advances (FIFO catch-up, `deferred` reported). No starvation, no loop (single pass). GOOD.

NIT 2a (fill dedupe key, low likelihood): `_fill_key` = (close, model, E, tol, deb, trade_ts)
(daily_replay.py:173). In the seed data tol/deb are null for every ideal/lag/old row, so within
the base cell the key reduces to (close, trade_ts). Two DISTINCT prints in the same window+cell
sharing one server `trade_ts` (different print_price/print_size/offer) would collide and the
second be dropped as a dup. Measured: 0 collisions across all 635 seed rows, so this is
theoretical -- but the key does not include print_price/print_size, so it cannot distinguish two
same-ts prints. If ever a concern, add print_price+print_size to the key. Not blocking.

NIT 2b (per_window key): `_pw_key` = (close_time or close, cell) but per_window rows carry NO
`cell` field, so the key is effectively (close_time, None). This is fine -- there is exactly one
per_window row per window -- and 0 collisions measured. The `cell` term is forward-looking (as
the code comment states). No action needed; noted for accuracy.

NIT 2c (skip accounting): when the lab SKIPS a partial journal but still exits 0, the daily
runner marks EVERY basename in `to_run` done (mark_done, daily_replay.py:491) including the
skipped one, at its current size. A permanently-corrupt `.gz` is thus silently dropped forever
(until its size changes) and the daily log line still says `replayed=<len(to_run)>`. The runner
only sees the lab's return code, not its skip count, so a skip is visible only in scheduler.out /
the lab's SKIP line. Behaviorally reasonable (you do not want to retry a corrupt file daily), but
the log/manifest over-report. Consider surfacing the lab's skip count. Not blocking.

--------------------------------------------------------------------------------
## 3. lab.py change (skip partial + --only/--files-from)

- Sealed/holdout still hard-refused: the per-window loop (lab.py:361-367) has
  `except SealedDateRefusal: raise` ORDERED BEFORE `except Exception`, so a sealed journal
  re-raises out of main() to a non-zero process exit; the daily runner then does NOT update the
  manifest and returns EXIT_ERROR. main() has no outer try/except that could swallow it
  (verified). SealedDateRefusal is defined in frames.py; assert_not_sealed covers both the SEAL
  (2026-08-02..-18) and the range holdout (2026-08-20..-29), acknowledge defaults False and the
  runner never passes it. HOUSE LAW HELD.
- Safe for --apply-to-forward / calibration: both operate on `results` (only successfully
  replayed windows); a skipped window simply does not contribute, which is more correct than the
  old behavior (a partial journal previously crashed the whole run). `if not results: return 1`
  (lab.py:373) fails loudly if EVERYTHING is skipped, and run_date = max(close of results) is
  safe because results is non-empty past that guard. GOOD.
  Residual risk (NIT 3a): a systematic parse regression that throws for a SUBSET of windows is
  now masked per-window (only a SKIP line), whereas before it aborted loudly. The total-failure
  guard catches the all-fail case; a partial regression could slip. Acceptable for an ops job.
- --only + --since: discover_journals(cal_dir, since=args.since) runs FIRST, then _filter_paths
  restricts to the wanted set (lab.py:341-347). They intersect: a wanted basename older than
  --since is reported "missing" and skipped. The daily runner passes no --since, so no conflict.
  Correct and backwards-compatible (wanted is None -> keep all). Covered by
  test_lab_wanted_basenames_and_filter.

NIT 3b (per-increment calibration): the daily runner replays only NEW windows, so each run's
calibration.json is aggregated over just those few windows rather than full history. This does
NOT affect the tripwire (computed from merged fills_all only) and the per-run calibration.json is
not consumed downstream by the runner, so it is cosmetic. Worth knowing if anyone reads a daily
run dir's calibration.json expecting a full-history aggregate.

--------------------------------------------------------------------------------
## 4. Pre-existing failure of test_cli_writes_outputs on main -- CONFIRMED

Checked out ba1694f's sim/v32_replay/lab.py + test_replay.py into THIS worktree only, ran the
single test, then restored to d7afce3 (worktree verified clean after):

  FAILED test_cli_writes_outputs -- ValueError: no window_meta record found in
  ...\pilot\tests\fixtures\v32\incident_20260914T220000Z_orderpath.jsonl.gz

git ls-tree ba1694f confirms the three incident_*.jsonl.gz fixtures (no window_meta) were already
present in the shared FIXTURE_DIR at the base commit. At ba1694f the lab had no skip logic, so the
un-pinned `--journals FIXTURE_DIR` glob replayed an incident fixture and crashed. The builder's
diagnosis HOLDS. The fix -- pinning `--only live_window_20260914T170000Z_head.jsonl.gz`
(test_replay.py:107-110) -- is the right, minimal fix (deterministic, does not lean on the new
skip behavior). GOOD.

--------------------------------------------------------------------------------
## 5. register_daily_replay.ps1

- 10:10 UTC -> local at registration (lines 66-70): (Get-Date).ToUniversalTime().Date preserves
  Kind=Utc, +10:10, .ToLocalTime() -> correct. Box is UTC-4 -> 06:10 local.
- DST (Nov 1 2026, EDT->EST): a -Daily trigger keeps its LOCAL time, so the fixed 06:10 local
  will fire at 11:10 UTC after the fall-back (offset -> UTC-5), a 1-hour drift. The script
  DOCUMENTS this (lines 17-19) and instructs a re-run after DST to re-pin. Documented limitation,
  not a bug -- as the task expected me to note.
- ExecutionTimeLimit 2h vs --max-windows 60 * ~50s = ~50 min: comfortably inside. FINE.
- Interactive logon (default): runs ONLY when Brad is logged on. Correct for a local-disk job and
  needs no admin shell; S4U (logged-off) needs an ADMIN PowerShell. Both stated. STATED.
- Working dir = repo root (line 88/120); command `python tools\daily_replay.py` is relative and
  resolves against it. PythonExe resolved via Get-Command python (line 59-62). GOOD.
- The registered action runs the REAL replay (no --dry-run in $runArg); the ps1's own -DryRun only
  previews registration. Correct intent (a morning job that actually replays).

NIT 5a (log growth): the scheduler redirect `>> scheduler.out` and daily_replay's append-only
`replay_daily.log` both grow unbounded. Trivial for a daily line, but no rotation. Note only.

--------------------------------------------------------------------------------
## 6. House law + hygiene

- Offline only: no proxy / 8642 / .pem / .env references in the new code (only the docstring line
  reaffirming "never the proxy"). No sealed_eval access. PASS.
- Reads only *.jsonl.gz: list_gz_journals filters `.endswith(".jsonl.gz")`; test asserts a live
  `.jsonl` is ignored. PASS.
- Sealed/holdout: refused (section 3). PASS.
- Nothing touches the live tree: all work was in dv3_wt_review; my --seed-from smoke test wrote
  only to scratch (module constants redirected). Worktree `git status` clean; no commits. PASS.
- ASCII-only: the builder's added lines are all ASCII (verified byte-scan of the diff). lab.py
  contains 41 non-ASCII bytes but ALL are PRE-EXISTING (em-dash / x / >= / minus / Delta in the
  report renderer at lines 1,89,195,199,229,253-260) -- none on added lines. Not introduced here.
- .gitignore: adds replay_tripwire.txt/.json + replay_daily.log; sim/out/ was already ignored
  (line 3), so fills_all/per_window_all/manifest/daily run dirs are runtime-only. All four
  artifact paths confirmed via `git check-ignore`. PASS.

--------------------------------------------------------------------------------
## Suite counts (measured)

- pilot/ (cd pilot; python -m pytest -q): 1278 passed, 5 skipped, in ~20 s.
  (Includes the 15 test_daily_replay + 3 test_register_daily_replay.)
- sim/v32_replay/tests (python -m pytest -q sim/v32_replay/tests from repo root): 23 passed.
  Note: `python -m pytest -q` at the REPO ROOT errors on a test_replay.py basename collision
  (pilot/tests/test_replay.py vs sim/v32_replay/tests/test_replay.py -- a pre-existing rootdir
  layout quirk, not from this diff); run the two suites separately as above.

--------------------------------------------------------------------------------
## Cosmetic

- WATCH reason string (compute_tripwire:293): "...7-day not yet at n>=15 or > kill" -- for the
  09-26 case the 7-day IS at n=81 and mean IS > kill, so the "or > kill" disjunct is the true one;
  wording is technically correct but slightly confusing. Harmless.
- Kill-line comparison uses the 3-dp rounded mean, so a true mean of 1.9996c rounds to 2.000 and
  is NOT < 2.0 -> no trip. Sub-0.001c boundary effect, negligible.
