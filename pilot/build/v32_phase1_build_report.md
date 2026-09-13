# V3.2 Phase 1 build report — pure core (`service/v32/`)

Branch `v32/phase1-core` (base origin/main `afcde6b`). Builder: Opus 4.8. Scope: the pure decision
core + policy + tests only. No process spine, executor, ledger, or ceremony (Phases 2-4). No network,
no clock reads, no disk beyond the shipped policy + the golden fixture. Holdout (2026-08-20..29) and
the seal (2026-08-02..18) untouched; the one real hour used is 2026-09-04 (Fable's explicit OK).

## What was built

Files (all absolute under the worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`):

* `pilot/service/v32/__init__.py` — package exports.
* `pilot/service/v32/params.py` — `V32Params` frozen dataclass + `load_v32_params(path, expected_sha)`
  with the box's canonical-sha scheme (`canonical_sha256` == `box.canonical_sha256`, pinned by a test).
  Missing required key -> `KeyError` (fail closed); sha drift -> `V32ParamsShaMismatch`.
* `pilot/policy/v32_params.json` — the shipped policy. Canonical sha pinned in code as
  `FROZEN_V32_PARAMS_SHA256 = 69646917691ac1995b82f75bb0de2ca3796ee52b068ae283b1ee9df1455ceed1`.
  Values: E=0.10, tol=0.02, deb_ms=2000, quote_start_s=900, quote_end_s=300, contracts=1,
  wing_margin=0.02, lock_floor=-0.10, no_orders_after_s_to_settle=1, freshness_max_age_s=1.0,
  max_sets_per_hour=1, n_min=0.05, replace_rate_alarm_per_min=60, bucket_width=100,
  shadow_Es=[0.08,0.10,0.12]. (tol/deb_ms are the provisional PLAN_V32 RESULTS_PLACEHOLDER defaults;
  nothing in Phase 1 depends on the final tuned numbers.)
* `pilot/service/v32/events.py` — `BookUpdate`, `Trade`, `Fill`, `OrderAck`, `OrderCancelled`,
  `ClockTick` (each carries a server-derived `server_ts`), plus pure classifiers
  `parse_strike_ticker` (round(strike)+0.01 -> floor int, e.g. `KXBTCD-...-T77799.99` -> 77800),
  `parse_bucket_ticker` (from the static discovery `ticker -> (floor, cap)` map — never guesses bucket
  syntax), and `classify_ticker`. `BookUpdate.top` reuses `service.book.TopOfBook`.
* `pilot/service/v32/actions.py` — `ActionKind` (str enum) with `PLACE_REST`, `CANCEL_REST`,
  `TAKE_WINGS`, `RETRY_WING`, `STAND_DOWN` and the `WOULD_PLACE_REST`/`WOULD_CANCEL_REST`/
  `WOULD_TAKE_WINGS` shakedown twins; `twin_kind`; `LegOrder`; `V32Action`.
* `pilot/service/v32/core.py` — `V32State`, the records (`RestOrder`, `RestFill`, `WingLeg`,
  `ShadowFill`, `ShadowSub`), the pure primitives (`solve_n`, `wing_cost`, `lock_value`), and
  `decide_v32(params, state, event) -> (state, actions)`.
* `pilot/tests/test_v32_core.py` (33 tests), `pilot/tests/test_v32_golden.py` (5 tests),
  `pilot/tests/fixtures/v32/golden_20260904T200000Z.json` (148 KB compact fixture).

## The decide protocol (what `decide_v32` does)

Time comes ONLY from `event.server_ts`. Money is Decimal; fee is the imported audited census fee
(`service._simlaw.fee`, never retyped).

* **BookUpdate** (strike or bucket): fold the top + its ts; recompute the quote context (spot bucket =
  highest YES mid among valid two-sided bucket books; Su = Sd + bucket_width; W = yes_ask(Sd) +
  fee + no_ask(Su) + fee with both strike books present AND fresh within freshness_max_age_s;
  cap = (1 - yes_bid(Sd)) - 0.01; desired n = largest whole-cent n with n+fee(n) <= 2 - E - W, n<=cap;
  and every shadow n(E) re-solved no-lag); complete any awaiting shadow fill; step the wings; run the
  requote gate.
* **Trade**: shadow-fill check — a spot-bucket YES print strictly above 1 - n_shadow(E) records a
  shadow fill (once per E per hour). No rest-fill is inferred from public trades (that is Fill /
  OrderCancelled, i.e. the exchange).
* **Fill**: a wing-leg fill updates leg status (both filled -> sets_done += 1); a rest fill books the
  RestFill, clears the rest, and takes both wings (subject to the lock floor + settle cutoff).
* **OrderAck**: the pending create becomes the live rest (swap).
* **OrderCancelled**: clears the slot / the in-flight flag; `filled_count_before_cancel > 0` is a rest
  fill.
* **ClockTick**: window cutoffs + wing step (cancel the rest at/after T-quote_end_s).

Requote gate (mirrors `pf_ms_requote2.py`): never two live rests; hold while a PLACE is pending
(awaiting ack); a live rest is REPLACED (CANCEL + PLACE, old stays fillable until the new acks) only
when |desired_n - n_resting| >= tol AND >= deb_ms since the last replace; a bucket change cancels the
old and places on the new bucket after the cancel confirms; replaces in a trailing 60 s above
replace_rate_alarm_per_min cancel the rest and stand the hour down; a stale/missing wing, no spot
bucket, n < n_min, past T-quote_end_s, or a completed set each cancel the rest + STAND_DOWN
(reason-deduped). One rest fill = the one entry for the hour (quoting stops immediately; sets_done
increments later, on wing completion). Shakedown downgrades every order-emitting action to its WOULD_*
twin.

## Test results

```
$ cd pilot && python -m pytest -q tests/test_v32_core.py tests/test_v32_golden.py
38 passed in 3.85s

$ cd pilot && python -m pytest -q            # whole suite, nothing else broke
670 passed in 25.18s
```

Unit coverage: policy sha pin + mismatch refusal + missing-key KeyError + canonical-sha == box;
solve_n vs brute force over the full whole-cent grid; cap never crossed; fee agreement with the
scratch `KALSHI_FEE_EXACT` lambda; spot selection; place/ack lifecycle; shakedown WOULD_* only;
requote tol / debounce / in-flight hold / bucket change; freshness cancel; window cutoffs; n_min
stand-down; fill -> TAKE_WINGS with lock; lock-floor defer-then-take; one set per hour; both-wings ->
sets_done; wing no-fill -> RETRY_WING; partial-fill-before-cancel; replace-rate alarm; shadow fill +
completion + once-per-hour + ignore no-side/non-spot.

Golden (one real hour, close 2026-09-04T20:00:00Z, spot bucket 79600):
* Ported `pf_ms_requote.py` IDEAL (E=0.10) reproduces the known detail line: offer **0.57**, print
  **0.58**, n=0.43, lock **+12.38c** (+12c to the cent).
* Ported `pf_ms_requote2.py` LAGGING (tol=0.01, deb=0): n=**0.45**, offer 0.55, print 0.56, lock
  **+10.36c**.
* The CORE, replayed through the fixture with a requote2-equivalent 200 ms-ack executor harness,
  produces the SAME live fill (n=0.45, lock +10.36c) as the ported lagging reference, and takes both
  wings (YES@79600 limit 0.78, NO@79700 limit 0.66).
* The CORE's no-lag shadow at E=0.10 = the lagging fill (0.45/0.55/0.56/+10.36c); at E=0.12 it
  reproduces the ideal E=0.10 line (0.43/0.57/0.58/+12.38c) — see CONFESSION 1.

## CONFESSIONS — deviations from the plan/task, and why

1. **The shadow cannot reproduce `pf_ms_requote.py`'s -1 s n-solve; it is a no-lag state machine.**
   The plan (Phase-1 text) and the task both specify the shadow re-solves n "with no lag" on every
   tick and checks a trade against the carried n. `pf_ms_requote.py`, however, solves its ideal n from
   the wing price at **print - 1 s** (`W_ms(s, pts*1000 - 1000)`) and completes at **print + 1.5 s**.
   For this hour the wings moved W from 1.4477 (T-1 s) to 1.4290 (at the trade) in the final second,
   so the ideal's -1 s convention lands n=0.43 (offer 0.57) while the true last-book (no-lag) value is
   n=0.45 (offer 0.55). A **pure state machine has no 1-second lookback** (it carries only current
   tops, not a time series), so it structurally cannot bit-reproduce the -1 s offset. Resolution
   (safest reading + document, per Fable's instruction): the core's shadow is the no-lag rule; the
   golden test **ports `pf_ms_requote.py`'s exact rule inline** and asserts IT yields 0.57/0.58/+12.38c
   (proving the fixture is faithful to the sim ideal), then asserts the core's no-lag shadow output
   separately. Striking cross-check that makes the gap legible: the ideal's -1 s is equivalent here to
   raising E by ~0.02, so the core's **E=0.12** no-lag shadow reproduces the ideal **E=0.10** line
   exactly (0.43/0.57/0.58/+12.38c). **QUESTION FOR BRAD below.**

2. **Bucket change waits for OrderCancelled before placing (spec's safe reading), not the sim's
   same-tick swap.** `pf_ms_requote2.py` drops the old quote and places the new on the same forced
   tick; the task text says "CANCEL then PLACE on the next tick after OrderCancelled". I implemented
   the latter (never a lingering order on the vacated bucket). This does not affect the golden hour
   (no bucket change precedes the -843 s fill) but means the core is NOT bit-identical to requote2
   across a bucket change. A **normal** |dn| replace does keep the old rest fillable until the new
   acks (requote2-faithful).

3. **Exact Decimal census fee vs the scratch float lambda.** The core uses `service._simlaw.fee`
   (Decimal, matches the audited golden literals). The scratch `rangelab.KALSHI_FEE_EXACT` is a float
   lambda that over-charges by exactly one $0.0001 tick at six whole cents (0.10, 0.20, 0.40, 0.50,
   0.60, 0.70) — a float-rounding artifact. Max disagreement $0.0001, well below the cent; the golden
   locks match to the cent either way. A unit test pins this (exact everywhere except those six, and
   there b - a == $0.0001).

4. **Bucket-book freshness.** The `freshness_max_age_s` (1 s) gate applies to the STRIKE (wing) books
   only. In Phase 1 the bucket book comes from minute candles, so a 1 s gate would make it perpetually
   stale; the bucket book is gated on presence + a valid two-sided book instead. In live (Phase 2+)
   the bucket book is a ms WS orderbook and the same freshness field can bind it.

5. **Fixture window trim.** Strike ms books in the fixture are trimmed to T-16..T-13 (not the full
   T-16..T-4); the one-set-per-hour rule stops quoting at the -843 s fill, so post-fill strike ticks
   are irrelevant to the asserted result. Bucket candles + spot prints span the full T-16..T-4. The
   fixture is 148 KB (prices as integer cents; ts as deltas from close_epoch).

## Open items for Phase 2/3 (assumptions to confirm with the executor)

* **Order payload shapes assumed, not yet wired.** PLACE_REST carries side="no", action="buy",
  price=n, count, expiration_epoch = close_epoch - quote_end_s, client_order_id. The Phase-3
  executor must map these to the proxy create (post_only, TIF good_till_canceled, exchange_index).
* **The no-fill signal for a wing leg** is modeled as a `Fill` with count 0 (-> RETRY_WING). The real
  IOC no_fill / partial shape from the create response / fill channel is a Phase-3 mapping.
* **The requote2 200 ms new-quote-live latency** is a sim constant; live it becomes the real
  OrderAck round-trip. The core is event-driven on OrderAck, so it needs no latency constant — but the
  Phase-2 spine must feed OrderAck/OrderCancelled from the create response AND the fill/status channel.
* **Bucket discovery** must supply the static `ticker -> (floor, cap)` map (record_range analogue);
  the core refuses to classify a bucket ticker without it.

## QUESTION FOR BRAD

The Phase-1 shadow is a pure no-lag state machine, so for a fast hour its E=0.10 shadow reads
0.45/0.55/+10.4c where `pf_ms_requote.py`'s ideal E=0.10 detail reads 0.43/0.57/+12.4c — the whole
gap is the ideal's hardcoded -1 s n-solve (the wings moved ~$0.02 of W in the final second). The no-
lag shadow is the honest "sim fill rule running live" for a real feed, and it happens to match the
lagging executor here. Do you want (a) the shadow left no-lag (recommended: it is what a live process
can actually compute, and dry mode then reports the live-achievable statistic), or (b) a shadow that
buffers ~1 s of wing history to bit-reproduce the ideal's -1 s convention (heavier, and arguably an
artifact)? Phase 1 ships (a); say the word for (b).
