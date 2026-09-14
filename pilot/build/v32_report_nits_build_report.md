# V3.2 report nits — build report

Branch `v32/report-nits` (off `origin/main` @ 6957554). Opus 4.8 builder. No network; no sealed/holdout
read. The live dry-run ledger row was read READ-ONLY from
`C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v32_ledger.jsonl` for diagnosis only.

## What was wrong (from the 2026-09-14T18:00:00Z live dry row)

Receipts pulled from the live row:

- `spot_bucket_ticker`, `Sd`, `Su` = **null** at close, although the window quoted spot bucket
  78800/78900 for ~10 min (`replaces=81`, `would_places=81`, last rest 0.53). Cause: the row read the
  spot bucket from the FINAL `V32State`, but by close the requote loop has hit the T-5 quote-end cancel
  and the reset-at-close state has nulled `spot_Sd`/`spot_Su`.
- `stand_downs=1` with `stand_down_reason=null`. Cause: the T-5 end-of-quoting cancel emits a
  `STAND_DOWN("past_quote_end")` action; the row counted it as a stand-down and never carried the
  reason string.
- Report FALSIFIER SCOREBOARD printed `data-age p99: strike n/a bucket n/a` even though the summary
  carried `lag_stats.{strikes,buckets}.{mean,p99,last,n}`. Cause: the scoreboard computed p99 from the
  per-row single `strike_lag_seconds`/`bucket_lag_seconds` gauge and only over ARMED rows — dry rows
  never contributed, and `lag_stats` was in the summary but not on the ledger row the report reads.

## The fix

### `service/run_v32.py` — capture WHILE quoting
`V32Driver` now snapshots the last-quoted bucket in `_capture_quote(a)`, called on every
place/would-place from `_journal_action`. It records `_last_quoted_bucket_ticker`, `_last_quoted_Sd`,
`_last_quoted_Su`, `_last_rest_price` (the action's price), `_last_desired_n` (`state.desired_n`), and
appends each distinct `spot_Sd` to `_spot_buckets_quoted` in first-appearance order. The `STAND_DOWN`
branch of `_journal_action` now splits the reason: `"past_quote_end"` sets `_quote_end_cancel=True` and
is NOT tallied; every other reason increments `_real_stand_downs` and sets `_last_stand_down_reason`.
`_finalize` passes all of these into the row build and mirrors them into the summary dict (which now
also carries `spot_bucket_ticker`/`Sd`/`Su`/`last_rest_price`/`last_desired_n`/`spot_buckets_quoted`/
`quote_end_cancel`/`stand_downs`/`stand_down_reason`), plus `lag_stats` was already in the summary.

### `service/v32/ledger.py` — carry the new fields
`build_v32_ledger_row` gained optional kwargs (`last_quoted_bucket_ticker`, `last_quoted_Sd`,
`last_quoted_Su`, `last_rest_price`, `last_desired_n`, `spot_buckets_quoted`, `quote_end_cancel`,
`real_stand_downs`, `last_stand_down_reason`, `lag_stats`). `spot_bucket_ticker`/`Sd`/`Su` now PREFER
the last-quoted capture and fall back to the end-of-window state. New row fields: `last_rest_price`,
`last_desired_n` (Decimal-safe strings), `spot_buckets_quoted`, `quote_end_cancel`, `lag_stats`.
`stand_downs` uses `real_stand_downs` when provided, else the legacy raw action count; `stand_down`
(the full-standdown flag) is unchanged; `stand_down_reason` carries the full-standdown reason or, absent
that, the last real stand-down record.

### `service/v32/report.py` — read lag_stats, new table columns
New helper `_lag_stats_p99(rows, conn)` = **MAX over windows** of `lag_stats[conn]["p99"]` (read over
ALL rows, not just armed). The scoreboard exposes `strike_lag_stats_p99_s`/`bucket_lag_stats_p99_s`; the
render line is now `data-age p99 (max/window): strike … bucket …`, preferring the lag_stats value and
falling back to the legacy per-row p99, then n/a only when both are absent. The per-window table gains a
`lastRest` column (last rest price) and an `Sd` column from the new field. Legacy per-row p99 fields
(`strike_lag_p99_s`/`bucket_lag_p99_s`) are unchanged, so the existing scoreboard test still holds.

## Tests

New file `pilot/tests/test_v32_report_nits.py` (8 tests): ledger capture across a fake window that
quotes two buckets (last-quoted ticker/Sd/Su/rest/desired-n + `spot_buckets_quoted`); quote-end cancel
excluded from stand-downs; a real stand-down counted + reasoned; legacy row shape preserved; JSON
round-trip; scoreboard reads lag_stats p99 as max-across-windows and renders it; n/a only when lag_stats
absent; table renders Sd + lastRest; legacy row without the new fields still renders.

## Receipts

- `cd pilot && python -m pytest -q` → **823 passed** (baseline 815 + 8 new).
- Report rendered read-only over the live legacy ledger: the three pre-fix rows render cleanly with the
  new `Sd`/`lastRest` columns as `-` and the scoreboard line as `data-age p99 (max/window): strike n/a
  bucket n/a` (legacy rows predate the fields; future dry rows written by the patched code carry them).

## Surprising / worth noting

- In the core, the rest order's `price` IS `state.desired_n` (`_place_action` sets `price=n`,
  `n=st.desired_n`), so `last_rest_price` and `last_desired_n` are equal in normal operation; both are
  captured as the task asked, kept conceptually distinct (placed price vs current target).
- The pre-fix live 18:00Z row is a legacy row: it will keep showing null bucket / `stand_downs=1`. Only
  rows written by the patched `run_v32` reflect the fix; this is expected and is why the legacy-render
  path is tested.
