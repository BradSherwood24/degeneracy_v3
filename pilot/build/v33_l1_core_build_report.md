# V3.3 Phase L1 build report — the rolling-ladder core

Builder: Opus 4.8 (Brad's mandate). Branch `feat/v33-l1-ladder-core` off `origin/main` b5059ee.
Date 2026-09-22. Scope: a PURE, deterministic fork of the V3.2 core into `pilot/service/v33/`. No
runtime, executor, ledger, report, shadow-lab or ceremony (those are L2/L3). No network, no proxy, no
sealed/holdout read. `python` only.

## Files delivered (all absolute under the worktree `C:/Users/Brads/Python_stuff/dv3_wt_range`)
- `pilot/service/v33/__init__.py` — package re-exports.
- `pilot/service/v33/params.py` — `V33Params`, `load_v33_params`, `FROZEN_V33_PARAMS_SHA256`,
  `canonical_sha256` (same scheme as v32/box), the ladder-range shadow-E invariant.
- `pilot/service/v33/events.py` — re-export of the V3.2 event vocabulary (unchanged; no new event kind).
- `pilot/service/v33/actions.py` — re-export of the V3.2 action vocabulary; `V33Action = V32Action`.
- `pilot/service/v33/core.py` — the ladder core (`decide_v33`, `V33State`, `RestOrder`, `RungFill`,
  `WingBatch`, `CoalesceGroup`, `RollPending`, `_roll`, coalescing).
- `pilot/policy/v33_params.json` — the frozen L1 policy. Canonical sha
  `32d6cefcc16400420a6934a3f4ad44d34a2119ad5d83aec628596920c308d36f` (pinned in `params.py`).
- `pilot/tests/test_v33_params.py` (8), `pilot/tests/test_v33_core.py` (49),
  `pilot/tests/test_v33_golden.py` (9).
- `pilot/tests/fixtures/v33/golden_20260920T040000Z.json` (4.4 KB) — the golden-(a) fixture, extracted
  read-only from the LIVE-tree journal `journals_v32/20260920T040000Z.jsonl.gz` with the SAME filter as
  `pilot/build/mc/v33_ladder_ideal.py` (spot-bucket B80450 YES prints T-15..T-5 + the study's per-rung
  n/W/lock). 2026-09-20 is outside the 08-20..29 holdout and the 08-02..18 seal.

## Test counts
- Before (full suite on this worktree): 961 collected → 957 passed, 2 skipped, 2 errored. The 2 skips +
  2 errors are pre-existing environmental gaps (missing `historical-data/` corpus + `sim/out` train
  artifacts for `test_quintile` / a couple of corpus tests), NOT introduced by L1. To run the suite I
  provisioned the gitignored TRAIN artifact `sim/out/census_train.csv` (copied from the LIVE tree; a
  train artifact, not sealed/holdout; gitignored, never committed) — that lifted collection to the
  full 961.
- After: 1023 passed, 2 skipped, 2 errored. Delta = +66 all-green v33 tests; the previously-passing 957
  are unchanged (no regression). The 2 skips/2 errors are byte-for-byte the same pre-existing gaps.
- v33 breakdown: params 8, core 49, golden 9 = 66. `check_invariants` runs after EVERY event in every
  core test via the `_feed` wrapper.

## What was reused vs forked
Reused READ-ONLY from `service.v32.core` (imported, never reimplemented, so the two cores cannot drift on
shared law): `solve_n`, `wing_cost`, `lock_value`, `_compute_W`, `_bucket_cap`, `_select_spot`, `_fresh`,
`_fold_book`, and the ENTIRE in-process shadow (`ShadowSub`, `ShadowFill`, `_shadow_on_trade`,
`_shadow_complete`, `_wing_prices`). `V33State` deliberately keeps the V3.2 book-state field names
(`strike_tops/ts/tickers`, `bucket_tops/ts/tickers`, `spot_Sd/Su`, `shadows`, `bucket_map`,
`close_epoch`) so those helpers duck-type over it. Forked (rewritten for the ladder): the state record,
placement, the roll, the lifecycle handlers (`_apply_ack/_amended/_cancelled/_fill`), rung-fill booking,
and the coalesced wings.

## Design decisions I had to make (and why)

1. **Cap / n_min interaction — the whole ladder shifts, it never re-prices K orders.**
   `n_top = solve_n(2 - E_min - W, cap)` with `cap = no_ask(B) - 0.01` — the SAME `solve_n` and cap as
   V3.2. Because every rung is anchored RELATIVE to `n_top` (rung k at `n_top - k`), a binding cap that
   pulls `n_top` down pulls the WHOLE ladder down by the same amount and every rung stays post-only.
   `n_min` truncates FROM THE BOTTOM at placement: `_desired_rungs` stops at the first `n_top - k < n_min`,
   so K_effective < K. (Tests: `test_post_only_cap_shifts_whole_ladder_down`,
   `test_n_min_truncates_ladder_from_the_bottom`.)

2. **The roll trigger is `n_top` vs an ANCHOR, not vs the top survivor's price.**
   I added `V33State.anchor_n_top` = the n_top the ladder currently represents (set at placement, stepped
   ±1c on each confirmed roll/shrink/fallback). The roll fires on `dn = n_top - anchor_n_top` (subject to
   `tol` and `deb_ms`). This was a CORRECTION found during testing: a naive `dn = n_top - top_price`
   trigger fires spuriously right after a PARTIAL sweep (the sweep removes the top rungs, so the top
   survivor sits below n_top even though W — hence n_top — never moved), which would refill the shallow
   region. Anchoring the trigger fixes it: with W constant `dn = 0` after a partial sweep, no roll.
   (Tests: `test_no_spurious_roll_after_partial_sweep_with_w_constant`,
   `test_survivors_roll_after_partial_sweep_on_real_n_top_move`.)

3. **The roll direction (Brad's exact mechanism).** n_top DOWN 1c → amend the TOP order (shallowest) to
   `bottom_survivor - 1c` (it becomes the new deepest); n_top UP 1c → amend the BOTTOM order (deepest) to
   `top_survivor + 1c`. The other K-1 orders keep their `order_id` and queue. The MOVE TARGET uses the
   actual survivor extents (so it works with gaps from fills); only the TRIGGER uses the anchor.

4. **E_rung after a roll.** `E_rung = E_min + (n_top - price)`, recomputed for EVERY ladder order in
   `_recompute_context` whenever n_top changes, and set from the current n_top in `_emit_roll` for the
   moved order. So a moved order gets the correct deep margin and every unmoved order's label re-derives
   against the new top (the order that used to be #2 becomes the new top with E_rung = E_min, etc.). The
   invariant `E_rung == E_min + (n_top - price)` is asserted every step.

5. **Roll queueing under the debounce.** `roll_pending` holds the one moving order; any further W move is
   QUEUED (no second amend) until the amend is ACKNOWLEDGED. On `OrderAmended`, `_apply_amended` updates
   the moved order, steps the anchor, and RE-RUNS `_roll` so a multi-cent move continues converging one
   cent at a time. The `tol`/`deb_ms` gate is applied to the n_top signal EXACTLY as V3.2 applies it to
   its single price — so with the shipped `deb_ms = 5000` each cent of a multi-cent move paces at 5 s
   (rare: W seldom jumps >1c in 5 s). The 2c golden uses `deb_ms = 0` to exercise the ack-driven second
   roll. **Open question for the reviewer (Q-ROLL-DEB):** is the intended semantics "debounce each cent"
   (what I built — literal "same way V3.2 applies them") or "debounce the START of a convergence, then
   roll each acked cent without re-debouncing"? The latter converges faster on a fast 2c move; easy to
   switch (drop the `since_ms` check inside the `_apply_amended`-driven continuation only).

6. **The amend-first fallback.** The core emits `AMEND_REST` for a roll. If the executor's amend fails it
   falls back to cancel→create; that surfaces to the core as an `OrderCancelled` for the rolling order →
   `_apply_cancelled` drops the old rung and emits a fresh `PLACE_REST` at the roll's target price (same
   end state, fresh queue), stepping the anchor. (Test: `test_roll_fallback_cancel_creates_fresh_rung_at_target`.)

7. **n_min shrink on a roll.** If a shift-down roll would place the new bottom below `n_min`, the core
   CANCELS the top rung instead (the ladder shrinks by one; counted as a one-order roll). This is the one
   place a roll is a cancel, not an amend; it is rare (n_top near n_min ⇒ ~$1.90 wings). Documented; test
   `test_roll_down_past_n_min_shrinks_from_top`. After a shrink the ladder does NOT auto-regrow to K until
   the next bucket-change re-placement — a stated L1 limitation (deep-pin edge, low value to handle now).

8. **Wing coalescing (Q2) — a no-thread timer off the event clock.** A rung fill enters an OPEN
   `CoalesceGroup(first_ts, fills)`. A later fill within `wing_coalesce_ms` (150) of `first_ts` JOINS it;
   a fill after that FLUSHES the group into a `WingBatch` and opens a new one. Any event (book/clock/trade)
   whose `now > first_ts + wing_coalesce_ms` also flushes it (via `_coalesce_flush` at the top of
   `_wing_step`). The wings are taken sized to the batch's TOTAL count once the batch is closed AND the
   strike books are fresh to price them. So a full sweep (all rungs within 150 ms) → one wing pair sized
   to K; two fills 300 ms apart → two pairs. (Tests: the two coalesce tests + the sweep test.)

9. **Per-fill vs per-rung locks.** `RungFill` carries `rung` + `E_rung` (captured at fill) and `price`
   (the resting n). The batch's total lock = Σ count·(2 − (n + fee(n)) − W_paid); per-rung solved/realised
   locks are recoverable from `rest_fills`. `lock` on the `TAKE_WINGS` action is the batch total
   (informational). This is what the falsifier (PLAN sec 6) needs.

## The action / event contract L2 will wire (a superset of V3.2)
`decide_v33(params, state, event) -> (state, actions)`. Events consumed (all V3.2): `BookUpdate`,
`Trade`, `Fill`, `OrderAck`, `OrderAmended`, `OrderCancelled`, `ClockTick`. Actions emitted (all V3.2
kinds; no new kind): `PLACE_REST` (per rung, count = `lots_per_rung`), `CANCEL_REST` (per rung),
`AMEND_REST` (the roll, ONE order; `order_id` persists, `updated_client_order_id` = new coid),
`TAKE_WINGS` (sized to a coalesced batch total, 2 legs), `RETRY_WING`, `STAND_DOWN`,
`SHADOW_FILL_OUTSIDE_WINDOW`, `SHADOW_FILL_BELOW_MIN`, plus the WOULD_* shakedown twins.
Key state L2 reads: `ladder` (each `RestOrder` has coid/order_id/price/count/rung/E_rung/bucket_Sd),
`roll_pending`, `wing_batches` (each `WingBatch.fills` = per-rung breakdown, `.total_count`),
`rest_fills`, `rungs_filled`, `sets_done`, `roll_count`, `roll_single_order_count`, `replace_count`,
`anchor_n_top`, `n_top`, `cap`, `W`, `spot_Sd/Su`. The executor's amend-fail fallback is delivered to the
core by feeding an `OrderCancelled` for the rolling `order_id` (the core then re-places at the target).

## Golden (a) numbers vs the study (2026-09-20T04:00:00Z, sweep W = 1.4737)
The study (`v33_ladder_ideal.py`, whose logic I re-ran on this one window; result matches the persisted
`pilot/build/mc/v33_ladder_ideal.json` rung_locks, cross-checked in `test_study_rungs_match_persisted_ideal_json`)
solves each rung independently and, at this W, its two deepest rungs COLLAPSE onto the same price:

| study E | n | solved lock | core rung n | core lock |
|--------:|----:|-----:|----:|-----:|
| 5 | 0.45 | +5.89c | 0.45 | +5.89c |
| 6 | 0.44 | +6.90c | 0.44 | +6.90c |
| 7 | 0.43 | +7.91c | 0.43 | +7.91c |
| 8 | 0.42 | +8.92c | 0.42 | +8.92c |
| 9 | 0.41 | +9.93c | 0.41 | +9.93c |
| 10 | 0.40 | +10.95c | 0.40 | +10.95c |
| 11 | 0.39 | +11.96c | 0.39 | +11.96c |
| 12 | 0.38 | +12.98c | 0.38 | +12.98c |
| 13 | 0.37 | +13.99c | 0.37 | +13.99c |
| 14 | 0.36 | +15.01c | 0.36 | +15.01c |
| 15 | **0.36** | **+15.01c** | **0.35** | **+16.03c** |

**Load-bearing divergence (documented, asserted).** The core lays down K DISTINCT consecutive cents; it
cannot rest two orders on one price (the invariant). So where the study's E14 and E15 both solve to
n=0.36, the core's 11th rung sits at a distinct **0.35** (lock +16.03c) — reproducing the study EXACTLY
for the 10 shared rungs (0.45..0.36 → +5.89c..+15.01c) and delivering a DEEPER 11th. The lock is
print-independent (`lock = 2 − n − fee(n) − W`), so the golden pins the strike books at the sweep-instant
W=1.4737 (whole-cent books yes_ask(Sd)=0.55, no_ask(Su)=0.90 → W=1.4737 exactly) and replays the REAL
125-print tape; all 11 rungs fill, coalescing into 2 batches (a 3-rung print ~T-503s + an 8-rung burst
~T-466s), 11 contracts total. (Test: `test_golden_a_full_sweep_fills_all_11_rungs_with_study_locks`.)

## Deliberate divergences from V3.2 (a faithful fork, not a refactor)
- `E` (single rung) → `E_min` (ladder-top anchor); `contracts` (order size) → `lots_per_rung` (per rung).
- `_requote` (one price, amend-first) → `_roll` (Brad's one-order-per-cent shift, amend-first with the
  same cancel→create fallback), plus `anchor_n_top` for the trigger.
- One resting order → a `ladder` tuple of K; `rest_live/rest_pending/rest_remaining` → `ladder` +
  per-order `pending`/`live`.
- Single-fill `WingBatch` → coalesced `WingBatch` (per-rung `fills`, total-count wings) + `CoalesceGroup`.
- Shadow: kept BYTE-IDENTICAL (imported from v32) — its E ladder is now validated to lie inside
  `[E_min, E_min + (rungs-1)c]` instead of `E in shadow_Es`.
- `max_sets_per_hour` 1 → 11 (K); `replace_rate_alarm_per_min` 60 → 120 (per ladder).
- The one-set-per-hour latch → an allotment latch when every rung has filled (or `max_sets` reached).

## Not done in L1 (correctly out of scope)
Executor/venue-truth reconciliation, ledger/money-math, report/scoreboard, the live shadow ladder + SO-3
deep observation ladder, stops wiring (S1..S5, S4 per Q5), ceremony/falsifier. The core exposes every
quantity these need.

## Anything unverified / open questions for the reviewer
- **Q-ROLL-DEB** (see decision 5): debounce-each-cent vs debounce-the-start-of-convergence.
- **n_min shrink regrowth** (decision 7): the ladder does not auto-regrow to K after an n_min shrink until
  a bucket-change re-placement. Acceptable? (Deep-pin edge.)
- **Shift-up after a partial sweep** relocates a surviving DEEP order to a SHALLOW price (into the region
  where rungs already filled). It never adds a lot (total filled ≤ K always), but it is a survivor moving
  back up the ladder — confirm that matches the intended risk model.
- **Golden (a) W pinning**: golden (a) holds W at the sweep instant (1.4737) and relaxes the freshness
  bounds for the multi-minute print replay (the fixture carries prints, not a dense book tape). Freshness,
  spot selection, and W-drift/rolls are covered by dedicated unit tests, not golden (a). The realistic
  W-drift full-tape replay is L2's replay-lab job.
- Coalescing takes wings only when a batch is CLOSED AND the strike books are fresh; under a silent
  strike feed the take defers (same as V3.2's wing gate). L2's driver ticks the clock continuously, so a
  closed batch takes on the next fresh book — verify the driver feeds ClockTicks densely enough.

---

# Round 2 (2026-09-22) — addressing PR #85 review (BLOCK, narrowly)

Reviewer verdict was BLOCK on two label/invariant defects; mechanics were verified correct. All items
addressed below. Suite after Round 2: **1036 passed, 2 skipped, 2 errors** (the 2 skips/2 errors are the
same pre-existing corpus gaps; +13 new v33 tests over R1's 66 → **79 v33 tests**). The reviewer's own
adversarial probe (`scratchpad/adv_test.py`) now runs against this branch with **zero invariant
violations** in all four scenarios.

## BLOCKING #1 — `rung` refreshed alongside `E_rung` (FIXED)
`_recompute_context` now refreshes BOTH `rung` (`_rung_of(n_top, price)`) and `E_rung` for every ladder
order on every context recompute (`core.py` `_recompute_context`), and `_apply_amended` / the
`_apply_cancelled` fallback derive both from the CURRENT `n_top` (not emit-time). `RungFill.rung` is now
always the live position, so the §6 per-rung falsifier key is correct after any number of rolls. New
helper `_rung_of`. Tests: `test_rung_refreshed_with_e_rung_after_roll_down_and_up`,
`test_rungfill_captures_live_rung_after_rolls`.

## BLOCKING #2 — labels reconciled to current n_top + invariant relaxed for stranded rungs (FIXED)
(a) `_apply_amended` and the fallback re-place derive `rung`/`E_rung` from `st.n_top` at ACK time (guard
`None` → the stored target). (b) `check_invariants` no longer asserts `rung >= 0`; it asserts LABEL
CONSISTENCY (`rung == _rung_of(n_top, price)` AND `E_rung == E_min + (n_top - price)`) and explicitly
permits a rung ABOVE `n_top` (negative rung = "stranded above the top") as the honest transient during a
W revert or a cap crash. Tests: `test_w_reverts_during_in_flight_roll_invariants_green`,
`test_cap_crash_fast_shift_then_converges_invariants_green`, `test_invariant_flags_stale_rung_label`,
`test_invariant_allows_negative_rung_above_n_top`. The docstring's "safe to run live" claim now holds on
the fast-market sequences the reviewer demonstrated.

## Pacing (Q4/Q5 + Q-ROLL-DEB) — RESOLVED, param-driven (Brad's params)
Implemented as instructed: **`deb_ms` debounces only the START of a convergence**; once committed
(`converging_dir` == the move's sign) each subsequent cent rolls as soon as the prior amend acks (no
re-debounce) while the sign is unchanged; **a sign flip re-debounces**. Added
**`fast_shift_min_cents` (default 4)**: when `|n_top − anchor|` ≥ that after the start debounce (e.g. a
cap crash stranding most of the ladder), the whole ladder is shifted in ONE step (cancel-all/place-all,
like a bucket change) instead of crawling K rolls — closing the reviewer's ~65 s exposure window
(QUESTION #4). New state field `converging_dir`. Tests: `test_pacing_2c_one_debounce_then_ack_driven`
(2c completes in 2 acks with one debounce), `test_pacing_sign_flip_re_debounces`,
`test_pacing_1c_single_roll_unchanged`, `test_fast_shift_5c_move_cancels_all_places_all` (5c → one-step),
`test_3c_move_still_crawls_not_fast_shift`.
**Brad's params (both defaults recorded here):** `deb_ms = 5000` (start-of-convergence debounce),
`fast_shift_min_cents = 4` (large-jump one-step threshold). Both are params in `v33_params.json`; change
without code. Params re-pinned: new `FROZEN_V33_PARAMS_SHA256 =
c0201af78015e24fa7d8984d9f9747215330f5bee29fd2567290c2653c244a20` (R1 sha kept as
`PREVIOUS_V33_PARAMS_SHA256_L1_R1`).

## NITs (all done)
- **NIT #7 (`refill_in_window`)** — now ENFORCED in `load_v33_params`: only `false` is accepted in L1
  (Q3 no-refill); `true` fails closed (reserved for L2). Test:
  `test_refill_in_window_true_fails_closed`. Also `fast_shift_min_cents < 2` fails closed.
- **NIT #8 (stronger `check_invariants`)** — added: `roll_pending` ⇒ at most one ladder order under the
  moving coid; a taken wing batch has exactly two legs each sized to `total_count`;
  `rungs_filled == len(rest_fills)`; prices are whole cents.
- **NIT #9 (fallback cap re-check)** — the `_apply_cancelled` fallback re-place now clamps to `st.cap`
  (`min(target, cap)`) and drops the rung if that falls below `n_min`.
- **NIT #6 (5..15c vs 5..16c wording)** — PLAN_V33 §5 L1 updated (separate commit) to "rungs at
  n_top, n_top−1c … i.e. realised margins span 5..(E_min+K−1)c and, at low W where the study's deepest
  rungs collapse, the ladder's distinct deepest rung realises up to (E_min+K)c." **L2/L3 contract note:**
  L3 MUST compute each rung's "solved E" reference as `lock_value(price, W_at_fill)`, NOT the integer
  `E_rung` label — the label is the rung's nominal position; the true solved margin is the price/W
  economic value (at the golden W the deepest rung's label is 15c while its realised lock is +16.03c).

## Policy items left as Brad's call (no code change, per the coordinator)
- **Shift-UP into the filled region after a partial sweep** stays as built (whole ladder shifts, lot cap
  honoured — `live + filled ≤ K` verified). Noted as Brad's open question (reviewer QUESTION #3).
- **n_min shrink regrowth** — unchanged (safe direction; documented); confirm at L2/L3.
