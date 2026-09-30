# V3.3 falsifier L6 amendment -- build report

Branch `feat/v33-falsifier-l6` off `origin/main` (7728650). Brad's pre-freeze amendment to the DRAFT V3.3
falsifier, his verbatim words 2026-09-30 ~01:50Z:

> "Lets drop that realised lock to +4.0c, and lets add that daily loss of $3.00 as a early kill. Then lets
> also not lock any decision to an n over 15 fills."

Three changes, each mirrored doc <-> code, plus tests. The falsifier STATUS line is UNCHANGED
(`STATUS: DRAFT -- NOT FROZEN`); `policy/v33_params.json` and its sha are UNTOUCHED. V3.3 stays DRY.

## Change 1 -- verdict mean true-lock bar +6.0c -> +4.0c

- `service/v33/falsifier_pins.py`: `V33_FALSIFIER_MIN_MEAN_LOCK_CENTS` `Decimal("6.0")` -> `Decimal("4.0")`
  (comment updated to +4.0c). The early kill `V33_KILL_MEAN_LOCK_CENTS` (+2.0c) is UNCHANGED.
- `ceremony/v33_falsifier.md`: the verdict bullet (line ~100) "ladder mean true lock (realised, per
  contract) >= +6.0c [pin]" -> "+4.0c".
- `service/v33/report.py`: the gate value/threshold and the mean-lock gate comparison already derive from
  the pin (`>= +{V33_FALSIFIER_MIN_MEAN_LOCK_CENTS}c`), so no numeric literal changed there.

## Change 2 -- S4 day-loss ($3.00) becomes an EARLY / IMMEDIATE campaign KILL

The S4 cap ($3.00, `V33_S4_DAY_LOSS_CAP_DOLLARS` in `service/v33/stops.py`) is UNCHANGED and still halts
the UTC day exactly as before. L6 ADDS a campaign verdict: an S4 latch on ANY armed UTC day the report
covers forces the verdict to KILL, regardless of n (like the one-legged > 2 kill).

- `service/v33/falsifier_pins.py`: new pin `V33_KILL_ON_S4_DAY_LOSS = True` [pin], with a comment block.
- `service/v33/report.py`:
  - new import: `read_day_guard` (service.stops), `S4_DAY_LOSS` + `v33_day_guard_path` (service.v33.stops),
    `checkout_ops_dir` / `data_dir` / `ops_dir_v33` (service.paths), `V33_KILL_ON_S4_DAY_LOSS`.
  - new `_resolve_v33_guard_path(ops_dir, utc_day)` -- mirrors `run_v33._resolve_v33_guard_path`
    (DV3_DATA_DIR / checkout mid-cutover fallback), but only for the LIVE resolved ops dir; an explicit
    (test) ops dir is used verbatim.
  - new `_s4_kill_days(rows, ops_dir)` -- derives the report's UTC day range from the window rows'
    `close_time`, reads `ops/v33_stops_YYYY-MM-DD.json` per day via `read_day_guard`, returns the sorted
    days whose guard has a `latched` entry `kind == "S4"`. Returns `[]` when `ops_dir is None` (the
    file-free unit-test path) or the pin is False.
  - `build_falsifier_gate_table(rows, ops_dir=None)` -- new optional `ops_dir`. The kill list is seeded
    with `f"S4 day-loss latched on {day}"` for each S4 day BEFORE the mean/one-legged kills, so it fires
    at any n including n=0. Result dict gains `s4_kill_days`.
  - `build_v33_report(rows, ops_dir=None)` threads `ops_dir` into the gate table.
  - `_render_gate_table` VERDICT line now names "or S4 day-loss latched on any armed day" and prints an
    extra `S4 day-loss latched (campaign KILL, L6): <days>` line when it fires.
  - `main()` gains `--ops-dir` (default `ops_dir_v33()`), passed into `build_v33_report`.
- `ceremony/v33_falsifier.md`: new bullet under "## Kill (early / immediate)": "S4 day loss >= $3.00 [pin]
  latched on any armed day -> KILL (Brad 2026-09-30: the strategy should not lose; a day at the cap is not
  an exception to explain, it is the campaign's stop)", with the guard-file mechanism and
  `V33_KILL_ON_S4_DAY_LOSS = True [pin]`. The Stops S4 day-stop text is UNCHANGED.

Fail-closed note: this is the descriptive report, not the arming gate. A MISSING guard = no latch; a
CORRUPT guard yields no `latched` entries here (so no S4 kill from a corrupt file). The fail-closed-on-
corrupt discipline lives in `decide_v33_arming` (S5), which is untouched.

## Change 3 -- no decision locked to an n over 15 fills (verdict / promotion n 30 -> 15)

- `service/v33/falsifier_pins.py`: `V33_FALSIFIER_MIN_N` 30 -> 15; `V33_PROMOTION_MIN_N` 30 -> 15.
  `V33_KILL_MIN_N` stays 15.
- `service/v33/report.py`: no numeric literal -- the verdict/pending string and `min_n` already derive
  from `V33_FALSIFIER_MIN_N` (`f"n<{V33_FALSIFIER_MIN_N} pending (n={n})"`). No "30" literal survives in
  report.py except the date 2026-09-30.
- `ceremony/v33_falsifier.md` occurrences updated 30 -> 15 (verdict/kill/promotion only):
  - verdict intro (line ~95): `n >= 30` x2 -> `n >= 15`; `n<30 pending` -> `n<15 pending`.
  - capture fail-closed sentence (line ~106): "at `n >= 30` an UNMEASURABLE" -> `n >= 15`; "(Below
    `n >= 30` the gate reads `n-too-small`.)" -> `n >= 15`.
  - report-derivation sentence (line ~113): `n<30 pending` -> `n<15 pending`.
  - KILL "No re-spec" line (line ~110): "missed at `n >= 30`" -> `n >= 15`.
  - early-kill line (line ~158): "-> KILL without waiting for n=30" reworded to "-> KILL the moment
    `n >= 15` regardless of the other gates" (the stale n=30 is gone; the meaning "don't wait to score the
    full gate table" is preserved -- MIN_N now coincides with KILL_MIN_N).
  - Promotion (line ~166): "ALIVE at `n >= 30` [pin]" -> `n >= 15`.
- The per-rung shortfall's ">= 3 fills per rung" pin is UNCHANGED.

## L6 amendment section + runbook

- `ceremony/v33_falsifier.md`: new "## L6 amendment (2026-09-30, pre-freeze) -- Brad's verbatim words"
  section inserted directly ABOVE "## Registration", quoting Brad's line verbatim, listing the three old ->
  new changes with a one-sentence rationale each (structural lock so 15 contracts answer the execution
  question; +4.0c sits above the +2.0c early kill and below the 8c shallowest rung after ~5c slippage; a
  $3.00 day is the campaign's stop, not a data point).
- `ops/V33_RUNBOOK.md` line ~283: stale "will read `n<30 pending`" -> `n<15 pending`, with an L6 note.

## Deliberately NOT changed (history / append-only)

- The L4 amendment (2026-09-29) prose still reads "for the `n >= 30` verdict / kill / promotion gate
  counting" and "The gates themselves (mean lock +6.0c ...) are UNCHANGED". That is a DATED historical
  record of the L4 state; the doc's own convention (L4 keeps 5..15c history; L5 says "changes NOTHING in
  the frozen quantities above") is that amendment sections are time-layered deltas, and the task's
  enumeration of occurrences to change did not include it. The L6 section supersedes it. FLAG FOR REVIEW:
  if Brad wants the L4 prose annotated "(superseded by L6)", that is a one-line add -- I left it verbatim.
- `policy/v33_params.json`, the params sha, the falsifier STATUS line, mode files, tasks -- all untouched.

## Tests

- `tests/test_v33_falsifier_pins.py`: updated `test_promotion_pin_matches_doc` (== 15),
  `test_promotion_and_kill_constants_values` (MIN_MEAN_LOCK == 4.0); added
  `test_verdict_min_n_is_15_and_in_doc` (MIN_N == 15, doc has `n >= 15` / `n<15 pending`, and NO stale
  `n >= 30`/`n<30`/`n=30` in the Proposed-thresholds / Kill / Promotion live sections) and
  `test_s4_day_loss_kill_pin_matches_doc` (pin True, doc bullet + `V33_KILL_ON_S4_DAY_LOSS` reference).
- `tests/test_v33_report_scoreboard.py`: updated the two `n<30 pending` -> `n<15 pending` assertions and
  rewrote `test_gate_mean_lock_boundary_pass_and_fail` to the +4.0c bar (4c PASS/ALIVE, 3c FAIL/KILL);
  cosmetic comment fixes.
- `tests/test_v33_report_l6.py` (new): (a) S4 latch -> KILL with all gates green at n=15; S4 kill at n=0;
  S4 scanned across all report days; a non-S4 (S1_LEGGED) latch does NOT trigger it; pin True. (b) n=14
  pending / n=15 decided. (c) promotion pin 15. (d) mean +4.5c ALIVE / +3.5c KILL (and the +3.5c kill is
  the mean bar, not S4). Uses a tmp_path ops dir + `record_latched_stop` for the guard file.

Test line: `python -m pytest -q` -> **1374 passed, 1 skipped** in this worktree (main baseline 1364
passed, 1 skipped; +10 new tests). The 1 skip is environmental.

## Receipts

- pins: `V33_FALSIFIER_MIN_N=15`, `V33_FALSIFIER_MIN_MEAN_LOCK_CENTS=Decimal("4.0")`,
  `V33_PROMOTION_MIN_N=15`, `V33_KILL_ON_S4_DAY_LOSS=True`; `V33_KILL_MIN_N=15`,
  `V33_KILL_MEAN_LOCK_CENTS=Decimal("2.0")` unchanged.
- `V33_S4_DAY_LOSS_CAP_DOLLARS=Decimal("3.00")` unchanged in `service/v33/stops.py`.
- params sha unchanged: `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def`.
