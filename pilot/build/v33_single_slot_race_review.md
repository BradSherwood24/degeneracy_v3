# V3.3 single-slot cancel/re-place race — review

**VERDICT: APPROVE WITH NITS** (no defects; no code changes made on the review branch).

Reviewer: Opus 4.8. Date 2026-10-04. PR #135 `fix/v33-single-slot-race`, head `ef93e02`, base `main c31cdb0`.
Worktree: `C:\Users\Brads\Python_stuff\dv3_wt_review` (detached ef93e02 → branch `review/v33-single-slot-race`).

## Suite counts (run from `pilot/`, this worktree)
- `tests/test_v33_single_slot_race.py` alone: **6 passed** (23:19Z).
- Full suite `python -m pytest -q`: **1563 passed, 5 skipped, 0 failed** in 39.7 s (23:20Z).
  - Higher than the stated worktree baseline of 1510/10 because this checkout HAS the census/box/quintile
    corpus present (5 skips = 1 POSIX-only SIGTERM test + 4 data/corpus). 1563 = 1557 base + 6 new.
    Not a regression. (Nit N5.)

## Review questions

### 1. Correctness of (b): arm on every venue-id CANCEL, clear on every resolution, wedge bound
**PASS.** Every `CANCEL_REST` is emitted through `_cancel_action` (only def at core.py:2368; call sites
core.py:2040, 2419, 2746 — grep-verified, no raw `ActionKind.CANCEL_REST` elsewhere). All three sites arm:
- `_pt_cancel_unfilled_rungs` core.py:2042 (print-through stall),
- `_cancel_all` core.py:2422 (bucket change / stand-down / quote-end / allotment-done; `_cancel_all_then_replace`
  routes through it; guarded by `o.order_id is not None`),
- `_converge` shrink-cancel core.py:2745 (the single-slot-race site).
`_arm_cancel_pending` (core.py:2386) is a no-op when `order.order_id is None`, matching intent (nothing rests
at the venue for a pending-order cancel).

Clear paths: `_clear_cancel_pending` runs in `_apply_cancelled` core.py:1130 (keyed `ev_oid`) and `_apply_fill`
core.py:1244 (keyed `event.order_id`), both guarded for `None`. Every executor resolution of a DELETE emits
`OrderCancelled(order_id=oid)` — success `_finish_cancel` (v32/executor.py:927), status-truth
`_resolve_cancel_from_status`→`_finish_cancel`, 404→status fallback (async_executor.py:642-646,688-695), and
even the terminal `cancel_failed` path (async_executor.py:706). So a held price always clears.

**No wedge found.** An armed order carries a non-None `order_id`, so in `_cancel_rest_async`
(async_executor.py:600) `oid = action.order_id` is non-None → it never takes the `cancel_noop` (line 614,
`order_id=None`) nor `cancel_deferred_unacked` (line 607, requires `oid is None`) branches, both of which would
return a None-id event that cannot clear the map. The `cancel_dup_inflight` short-circuit
(async_executor.py:630, returns `[]`) is safe: the other in-flight DELETE for that same oid emits the single
`OrderCancelled(oid)` that clears the price. Bound is honest and real: a never-confirming cancel blocks ONLY its
own price (keyed by order_id) and ONLY to quote end — the quote-end cancel-all and stand-down do not consult
`cancel_pending_px`. State is discarded at window end.

### 2. Interaction with gate D / A / B / C
**PASS, no double-count.** `cancel_pending_px` is additive and independent of `outstanding_cancels`
(the shrink site increments `outstanding_cancels` at core.py:2747 and arms the map at 2745; the confirm clears
the map at 1130 and decrements `outstanding_cancels` at 1224-1226 — separate, no interference) and of
`awaiting_replace`. Gate A (orphan fill hedging when stood down): `_apply_fill` clears the held price at the top
(core.py:1244) and does NOT alter fill booking or the wing take — clearing only drops a price-hold. Gate B:
clears are keyed by `order_id`, never by coid, so the `None==None` coid hazard cannot occur. Gate C sweep: a real
foreign order (no DELETE of ours in flight) is still a stray (part (a) excludes only `_cancel_oids_inflight`).

### 3. Correctness of (a): `_exclude_cancelling`
**PASS.** `_cancel_oids_inflight` is armed in `_delete_and_resolve_async` (async_executor.py:633) and discarded
in its `finally` (648) — covers `_cancel_rest_async`, `cancel_after_ack`, amend-fallback, and the standdown
sweep. `_exclude_cancelling` (async_executor.py:475) drops entries whose `order_id ∈ _cancel_oids_inflight`
before `_invariant_verdict`, so they leave `count_resting`, the `dup_price` test, AND `strays` together. Applied
AFTER `_filter_phantoms` on BOTH reads (first read async_executor.py:526, backoff re-read 541) — ordering
correct (a cancelling order is not yet cancel-confirmed, so the phantom belt must keep it). A real stray is
untouched (regression test confirms the trip). The confessed residual sliver (arm happens after the cancel-lane
pacer `acquire_async` in `_cancel_rest_async:601`) is real but sub-ms (priority lane) and is exactly what part
(b) closes in the core — belt + gate together.

### 4. Roll fallback (confession 3)
**No defect; question/nit.** Rolls emit `AMEND_REST`, never `CANCEL_REST`, so they never arm the map. Roll
targets are drawn only from `VACANT` (core.py:2734), and `VACANT` now excludes held prices (core.py:2694), so no
roll can target a held price. The amend→cancel→create fallback (`_apply_cancelled`, rp branch, core.py:1212
`_place_one`) re-places at `rp.target_price`, a price chosen from VACANT at emit time; for it to equal a held
price, an order would have had to rest at a price that was simultaneously VACANT (empty) at roll-emit — a
contradiction. If it ever did coincide, part (a)'s invariant exclusion is the backstop. The confession is
honest; leaving the fallback un-gated does not widen scope. No same-price re-place arises from amend→cancel
(amend changes price to a different target).

### 5. DRY / byte-identity
**PASS.** `cancel_pending_px` appears nowhere outside core.py (grep-verified): not serialised to any ledger,
report, or journal row — no schema break. With nothing pending, `_held_cancel_prices` returns `∅`, so the
`_place_all` (core.py:2509) and `_converge` (2693) filters are no-ops and `_clear_cancel_pending` is a no-op —
decisions byte-identical to base. Goldens pass unchanged (full suite green).

### 6. Tests
**PASS, pin the behaviour.** The 6 tests would fail on `main c31cdb0`: part (b) tests import
`_arm_cancel_pending/_clear_cancel_pending/_held_cancel_prices` and `cancel_pending_px` (absent on main →
collection error); the golden additionally asserts no `PLACE_REST` at the held price (main re-places → assert
fails). Part (a) `test_inflight_cancel_excluded...` asserts `stand_down_reason is None` and
`rest_invariant_cancelling_excluded >= 1` (main trips → fails). `test_real_stray...` is a correct regression
guard. Coverage gaps (nits, not blockers): the backoff RE-READ exclusion branch (async_executor.py:541) is not
directly exercised (code is symmetric with the tested first read); the wedge bound (release at quote-end) is not
pinned by a test; two orders armed at the same price concurrently (clearing one leaves the price held by the
other) is correct-by-construction (`values()` keyed by distinct oids) but untested.

### 7. Replay
No `sim/v33_replay` harness exists; `pilot/service/replay.py` is the daily-replay tool. The 02:00Z journal SHAPE
is reproduced in-core by `test_single_slot_race_holds_replace_until_cancel_confirms`, which satisfies the intent.
I did not run a full journal replay (heavy; near the armed window) — not verified by that route.

## Nits (no code change requested)
- **N1** Backoff re-read exclusion branch untested (symmetric with the tested first read).
- **N2** Wedge bound (release only at quote-end) documented but not pinned by a test.
- **N3** Concurrent arm of two orders at the same price untested (behaviour is correct).
- **N4** Roll-fallback re-place bypasses the held filter (confession 3); unreachable in practice, backstopped by
  part (a). Could add an assertion/guard later if roll-target invariants ever change.
- **N5** Measured suite 1563/5 ≠ stated 1510/10 (this checkout has the corpus present); not a regression.

## Could not verify
- Live proxy / WS / venue behaviour (house law — fakes only).
- Full journal replay through `service/replay.py` (not run near the armed window).
