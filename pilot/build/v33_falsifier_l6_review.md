# V3.3 falsifier L6 amendment -- adversarial review

Reviewer: Opus 4.8 (Fable-delegated). Worktree `dv3_wt_review`, detached at `origin/feat/v33-falsifier-l6`
(`849fce2`, on `53d80a1`, off main `7728650`). Fixes/tests on branch `review/v33-falsifier-l6`.

Under review: Brad's pre-freeze L6 amendment (verbatim 2026-09-30: "Lets drop that realised lock to +4.0c,
and lets add that daily loss of $3.00 as a early kill. Then lets also not lock any decision to an n over 15
fills.") to the DRAFT V3.3 falsifier, mirrored in `service/v33/falsifier_pins.py` + `service/v33/report.py`
with tests.

## VERDICT: APPROVE WITH NITS -- after the fixes on `review/v33-falsifier-l6`

The three amendment changes are correct, faithful to Brad's words, doc<->code agree, and the STATUS line /
params sha / mode files / tasks are untouched. Brad's quote is byte-exact in the frozen doc. The one
material defect found is a **live-reachable `NameError` crash** the builder's tests could not see (F1) --
fixed here. Two builder-flagged gaps (F2 no-row day, F3 corrupt guard) are real; I agree and fixed both.
The doc-annotation the task required (F4) and the stale ancillary-doc gate statements (F5) are done. None
of these touch anything above Registration in a way that changes a pinned quantity -- the L4 prose keeps
its dated wording with a "(superseded by L6)" annotation, exactly as the task asked.

## Findings

### F1 (BUG, MUST-FIX -- fixed) -- `os` was never imported in `report.py`, but `_resolve_v33_guard_path` calls `os.path.exists`
`service/v33/report.py` used `os.path.exists(...)` in `_resolve_v33_guard_path` (the mid-cutover checkout
fallback) with no `import os`. Proven at runtime: forcing the fallback branch (`data_dir()` not None and
`ops_dir == ops_dir_v33()`) raises `NameError: name 'os' is not defined`. The whole suite missed it because
every unit test passes an EXPLICIT tmp ops dir (`ops_dir != ops_dir_v33()`), which returns early at the
guard-clause before line 365 -- so the crash only appears in LIVE operation, in exactly the checkout
safety path this function exists to provide. In `main()` (`--ops-dir` default `ops_dir_v33()`), with
`DV3_DATA_DIR` set, `python -m service.v33.report` would crash while computing the L6 S4 scan.
Fix: `import os` (+ `glob`, `re`). Regression test `test_resolve_guard_path_checkout_fallback_does_not_raise`
exercises the fallback branch (primary missing, checkout present) and asserts the checkout path is returned.

### F2 (gap 2a, agreed -- fixed) -- an S4 latch on a day with no ledger row was invisible
`_s4_kill_days` derived its day set from window rows only, so a day that latched S4 but produced no ledger
row (process died after latching, or the row was deduped) escaped the campaign kill. Replaced with
`_s4_scan(rows, ops_dir)`, which scans the report's `[min row day, max row day]` RANGE and, inside it,
EVERY `v33_stops_*.json` guard file present in `ops_dir` (unioned with the checkout dir when the live
fallback is active), not only days with rows. Out-of-range guard files are ignored (different reporting
window). Tests: `test_s4_latch_on_rowless_day_in_range_is_seen`, `test_s4_latch_outside_row_range_is_ignored`
(plus the builder's `test_s4_scans_all_report_days` still holds).

### F3 (gap 2b, agreed -- fixed) -- a CORRUPT guard was silently dropped
A corrupt guard read as `latched == ()` -> no S4 kill and no signal, so a bare `ALIVE-so-far` could print
over an unreadable stop file whose S4 state is unknown. **Chosen remedy: an explicit WARNING that taints
the verdict, not a hard KILL.** Argument: the report is descriptive; the fail-closed-on-corrupt KILL
discipline that refuses to ARM already lives in `decide_v33_arming` (S5, untouched), which keeps refusing
all day on a corrupt guard. Force-killing the campaign on a transient corrupt file would over-reach the
scoreboard's role. But silence is unacceptable, so `_s4_scan` returns `corrupt_days`, the verdict gains
`-- WARNING: guard CORRUPT on <days> (S4 state UNKNOWN; verify)` (so `ALIVE-so-far` can never read
all-clear), the result dict gains `s4_corrupt_days`, and `_render_gate_table` prints a dedicated WARNING
line. A corrupt guard is NOT counted as an S4 kill. Tests: `test_corrupt_guard_surfaces_warning_and_taints_alive`,
`test_corrupt_guard_warning_rides_alongside_s4_kill`, `test_render_gate_table_shows_s4_and_corrupt_lines`.

### F4 (doc, task-required -- fixed) -- L4 prose annotated
The L4 amendment paragraph (dated 2026-09-29) still names `n >= 30` and "mean lock +6.0c ... UNCHANGED" as
the gate. Added a one-line `_(Dated L4 record: ... SUPERSEDED by L6, 2026-09-30 -- the live gate is now
n >= 15 and +4.0c ...)_` annotation directly under it, so a reader of the frozen doc cannot mistake the
historical figures for the live gate. (This sits above the three verdict/kill/promotion sections the pins
test scans, so no test text collides.)

### F5 (doc hygiene -- fixed) -- stale current-gate statements in ancillary docs
- `PLAN_V33.md` sec 6: "Proposed gates at n >= 30", "mean true lock >= +6.0c", "Brad's dated word after
  n >= 30", and "rungs to 20c" -> updated to n >= 15, +4.0c, n >= 15, "rungs deeper than 18c"; added the
  S4 campaign kill to the Kill line, each with an L6 date tag.
- `ops/V33_ARMING.md` line ~148: "the n>=30 verdict" -> "the V3.3 n>=15 verdict (L6 2026-09-30; was n>=30)".
- `ops/V33_RUNBOOK.md` line ~261: "the n>=30 rung-fill counter" -> "n>=15 ... (L6; was n>=30)", added the
  S4 campaign kill to the verdict summary; and the stale DEEP END "16..25c" band -> "derived from the live
  ladder; 19..28c after the 8..18c shift" (the render already derives the band; only the doc text lagged).

### Verdict-logic verification at the new n (checklist 3) -- CORRECT
At n=15 the early kill (mean < +2.0c) and the verdict gate (mean >= +4.0c) now share the same n. The report
prints ONE coherent verdict: the S4/mean-early/one-legged kills are seeded into `kills` first and short-
circuit to a single `KILL: ...` line; the gate-fail list only runs in the `else`. Confirmed by the matrix
tests: **+1.5c** -> single early-kill line ("< +2.0c", no "mean true lock" gate text, one `KILL`); **+3.0c**
-> KILL via the mean-lock gate (a miss is a kill); **+4.0c exactly** -> PASS/ALIVE (bar is `>=`, not `>`);
**+4.5c** -> ALIVE. (`test_verdict_matrix_*`.)

### Doc<->code agreement, quote fidelity, scope (checklists 1, 4, 6) -- CORRECT
- Pins agree with the doc; the agreement test covers the new `V33_KILL_ON_S4_DAY_LOSS` pin, MIN_N==15,
  MIN_MEAN_LOCK==4.0, PROMOTION_MIN_N==15, and asserts no stale `n >= 30`/`n<30`/`n=30` in the live
  Proposed-thresholds / Kill / Promotion sections. Two stale trailing comments in the test (`# n >= 30`,
  `# +6.0c`) were cosmetic; corrected (N1).
- Brad's quote is byte-exact in the frozen `ceremony/v33_falsifier.md` (whitespace-normalized substring
  match). The build report's copy differs only by the blockquote `>` line-wrap; it is not frozen.
- Doc diff hunk-by-hunk: exactly the three requested changes + the S4 kill bullet + the L6 section + the F4
  annotation. STATUS line `DRAFT -- NOT FROZEN` unchanged; params JSON and sha
  `295590ce...f532def` untouched; no mode files or tasks touched.

## Nits
- N1 (fixed): stale `# n >= 30` / `# +6.0c` trailing comments in `test_v33_falsifier_pins.py`.
- N2 (informational, not changed): the builder's dated `build/v33_falsifier_l6_build_report.md` describes
  an internal `_s4_kill_days(rows, ops_dir)`; this review renamed it to `_s4_scan(rows, ops_dir)` returning
  `(s4_days, corrupt_days)`. No test or caller referenced the old name (they use the `s4_kill_days` dict
  key, which is preserved). The build report is a historical artifact and was left as-is.

## Receipts
- Full suite: `python -m pytest -q` in the worktree -> **1379 passed, 5 skipped** (branch baseline 1370
  passed, 5 skipped; +9 review tests). The 5 skips are environmental.
- CLI smoke: `python -m service.v33.report --ledger <empty> --ops-dir <tmp>` runs clean, prints
  "verdict at n >= 15", "mean true lock ... >= +4.0c".
- Pins unchanged by review: `V33_FALSIFIER_MIN_N=15`, `V33_FALSIFIER_MIN_MEAN_LOCK_CENTS=Decimal("4.0")`,
  `V33_PROMOTION_MIN_N=15`, `V33_KILL_ON_S4_DAY_LOSS=True`, `V33_KILL_MIN_N=15`; params sha
  `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def`.
