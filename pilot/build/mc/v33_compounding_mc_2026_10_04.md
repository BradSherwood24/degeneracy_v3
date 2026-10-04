# V3.3 compounding Monte Carlo -- "11 rungs, 1/3 of the balance per ladder, scale the lots to keep the ratio"

*2026-10-04 ~23:00Z. Brad: "We're at $22 for a full 11-rung, so roughly 1/3 of the balance can be tied into a
single trade. Could you also do another monte carlo for me? Staying at the 11-rung, and 1/3 of the balance, just
increasing sizing of orders at those rungs to keep the ratio, how are we looking?"*

Script: `pilot/build/mc/v33_compounding_mc.py` (stdlib only; `python v33_compounding_mc.py 20000`).

## Short answer

At the live edge, scaling lots per rung to 1/3 of the balance turns $69 into a median **~$430 in 90 days**
(p10 $330, p90 $565), reaching 6 lots per rung. Nothing changes for the first month: the step from 1 to 2
lots needs $132 of balance, which the median path reaches around day 35. The proxy's 2-contracts-per-order
cap, as it stands today, holds the 90-day median to **~$315**. Flat 1 lot per rung is **~$225**.

Two things the frozen falsifier does NOT survive at size, and both need an amendment in Brad's words before
the first size step: the S4 day-loss kill is written as $3.00, and the one-legged pin as 2 CONTRACTS. At 3+
lots per rung an ordinary adverse-repricing window like 06:00Z (-$0.23 at 1 lot) is -$0.70 and a bad day
crosses $3.00 in 37% of 90-day paths; one naked rung at 2 lots is already 4 contracts = KILL.

## Sample (the live V3.3 armed windows, 1 lot per rung)

48 armed windows with ledger rows, 2026-09-30 .. 2026-10-04 22:00Z. EXCLUDED: 10-02 02:00Z (the pre-gate 11-naked
incident; the defect is fixed and its ledger row is collateral, not P&L). INCLUDED as -$0.40: 10-03 02:00Z (two
naked contracts, the pin event; also fixed by gates A-E, kept as an honest tail at 1-in-48). P&L = ledger
`realized_delta` = venue revenue - cost - fees.

| window | lots | P&L at 1 lot/rung |
|---|---|---|
| 09-30 22:00Z | 2 | +$0.311 |
| 10-03 00:00Z | 1 | +$0.089 |
| 10-03 01:00Z | 3 | +$0.316 |
| 10-03 02:00Z | 2 naked | -$0.400 |
| 10-04 06:00Z | 8 | -$0.226 |
| 10-04 14:00Z | 11 | +$1.580 |
| 10-04 18:00Z | 3 | +$0.266 |
| 10-04 20:00Z | 11 | +$1.642 |
| 40 other windows | 0 | $0 |

Mean +7.45c per window = **+$1.71 per day** at 23 windows a day (the 21:00Z hour has no $100 buckets).
Per-window sd $0.336 -> standard error of the mean **4.8c**: the mean is not well known, and two of today's
windows carry most of it. Scenario D halves the edge for that reason.

## Model

Each simulated window draws one of the 48 outcomes (i.i.d. bootstrap). Lots per rung before each fill
`m = max(1, floor((balance / 3) / $22))`, capped by scenario. P&L scales linearly with `m` (fills assumed to scale
with order size -- see the walls below). 23 windows a day, 90 days, 20,000 paths, starting balance $69.41.

| scenario | what it assumes |
|---|---|
| A unconstrained | lots scale freely with balance |
| B proxy cap 2 | `MAX_CONTRACTS_PER_ORDER=2` stays (today's proxy) -> m <= 2 |
| C absorption 100 | a sweep can fill at most 100 of our lots per window (print-size wall) |
| D half edge | every filled window pays half (today's two ladders were the lucky half) |
| E flat | 1 lot per rung forever (what runs today) |

## Results (balance $; p10 / p50 / p90)

| scenario | day 30 | day 60 | day 90 | lots/rung at d90 (p50) | max drawdown p50 / p90 | P(any day loss > $3.00) within 90 d |
|---|---|---|---|---|---|---|
| A unconstrained | 110 / **121** / 133 | 180 / **217** / 265 | 328 / **429** / 565 | 6 | $4.8 / $7.9 | 37% |
| B proxy cap 2 | 110 / 121 / 132 | 180 / 211 / 244 | 276 / **314** / 354 | 2 | $2.5 / $3.6 | 1.6% |
| C absorption 100 | 110 / 121 / 132 | 180 / 217 / 267 | 331 / **430** / 565 | 6 | $4.9 / $7.9 | 37% |
| D half edge | 89 / 95 / 101 | 113 / 121 / 129 | 141 / **160** / 180 | 2 | $0.9 / $1.4 | 0% |
| E flat 1 lot | 110 / 121 / 132 | 156 / 172 / 188 | 205 / **224** / 243 | 1 | $1.4 / $2.0 | 0% |

Milestones (scenario A): $200 in a median 57 days (every path by day 90); $500 in 24% of paths by day 90
(median day 86 when reached); $1,000 not within 90 days. The 100-lot absorption cap never binds inside 90 days
(C = A), because the median path only reaches 6 lots per rung = 66 lots on a full-ladder sweep.

Reading the shape: the first 30 days are identical in A, B, C and E because the balance stays under $132 and
`m = 1` throughout. Compounding only starts to show in month two. The curve is roughly +2.6% of balance per day
once `m` can track the balance continuously (doubling time ~27 days), but the integer steps and the $22 ladder
granule make it slower at the small end.

## The walls, in the order they bind

1. **The frozen pins are written in contracts and dollars.** One-legged pin: `<= 2 contracts else KILL`. At 2
   lots per rung a single naked rung is 2 contracts (survives), at 3 lots it is 3 (KILL); a naked PAIR of rungs
   at 2 lots is 4 (KILL). S4: `$3.00 day loss` -> campaign kill. The 06:00Z-type adverse-repricing window (8 lots,
   -$0.23 at 1 lot) is -$0.68 at 3 lots and -$1.35 at 6; two of them in a day at 6 lots is a KILL by the current
   text. In scenario A 37% of 90-day paths cross the $3.00 day line at least once. These are not strategy
   failures, they are a $3.00 line being asked to judge a $400 book. Before the first size step the falsifier
   needs an amendment restating S4 as a fraction of the day-start balance (the same ~4.5% that $3.00 is of $66)
   and the one-legged pin per RUNG-EVENT or as a fraction of lots, with Brad's dated words -- exactly the L5 rule
   for `rung_lots`. Without the amendment, sizing up is a faster route to KILL, not to $430.
2. **Proxy `MAX_CONTRACTS_PER_ORDER = 2`** (Brad's `.env`, restart). Scenario B is where we actually are once the
   balance passes $132: 2 lots per rung, ~$315 at day 90. The wing executor already chunks into 2-lot orders;
   the REST order per rung carries `count = lots`, so the cap binds the rests. Raising it is Brad's lever and
   was proposed as 2 -> 11 earlier.
3. **`rung_lots` is the mechanism** (L5, merged): a params change + sha re-pin + Registration line sets the lots
   per rung. "Keep the ratio" in production = a rule that re-derives `rung_lots` from the day-start balance (a
   small build: compute at arming, write the params row, re-pin per day is impractical -> the rule itself must be
   the pinned thing). That is a design question for Brad: step the weights by hand at each $66 of balance, or pin
   a formula.
4. **Print size / absorption.** Today's 14:00Z second sweep filled 9 lots in ONE print at 1 lot per rung; the
   10-02 23:48Z pump traded ~225 lots through our level (20 would have filled at the level we rested). The V3.2
   capacity study put bucket taker flow at ~4k lots per window and sweeps ~1.5k, wing depth +2c median 2.8k lots.
   Scenario C's 100-lot cap does not bind within 90 days. Where it starts to bite -- partial fills of a scaled
   rung on a thin print -- the partial-fill wing machinery already hedges the filled part and leaves the rest
   resting.
5. **Adverse repricing scales with size and the lock does not change.** Scenario D (half edge) is the honest
   lower bound on today's sample: $160 at day 90. The edge per set is the thing n=45 is about to judge; this MC
   does not know it better than the falsifier does.

## What this MC does not model

Regime clustering (two full ladders in six hours tonight; the ideal study found pumps bimodal), queue priority
loss at size (a 2-lot rest at a price sits behind the 1-lot that was there; price priority still first), the
one-legged tail being FIXED (kept at 1-in-48 as a conservative drag), fees changing with price (linear scaling
of today's realized rows carries today's fees), and any change in Kalshi behaviour toward a larger maker.
Every number above is a bootstrap of eight filled windows. Re-run it at n = 45.
