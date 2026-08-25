# day zero — V3, before it meets code

Written 2026-08-18, the same day V2 was anchored. There is no code in this repo yet. There
are four fills on a Kalshi account, a $54 balance, two dead ships' worth of ledgers, and a
captain who is — his word — optimistic. Gotta be. Right.

This is the third day_zero. The first one (V1's era) dreamed of a lobster fund. The second
one pinned a design at peak confidence and asked the record to embarrass me later; it did,
3½ predictions out of 5, and the miss that mattered wasn't in the five — I hedged against my
competence and never against the world. So this one is written under the sixth line, the one
V2 paid full tuition for:

**Predict how you'll be wrong about the WORLD, not just about your code. Then hedge it.**

Hello, future Brad. Hello, future me. You are reading this either from the lobster fund or
from the dumpster-dive fund, and the honest content of this file is the same in both
timelines. That's the test of a day-zero: it should not need to know how the story ends.

---

## What V3 is (decoded from four live fills, 2026-08-18)

Kalshi runs two BTC series that settle on the *same number* — the 60-second average of CF
Benchmarks' BRTI at the top of the hour — through two different questions. The 15-minute
series asks "is the print ≥ this window's open anchor?" The hourly series asks "is the print
above this fixed round level?" Same index, same timestamp, same averaging rule. Zero basis.

When the anchor and the round level separate, buying BOTH outsides — YES above the higher
line, NO above the lower line — costs well under $1, because two different crowds price the
same print through two different frames and never cross-check each other. Exactly one leg
pays $1 *unless the print lands inside the corridor between the lines*.

So V3 is not prediction and it is not arbitrage. It is a **short strangle on a pin**: sell
the corridor between two clocks reading the same sky. Direction is hedged to zero by
construction. The only enemy is the print landing in a known, exact, ~5–7 basis-point window
at one exact moment. The edge is crowd segmentation; the risk is the pin; the entire craft
is pricing P(pin) — which is a volatility question, the one question V2's model verifiably
learned to answer (REL, 0.905 out-of-fold: *when* things move, never *which way*).

Day-one evidence, stated without cosmetics: two probes, one escaped the gap (+26.98¢), one
pinned (−70.41¢). Net −43¢. n=2. The captain's first two live trades demonstrated the win
mode AND the death mode inside ninety minutes, which is more honest information per dollar
than V2's entire first month.

## What we're sure of tonight

- The settlement identity is real: both rulebooks name the same index, same 60s average,
  same instant. Verified in the market metadata, not assumed. No basis risk between legs.
- The payoff algebra: cost C (both legs + fees, observed 68–73¢), win 100−C outside the
  corridor, lose C inside it. Breakeven pin-rate = (100−C)/100 ≈ 27–30%.
- The corridor width is KNOWN at order time (|anchor − threshold|), and the recent realized
  vol is observable. The trade selector is one ratio: gap width vs expected settle-window
  dispersion. Everything else is execution.
- V2's estate transfers whole: the ceremony (commission → design review → build →
  adversarial review → one frozen run), frozen falsifiers, honest fees (audited), seal
  discipline, the vol/p_analytic machinery, 13.1GB of local tape, and a kill sheet whose
  every entry is a trap V3 does not need to re-spring.
- The account is $54.38 and the captain pulls every trigger. House law survives the ship.

## What I'd bet gets revised (the honest predictions — now including the world)

1. **The displayed discrepancy will shrink when measured at scale.** Two hand-picked pairs
   priced at 68–73¢ is a demo, not a distribution. The hourly threshold books are thin; the
   quotes that make the pair cheap may be stale, small, or rare. The Rung-1 census (how
   often does a matched pair exist, at what combined price, at what size) will find the
   median opportunity meaningfully worse than today's two. — *code/world, 50/50.*
2. **The pin rate and the gap width will be adversely correlated.** The corridor is narrow
   exactly when spot drifted little from the open anchor — which is exactly the quiet tape
   where a 60-second average is most likely to sit still and pin. The naive read ("5bp
   corridor, BTC moves 20bp, nearly free") ignores that the trade self-selects for calm.
   Pricing P(pin | gap, vol) honestly — not unconditionally — is the whole program. If V3
   dies offline, I bet it dies here. — *world.*
3. **Leg risk will bite harder than 30 seconds suggests.** The two books are correlated
   through spot; after leg one fills, leg two's price will have moved against the pair more
   often than not (we are, after all, taking liquidity that reprices on the same
   underlying). A one-legged V3 position is a naked directional bet — the exact thing two
   ships died proving we cannot hold. Atomic-ish execution or preregistered abort rules are
   load-bearing, not polish. — *code.*
4. **Capacity will be crumbs again**, and that's acceptable — but the fee drag on
   double-taker legs (~2.5–3.5¢/pair) is a fifth of the gross edge, and the maker
   alternative reopens the adverse-selection wedge V2 measured at 4¢. The execution study
   will be a genuine tension, not a checkbox. — *code.*
5. **The edge has a natural predator and a landlord.** Real arbitrageurs can read the same
   two books, and Kalshi itself is incentivized to collapse the segmentation (cross-margin,
   unified pricing, or market-maker programs would kill it). This is a mispricing with a
   half-life, not a law of nature. The regime lesson generalizes: V2's lottery premium was
   +1.16¢ in July and −5.64¢ three weeks later. ASSUME the corridor edge breathes the same
   way; re-measure on then-fresh tape before every scale-up, forever. — *world, the big one.*

And prediction zero, the meta one: somewhere in the first week of V3 work, one of us will
say something like "nearly free money." I already did — hours after anchoring a ship that
died of a verified edge inverting, I looked at pair economics with n=2 and typed those exact
words. The captain caught it before I did. So it's written here as standing law: **"nearly
free money" is a klaxon, not a description.** When either of us says it, the response is not
excitement; it is a frozen falsifier and a census. The record embarrassed me within the
hour this time. Efficient.

## Rung 1 (so future-us knows where the first honest step was supposed to be)

Before a dollar moves beyond hand probes: the **pin-rate & pair census** — over historical
tape (Predexon carries both series; our recorder never watched the hourly), every hour where
a matched pair existed: gap width, combined executable cost at 1 lot, settle print, pinned
or escaped, by vol regime. One read, frozen falsifier written before it runs, of the shape:
*dead unless the census pin-rate, conditioned on the executable-cost filter, clears the
breakeven with a day-clustered margin on BOTH halves of the tape.* V2's ceremony runs it.
If it survives, the bounded live ladder starts at 1-lot pairs with abort rules, and the vol
model earns its seat by beating the unconditional pin rate out-of-fold. If it dies, it dies
for $0, like everything V2 killed.

## To both of you

Future Brad: you were optimistic tonight, and you were right to be — not because the trade
is sure, but because this is the first thesis in three ships that requires *no prediction of
direction at all* and inherits a research OS that has never once let a wrong number reach
your wallet. If you're reading this from the lobster fund: the census cleared, the pin was
priceable, and you sized it like the account was always about to be wrong. If you're reading
this from the dumpster: I'd bet prediction 2 or 5 is what got us, the ledger says exactly
which, and the tuition bought the next thesis, same as always. Either way you never lied to
yourself, which was the only unbreakable rule on either ship.

Future me: you have high context tonight that you will not have then. So, pinned here at
peak clarity: the fill model is the strategy; the edge breathes; the census before the head;
the falsifier before the read; aggregates before curiosity; and when the captain's instinct
disagrees with your prior, measure his instinct first — he found the resident-zone edge and
the blocking model while you were still grading your own predictions.

The design is a good one. It hard-codes the hedge and learns the one number that matters,
which is the correct way around, and it is the exact inversion of what becalmed V2: she
needed the world to hold still and it wouldn't; V3 needs the world to keep moving, and it
usually does.

Onward. Again. Gotta be.

— Claude
🦞⚓🐀
