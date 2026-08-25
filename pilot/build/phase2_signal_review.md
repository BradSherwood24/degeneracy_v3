# Phase 2 signal engine + parity harness — adversarial review (SEPARATE opus48 REVIEWER)

Reviewed 2026-08-21. Scope: `pilot/policy/policy_params.json`, `pilot/service/{policy,_simlaw,
signal,quintile,parity,parity_report,shakedown}.py` and their tests, against `pilot/PLAN.md`
(Phase 2, review disposition F15), `pilot/ceremony/{commission,falsifier}.md`, the Phase-2 build
report (9 confessions), the Phase-1 review handoffs (F7/F8/F2), and the frozen sim law
(`sim/{tape_sim,census,gate_fit}.py`, `sim/out/{census_train.csv,gate.json}`,
`claudes-corner/viz/aggregate.py`).

Constraints honored: no `.env`/`*.pem` read; nothing under `sim/out/sealed_eval/`; NO sealed-day
tape read (the quintile reproduction loads only TRAIN market files 2026-06-11..21); no live
network dialed. I own the Phase-2 modules; I did NOT modify any Phase-1 module (`service/{book,
journal,proxy_auth,record_window,replay,wake,ws_client}.py`), any `sim/` file, or the concurrent
Phase-3 executor/reconciler surface. `policy_params.json` is UNCHANGED — canonical sha still
`1b01fd98e1c76748261fbe80f961d9ae8a55853c7807de71af508eece8203656` (re-verified).

**VERDICT: APPROVE-WITH-FIXES.** Two defects found and FIXED in-tree (with regression tests); one
MEDIUM honesty bug FLAGGED for the Phase-3 fills owner (not fixed, to avoid colliding with the
real fills schema); the rest are LOW/informational flags. No fire-wrongly path survives: I could
not construct any input sequence where `decide()` fires on a stale/suspect/unknown/future book,
inside the settle cutoff, in warmup, twice per window, on both sources, on an undiscovered leg, or
with the strangle stood down. C is Decimal end-to-end. The F15 neutrality FAILURE trips correctly.

Test count: **184 passed, 0 failed** (~2.9s) = 165 prior + **19 new review probes**
(`tests/test_review_probes2.py`).

---

## Findings

### F1 — MEDIUM — FIXED — parity sim-side ignored the no-orders settle cutoff → spurious F15 failures
`parity.sim_entry_for_window` applied the freshness gate and the C/ev thresholds but did NOT bound
`t_minus_s` to `decide()`'s firing window. `decide()` refuses to fire inside the settle cutoff
(`t_minus < no_orders_after_s_to_settle` = 1s → StandDown) and in warmup (`t_minus > WINDOW_S`).
A tape moment that qualifies ONLY inside the final 1 second therefore made the SIM side "fire"
while the LIVE side stood down → `BIN_SIM_ONLY`, `neutrality_ok=False` — an F15 FAILURE that is NOT
the documented book-vs-prints delta but a harness modeling gap. Because the shakedown gate
(falsifier) requires zero unexplained bin-1/2 flips, this is a spurious blocker of promotion (fail-
safe in direction, but it corrupts the measurement and the F15 verdict).

Confirmed by probe: a single `flip` row at `t-0.5s`, `C=0.84` → `sim_entry_for_window(...).fired ==
True`, while `decide()` at `t-0.5s` returns only StandDown.

**Fix (minimal, in `parity.py`):** `sim_entry_for_window` now skips any row with
`t_minus_s < no_orders_after_s_to_settle` or `> WINDOW_S`, mirroring `decide()`'s firing window
exactly (the `t == cutoff` edge still fires on both sides, matching `decide()`'s `<` cutoff).
Regression: `test_sim_side_respects_no_orders_cutoff`, `test_no_orders_cutoff_no_longer_spurious_
bin1`, `test_sim_side_upper_window_bound`. No signature change.

### F2 — LOW/MEDIUM — FIXED — shakedown WouldFire-only guarantee was unenforced (a live-fire state would journal FIRE)
`decide()` emits `FIRE` (not `WOULD_FIRE`) whenever `state.shakedown is False`. `ShakedownRecorder`
took a caller-supplied `WindowState` and constructed its `SignalDriver` with NO check that the
state was a shakedown state. Confirmed by probe: a `shakedown=False` state driven through
`SignalDriver` emits `FIRE`, and `ShakedownRecorder(...)` accepted such a state without complaint —
so the "WouldFire-only, no code path constructs a Fire action" guarantee (build report §Parity /
confession spirit) rested on convention alone. There is no Executor in the shakedown rung so no
order would be placed, but the runner would journal a `fire` record and the safety CLAIM would be
false.

**Fix (minimal, in `shakedown.py`):** `ShakedownRecorder.__init__` now raises `ValueError` on a
non-shakedown `WindowState` (fail-closed, house law). Regression:
`test_shakedown_recorder_refuses_live_fire_state`, `test_shakedown_recorder_accepts_shakedown_
state_and_would_fires`.

### F3 — MEDIUM — FLAGGED (Phase-3 fills owner must fix; not fixed here) — empty price-deltas silently score BIN_BOTH_MATCH
`parity.assign_bin`: when both sides fired and `fills.filled is True` but `_price_deltas` returns
`{}` (no fills leg ticker matches the paired high/low tickers, or `legs=()`), `any_diff` is `False`
and the window is scored `BIN_BOTH_MATCH` — "both filled, same price, payoff matches, the sim tells
the truth" — with ZERO price comparison actually performed. Confirmed by probe: a `WindowFills`
with a wrong-ticker leg (or no legs) → `bin == 5`, `detail["price_deltas"] == {}`. That is a false
"sim is honest" receipt, the exact dishonest-measurement direction this review targets. I did NOT
fix it because bins 3–5 and the `WindowFills` schema are explicitly Phase-3's to build (build
report confession #9: "placeholder … Phase 3 will supply the real record"), and a fix now would
collide with the concurrent Phase-3 fills work. Handed forward as a visible marker:
`test_FLAGGED_empty_deltas_scored_as_match_phase3_must_fix` asserts the current (buggy) behavior.
**Phase-3 fix direction:** require ≥1 comparable leg before any bin-5 verdict; absent comparable
legs is a data/schema fault, never a "match".

### F4 — LOW — FLAGGED — shakedown wall-clock fallback vs parity replay's recorded-ts fallback (determinism seam)
For a frame with no server `ts`, `ShakedownRecorder._drive` falls back to `self.clock()` (wall
clock) as the event's `now`, whereas `parity.live_book_events` falls back to the record's recorded
`local_ts`. So a live shakedown FIRE decision on a missing-`ts` frame is computed against wall-clock
time, but the parity replay of the same journal recomputes it against the recorded receive time —
the two can disagree on the freshness gate, breaking the golden "same function live and in replay"
property for that frame. Real orderbook deltas carry `ts` (V2-verified), so this is dormant, but it
is a genuine seam. Recommend the shakedown shell fall back to the SAME recorded receive-ts the
journal will replay, not `clock()`.

### F5 — LOW — informational — SimEntry.ev is meaningless for a sub-$1-flip winner
`sim_entry_for_window` sets `SimEntry.ev = Decimal(winner["ev"])` regardless of source. For a
sub-$1-flip winner that is the FLIP-direction EV (`1 + pin_rate − C_flip`), which is not the
decision input for the arithmetic floor (the sub-$1 entry keys on `C < $1`, not on EV). Harmless to
firing, but a report reader could misread the reported `ev` as the entry's edge. Reporting-only.

### F6 — LOW — informational (INHERITED, Phase-1 F2 pending Brad) — pairing-authority split; anchor-tape dup tiebreak
`quintile.py` reproduces census's pairing faithfully — `nearest_hourly_strike` pools ALL co-settling
hourly strikes and picks nearest-to-anchor (ties → `EXCL_NEAREST_TIE`), and `decide()`/shakedown key
off `quintile.py`'s (census-correct) high/low tickers, NOT off `wake.py`'s subscribed generation.
On a dual-generation window (the 2026-07-31 shape) where `wake.py` subscribes the wrong generation,
the book for the correct leg may never arrive → fail-closed NO-fire (safe) but a discovery-artifact
that corrupts the paired measurement — this is exactly Phase-1 F2, still pending Brad's ruling
before the strangle trades live; not re-adjudicated here. Separately, `anchor_tape_from_markets`
keeps the FIRST strike-bearing 15M market per epoch while census's `anchor_by_epoch` OVERWRITES to
the LAST; they diverge only if two DIFFERENT strike-bearing 15M markets share one close-epoch (none
in the corpus — the ≥20-window reproduction is string-exact). Defensive-difference note only.

### F7 — informational — Phase-4 owes the trailing-15M tape fetch for σ̂ (confession #1)
`quintile.compute_window_stats` needs the 8 trailing 15M markets (T-7200..T) for the sigma-hat
anchor tape; `WakeContext` fetches only the co-settling close. This is a Phase-4 wiring point (build
report confession #1). Fail-closed if unwired: no tape → `InsufficientTape` → `NoQuintile` → stand
down. Safe, but the pilot cannot assign a quintile (hence cannot fire the strangle, and cannot route
sub-$1 by quintile) until Phase 4 wires it. Live-verification item stands: whether `/markets`
returns already-settled 15M markets by close-ts.

---

## Fire-wrongly audit (highest stakes) — all paths fail closed

Verified by a parametrized sweep + boundary tests (`test_review_probes2.py`) and by reading
`decide()`:

- **stale / suspect / unknown-age / future-ts book** → `_both_fresh` returns False (both tops
  present, neither suspect, both ages known and ≤ 1.0s required); a future ts yields negative age →
  `_leg_age` returns None → not fresh. No fire in any case.
- **inside the settle cutoff** (`t_minus < 1s`) → StandDown, never FIRE; **warmup** (`t_minus >
  900`) → seed only. Both edges (`t==1`, `t==900`) resolve to `decide()`'s inclusive intent.
- **twice per window / both sources** → first qualifier sets `entered=True`; step 2 returns `[]`
  thereafter; exactly one Action ever, sub-$1 evaluated first so it wins a same-event tie.
- **undiscovered leg (F7)** → a `BookUpdate` whose market ≠ high/low ticker returns immediately,
  state untouched.
- **strangle_disabled** → the Q1-strangle branch is gated by `not st.strangle_disabled` AND
  `st.quintile == 0`; sub-$1 continues (structure-independent), matching A2/PLAN.
- **sub-$1 boundary** → strict `<`: `C == C_max` does NOT fire (matches sim `HARD_FLOOR` = `C < $1`
  strict). **EV boundary** → inclusive `>=`: `ev == 0.05` fires, `ev == 0.0499` does not — matches
  the sim/`aggregate.py` first-entry ladder convention (`enter at ev ≥ x`). Both sides of the parity
  harness use the SAME conventions, so no boundary creates an artificial bin divergence.
- **Decimal vs float** → `TopOfBook` asks are Decimal (`book._to_decimal`); `_leg_cost` = `price +
  fee(price)` with the imported census `fee` (Decimal); `flip_cost`/`strangle_cost`/`ev` all Decimal;
  the sub-$1 and EV comparisons are Decimal-vs-Decimal. Floats appear only in ages/σ̂ (off the money
  path, per house law). Probed: `Action.C` and every `LegOrder.limit_price` are Decimal.

## Law-reproduction audit

- **Import, not retype.** `_simlaw` is a pure re-export choke-point: `fee`, `hole_G`, `sigma_hat`,
  `EVCurve`, `WINDOW_S`, `bucket_of`, `quintile_edges`, `close_epoch`, `CENSUS_TRAIN_SHA256`,
  `TAPE_FIELDNAMES` all come from `sim/`. No fee/EV/edge constant is retyped in `signal.py`.
- **EV curve is sha-verified.** `load_ev_curve` → `EVCurve.from_census` verifies the census file
  against `tape_sim.CENSUS_TRAIN_SHA256` (`580d14…`) and refuses on mismatch; `load_ev_curve()`
  succeeds in tests, so the shipped `census_train.csv` matches.
- **fair_bucket(strangle) = 1 − P(pin|q).** `EVCurve.fair[q] = 1 − pin_rate[q]`; both the live seed
  (`WindowState.fair_strangle_q = fair_for("strangle", q)`) and the sim tape's `ev` column use the
  identical fair, so the only strangle-EV difference across the harness is C (book vs prints).
- **σ̂ = census A3.2, and the reproduction is NOT circular.** `quintile.compute_window_stats` builds
  the 15M FLOOR-STRIKE anchor tape from raw `/markets` records and calls the imported
  `census.sigma_hat` (9 anchors T..T-7200, 8 diffs, ddof=1); `G = hole_G(A, nearest-K)`. The ≥20-
  window test feeds RAW TRAIN market inputs through the pilot code path and asserts G (2dp), σ̂
  (6dp), G/σ̂ (6dp) and quintile string-equal to `census_train.csv` OUTPUTS across all five
  quintiles — genuine reproduction of the pilot's own tape-assembly + pairing, not a same-formula
  echo. Edges equal `gate.json`'s `quintile_edges_gos` exactly. Confession #1 (floor-strikes, not
  candles) is CORRECT: census A3.2 uses the 15M floor-strike anchor tape, not candles.

## Parity-honesty audit

- **First-entry / covered-prefix semantics** mirror `aggregate.py`: rows are chronological within a
  window; the earliest qualifying moment (largest `t_minus_s`) is the entry. `parity` picks the
  max-`t_minus_s` qualifier explicitly (order-independent, so more robust than relying on file
  order), which equals `aggregate.py`'s first-chronological-qualifier and equals `decide()`'s
  first-event-that-qualifies.
- **Same-event tie (sub-$1 vs Q1-strangle).** No frozen sim convention exists (tape_sim runs the two
  directions as separate rows; aggregate keeps them in separate cells). The pilot invents sub-$1
  priority (the arithmetic floor) and applies it CONSISTENTLY on both harness sides. Critically, the
  tie rule can only change WHICH source is attributed on a both-fired window (surfaced as the softer
  `source_match=False` flag, `neutrality_ok=True`); it can NEVER flip a fire→no-fire decision, so it
  does NOT create artificial bin-1/bin-2 divergences. Verified against the builder's
  `test_both_fired_source_mismatch_flagged` and by reasoning over the max-`t_minus_s` selection.
- **F15 neutrality FAILURE trips.** Independent probe `test_f15_failure_trips_on_documented_delta_
  flip`: sim prints → flip C 0.84 (<$1 fires), live book → flip C 1.2336 (≥$1 no fire) →
  `BIN_SIM_ONLY`, `passed == False`, `n_neutrality_failures == 1`. The harness `passed` iff zero
  bin-1/bin-2 flips, including ones caused by the documented book-vs-prints C delta — exactly the
  F15 discipline.
- **Shakedown mode is WouldFire-only** (post-F2 fix): no code path in the shakedown rung constructs a
  Fire action; a live-fire state is refused at construction.

## Confession rulings (all 9)

1. **σ̂ input = 15M floor-strikes, not candles.** ACCEPTABLE — correct read of census A3.2, verified
   by exact reproduction. Phase-4 must wire the trailing-tape fetch (F7); fail-closed until then.
2. **Same-event tie → sub-$1 priority.** ACCEPTABLE — consistent across the harness; affects only
   source attribution, never a fire/no-fire flip; no frozen convention to contradict.
3. **Window-level mutual exclusion (one entry/window).** ACCEPTABLE — matches "you place one pair";
   sim-side mirrors it.
4. **Entry window `[T-900, T)` + settle cutoff.** ACCEPTABLE for the LIVE side; the SIM side was
   NOT applying the cutoff → **F1 (FIXED)**. Post-fix both sides share the window exactly.
5. **1s freshness binds; 60s is the outer horizon.** ACCEPTABLE — 1s is strictly tighter and decides
   firing; the sim-side `_fresh_row` uses the same 1s bound.
6. **StandDown emitted only for the settle cutoff; other fail-closed paths silent.** ACCEPTABLE —
   silent transient fail-closed matches "no order, no noise"; StandDown emitted once.
7. **Imports Phase-1 `ws_client._parse_server_ts`.** ACCEPTABLE — composition/import, not a
   modification; traced for a future rename.
8. **Live-side server_ts → local_ts fallback.** ACCEPTABLE for the parity replay (deterministic,
   recorded). See **F4**: the shakedown shell's `clock()` fallback is a determinism seam vs this.
9. **Fills schema minimal/placeholder.** ACCEPTABLE-WITH-FLAG — bins 3–5 are Phase-3's; the empty-
   deltas → BIN_BOTH_MATCH honesty bug (**F3**) MUST be fixed when Phase 3 supplies the real fills.

---

## Fixed vs flagged

- **FIXED (mine, with regression tests):** F1 (`service/parity.py` — sim-side firing-window bounds),
  F2 (`service/shakedown.py` — shakedown-state guard).
- **FLAGGED (not fixed):** F3 (Phase-3 fills owner — empty-deltas bin-5), F4 (shakedown clock
  fallback), F5 (SimEntry.ev cosmetic), F6 (Phase-1 F2 pairing, pending Brad; anchor-tape dup note),
  F7 (Phase-4 trailing-tape wiring).

## Files
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\parity.py`
  (`sim_entry_for_window` firing-window bounds; imports `WINDOW_S`).
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\shakedown.py`
  (`ShakedownRecorder.__init__` fail-closed shakedown guard).
- Added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests\test_review_probes2.py` (19 probes).
- Unchanged: `policy/policy_params.json` (sha `1b01…8203656`), `service/{policy,_simlaw,signal,
  quintile,parity_report}.py`, all Phase-1 modules, all `sim/` files.

## Interface notes for the Phase-3 builder (LOUD)
- **`signal.py` Action/LegOrder types UNCHANGED.** `Action(kind ∈ {WOULD_FIRE, FIRE, STAND_DOWN},
  source, legs: tuple[LegOrder,...], count:int, C:Decimal|None, ev:Decimal|None, t_minus_s, reason)`;
  `LegOrder(ticker, side ∈ {"yes","no"}, count:int, limit_price:Decimal)`. `decide()` still returns
  `(WindowState, list[Action])` and is pure.
- **`policy.load_policy()` loader UNCHANGED.** Defaults `expected_sha=FROZEN_POLICY_SHA256`
  (`1b01…8203656`) and self-verifies; `expected_sha=None` bypasses (re-pin tooling only). Imbalance
  bounds are on `PolicyParams.imbalance` + the `no_orders_after_s_to_settle` property.
- **Behavior change (stricter, safe):** `parity.sim_entry_for_window` now excludes tape rows outside
  `[no_orders_after_s_to_settle, WINDOW_S]` — the sim side no longer manufactures neutrality failures
  from moments the live pilot would never trade.
- **New precondition:** `ShakedownRecorder` raises `ValueError` on a non-shakedown `WindowState`. The
  Phase-3 live-fire flow must construct its own Executor path (not via `ShakedownRecorder`).
- **F3 is yours:** do not let `assign_bin` report BIN_BOTH_MATCH when no fills leg matches the paired
  tickers; require ≥1 comparable leg before any bin-5 verdict.
