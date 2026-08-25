# Rung 1.5 commission — the tape sim (2026-08-19, ordered by Brad)

Brad's words: "lets just build out a sim that compares time prices of both markets. See
how many points where C would be lower than EV within those 15 minutes."

Light ceremony (Brad present and interactive): commission → opus48 build → one opus48
adversarial review → run on the test week. No sealed read is involved; train days only.

## What it is

For every census-eligible hour window in the input range, walk the executed-trades tape
through the final 15 minutes (T−900s .. T) and measure, moment by moment, what a taker
actually paid for the corridor pair — then count when that cost was below the pair's
expected value.

## Data

- `historical-data/{15-minute,1-hour}/trades/YYYY-MM-DD.jsonl.gz` — gzip JSONL, one line
  per market: `{"ticker":..., "trades":[raw API trade objects]}`. Trades are newest-first;
  sort by `created_time` (microsecond ISO) at load. Fields per trade: `created_time`,
  `yes_price_dollars`, `no_price_dollars`, `count_fp`, `taker_side` /
  `taker_outcome_side` ("yes"/"no"), `taker_book_side`, `trade_id`, `ticker`.
- Markets metadata + pairing: SAME law as Rung 1 (import from `sim/census.py`, do not
  re-implement): anchor A = 15M floor_strike; nearest 1H strike K; G=|A−K|; orientation;
  σ̂ = sample stdev of 8 diffs from 9 trailing 15M anchors incl. A(T) (A3.2), stitched
  backward across days; exclusion taxonomy as in census.
- `sim/out/census_train.csv` (sha 580d143f…) — source of the EV curve. Verify sha before
  use; refuse on mismatch (A3.10 spirit).

## The price series (honest-fills law)

- We buy YES on the HIGH line and NO on the LOW line. At time t, the achievable price of
  a leg is the price paid by the MOST RECENT TAKER ON OUR SIDE of that leg
  (`taker_outcome_side` == the side we'd buy; use that side's price field). This is what
  a real taker actually paid, not a quote model. No bid/complement inference.
- Staleness: a leg's price is live for STALENESS_S = 60s after its fill; a moment counts
  only when BOTH legs are live. (Parameter, reported in output.)
- C(t) = leg1 + fee(leg1) + leg2 + fee(leg2), fee(p) = ceil(0.07·p·(1−p)·10000)/10000 —
  the audited formula, per leg.

## EV

- Primary: bucket-fair. From census_train.csv OK rows, pin rate by G/σ̂ quintile (edges
  from the train distribution, same as gate_report). fair = 1 − P̂(pin | quintile of this
  window's G/σ̂). EV(t) = fair − C(t).
- Secondary column: linear law fair_lin = 1 − 0.006·G (the 0.6pp/$ census law).
- Windows without σ̂ (head) are excluded, same as census.

## Outputs (sim/out/)

- `tape_points.csv` — one row per EVALUATION EVENT (a fill on either leg while the other
  is live): date, close_time, t_minus_s, G, sigma_hat, g_over_sigma, quintile, leg prices
  + ages, C, fair_bucket, fair_lin, ev_bucket, ev_lin, pin (window outcome), window payoff
  of entering at this C (escape → 1−C, pin → −C).
- `tape_report.md` — aggregates ONLY (no per-trade dumps): per window count of EV+ events
  and EV+ seconds (carry-forward between events); distribution of EV+ moments across the
  15 minutes (per-minute histogram); by quintile; realized P&L of the policy "enter once
  at the FIRST EV+ moment of the window"; comparison line vs the candle C-ladder finding
  (every candle snapshot negative). Print aggregates only to stdout.
- Receipt json: input files + shas, parameters, row counts.

## Law

- Train days only. Loader must hard-refuse the 17 sealed dates (reuse `sim/loader.py`
  SEALED_DATES or import its guard). No API calls — local files only.
- Runner: `python sim/tape_sim.py --start 2026-06-13 --end 2026-06-19 --out sim/out/`.
  Refuse silently-missing days (report which days lacked trades files).
- Tests under sim/tests/ (fee, pairing reuse, staleness, EV bucketing, seal refusal).
- Confessions section in the build report: every judgment call not covered here.

— Commissioned by Claude (Fable), on Brad's order. Build: opus48. Review: opus48, separate session.

## Amendments (2026-08-19, post-review adjudication + Brad's flip order)

Review verdict: CONDITIONAL PASS (rung15_review.md). Adjudicated as binding:

- **A15.1 (review F1, required):** `loader.load_trades` must sort by PARSED epoch, not by
  the `created_time` string — microsecond fields are variable-width, string order inverts
  chronology (40 measured inversions on 2026-06-13). Fix the sort, correct build-report
  confession #11, delete the dead `order_seen` code, and add a variable-microsecond-width
  sorting test. (Zero impact on produced numbers — the walk re-sorts — but the loader's
  advertised contract was false and its test certified the false claim.)
- **A15.2 (review F2, required):** pre-check trades-file existence per day; record genuinely
  missing days in the receipt's `missing_trade_days` and continue, instead of the current
  unreachable-graceful-path-plus-hard-crash.
- **A15.3 (review F3/F5, report wording):** the report must state that DWELL-SECONDS, not
  event counts, is the time-faithful measure (event counts are trade-multiplicity-weighted),
  and must break out how many eligible windows were census-status NO_PAIR (tape-priceable
  but candle-unpriceable; their fair is imputed from OK-row quintiles).
- **A15.4 (Brad's order — the FLIP direction):** the sim measures BOTH directions.
  Flip pair = buy NO on the HIGH line + YES on the LOW line. Payoff floor: $1 in every
  escape/boundary branch, $2 on pin (both orientations, both settlement rules verified).
  Honest-fills law mirrored: high-leg price = most recent NO-taker fill's no_price; low-leg
  price = most recent YES-taker fill's yes_price. Same fee law per leg. fair_flip =
  1 + P̂(pin | quintile); fair_flip_lin = 1 + 0.006·G. Payoffs: escape → 1 − C_flip,
  pin → 2 − C_flip. Additional hard-floor count: moments where C_flip (fees in) < $1.00 —
  riskless entries needing no pin model. CLI `--direction strangle|flip|both`, default
  both; outputs get a `direction` column (or per-direction files) and the report shows the
  two directions side by side, including the flip policy "enter once at first hard-floor
  moment" and "enter once at first EV+ moment".
- Review F4 (histogram t=T−900 edge) and F6 (fail-open trade_id dedupe): accepted as
  documented, no change ordered.

## Amendments round 2 (2026-08-19, flip review adjudication — review record in rung15_review.md addendum)

Flip review verdict: CONDITIONAL PASS — flip arithmetic verified correct (settlement floor
23/23 windows both orientations, honest-fills mirror exact, zero complement violations in
1.27M trades, fees single-applied); required fixes are FRAMING and one CSV column:

- **A15.5 (review-2 F1, required):** the hard-floor policy report must decompose GUARANTEED
  floor (Σ(1−C), the only riskless component) from pin-inclusive realized payoff, and
  report both. The riskless claim may only ever cite the guaranteed component.
- **A15.6 (review-2 F3, required):** report the distribution of guaranteed floor magnitudes
  (per entered window), not just counts — sub-tick floors (<$0.01) must be visible as such.
- **A15.7 (review-2 F2, required):** the report must state that hard-floor moments at 60s
  staleness are NOT simultaneously-available boxes (measured: only 36% of hard-floor dwell
  survived a 5s tolerance on 2026-06-13). The orchestrator will run the week at BOTH 60s
  and 5s staleness and present both.
- **A15.8 (review-2 F4, required):** the `hard_floor` CSV column must be blank/absent for
  strangle rows (a sub-$1 strangle is not riskless); flip rows only.

## Amendment round 3 (2026-08-19, Brad's cross-side insight)

- **A15.9 (CROSS-SIDE REFUTATION, ordered by Brad):** within each leg, YES and NO share
  one book, and every trade prints at the touch of one side: a taker buying side X at
  price p proves side-X ask = p, equivalently the OTHER side's bid = 1−p, at that instant.
  Therefore an opposite-side print can REFUTE a carried same-side price: if our carried
  buy price for side S is p_S (from the last S-taker fill) and a LATER opposite-side fill
  implies bid(S) ≥ p_S, the ask at p_S cannot still be standing (ask ≥ bid, no locked
  book) — the carried price is DEAD regardless of staleness remaining. Law: a leg's
  carried price expires at min(staleness horizon, first refuting opposite-side print).
  Refute-only — opposite-side prints never create or improve a carried price (no
  availability is ever fabricated); consistency (implied bid < p_S) does NOT extend the
  staleness horizon. No tick-size assumption: refute on implied bid ≥ p_S exactly
  (sub-penny prices exist in this tape; do not assume $0.01 ticks). Applies to both legs,
  both directions, and to the hard-floor counter. Verified example: the 2026-06-13 02:00
  flip "hard floor" at C=0.9982 is refuted — NO-taker prints at yes-bid 0.99 throughout
  the 38s gap pin the yes ask ≥ 1.00, so true C ≥ ~1.008.
- **A15.10 (review-3 F1, required):** dwell truncation must key on state CHANGES only: an
  event's dwell runs until the next SAME fill on either leg, the first REFUTING opposite
  print (on a still-live leg), staleness expiry, or T — whichever first. CONSISTENT
  (non-refuting) opposite prints are transparent to the dwell clock (the law says they
  change nothing; review-3 measured them wrongly deleting ~44–49% of time-faithful dwell
  and mis-attributing the loss to refutation). Add a test asserting a consistent OPP does
  not shorten dwell; correct the before/after "% survived" attribution in the report.
  Optional hardening (review-3 F2): assert per-trade yes+no price complementarity in
  _leg_stream (verified 0 violations in 1.27M trades; a future violation should hard-fail,
  not silently mis-refute).
