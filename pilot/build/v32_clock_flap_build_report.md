# V3.2 clock-flap hotfix — build report

Branch `v32/fix-clock-flap` (off `origin/main` @ 76c4309). Opus 4.8 builder. No network; no sealed/
holdout read. The one live journal below was read READ-ONLY.

## What happened live

First live-dry V3.2 window, close `2026-09-14T17:00:00Z`, journal
`pilot/journals_v32/20260914T170000Z.jsonl` (gzipped to `.jsonl.gz` by the live journal rotation while
this fix was being built). Within the first minute after the 16:44:55Z connect the core emitted
**61 `would_place_rest` + 61 `would_cancel_rest`** in ~5 s, the replace-rate alarm
(`replace_rate_alarm_per_min=60`) latched `stood_down`, and the window then quoted nothing for the rest
of the hour (`stand_down_reason: "stood_down"`). The 62 `stand_down` reasons alternated between
`stale_or_missing_wing` (41) and `stale_bucket` (19) plus the terminal `replace_rate` + `stood_down`.

## Root cause (verified by replay, not assertion)

The two WS connections — strikes (KXBTCD) on one, buckets (KXBTC) + the co-listed 15M on the other —
carry INDEPENDENT server clocks that interleave. A `BookUpdate` was driven into the core with
`server_ts = <that frame's own ts_ms>`, which the core used as `now`. When a frame arrived from the
SLOWER connection, `now` regressed below a book already folded from the FASTER connection, so in
`service/v32/core.py::_fresh` (`0.0 <= age <= bound`) the age went NEGATIVE → read "stale" →
`_compute_W` / `_wing_prices` returned `None` → `_requote` cancelled the rest (`stale_or_missing_wing`
or `stale_bucket`); the next frame (newer ts) made W valid again → place; ~1 ms flap.

Concrete evidence from the journal (spot bucket `78600`, ticker `KXBTC-26SEP1413-B78650`; wings
`T78599.99`/`T78699.99`):

| idx | record | market | ts_ms | eval result |
|----|--------|--------|-------|-------------|
| 920 | ws delta | `KXBTC-…-B78650` (bucket) | 1789404296061 | — |
| 921 | `v32_eval` | spot 78600 | — | **W = 1.3021** (valid) |
| 922 | ws delta | `KXBTCD-…-T78699.99` (strike) | 1789404296**038** | — |
| 923 | `v32_eval` | spot 78600 | — | **W = null** |

The strike frame at idx 922 folded with its own ts `…296038`, which is **23 ms BEHIND** the bucket
book stamped `…296061` at idx 920. `now = 296038 < bucket_ts = 296061` → age = −0.023 s → pre-fix
"stale" → W null → cancel. −23 ms is well inside both the 30 s bucket bound and the 1.0 s strike bound:
pure clock interleave, not staleness.

Faithful replay of the raw journal frames through the REAL `V32Recorder` + `V32Driver` (dry/shakedown,
fake clock following `local_ts`, `m15` ticker skipped as live):

* **pre-fix:** 61 `would_place_rest`, 61 `would_cancel_rest`, 62 `stand_down`, `stood_down=True`
  (exact match to the live journal).
* **post-fix:** 1 `would_place_rest`, 0 `would_cancel_rest`, 0 `stand_down`, `stood_down=False`.

`run_v32.V32Driver._stamp` already kept a MONOTONE `server_now()` for the ClockTick pump (P3-3), but
`_drive_book` / `on_book_update` and `on_trade` passed the raw frame ts as the event's `server_ts`, so
the flap slipped through on the book/trade path.

## The fix (belt and braces)

1. **`run_v32.py` (evaluation clock is monotone).** `on_book_update` now drives the core with
   `server_ts = self._last_server_ts` (the monotone clock = max(frame ts, previous) across BOTH
   connections, never behind any folded book) and carries the FRAME'S OWN ts as a new
   `BookUpdate.book_ts`; `_fold_book` records `book_ts` as the market's age anchor, so a genuinely
   stalled feed still ages that book out while the monotone clock advances off the live connection.
   `on_trade` likewise stamps the `Trade`'s `server_ts` with the monotone clock (a Trade folds no book,
   so it needs no `book_ts`). Driver docstring updated with the CLOCK LAW.
2. **`core.py::_fresh` (small-negative tolerance).** `-bound <= age <= bound`: a book stamped up to
   `bound` AHEAD of the evaluation clock is fresh (clock interleave, not staleness). Strike bound 1.0 s
   and bucket bound 30 s unchanged; a book more than `bound` STALE (age > bound) still ages out. This
   is a single chokepoint — every book-freshness check (`_compute_W`, `_wing_prices`,
   `spot_bucket_stale`, `_shadow_on_trade`) routes through `_fresh`, so the tolerance applies
   consistently, and it also covers the private-order paths (`Fill`/`OrderCancelled`) whose `now` is the
   order channel's own ts and can trail the book clock. Docstring updated with the LAW.
3. **`events.py`.** `BookUpdate` gains `book_ts: float | None = None` (frame's own ts); `None` falls
   back to `server_ts` (pre-fix single-clock behavior) so direct-constructed events — unit tests, the
   golden harness — are unchanged.

Consistency check (item 4): the same negative-age assumption lives ONLY in `_fresh`; `_shadow_on_trade`,
the `spot_bucket_stale` gate, `_compute_W`/`_wing_prices`, and the ClockTick path all call `_fresh`
(ClockTick already uses the monotone `server_now()`), and the requote debounce/`replace_times` windows
are monotone-safe once `now` is monotone. No other site needed a change.

## Item A — per-connection lag read as None at flush

`KalshiWebSocketClient.force_close()` sets `last_delta_lag_seconds = None` at window end, and
`_finalize` read `strike_ws.current_lag_seconds()` / `bucket_ws.current_lag_seconds()` AFTER the
connections had closed → both `None` in the row/summary and "mean data-age n/a" in the report, despite
~1.35 M streamed frames. Fix: `LagSampler` samples each connection's `current_lag_seconds()` on the
0.5 s clock-pump tick while the sockets are live; `_finalize` now records the sampled **mean** into the
ledger's per-window `strike_lag_seconds` / `bucket_lag_seconds` (the field the report reduces to a
cross-window p99) and the full `{mean, p99, last, n}` per connection into the summary's `lag_stats`.

## Item B — shadow booked 0 fills

The shadow is NOT suppressed by `stood_down` / the replace-rate alarm / the live rest state — verified
by reading the core: `_recompute_context` re-solves each shadow `n` whenever W and cap are valid (fresh
spot bucket + fresh wings), and `_shadow_on_trade` / `_shadow_complete` check only spot-match + taker
side + freshness. None of them reads `st.stood_down` or the rest slots. The live window's 0 shadow
fills were a DOWNSTREAM effect of the same clock-flap: W flapped to `None`, so the shadow `n` was `None`
on ~half the ticks and any qualifying trade landing on a `None` tick recorded nothing. Post-fix,
replaying the head fixture, shadow `n(0.10)` is re-solved on **9300 / 9773 ticks** (95%; the 473 `None`
ticks are the pre-context warmup). No shadow fill appears in the fixture only because it covers just the
first ~10 s of the T-900..T-300 window and none of its 142 trades qualified. A regression test asserts
the shadow re-solves `n` and books a fill on a qualifying print even with `stood_down=True`.

## Item C — 15M disk (report only, no code change here)

The 15M market (`KXBTC15M-26SEP141300-00`) accounted for ~1.28 M of the hour's ~1.35 M records
(27.5 MB gz for the hour → ~650 MB/day gz). Follow-up option (not this branch): thin the 15M recording
to top-of-book + trades rather than full-depth deltas.

## Why the golden still holds

`tests/test_v32_golden.py` constructs `BookUpdate(tk, top, ts)` positionally (3 args), so `book_ts`
defaults to `None` and `_fold_book` falls back to `server_ts` — identical to pre-fix. Its heap replays
events in strictly increasing `ts`, so ages are never negative and the `-bound` tolerance is never
exercised. All four golden assertions (ideal E=0.10 line, lagging tol01/deb0 line, core live fill,
no-lag shadows) are unchanged.

## Receipts

* Files changed: `pilot/service/v32/core.py`, `pilot/service/v32/events.py`, `pilot/service/run_v32.py`,
  `pilot/tests/test_v32_core.py`, `pilot/tests/test_run_v32.py`.
* New fixture: `pilot/tests/fixtures/v32/live_window_20260914T170000Z_head.jsonl.gz` (window_meta +
  m15_recording + 9773 real `kalshi_ws` frames through the pre-fix flap + alarm; 191,623 bytes gz).
* Replay count: pre-fix 61 place / 61 cancel / stood down; post-fix 1 place / 0 cancel / healthy.
* Tests: `cd pilot && python -m pytest -q` → **815 passed** (baseline 808 + 7 new: 3 core interleave,
  1 recorder skew, 1 fixture replay, 1 lag sampler, 1 shadow-under-stand-down).
