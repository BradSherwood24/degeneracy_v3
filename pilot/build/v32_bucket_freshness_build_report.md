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

Ruling R-STALE-SPOT (coordinator 2026-09-14): the gate is on the SELECTED spot, not an exclusion
filter — a stale spot stands down rather than falling through to a fresh lower-mid bucket.

1. `_select_spot(st)` selects the highest-YES-mid bucket among valid two-sided books **regardless of
   age**. Rationale: the edge is spot-bucket-only (the 2026-09-01 range scan found OTM-bucket pumps are
   the informed ones — all-buckets pays far less than spot-only), so resting on a fresh lower-mid
   bucket because the spot book went quiet is the wrong trade, not a lesser one.
2. `_recompute_context` sets `spot_bucket_stale` = the SELECTED spot's own book is older than
   `bucket_freshness_max_age_s`, and solves W/cap/desired_n (and every shadow n) only when the spot is
   present AND fresh. A stale selected spot -> `_requote` cancels any live/pending rest and stands down
   with reason **`stale_bucket`** — no PLACE (never on a lower bucket), no shadow fill, until fresh.
   `no_spot_bucket` is reserved for a genuine absence of any two-sided book. No new action kinds.
   `_bucket_cap` is only called on a fresh selected spot, so it never reads a stale book.
3. `_shadow_on_trade` gates the shadow fill on the selected spot bucket being fresh at the trade's
   `server_ts` ("a shadow fill needs a fresh cap too").
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

## Tests (808 passed, baseline 801 + 7)

New in `tests/test_v32_core.py`: stale selected spot -> CANCEL_REST + `stale_bucket` + no PLACE (spot
floor unchanged); fresh again -> PLACE resumes; (a) a stale NON-spot lower bucket has no effect (spot
unchanged, quoting continues); (b) a stale SPOT with a fresh lower bucket present -> CANCEL_REST +
`stale_bucket`, no PLACE on the lower bucket or any other; ClockTick-only staleness cancels; shadow
does not book a fill on a stale bucket (with a fresh-bucket positive control). `test_params_load_and_sha_pin`
asserts the new field. New `test_v32_falsifier_pins.py::test_bucket_freshness_pin_matches_params_and_doc`.

## The golden harness (the surprising bit)

`tests/test_v32_golden.py` feeds spot-bucket tops from the `1-hour-range` **minute candles** (the range
historical series is minute-candle only; the strike ms books are dense). The reference fill print for
close 2026-09-04T20:00:00Z sits at dt=-843 s — **~57 s after** its spot-bucket candle at dt=-900 s.
Under the live 30 s bucket gate the selected spot would be stale at the print (and at the dense strike
ticks before it), standing the hour down — the golden fill would never fire.

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
