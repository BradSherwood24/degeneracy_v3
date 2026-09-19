# V3.2 build report -- fill-rate gate -> CAPTURE RATIO (MEASUREMENT CLARIFICATION 3)

Branch: `feat/capture-ratio-gate` (off `origin/main` = 5f22880). Worktree `dv3_wt_v11`. Not merged.
Brad's verbatim go (2026-09-19 ~22:40Z): "I agree with the second there. Looks like our dry spell has ended".

## What changed and why

The frozen falsifier's fill-rate gate (`live fill rate >= 2.0 sets/day`) measured the TAPE REGIME, not
our execution: June-July carried ~3x September's pump traffic, and the 2.0/day pin was itself calibrated
on the lean W36 forward week. Live is at n=7, mean lock +11.1c, 100% positive, exec gap -0.6c,
one-legged 0 -- but ~1.5 sets/day only because the pumps are scarcer. The ideal shadow already measures
pump AVAILABILITY, so `live fills / ideal-shadow fills at our threshold` isolates EXECUTION.

Registered at n=7 (before the n>=30 verdict), honouring the registered-specs rule.

## Definition (as built)

CAPTURE RATIO = (live completed sets) / (ideal-shadow E=0.10 fills inside the T-15..T-5 quoting window),
BOTH counted over ARMED windows carrying a spot bucket (`effective_mode == "armed"` AND a
`spot_bucket_ticker`). The shadow fills at most once per window (window-gate, PR #54), so the
denominator counts each qualifying window at most once. A window the live path stood down for ANY
pilot-side reason (replace_rate alarm, stale wing, phantom, cancel_failed) yields 0 completed sets and
so counts as a MISS. Windows the pilot never ran (box outage) leave no armed row and count for neither.

## Pin (Registration 3)

- `service/v32/falsifier_pins.py`: new `V32_CAPTURE_RATIO_MIN = Decimal("0.50")` (Claude's proposal;
  Brad confirms the 0.50 number at merge). The old `V32_FALSIFIER_MIN_FILL_RATE_PER_DAY = 2.0` stays
  DEFINED (add-only law); the verdict no longer gates on it, and the report still prints sets/day as
  info.

## Files touched

- `pilot/service/v32/falsifier_pins.py` -- new capture-ratio pin.
- `pilot/service/v32/report.py` -- `build_falsifier_scoreboard` computes `capture_live_sets`,
  `capture_shadow_fills`, `capture_ratio`; verdict at n>=30 gates on `capture_ratio >= pin` in place of
  fill rate; scoreboard prints `capture ratio = live X / shadow Y = Z%  (>= 50% [pin] Registration 3)`
  and labels the fill rate "(info, superseded as a gate by Registration 3; pin was 2.0/day)". JSON
  output is additive (three new keys). New `_pct` helper.
- `pilot/ceremony/v32_falsifier.md` -- appended Registration 3 (append-only; STATUS line and params sha
  untouched). The "Proposed pre-registered thresholds" fill-rate bullet is NOT edited in place: the
  doc's own freeze rule forbids editing threshold text above the Registration section, so (following
  MEASUREMENT CLARIFICATION 1's precedent, which likewise left the bullet untouched) the pointer lives
  in the Registration entry, which is authoritative -- at n>=30 the bullet is read as superseded.
- `pilot/ops/V32_ARMING.md` -- section D MUST-CONFIRM item 9 (scoreboard prints the capture ratio) +
  the section A step-5 report note.
- `pilot/tests/test_v32_falsifier_pins.py` -- ADD-ONLY: pin value, Registration-3 doc assertions,
  verdict-uses-capture-ratio (no existing assertion edited/deleted).
- `pilot/tests/test_v32_report_scoreboard.py` -- fixtures gained `spot_bucket_ticker`; the old
  `test_scoreboard_kill_on_low_fill_rate` became `test_scoreboard_low_fill_rate_no_longer_kills`
  (low fill rate whose availability was captured is ALIVE); new capture-ratio KILL / counting /
  exclusion / None / legacy-row / render tests.

## Add-only check on test_v32_falsifier_pins.py

No existing assertion pins the verdict to the 2.0/day fill-rate gate; the only fill-rate assertion is
`test_verdict_pins_match_doc`'s `">= 2.0 sets/day" in doc`, which still holds (the bullet text is
unchanged). Nothing in the add-only file contradicts this change, so nothing needed editing.

## Suite

`cd pilot && python -m pytest -q` -> 926 passed (main was 918; +8 new tests). ~31s.

## Scoreboards on a READ-ONLY copy of the live ledger

Source `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v32_ledger.jsonl` (124 rows) copied into
scratch, read-only; the live tree was never written.

### BEFORE (origin/main report)

```
  completed sets n = 7   (rest fills total = 7, one-legged = 0)   armed windows = 112   armed days = 112/24 = 4.67
  realized lock: mean +11.1c  median +11.4c  p10 +10.1c  min +10.1c
  %positive = 100.0   fill rate = 1.50/day
  shadow E=0.10: mean lock +10.5c   execution gap (shadow-live) -0.6c
  ...
  VERDICT: n<30 pending (n=7)
```

### AFTER (this branch)

```
  completed sets n = 7   (rest fills total = 7, one-legged = 0)   armed windows = 112   armed days = 112/24 = 4.67
  realized lock: mean +11.1c  median +11.4c  p10 +10.1c  min +10.1c
  %positive = 100.0   fill rate = 1.50/day (info, superseded as a gate by Registration 3; pin was 2.0/day)
  capture ratio = live 7 / shadow 12 = 58.3%  (>= 50% [pin] Registration 3)
  shadow E=0.10: mean lock +10.5c   execution gap (shadow-live) -0.6c
  ...
  VERDICT: n<30 pending (n=7)
```

Current CAPTURE RATIO = 7/12 = 58.3% (>= the 0.50 pin). The 5 misses of the 12 shadow-available
windows: 1 pre-PR#54 counting artifact (09-15 04Z), 2 rest-absent-at-print during replace churn
(09-15 14Z/15Z), 1 phantom-resting stand-down (09-15 18Z, PR #56), 1 replace-rate alarm stand-down
(09-18 14Z). Verdict stays `n<30 pending`; the ratio does not decide until n>=30.
