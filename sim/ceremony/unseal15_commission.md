# COMMISSION — Rung 1.5 sealed read (the quintile-conditional tape policy)

Commissioned 2026-08-20 on Brad's go ("Lets do it!"). This document is the binding
specification for the ONE sealed read of the trades tape. The sealed days are spent
once, on this policy family, and never again.

## What is being tested

The **frozen expanded policy**, shaped entirely on train data (2026-06-13..08-01,
1,142 windows) and frozen BEFORE any sealed byte is read:

- **Eligibility**: census-eligible corridor windows (same eligibility law as the train
  tape runs). Freshness law: entry only at events where BOTH leg ages ≤ 1.0 s.
  Staleness horizon 60 s; cross-side refutation (A15.9) and dwell law (A15.10) active —
  i.e., the reviewed `sim/tape_sim.py` EXACTLY as it ran on train. No code changes.
- **Quintiles**: assigned from g/σ̂ using the FROZEN census quintile edges (the
  sha-pinned `census_train.csv`, sha `580d143f...`). NOT recomputed on sealed data.
- **Entry rules (first qualifying event wins, one entry per window):**
  - Q1: strangle at EV−5 (ev ≥ 0.05) OR flip at C < $1.00 — whichever prints first.
  - Q2, Q3, Q4: flip at C < $1.00 only.
  - Q5: flip at EV−10 (ev ≥ 0.10). (Train fact: sub-$1 never preempts in Q5.)
- **Payoff**: settlement-realized, fees in — the tape sim's payoff column, unchanged.

Train reference numbers (the runner's dry-run MUST reproduce these exactly from
`sim/out/full60/tape_points.csv` before it is permitted to touch sealed data):
599/1142 entered (52.45%), pooled mean **+3.71¢/entry**, 62 pins;
per-source: Q1-strangle 88, Q5-flip 201, sub-$1 310 (of which Q1 126, Q2 90, Q3 56,
Q4 38, Q5 0).

## The sealed universe

UTC days 2026-08-02..2026-08-18 (17 days), MINUS the two burned hours
(close_times `2026-08-18T19:00:00Z` and `2026-08-18T20:00:00Z`, contaminated in the
Rung 1 campaign — excluded from all clause quantities and reported only as an
exclusion count). Expected ~390 eligible windows, ~200 entries at train rates.

## Procedure (fail-closed, in order)

1. `sim/unseal_runner15.py` is the ONLY sanctioned caller of
   `acknowledge_sealed_read=True` for this read (the Rung 1 `unseal_runner.py`
   remains never-run; loader A3.7 additionally refuses until `falsifier.md` is FROZEN).
2. Preflight (any failure refuses before any sealed byte): falsifier FROZEN;
   `census_train.csv` sha matches the pinned value; sealed trades/markets/candles
   day-files all present; train dry-run certificate present (see 3).
3. **Train dry-run gate**: the runner's policy evaluator must first reproduce the
   train reference numbers above to the cent from the existing full60 output
   (no sealed access involved). It writes `sim/out/unseal15_dryrun.json`; the sealed
   path refuses to run unless that file exists and matches.
4. The sealed read: tape_sim.run() per single sealed day (memory law: one day at a
   time, the chunking pattern), outputs quarantined under `sim/out/sealed_eval/`
   (deny-ruled before the run). The policy evaluator consumes those rows in-process.
5. Output: AGGREGATES ONLY (`sim/out/unseal15_result.json`) — clause quantities,
   per-side counts/means, per-day means for the bootstrap; NO per-window sealed rows,
   timestamps, or prices leave the quarantine directory.
6. One execution. No re-runs, no per-quintile retunes, no alternate gates, no second
   policy. Whatever the clauses say, stands. The read is registered in SEAL.md with
   timestamp and scope.

## Build & review

Runner built by opus48 (Brad's standing mandate), adversarially reviewed by a second
opus48 before the dry-run certificate is accepted. The falsifier lives in
`sim/ceremony/falsifier.md` (Rung 1 section preserved as history; Rung 1.5 clauses
appended) and freezes only on Brad's explicit go phrase after he reads the clauses.

## After the read

- The sealed days are SPENT for this policy family. Any future strategy evaluated on
  2026-08-02..18 is contaminated-by-construction and must say so.
- Post-seal forward data (2026-08-19 onward) remains virgin and accumulates ~23
  windows/day for any future test.
