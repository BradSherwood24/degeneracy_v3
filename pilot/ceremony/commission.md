# PILOT COMMISSION — live auto-fire dual-contract pilot (floor-arithmetic policy)

Commissioned 2026-08-21 on Brad's explicit go: "Lets start it! Go for full build."
Binding companion: `pilot/ceremony/falsifier.md` (DRAFT until Brad's freeze; must be
FROZEN before Phase 5 places any order). Build plan: `pilot/PLAN.md` (rev 2, post
opus48 review `pilot/PLAN_review_opus48.md`). This commission fixes WHAT the pilot is
and HOW it runs; the falsifier fixes how it is judged.

## Purpose

The pilot is a MEASUREMENT, not a strategy validation. The question:

> How close does observed behavior with real fills match simulated behavior over the
> same data?

It is revival clause (b) of the Rung 1.5 sealed-read verdict (live fill telemetry) by
the front door. It cannot certify an edge; it can only resolve whether the tape's
honest-fills assumption survives contact with a real order book at our latency.

## The policy (frozen roster — the sealed-read survivors, no flip-EV anywhere)

- **sub-$1 flip**, all quintiles: first moment C < $1.00 net of fees, both legs.
- **Q1-strangle EV−5**: G/σ̂ in the bottom train quintile, fresh pair ≤ 1 s, ev ≥ 5¢.
- Parameters identical to the sim (staleness, freshness, A15.9, A15.10, audited fee
  formula, census EV curve). Policy config = frozen JSON + sha; the service refuses
  to run on sha mismatch. Signals compute C from LIVE ASKS (the book we can fill
  against); the book-vs-prints delta vs the historical sim is a named, reported
  structural difference.

## Execution mechanics (Brad's locked decisions)

- IOC limit orders at max price (the venue has no market type; IOC only fills taker).
  IOC at ALL sizes — order type is never a variable when size changes.
- Both legs submitted in ONE batch-create request (minimizes inter-leg latency; NOT
  atomic — per-entry success/error).
- Auto-fire within hard caps. ALLOW_ORDERS is Brad's lever alone; the proxy enforces
  independent caps (max contracts/order, ticker whitelist, persisted daily budget).
- Hedge ratio 1:1 always. Imbalance protocol (partial/orphan): retry-buy the deficient
  leg bounded by the pair-cost ceiling (falsifier-pinned: $1.00 + average window
  return for sub-$1; total cost ≤ bucket-fair for strangle), max 5 retries per side,
  each bounded by seconds-to-settlement; then sell-down to match, target rounds DOWN.
  End state 1:1 or 0:0. Reconciler decides rebalances; Executor dispatches (single
  order-authority mutex).
- Local hosting on Brad's always-on machine; Kalshi-app notifications; wake early
  each hour (UTC schedule); local clock trusted, server-ts deltas journaled.
- Journal buffered in memory, flushed at window close (accepted cost: a crash loses
  that window's replay record, never a position — exchange truth is authoritative).

## The measurement protocol (the five bins)

Every live day is paired against the frozen sim over the same data. Every fired or
would-fire signal lands in exactly one bin:

1. sim fired / live never saw it (feed gap, latency)
2. live fired / sim didn't (signal-engine divergence)
3. both fired / live didn't fill (fillability — the pilot's reason to exist)
4. both filled / different price (slippage, measured per side)
5. both filled, same price, payoff matches (the sim tells the truth)

Plus the imbalance bin: every partial/orphan event and its resolution path.
Computed by a defined script from the journal + fill records, never by eyeball.
Known confound (accepted): our own prints re-enter the fetched tape at noise scale
(1–2 contracts vs ~20k prints/entered window); contingency = size+price+time
exclusion. Actual venue fees (average_fee_paid) are used everywhere, not the model.

## The ladder

0. **Shakedown**: ALLOW_ORDERS off. Minimum 2 runs, extended until ≥ 1 would-fire is
   logged; backstop 3 calendar days then escalate to Brad.
1. **1 pair**: goal = ready to size to 2. Paired report that evening.
2. **2 pairs**: goal = ready to size to 5. Adds second-pair book-walk measurement.
3. **Readiness report for 5** goes to Brad with accumulated five-bin evidence.

Promotion between rungs is by the falsifier's pinned gates. The pilot ends at the
2-pair stage's completion; sizing to 5 requires a new commission.

## Roles

- opus48 builds; a SEPARATE opus48 adversarially reviews each phase (Phase 3's review
  includes independent reimplementation of decide()).
- The bridge (Claude) orchestrates, writes ceremony, computes reports; never places
  an order and never flips ALLOW_ORDERS.
- Brad: freezes the falsifier, sets ALLOW_ORDERS, restarts the proxy for Phase 0
  deploy, rules on promotions, owns every live trigger.

## Standing disclosures

- Sealed days 2026-08-02..18 are spent-as-train; all pilot comparisons run on live
  forward days (which also accrue toward revival clause (a)).
- Fillability at 1–2 contracts says nothing about 5+ without the book-walk data.
- DEFERRED before falsifier freeze: stop semantics (review F4 crash-mid-window
  supervisor; F8 hold-complete-floor-pairs vs flatten). Brad returns to these.
- ~$10/day capital at dual size; account balance is the hard physical cap.
