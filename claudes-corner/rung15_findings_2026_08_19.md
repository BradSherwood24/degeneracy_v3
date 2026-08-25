# Rung 1.5 findings — the trades-tape sim (2026-08-19, evening)

The full record of what the executed-trades tape said about the corridor pair, one test
week (2026-06-13..19, 158 census-eligible windows, ~9M trades walked). Ceremony trail:
`sim/ceremony/rung15_commission.md` (amendments A15.1–A15.10), `rung15_review.md` (three
review rounds), `rung15_build_report.md` (four build rounds, 98 tests). Frozen outputs:
`sim/out/week60_r2/` and `week5_r2/` (post-refutation, definitive); `week60/`, `week5/`
(pre-refutation, preserved). All train data; the seal never touched.

## The instrument

- **Data**: Kalshi `GET /markets/trades` — historical, microsecond timestamps, price,
  size, taker side. NO historical orderbook exists (WS-only, real-time). Trades retention
  ~65 days (shorter than candles): empty ≤2026-06-12, present from 06-13.
- **Completeness proven**: summed fetched trade sizes match the exchange's own per-market
  `volume` field 96/97 exactly on the probe day (the 97th predates trades retention).
- **Honest-fills law**: a leg's price at time t = what the most recent taker ON OUR SIDE
  actually paid; staleness horizon (60s primary / 5s bracket); C includes audited per-leg
  taker fees.
- **Cross-side refutation (A15.9, Brad's law)**: within a leg, YES and NO share one book,
  so every trade prints the touch — an opposite-side print implying bid ≥ our carried
  price KILLS that carried price (ask ≥ bid; a locked book cannot stand). Refute-only;
  no tick assumption. Independently verified exact (row-by-row zero mismatches).
- **Dwell clock (A15.10)**: dwell truncates only on state changes (SAME fill, refuting
  print, expiry, T); consistent opposite prints are transparent.

## The kill chain — how each apparent edge died

1. **Naive week numbers looked alive** (strangle first-EV+ +0.95¢/wk; flip fresh-bucket
   event-mean +1.84¢; 80 "riskless" hard-floor windows realizing +3.2¢).
2. **V2's stale-book-mirage test** (stratify by stale-leg age): the flip's EV+ rate GREW
   with staleness (33%→77%) while realized payoff decayed negative — the stale tail was
   leg-lag phantom, V2's mirage reborn cross-leg. (Strangle dips were fresh-concentrated
   and survived this test.)
3. **Event-weighting distortion**: the flip fresh bucket's +1.84¢ event-mean became
   −2.7¢/window as a per-window first-entry policy — busy windows and pin-payoff events
   dominated the event average. Windows, not prints, are the unit.
4. **Cross-side refutation**: the showcase "riskless box" (06-13 02:00, C=0.9982 on a
   38s-old $0.99 leg) was DISPROVEN by 20 opposite-side prints pinning the ask ≥ $1.00
   through the entire gap. The known-answer test enshrines it. Refutation killed the one
   non-trivial floor of the smoke day and every flip floor >$0.053 in the week.
5. **The dwell-clock bug** (found by review round 3) was the mirror failure — it
   UNDERSTATED exposure ~45% and overstated refutation's bite (true strangle EV+ time
   survival under refutation: 87%, not 47%). Fixed before the definitive run.

## Definitive week results (post-refutation, fresh pairs ≤1s)

**Strangle depth ladder** (enter once at first moment C ≤ fair − margin):

| margin | %time | windows | first-entry mean | pins |
|---|---|---|---|---|
| EV−0 | 12.1% | 124 | +0.63¢ | 19 |
| EV−5 | 6.6% | 93 | +1.97¢ | 19 |
| EV−10 | 4.4% | 71 | **+2.58¢** | 19 |
| EV−15 | 2.9% | 58 | +1.68¢ | 19 |
| EV−20 | 2.0% | 46 | −1.48¢ | 19 |

**All 19 pin windows appear at EVERY rung** — doomed windows dip through every threshold
(the market reprices coming pins as "cheapness"): Rung 1's adverse selection at tape
resolution. Peak +2.58¢ at EV−10; SE ≈ ±5–6¢ → statistically unresolved on one week.

**Flip depth ladder** (fair = 1 + P̂(pin|quintile)):

| margin | windows | first-entry mean | pins |
|---|---|---|---|
| EV−0 | 153 | −2.75¢ | 15 |
| EV−10 | 91 | +0.07¢ | 9 |
| EV−15 | 61 | **+3.36¢** | 7 |
| EV−20 | 27 | +0.41¢ | 3 |

Opposite pin signature: pins DRAIN with depth (15→3; deep-cheap flip windows pinned 11%
vs ~20% implied) — informed sellers release the pin lottery when they know it's dead. The
flip's $1 floor bounds the damage (wrong costs C−1, right pays ~+0.9), which is why its
deep rungs stay positive despite adverse flow. Same caveat: ±3–4¢ SE, uncertified.

**Sub-$1 flip entries (the hard floor)**: existed in **34.8% of windows** (55/158),
robust across staleness brackets. Realized +4.2¢/window entered (+4.3% on capital);
guaranteed floor component only +0.6¢/window (48/55 floors sub-tick). Timing: **late** —
58% of sub-$1 dwell in the final 4 minutes, median first touch T−6min, birth seam ≈ none.
Gap profile: 88% of $0–10-gap windows offer it, 13% of $30–50, 0% above $50.

**The conditioning discovery (kills the naive x-framing)**: writing $1 = EV − x gives
x = P(pin). Bucket-level x̄ = 15.2¢ across all windows, but only 10.0¢ in floor windows
and 6.1¢ event-weighted at floor moments — and **all 9 wide-gap (G≥$30) sub-$1 windows
ESCAPED (0 pins), mostly going sub-$1 late**. The market offers the floor after spot has
run from the corridor and the CONDITIONAL pin probability has collapsed; the static
bucket curve is stale news at that moment. This dissolves the "2 pins vs 5.4 expected"
gap (conditioning, not luck) and reclassifies the sub-$1 flip: a floor-protected way to
collect sub-tick dust on resolved lotteries. Riskless, positive, not a trade.

## What Rung 1.5 concludes

At full trade-tape fidelity, with refutation applied and honest aggregation, **this pair
is efficient minus fees in both directions**. This is a stronger kill than Rung 1: it
survives the candle-resolution objection that motivated the whole tape effort. Three
shapes remain unresolved (not edges — hypotheses with shapes), preregistered for the
full-tape run:

1. **Strangle EV−5..10 bump** holds near +2¢ with SE crushed? (Watch q3: it supplies the
   most deep dips AND sits on the census pin-curve's suspicious q2/q3 flat spot.)
2. **Flip EV−10..15 bump** holds near +3¢ with the pin-drain visibly overpaid-for by the
   floor asymmetry?
3. **Conditional-x sub-$1**: does sub-$1 EVER print early/unresolved (wide gap, spot
   still near the corridor, pin genuinely alive)? 2 of 9 wide-gap cases were
   earlier-window this week. Requires conditioning x on spot-vs-corridor position at
   entry (the 15M tape is the spot proxy). This is the corrected form of the question.
4. **Brad's exponential frontier (frozen 2026-08-19 ~19:50 ET, BEFORE anyone read the
   full-tape outputs — full60 was concatenated on disk but unread; full5 still running)**:
   on the week-data strangle surface Brad sees a positive ridge shaped like an
   exponential frontier in (minutes-remaining, margin) — deep-margin entries in the
   MID-window sit well positive (e.g. ~EV−15 at ~8 minutes remaining), with the
   required margin decaying as time runs out until the final-minute pin cliff kills
   everything. Proposed mechanism: pins concentrate in the final minute, so a deep
   discount seen mid-window is cheapness NOT yet contaminated by doomed-window flow —
   time-to-close is the missing conditioning variable in the flat depth ladder.
   Test on the full tape: does a contiguous green region above the frontier survive
   min-n ≥ 10 at fresh ≤ 1s AND survive excluding the discovery week (06-13..19)?
   Prediction if real: the ridge sharpens; if artifact: it dissolves into isolated
   small-n cells.

## FULL-TAPE VERDICTS (2026-08-19 ~22:20 ET — 50 train days 06-13..08-01, 1,142
## strangle / 1,142-window corpus, chunked run full60/full5, ex-week cut = 43 days)

Mechanics: the monolithic 50-day run MemoryErrors (tape_sim loads all trades up front);
rerun as 8 week-sized chunks per staleness — law-equivalent (windows partition by
_assigned_day, tape_sim.py:419; EV curve is the sha-pinned census in every chunk) —
then concatenated. Ex-week = grep-excluding 06-13..19 before re-aggregation. SE script
cross-checked against the aggregator (n/means exact) before trusting variances.

1. **Shape 1 (strangle EV−5..10 bump): DEAD.** Full-corpus ladder is negative at every
   rung and monotonically worse with depth: EV−0 −1.56¢ (n=949) → EV−10 −3.25¢ (n=534)
   → EV−20 −7.11¢ (n=363). All 176 pins appear at every rung. Ex-week: worse still
   (EV−10 −4.14¢). The week's +2.58¢ was pin-luck: week pin rate 12.0% vs 15.4% full.
2. **Shape 4 (Brad's exponential frontier): DEAD.** The week's green wall dissolves to
   a uniformly red surface at min-n ≥ 10, fresh ≤ 1s — every (margin, minutes-remaining)
   cell negative except scattered small-n cells at depth. Brad's cell (EV−15, 8 min):
   −5.2¢ on n=57. The frozen prediction discriminated exactly as designed: artifact.
   NOTE the final-minute death wall DID replicate (entries in the last 1–2 min lose
   −5..−8¢ at every margin, n=250–380/cell) — the pin concentration is a confirmed law
   of the pair; it just has no tradable complement elsewhere on the surface.
3. **Shape 2 (flip EV−10..15 bump): SURVIVES SHAPE, UNRESOLVED SIZE.** Monotone-improving
   ladder with pin-drain intact, full corpus AND ex-week: ex-week EV−10 +1.91¢ (n=541,
   t=1.46), EV−15 +2.56¢ (n=345, t=1.64), EV−20 +3.82¢ (n=132, t=1.23), EV−25 +4.99¢
   (n=107, t=1.57); pins drain 131→12. Every rung individually under 2 SE even on 43
   days. The floor-asymmetry mechanism stands; the size of the edge remains uncertified
   and may be small enough to be depth/fill fiction.
4. **Sub-$1 flip at scale: real, riskless-by-construction, small.** Ex-week: 308/975
   windows (31.6% full-corpus), mean +3.00¢ per entered window (SE 0.87¢), minimum
   realized payoff +0.001¢ (every payoff positive — the $1 floor is arithmetic).
   Pins supply ~2/3 of the mean (7 pins ≈ +2¢ of the +3¢); the guaranteed floor
   component stays sub-tick dust. Conditional pin rate after a sub-$1 print: 2.5%
   (9/361 full corpus) vs 15.4% base — the conditioning discovery replicates: floors
   print on ~resolved lotteries. ≈ +0.95¢/window unconditional. Open question is
   entirely microstructure: fillable size at those prints, maker-vs-taker race.
5. Shape 3 (conditional-x sub-$1, spot-vs-corridor at entry) remains the one
   unexecuted analysis; its headline is already visible in the 2.5%-vs-15.4% collapse.

**Standing verdict after the full tape: the strangle is dead in every direction we can
measure. The flip carries a persistent, monotone, floor-protected positive shape that
no cut has killed — and no cut has certified.** The next honest step is not more slicing
of this tape; it is depth: do sub-$1 and deep-discount flip prints carry fillable size
(count_fp is in the raw trades), and does the shape survive per-window capital
accounting at realistic size?

## Brad's conditioning generalization (2026-08-20, pre-unseal)

Observing that the flip is EV−x-positive ONLY in Q5 while sub-$1 works everywhere,
Brad asked: "doesn't that mean our flip EV is calculated too high?" Yes — and it
generalizes the resolved-lottery discovery to the whole policy: the census fair
(1 + bucket-P̂(pin)) is a STATIC average, but dips select for informed pin-death flow,
so the conditional pin probability at entry moments sits below the bucket rate in
every quintile. Consequences, all observed: flip EV−x fails in Q1–Q4 (buying dead pin
probability); sub-$1 works everywhere (model-free floor arithmetic); Q5 EV−10 survives
because the 10¢ discount exceeds the ~7–8¢ conditional overstatement with the floor
bounding drained losses; and symmetrically the strangle's fair (1 − P̂) is UNDERstated,
making Q1-strangle bars conservative — which is why that side's survivors are real.
The frozen policy, honestly described: floor arithmetic where the model is known-stale,
plus the one bucket per direction where the discount exceeds the conditioning bias.

## V2 lessons imported (from the archaeology, `degeneracy_v2`)

- The stale-book mirage (REST 2–10s behind WS; "+10.4¢ edge" was self-front-running) —
  its stratify-by-staleness test is now standard here and killed our flip stale tail.
- Sided trade-tape sparsity ≠ feed lag: V2's thresholds (2s book-age guard, 45s watchdog,
  max(lag,silence)) apply to a LIVE WS book only — shelf them until we run one. Then use
  them as written.
- If we ever record the WS feed: record `sid`/`seq` (V2's recorder dropped them; dropped
  deltas were only detectable via book under-runs).
- Trades are the only independent anchor ("a real execution cannot lag") — our substrate.
- V2's "+7¢/entry fill tax" was retroactively remeasured as its own staleness bug
  (~+0.5–1¢ real); do not inherit the stale figure.

## Meta (for whoever reads this next)

Brad personally caught, in one session: the V2-lag rhyme, the event-weighting distortion,
the cross-side refutation law, and the "$1 = EV − x" framing whose honest answer exposed
the conditioning flaw. The ceremony caught: the crash-day bug, the string-sort bug, the
flattering hard-floor framing, and the dwell clock. Twice my quick scratchpad scripts were
wrong where the twice-reviewed sim was right. The discipline is not overhead; it is the
product. 🐀⚓
