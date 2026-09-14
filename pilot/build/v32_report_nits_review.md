# V3.2 report nits — review

Reviewer: Opus 4.8. Branch `v32/report-nits` @ ced5862 on base 6957554. Worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. No network; no sealed/holdout/ledger/journal reads.

## Verdict: APPROVE

Small, well-scoped reporting/ledger change. All five probes pass; no blocking defects; no fixes required.
`cd pilot && python -m pytest -q` → **823 passed** (matches builder: baseline 815 + 8 new).

## Probes

1. **`_capture_quote` on both would/real place, not FrozenExecutor-only — CONFIRMED.**
   `_capture_quote(a)` is called inside `_journal_action` under `if k in (WOULD_PLACE_REST, PLACE_REST)`
   (run_v32.py:969-976). `_journal_action` runs on every core-emitted action from the three decide/route
   loops (run_v32.py:863, 932, 949), independent of executor. Executors return synthetic *events*, not
   place *actions* (only `decide_v32` emits places), so capture is driven by the real quote decision under
   either FrozenExecutor (dry, WOULD_PLACE_REST) or LiveExecutor (armed, PLACE_REST). `self.state` is set
   before journaling, so the captured Sd/Su/desired_n match the state that produced the action.

2. **T-5 quote-end no longer a stand-down; real alarms/staleness reflected — CONFIRMED.**
   `past_quote_end` sets `_quote_end_cancel=True` and is excluded from `_real_stand_downs`; every other
   reason increments the count and sets `_last_stand_down_reason` (run_v32.py:999-1003). Verified against
   the core's reason set (core.py:787-808: stood_down/set_complete/past_quote_end/no_spot_bucket/
   stale_bucket/stale_or_missing_wing/n_below_min/replace_rate); `_standdown` dedupes per reason-change
   (core.py:745-750), so the count is per stand-down transition, not per tick. Day-guard S1_LEGGED /
   A_REPLACE accounting is untouched (not present anywhere in the diff; grep confirms).

3. **Ledger schema backward compatible — CONFIRMED.** All new `build_v32_ledger_row` kwargs are optional
   with defaults; `stand_downs` falls back to the raw action count when `real_stand_downs` is None; the
   early `_stand_down` path (run_v32.py:1465) and pre-fix rows render (report uses `.get()` throughout;
   `_lag_stats_p99` guards `(r.get("lag_stats") or {})`). Covered by `test_legacy_row_shape_preserved`,
   `test_report_table_renders_legacy_row_without_new_fields`, and a JSON round-trip test.

4. **Scoreboard p99 = max across windows and says so — CONFIRMED.** `_lag_stats_p99` returns
   `max(vals)` over `lag_stats[conn]["p99"]` across ALL rows (report.py:146-155); render line reads
   `data-age p99 (max/window): strike … bucket …` (report.py:310-311). Falls back to the legacy per-row
   p99 only when no row carries lag_stats, then n/a.

5. **Nothing else in the diff — CONFIRMED.** 5 files, all under `pilot/`: run_v32.py, v32/ledger.py,
   v32/report.py, new test file, new build report. No decision-math or money-math touched — the new
   attributes and fields are observability-only.

## Findings (non-blocking)

- OBSERVATION (minor, pre-existing core behavior): a `replace_rate` stand-down counts as 2 in
  `_real_stand_downs` (the root `replace_rate` transition, then the subsequent `stood_down` transition on
  the next event since `stood_down=True` latches), and `stand_down_reason` then surfaces the generic
  `stood_down` rather than the root `replace_rate`. This is how the core emits STAND_DOWN across a latched
  stand-down; not introduced by this change and not on the money path. No fix.
- OBSERVATION (cosmetic): when the scoreboard falls back to the legacy per-row `*_lag_p99_s` (legacy rows
  only), the render still labels it `(max/window)`, though that legacy value is a p99-across-windows, not a
  max. Legacy-only path; no fix.
- OBSERVATION: `_capture_quote` takes the bucket ticker from the action (`a.ticker`) but Sd/Su/desired_n
  from `self.state`; consistent in normal operation (both captured atomically at journaling), matching the
  builder's noted intent (last_rest_price == last_desired_n in the core). No fix.

## Fixes applied

None — no clear defect found.
