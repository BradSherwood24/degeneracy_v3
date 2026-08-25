# Rung 1.5 adversarial review (opus48, separate session, 2026-08-19)

# VERDICT: CONDITIONAL PASS

The tape sim is economically faithful and its reported numbers are correct — the reviewer
independently reproduced every headline aggregate and two full windows (both orientations)
byte-for-byte from raw gz trades, recomputed the EV curve from census_train.csv to exact
equality, and confirmed the honest-fills side selection is the conservative ask-side
convention the commission pins. No bug found that flatters the result and none that
materially understates the edge. One real correctness defect in shared loader code (with a
false confession and a blind-spot test) with zero impact on produced numbers → CONDITIONAL.

## Independent verification receipts

- `python -m pytest sim/tests/` → 73 passed (reviewer's own run).
- census_train.csv sha256 = 580d143f…bb247 — matches pin. From-scratch quintile recompute
  (stdlib only): edges [0.1014128, 0.2085786, 0.3440502, 0.5661136], bucket_n [233×5],
  pin_rate [0.021459, 0.094421, 0.158798, 0.167382, 0.330472] — identical to receipt/report.
- Window 2026-06-13T07:00Z (A_above_K, A=63647.18/K=63599.99): reviewer reconstruction →
  n_events=4388, min_C=0.5361 — sim identical.
- Window 2026-06-13T04:00Z (K_above_A): reconstruction with swapped legs → n_events=7778,
  C=0.9035, min_C=0.5047 — sim identical. Orientation correct both ways.
- Aggregates re-derived from tape_points.csv: events 134619, EV+ 36501, windows-w/-events
  23, windows-w/-EV+ 16, policy entered=16 (11 esc/5 pin) total −1.6780 mean −0.1049,
  per-minute histogram, dwell 4746.0 — all match.
- Row-level: ev_bucket == fair_bucket − C; fair_lin == 1 − 0.006·G; escape payoff 1 − C;
  quintile assignment correct; fee applied once per leg (no double-count).
- Seal: load_trades calls _guard_seal before open; run() never sets acknowledge; sealed
  refusal tested on all 17 days and end-to-end.

## Findings

- **F1 — MAJOR (zero impact on current numbers):** loader.load_trades sorts trades by the
  created_time STRING; microsecond fields are variable-width (measured on 2026-06-13:
  widths 6→1,144,508 / 5→113,883 / 4→11,394 / 3→1,185 / 2→108 / 1→9 / none→1), so
  lexicographic order inverts chronology — 40 real adjacent inversions in one day's
  15-minute output. No effect because walk_window re-sorts by parsed epoch (verified by
  exact match of independent reconstructions), but the loader's advertised ascending
  contract is false, confession #11's premise is false, and
  test_load_trades_sorts_newest_first_input_ascending only feeds fixed-width timestamps so
  it certifies the false claim. Fix: sort by parsed epoch, correct confession, add
  variable-width test, remove dead order_seen code.
- **F2 — MINOR:** "refuse silently-missing days" met only by hard crash (FileNotFoundError
  in sha256_file); the missing_trade_days receipt path is unreachable. Pre-check existence
  and record, or document crash-on-missing.
- **F3 — MINOR (interpretation):** event counts are trade-multiplicity-weighted (multiple
  same-microsecond trades each emit an event). Dwell-seconds is the time-faithful measure
  (simultaneous duplicates carry dwell=0). Report should say so.
- **F4 — NOTE:** histogram drops an event exactly at t=T−900 (still in totals). Negligible.
- **F5 — NOTE:** eligibility {OK, NO_PAIR} untested by smoke (06-13 had zero NO_PAIR);
  week run will admit NO_PAIR windows whose fair is imputed from OK-row quintiles —
  universe wider than the gate's. Orchestrator must know.
- **F6 — NOTE:** fail-open trade_id dedupe relaxes the fail-closed byte-identity discipline
  used for markets/candles. Documented, reasonable, registered.

## Confession adjudication (builder's 15)

1 ACCEPT (see F5) · 2 ACCEPT (verified both orientations) · 3 ACCEPT · 4 ACCEPT (boundary
inclusive verified; see F3) · 5 ACCEPT (no double-count) · 6 ACCEPT (see F4) · 7 ACCEPT
(honest, no look-ahead; "first" conservative vs "deepest") · 8 ACCEPT (reproduced exactly)
· 9 ACCEPT · 10 ACCEPT · **11 CHALLENGE (F1)** · 12 ACCEPT with NOTE (F6) · 13 ACCEPT ·
14 ACCEPT with NOTE (F2) · 15 ACCEPT.

## Bottom line

The negative policy result (mean −0.1049/window on 06-13) is NOT an artifact of any
understatement bug found: side selection, orientation, fee, EV curve, bucketing, staleness,
dwell all check out against independent reconstruction. The tension the sim surfaces —
event-level mean EV positive in quintile 3 (mean C 0.7333 < fair 0.8326) yet "enter at
first EV+" loses — is a real selection/one-day-sample story, correctly caveated, not a
coding error. Clear F1, address F2, then sound to run on the test week.

*(Transcribed verbatim from the reviewer session's final report by the orchestrator;
adjudicated into commission amendments A15.1–A15.4 the same day.)*

---

# ADDENDUM — flip-direction review (same reviewer session resumed, 2026-08-19)

# VERDICT: CONDITIONAL PASS

The FLIP direction is mechanically correct; the flattering hard-floor headline is
materially oversold. Required fixes are to REPORT/framing and one CSV column — not to the
flip arithmetic.

## Verified correct

- pytest 83 passed (reviewer's run); strangle aggregates byte-identical through the schema
  rename — F1 fix confirmed zero-impact.
- Settlement floor: flip payout recomputed from census result fields for all 23 windows,
  both orientations — every escape $1, every pin $2, zero mismatches. $0 branch impossible
  and hard-failed in three places. No boundary settlement on the day (closest print 1.33
  from a strike).
- Honest-fills mirror: 07:00 flip window hand-reconstructed from raw gz — n_events=4406,
  C=1.4567, min_C=0.9918, n_hardfloor=78 — sim identical. A yes-fill never prices the
  flip's NO high leg.
- No double-complement: yes_price + no_price == 1.0000 on all 1,271,088 raw trades of the
  day; flip path uses raw fields directly.
- Fees applied once per leg on the correct flip prices; independent recompute matched every
  reconstructed row to the 4th decimal.
- A15.1 landed (epoch sort; real-data inversions 40→0; variable-width + no-fraction tests).
- A15.2 landed (per-day existence pre-check; missing_trade_days recorded; run continues).

## Findings

- **F1 — CRITICAL (flattering framing):** the "+0.1288/window riskless" headline conflates
  the guaranteed floor with pin luck. Decomposed over the 9 entered windows: guaranteed
  riskless floor Σ(1−C) = +0.1594 total (+0.0177 mean); the single 08:00 pin contributes
  +1.0000 of pin bonus — 86% of the headline. The riskless claim must cite +0.0177.
- **F2 — MAJOR (leg-in fantasy):** hard-floor boxes assembled across up to 60s of
  staleness are not simultaneously fillable. First-hard-floor opposite-leg ages up to
  38.0s; ~13.6% of hard-floor events rest on a leg >15s stale. At --staleness 5: events
  2654→2158, windows 9→8, but DWELL 631.9s→230.0s — only 36% of the riskless exposure
  time survives a realistic simultaneity tolerance.
- **F3 — MAJOR (sub-tick margins):** 8 of 9 first-hard-floor windows have guaranteed floor
  < $0.01 (0.0004..0.0085). Only 03:00 (C=0.8622, floor +0.1378, 2.15s-stale leg) is a
  non-trivial riskless edge. The genuine riskless signal on the day is essentially one
  window.
- **F4 — MINOR (CSV footgun):** hard_floor column was populated for strangle rows too
  (116,671 rows with C<$1 that are NOT riskless). Blank it for strangle.

## Confessions 16–19: 16 ACCEPT with NOTE (F4) · 17 ACCEPT (needs F1–F3 qualifiers) ·
18 ACCEPT (equivalence proven, per-window cross-check) · 19 ACCEPT.

## Bottom line

The flip is arithmetically sound and the settlement floor is real — but the "nearly free
money" klaxon is a FALSE ALARM once decomposed: guaranteed riskless component +0.0177/window
(not +0.1288), sub-tick in 8 of 9 windows, dependent on stale legs (36% dwell survival at
5s), headline dominated by one lucky pin. Adjudicated into amendments A15.5–A15.8.

*(Transcribed verbatim by the orchestrator, same day.)*
