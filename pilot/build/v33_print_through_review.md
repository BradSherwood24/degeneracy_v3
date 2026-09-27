# V3.3 PRINT-THROUGH WINGS -- adversarial review (2026-09-26)

Reviewer: Opus 4.8 (delegated). Worktree `C:\Users\Brads\Python_stuff\dv3_wt_review`, detached at
`1d31d8b` (branch feat/print-through-wings), base `ba1694f`. Read-only on the live tree; no proxy; no
seal/holdout. This doc is written but NOT committed.

## VERDICT: BLOCK (for arming) -- OFF-path merge is safe; do NOT flip `print_through` true until F1+F2 fixed

The feature ships `print_through: false` and the OFF-path is byte-clean (proved below): merging it inert is
safe. But the ON-path has one HIGH-severity correctness bug in the headline safety branch (fail-closed
unwind opens a NAKED SHORT instead of going flat) and one MED-HIGH double-commit in the stall cancel-race
that violates the stated "cancel-first = never double-filled" guarantee. Both are real money when Brad
flips the master switch. Fix F1 and F2, add tests that assert the emitted legs (not just the action kind),
then this is armable-to-dry.

## Suite + OFF-path receipts

- `python -m pytest -q` from `pilot/`: **1282 passed, 5 skipped** in ~18s. The 5 skips are all
  corpus-absent / POSIX-only and unrelated to this feature:
  `test_box_golden.py:300,351` (historical-data absent), `test_quintile.py:74,94` (corpus absent),
  `test_supervisor.py:322` (POSIX SIGTERM). Builder reported 1286 passed / 1 skipped from an env with the
  historical-data corpus present; 1282+5 == 1286+1 == 1287 total -> consistent, no regressions.
- `tests/test_v33_print_through.py` + `tests/test_v33_hardening.py`: 42 passed.
- OFF-path proof (code + suite): with `print_through=false`, `_print_through_step` returns `(st, [])` on
  its first line and `_pt_stall_step` returns `(st, [])` (`not params.print_through`), so `st` identity is
  unchanged. The invariant change (`b.leg_count` vs `b.total_count`) reduces to `total_count` for any
  non-print_through batch (`leg_count` property returns `total_count` when `print_through` False). The
  `_maybe_close_set` refactor keeps the exact `sets_done += total_count` path for normal batches. The
  ledger taker-fee add is gated on `getattr(rf, "taker", False)` which defaults False for every maker rung
  fill -> existing rows unchanged. The whole pre-existing suite passing unchanged is the receipt.
- Params sha re-pin verified: `policy/v33_params.json` hashes to
  `2e60980762ea6531b707c1c0bc93d69577fd3257295238e63f122d53afdd995e` == `FROZEN_V33_PARAMS_SHA256`. All
  in-scope references to the old sha are correctly the `PREVIOUS_*` chain (params.py constant, falsifier
  "prior sha", hardening test PREV assertion). Only stray hit `build/v33_bucket_flap_fix_build_report.md:107`
  is a historical report (out of scope, pre-existing). PREVIOUS_V33_PARAMS_SHA256_FLAP_R2 added; chain
  intact. Loader fails closed on negative ticks/slack/stall and unknown policy string
  (`V33_PRINT_THROUGH_POLICIES`); `print_through_min_lock_c` intentionally allowed negative.

## Findings (ranked by severity)

### F1 [HIGH] Fail-closed unwind sells the NEVER-FILLED wing -> naked short (opposite of "go flat")
`service/v33/core.py::_pt_unwind` (lines ~536-543) builds sell legs for BOTH the YES and NO wing of the
batch without checking `lg.status == "filled"`, and sizes each to `shortfall`. In the STALL->unwind branch
both wings are filled, so this is correct. But `_pt_fail_closed` (the partial-wing safety path) calls
`_pt_unwind` precisely when ONE wing did not fill. It then emits a sell of the unfilled wing -- a leg we
never bought -- creating a naked short, compounding the one-legged risk the branch exists to close.

Verified empirically (the exact scenario in `test_partial_wing_fill_fails_closed`): YES wing fills 3, NO
wing fills 0. The emitted UNWIND_WINGS legs are:
```
UNWIND leg: T79599.99 yes sell count=3   (correct: we hold 3 YES -> flat)
UNWIND leg: T79699.99 no  sell count=3   (WRONG: we hold 0 NO -> naked short 3 NO)
```
`roundtrip_cost` (0.2358 here) is also overstated: `_batch_wpaid_held` (lines ~356-361) counts an unfilled
leg's `limit` as if paid. With `print_through_slack_c=0` (the shipped default), any 1-tick ask move between
decision and venue produces exactly this partial -> fail-closed path, so this is not a corner case; every
fail-closed would open a naked short. The existing test only asserts `[a for a in acts if a.kind ==
UNWIND_WINGS]` is non-empty, so it passes while the bug is live -- a genuine test blind spot (item 5).
Fix: in `_pt_unwind`, emit a sell only for legs with `status == "filled"`, sized to that leg's filled
count; compute `w_paid`/roundtrip from filled legs only.

### F2 [MED-HIGH] Stall-complete + racing fill-before-cancel double-commits (violates cancel-first guarantee)
`service/v33/core.py::_pt_resolve_stall` (lines ~458-515) cancels the unfilled rests, then books a taker
bucket-NO to `complete` the set and marks the trigger `resolved=True`. If a pre-hedged rung then fills in
the race between the cancel emission and its confirm, the fill arrives as an `OrderCancelled` with
`filled_count_before_cancel>0` and is booked via `cancel_ctx` in `_apply_cancel` -> `_book_rung_fill`.
Because the trigger is now `resolved`, `_pt_trigger_index_for_coid` returns None, so the fill routes to
`_coalesce_add` and, on the next coalesce flush, emits a SECOND `TAKE_WINGS`.

Verified empirically (stall->complete with a low floor, then `OrderCancelled(OID-rung, filled=1)`, then a
clock tick to flush): final state = `rest_fills=[(3, taker complete), (1, racing rung)]` (4 bucket-NO
lots), two wing batches (3 + a new pair being taken = 4 wings), `sets_done=3` while true exposure is 4
sets. So the venue taker-NO we bought to `complete` is redundant with the rung that actually filled: we
over-buy one full set (extra taker fee + extra wing pair) and `sets_done` under-counts the real position.
This directly contradicts the build report's "cancel-first is the guard against double-fill" -- cancel-first
prevents a duplicate PLACE, not this complete-vs-late-fill double. `_apply_fill` (WS `fill` channel) is
safe because the dropped ladder order yields None; only the `OrderCancelled`+cancel_ctx path double-books.
Fix: when a fill books for a coid whose print-through trigger already `resolved` as `complete`, reconcile
against the completed shortfall (e.g. drop/offset the redundant taker leg or suppress the second take)
rather than coalescing a fresh batch; or have the complete branch reserve the coids so a late fill collapses
into the completed set.

### F3 [MEDIUM] `complete` books the taker set BEFORE the IOC confirms (optimistic fill)
`_pt_resolve_stall` books a `RungFill(taker=True)` at the current `no_ask` and calls `_maybe_close_set`
(counting the set) in the same tick; the executor `_take_bucket_no` sends the IOC and, on a fill shortfall,
only `_record_alarm(...)` -- it feeds nothing back to the core. So a missed/partial marketable IOC leaves
the core P&L and `sets_done` overstated with only an operator alarm as the backstop. The honest taker FEE
is applied, but the FILL is assumed. IOC-at-ask is marketable and this is armed-only + rare, but it is an
optimistic-fill deviation from house convention; at minimum the report/falsifier should surface
`pt_bucket_no_takes` vs `pt_bucket_no_fills` so a chronic shortfall is visible. (Same shape for `unwind`:
F4.)

### F4 [MEDIUM] `unwind` drops the batch flat before the sell IOC confirms; a partial unwind is silently naked
`_pt_unwind` removes the batch + legs from state and records `roundtrip_cost` assuming the sell fills at the
bid; the executor `_unwind_wings` sends the IOC best-effort and alarms only on an unrouted leg (not on a
partial fill). A partial unwind leaves real wings held while core state believes it is flat, and (unlike
`_pt_fail_closed`) the stall->unwind branch does NOT stand the window down -- the ladder keeps trading with
an unaccounted position. Consider: reconcile the unwind fill count, and stand down (or re-attempt) on an
unwind shortfall.

### F5 [LOW] No upper-distance bound on a qualifying print
`_print_through_step` (line ~396) qualifies every live rung with `yes_print + ticks*1c + eps >= (1 - o.price)`
-- i.e. any rung whose offer is at OR below the print. A single high YES print (or an erroneous/one-off
print far above the ladder) pre-hedges EVERY cheaper rung at once, not just the ones a real sweep is
reaching, exposing all of them to the stall/unwind round-trip if they do not fill. In a true sweep this is
the intent; the risk is a lone deep print. Consider bounding how far above the offer a print may be, or
requiring the print size to be consistent with a sweep.

### F6 [LOW] Pre-emptive trigger ignores the window/settle cutoff
`_print_through_step` gates on `rest_allotment_done / stood_down / pt_stood_down` but not on `_in_window` or
`no_orders_after_s_to_settle`. A bucket print arriving after quote-end (or in the final second) can still
fire an early wing take for a rung that then cannot fill before settle -> forced unwind. Wing takes are
legitimately post-window (they hedge real fills), but a PRE-emptive take on an unfilled rung near settle is
different. Consider gating the trigger to the quote window.

### F7 [LOW] `lots_per_rung > 1`: a partially-filled rung can be re-hedged by a second trigger
Once a rung's coid enters `filled_coids` (first partial), `_pt_covered_coids` no longer covers its resting
remainder, so a later qualifying print could open a SECOND trigger pre-hedging the same coid. Inert at the
pinned `lots_per_rung=1`; flag if that ever changes.

### F8 [NIT] Pacing of a multi-print sweep may defer the early take and defeat the point
The pre-emptive `TAKE_WINGS` reuses the inherited (non-priority) wing path; a fast multi-print sweep fires
several bursts in ~50 ms. If the write-token bucket throttles them, the "wings in hand at the pre-jump ask"
premise erodes and a delayed take is more likely to partial -> fail-closed (F1). Tuning/observation item,
not a correctness bug.

## Item-by-item answers to the brief

- OFF-path (1): identical; proved by code reduction + unchanged baseline suite. Ledger taker branch and
  new row key are inert for existing rows. Goldens (`tests/test_v33_*`) pass unchanged.
- Qualifying print (2a): only `taker_side=="yes"` on the ladder's bucket ticker qualifies; a NO-side taker
  and an away-from-offer print correctly do not trigger (tests confirm). A print ABOVE the offer DOES
  trigger (intended for a sweep; see F5 for the lone-print risk). Re-entrancy: a covered coid is excluded
  via `_pt_covered_coids`; a filled+dropped rung leaves the ladder so it cannot re-trigger; a second rung
  gets its own trigger (invariant asserts one active trigger per coid). Chunking to `wing_cap` and pacing
  are honored in the executor.
- Attribution (2b): fill-after-trigger attaches once (no coalesce, no second take) -- confirmed. Fill on a
  non-pre-hedged rung routes normally. Set closes only when both wings filled AND all pre-hedged rungs
  filled. The problem case is the RESOLVED-trigger late fill (F2).
- Stall (2c): cancel-first ordering is present, but the complete-vs-late-fill race double-commits (F2).
- Unwind (2d): side/action mapping is correct for a genuine flat (sell YES=side yes/action sell, sell
  NO=side no/action sell); the bug is selling an UNFILLED leg in fail-closed (F1). Unwind partial-fill has
  no core stand-down (F4).
- Fail-closed partial (2e): cancels rests, unwinds, stands down -- but unwinds the wrong (unfilled) leg
  (F1); a cancel that 404s because the rung just filled routes to the same late-fill path as F2 while the
  window is already stood down, leaving a naked bucket-NO (subset of F2/F4).
- Dry mode (3): the driver's `_simulate_ladder_fills` + FrozenExecutor path simulates the trigger; stall
  actions become WOULD_* twins via `_mk`/`twin_kind`; the report's lock uses `lock_at_trigger` (trigger-time
  asks), not fill-time -- correct.
- Params/sha (4): fails closed; re-pin consistent; PREVIOUS_* chain intact (see receipts).
- Tests (5): honest for the happy paths, but the fail-closed test asserts only the action KIND, not the
  legs, hiding F1; no test exercises the F2 cancel-race. Add leg-level assertions and a race test.
- Scope (7): only the listed files touched; no `service/v32/*`, no `record_window.py`, no `run_v32.py`;
  added lines are ASCII.

## Follow-ups to queue
1. Fix F1 (fail-closed unwind: filled legs only) + test asserting emitted legs/counts. [before arming]
2. Fix F2 (complete/late-fill reconciliation) + a cancel-race test. [before arming]
3. Surface `pt_bucket_no_takes` vs `pt_bucket_no_fills` and unwind fill reconciliation in the report/falsifier;
   decide stand-down-on-unwind-shortfall (F3/F4).
4. Decide F5 upper-bound and F6 window gate as tuning levers before a dry watch.
5. Brad's open questions stand (ship on/off, min_lock floor, slack, V3.2 registration).

# ROUND 2 review (commit 40956cf, git diff 1d31d8b..40956cf)

Reviewer: Opus 4.8. Same rules; review worktree only; no commits/push/PR; live tree read-only. All claims
below were repro'd against 40956cf (core-level event feeds + the two live tapes, read-only).

## VERDICT: APPROVE WITH NITS -- all round-1 correctness bugs are fixed and verified. Ships OFF, safe to
merge. Before ARMING, Brad must know the shipped default (print_through_ticks 1) is close to INERT on the
two real fast-sweep tapes (N1 below): neither the 10:00Z nor the 17:00Z loss rung would be pre-hedged. That
is a tuning/expectations matter, not a correctness defect. Do a dry watch and consider ticks >= 2.

## Round-1 fixes -- verified fixed
- F1 (naked short) FIXED. _pt_unwind now sells only status=="filled" legs sized to held count;
  _batch_wpaid_held counts filled legs only. Repro (YES wing fills, NO wing 0): the sole UNWIND leg is
  "yes sell count=1" -- no NO-side short; pt_one_legged latched, stood down, resolution "partial".
- F2 (stall double-commit) FIXED via the two-phase stall. _pt_begin_stall cancels the rests and sets
  stall_pending; finalise runs from _pt_on_cancel_confirmed in _apply_cancelled AFTER the racing fill is
  booked and attributed to the SAME batch. Repro (11 pre-hedged, floor -100):
  * 1 of 11 races the cancel -> complete size = 10 (true shortfall), NO second TAKE_WINGS, one batch.
  * all 11 race -> shortfall<=0 -> NO TAKE_BUCKET_NO, resolution "filled", sets_done=11, one batch.
  * 0 race -> complete 11. The round-1 over-buy is gone.
- F3 (optimistic complete) FIXED for armed. _pt_complete mints complete_coid and books from the IOC
  response (_pt_apply_complete_fill); DRY still books optimistically (see N2). Repro: complete Fill(11)
  closes the set (sets_done 11); a DUPLICATE Fill on the same complete_coid is a no-op (complete_coid nulled
  after first apply) -- no double-book; a SHORTFALL Fill(4 of 11) sells back the 7 un-hedged wings of BOTH
  legs and stands down (print_through_complete_short), taken_count shrunk to 4. Taker fee is applied to the
  taker RungFill (ledger taker branch); maker rungs still fee 0.
- F4 (unwind partial / no stand-down) FIXED. _unwind_wings reconciles sold vs want; a shortfall bumps
  pt_unwind_shortfalls, journals print_through_unwind_short, and sets executor stand_down_reason (propagated
  by the driver at run_v33.py:408). Core _pt_unwind sells filled legs only.
- F5 (over-trigger) FIXED to a strict leading edge: (offer - ticks*1c) - eps <= yes_print <= (offer - 1c)
  + eps. Empirically (offers 0.50..0.60): at ticks=1 a print pre-hedges exactly the rung one tick ABOVE it
  (print 0.51 -> offer 0.52); the at-offer/crossing print is excluded (print 0.60 with no 0.61 offer -> no
  fire). See N1 for the real-tape consequence.
- F6 FIXED: _print_through_step requires _in_window and candidates must be live and not pending and
  order_id is not None (acked, resting).
- F7 FIXED: _pt_covered_coids / _pt_trigger_index_for_coid now cover EVERY rung_coid of an unresolved
  trigger, so a partially-filled rung's remainder cannot be re-hedged and repeated partials attach to the
  same batch. (Nit N4: filled_coids may then carry a duplicated coid -> the per-trigger summary "filled"
  count can overstate for lots_per_rung>1; report-only, pinned lots_per_rung=1.)
- F8 (pacing): the pre-emptive take routes through the inherited _take_wings with priority tokens.
- Mirror latch: _sync_wing_mirrors now ORs pt_one_legged so a fail-closed/complete-short that DROPS the
  batch still reports one_legged -- a real round-1 gap, now closed.

## New-code adversarial checks (coordinator items 1-4)
1. Two-phase stall / cancel-never-confirms: every cancel-resolution path in the inherited executor returns
   OrderCancelled(order_id=<real oid>) -- 2xx (_finish_cancel), 404/expired status-truth
   (_resolve_cancel_from_status), and even the cancel_failed giveup (v32/executor.py:786) return the real
   oid with filled=0 AND set stand_down_reason. So _pt_on_cancel_confirmed (matches by order_id) always gets
   its confirm; stall_pending does not hang in the executor-driven flow. The only OrderCancelled(order_id=
   None) returns are place-path guards / pending-create cancels, none of which apply to an acked stall
   cancel (F6 requires order_id). DRY: FrozenExecutor's WOULD_CANCEL_REST also returns
   OrderCancelled(order_id=oid), so the two-phase stall finalises in dry too (the slow-sweep dry golden
   confirms unwind after one tick). RESIDUAL (N3): there is no core-side stall_pending TIMEOUT; the only
   backstop if a confirm were truly lost is the _wing_step cutoff, which marks the batch one_legged+resolved
   "partial" but does NOT unwind the (both-filled) held wings nor stand down -- they ride to settle as an
   un-unwound box (valued, not naked, and one_legged is counted). Low, given the confirm guarantee above; a
   defence-in-depth stall_pending timeout would remove the last hang path. fill-before-cancel exceeding the
   pre-hedged count is venue-bounded (rung size = lots_per_rung); the _maybe_close_set gate is
   total_count < taken_count, so an impossible over-fill would under-count sets, never naked-over-hedge.
2. _pt_apply_complete_fill: keyed on complete_coid, nulled after first application -> a retry/duplicate poll
   Fill is a no-op (verified). No coid collision (unique _mint_coid namespace). CAVEAT (N5): it assumes ONE
   aggregate Fill per complete (the executor returns exactly that). If the venue/poll ever delivered the
   complete as TWO Fills on one coid, the first (partial) would null the coid, stand down, and unwind -- and
   the second would be dropped. That is safe (stands down), but would under-book the later lots; fine as
   long as _take_bucket_no keeps returning a single aggregate Fill.
3. F5 real tapes (READ-ONLY journals_v32): see N1. Same-event-batch fill+print: events are decided one at a
   time; either order is safe -- print-first pre-hedges then the fill attributes; fill-first drops the rung
   from the ladder so the later print finds no candidate (normal coalesce take already ran). No double.
4. DRY vs ARMED (N2): DRY books complete optimistically (full shortfall at no_ask, always a "successful"
   set) and unwind assuming the bids fully fill; ARMED books from the venue and, on a complete- or
   unwind-shortfall, unwinds + STANDS DOWN. So the dry scoreboard is systematically ROSIER than armed for
   both stall branches, and dry cannot surface complete-short / unwind-short stand-downs (the report's
   complete_fills / attempted and unwind_shortfalls are 0 in dry -- no venue). Read a clean dry run as "the
   trigger logic fires and attributes correctly," NOT as "the stall economics will hold when armed."

## OFF-path (item 5): still byte-identical
With print_through=false, st.print_through is always empty, so the two newly-unconditional calls in the hot
path are true no-ops: _pt_apply_complete_fill (top of _apply_fill) returns None on an empty tuple;
_pt_on_cancel_confirmed (in _apply_cancelled) returns early on order_id is None or not st.print_through.
_sync_wing_mirrors ORs pt_one_legged (always False off). _pt_covered_coids in _converge is empty. The whole
baseline suite passes unchanged.

## Suite (item 6)
python -m pytest -q from pilot/: 1288 passed, 5 skipped (~23s). Print-through file: 28 passed. Builder
reported 1292 passed / 1 skipped; 1288+5 == 1292+1 == 1293 total -- the 4 delta are the same corpus-absent /
POSIX-only skips (box_golden x2, quintile x2, supervisor) present in this checkout. Green.

## Findings ranked
- N1 [MEDIUM -- effectiveness/expectations, not a bug]. The shipped default print_through_ticks 1 requires a
  bucket YES print at EXACTLY offer-1c to pre-hedge a rung. On the real tapes the coordinator cited:
  * 10:00Z (bucket B84050, filled offer 0.72): YES-taker prints ran 0.57,0.58,0.59,0.61 then the crossing
    0.72 (the fill), then 0.78. No print at 0.71 (nor 0.70). At ticks=1/2/3 the 0.72 rung is NOT pre-hedged
    -- the sweep jumped 0.61 -> 0.72 (11c) in one step. The shipped default would NOT have helped that loss.
  * 17:00Z (bucket B84150, filled offer 0.79): the whole sweep landed in ONE same-millisecond burst
    (0.67,0.71,0.72,0.73,0.74,0.77,0.79,0.80,0.83,0.85,0.87,0.89), skipping 0.78 (0.77 then 0.79). At
    ticks=1 the 0.79 rung is NOT pre-hedged (no 0.78 print; the 0.79 crossing print is excluded by the upper
    bound). At ticks=2 the 0.77 print would pre-hedge offers 0.78+0.79 -> the 0.79 rung IS caught.
  Net: at the shipped default the feature fires on neither tape's loss rung; only 17:00Z is catchable, and
  only at ticks>=2. Brad should size print_through_ticks from a dry census of "how far below the offer the
  last pre-cross print lands" before expecting the build report's early-hedge wins. Not a correctness issue
  -- when it does not fire, the ladder falls back to the existing coalesce wing take (status quo).
- N2 [MEDIUM] DRY optimistic booking of stall complete/unwind flatters the dry scoreboard vs armed (item 4
  above). Consider booking DRY completes/unwinds against a simulated fill rate, or clearly labelling the dry
  complete/unwind numbers as optimistic in the report.
- N3 [LOW] No core-side stall_pending timeout; the _wing_step cutoff backstop marks "partial" without
  unwinding the held wings. Mitigated by the executor's guaranteed OrderCancelled(oid). A timeout that
  unwinds on a lost confirm would close the last hang path.
- N4 [LOW] filled_coids can carry a duplicated coid after repeated partials (lots_per_rung>1) -> the
  per-trigger "filled" summary count overstates; report-only, inert at pinned lots_per_rung=1.
- N5 [LOW] _pt_apply_complete_fill assumes a single aggregate complete Fill; safe (stands down) but
  under-books if the venue ever splits it. Keep _take_bucket_no returning one aggregate Fill.

## Follow-ups to queue
1. Dry census of pre-cross print distance below the offer -> pick print_through_ticks (likely >= 2); only
   then does a dry watch of the trigger economics mean anything (N1).
2. Label or simulate the dry complete/unwind economics so the dry scoreboard is not read as armed truth (N2).
3. Optional: a stall_pending timeout that unwinds on a lost cancel confirm (N3).
