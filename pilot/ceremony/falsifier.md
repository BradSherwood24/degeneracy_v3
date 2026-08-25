# PILOT FALSIFIER — live auto-fire dual-contract pilot

STATUS: FROZEN

Frozen 2026-08-22 on Brad's explicit go (verbatim, this session: "go ahead and
freeze, write up and handoff and log everything."). From this line down, nothing
in this document may change except appended verdicts in Registration. The service
checks this line at arming (same discipline as the sealed-read loader A3.6/A3.7).
All [pin] values are FROZEN AT THE DRAFT VALUES computed 2026-08-21 — these are
the numbers the built-and-reviewed service mirrors (executor/stops constants);
freezing them unchanged preserves code-text agreement. Immutable now.

DEFERRED ITEMS — ALL RESOLVED AT FREEZE:
- F4 (crash-mid-window) — RULED by Brad 2026-08-21: option (a), ACCEPT. No
  supervisor, no auto-restart, no auto-flatten. A crashed window's positions ride
  to settlement unmanaged (max exposure 1–2 contracts); the crash is NOTED, not
  fought: nonzero exit + startup-failed/aborted ledger row + journaled traceback +
  scheduler log; the next window's reconcile-first sees any residue, journals it,
  and stands down. Revisit at the readiness-for-5 boundary.
- F8 (stop position action) — RULED by Brad 2026-08-22 (verbatim: "F8 is good on
  both."): hold complete floor-protected pairs to settlement; flatten unprotected
  exposure reduce-only. As built and tested.
- V2 retirement bar — RULED by Brad 2026-08-22 (verbatim: "4 cents per 15 is
  good."): 4¢/side mean bin-4 slippage after ≥15 fills. Brad's standing intent,
  recorded alongside: sim accuracy is to be improved regardless of verdict, even
  at deltas as small as 0.5¢ ("We'll want to increase Sim accuracy regardless.
  Even if off by 0.5 cents").

## What is being judged

Not the edge. The SIM'S HONESTY: does observed behavior with real fills match
simulated behavior over the same data? Judged via the five-bin paired protocol
(commission.md) computed by script from journal + fill records.

## Alarms (notify, keep running)

- A1: bin-4 sim-vs-live price delta > 2¢ per fill side — VISIBILITY tier (Brad,
  2026-08-21): each such fill is individually flagged in the daily paired report
  with running count and mean; a structural book-vs-prints offset is half-expected
  here and does NOT retire anything. (Fill-vs-own-IOC-limit is not the measured
  quantity — an IOC cannot fill worse than its limit.)
- A2: ladder-map deviation (expected: $100 all hours except 21:00 UTC = $250,
  $500 Fridays; dual-generation listings validated against the leg actually paired).
  Alarm + strangle stands down; sub-$1 may continue.
- A3: entry-signal rate outside [12%, 55%] of eligible windows, rolling 3 days.
- A4: ≥ 5 book-guard trips (stale/lag/silence gates) in one UTC day → stand down
  for the day.

## Stops (freeze orders; position action per F4/F8 as ruled above)

- S1 (arithmetic, n=1): any FILLED sub-$1 pair with net realized < 0 using ACTUAL
  venue fees (average_fee_paid). One violation halts the pilot: our fee model, fill
  price, or leg pairing is wrong and everything downstream inherits it.
- S2: imbalance the protocol fails to restore to 1:1 or 0:0 within its bounds.
- S3: a non-fill the system believes is a fill, or ANY reconciliation mismatch
  between exchange truth and the local ledger.
- S4: daily realized-loss cap: **$5.00** [pin].
- S5: policy sha mismatch, frozen-falsifier line absent, or proxy /health caps
  absent at arming → refuse to arm (never fires an order to begin with).

## Imbalance protocol bounds (Brad's rulings 2026-08-20)

- I1: retry-buy pair-cost ceiling: sub-$1 = **$1.00 + 3.20¢ = $1.0320** per pair
  [pin: $1.00 + train sub-$1 mean window return]; Q1-strangle = total pair cost
  ≤ bucket-fair (EV ≥ 0 at fill).
- I2: max **5** retries per side per window.
- I3: no rebalance order initiated with < **3 s** [pin] to settlement; past that
  bound, sell-down only; past **1 s** [pin], no orders at all (position rides to
  settlement and is reported).
- I4: sell-down target count always rounds DOWN (equal-at-lower). End state 1:1
  or 0:0 — anything else at settlement is an S2 stop retroactively.

## Promotion gates (attempt-counted; calendar day is a minimum, not a trigger)

- P1 (1 pair → 2 pairs): ≥ **10** fired signals AND fill rate ≥ **60%** AND zero
  unresolved imbalances AND zero S1 violations AND mean |slippage| ≤ **1¢** per
  side AND ≥ 1 calendar day at 1 pair. Brad rules on the report; the gate is the
  floor — he may hold longer, never promote through a failed gate.
- P2 (2 pairs → readiness report for 5): ≥ **10** further fired signals at 2 pairs
  incl. ≥ **5** sub-$1 entries AND P1 conditions still holding AND second-pair
  book-walk measured (price delta of 2nd contract vs 1st on each leg, reported).
  The report closes the pilot; sizing to 5 = new commission.

## Sim-retirement verdicts (the "incredibly inaccurate" bands, pre-committed)

- V1 DEAD-FILLS: after ≥ **15** firings, bin-3 (both fired, live unfilled) > **50%**
  of firings → the tape's honest-fills assumption is refuted at our latency. The
  +3.8¢/+9¢ sim numbers are retired as unreachable; pilot ends.
- V2 RETIRE-SIM: after ≥ **15** fills, mean bin-4 slippage > **4¢ per side** [pin —
  RULED by Brad 2026-08-22; the 2¢ level is the A1 visibility tier, not
  retirement — book-vs-prints offset is expected] sustained (rolling) → sim
  prices are not our prices; sim economics retired pending redesign; pilot ends
  with the measurement as its product.
- V3 VOID-REGIME: entry-signal rate outside [12%, 55%] over the WHOLE pilot →
  the market wasn't the one we shaped on; measurement stands, policy untested.
- V4 CATASTROPHE: pooled realized mean across all filled entries < **−10¢** at any
  point after ≥ 15 fills → stop everything; something structural is wrong beyond
  fill mechanics.
- V5 MATCH: none of V1–V4 by pilot end → the sim's numbers stand as live-confirmed
  at 1–2 contracts; the readiness report for 5 carries the evidence.

## Power disclosure (pinned so nobody re-litigates after)

At pilot n (~15–25 fills), per-entry SD ≈ 29¢ → SE ≈ 6–7¢: economics clauses (V4)
catch catastrophes only and certify nothing. The sharp instruments are S1 (binary
arithmetic, n=1) and the PAIRED bins (market noise cancels in sim-vs-live deltas).
Fill rate at 60% threshold with n=15: a true 80% filler fails ~5% of the time; a
true 40% filler passes ~6% — accepted odds for a two-day gate with a human on the
promotion lever.

## Shakedown gate (pre-live)

≥ 2 runs with ALLOW_ORDERS off, extended until ≥ 1 would-fire logged; backstop
3 calendar days → escalate to Brad. Zero unexplained live/sim signal divergences
(bins 1–2) on shakedown windows; a documented delta may not flip any fire/no-fire
decision.

## Registration

The freeze line, Brad's verbatim go, and every verdict get appended here and
mirrored in `claudes-corner/`. No narrative revivals; a retired sim number returns
only via a new commission with fresh forward data.

---

**FREEZE RECORD — 2026-08-22**

Frozen by Claude on Brad's verbatim go: *"F8 is good on both. 4 cents per 15 is
good. We'll want to increase Sim accuracy regardless. Even if off by 0.5 cents.
go ahead and freeze, write up and handoff and log everything."*

Pins frozen at draft values (computed 2026-08-21, mirrored in service constants,
verified by independent review): S4 $5.00 · I1 $1.0320 · I3 3s/1s · V2 4¢/side.
Shakedown gate satisfied at freeze: 25 full windows, 12 would-fires (all settled
floors ≥ 0), parity 23/23 decision agreement, F15 PASS, zero unexplained bin-1/2
divergences (the single 18:00Z bin-2 is documented as a metadata-vintage artifact
in `historical-data/PARTIAL_DAY_2026-08-21.marker`, re-comparable after refetch).
Policy sha 1b01fd98e1c76748261fbe80f961d9ae8a55853c7807de71af508eece8203656.

Arming (Brad's hands, after this freeze): ALLOW_ORDERS=true + proxy restart;
pilot/ops/mode.txt → armed. First armed window runs S5 against this file.
