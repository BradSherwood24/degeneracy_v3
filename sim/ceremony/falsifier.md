# FALSIFIER — the one-shot sealed read

STATUS: FROZEN

FROZEN 2026-08-20 on Brad's explicit go, verbatim: "break the seal. freeze it and run."
Build approved for arming by adversarial review (unseal15_review.md, fix-verification
appended). From this line down, nothing changes until the read is executed and registered.

> **POST-READ DISPOSITION (2026-08-20, bridge): VERDICT = DEAD.** The read executed
> 11:18–11:29 ET (exit 0, one shot, marker + result present). Clause application, by
> the letter: C4 entry rate 48.79% ∈ [25%,75%] → verdict VALID. C1 pooled mean
> +1.15¢ > 0 → pass. C3 Q1-strangle +9.06¢ and Q5-flip −2.30¢, both > −3¢ → pass.
> **C2 day-clustered bootstrap lower bound −2.76¢ ≤ −2.0¢ → FAIL → DEAD.**
> Registered kill of the Rung 1.5 expanded quintile-conditional policy. Revival only
> via the frozen clauses: (a) ≥30 fresh post-seal days, (b) live fill telemetry
> contradicting honest-fills C, (c) venue structure change. No narrative revivals.
> Post-mortem observations (information, not verdict): Q5-flip — the model-dependent
> bulk side, 92 of 181 entries — flipped from train +4.64¢ to sealed −2.30¢ with pin
> rate among entered 18.5% vs train 25.4% (the conditioning bias widened, per Brad's
> pre-read generalization); the floor-arithmetic components held (sub-$1 +3.83¢ on 74,
> Q1-strangle +9.06¢ on 15); day-mean dispersion −13.1..+20.2¢ drove the C2 breach.

This file governs the sealed read (loader A3.7 keys on this file). It now carries TWO
sections: the Rung 1 candle-gate falsifier (historical — its read was never armed, see
its disposition note) and the ACTIVE Rung 1.5 tape-policy falsifier below it. Freezing
this file (replacing the STATUS line above with exactly `STATUS: FROZEN`, on Brad's
explicit go) arms ONLY the Rung 1.5 read defined in `unseal15_commission.md`; the
Rung 1 gate remains empty and unarmed.

---

# SECTION 1 (HISTORICAL) — Rung 1 candle-gate falsifier, never armed

Clause structure written 2026-08-19 BEFORE the train census ran (so the shape of failure is
chosen blind). The bracketed numbers get pinned immediately after the train fit — before any
sealed access — and the whole document freezes on Brad's explicit morning go. TO FREEZE:
replace the STATUS line above with a line reading exactly `STATUS: FROZEN`. Freeze state is
keyed on that dedicated line (A3.6), not a whole-file substring, so this descriptive prose
about freezing changes nothing. Both `sim/unseal_runner.py` (preflight) and `sim/loader.py`
(A3.7 defense-in-depth) refuse the sealed read until such a line is present.

> **POST-TRAIN DISPOSITION (2026-08-19, bridge):** the frozen train run produced an EMPTY
> gate (no G/σ̂ bucket clears; see `sim/out/gate_report.md`). Per the commission there is
> nothing to apply to the sealed days — **no sealed read is armed under this specification.**
> This document stays DRAFT, the numbers stay unpinned, and the 17 sealed days stay virgin
> for whatever future preregistered read earns them. Any revised gate (e.g. cost-capped,
> different entry mechanics, live-fill-informed C) is a NEW commission with its own
> falsifier — it may spend the sealed read only once, on Brad's go.

## The read

Apply `sim/out/gate.json` (frozen from train) to the sealed 17 days (2026-08-02..18).
One execution. Aggregates only. No retunes, no second read, no alternate bucketings,
no snapshot shopping. Descriptive columns are reported but decide nothing.

## Clauses — the strategy is DEAD unless ALL of these hold on gated sealed hours

- **C1 (economics, primary):** pooled EV/pair in the BASE column > 0, AND its day-clustered
  95% bootstrap lower bound (seed 26, 10,000 draws) > **[−X¢ — pinned after train fit]**.
- **C2 (pin rate vs breakeven):** gated pin rate < (1 − C̄_sealed) − **[margin, pinned]**,
  where C̄_sealed is the mean BASE cost actually observed on gated sealed hours.
- **C3 (day consistency):** EV negative on no more than **[K of 17]** sealed days
  (T3 precedent: a regime inversion shows up as most-days-negative, not as noise).
- **C4 (participation):** the gate plays ≥ **[N]** sealed hours. Below that, the read is
  VOID-THIN (neither survive nor dead) — insufficient support, not evidence.
- **C5 (fidelity honesty):** if C1 passes in BASE but EV ≤ 0 in WORST, the verdict is
  capped at **SURVIVE-FIDELITY-LIMITED**: no live sizing may cite this read; the next step
  is live 1-lot probes to resolve C, not more offline work.

## Pre-committed verdict handling

- **SURVIVE:** proposal for a bounded 1-lot live ladder (with abort rules for leg risk)
  goes to Brad. No order is placed by this campaign.
- **DEAD:** registered kill. Revival only via named clauses: (a) materially longer tape
  (Predexon or accrued recorder days), (b) live fill telemetry showing candle C was
  pessimistic, or (c) a structural change in the venue's ladder/fees. No narrative revivals.
- **VOID-THIN:** the gate was too restrictive to read; the campaign returns to Brad with
  options, none of which include loosening the gate post-hoc.

## Standing disclosures carried into the read

The three known sealed hours (SEAL.md disclosures) cannot steer an aggregate read of ~400
hours but are re-stated in the results document. The n=2 live probes are anecdotes, not
evidence, and appear nowhere in the falsifier arithmetic.

---

# SECTION 2 (ACTIVE) — Rung 1.5 tape-policy falsifier

Written 2026-08-20 BEFORE any sealed byte is read. Policy and procedure are fixed in
`unseal15_commission.md`; this section fixes the verdict arithmetic. All numbers below
are PINNED now, from train statistics alone (train: 599 entries, +3.71¢/entry pooled,
SD ≈ 28.9¢; expected sealed sample ≈ 200 entries → SE ≈ 2.0¢).

## The read

Apply the frozen expanded policy (Q1: strangle EV−5 / sub-$1 flip first-come;
Q2–Q4: sub-$1 flip; Q5: flip EV−10; fresh ≤ 1 s; 60 s staleness; A15.9 + A15.10) to
the sealed days 2026-08-02..18 minus the two burned close_times. One execution,
aggregates only, via `sim/unseal_runner15.py` after its train dry-run certificate.

## Clauses — the policy is DEAD unless ALL of these hold on sealed entries

- **C1 (economics, primary):** pooled realized mean per entry > **0¢**.
- **C2 (clustering guard):** day-clustered 95% bootstrap (seed 26, 10,000 draws) lower
  bound of the pooled per-entry mean > **−2.0¢**.
- **C3 (risk-bearing sides not catastrophic):** the two sides that can lose —
  Q1-strangle entries and Q5-flip entries — EACH have realized mean > **−3.0¢**.
  (The sub-$1 component is floor-protected and cannot drive a loss; it is reported
  but cannot rescue C3.)
- **C4 (participation / regime check):** entry rate on eligible sealed windows within
  **[25%, 75%]** (train: 52.5%). Outside that band the read is **VOID-REGIME**
  (neither survive nor dead): the sealed fortnight is a different market than train
  and the policy was not meaningfully tested. The seal is still spent.

## Graduation bar (pre-committed, above mere survival)

- **PASS-TO-PILOT** requires: pooled mean ≥ **+1.5¢/entry** AND the C2 bootstrap lower
  bound > **0¢** AND C3 holds. Verdict: proposal for a bounded 1-lot live pilot goes
  to Brad (no order placed by this campaign).
- **SURVIVE (below pilot bar):** clauses hold but mean < +1.5¢ or lower bound ≤ 0 —
  the policy lives as a paper-trade candidate on post-seal forward data only. No live
  sizing may cite this read.
- **DEAD:** any of C1–C3 fails. Registered kill. Revival only via: (a) materially new
  data regime (≥ 30 fresh post-seal days), (b) live fill telemetry contradicting the
  tape's honest-fills C, or (c) venue structure change. No narrative revivals.

## Power disclosure (pinned so nobody re-litigates it after the read)

At train-true mean +3.71¢ and SE ≈ 2.0¢, the probability this falsifier wrongly kills
a genuinely +3.7¢ policy is ≈ 3% (C1) plus small contributions from C2/C3; the
probability a genuinely-zero policy wrongly survives C1 is ≈ 50% BUT such a survivor
is capped at paper-trade unless it also clears the +1.5¢/lower-bound>0 pilot bar
(≈ 4% false-pilot rate for a zero-edge policy). These are accepted odds; the sealed
read is a filter, not a proof, and the pilot bar is where money decisions gate.

## Standing disclosures carried into the read

SEAL.md disclosures №1–4 (metadata-level) re-stated in the result document. The two
burned hours are excluded from all clause quantities and reported as a count. The
fillability caveat stands regardless of verdict: all tape prices assume joinable
prints; only live probes resolve that.
