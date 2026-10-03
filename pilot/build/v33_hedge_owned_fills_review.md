# Review -- PR #119 `fix/v33-hedge-owned-fills` (re-arm gates A/B/C/G)

*Opus 4.8 reviewer, 2026-10-03. Branch `review/v33-hedge-owned-fills` at PR head 4c2394a. Adversarial review of
the fix for the 2026-10-03 02:00Z naked fill. I did not write this code.*

## Verdict: APPROVE WITH NITS

The three defects named in the brief are closed and the money path is sound and well-tested. The golden fixture
is faithful to the journal (verified below), 13/14 core tests and the executor tests fail on the pre-fix core,
and the full suite is green. I fixed one real DEFECT myself (an unguarded `None` price on the new orphan ingest
path would raise inside decide -- brief lesson 3). Everything else is a NIT or a correctly-scoped residual.

## What I verified (evidence)

**Fixture provenance (task item 4).** Decoded the real journal `20261003T020000Z.jsonl.gz` (510,672 records):
- 9 `rest_invariant_violation` rejections, coids 31,25,33,24,28,30,29,26,32 at the exact dts in the fixture;
  `#31` first at dt -235.053.
- `executor_standdown` alarm at dt -235.053 = the first rejection (the driver stand-down lands BEFORE the
  rejections are decided -- the fixture/golden model is faithful).
- The pre-fix core's cancel burst at -235.053 is EXACTLY `#24..#33` (10 coids, **missing #23**) -- matches
  `precfix_core_cancel_list`.
- The three ws `rest_fill`s: `#23` 0.40 @0.22, `#23` 0.60 @0.22, `#27` 1 @0.18 -- match the fixture.
- `window_meta` + `async_writer_enabled{"from_mode":"armed"}`: **the live armed roster runs the ASYNC executor**
  (relevant to residual R1 below).

**A, the money path (task item 1).** Traced ws fill -> `run_v33.on_fill` (pumps `V33Fill(source="ws",
market_ticker=market)`) -> `_apply_fill` (no ladder match) -> `_apply_orphan_fill` -> `_book_orphan_fill` ->
`_book_rung_fill` (fill_Sd from the fill's own ticker, D2, **not** `spot_Sd`) -> `_coalesce_add` -> `_wing_step`
(does NOT read `stood_down`) -> `TAKE_WINGS` -> async dispatch -> `_take_wings_async` (answered while stood
down). The dry-sim and poll dedupe hold in both arrival orders: `on_poll_fill` pre-subtracts `rest_booked_by_coid`
(driver), `source="poll"` books the pre-delta'd count as-is and never touches `fill_seen_by_coid`, and every
other source accumulates increments in `fill_seen_by_coid` and books `min(count, seen-booked)`; both orphan entry
points (fill and cancel-confirm) anchor on `rest_booked_by_coid`, so a cancel-confirm + late ws echo of the same
lots never double-books (tests `test_orphan_ws_echo_after_cancel_confirm...`, `..._poll_catchup_then_ws_echo...`).
The driver end-to-end test `test_driver_executor_standdown_sweeps_and_a_ws_fill_while_stood_down_is_hedged`
exercises the real sweep scheduling and confirms wings are actually POSTed (1.40 lots/side).

**B, cancel identity (task item 2).** `_apply_cancelled` attributes by `order_id`, else `client_order_id`, else
matches nothing (no `None == None`), and alarms `cancel_unattributed`. Every executor path now stamps the coid on
rejects/no-ops/cancel_failed/confirms (grepped: v32 sync, v33 sync, v33 async, FrozenExecutor). `outstanding_cancels`:
every producer increments by 1 ONLY when `o.order_id is not None` and records it in `cancel_ctx`; the new
decrement fires ONLY for `ev_oid is not None and (live_order or ev_oid in cancel_ctx)`. A counted cancel always
yields an oid-carrying `OrderCancelled` (both `_resolve_cancel_success_async` and `_cancel_nonok_async`), and the
`_cancel_oids_inflight` dedup guarantees exactly one such event per oid -- so it cannot go negative (guarded `> 0`)
and cannot get stuck positive. **Re the #120 concern** ("untracked `OrderCancelled` releases a re-place early"):
#119's "only a counted cancel decrements" rule DOES close it here -- an id-less reject, a late-ack cancel of a
never-counted pending rung, or an oid not in `cancel_ctx` never decrements (tests
`test_pending_rejection_never_decrements...`, `test_ack_path_cancel_of_an_uncounted_create...`). #120 widening the
set of tracked cancel-alls is a separate surface; it must preserve this exact invariant. **V3.2 byte-identical:**
v32 `_apply_cancelled` reads only `event.order_id`/`filled_count_before_cancel` (confirmed by read), never the new
`client_order_id`/`price`/`market_ticker`; v32's single-slot `None == None` match is correct for its one pending
rest and unaffected. v32 tests untouched and green.

**C, ownership (task item 3).** `standdown_sweep_async` is one-shot (`_standdown_swept`) and the driver schedules
it once (`_standdown_sweep_scheduled`, set before `create_task`, single loop thread -> no double-schedule).
`_delete_and_resolve_async` dedups by `_cancel_oids_inflight`, so the sweep racing a core `CANCEL_REST` is one
DELETE / one event (`test_core_cancel_racing_the_sweep...`). In-flight creates are registered at the TOP of
`_place_rest_async`/`_place_one_chunk_async` (before pacer + pre-flight), cleared in `finally`; a create is in
exactly one of {in-flight->deferred, rest_book-live->target} at any await point, so the sweep never double-covers
one order. The builder's deviation (a cancel during the pre-flight SKIPS the POST via `_skip_before_post` rather
than post-then-cancel) emits `OrderCancelled(order_id=None, client_order_id=coid)` -> the core drops exactly that
pending slot (B) and, being uncounted, does not touch `outstanding_cancels`
(`test_single_cancel_during_preflight...`). A refused amend becomes a `CANCEL_REST` carrying the ORIGINAL coid ->
the core's roll-fallback matches `rp.old_coid` and, stood down (`not st.stood_down` at core.py:1064), does NOT
re-place (`test_stood_down_roll_fallback_does_not_replace`). The stood-down executor answers
TAKE_WINGS/RETRY_WING/cancels and refuses only PLACE/AMEND (dispatch ordering in `on_action_async` checks the
refusal before the handler).

**Golden fixture honesty (task item 4).** Swapped in `origin/main:pilot/service/v33/core.py`: 13 of 14 tests in
`test_v33_naked_fill_core.py` FAIL on the pre-fix core (only the pure-fixture provenance test passes). Restored
(md5 verified identical to the branch file).

**Anything unmentioned (task item 6).** `ALARM`/`WING_NETTED` are excluded from BOTH dispatch paths (`places` is
PLACE_REST-only; async `order_actions` is `_ORDER_ACTION_KINDS`-only; sync `_pump` skips `_INFO_ACTION_KINDS`) --
no ALARM can reach an executor. Decimal fractional counts (0.40/0.60) flow correctly through `_q_count`. The new
sweep/alarm async paths are guarded (`_standdown_sweep_task`, `_dispatch_async`, `_ingest_async` all catch).

## Findings

### F1 -- DEFECT (FIXED by me) -- unpriced owned orphan fill raises on the decide ingest path
`pilot/service/v33/core.py` `_apply_orphan_fill` (~line 1184, pre-fix). The function passed `event.price` to
`_book_orphan_fill`, which does `_rung_of(st.n_top, price)`; a `None` price raises `TypeError` (confirmed:
`_rung_of(Decimal('0.22'), None)` -> `unsupported operand type(s) for -: 'decimal.Decimal' and 'NoneType'`).
`decide_v33` is called without a try in `_pump_async`, so an unpriced fill would propagate up the loop-side ingest
path -- exactly brief lesson 3 ("an exception anywhere on the ingest path is a dropped event elsewhere"). The
SIBLING cancel-confirm orphan branch in `_apply_cancelled` already guards this (`orphan_rung_fill_unpriced`); the
fill branch did not. Owned fills are always priced live (ws/poll = `rec.price`, POST = `action.price`), so this is
fail-closed defence, not a normal path -- but the asymmetry is a real hole on a money PR.
**Fix:** added the symmetric guard BEFORE `fill_seen_by_coid` is touched (so a dropped fill never skews the
dedupe), emitting `orphan_rung_fill_unpriced`. Test `test_orphan_fill_with_no_price_is_alarmed_unpriced_not_crashed`.

### F2 -- NIT (not fixed, by design) -- `orphan_fill_not_bucket` drops an owned fill without fail-closed stand-down
`pilot/service/v33/core.py:1186-1189`. The previous reviewer's lead. When an owned fill's `market_ticker` names a
non-bucket market AND there is no `cancel_ctx`, the fill is alarmed and DROPPED (not booked, not stood down),
whereas a fill with NO ticker falls through to `_book_rung_fill`'s fail-closed stand-down. I investigated and
concluded this is **defensible, not a defect**: (a) `st.bucket_tickers` accumulates every subscribed bucket's
ticker for the whole window (v32 `_fold_book`, never reset), and a rung is placed on a bucket already folded, so a
REAL owned bucket-NO rung fill always resolves -- this branch is near-unreachable for a genuine rung; (b) the one
plausible way to reach it is a stray STRIKE/wing fill, for which booking as a bucket-NO rung and standing the hour
down would be WRONG. Dropping + alarming is the safer response under that ambiguity. Gate E (reconciliation vs the
venue's `/portfolio/fills`) is the correct home for catching any truly-owned-but-unhedged fill. Leaving as-is.

### F3 -- NIT (residual, acceptable) -- sync `V33LiveExecutor` has B but not C (no sweep / in-flight guard / refuse)
`build_executor_v33` constructs the sync `V33LiveExecutor` by default and the async `V33AsyncExecutor` only when
`--async-writer`/env is set. Gate C lives only on the async executor. **This is acceptable for re-arm:** the
incident window's `async_writer_enabled{from_mode:"armed"}` confirms the LIVE armed roster runs the async executor,
and gate A (core) hedges any ws-surfaced owned fill on EITHER executor, so the money defect is closed regardless.
The sync path is a latent gap only for a non-standard armed run with the async writer OFF. Builder flagged it.

### F4 -- NIT -- `ALARM` action carries a `Decimal` `count` in an `int`-annotated field
`V33Action.count: int = 0` (`pilot/service/v32/actions.py:99`); the ALARM actions set `count=<Decimal>`. Python
does not enforce the annotation and the only consumer is `_count_out(a.count)` (journal), which handles Decimal, so
this is harmless. Noted for cleanliness only.

### F5 -- NIT -- `fill_seen_by_coid` / amend-cross interaction (theoretical)
On the normal ladder path `fill_seen_by_coid` is incremented by the full `event.count` BEFORE the `amend_cross_pending`
skip, so `fill_seen` can exceed `rest_booked_by_coid` for an amended rung. If such a rung were then driven onto the
orphan path with further distinct-trade-id echoes, the `min(count, seen-booked)` dedupe could over-book. I could not
construct a reachable path (an amended order has a venue id and is resolved via `cancel_ctx`, not the pure-orphan
branch; WS dups are caught by trade_id; polls dedupe on `rest_booked`). Pre-existing N1 behaviour on the normal path
is unchanged. Flagging for the record; no action.

## What I changed
- `pilot/service/v33/core.py` `_apply_orphan_fill` (~1184): added the `event.price is None` fail-closed guard
  (`orphan_rung_fill_unpriced`) before the dedupe accumulator is touched. [F1]
- `pilot/tests/test_v33_naked_fill_core.py`: added `test_orphan_fill_with_no_price_is_alarmed_unpriced_not_crashed`.

## Tests
Full suite in this worktree (Rung-1 census data present here, so more runs than the PR's 1438):
pre-change **1491 passed / 5 skipped**; post-change **1492 passed / 5 skipped** (+1 my test). 0 failures.
