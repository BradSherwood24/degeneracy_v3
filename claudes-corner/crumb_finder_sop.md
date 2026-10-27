# The Crumb Finder — an SOP for feeding the colony (2026-10-26)

**Status: THINKING, months out. Nothing here is actionable or authorized.** Brad: "This is just me
thinking multiple months out, nothing actionable yet." No build, no falsifier, no data pull is
implied by this doc. It exists so the idea is written down at a known spot.

Lineage: V2's rat-attack codex (`../degeneracy_v2/claudes-corner/rat_attack/`, esp. 00 overview,
06 graveyard, 07 method, 08 roadmap item 2 "anomaly harvester") + V3's road from census to the
V3.3 ladder pump-fader.

---

## 1. The rat, restated

Brad's V2 frame: we are a capacity-limited solo operator with fund-grade tooling. A strategy that
caps out at a few hundred dollars a day can't pay a fund's team, so funds structurally never
occupy it. We eat the crumbs they leave. A **crumb** is a profit someone leaves on the table; a
**tin** is a participant who *must* trade for non-price reasons and so bakes fresh crumbs daily.

V2 hunted **statistical crumbs** — mispricings inside one market (the lottery bias, the freakout
fade, theta). They are regime-dependent, decay in days to weeks (external half-life ~9 days on
this venue), and the market is calibrated to ~0.99 in the final hour. Every one died: to
calibration, to the adverse-selection wedge (~−4c of news per maker fill), or to a regime flip
(Lottery Tax +1.16c → −5.64c/fill, Aug 10–13).

V3 found a different kind: a **structural crumb**.

## 2. What the V3 crumb is

Two order books are tied by a payoff identity:

    bucket NO [F, F+100)  +  YES strike Sd (> F−0.01)  +  NO strike Su (> F+100)  =  $2, always

Each book is traded by a different crowd, and each crowd prices only its own book. When an
impatient bucket taker sweeps the spot bucket (a pump), our resting bucket-NO rung fills below the
solved lock while the strike books have not yet moved; we take the wings and the set pays $2 at
settlement. We predict nothing.

Why it survives where V2's crumbs did not:

1. **An identity has no regime.** Calibration, volatility, direction — none of it can break
   "the set pays $2". Only *competition* can close the gap.
2. **The fee moat.** A pure-taker arb pays ~4–5c of fees to chase a 2–3c dislocation; only a
   *resting maker on the swept leg* (maker fee $0 on crypto) earns it.
3. **Adverse selection is neutralised.** Being picked off doesn't matter when the hedge exists
   within ~200 ms. The V2 toll booth collapses to one engineering number: how far W moves before
   the wings land. Speed, not prediction.
4. **It is fund-sized wrong.** Capacity = retail impatience on one bucket (~$100–300/day ceiling on
   BTC, `v32-capacity`). Nobody with a payroll can live there.

**The tin:** the impatient bucket taker — wants exposure *now*, never looks at the strike ladder.

**General shape (the thing to hunt):** *an identity across books with mismatched crowds and
mismatched price-discovery speed, protected by a fee asymmetry that only a maker can cross.*

## 3. The SOP — what we actually did, generalised

| # | Step | V3 instance | Kills here look like |
|---|---|---|---|
| 1 | **Kill** — know where not to dig | V2 graveyard: prediction + in-market statistics dead | — |
| 2 | **Map the board** — every co-settling contract + its payoff | range arb scan, board identity (raw sub-$1 hedge = standing state 98% of hours; all-in never persists) | no co-settling family |
| 3 | **Find the identity** — combinations paying a constant | the 3-leg $2 set; payoff matrix [[1,0,1],[0,1,1],[1,1,0]], det −2 | no constant-payout combination |
| 4 | **Find where flow breaks it** — which leg is swept, which is deep/quiet | pumps on the spot bucket; wings deep | flow on the hedge legs, or no flow anywhere |
| 5 | **Persistence vs our speed** | execution physics; wing-reprice study (ETH +2.2c vs BTC +1.4c @200 ms) | hedge reprices faster than our RTT |
| 6 | **Data integrity** | hourly-band look-ahead (strangle-fader VOID, pump-fader clean) | the edge lives in a biased sample |
| 7 | **Honest sim on ms journals** — maker fills decide | V3.2/V3.3 replay; ladder finding | fills don't survive honest queue rules |
| 8 | **Ceremony ladder** — prereg falsifier → dry → test fires | TF#1..#4 | any frozen KILL |
| 9 | **Scale to the flow wall, then re-census** | 1 → 3 lots; ETH 1 lot | — |

Steps 2–5 are the **crumb finder**. Steps 6–9 are the ceremony we already own. The finder's job is
to make steps 2–5 cheap, repeatable and blind.

## 4. Where the next crumbs might be (unmeasured — hypotheses only)

Honest prior: **this exact family is nearly mapped.** The 09-22 multi-series scan found only 7
underlyings with co-settling hourly range+strike pairs, all crypto; ETH is a marginal #2 (~11% of
BTC's spot-bucket flow), XRP/HYPE/DOGE/SOL/BNB dead. ETH is probably the last site of *this exact
shape*. A colony needs new identity *kinds*, ranked by reuse of what exists:

1. **Same identity, other tenors on Kalshi crypto** — daily / weekly / monthly range vs strike, if
   such pairs are listed (not checked). Reuses everything; `V33SeriesSpec` (PR #154) is the hook.
2. **Partition identities within one series** — an event's buckets sum to $1. The 09-01 range arb
   scan killed this as taker but found positive *maker-side* cells, never followed up as a maker.
3. **Non-crypto events with both a range ladder and a threshold ladder** — economic releases,
   weather highs, index closes. Slower, fewer windows, different (possibly more naive) crowds. The
   scan ruled out *hourly* pairs only, not daily/event ones.
4. **Cross-venue identity** — e.g. a Polymarket market mirroring a Kalshi event. Same structure,
   but capital on two venues + access/ops friction. Parked.

## 5. The Crumb Finder v1 (sketch, not a spec)

Read-only, through the proxy, touches no money.

- **Stage 1 — identity miner.** Pull `/series`, `/events`, `/markets` for the whole exchange. Group
  markets by (settlement time, settlement source). Parse each market's payoff into outcome
  intervals, build the payoff matrix, and solve for small-integer combinations that pay a constant
  (a small LP / integer null-space search — Brad's det −2 matrix, generalised). Output: every box
  on the exchange. Metadata only; nearly free.
- **Stage 2 — board scanner.** Per box, sample books + the trades tape: all-taker box cost net of
  exact fees (`0.07·p·(1−p)` rounded up per fill); the maker-on-leg-i lock price for each leg;
  taker volume per leg (who sweeps); hedge-leg depth. **Score = expected maker fills/day × lock
  margin.**
- **Stage 3 — persistence.** For the top cells, a few days of WS recording and the wing-reprice
  study run generically: hedge-leg repricing after flow on the maker leg, at our RTT.
- **Then the ceremony** (§3 steps 6–9) for anything that survives.

**The tool's own falsifier** (borrowed from V2 roadmap item 2): run blind, it must rediscover the
BTC pump-fader as #1 and rank ETH a marginal #2. If it can't find the crumb we already know about,
its other rankings mean nothing.

## 6. Architecture implications (for whenever this matters)

- **N-leg box engine.** V3.3's core handles one box shape (3 legs, 1:1). The general step is a
  core parameterised by a payoff matrix — V4-sized. Brad's matrix-inversion idea (implied outcome
  probabilities off the box: total = sum/2, q_i = total − leg_i) becomes its pricing and
  calibration column, and is what makes non-1:1 hedges tractable.
- **Per-strategy attribution** stays a hard requirement before strategies share the account (V2
  00 overview): coid prefix per roster (`v33-`, `v33e-` already) + a local strategy column.
- **Account-wide risk.** Shared balance → shared S4 (the #154 ruling) generalises to a colony
  risk budget; the reserve rule (25% as of 10-26) is sized on the SUM of concurrent full sweeps.

## 7. Decay tripwires for structural crumbs

A structural crumb's threat is competition, not regime. Watch, per roster:
- **Queue ahead of our rungs** at placement (another maker resting at our levels);
- **Fill rate at constant flow** (pumps still happen, we stop getting them);
- **Wing repricing speed** after sweeps (someone else is hedging the same identity).

The first two are already recoverable from our journals.

## 8. Sequencing (if and when Brad says go)

V3.4 speed work first — speed is the moat on the crumb we have. Stage 1 of the finder is
metadata-only and could run in parallel as a side quest. Everything else waits for its own word.
