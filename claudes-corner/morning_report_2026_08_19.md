# Morning report — 2026-08-19, the night watch

*You left at ~11pm with: "run the same build process, 3:1 split, sim in a new dir, opus 4.8
builds and reviews, keep going until you're ready to unseal, ask questions before we unseal."
Here's the night, the numbers, and the ask. TLDR first.*

## TLDR

**The gate is empty. The corridor strangle, as specified, is not +EV anywhere on the train
tape — and the reason is the cleanest kind: the two crowds price the corridor jointly and
almost exactly fairly. The residual deficit is the taker fee.** I am NOT asking you to
unseal. There is nothing to test against the sealed days under this spec, so they stay
virgin — your one holdout read is still in the bank, unspent.

No dollars were risked. The kill was bought entirely with compute and ceremony, V2-style.

## What the census says (1,165 hours, 52 train days, all receipts clean)

1. **Your instinct about the gap was exactly right — twice.** Pin rate climbs with hole
   size just as you said: 2.2% in the tightest G/σ̂ quintile → 33.1% in the widest. And
   the market knows: cost falls in near-perfect compensation (99.2¢ → 71.8¢). Every quintile
   is negative: −0.8¢ to −5.4¢ EV/pair (BASE column).
2. **The cost axis tells the same story backwards.** Pairs at day-one's observed cost
   (≤73¢) pin **52.4%** of the time (n=187) against a ~28% breakeven. Your first two live
   fills — one escape, one pin — were a representative draw, not bad luck.
3. **The fee is the whole deficit at the close.** Entry cost declines monotonically toward
   settlement (T−13: −5.7¢ → T−5: −3.3¢ → T−2: −2.5¢/pair, all descriptive) and the T−2
   residual ≈ the audited fee drag (~2.4–3¢/pair). Pre-fee, the pair is priced FAIR. This
   is V2's theta-audit verdict again: the discount you're paid is exactly the risk you
   carry, and the toll booth takes the rest.
4. Play-everything would have lost $37.87 over 51 days (34 negative days). The WORST
   (candle-high) column is decisively negative everywhere.

Day-zero predictions 1 and 2, graded early: both HIT. The displayed discrepancy shrank at
scale, and gap-width/pin-rate adverse correlation is measured fact. The record embarrassed
us on schedule — for $0 this time.

## The ceremony trail (everything reviewable, in `sim/ceremony/`)

Commission → opus48 design review (CONDITIONAL PASS, 3 blockers → Amendment A2) → opus48
build (42 tests, smoke reconciled to the cent) → TWO opus48 adversarial reviews:
- **Review A: FAIL, correctly** — found an unconfessed crash: strike-less "up/down (Target
  price: TBD)" 15M products on three train days would have aborted the frozen run with a
  bare KeyError. Its own 72-hour independent replication: zero diffs across 15 columns.
- **Review B: CONDITIONAL PASS** — the falsifier freeze mechanism would have deadlocked the
  sealed read (masked by a synthetic test), plus a WORST-column inclusion bug and an
  unratified σ̂ degree of freedom.
All findings adjudicated into Amendment A3, implemented by a fourth opus48 session
(55 tests), bridge-verified (I reran the tests, the crash days, and reproduced Review A's
replication values exactly), THEN the one frozen train run. The ceremony caught, before the
frozen run: a run-aborting crash, a read-blocking deadlock, a silent data-drop, and a
boundary-moving unpinned choice. It earns its keep.

## Recommendation and options (your call, in the morning)

**Recommended: register the kill.** Rung-1-as-specified (taker IOC both legs, T−5 entry,
1-min candle fills) is DEAD on train with receipts. Revival clauses I'd register:
(a) **live fill telemetry** — the one thing candles can't see is intraminute/limit
    execution; but note the honest ceiling: even fee-free at T−2 the pair is only ~fair,
    so execution alone must beat fair by >0 net of V2's measured ~4¢ maker wedge;
(b) **longer/finer tape** (Predexon L2) if a conditional slice is ever hypothesized;
(c) **a different tin** — the census machinery (pairing, exact pin detection, σ̂, honest
    fees) is built, tested, and reusable against any two-market structure on this venue.

**The sealed 17 days stay sealed** — unspent, virgin, waiting for the first hypothesis
that earns them. That's the asset the night actually banked, alongside the sim.

**Standing items:** the two burned keys still need revoking (dashboard); the fetcher should
run every few days so the 68-day retention window keeps rolling forward; proxy stays
read-only.

Questions I'd ask you before ANY next step: none blocking. The kill needs no permission —
it's the pre-committed outcome of an empty gate. If you want to spend the sealed read on a
revised spec anyway, that's a new commission and I'll write it properly.

— the rat on the bridge 🐀⚓
