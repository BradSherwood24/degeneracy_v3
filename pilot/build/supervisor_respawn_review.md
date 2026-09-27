# Opus 4.8 adversarial review -- supervisor respawn hotfix

Worktree: C:\Users\Brads\Python_stuff\dv3_wt_review (detached @ 95f3b75; base origin/main c02b8d4)
Scope reviewed: pilot/service/supervisor.py, tools/dedupe_v33_ledger.py,
pilot/tests/test_supervisor.py, pilot/tests/test_dedupe_v33_ledger.py (+ the build report).
Uncommitted; no push, no PR. python only. No proxy calls.

## VERDICT: APPROVE WITH NITS

The supervisor fix -- the thing that matters now, and that will drive V3.2 real money once
registered -- is correct on every path I traced and I reproduced the storm on the old code
(1501 spawns) collapsing to exactly 1 on the new code. Ship it.

ONE finding rises near-blocker but is LATENT (does not fire against today's all-dry V3.3
ledger): the dedupe tool's "keep the last row per close_time" silently destroys the window
row when an ARMED window later gets a settlement-backfill row (same close_time). It must be
guarded BEFORE V3.3 (or any armed roster) is deduped. Details in Finding 1.

---

## Finding 1 (HIGH, latent) -- dedupe collapses window+backfill pairs, destroying falsifier fields
tools/dedupe_v33_ledger.py:72-100 (dedupe_rows), :8-11 (docstring claim)

The docstring asserts "The V3.3 ledger is append-ONE-row-per-window by contract, so ... the
LAST row is the one to keep." That contract is FALSE for armed windows.

Confirmed multi-row-per-close semantics (same file, same close_time):
- Window row: run_v33.py:656 `append_v33_ledger_row(row, ledger_path)` -- carries the full
  economic payload (ladder, rung_fills, wing_batch_sets, floor_booked, held_legs,
  realized_unsettled=True, dry_sim, ...).
- Backfill row: run_v33.py:745 `append_v33_ledger_row(bf, args.ledger)` (same ledger). Built
  by ledger.py:466 build_v33_backfill_row with `"close_time": window_entry.get("close_time")`
  == the SAME close_time, plus `backfill_of`. It is SPARSE: roster, close_time, mode=backfill,
  backfill_of, armed, held_legs, settlement_results, settlement_payoff, floor_netted,
  realized_delta, legs_priced, backfill_note, realized_unsettled=False, flushed_at. It carries
  NO rung_fills / wing_batch_sets / ladder / floor_booked / lock_solved / realized_lock.

The backfill row is appended LATER (in a subsequent window's prepare(), run_v33.py:742-745),
so it is the LAST row for that close. dedupe_rows keeps the last (line 89 `out[slot[ct]] = row`)
-> it keeps the sparse backfill row and DISCARDS the full window row.

The v33 reporter/falsifier reads exactly the discarded fields off window rows:
- report.py:68 `_is_window_row` = `mode != "backfill"`; :81 `_row_rung_fills`;
- margin/kill stats read rung_fills.lock_solved / realized_lock (report.py:169-233, 295-325)
  and wing_batch_sets (report.py:325, 497-507).
Collapsing away the window row silently deletes that window from the falsifier's per-margin
lock census and from realised set-lock accounting.

Why it is only LATENT today: v33_settlement_backfill_sweep SKIPS dry_sim rows
(ledger.py:500 `if r.get("dry_sim"): continue`), and the live V3.3 ledger is all-dry, so no
close currently has two rows -> the immediate stand-down-storm cleanup is SAFE. The bug arms
the moment V3.3 arms (the flip gates) and any armed window settles, or if this generic tool is
ever pointed at an armed ledger. Given the house rule that every economic number is load-bearing,
fix before arming.

Suggested fix (one guard): in dedupe_rows, pass rows that carry `backfill_of` (or mode ==
"backfill") straight through UNTOUCHED (like the no-close rows at :83-86), and collapse only the
non-backfill window rows by close_time. For a no-bucket close every duplicate is a stand-down,
so keeping the last window row stays correct; a settled armed close then retains BOTH its window
row and its backfill row. (Also correct the docstring's "one row per close_time" claim.)

## Finding 2 (NIT) -- only 1 of the 5 new supervisor tests discriminates the fix
pilot/tests/test_supervisor.py:454-579

Old-code check (reverted supervisor.py to c02b8d4 in this worktree, ran the 5, restored):
- test_fast_standdown_in_band_runs_once_then_sleeps_close_already_run -> FAILS on old code:
  `assert 1501 == 1` (the storm reproduced to the same order of magnitude as the field
  measurement). This is the real guard test.
- test_boot_inside_band_runs_current_close_once, test_watchdog_killed_child_not_rerun_for_same_close,
  test_normal_path_next_spawn_at_next_forty, test_once_exits_after_one_window_with_guard -> PASS on
  old code too. They are non-regression / documentation tests, not discriminating.
The watchdog scenario in particular passes on old code because the default deadline (close+120s
= :02:00) lands OUT of the launch band, so the old loop would not have respawned there either.
The genuinely dangerous watchdog case -- a large grace whose kill lands INSIDE the next band --
is NOT covered by a test (I verified by reasoning it is handled: last_close_run holds the OLD
close, the new close differs, guard does not fire, the new close runs once). Consider adding it.
No test passes spuriously due to a frozen fake clock: every fake `_sleep` either advances the
clock or requests stop, and (a)'s failure on old code proves the clock advances.

## Finding 3 (NIT) -- float re-serialisation of `flushed_at` not byte-identical
tools/dedupe_v33_ledger.py:103-123 (_atomic_write) vs _load_rows

Decimal economic fields are stored AS STRINGS by the ledger writer, so they survive
json.loads -> json.dumps unchanged (round-trip verified; `_json_default` is effectively never
reached because loads never yields Decimal). But `flushed_at` is a raw float; loads->dumps may
reformat it. Not economic, not a correctness issue -- noting for completeness.

## Finding 4 (NIT) -- single .bak, and a corrupt interior line makes the tool refuse
tools/dedupe_v33_ledger.py:161-162, :54-69

`.bak` is a fixed name (path + ".bak") and shutil.copy2 overwrites it; a real second run that
found new dupes would overwrite the only backup. Mitigated: the idempotent early-return at
:157-159 means a no-op second run does NOT touch .bak. _load_rows raises on a malformed INTERIOR
line (only a truncated trailing line is tolerated). append_v33_ledger_row writes json then "\n"
as two calls, so a kill between them yields a newline-less line that the next append merges into
a corrupt interior line -> the tool would raise and refuse rather than corrupt. Fail-safe;
acceptable.

---

## Path walk (supervisor.py:553-621) -- all confirmed correct
(a) fast stand-down at :40:01 -> one spawn; guard fires (close_epoch==last_close_run, in band)
    -> emits sleep reason=close_already_run, _sleep_until(next_forty). If _sleep_until returns
    True (stop requested), emits stop_idle and returns 0 cleanly (:580-582). CORRECT.
(b) child runs past :00 -> top of loop now ~:00:20 out of band -> sleep-to-band -> at :40 the new
    close_epoch = next :00 differs from last_close_run (previous :00) -> guard does NOT block the
    new close (:574-576). CORRECT.
(c) watchdog kill at :02:00 -> out of band -> normal sleep-to-band, guard never reached. Large
    grace (e.g. 45 min) whose kill lands at :45 (in band): last_close_run is the OLD close,
    new close_epoch differs -> guard does not fire -> the new close runs once. CORRECT (untested;
    see Finding 2).
(d) --now first pass then fast exit in band -> first pass skips the band gate (:554), spawns,
    sets last_close_run; next pass hits the guard (same close, in band) -> no second spawn.
    CORRECT (proved by the len(spawns)==1 test).
(e) boot at :59:59 in band -> one run for the :00 close 1s away; pre-existing, unchanged. Silly
    (1s window) but not introduced by this diff.
(f) float epoch equality: both sides come from _next_top_of_hour_epoch = (floor(now/3600)+1)*3600,
    an exact multiple of 3600 (exactly representable). Same hour bucket -> exactly equal; different
    bucket -> differ by >=3600. No misfire. The child's own clock is irrelevant (guard compares
    only supervisor-computed values). CONFIRMED.

Crash visibility (task item 2): a child that crashes with exit 1 in band now runs ONCE per close;
the "window" event still logs exit_code=rc and status="exited" (:603-613), so a crash remains
visible (exit_code != 0). Restart-on-failure is delegated to the scheduled task, as intended.
Nit: stand-down (rc 0) and crash (rc!=0) both show status "exited"; only exit_code distinguishes.

Dedupe safety (task item 3): os.replace(tmp, path) with tmp in the same dir -> atomic on Windows;
the ledger append is a brief open-write-close, not a held handle, and the :02-:33 UTC band
(in_safe_band, minute on datetime.now(timezone.utc)) sits well clear of the :40->:00 window and
the ~:40 backfill append. --dry-run returns before any write and before the .bak copy (:153-155)
and skips the band guard (:138). dedupe_rows preserves first-occurrence order (:88-92). Verified.

## Test results (receipts)
- 5 new supervisor tests on NEW code: 5 passed, 31 deselected.
- 5 new supervisor tests on OLD code (c02b8d4 supervisor.py, then restored): 1 failed
  (fast_standdown ... assert 1501 == 1), 4 passed. Working tree restored clean (git status empty,
  git diff empty vs 95f3b75).
- Dedupe tests: 11 passed.
- Full suite from pilot/ (`python -m pytest -q`): 1329 passed, 5 skipped (total 1334).
  The 5 skips are environmental: test_box_golden.py:300/351 and test_quintile.py:74/94
  (historical-data corpus absent/gitignored in this worktree) + test_supervisor.py:323
  (POSIX-only SIGTERM). The build report's "1333 passed, 1 skipped" (also total 1334) reflects an
  environment where the 4 corpus-gated tests ran; none FAIL here -- the delta is skip vs pass, not
  a regression.
- Added lines are ASCII-clean (diff filter over c02b8d4..95f3b75: no non-ASCII).
- Scope: only the four listed files + the build report were changed.

---

# ROUND 2 -- commit bf087c0 (git diff 95f3b75 bf087c0)

## VERDICT: APPROVE

Round-1 Finding 1 (the HIGH-severity latent data-loss bug) is FIXED and verified. The conflict
guard cannot be tripped by the storm cleanup it was built for (proved against the live ledger).
The one gap I flagged in Round-1 Finding 2 (in-band large-grace watchdog untested) is now covered.
Suite green. Scope clean (supervisor.py untouched this round; only the dedupe tool, its tests, one
supervisor test, and the build report changed). Ship it.

## (1) Finding 1 scenario against bf087c0 -- FIXED
tools/dedupe_v33_ledger.py:_is_backfill (mode=="backfill" or backfill_of present), dedupe_rows
passthrough branch. Direct check on the actual bf087c0 function with a synthetic ledger
[armed window row (rung_fills/wing_batch_sets/ladder/floor_booked/held_legs), a duplicate
stand-down for another close, a backfill row SAME close_time, two more stand-downs]:
  stats read=5 unique_close=2 removed=2 no_close_kept=0 backfill_kept=1 written=3; conflicts=[]
  survivors: (13:00Z, armed), (21:00Z, dry), (13:00Z, backfill)
  - the armed WINDOW row survives with rung_fills / wing_batch_sets / ladder / floor_booked INTACT;
  - the backfill row also survives, IN ORDER (window row precedes backfill row);
  - the duplicate stand-downs collapse to one.
The reporter/falsifier fields the backfill row lacks are no longer destroyed. Fix confirmed.

## (2) Conflict guard cannot trip on the storm -- verified against the LIVE ledger (read-only)
Read C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v33_ledger.jsonl (4002 lines,
unmodified). The 2026-09-26T21:00:00Z close has 1109 rows, ALL non-backfill stand-downs
(mode="dry", stand_down=True, reason "no KXBTC range buckets co-settling at ..."). Full field diff
of two consecutive rows AND a distinct-value scan across ALL 1109 rows: the ONLY field that ever
varies is `flushed_at`. `record_count` is constant (0) here. Both are in
_IGNORE_ON_COMPARE = ("flushed_at", "record_count"), so _compare_key collapses all 1109 to one
variant -> conflicts=[] -> NO refusal. The tool will NOT choke on the very cleanup it was built for.
Positive control: a synthetic close with two genuinely different non-backfill rows
(lots_filled 0 vs 5) IS flagged; run() returns exit 2 and writes nothing without --force; with
--force it collapses to the LAST non-backfill row (lots_filled=5) and writes the .bak. Also noted:
the time-band guard is checked BEFORE the conflict guard, so a conflict outside [:02,:33] returns 1
(band) not 2 (conflict) -- correct precedence, not a bug.

## (3) --dry-run against a COPY of the live ledger (scratch dir; live path never written)
Copied the live ledger to the scratchpad and ran:
  python tools/dedupe_v33_ledger.py --dry-run --path <scratch>/live_ledger_copy.jsonl
  exit 0
  read=4002 unique_close=94 removed=3908 no_close_kept=0 backfill_kept=0 written=94
  no .bak written; copy still 4002 lines; live file untouched.
Zero conflicts across the ENTIRE live ledger (no close has two genuinely different window rows), so
a real cleanup would collapse 4002 -> 94 rows cleanly. backfill_kept=0 confirms the live ledger is
still all-dry (no backfill rows yet), consistent with the latent-bug framing in Round 1.

## (4) Tests / suite
- New dedupe + supervisor tests: 51 passed, 1 skipped (the POSIX-only SIGTERM test).
- Full suite from pilot/ (python -m pytest -q): 1334 passed, 5 skipped (total 1339). The 5 skips are
  the same environmental ones as Round 1 (test_box_golden 300/351 + test_quintile 74/94 =
  historical-data corpus absent; test_supervisor 323 = POSIX-only SIGTERM). The build report's
  "1338 passed, 1 skipped" is the same 1339 total in an env where the 4 corpus-gated tests ran;
  no failures here, delta is skip-vs-pass.
- N2 supervisor test (test_large_watchdog_grace_kill_in_next_band_runs_new_close_once,
  watchdog_grace_s=2500 -> kill at 01:41:40 inside the 02:00 band): traced and passing -- 2 spawns
  (01:00 watchdog-killed, then 02:00 the NEW close), no close_already_run. This closes the Round-1
  Finding 2 coverage gap. Note it is a "fix does not overreach" test (the guard keys on close epoch,
  so it does not fail on old code either -- old code had no such over-suppression); the sole
  storm-discriminating test remains test_fast_standdown... from Round 1.
- Round-2 added lines are ASCII-clean; scope limited to the four listed files (supervisor.py
  unchanged this round).

Round-1 nits 3 (flushed_at float re-serialisation) and 4 (single .bak) remain as-is; both are
non-economic / fail-safe and do not block.
