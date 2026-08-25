# Rung 1.5 build report — the tape sim (opus48 build, 2026-08-19)

Commission: `sim/ceremony/rung15_commission.md`. Built by opus48 on Brad's order; review is a
separate opus48 session. Train days only; no sealed read; no network.

## Files written
- `sim/tape_sim.py` — the tape sim (CLI `--start --end --out --staleness --census`).
- `sim/loader.py` — ADDED `load_trades()` + `"trades"` kind (`.jsonl.gz`) reusing the same
  `_guard_seal` seal defense as markets/candles. No existing behavior changed.
- `sim/tests/test_tape_sim.py` — 18 tests.
- Smoke outputs (one day): `sim/out/tape_points.csv`, `sim/out/tape_report.md`,
  `sim/out/tape_receipt.json`.

## Law reused by import (NOTHING forked)
- Pairing / σ̂ (A3.2) / exclusion taxonomy / pin-escape outcomes → `census.build_census`.
  Eligibility = rows whose census status is `OK` or `NO_PAIR` (both carry a valid pair,
  non-degenerate G, a valid σ̂, and a consistent outcome).
- Per-leg fee (audited formula, golden literals) → `census.fee`; G → `census.hole_G`.
- G/σ̂ quintile edges + bucket assignment + OK-row read → `gate_fit.quintile_edges`,
  `gate_fit.bucket_of`, `gate_fit.read_ok_rows`. The EV curve's edges and per-bucket pin
  rates reproduce `gate.json` exactly.
- The SEAL (17 hardcoded UTC days) + trade-tape load → `loader`. tape_sim never sets
  `acknowledge_sealed_read`.

## census_train.csv verification
- Full sha256: `580d143fa5d3581a8bbee9d5cc2b45f800d25fa84db7967c3cf591a8ac7bb247`
  (matches the commission's `580d143f…` prefix). `EVCurve.from_census` recomputes the sha
  and refuses (`IntegrityError "sha mismatch"`) on any drift; recorded in the receipt as
  `census_csv_sha256`.

## Tests: `python -m pytest sim/tests/` → 73 passed (18 new + 55 pre-existing, all green)
New tests cover: fee/C composition + audited golden; honest-fills side selection (a
`taker_outcome_side="no"` H-market fill never sets the YES-leg price); staleness expiry
(stale/live/exact-boundary); EV bucket assignment + edge-tie-to-upper + `ev = fair − C`;
census-sha refusal + real-census acceptance; sealed-date refusal (`load_trades` on every
sealed day, and `run()` end-to-end); newest-first input sorted ascending on load;
boundary-duplicate dedupe by `trade_id`; leg reconstruction + anchor-drift hard-fail.

## Smoke run (ONE day, per hard law): `--start 2026-06-13 --end 2026-06-13`
stdout aggregates:
```
tape_sim aggregates (2026-06-13), staleness=60s
  eligible windows        : 23
  windows w/ events       : 23
  windows w/ EV+ moment   : 16
  evaluation events       : 134619
  EV+ events              : 36501
  EV+ dwell-seconds       : 4746.0
  policy(first EV+): entered=16 (esc 11/pin 5) total_payoff=-1.6780 mean=-0.1049
```
Reading: transient EV+ moments (paid C below the pair's bucket-fair EV) DO occur on the live
tape — 36,501 of them across 23 windows on this day — but entering at the first EV+ moment
of each window realized a NEGATIVE mean payoff (−0.1049/window). The EV+ dips were not
capturable into winning entries on this day, consistent with Rung 1's candle census (every
candle snapshot −EV, gate empty). One day only; not a conclusion.

## CONFESSIONS — judgment calls the commission did not settle
1. **Eligibility = census status ∈ {OK, NO_PAIR}.** The commission says "every
   census-eligible hour window" with "exclusion taxonomy as in census." `NO_PAIR` is a
   census verdict driven ONLY by candle-quoting at T−5 (a candle-economics test). The tape
   sim prices from TRADES, so I keep NO_PAIR windows (they have a valid pair + σ̂ + outcome)
   and drop only the `EXCL_*` windows. If the reviewer wants OK-only, it is a one-line
   filter change.
2. **Leg-ticker reconstruction matches census's OWN chosen K.** Rather than re-run the
   nearest-strike selection (a fork risk), `legs_for_row` picks the 1H market whose
   `floor_strike == threshold_K` from the census row and cross-checks A and K against the
   row with zero tolerance (hard fail on any drift). Nearest-strike law therefore lives in
   exactly one place (census).
3. **No prior-day σ̂ backfill.** `run` calls `build_census(eval_dates)` with no lookback
   day, so the first window(s) of the range's first day lack the 9-anchor tape and are
   `EXCL_SIGMA_*` (dropped, receipted by census) — exactly how census treats a single-range
   load. On 06-13 this dropped T=01:00 (`EXCL_SIGMA_HEAD`), leaving 23 of 24 hours. The
   orchestrator's 06-13..06-19 week run will likewise lose 06-13's first hour(s); to keep
   them, one would load the prior calendar day purely as anchor tape. I did not, to stay
   consistent with census and with the "one day" smoke discipline.
4. **Evaluation-event definition.** An event is a fill with `T−900 ≤ t ≤ T` on either leg
   while the OTHER leg's most-recent same-side fill is `≤ staleness_s` old. Fills before
   T−900 seed the live prices (warmup) but never emit an event; fills after T are ignored
   (settlement is at T).
5. **EV+ dwell-seconds ("carry-forward").** Each event's C is credited live-time from its
   timestamp until the next fill on either leg OR until the older of its two live fills
   expires (whichever comes first), clamped to T. EV+ dwell-seconds sums that duration over
   events with `ev_bucket > 0`. This is my operationalization of the commission's
   "EV+ seconds (carry-forward between events)".
6. **Per-minute histogram** counts EV+ EVENTS (not seconds) by `floor((T−t)/60)`; minute 0
   = the final minute before close.
7. **Policy "enter at first EV+ moment"** realizes `escape → 1−C`, `pin → −C` at that
   event's C; the mean is over windows that had ANY EV+ moment (windows with none do not
   enter and are not scored as 0).
8. **fair_bucket** = `1 − P̂(pin | quintile)` where P̂ is the per-quintile pin rate over
   `census_train.csv` OK rows; quintile via `gate_fit.bucket_of` (bisect_right, ties → upper
   bucket). Reproduces `gate.json` edges and rates.
9. **`count_fp` (trade size) ignored.** The honest-fills law prices at "the price paid by
   the most recent taker on our side" — a per-price, size-agnostic quantity (1 lot). Size is
   not used.
10. **`taker_outcome_side` is the side field** (as pinned by the commission). `taker_side`
    is ignored even though it equals `taker_outcome_side` in the sampled tapes.
11. **Timestamps.** created_time (microsecond ISO) → epoch float for staleness; sorting by
    the fixed-width ISO string is chronological (loader sorts ascending on load).
12. **Trade dedupe is fail-OPEN by `trade_id` union**, not the byte-identity assert that
    markets/candles use, because trade payloads are large lists and a boundary-duplicate
    market's two copies would legitimately union to the same superset. Documented in
    `load_trades`.
13. **Decimal for money** (prices, C, fair, EV, payoff) to reproduce the audited fee/C
    exactly; ages, dwell, and g_over_sigma are floats.
14. **Output names/dir.** Default `--out` is `sim/out/`; the sim writes `tape_points.csv`
    (one row per evaluation event), `tape_report.md` (aggregates only), `tape_receipt.json`.
15. **Staleness boundary is inclusive** — a leg exactly `staleness_s` seconds old counts as
    LIVE (`age <= staleness_s`).

---

# POST-REVIEW AMENDMENTS (2026-08-19) — implementing A15.1–A15.4

Review verdict: CONDITIONAL PASS (`rung15_review.md`). Every headline number was reproduced
byte-for-byte by the reviewer (two windows, both orientations; EV curve exact). The four
amendments below were adjudicated binding by the orchestrator and are now implemented. All
existing tests still pass (73 → 83).

## A15.1 (review F1, required) — loader sort by parsed epoch
- `loader.load_trades` now sorts by `parse_created_epoch(created_time)`, NOT the raw string.
  New `loader.parse_created_epoch` is the single parse; `tape_sim.parse_ts` is now an alias.
- Dead `order_seen` code deleted. `_side_fills` docstring corrected (no false "chronological
  input" claim; it parses to epoch and the walk re-sorts).
- New test `test_load_trades_variable_microsecond_width_sorts_chronologically` feeds
  VARIABLE-width fractional seconds INCLUDING a no-fraction timestamp and proves the loaded
  order is chronological and DIFFERS from a naive string sort (the no-fraction `…:01Z` sorts
  after `…:01.25Z` as a string because `'Z'`(90) > `'.'`(46) — the exact inversion class the
  reviewer measured). Plus `test_parse_created_epoch_handles_missing_fraction`.
- **Impact on numbers: zero.** The strangle smoke aggregates are byte-identical to the
  reviewed build (events 134619, EV+ 36501, dwell 4746.0, policy entered=16/−1.6780/−0.1049),
  because `walk_window` always re-sorted by parsed epoch. The defect was a false loader
  contract + a blind-spot test, both now fixed.

### Correction to confession #11 (do not rewrite history — this supersedes it)
Confession #11 above claimed "sorting by the fixed-width ISO string is chronological." That
premise was **FALSE**: `created_time` fractional seconds are variable-width, so string order
inverts chronology (a no-fraction timestamp sorts after a fractional one in the same second).
The corrected rule: **all trade ordering goes through `parse_created_epoch` (a parsed float
epoch); the `created_time` string is never used as a sort key.**

## A15.2 (review F2, required) — missing trades-file pre-check
- `run()` now pre-checks each day's trades file with `os.path.exists` per series, records
  genuinely missing days in the receipt's `missing_trade_days`, loads only present days, and
  CONTINUES (windows on a missing day simply get empty tapes → no events). The old
  unreachable sha-basename path is removed.
- New test `test_run_records_missing_trade_day_and_continues` (corpus has markets/candles but
  no trades files) asserts the day is recorded, the run finishes, and events == 0.

## A15.3 (review F3/F5, report wording)
- The report now carries a **Measure note**: event counts are trade-multiplicity-weighted;
  **dwell-seconds is the time-faithful measure**. It also breaks out how many eligible
  windows were census-status `NO_PAIR` (tape-priceable but candle-unpriceable; fair imputed
  from OK-row quintiles). On 06-13 that count is 0 (the week run will admit some).

## A15.4 (Brad's order) — the FLIP direction
- Flip = BUY NO on the HIGH line + BUY YES on the LOW line. Honest-fills mirrored in
  `_direction_legs`: flip high-leg price = most-recent NO-taker fill's `no_price_dollars`;
  flip low-leg price = most-recent YES-taker fill's `yes_price_dollars`. Same per-leg fee.
- `fair_flip = 1 + P̂(pin|quintile)` (`EVCurve.fair_flip`); `fair_flip_lin = 1 + 0.006·G`
  (`fair_linear(G,"flip")`). Payoffs: escape → 1 − C_flip, pin → 2 − C_flip.
- **Payoff-floor verified in code** (`flip_settlement_payout`) from the SAME result fields
  census uses: $2 on pin (H:no ∧ L:yes → both flip legs pay), $1 on every escape/boundary
  branch, hard-fail on the impossible $0 branch — and `run()` asserts this per window against
  the pin flag for the row's actual orientation. Test
  `test_flip_settlement_payout_all_branches_both_orientations` covers all combos.
- **Hard-floor count**: moments where C_flip (fees in) < $1.00 — riskless (flip pays ≥ $1 in
  every branch, so C_flip<$1 ⇒ positive regardless of outcome). Reported as events, windows,
  dwell-seconds, per-quintile, and the policy "enter at first hard-floor moment."
- CLI `--direction strangle|flip|both` (default `both`).

### New confessions (16–19)
16. **Single `tape_points.csv` with a `direction` column** (not per-direction files). Leg
    columns are GENERIC — `high_leg_price`/`low_leg_price` (+ ages) — because the two legs
    mean opposite sides per direction (strangle: YES-on-H/NO-on-L; flip: NO-on-H/YES-on-L).
    This RENAMES the reviewed strangle columns (`yes_price`→`high_leg_price`, etc.) and adds
    `direction`, `fair` (was `fair_bucket`), `ev` (was `ev_bucket`), `hard_floor`. Numbers
    unchanged; only the schema. `walk_window` still exposes the old `yes_price`/`no_price`/
    `ev_bucket` aliases in-memory so the reviewed strangle contract and its tests hold.
17. **Hard-floor is computed for both directions but reported as riskless only for the flip.**
    A strangle with C<$1 is NOT riskless (a pin still loses −C), so the side-by-side table
    shows `n/a` for the strangle hard-floor rows; the flip is where the $1 settlement floor
    makes C_flip<$1 a genuine riskless entry.
18. **Flip settlement uses the deterministic $2/$1 floor** (`settle_amount` = 2 on pin, 1 on
    escape) rather than re-deriving per-leg $1 payouts inside the walk; the two are identical
    (`flip_settlement_payout` proves it) and the deterministic form keeps the walk direction-
    agnostic (`payoff = settle_amount − C` for both directions: strangle settle = 1 escape /
    0 pin, flip settle = 1 escape / 2 pin).
19. **`min_anchor` σ̂ / eligibility unchanged for the flip** — the flip reuses the SAME
    census pair, σ̂, quintile, and pin/escape outcome as the strangle (only the leg sides and
    the fair/payoff formulas differ), so no new census interaction was introduced.

---

# POST-REVIEW AMENDMENTS ROUND 2 (2026-08-19) — implementing A15.5–A15.8

Flip review verdict: CONDITIONAL PASS. The flip ARITHMETIC verified fully correct (settlement
floor 23/23 windows both orientations, honest-fills mirror hand-reconstructed exact, zero
complement violations across 1.27M trades, fees single-applied). The four required fixes are
FRAMING/reporting plus one CSV column — no economic number changed. All prior tests stay
green (83 → 88).

## A15.5 (review-2 F1, required) — decompose GUARANTEED floor from realized payoff
- The flip hard-floor policy now reports TWO figures: the **GUARANTEED floor Σ(1−C)** (the
  only riskless component — the flip banks 1−C in every branch) and the pin-inclusive
  **REALIZED** payoff Σ(settle−C) (which adds the +$1 pin bonus, NOT guaranteed). The report,
  stdout, and receipt all carry both, and the word "riskless" is attached ONLY to the
  guaranteed total. New `first_hardfloor_C` in the walk summary; new `_hardfloor_policy`.
- Smoke (06-13): **guaranteed +0.1594** (mean 0.0177/window) vs **realized +1.1594**
  (mean 0.1288). The earlier build's single "+1.1594 riskless" framing is thereby corrected:
  only +0.1594 is riskless; the remaining +$1 came from one pin.

## A15.6 (review-2 F3, required) — distribution of guaranteed-floor magnitudes
- The report shows a sub-tick count (floors < $0.01), a magnitude histogram (`FLOOR_BUCKETS`),
  and the sorted per-window guaranteed-floor list. Smoke: **8 of 9** entered windows are
  sub-tick; the per-window floors are `[0.0004, 0.0005, 0.0007, 0.0018, 0.0018, 0.0018,
  0.0061, 0.0085, 0.1378]` — i.e. the riskless edge is one $0.14 window plus eight essentially
  zero (< one price tick) floors. Tests: `test_floor_histogram_flags_sub_tick`,
  `test_hardfloor_policy_decomposes_guaranteed_from_realized`.

## A15.7 (review-2 F2, required) — availability caveat (not simultaneous boxes)
- The flip hard-floor section now states plainly that moments counted at the 60s staleness
  tolerance are NOT proven simultaneously-available boxes (a fill on one leg is paired with
  the other leg's most-recent fill up to `staleness_s` old), citing the reviewer's measured
  ~36% of hard-floor dwell surviving a 5s tolerance on 06-13, and noting the orchestrator
  runs the week at BOTH 60s and 5s. No dual-staleness code added (the CLI `--staleness` flag
  already parameterizes it; the orchestrator invokes twice).

## A15.8 (review-2 F4, required) — hard_floor CSV column flip-only
- `tape_points.csv` now leaves `hard_floor` **BLANK for strangle rows** (a sub-$1 strangle is
  not riskless) and populates it (`0`/`1`) only for flip rows. Row assembly factored into
  `_csv_event_row`. Tests: `test_csv_hard_floor_blank_for_strangle`,
  `test_csv_hard_floor_populated_for_flip`, and an end-to-end
  `test_smoke_csv_strangle_rows_have_blank_hard_floor_flip_do_not` reading the written CSV.

### New confessions (20–21)
20. **Guaranteed floor is defined as 1−C at the ENTRY moment** (the escape branch of the flip
    payoff), independent of the realized outcome. This is the exact riskless quantity because
    the flip settlement floor is $1; the pin bonus (+$1) is reported separately and never
    labeled riskless.
21. **The one end-to-end CSV test runs the real one-day smoke inside pytest** (single day
    2026-06-13, non-sealed, per hard law) to assert the A15.8 column contract on the actually
    written file; it adds ~12s to the suite. Kept because the column contract is exactly the
    kind of thing a pure-unit test can certify while the wiring silently regresses.

---

# POST-REVIEW AMENDMENTS ROUND 3 (2026-08-19) — implementing A15.9 (cross-side refutation)

Flip review verdict: CONDITIONAL PASS — the flip ARITHMETIC verified fully correct
(settlement floor 23/23 both orientations, honest-fills mirror hand-reconstructed exact, zero
complement violations across 1.27M trades, single-applied fees). Then Brad added a
substantive law improvement, A15.9.

## A15.9 — cross-side refutation (Brad's insight)
- **The law:** within a leg, YES and NO share ONE book, so every trade prints the touch. A
  carried buy price `p_S` (from the last same-side taker fill) DIES at the first LATER
  opposite-side print whose implied bid `(1 − opposite_price) ≥ p_S` — because ask ≥ bid, the
  `p_S` ask provably no longer stands. Carried lifetime = `min(staleness horizon, first
  refuting print)`. Refute-ONLY (opposite prints never create/improve/extend a price;
  consistent prints with implied bid < p_S change nothing). No tick assumption — refute on
  `≥` exactly (sub-penny prices exist). A refuted leg is NOT live until its next same-side
  fill. Applies to both legs, both directions, EV+ evaluation, dwell, and the hard-floor
  counter.
- **Implementation:** each leg is now a full `_leg_stream` of `(ts, kind, price)` where kind
  is `SAME` (a fill on our side — sets carried buy price) or `OPP` (opposite-side print —
  carries implied bid `1 − opposite_price`). `_walk_direction` tracks per-leg `carried` and
  `refuted`; an OPP with implied bid ≥ carried (and still within staleness) sets `refuted`;
  a SAME clears it with a fresh carried price. OPP prints are fill boundaries in the merged
  stream, so a refuting print truncates the prior event's dwell at its timestamp. Liveness =
  carried present AND age ≤ staleness AND not refuted. Events still emit only on SAME fills.
- **Known-answer (commission-mandated), PASSES:** the 2026-06-13 02:00 flip "hard floor" at
  t_minus 81.309 (C=0.9982, low leg 0.9900 aged 38s) is ABSENT after A15.9 — NO-taker prints
  at implied yes-bid 0.99 (≥ carried 0.99) refute the carried low-leg price during the 38s
  gap. Test `test_a159_known_answer_0200_flip_hardfloor_refuted`.
- **New tests (7):** refutation kills carried price / blocks event; equal-price refutes (≥,
  not >); opposite print below carried does NOT refute; refutation truncates dwell (60s→30s);
  refuted leg revives only on the next same-side fill; same-timestamp tie-break (SAME
  survives); the known-answer. Four prior flip pure-tests updated to the `(ts,kind,price)`
  stream shape (fixtures now legitimately carry SAME/OPP). The smoke CSV test was refactored
  onto a module-scoped `smoke_rows` fixture so the real smoke runs ONCE for all CSV/known-
  answer assertions. **95 tests pass.**
- **Before/after (smoke 2026-06-13, `--compare-no-refute`).** The no-refute counterfactual
  reproduces the pre-A15.9 numbers EXACTLY (regression check), and refutation prunes them:

  | direction | metric | before (pre-A15.9) | after (A15.9) | survived % |
  |---|---|---|---|---|
  | strangle | evaluation events | 134619 | 119367 | 88.7 |
  | strangle | EV+ events | 36501 | 32913 | 90.2 |
  | strangle | EV+ dwell-seconds | 4746.0 | 2253.7 | 47.5 |
  | strangle | hard-floor events (n/a for riskless) | 116671 | 102784 | 88.1 |
  | strangle | hard-floor dwell-seconds | 17435.1 | 7692.5 | 44.1 |
  | flip | evaluation events | 132693 | 113599 | 85.6 |
  | flip | EV+ events | 61202 | 49828 | 81.4 |
  | flip | EV+ dwell-seconds | 10541.1 | 4217.3 | 40.0 |
  | flip | hard-floor events | 2654 | 1940 | 73.1 |
  | flip | hard-floor dwell-seconds | 631.9 | 144.3 | 22.8 |

  The flip hard-floor **guaranteed floor** falls from +0.1594 (9 windows, one $0.1378) to
  **+0.0423 (8 windows, ALL sub-tick)** — the single non-sub-tick floor (the 02:00 window)
  was refuted away. Dwell is hit hardest (flip hard-floor dwell → 22.8%), confirming the
  refutation removes exactly the carried-price time the touch no longer supported.

### New confessions (22–24)
22. **Implied bid computed as `1 − opposite_price`** (the law's literal form), which equals
    the record's same-side price field under the verified-exact complementarity
    (`yes+no == 1.0000`); I use `1 − field_opp` rather than reading `field_S` so the code
    mirrors "bid(S) = 1 − q". No per-trade complementarity assert is added (the review
    verified 0 violations in 1.27M trades); a future non-complementary record would be caught
    only if it flipped a refutation, not by an explicit guard.
23. **Same-timestamp tie-break:** at an equal epoch, OPP prints are processed BEFORE SAME
    prints (sort key `(ts, 0 if OPP else 1)`), so a co-timestamp same-side fill is the
    surviving evidence — its fresh carried price is not refuted by a same-instant opposite
    print (per the commission's directive). Same-side vs same-side ties keep a stable order
    (multiplicity preserved).
24. **`--compare-no-refute` gates the counterfactual pass** (default OFF). The primary run is
    single-pass A15.9-ON (the law); the before/after table only appears when the flag is set
    (used for the smoke), so the orchestrator's week run stays one pass per staleness. The
    no-refute pass reuses the same walk with `refute=False` (OPP prints dropped from the
    merged stream), which is the exact pre-A15.9 behavior — hence a live regression oracle.

**NOTE (superseded by A15.10 below):** confession 22's "no per-trade complementarity assert"
and the round-3 dwell/survival numbers are corrected in ROUND 4 — the dwell figures there
(e.g. flip hard-floor dwell → 22.8%) were inflated by the F1 defect and are superseded.

---

# POST-REVIEW AMENDMENTS ROUND 4 (2026-08-19) — implementing A15.10 (dwell defect fix)

Review-3 verdict: CONDITIONAL PASS. The A15.9 EVENT-LEVEL implementation verified EXACT
(independent re-implementation, row-by-row zero mismatches on 07:00 both directions, known-
answer confirmed, regression oracle confirmed, tie-break adjudicated honest). One MAJOR
defect in the DWELL clock (F1) — event existence, C, EV, payoffs, policies, and the known-
answer were already exact and are UNTOUCHED.

## A15.10 (review-3 F1, required) — dwell keys on state changes only
- **The defect:** dwell used `next_ts = the next merged element of ANY kind`, so a CONSISTENT
  (non-refuting) opposite print truncated the interval it should be transparent to. The tail
  from a consistent OPP to the next real state change was credited to nothing — ~44–49% of
  time-faithful dwell wrongly deleted, and the "% survived refutation" mis-attributed
  consistent-OPP truncation to refutation.
- **The fix:** an event's dwell now runs until the FIRST of — next SAME fill on either leg /
  first REFUTING opposite print against THIS event's carried prices (hp/lp are fixed until
  the next SAME) / staleness expiry / T. Consistent opposite prints are skipped in the dwell
  scan. The scan only visits the OPPs of the current inter-SAME interval, so it stays
  O(total fills). Event existence/C/EV/payoff/hard_floor are unchanged.
- **Known-answer against the reviewer's independent figure, MATCHES:** 2026-06-13 07:00 flip
  total window dwell = **596.27s** (reviewer's faithful 596.26s; the pre-fix code gave
  305.15s). Confirmed from the written CSV.
- **F2 hardening (done):** `_leg_stream` now asserts per-trade complementarity
  `abs(yes+no − 1) < 0.0001` and HARD-FAILS on a violation (rather than silently
  mis-refuting on a wrong implied bid). Confession 22's "no assert" is thereby retracted.
- **New tests (3):** a CONSISTENT OPP between two SAME fills does NOT shorten dwell (60.0s,
  the reviewer's proof shape); a REFUTING OPP still truncates (30.0s) — the fix keeps
  refutation live; non-complementary trade hard-fails in `_leg_stream`. **98 tests pass.**

### Corrected before/after (smoke 2026-06-13, `--compare-no-refute`) — supersedes ROUND 3
The "before" column (pre-A15.9 same-side print-to-print dwell) is unchanged; the "after"
(A15.9 with the corrected dwell clock) rises, so survived-% now reflects the TRUE refutation
share, not the consistent-OPP artifact.

  | direction | metric | before | after | survived % (was, buggy) |
  |---|---|---|---|---|
  | strangle | evaluation events | 134619 | 119367 | 88.7 (88.7) |
  | strangle | EV+ events | 36501 | 32913 | 90.2 (90.2) |
  | strangle | EV+ dwell-seconds | 4746.0 | 4128.1 | **87.0** (was 47.5) |
  | strangle | hard-floor events | 116671 | 102784 | 88.1 (88.1) |
  | strangle | hard-floor dwell-seconds | 17435.1 | 14422.0 | **82.7** (was 44.1) |
  | flip | evaluation events | 132693 | 113599 | 85.6 (85.6) |
  | flip | EV+ events | 61202 | 49828 | 81.4 (81.4) |
  | flip | EV+ dwell-seconds | 10541.1 | 7590.0 | **72.0** (was 40.0) |
  | flip | hard-floor events | 2654 | 1940 | 73.1 (73.1) |
  | flip | hard-floor dwell-seconds | 631.9 | 224.7 | **35.6** (was 22.8) |

  Event counts and guaranteed floors are unchanged (dwell-only fix): flip hard-floor
  guaranteed floor still +0.0423 over 8 all-sub-tick windows. The dwell rows all rose;
  refutation's real bite on flip hard-floor dwell is 64.4% (to 35.6% surviving), not the
  77.2% the defect implied.

### New confession (25)
25. **Dwell boundary scan uses THIS event's fixed carried prices (hp, lp).** Because carried
    prices change only at SAME fills and each event sits at a SAME fill, the OPPs between it
    and the next SAME are tested for refutation against a single (hp, lp) pair — correct by
    construction and O(total fills) overall. A refuting OPP that lands after a leg's staleness
    expiry is harmless: `min(boundary, expiry, T)` already ends the interval at expiry.
