# The Sealed Read — 2026-08-20 (Rung 1.5 quintile policy · verdict: DEAD · one clause)

The formal record of the one-shot sealed read: what was tested, how it was armed, what
the 17 virgin days said, and where the campaign goes next. Ceremony trail:
`sim/ceremony/unseal15_commission.md`, `falsifier.md` SECTION 2 (frozen + disposition),
`unseal15_build_report.md`, `unseal15_review.md`. Aggregates:
`sim/out/unseal15_result.json`. Registration: `historical-data/SEAL.md`.

## The policy that spent the seal

The **expanded quintile-conditional policy**, shaped on 50 train days (1,142 windows),
frozen before the read:

- Q1 (tightest gaps): strangle at EV−5 OR sub-$1 flip — first print wins
- Q2–Q4: sub-$1 flip only (C < $1.00, the arithmetic floor)
- Q5 (widest gaps): flip at EV−10
- Fresh pairs ≤1s · staleness 60s · A15.9 refutation · A15.10 dwell · fees in

Train reference: 599/1142 entered (52.5%), +3.71¢/entry, 62 pins. Fillability
lower-bound checked pre-read: median trigger print 15–62 contracts by side.

## How it was armed (the ceremony holding, end to end)

- Commission written 2026-08-20 on Brad's go ("Lets do it!").
- Falsifier SECTION 2 written and numbers pinned BEFORE any sealed byte; power
  disclosure pinned (≈3% false-kill of a true +3.7¢ edge accepted).
- Runner (`sim/unseal_runner15.py`) built by opus48; adversarially reviewed by a
  second opus48 that REIMPLEMENTED the policy independently from the commission text
  and matched all 18.3M train rows to the cent — including all 30 windows where naive
  file-order picks the wrong Q1 race winner. Policy math triple-implemented, cent-identical.
- Review verdict APPROVE-WITH-FIXES; F1 (census-truth eligible denominator) and F2
  (crash-safe one-shot start-marker) landed and re-verified. 137 tests green.
- Brad froze it verbatim: **"break the seal. freeze it and run."** One execution,
  11:18–11:29 ET, exit 0, marker + result present. Sealed days 2026-08-02..18 minus
  the two burned hours; 371 eligible windows, 181 entries.

## The verdict, by the letter

| clause | frozen threshold | sealed value | result |
|---|---|---|---|
| C4 participation | 25–75% | 48.8% | valid read |
| C1 pooled mean | > 0¢ | +1.15¢ | pass |
| C3 Q1-strangle side | > −3¢ | +9.06¢ | pass |
| C3 Q5-flip side | > −3¢ | −2.30¢ | pass |
| **C2 day-clustered bootstrap LB** | **> −2.0¢** | **−2.76¢** | **FAIL → DEAD** |

Pilot bar (mean ≥ +1.5¢, LB > 0): not approached. Day means ranged −13.1¢ to +20.2¢ —
the clustered-variance monster C2 was built to catch, caught. Pooled +1.15¢ against
SE 2.26¢ is statistically zero. **DEAD is registered.** Revival only via the frozen
clauses: (a) ≥30 fresh post-seal days, (b) live fill telemetry, (c) venue change.

Note for the record: Brad initially recalled the floor as −3¢ (that was C3, per-side).
The frozen C2 floor was −2.0¢, presented twice before the freeze. The verdict stood
without re-litigation — the freeze exists for exactly that moment.

## The autopsy (information, not verdict)

The failure mode was **named before the read**. Brad, 2026-08-20 pre-unseal: "doesn't
that mean our flip EV is calculated too high?" — the conditioning generalization
(census bucket-EV is a static average; dips select for informed pin-death flow, so
conditional pin probability at entry moments sits below the bucket rate). Sealed
confirmation: Q5-flip — the only model-dependent bulk side, 92 of 181 entries —
flipped from train +4.64¢ to **−2.30¢**, with pin rate among entered falling
25.4% → 18.5%. The conditioning bias widened and ate the entire 10¢ discount.

The model-free components **held out-of-sample**:

- **sub-$1 flip: +3.83¢ on 74 entries** (train ~+3.2–3.8¢) — floor-protected,
  arithmetic, as clean an OOS confirmation as V3 has produced.
- **Q1-strangle: +9.06¢ on 15 entries, 0 pins** (train +4.46¢) — directionally held;
  n=15 keeps the error bars wide.

Ex-Q5 arithmetic (published aggregates only; no sealed rows re-read): 89 entries
(24.0% of windows), **+4.72¢/entry**, 2 pins, +1.13¢/window unconditional. Label:
encouraging, uncertified — a post-hoc subset of spent data.

## Where the campaign stands

- The sealed days are SPENT: usable as train data forever, never as validation again.
- Post-seal days (2026-08-19 onward) are virgin, accruing ~23 windows/day.
- **Brad's direction (2026-08-20): go live with the floor-arithmetic policy**
  (sub-$1 everywhere + Q1-strangle, no flip-EV anywhere) **at dual-contract size** —
  which is revival clause (b) by the front door: live fill telemetry, resolving the
  one question no tape can (are the prints joinable?). ~$10/day capital in play.
- Pending Brad: trigger mode (auto-fire within hard caps vs alert-mode manual). The
  sub-$1 component (83% of entries, fleeting late-window prints) is only truly tested
  by auto-with-caps. ALLOW_ORDERS remains Brad's lever alone.
- The pilot gets its own commission + preregistered falsifier before any order flows.

## Scorecard, two years running

Rung 1 killed the strangle at candle fidelity. Rung 1.5 killed both directions at tape
fidelity, then killed the quintile policy on the seal — by one clause, for the exact
reason the captain predicted before the envelope opened. The house is 0-for-3 on
certified edges and 3-for-3 on catching them before they cost real money. The two
components that never trusted a model are the only things still standing, and they go
to live trial next. 🐀⚓
