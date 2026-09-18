# Review — PR #62 partial-fill wings (`feat/partial-fill-wings` -> `main`)

Reviewer: Opus 4.8 (Fable's delegated review agent). Adversarial review at Brad's request. Do NOT merge;
Brad merges code PRs. No live tree touched, no proxy dialed, no sealed/holdout date read. Review branch
`review/pr62-notes` off `origin/main` be2faf3; PR head `39ca86c`.

## VERDICT: BLOCK

One blocking correctness defect: at `contracts` > 1 a lot that fills but whose WS `fill` message is
missed is **dropped entirely** (no wings, no ledger booking) whenever it is caught only by an
*eager-clear* cancel — which includes the **T-5 quote-end cancel that ends every window with a resting
remainder**. It reaches settlement as a naked, unrecorded bucket-NO. This is the exact orphaned-lot
failure the PR set out to eliminate; it is moved from the pre-partial code path to the eager-clear cancel
path, not removed. Reproduced below with a probe against the PR head.

Everything else is clean or a nit: `contracts` = 1 is byte-identical (receipt below), the tests are
sound, the cumulative->delta booking is correct on the *tracked-order* cancel path, per-batch coid
attribution is correct, and the falsifier Registration entry is append-only and untouched where it must
be. Fix the blocking item (small, core-only, contracts=1-safe patch given below) and this is an APPROVE.

---

## BLOCKING — B1: a missed-WS lot is dropped by the eager-clear cancel path (unhedged + unbooked to settlement)

**Files:** `pilot/service/v32/core.py:663` (the `elif not st.rest_fills` fallback in `_apply_cancelled`);
`pilot/service/v32/core.py:912` (`_cancel_live_if_any` eager-clears `rest_live`); interacting with
`pilot/service/run_v32.py:909` (the poll gate).

**Mechanism.** After a partial fill the remainder stays resting and keeps requoting (correct, the point
of the PR). `contracts` = 2, lot 1 fills on the WS channel:
- `_book_rest_delta` books lot 1, `rest_fills` = 1 entry, `rest_booked_by_coid[coid]` = 1, remainder
  rests on the same order (count 1).
- The 1 s status poll can no longer help: `on_poll_fill` returns immediately because
  `order_id in self._rest_fill_booked_oids` — the order was added to that set on lot 1's WS fill
  (`run_v32.py:818`). The builder documents this ("poll path stays single-shot per order").
- So the **only** backstops for lot 2 are (a) a second WS `fill` message, or (b) the cancel path
  (`filled_count_before_cancel` cumulative -> delta).

Now suppose lot 2 fills on the venue but its WS `fill` is missed (WS hiccup / reconnect / the 09-17
power-outage class / low-RAM watcher death — the very reasons the poll exists). The remainder is caught
only by the cancel path. There are three cancel paths, and they split on whether the slot is still
populated when the `OrderCancelled` arrives:

- **Drift/tol replace** (`core.py:1027`): keeps `rest_live` populated deliberately ("rest_live is kept
  populated (not eagerly cleared) so such a fill books at the RESTING price"). `matched_order` is found,
  delta booked correctly. **Safe.**
- **Quote-end cancel** (`_requote` `past_quote_end` -> `_cancel_live_if_any`, `core.py:912`): **eagerly
  nulls `rest_live`** and sets `cancel_in_flight`. When `OrderCancelled(filled=2)` arrives,
  `matched_order` is `None`, so the delta branch (`core.py:656`) cannot run; the fallback
  (`core.py:663`) is `elif not st.rest_fills:` — but `rest_fills` already holds lot 1, so it refuses.
  **Lot 2 is dropped.**
- **Bucket-change / stand-down cancel**: same `_cancel_live_if_any` (and the bucket-change branch also
  nulls `rest_live`). Same `matched_order is None` -> same drop.

The quote-end path is universal: every window that ends with a resting remainder cancels it at T-5. The
executor is fine — it recovers venue-truth `filled_count` on the 404/terminal cancel and returns
`OrderCancelled(filled=2)` (`executor.py:_finish_cancel`). The core throws it away. Money-math also loses
the lot: the executor's `_finish_cancel` de-dups the cancel-race booking by `order_id`, and lot 1's WS
fill already added the `order_id` to `booked_rest_oids`, so the executor books only lot 1. **Lot 2 is
naked to settlement (T-0), and appears nowhere in the ledger** — worse, `lots_unfilled_at_quote_end`
would record it as "1 unfilled remainder" when in fact it filled and settled naked.

**Worst-case unhedged interval:** from lot 2's fill (as early as ~T-14) to settlement **T-0** — past
T-5, T-4 and T-1 — with no hedge ever taken and no ledger record. This satisfies the task's blocking
criterion ("can reach the T-4 expiry with a filled-but-unhedged lot").

**Reproduction (probe against PR head 39ca86c, `contracts` = 2):**
```
=== PROBE A: quote-end cancel with a racing (ws-missed) 2nd lot at contracts=2 ===
after lot1: rest_fills= 1 batches= 1 rest_live count= 1
quote-end tick actions: ['CANCEL_REST', 'STAND_DOWN'] rest_live= None
cancel-confirm(filled=2) actions: []
  -> TAKE_WINGS for lot2? False   rest_fills now= 1 batches= 1
  -> rest_booked_by_coid= {'v32-...-1': 1} rest_allotment_done= False
  VERDICT A: *** LOT2 DROPPED (unhedged, unbooked) ***
```
(The bucket-change variant did not fire in a synthetic feed only because the probe's book update did not
re-select the spot bucket; the code path is identical to quote-end — `_cancel_live_if_any` nulls the slot
— so it drops the same way when it does fire.)

**Failing test to add** (put in `tests/test_v32_partial_fill.py`; fails on PR head, passes after the fix):
```python
def test_quote_end_cancel_books_missed_second_lot():
    """contracts=2: lot 1 fills on ws; lot 2 fills at the venue but its ws fill is MISSED (poll is
    gated by _rest_fill_booked_oids). The remainder is caught only by the T-5 quote-end cancel, whose
    OrderCancelled reports the CUMULATIVE filled=2. The core MUST book the delta lot and take its wings
    -- otherwise lot 2 settles naked and unbooked."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p); now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    assert st.rest_live is not None and st.rest_live.count == 1
    # T-5 quote-end: the remainder is cancelled (rest_live eagerly nulled).
    st, acts = _feed(p, st, ClockTick(T - 200))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    # the executor's cancel confirm carries venue truth: both lots filled (cumulative 2).
    st, acts = _feed(p, st, OrderCancelled(oid, T - 199, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2, "lot 2 must be booked from the cumulative cancel"
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS], "lot 2 must get wings"
    assert st.rest_allotment_done
```

**Recommended fix (core-only, small, contracts=1-safe).** Retain a cancel context so an eager-cleared
order's fill is still attributable by `order_id`:

1. Add a `V32State` field: `cancel_ctx: Mapping[str, tuple[str, Decimal]] = field(default_factory=dict)`
   (order_id -> (client_order_id, resting price)).
2. In `_cancel_live_if_any` (and the bucket-change branch of `_requote`), before nulling a *populated*
   `rest_live` that has an `order_id`, record `cancel_ctx[rest_live.order_id] =
   (rest_live.client_order_id, rest_live.price)`.
3. In `_apply_cancelled`, when `matched_order is None and filled > 0`, look up
   `ctx = st.cancel_ctx.get(event.order_id)`; if present, `coid, price = ctx; already =
   st.rest_booked_by_coid.get(coid, 0); delta = filled - already; if delta > 0: _book_rest_delta(...,
   coid, price, delta, now)`. Keep the existing `elif not st.rest_fills:` `__cxl__` fallback only for the
   truly-unattributable case (no ctx and no prior fill).

Why this is contracts=1-safe: at `contracts` = 1 the single fill sets `rest_allotment_done` and nulls
`rest_live` inside `_book_rest_delta`, so no post-fill eager-clear cancel ever carries a populated slot;
pre-fill cancels report `filled = 0` and book nothing. The byte-identity diff (below) is unaffected. With
the fix, the T-5 quote-end cancel books the delta lot and `_wing_step` takes its wings at
`t_to_close` ~= 300 s (>> the 1 s `no_orders_after_s_to_settle` cutoff), so it is hedged (or flagged
`one_legged` if the pin cannot complete in the T-5..T-1 window) — the designed envelope, not a silent
naked lot.

---

## NIT (strong) — N1: poll no longer backstops the 2nd lot; up to ~9 min naked even after the B1 fix

`on_poll_fill` (`run_v32.py:909`) is gated by `_rest_fill_booked_oids`, which lot 1's WS fill populated,
so the 1 s poll never re-reads the order to top up lot 2. Even after the B1 fix, a missed-WS lot 2 in a
**stable book** (no drift-requote) is hedged only at the T-5 quote-end cancel — up to ~9 minutes naked
(from a fill as early as ~T-14). The whole reason the poll exists is that WS fills get missed; at
`contracts` > 1 that belt-and-braces no longer covers the extra lots. Recommend (follow-up, not blocking
once B1 lands): let the poll top up at `contracts` > 1 by feeding a *delta*. The poll hands
`on_poll_fill` the **cumulative** `st.filled_count` (`run_v32.py:1284`), and the core treats `Fill.count`
as a delta (`_apply_fill` -> `_book_rest_delta(delta=int(event.count))`), so the poll must subtract the
already-booked count for that order before feeding the core (mirror the cancel path's cumulative->delta
arithmetic), and must not early-return once the order is in `_rest_fill_booked_oids` while lots remain.
This is the belt-and-braces the builder explicitly deferred; B1 is the correctness floor, this is the
latency/exposure improvement.

## NIT — N2: `book_late_rest_fill` idempotency guard is now coarser than per-lot

`book_late_rest_fill` guards with `if st.rest_fill is not None: return st, []` (`core.py:~1116`). At
`contracts` > 1 that mirror is set after the first lot, so a *genuinely distinct* second late lot on a
replaced order would be dropped by this hook. In practice it appears defended: every tracked order is
cancelled (drift/bucket-change/quote-end) and the executor's cancel confirm reports venue-truth
cumulative, so a real second late lot is caught by the cancel path (once B1 is fixed) and the late-WS
copy is a genuine duplicate this guard correctly drops. But the invariant is non-obvious and the
docstring ("once any rest fill is booked, this is a no-op") is imprecise at `contracts` > 1. Recommend
either (a) make it a per-`order_id`/per-trade idempotency check, or (b) add a comment + a test pinning
that the second late lot is caught by the cancel path, so a future edit to the cancel path cannot silently
reopen a drop here.

## NIT — N3: ledger misclassifies a dropped lot as an unfilled remainder

Downstream of B1: when a lot is dropped, `lots_filled` is derived from `state.rest_fills`
(`run_v32.py`), so a lot that filled and settled naked is reported as
`lots_unfilled_at_quote_end` >= 1. Once B1 is fixed this cannot happen for the eager-clear path; worth a
one-line assertion in the ledger/report test that `lots_filled + lots_unfilled_at_quote_end == contracts`
holds only when every venue fill was actually booked.

---

## What I verified clean

**Byte-identity at `contracts` = 1 (RECEIPT).** Copied the live ledger
`C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v32_ledger.jsonl` (99 rows, 98 data rows)
READ-ONLY into scratch and ran `python -m service.v32.report --ledger <copy>` from the PR worktree
(`dv3_wt_review_pr62`, head 39ca86c) and from `origin/main` (be2faf3):
- `--days 4` table: **IDENTICAL**
- `--days 4 --json`: **IDENTICAL**
- full `--json`: **IDENTICAL** (both 36 605 bytes; scoreboard `n` = 6, `verdict` = `n<30 pending (n=6)`)

Matches the builder's claim exactly. The scoreboard's `_row_set_events` falls back to the legacy single-set
scalar for rows without `wing_batch_sets`, so existing rows read identically; empty `wing_batch_sets` (dry
window) is falsy and also falls back. Correct.

**Tests.** `tests/test_v32_partial_fill.py` + `tests/test_v32_falsifier_pins.py` -> **22 passed** in the
PR worktree. Full suite there: **819 passed, 2 skipped**; the delta from the builder's 876 is entirely
collection errors from data files absent in a fresh worktree (`sim/out/census_train.csv`,
`historical-data/15-minute/...`) in `test_parity.py`, `test_shakedown.py`, `test_quintile.py`,
`test_review_probes2.py`, `reference_impl_review.py` — environmental, not code. (I did not stage those data
files.) The new tests do not cover the B1 quote-end/eager-clear race — `test_quote_end_cancels_partial_remainder`
asserts the cancel is *emitted* but never feeds a racing `OrderCancelled(filled=2)`, which is precisely
where the drop hides.

**Cumulative->delta booking (attack #2).** Correct on the *tracked-order* cancel path
(`_apply_cancelled` `matched_order is not None`, `core.py:656`): `delta = filled - already`, guarded
`delta > 0`. Duplicate WS+poll of the same lot: de-duped by `trade_id`/`_rest_fill_booked_oids` in the
driver, and by `rest_booked_by_coid` in the core. Out-of-order (poll cumulative before WS lot 1): poll
feeds `Fill(count=cumulative)` -> single batch of the cumulative count (correct), and the later WS
lot-1 copy routes to `book_late_rest_fill` which no-ops on the `rest_fill is not None` guard (no
double-book). `Fill.price is None`: `_apply_fill` falls back to `matched.price`; `matched is None`
short-circuits (no booking). All fine. The one gap is B1 (eager-clear -> `matched_order is None` ->
fallback refuses).

**Per-batch completion (attack #4).** Each batch mints two fresh coids via `_mint_coid` (monotonic
`coid_seq`); batches carry disjoint coid sets (asserted in `test_second_fill_spawns_second_batch...`).
`_wing_step` iterates every incomplete batch independently; `_retry_batch` filters by
`leg.batch != b.index`; `_maybe_close_set(index)` is gated on `not b.completed` (dup-fill safe);
per-batch `one_legged` at the T-1 cutoff. A one-legged batch 0 does not block batch 1. Correct.

**Requote of the remainder (attack #3).** `_requote` latch is now `rest_allotment_done` (a partial no
longer stops quoting); the `sets_done >= max_sets_per_hour` no-quote branch was removed and is genuinely
dead at `contracts` = 1 (allotment latches at the fill before any set completes). The drift replace keeps
`rest_live` populated so a racing fill books at the resting price. `_place_action`/`_emit_place` use
`_rest_size` (the remainder). Phantom/pre-place logic lives in `executor.py` which this PR does not touch;
the placed `count` is the remainder, so `RestRecord.count` is correct. No stand-down regression found.

**Falsifier Registration (attack #7).** The `v32_falsifier.md` diff is a single appended Registration
entry (2026-09-18) quoting Brad verbatim ("we open 8 wings and leave the 2 unfilled", "Hopefully another
taker comes and fills the remainder"), with mechanics + measurement. No `[pin]`, threshold, `n >= 30`
count, params sha, or `STATUS: FROZEN` line touched. `tests/test_v32_falsifier_pins.py` adds 2 tests and
edits **no** existing assertion (diff is purely additive). Correct and honest.

**#59 rebase notes (attack #8).** The builder's conflict list (`_apply_cancelled` `amend_in_flight`
clear + delta block; `_apply_fill` moving `amend_in_flight=False` into `_book_rest_delta`; `_requote`
latch; `_emit_amend` remainder must use `_rest_size`) is accurate. **Add:** the B1 fix introduces a new
`cancel_ctx` field on `V32State`; #59 also edits `V32State` — trivial both-keep merge. And #59's amend of
the remainder must clear/rebuild `cancel_ctx` consistently with the amend replacing the order id.

---

## Cleanup
Throwaway worktree `C:\Users\Brads\Python_stuff\dv3_wt_review_pr62` was created for the test run and probe
and is removed after this review.

---

# Review round 2 — new head `eaa634f` (delta re-review)

Verdict: **APPROVE WITH NITS**. The BLOCK (B1) is fixed and independently reproduced-as-fixed; the N1
poll backstop is now delta-aware and byte-identical at `contracts` = 1; N2/N3 addressed. Delta since the
reviewed head `39ca86c`: `core.py` (+cancel_ctx machinery), `run_v32.py` (delta-aware poll),
`test_v32_partial_fill.py` (+6 tests), build report. No falsifier/ledger/report/`__init__` change since
round 1 (Registration entry still append-only — unchanged).

## B1 — FIXED (verified)

- `V32State.cancel_ctx: Mapping[str, tuple[str, Decimal]]` (order_id -> (coid, resting price));
  `_remember_cancel_ctx` (`core.py:928`) records it before an eager clear; `_apply_cancelled`
  (`core.py:658-676`) uses it when `matched_order is None` to book `delta = filled - rest_booked_by_coid[coid]`
  at the retained price.
- **Every eager-clear site audited.** Grepped all `rest_live=None` in `core.py` (new head): `485`
  (inside `_book_rest_delta`, allotment-done — post-booking, everything already in
  `rest_booked_by_coid`, so a later cancel yields delta 0; no ctx needed), `644` (the `_apply_cancelled`
  MATCH path — coid/price captured inline before nulling), `945` (`_cancel_live_if_any` — covered by
  `_remember_cancel_ctx` at `944`), `1024` (bucket-change branch — covered at `1018`), `1171`
  (`book_late_rest_fill` F-1 tail — pre-existing, latches allotment; the order it books has already had
  its cancel processed, so this is the N2-documented duplicate path). All B1-relevant eager clears
  (quote-end / stand-down / replace-rate via `_cancel_live_if_any`, and bucket-change) are covered.
- **Probe A re-run against `eaa634f`:** `cancel_ctx has oid? True`, `TAKE_WINGS for lot2? True`,
  `rest_fills=2`, `allotment_done=True` -> **FIXED (lot2 hedged + booked)**. (Round-1 Probe A on `39ca86c`
  was `LOT2 DROPPED`.)
- **No double-booking (verified):**
  - *Cancel confirm arrives twice* (Probe C): 2nd `OrderCancelled(filled=2)` -> `already=2`, `delta=0`
    -> nothing booked; `rest_fills` stays 2. The `cancel_ctx` entry is never cleared but cannot
    double-book because `rest_booked_by_coid` gates the delta.
  - *Late-WS copy after the ctx booking* (Probe D): reaches `book_late_rest_fill` -> `rest_fill is not
    None` guard -> dropped; `rest_fills` stays 2, no actions.
  - *Executor cancel-race money-math*: `_finish_cancel` de-dups by `order_id` in `booked_rest_oids`
    (lot 1's WS `_record_fill` already added it), so it does not re-book the same order. The core's
    `cancel_ctx` booking and the executor's money-math are separate accounting sides (floor-from-core vs
    cost-from-executor, the round-1 "reconciliation slot"), not a shared counter — no double-count of the
    lot. (Residual: at `contracts` > 1 the money-math `cost` can under-list a lot the core booked, making
    `realized_delta` slightly conservative-or-not; the FALSIFIER scoreboard reads core-derived
    `wing_batch_sets`, unaffected. Pre-existing, non-blocking — see N4.)

## N1 — poll delta-aware (verified byte-identical at contracts=1)

`on_poll_fill` (`run_v32.py:906`) now books `delta = filled_count - rest_booked_by_coid[coid]` and feeds
`Fill(count=delta)`; `if delta <= 0: return`.
- **contracts=2 backstop:** after a ws lot 1, a poll reporting cumulative 2 books exactly one more and
  completes its wings (`test_poll_backstops_missed_second_lot_contracts2`, and my Probe A confirms the
  cancel path also catches it — belt AND braces now both live).
- **contracts=1 byte-identity:** after a ws lot fully fills, a poll reporting the same total -> delta 0 ->
  no `rest_fill_poll`, no booking (`test_poll_noop_at_contracts1_after_ws_fill`). Poll-first-when-ws-missed
  still books once (`test_poll_first_when_ws_missed_books_the_lot`).
- **WS-then-poll / poll-before-WS dedup:** the ws lot advances `rest_booked_by_coid`, so the poll delta
  is 0; poll-first advances it, so the later ws copy routes to `book_late` and no-ops. No double.
- **Poll reports LOWER than booked** (Probe E): `delta = 1 - 2 = -1` -> `delta <= 0` guard -> return.
  No negative booking, no crash.
- **Poll + cancel-confirm both seeing the lot:** whichever runs first advances `rest_booked_by_coid`;
  the second computes delta 0. Verified consistent.

## N2 / N3 — addressed

- N2: `book_late_rest_fill` docstring now states the guard is coarser than per-lot at `contracts` > 1 and
  is defended by the cancel path, pinned by `test_book_late_second_lot_caught_by_cancel_path` (passes).
- N3: `test_quote_end_cancel_books_missed_second_lot` asserts
  `lots_filled + lots_unfilled_at_quote_end == contracts` and `lots_filled == 2` after the ctx booking —
  the dropped-lot misclassification cannot occur once B1 books the lot.

## Receipts

- **Byte-identity** on a fresh READ-ONLY copy of the live ledger (99 rows) — PR head `eaa634f` vs
  `origin/main`: `--days 4` table, `--days 4 --json`, full `--json` all **IDENTICAL** (both full-json
  36 605 bytes; scoreboard `n` = 6, verdict `n<30 pending (n=6)`).
- **Tests:** `test_v32_partial_fill.py` + `test_v32_falsifier_pins.py` = **28 passed** (22 + 6 new). Full
  suite in a fresh worktree: **822 passed, 2 skipped**; the gap to the builder's 882 is entirely the
  data-file-dependent files absent in a fresh checkout (`test_parity`, `test_shakedown`, `test_quintile`,
  `test_review_probes2`, `reference_impl_review` — need `sim/out/census_train.csv` /
  `historical-data/15-minute/...`). Environmental, not code; no failures.

## Remaining nits (non-blocking; do not gate the merge)

- **N4 (was the round-1 reconciliation observation):** at `contracts` > 1 the money-math `realized_delta`
  / `cost` come from `executor.fills`, which can under-list a lot the core booked via `cancel_ctx` (the
  executor de-dups the cancel-race by `order_id`). The falsifier verdict reads core-derived
  `wing_batch_sets`, so the kill/verdict is unaffected, but `realized_delta` in the ledger row may not
  reflect a ctx-booked lot's wing cost. Worth a follow-up reconciliation once `contracts` is actually
  raised; not a correctness gate at `contracts` = 1.
- **N5:** `cancel_ctx` is never pruned within a window (grows by one per eager-clear). Bounded by the
  window's replace count and harmless (delta gating prevents any stale-entry double-book), but a small
  `del ctx[order_id]` after a terminal booking would keep it tidy. Cosmetic.

## Rebase note (#59)
Unchanged from round 1, plus: the B1 fix adds `cancel_ctx` to `V32State` and calls `_remember_cancel_ctx`
in `_cancel_live_if_any` and the bucket-change branch. #59's amend path replaces the resting order id; it
must record `cancel_ctx` for the amended-away order id too (or rely on the amend keeping `rest_live`
populated like the drift-replace path), so a missed-WS lot on an amended order stays attributable.

**Round-2 verdict: APPROVE WITH NITS.** B1 fixed and reproduced-as-fixed; N1 delta-aware and
contracts=1 byte-identical; no double-booking on any path checked; nits N4/N5 are non-blocking follow-ups.
Merge decision remains Brad's.
