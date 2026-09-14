# V3.2 build — per-market spot-bucket FRESHNESS gate (branch `v32/bucket-freshness`)

Builder: Opus 4.8 (Fable-delegated). Base `origin/main` 4684bad. Worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. No proxy, no socket, no sealed/holdout date read.

## What changed and why

Review finding 8 (`pilot/build/v32_m15_review.md`): `V32State.bucket_ts` was stored but never read —
`_select_spot`/`_bucket_cap` had no freshness gate, so a stalled bucket feed (a bucket-connection
stall while the strike connection stays fresh, or a partial blackout masked by the liquid co-listed
15M sharing the bucket connection) could feed spot selection and the `cap = no_ask(B) - 0.01` while the
connection looked alive. The strike (wing) books already had the gate. This PR adds the symmetric
per-market gate on the spot-bucket books, in the pure core.

## Law implemented

1. `_select_spot(st, params, now)` now excludes EVERY bucket whose book age exceeds
   `bucket_freshness_max_age_s` (stale buckets excluded, not just the selected one), selecting the
   highest-YES-mid among the FRESH two-sided books. It also returns `had_stale_valid` so a stall is
   journaled distinctly. The cap is gated transitively: `_bucket_cap` is only ever called on the
   freshly-selected spot, so it can never read a stale bucket book.
2. When no fresh two-sided bucket remains, `_requote` cancels any live/pending rest and stands down
   with reason **`stale_bucket`** (vs `no_spot_bucket` for a genuine absence of any two-sided book) —
   no new PLACE, no shadow fill, until fresh again. No new action kinds.
3. `_shadow_on_trade` gates the shadow fill on the spot bucket being fresh at the trade's `server_ts`
   ("a shadow fill needs a fresh cap too").
4. ClockTick already runs `_recompute_context` + `_requote`, so a stalled bucket feed with no further
   bucket frames cancels the rest on the next tick (law 3), like the strike silent-feed fix F-B.
5. Separate param `bucket_freshness_max_age_s` (default **30.0**) added to `V32Params` and
   `pilot/policy/v32_params.json` — the bucket bound is larger than the 1.0 s strike bound because
   range buckets are thin and tick far less often (a 1.0 s bucket gate would stand the strategy down
   most of the time). New state field `V32State.spot_bucket_stale`.

## Params re-pin (pre-freeze build act)

- `pilot/policy/v32_params.json`: added `"bucket_freshness_max_age_s": 30.0`.
- `FROZEN_V32_PARAMS_SHA256` re-pinned in `service/v32/params.py`:
  - old `c6715fc7fd8339e0cc8877bd39bb78b04239eda9c490bde71a53333a48bdfb92`
  - new `0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc`
- `ceremony/v32_falsifier.md`: both sha occurrences updated; the Policy "Values" line and the A_STALE
  alarm now list `bucket_freshness_max_age_s 30.0 [pin]`. `falsifier_pins.py` unchanged (it holds
  verdict/retirement/promotion gates, not policy params; it asserts neither the params sha nor the
  field list).

## Tests (807 passed, baseline 801 + 6)

New in `tests/test_v32_core.py`: stale spot bucket -> CANCEL_REST + `stale_bucket` + no PLACE; fresh
again -> PLACE resumes; a stale NON-spot bucket is only excluded (spot falls back, no stand-down);
ClockTick-only staleness cancels; shadow does not book a fill on a stale bucket (with a fresh-bucket
positive control). `test_params_load_and_sha_pin` asserts the new field. New
`test_v32_falsifier_pins.py::test_bucket_freshness_pin_matches_params_and_doc`.

## The golden harness (the surprising bit)

`tests/test_v32_golden.py` feeds spot-bucket tops from the `1-hour-range` **minute candles** (the range
historical series is minute-candle only; the strike ms books are dense). The reference fill print for
close 2026-09-04T20:00:00Z sits at dt=-843 s — **~57 s after** its spot-bucket candle at dt=-900 s.
Under the live 30 s bucket gate, hundreds of dense strike ticks between -900 and -843 would each
recompute and (correctly, for a minute-candle artifact) find every bucket stale, standing the hour
down before the print — the golden fill would never fire.

The golden test asserts the fill ECONOMICS (n=0.45, lock +10.36c) reproduce the reference; the
freshness gate is covered by the dedicated core unit tests above. So the harness relaxes ONLY the
bucket bound (`bucket_freshness_max_age_s=3600.0`, well above the 60 s candle cadence), restoring the
pre-gate bucket behavior; the 1.0 s STRIKE gate still binds unchanged. This is a fixture-cadence
accommodation, documented inline in `_run_core`, not a change to the fill logic — all golden
assertions reproduce byte-for-byte.

## Files

- `pilot/service/v32/core.py` — `_select_spot` freshness gate + `had_stale_valid`; `spot_bucket_stale`
  state; `_recompute_context` wiring; `_requote` `stale_bucket` reason; `_shadow_on_trade` freshness
  gate; docstrings.
- `pilot/service/v32/params.py` — `bucket_freshness_max_age_s` field/load; re-pinned sha.
- `pilot/policy/v32_params.json` — new key.
- `pilot/ceremony/v32_falsifier.md` — sha, Values line, A_STALE, `[pin]`.
- `pilot/tests/test_v32_core.py`, `pilot/tests/test_v32_falsifier_pins.py`, `pilot/tests/test_v32_golden.py`.
