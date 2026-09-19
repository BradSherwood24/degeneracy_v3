# V3.2 build report — amend-first replace with cancel+create fallback

**Date:** 2026-09-15
**Branch:** `feat/amend-first-replace` (based on `fix/shadow-quote-window` / PR #54)
**Authority (verbatim):** Brad, 2026-09-15 ~18:10Z — *"Yea, I agree. Use the cancel and recreate flow as
a backup if our post to ammend the order fails. Go ahead and build that"*, in reply to the proposal to
replace the mid-window replace mechanics with Kalshi's Amend Order, falling back to cancel+create.

## The problem

The mid-window replace was strictly SEQUENTIAL: `CANCEL_REST → wait for OrderCancelled → PLACE_REST` on a
later tick (R-OVERLAP ruling, never two live rests). Correct, but it leaves **nothing resting for ~one
round-trip (~0.7 s) per replace** — ~60-90 s per busy hour at ~77 replaces/h. A qualifying pump print that
lands in that no-rest gap is a fill the strategy should have taken and did not.

### The miss it caused (2026-09-15 14:00Z window)

The live rest was cancelled at **13:45:20Z** for a requote; a qualifying spot-bucket YES print landed in
the cancel→recreate gap before the new rest was live, so the window took no fill it otherwise would have.

### Rest-absent share (fraction of the quoting window with NO live rest) — 2026-09-15 windows

| window (UTC close) | rest-absent share |
|--------------------|-------------------|
| 01:00Z–05:00Z      | 1–5 %             |
| 14:00Z             | 11 %              |
| 15:00Z             | 15 %              |
| 16:00Z             | 11 %              |

The busy hours (14–16Z) spent 11–15 % of the 10-minute quoting window with no order on the book purely to
the cancel→recreate gap — the direct cause of the 14:00Z miss and a standing drag on fill rate.

## The design (amend-first)

A same-bucket requote (|dn| ≥ tol and ≥ deb_ms, the existing gate) now emits `AMEND_REST` — Kalshi
**Amend Order V2** (`POST /portfolio/events/orders/{id}/amend?exchange_index=<idx>`, `exchange_index`
also in the body). The `order_id` **persists**; a price change **forfeits queue position exactly as
cancel+create did** (documented amend semantics), so the economics are unchanged — but the order stays
resting continuously and reaches the new price ONE round-trip sooner, with no no-rest gap.

- **Core** (`service.v32.core`): `_emit_amend` mints a new coid and sets `amend_in_flight` (holds all
  further requotes); `_apply_amended` updates `rest_live.price`/coid IN PLACE on `OrderAmended` and
  clears the flag. Bucket changes (different ticker — an amend cannot change the ticker) and the
  quote-end cancel are UNCHANGED (still cancel). "Never two live rests" preserved — the one order is
  amended. An amend IS a replace for `replace_count`, the A_REPLACE alarm, and the ledger `replaces`
  counter (counted on CONFIRM, mirroring the cancel+create path which counts at the create — so a
  fallback counts exactly once too).
- **Fill during the amend:** `OrderAmended.fill_count > 0` (a TAKER fill at `average_fill_price`,
  normalized to NO-space) is routed into the wings exactly like a fill-before-cancel; with count 1 the
  order is fully filled and nothing remains resting. The executor books the taker fill into money-math
  with the REAL taker fee (`average_fee_paid`), de-duped by `order_id`.
- **Executor** (`service.v32.executor`): `_amend_rest` POSTs the sharded amend; `_amend_body` sends
  `side:"ask"` (the bucket-NO buy's book side), `price` = YES-space `1 − n` (same convention as the
  create), `count:"1.00"`. On 2xx → `OrderAmended` + RestBook updated (new coid live, same order_id, old
  coid retained "amended" for late-fill attribution, F-1).

## The fallback (Brad's requirement)

On **ANY** non-2xx / timeout / exception, `_amend_rest` journals `amend_failed` (recording
`fallback:"cancel_create"`) and runs the existing sequential path: the **sharded** DELETE (PR #50
backoff / status-truth), whose `OrderCancelled` clears the core's rest so the next tick re-places via
`PLACE_REST` through the **pre-PLACE venue-truth invariant**. This is the proven cancel → confirm →
create, unchanged. A cancel-race fill discovered in the fallback is routed to the wings as before.

## Counters / journals / report

- Journals: `amend_rest` (request), `amend_confirmed` (response), `amend_failed` (+ that the fallback was
  taken), `amend_fill` (when the amend crossed).
- Ledger counters (additive, `.get` default 0): `amends_attempted`, `amends_confirmed`, `amends_failed`,
  `amend_fallbacks`, `fills_on_amend`.
- Report: an amend totals line next to `replaces`.
- Modes: `WOULD_AMEND_REST` twin in dry/shakedown (FrozenExecutor synth-amends so the dry state machine
  cycles); FrozenExecutor **refuses** a real `AMEND_REST` (P3-1, like PLACE/CANCEL).

## Falsifier

Registered as a **MECHANICS CLARIFICATION** (2026-09-15 ~18:10Z) in `ceremony/v32_falsifier.md` — no
[pin], the params sha, the `n ≥ 30` count, or the STATUS line touched (STATUS stays FROZEN). The
"What is being judged" line's `(cancel → confirm → create, never two live rests)` was edited in place to
`(amend-first, cancel → confirm → create as the fallback; never two live rests)`. The golden test
`test_core_live_fill_matches_lagging_reference` still reproduces the +10.36c reference EXACTLY (the
reference model `_ref_lagging` is itself single-lag — the order rests at its last price during one RTT —
so amend-first, which is also single-lag, matches it; the old cancel+create, a double-lag with a gap,
happened to match too on this fixture).

## Unknowns / risks (must confirm live)

1. **`post_only` on amend is UNDOCUMENTED.** The amend body inherits no `post_only`. We therefore treat
   `fill_count > 0` in the amend response as a REST FILL at `average_fill_price` and route it into the
   wings (it can only be at our amended price or better). If the venue never crosses on an amend (amend is
   maker-only), this path simply never fires — safe either way.
2. **Amend rate limits are UNDOCUMENTED.** At ~77 replaces/h the amend rate is ~0.02/s — far under any
   plausible limit — but this is unverified. Watch for amend 429s (they route to the fallback, which is
   safe, but a burst of 429s would mean the venue rate-limits amends more tightly than creates).
3. **PROXY CAP GAP (blocking for the amend benefit; safe without it).** The proxy today REFUSES every
   non-create order-write POST, so **every amend 403s and falls back to cancel+create** — armed behavior
   is identical to the old sequential replace until Brad applies the proxy change in
   `pilot/ops/proxy_amend_cap.md` and restarts. The failed amend 403 is a local proxy round-trip; it
   never reaches Kalshi and never consumes budget.
4. **Units of `average_fill_price` on an amend response** are assumed to follow the create-response
   convention (a NO order's price reported in YES-space; normalized via `normalize_fill_to_side`). If the
   amend response reported NO-space directly, the booked fill price would be wrong — the
   `exec_price_mismatch` alarm and the first `amend_fill` record are the live check. Booking is conservative
   in the meantime (a crossed amend can only fill at our amended price or better).

## Tests

`python -m pytest pilot/tests -q` — all green in the live tree (see the PR body for the count). New/updated
coverage: `tests/test_v32_amend.py` (12: wire body/path, 2xx one-order, 2xx cross → taker fill routed,
404/500/timeout → fallback cancel+create with pre-PLACE invariant + never two rests, FrozenExecutor
refuses / synth-amends, ledger counters, report totals); `tests/test_v32_core.py` (amend-first replace,
fill-during-amend, amend counts as replace, A_REPLACE via amends, quote-end/bucket-change still cancel,
shakedown WOULD_AMEND); `tests/test_v32_golden.py` (harness models `AMEND_REST → OrderAmended`; the
+10.36c reference still reproduces); `tests/test_run_v32.py` (FrozenExecutor dry amend cycle, F-1 late
fill on a retained pre-amend coid); `tests/test_v32_falsifier_pins.py` (new assertion: the MECHANICS
CLARIFICATION + "amend" are in the Registration; existing pin assertions unchanged and green).

## Rebase 2026-09-19 (onto origin/main `fffc062` = PR #56 phantom-resting + PR #62 partial-fill wings)

Rebased `feat/amend-first-replace` (was `43c5f59`, based pre-#56/#62) onto `origin/main` `fffc062`. The
branch's two commits replayed; conflicts resolved with the semantics registered by the partial-fill
build's own `#56 / #59 conflict hunks` table plus the PR #59 review's nit 2 (latent partial-fill
assumption, now live because #62 makes `contracts` > 1 real). Suite after rebase: **912 passed, 2 skipped,
2 errors** (`cd pilot && python -m pytest -q`); the 2 errors are the pre-existing `tests/test_quintile.py`
`FileNotFoundError` on `historical-data/15-minute/...`, the KNOWN data-absence in a worktree without the
corpus — code untouched by this branch (same 2 the PR #59 review recorded).

### Conflict hunks and how each was resolved

- **`core.py` V32State fields** (auto-merged) — kept BOTH #59's `amend_in_flight` (resting-order
  lifecycle block) and #62's partial-fill fields (`wing_batches`, `rest_fills`, `rest_remaining`,
  `rest_allotment_done`, `rest_booked_by_coid`, `next_batch_index`, `partial_fills`, `cancel_ctx`).
- **`core.py` `decide_v32` dispatch / `_requote` / `_book_rest_delta` helpers** (auto-merged) — #59's
  `OrderAmended` branch, the `amend_in_flight` hold + bucket-change `and not st.amend_in_flight` guard, the
  amend-first replace tail (order_id-None -> sequential fallback), sitting on #62's `rest_allotment_done`
  latch and the removed `sets_done` no-quote branch. Verified by read, not just by clean apply.
- **`core.py` `_apply_cancelled`** (CONFLICT) — kept #62's cumulative->delta booking (matched-order,
  `cancel_ctx`, and `__cxl__` unattributable branches) AND merged #59's in-flight clear:
  `if matched or st.cancel_in_flight or st.amend_in_flight: ... cancel_in_flight=False, amend_in_flight=False`.
- **`core.py` `_apply_fill`** (CONFLICT) — kept #62's `_book_rest_delta` rest branch; #59's
  `amend_in_flight=False` (added to a `replace(...)` #62 deleted) was MOVED into `_book_rest_delta`'s
  allotment-done (`new_remaining == 0`) branch, next to `cancel_in_flight=False`.
- **`core.py` `_apply_amended`** (semantic follow-up, rewritten) — #59's version booked the amend cross
  through the deleted single-scalar `rest_fill`; rewritten to (1) CARRY `rest_booked_by_coid` FORWARD from
  the old coid to the new coid on every amend (the venue's cumulative fill follows the persisting
  order_id, but the coid rotates — without carry-forward a post-amend cumulative cancel/poll would re-book
  the pre-amend lots and over-fill), and (2) book any amend cross via `_book_rest_delta` at
  `average_fill_price`. Kalshi Amend Order V2 (verified against docs.kalshi.com) reports `fill_count` /
  `average_fill_price` for the fills FROM THE AMEND ONLY (per-amend, NOT cumulative), so that count IS the
  newly-filled delta; a full cross latches `rest_allotment_done` via `_book_rest_delta`.
- **`core.py` `_amend_action` count = REMAINDER** — changed `count=params.contracts` to
  `count=_rest_size(params, st)` so the amend body carries the still-resting remainder at `contracts` > 1
  (task point 5). At `contracts` = 1 this equals `params.contracts`.
- **`executor.py`** (auto-merged) — #56's phantom filter (`_filter_phantoms`, `_status_says_gone`,
  rewritten `_pre_place_invariant`, `CANCEL_SETTLE_S` / `INVARIANT_RECHECK_S`, `cancel_confirmed_ts`) and
  #59's amend path (`_amend_rest` / `_amend_body` / `amend_path` / `_amend_fallback`) live in disjoint
  regions; only the `__init__` counter block was adjacent and merged (both #56's phantom/recheck counters
  and #59's amend counters kept). The amend fallback uses #56's sharded cancel -> `_resolve_cancel_success`
  / `_cancel_nonok` create path unchanged.
- **`run_v32.py` `_compute_money_math` counters dict** (CONFLICT) — kept both #62's
  `rest_invariant_phantoms` / `rest_invariant_rechecks` and #59's amend counters. #62's delta-aware
  `on_poll_fill` and `contracts=` signature (untouched by #59) carried through cleanly.
- **`ledger.py` / `report.py`** (CONFLICT, both additive) — kept both #62's (invariant phantom/recheck +
  partial-fill per-set keys) and #59's amend counter params/keys/totals/render lines.
- **`ceremony/v32_falsifier.md` / `PLAN_V32.md`** (auto-merged) — the amend "What is being judged" in-place
  edit and the 2026-09-15 amend MECHANICS CLARIFICATION registration entry sit chronologically before
  #62's 2026-09-18 partial-fill entry; no `[pin]`, threshold, params sha, or STATUS line touched.
- **`ops/V32_ARMING.md`** (CONFLICT) — both added a MUST-CONFIRM item 7; kept #56's invariant-phantom item
  as 7 and renumbered #59's amend-lands item to 8.
- **`tests/test_v32_falsifier_pins.py`** (CONFLICT, add-only) — kept BOTH #62's partial-fill assertions and
  #59's amend-mechanics assertion; no existing assertion edited (house rule honoured).

### Tests updated / added by the rebase

- **`tests/test_v32_partial_fill.py::test_partial_fill_wings_sized_to_fill_remainder_stays_resting`**
  (UPDATED, #62 test) — the remainder's same-bucket requote now emits `AMEND_REST` (count = remainder 1),
  not `CANCEL_REST` -> `PLACE_REST`; the assertion was updated to the amend-first mechanic and confirms via
  `OrderAmended` with the booked count carried to the new coid. This is a legitimate behavior change
  (amend-first now governs the remainder), not a weakened test.
- **`tests/test_v32_partial_fill.py::test_amend_then_quote_end_cancel_books_missed_second_lot`** (NEW, task
  point 6) — contracts=2, lot 1 ws, amend the remainder (coid rotates), lot 2 fills but ws is missed, T-5
  quote-end eager-clear cancel confirms cumulative filled=2 -> EXACTLY one more lot booked with wings via
  the carried-forward `rest_booked_by_coid` + `cancel_ctx` (delta = 2 - 1).
- **`tests/test_v32_partial_fill.py::test_amend_cross_books_per_amend_delta_and_latches_allotment`** (NEW,
  task point 7) — an amend that crosses reports a per-amend `fill_count`=1; the core books it as one batch
  at `average_fill_price`, `rest_booked_by_coid[new_coid]` = carried 1 + delta 1 = 2 (no double-book), and
  `rest_allotment_done` latches.

### Behaviour-neutral at merge (unchanged from the original build)

The proxy still REFUSES every non-create order-write POST, so every amend 403s and falls back to the
proven cancel+create; armed behaviour is identical to the pre-#59 sequential replace until Brad applies
`pilot/ops/proxy_amend_cap.md` and restarts. The amend-cross + carry-forward paths added here therefore
change nothing live today; they are exercised only by the tests and by the amend path once the proxy cap
is applied. Two live-confirm items remain (both behaviour-neutral now): (a) the executor's amend money-math
de-dups by `order_id` (all-or-nothing), so at `contracts` > 1 an amend cross whose order already booked a
lot would skip the executor `fills` append — the CORE booking (strategy state) is correct via
`_book_rest_delta`; and (b) a partial (not full) amend cross at `contracts` > 2 that also echoes on the WS
`fill` channel could double the core batch — unreachable at `contracts` <= 2 (a cross that leaves 0
remaining nulls `rest_live`, so the WS echo no-ops). Both are documented for the first live amend window.
