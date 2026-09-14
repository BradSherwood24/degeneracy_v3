# V3.2 review — co-settling KXBTC15M recording (PR #35, branch `v32/record-15m`)

Reviewer: Opus 4.8 (Fable-delegated). Base `622d697`, reviewed at builder commit `4609c0d`; fixes
committed on the branch. Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`. Fakes only — no proxy, no
socket, no sealed/holdout date read.

## Verdict

**APPROVE with two fixes applied (both on-branch, tested).** The change is genuinely recording-only:
no 15M frame can reach `decide_v32` and no 15M ticker can be an order target — the isolation is sound
on every axis probed (snapshot/delta/trade/fill/ticker/market_positions, exch_map, bucket_map, strike
map). The two defects I fixed are (1) 15M discovery failure was fatal to a viable trading window
(recording-only data must never cost a trade) and (2) the adversarial "15M fill dropped as foreign"
claim was asserted in prose but untested. One residual (bucket-book freshness) is a **pre-existing,
documented core design gap that this PR neither introduces nor materially widens** — flagged, not
fixed, because fixing it is a trading-behavior change that belongs in its own falsifier-gated PR.

Suite: **801 passed** (builder 799 + 2 review tests). `tests/test_run_v32_m15.py` now 12.

## Findings (one line each)

1. ISOLATION / book+trade — SOLID. `V32Recorder.on_snapshot/on_delta/on_trade` guard `market in
   self.m15_tickers` and `return` before `_drive_book`/`driver.on_trade`
   (`service/run_v32.py:1030,1037,1043`), so no `BookUpdate`/`Trade` is ever created for a 15M ticker.
2. ISOLATION / defense-in-depth — even if a 15M frame WERE driven, `classify_ticker` returns None (a
   `KXBTC15M-…` ticker is not in `bucket_map` and fails the `KXBTCD-` strike prefix), so `_fold_book`
   is a no-op (`service/v32/events.py:127-133`, `service/v32/core.py:397-400`).
3. ISOLATION / fill — SOLID and stronger than a name check: `on_fill` attributes by
   client_order_id/order_id against the RestBook; a coid/oid V3.2 never placed → `rec is None` →
   `foreign_fill_ignored` journaled + dropped (`service/run_v32.py:744-753`). A 15M fill is impossible
   (no 15M order, no 15M private channel) AND would be dropped as foreign. Now TESTED
   (`tests/test_run_v32_m15.py::test_15m_fill_frame_dropped_as_foreign`).
4. ISOLATION / order targets — SOLID. `exch_map` = strikes + range buckets only, NOT extended with 15M
   (`service/run_v32.py:1502-1505`); `PLACE_REST`/`TAKE_WINGS` target only bucket/strike tickers from
   those maps, so no 15M ticker can be an order leg.
5. ISOLATION / ticker+market_positions — SOLID. `V32Recorder.callbacks()` wires only
   snapshot/delta/trade/fill (`service/run_v32.py:1069-1075`); there is no `on_ticker` /
   `on_market_positions` handler, and 15M subscribes public channels only (no private/ticker), so those
   frames are journaled by the `tap` hook and never dispatched into the core.
6. DISCOVERY FAILURE — **DEFECT, FIXED.** `discover_co_settling_15m` (proxy 5xx / malformed body)
   raised, and `main` called it unwrapped AFTER strike+bucket discovery already succeeded
   (`service/run_v32.py:1392` pre-fix), so a recording-only failure aborted a fully-viable trading
   window. Fixed: new `discover_co_settling_15m_safe` (`service/run_v32.py:311-324`) returns
   `(empty M15Discovery, error)`; `main` journals `m15_discovery_error` and continues. Strike/bucket
   discovery stay fatal (correct — they ARE the trade).
7. WATCHDOG/LAG — as-built claim is correct: `current_lag_seconds()`/`silence_seconds()` are
   per-CONNECTION gauges; a liquid 15M on the bucket socket only ever supplies fresher frames, so it
   cannot make the connection LOOK staler. No regression to the connect/reconnect watchdog's stated job.
8. BUCKET-BOOK FRESHNESS — **PRE-EXISTING GAP, documented, not this PR's.** `bucket_ts` is stored
   (`service/v32/core.py:412-417`) but NEVER read; `_select_spot`/`_bucket_cap` use `bucket_tops`
   with no freshness gate (`service/v32/core.py:284-296,320-325`), by deliberate Phase-1 design
   (docstring `service/v32/core.py:13-14`). See "Residual" below — the 15M addition does not create or
   materially widen it.
9. JOURNAL VOLUME / MEMORY — SAFE. `StreamJournal` is write-through: an `_idx` counter + flush every N,
   NO in-memory record list (`service/record_range.py:104-133`). Per-market state is one `BookMirror`
   (current levels, bounded) + an int `m15_frames`. RSS stays flat regardless of 15M frame volume. Disk
   estimate below.
10. LEDGER/REPORT SCHEMA — STABLE for legacy rows. `build_v32_ledger_row` defaults `m15_tickers=[]`,
    `m15_frames=0` (`service/v32/ledger.py:104-105,149-150`); the report reads
    `r.get("m15_frames", 0)` (`service/v32/report.py:105,301`). An older row missing the keys renders 0.
11. DOCS — CORRECT. `ops/V32_DRY_RUN.md:3-9` states the Phase-3 arm gate accurately
    (FROZEN + S5/reconcile/day-latch/S4 else `degrade_to_dry`, matching
    `service/run_v32.py:1477-1489`) and the recording-only 15M note. The `echo dry > ops\v32_mode.txt`
    lever line (`:26`) is addressed to Brad running his own manual dry window — a human runbook, not an
    agent-directed lever instruction. No house-law issue.

## Fixes applied (this review, on-branch)

- `service/run_v32.py` — added `discover_co_settling_15m_safe(...)`; `main` now uses it and journals a
  distinct `m15_discovery_error` (vs `m15_missing`) when discovery raised, continuing the window.
- `tests/test_run_v32_m15.py` — `test_discover_15m_failure_is_non_fatal` (safe wrapper swallows a
  raising proxy; raw discovery still raises) and `test_15m_fill_frame_dropped_as_foreign` (adversarial
  private fill on a 15M ticker → `foreign_fill_ignored`, never booked, `m15_frames` untouched).

## Residual (NOT fixed here — flagged for Brad/Fable)

The core has no per-market freshness gate on the spot-bucket book: a spot bucket whose book goes stale
(Kalshi stops its deltas) while the strikes stay fresh continues to feed `_select_spot`/`_bucket_cap`
(stale spot + cap), and the resting NO stays live. This is **pre-existing and by-design** (documented,
`core.py:13-14`) — buckets were never freshness-gated. The 15M change does not widen it in practice:
the bucket connection's silence/lag gauge was already per-connection, so with ~180 buckets a single
active bucket already masked staleness of the rest; adding one 15M market changes nothing about
per-market bucket freshness. The one narrow incremental effect: if ALL ~180 buckets went silent at
once while 15M kept streaming and strikes stayed fresh, the connection watchdog would no longer
force-close/reconnect (previously an all-bucket blackout would climb the silence gauge to threshold).
Low-probability, but real. If Brad wants it closed, the disciplined fix is a **separate,
falsifier-gated PR**: add a ClockTick-driven per-market bucket-freshness gate (cancel-rest /
stand-down when the spot bucket book ages past a bound, symmetric to the strike gate), or exclude 15M
from the bucket connection's silence gauge. It must not ride in a recording-only change — it alters
decision semantics.

## Disk cost note (probe 4)

Memory is bounded (write-through journal). Disk is NOT free: a liquid 15M adds its full raw frame
volume to `journals_v32/<close>.jsonl` before the crash-safe gzip at close. From the build report's
~106k top-of-book + ~30k trade frames/hour, the raw JSONL add is roughly **~30–50 MB/hour**
(order-of-magnitude; each delta/trade envelope ~150–400 B), compressing to roughly **~6–10 MB/hour**
gzipped — a meaningful but bounded per-hour increment on top of the strike+bucket tape. Estimate only:
I did not read a live journal (pilot/journals is out of bounds). Worth watching disk headroom over a
multi-day armed run given the box's prior disk pressure.
