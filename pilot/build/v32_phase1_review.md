# V3.2 Phase 1 review — pure core (`service/v32/`)

Reviewer: Opus 4.8 (Fable's delegated reviewer). Branch `v32/phase1-core`, PR #31, commits
`9ddd751` + `0be6625` on base `afcde6b`. Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`.
Reviewed as an adversary because Phase 3 places REAL resting orders + taker completions with Brad's
money. No network, no proxy, no holdout/seal read (the golden fixture is 2026-09-04, pre-extracted).

## PHASE 2/3 MUST (carry-forward for the next builder)

These are NOT fixed in Phase 1 (the pure core sends no orders). Close them before Phase 3 arming:

* **F-1 — retained cancel context.** Retain a `coid/order_id -> (price, count, bucket_Sd)` map of
  every order placed this hour so a `Fill` on a just-replaced / eagerly-cancelled order is booked
  (right price + bucket) and latches the one-set rule; drive cancels through a status-confirmed path
  so the fill arrives as `OrderCancelled.filled_count_before_cancel`. Without it a fill on an
  untracked own-order is dropped -> untracked unhedged leg + possible double entry.
* **L-1 — `solve_n` cap ceiling.** Floor the cap (or add `n <= cap` to the loop) so a non-whole-cent
  cap can never yield an `n` that crosses the book / trips post_only.
* **L-2 — out-of-order `server_ts`.** Feed a monotonic-ish clock, else a slightly-behind
  cross-market frame makes a strike look stale -> spurious cancel/replace churn.
* **L-3 — `no_ask == 1.0` wing.** `_wing_prices` accepts `na == 1` but caps the IOC limit at 0.99
  (never fills) while `_compute_W` rejects `na == 1`; reconcile the boundary.
* **L-4 — multi-lot partial.** At `contracts > 1` (size is pinned 1) a partial rest fill orphans the
  remaining lots on the exchange, untracked; handle if the size ever rises.

## RULINGS APPLIED (coordinator, on this review) — see the commit on `v32/phase1-core`

* **F-2 RESOLVED** — the initial both-wings take is now UNCONDITIONAL; `lock_floor` gates only the
  RETRY of a single missing leg. Core + docstrings + tests updated (see F-2 below, now historical).
* **M-1 RESOLVED** — `deb_ms` set to 5000 in `policy/v32_params.json`; `FROZEN_V32_PARAMS_SHA256`
  re-pinned to `c6715fc7fd8339e0cc8877bd39bb78b04239eda9c490bde71a53333a48bdfb92` (pre-freeze build
  act). Golden test uses its own explicit tol/deb and stays green.
* **L-6 RESOLVED** — `load_v32_params` now raises `V32ParamsInvalid` (fail-closed, clear message)
  when `E` is not in `shadow_Es`; test added.

## VERDICT: APPROVE WITH FIXES

Phase 1 is a well-built pure core: money is Decimal end to end, time comes only from event
timestamps, state is frozen + `replace`-transitioned, the sha-pin/fail-closed discipline mirrors
`box.py`, and the requote/fill/shadow law tracks the pinned sims. I found four clear defects (all
now fixed on the branch with regression tests) and a set of executor-facing design gaps that are NOT
Phase-1 merge blockers (the core cannot send an order in Phase 1) but MUST be closed before Phase 3
arming. Those are recorded below as findings, not code changes.

Suite after fixes: `python -m pytest -q` -> **674 passed** (was 670; +4 regression tests). The
v32 subset is 42 (was 38).

---

## Fixes applied (this branch)

All four were confirmed by a live repro before fixing and are pinned by a new test.

* **F-A (safety) — suspect strike book was priced.** `core.py:_compute_W` / `_wing_prices` checked
  presence/range but not `TopOfBook.suspect`, so a malformed-delta / seq-gap strike book (untrusted
  per the `TopOfBook` contract) was used to compute W AND to price the real taker wing completion.
  Buckets already honored `suspect` (via `_valid_two_sided`); strikes did not. Fixed: both reject a
  suspect Sd/Su top -> W None -> no quote / no take. Test `test_suspect_strike_book_is_not_priced`.
* **F-B (safety) — a silent feed left a rest live.** `decide_v32`'s `ClockTick` branch did not call
  `_recompute_context`, so `st.W` kept its last book value and staleness was never re-evaluated on a
  tick. A stale-then-silent feed (books stop arriving) left the resting order live until the next
  book frame or expiry — exactly the failure the task probes. Fixed: `ClockTick` now recomputes the
  context against the tick clock, so a strike gone stale relative to `now` makes W None and the
  requote gate cancels + stands down. Matches PLAN_V32's "on lag > freshness the core cancels the
  rest." Test `test_silent_feed_clocktick_cancels_stale_rest`.
* **F-C (money) — `filled_count_before_cancel` booked at the wrong price.** `_apply_cancelled`
  cleared `rest_live` BEFORE reading its price, then fell back to `st.desired_n` (which drifts with
  the wings). A partial fill during a cancel was booked at the current desired_n, not the resting
  order's actual fill price — corrupting the lock and the wing sizing. Repro showed 0.60 booked for
  a 0.45 resting order. Fixed: capture the matched order and book at `matched_order.price`. Test
  `test_partial_fill_before_cancel_books_at_resting_price_not_desired_n`. (Note the residual gap
  F-1 below for the eager-clear cancel paths where no slot matches.)
* **F-D (integrity) — duplicate wing fill double-counted a set.** `_maybe_close_set` incremented
  `sets_done` whenever all legs read "filled", so a fill reported twice (the plan wires fills from
  BOTH the fill channel AND a status poll) drove `sets_done` to 2 after completion. Fixed: gate on
  `wings_needed` so the close is idempotent. Test
  `test_duplicate_wing_fill_does_not_double_count_sets_done`.
* **F-E (legibility, no behavior change) — misleading maker-fee comment.** `lock_value`'s docstring
  said "maker fee 0 on the rest leg" while the formula charges `fee(n)` on that (maker) leg.
  Rewrote the docstring to state that the taker `fee(n)` is kept deliberately to stay bit-identical
  to the pinned sim (conservative: realized lock is ~1.7c HIGHER, never overstated). See M-1.

---

## Findings NOT changed in code (design / executor-facing / spec) — ranked

### HIGH — must be resolved before Phase 3 arming

* **F-1 — a fill on a no-longer-tracked order id is silently dropped; retained-cancel-context is
  missing.** `core.py:_apply_fill` books a rest fill only when the fill's `client_order_id` matches
  the CURRENT `rest_live`/`rest_pending`. The normal `|dn|` replace keeps the old rest fillable
  until the new acks (requote2-faithful), and the bucket-change / stand-down paths eagerly set
  `rest_live=None` at cancel-request time. In all of these there is a window where our own order is
  live on the exchange but not in either slot. Failure scenario: during a replace the OLD order
  fills and the exchange reports it as a standalone `Fill` (not folded into `OrderCancelled`); the
  core drops it -> we hold an untracked, unhedged bucket-NO (directional, up to ~$0.45 at risk),
  the one-set-per-hour latch (`rest_fill`) never sets, and the core keeps quoting -> a second
  entry. This also defeats F-C on the eager-clear paths (no matched order -> falls back to
  desired_n). Recommended fix (Phase 2/3, needs the executor's semantics): the core retains a small
  `order_id/coid -> (price, count, bucket_Sd)` map of orders it has asked to cancel, and both
  `_apply_fill` and `_apply_cancelled` resolve against it, so any fill on any order we ever placed
  this hour is booked at the right price/bucket and latches the one-set rule. Mitigation that makes
  it survivable in the meantime: Phase 3 MUST drive cancels through a status-confirmed path so the
  fill always arrives as `OrderCancelled.filled_count_before_cancel` (handled), and MUST feed a
  `Fill` only for a currently-tracked order. This is the single most important item for real money.

* **F-2 — deferring the initial take on the lock floor leaves a naked directional leg.**
  `_wing_step` gates the FIRST both-wings take on `lock >= lock_floor` computed from both wing
  costs. If it fails (or never clears before T-1s), NO wings are taken and we hold the lone
  bucket-NO to settlement — a directional bet (loses n if BTC settles in-bucket), not the "$1
  floor" that only two-of-three legs provide. With `lock_floor=-0.10` and typical +10c locks this
  almost never fires, but adversarially it is a real exposure path and it reads against the spec's
  "a missed wing leg is bounded, not naked" framing (which is about a missed leg AFTER the take, not
  skipping the take entirely). Decision for Brad/ceremony: should the lock floor gate only the
  RETRY of a missing leg (always take both on first fill to bound the position), or is holding the
  lone leg when completion is uneconomic intended? Left as a design question, not a code change.

### MEDIUM

* **M-1 — shipped `deb_ms` disagrees with the registered plan.** `policy/v32_params.json` has
  `deb_ms: 2000`; PLAN_V32 "Requote policy" pins **"Chosen defaults: E = 0.10, TOL = 0.02, DEB =
  5000 ms"** (the 77-replaces/h row). The build report calls tol/deb_ms "provisional
  RESULTS_PLACEHOLDER," but the plan text is explicit at 5000. Per house law ("Registered specs
  rule"), the frozen policy should carry the pinned 5000 (or the plan must be amended). I did NOT
  change the frozen json + re-pin the sha myself: that is a tuning/ceremony act (Phase 4 freezes the
  falsifier and its `[pin]` values) and belongs to Brad. Flagging for reconciliation before any dry
  run that is meant to represent the chosen policy. (`tol=0.02` already matches.)

* **M-2 — lock understates realized edge by `fee(n)` (~1.7c at n=0.45).** The resting bucket-NO is
  a maker (post_only) leg; Kalshi crypto maker fee is 0 (MEMORY kalshi-fee-exact), so the true lock
  is `2 - n - W_paid`, i.e. ~`fee(n)` higher than the code's `2 - (n+fee(n)) - W_paid`. This is the
  conservative direction and matches the pinned sim (the golden +10.36c/+12.38c depend on it), so I
  kept it and only corrected the misleading comment (F-E). Surface it in the Phase-4 falsifier so
  the live-fill thresholds are read on the same (fee-charged) convention as the shadow, and so the
  ~1.7c/contract conservatism is a documented feature, not a silent discrepancy.

### LOW

* **L-1 — `solve_n` ceilings the cap.** `solve_n` starts at `min(ceil_cent(budget), ceil_cent(cap))`
  and the decrement loop only tests the budget, never the cap. Today `cap` is always whole cents
  (`_bucket_cap` quantizes `(1-yes_bid)-0.01`), so it is safe, but if a non-whole-cent cap is ever
  passed, `ceil_cent(cap)` rounds UP and the returned n could exceed the true cap (cross the book /
  post_only reject). Cheap hardening: floor the cap (`ROUND_FLOOR`) or add `n <= cap` to the loop
  guard. Not fixed (no live path violates it; a change would touch a golden-pinned primitive).

* **L-2 — out-of-order cross-market `server_ts` can spuriously cancel.** `_fresh` requires
  `0 <= age`, so a bucket `BookUpdate` whose `server_ts` is slightly behind the last strike ts
  makes the strike look "future"/stale -> W None -> cancel, then re-place on the next in-order
  frame = churn. The `age >= 0` guard is defensible (fail-closed), but Phase 2 should feed a
  monotonic-ish clock or the requote/replace-rate alarm could see extra cancels under normal
  cross-market reordering. Observation only.

* **L-3 — wing take accepts `no_ask == 1.0` but caps the limit at 0.99.** `_wing_prices` allows
  `na == 1` (deep ITM), then the IOC limit is `min(na + margin, 0.99) = 0.99 < ask` -> never fills
  -> retries to T-1 -> bounded one-legged. `_compute_W` rejects `na == 1` (strict `< 1`), so the
  two disagree on the boundary. Harmless (bounded), noted for consistency.

* **L-4 — partial fill of a multi-lot rest orphans the remainder.** With `contracts>1` (out of
  scope: size is pinned at 1), a Fill of 1-of-2 clears `rest_live` and takes wings for 1; the other
  lot keeps resting on the exchange, untracked. Fine at contracts=1; note if the size ever rises.

* **L-5 — dead variable.** `_requote` computes `in_window` (line ~666) and never uses it; the gate
  keys off `t_to_close < quote_end_s` / `> quote_start_s`. Cosmetic.

* **L-6 — no invariant that `E in shadow_Es`.** If a future policy sets `E` outside `shadow_Es`,
  the in-process shadow won't track the live E. Today `shadow_Es=[0.08,0.10,0.12]` contains
  `E=0.10`. A one-line assert in `load_v32_params` would fail-closed; Phase-4 falsifier expects it.

---

## Probe-by-probe result (task's 10 axes)

1. **Money math.** `solve_n` exact-Decimal, brute-force-pinned; cap never crosses (whole-cent caps;
   L-1 is the only latent path). Prices all Decimal; no float leaks into money. lock = pinned-sim
   convention (M-2). OK.
2. **Requote gate.** Never two *tracked* rests; in-flight hold correct; bucket-change is
   cancel-then-place-after-confirm; `|dn|>=tol && >=deb_ms` matches the sim; replace-rate alarm
   trips + cancels. `filled_count_before_cancel>0` -> TAKE_WINGS (now at the right price, F-C).
   Gap: the two-live window during a normal replace + dropped foreign-coid fills (F-1).
3. **Fill handling.** Partial (count 1) OK at size 1 (L-4 at >1); duplicate rest fill deduped by
   `rest_fill is None`; duplicate WING fill now idempotent (F-D); unknown coid dropped (correct for
   truly foreign, wrong for our own replaced order — F-1); fill after quote_end but before T-1 still
   completes (correct); IOC no-fill -> RETRY_WING at fresh ask; lock-floor defer (F-2 caveat); one
   set/hour latches on `rest_fill` and cancels the rest.
4. **Cutoffs/time.** Window `[quote_end_s, quote_start_s]` inclusive; places stop at T-5, wings run
   to T-1; close/after -> cancel + stop. ClockTick now re-evaluates freshness (F-B).
5. **Freshness.** Strike staleness -> CANCEL + no re-place; `server_ts`-only clock; suspect now
   honored for strikes (F-A) and already for buckets. L-2 caveat on out-of-order ts.
6. **Shakedown/dry.** WOULD_* twins carry identical payloads (`_mk` downgrades kind only); no
   order-emitting action bypasses `_mk`; shadow runs in all modes and emits no actions. OK.
7. **Shadow correctness.** No-lag state machine; documented it cannot reproduce the ideal's -1s
   n-solve (CONFESSION 1). Confirmed: shadow n is recomputed on BookUpdate only, so the stored n at
   a Trade is the last-book n — its value is sensitive to intra-tick ordering between a bucket print
   and the co-triggered strike deltas (the ~2c-of-E gap the E=0.12≈ideal-E=0.10 cross-check shows).
   The no-lag rule is the honest live-computable statistic; I concur with shipping (a) and leaving
   Brad's (b) question open. Not a defect.
8. **Params.** sha scheme byte-identical to box (pinned by test); default `expected_sha` self-
   verifies; missing key -> KeyError; mismatch -> `V32ParamsShaMismatch`; all 15 tunables present;
   `shadow_Es` contains `E`. Gap: `deb_ms` value vs plan (M-1); no `E in shadow_Es` assert (L-6).
9. **Tests.** Golden assertions pin exact Decimals (0.55/0.56/+10.36c lagging; 0.57/0.58/+12.38c
   ideal; core live fill n=0.45/+10.36c; shadow E10 & E12) — a numeric regression fails them. No
   test reads a holdout/seal date (fixture is 2026-09-04, asserted); no test touches the network.
   Added 4 regression tests (F-A..F-D).
10. **Legibility/fail-closed.** Mirrors `box.py`/`box_runner.py` idioms well; fail-closed
    throughout. Minor: L-5 dead var, F-E comment (fixed).

## Files
* Reviewed: `pilot/service/v32/{core,params,events,actions,__init__}.py`,
  `pilot/policy/v32_params.json`, `pilot/tests/test_v32_core.py`,
  `pilot/tests/test_v32_golden.py` (+ `pilot/tests/fixtures/v32/golden_20260904T200000Z.json`).
* Changed: `pilot/service/v32/core.py` (F-A,F-B,F-C,F-D,F-E), `pilot/tests/test_v32_core.py`
  (+4 tests).
