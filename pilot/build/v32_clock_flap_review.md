# V3.2 clock-flap hotfix — Opus 4.8 review

PR #38, branch `v32/fix-clock-flap` @ d8f35b7, base 76c4309. Reviewed in worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. No network, no sealed/holdout read. `python -m pytest -q` →
**815 passed** (matches builder's count).

## Verdict: APPROVE — ship the second dry run.

The fix is correct, minimal, and in-scope. The two-layer design (monotone evaluation clock in the driver
+ `-bound` tolerance in `_fresh`) is the right shape. The real-fixture replay test is a genuine
regression guard: reverting BOTH source changes reproduces the exact live signature
(61 place / 61 cancel / 62 stand_down) and the test FAILS; restored, it passes. No clear defect found;
no code change made by the reviewer.

## Probe results

1. **Monotone evaluation clock.** Confirmed. `run_v32.py:741-743` — `on_book_update` calls
   `_stamp(server_ts)` (`_last_server_ts = max(prev, server_ts)`, run_v32.py:722) then drives the core
   with `server_ts=self._last_server_ts` and `book_ts=server_ts`. `_fold_book` (core.py:434,441,452)
   stores `book_ts` (frame's own ts) as each market's freshness anchor. Because `_last_server_ts` is the
   max over ALL frames on BOTH connections, every stored `book_ts <= now`, so **book-path ages are
   non-negative by construction** — the `-bound` branch is exercised only by the private-order paths and
   the direct-constructed core unit tests.
   *Residual (informational, not a blocker):* the monotone clock couples the two connections' freshness.
   If the bucket connection's server ts ran **persistently >1.0 s ahead** of the strike frames' own ts,
   `now` would ratchet to the bucket clock and every strike (wing) book would read `age > 1.0 s` → stale
   → permanent `stale_or_missing_wing` stand-down. To trip it the offset must exceed the **1.0 s strike
   bound** (the 30 s bucket bound is far more tolerant). Given both `ts_ms` are Kalshi venue server time
   (one clock), the observed live skew was 23–300 ms; a >1 s persistent inter-connection offset is
   implausible. Real risk: low/theoretical. Worth a line in ops notes, not a code change.

2. **`_fresh` symmetric tolerance.** No unsafe path. `core.py:299` `return -bound <= age <= bound`: a
   book stamped in the future by MORE than `bound` gives `age < -bound` → returns False (stale) — the
   conservative outcome. A future book is treated fresh only within `[-bound, 0)`, which is exactly the
   intended clock-interleave window. This only matters if the venue emits future timestamps; on the
   monotone book path `book_ts <= now` always, so it does not arise there at all.

3. **ClockTick ages a stalled feed out.** `server_now() = _last_server_ts + local_elapsed`
   (run_v32.py:729) keeps advancing while books stay anchored to their frozen `book_ts`, so
   `age = server_now() - book_ts` grows past the bound → cancel. Covered by
   `tests/test_run_v32.py::test_recorder_clock_skew_never_cancels_then_stale_still_does` (line ~544-551:
   a genuine >bound gap DOES cancel via an advancing `now`) and
   `test_clocktick_cancels_rest_at_quote_end` (line ~283: a ClockTick reaches the cancel path). *Minor
   gap:* no single test drives a pure ClockTick (no frames on either connection) past the strike bound to
   force a staleness age-out; the two mechanisms are covered separately. Not a blocker.

4. **Real-fixture replay test.** `tests/test_run_v32.py::test_replay_live_window_head_fixture_no_flap`
   replays the raw frames through the REAL `R.V32Recorder` + `R.V32Driver` (`on_snapshot`/`on_delta`/
   `on_trade`) — not a re-implementation. It asserts `would_place_rest <= 3` AND `would_cancel_rest <= 3`
   AND `not drv.state.stood_down` AND `replace_rate`/`stood_down` absent from stand-down reasons, plus
   spot selection unchanged (`spot_Sd == 78600`). **Pre-fix check performed:** reverted `_fresh` to
   `0.0 <= age <= bound` and the driver's monotone `on_book_update`/`on_trade` stamping, ran this one
   test → **FAILED** with `counts = {would_place_rest: 61, would_cancel_rest: 61, stand_down: 62}` (the
   exact live-journal signature). Source files restored (`git status` clean).

5. **LagSampler.** `run_v32.py:1150-1191`. Sampled on the 0.5 s pump tick via
   `_clock_pump` (`lag_sampler.sample()` at line ~1206) reading `current_lag_seconds()` while sockets are
   live — BEFORE `force_close()` nulls `last_delta_lag_seconds` (ws_client.py:173; force_close runs only
   in `main`'s finally, after the pump loop exits). `summary()` returns `{mean, p99 (nearest-rank), last,
   n}`; None-safe (returns None when a connection never delivered a frame; per-reading `None` skipped in
   `sample()`). p99 rank `min(n-1, max(0, ceil(0.99*n)-1))` is correct at the edges (n=1 → index 0).
   `_finalize` records `lag_sampler.mean(...)` into the ledger row and `summaries()` into
   `lag_stats`. Covered by `test_lag_sampler_captures_real_lag_despite_post_close_null`, which also
   asserts the sampler still returns the mean after the gauge is nulled.

6. **Shadow under stood_down.** `tests/test_v32_core.py::test_shadow_still_books_when_stood_down` sets
   `stood_down=True`, feeds a book tick (asserts `n` is re-solved to 0.45 and the LIVE place path is
   suppressed), then feeds a qualifying spot-bucket YES print and asserts a shadow fill is booked
   (`sub.filled`, `fill.n == 0.45`, `fill.print_price == 0.56`, `fill.lock == 0.1036`). Proves the alarm
   latch does not gate the shadow.

7. **Scope / safety.** Diff touches only `core.py` (`_fresh`, `_fold_book`), `events.py`
   (`BookUpdate.book_ts`), `run_v32.py` (monotone stamping, `LagSampler`, `_finalize` `lag_stats`),
   two test files, one fixture, and this build report — all within the stated scope. Fixture
   `live_window_20260914T170000Z_head.jsonl.gz` = **191,623 bytes** (<1 MB), 9773 `kalshi_ws` frames +
   `window_meta` + `m15_recording`, dated **2026-09-14** (NOT the 08-20..29 holdout). No socket/http call
   or holdout date appears in the changed tests (only docstrings affirming their absence). Golden harness
   unaffected (`book_ts=None` → `_fold_book` falls back to `server_ts`, pre-fix behavior).

## Fixes applied by reviewer

None. No clear defect. The two items above (Probe 1 residual coupling; Probe 3 missing pure-ClockTick
stall test) are informational — neither gates the dry run.
