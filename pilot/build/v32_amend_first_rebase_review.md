# Review — PR #59 amend-first replace, REBASE delta review (`feat/amend-first-replace` -> `main`)

Reviewer: Opus 4.8 (Fable's delegated review agent). Adversarial DELTA review of the rebase at Brad's
request. Do NOT merge; Brad merges. No live tree touched, no proxy dialed, no `.env`/`*.pem`/sealed read,
no Kalshi order/API calls (only read the public Amend Order V2 docs). Review branch
`review/pr59-rebase-notes` off `origin/main` `fffc062` (= #56 phantom-resting + #62 partial-fill wings).
PR head `cfbf10a` (force-pushed rebase). Fast-forward mergeable (`main` is an ancestor of the head).

## VERDICT: APPROVE WITH NITS

The rebase is sound. The amend-first mechanics compose correctly with #62's partial-fill batch model and
#56's phantom filter; `contracts` = 1 behaviour is byte-identical apart from additive amend counters (all
zero on the existing ledger); the load-bearing Kalshi Amend V2 semantics claim is CORRECT (verified
against the docs); the falsifier entry is append-only and touches no pin/threshold/STATUS/sha; the full
suite passes. Behaviour is fully neutral live today (the proxy 403s every amend, so the path only runs in
tests until Brad applies the proxy cap).

One elevated nit (N1) must be fixed **before** `contracts` is raised to 2 with the proxy amend-cap
applied: a **partial** amend cross double-books at the core when the venue also echoes the cross on the WS
fill channel — reproduced at `contracts` = 2 (the build report's "unreachable at contracts <= 2" is
wrong; it is reachable at exactly 2). It is not reachable in today's config, so it does not block this
merge, but it is a correctness gate on the `contracts` = 2 amendment.

---

## Load-bearing claim VERIFIED — Amend V2 `fill_count` is per-amend, not cumulative

The whole `_apply_amended` design books the amend cross as `delta = fill_count` directly (no
cumulative->delta subtraction). If the response reported the order's *cumulative* fills, that would
double-book the pre-amend lots. Verified against docs.kalshi.com (Amend Order V2): the response is an
**amend-specific result, not a full order object**, and `fill_count` / `average_fill_price` /
`average_fee_paid` are the **fills resulting from THIS amend only**; `remaining_count` is the
**post-amend resting quantity**. So `delta = fill_count` is correct. Combined with `_apply_amended`
carrying `rest_booked_by_coid` forward from the old coid to the new coid (the order_id persists, the coid
rotates), the cumulative->delta arithmetic in `_apply_cancelled` / `on_poll_fill` stays correct across an
amend. Reproduced (Probe P2): after a ws lot 1 (booked=1) and an amend that crosses 1 more, the state
shows `rest_fills`=2, `rest_booked_by_coid[new_coid]`=2 (carry-forward 1 + delta 1, **no double**),
`rest_allotment_done`=True, wings taken.
Sources: https://docs.kalshi.com/api-reference/orders/amend-order-v2

---

## What I verified clean

**Byte-identity at `contracts` = 1 (RECEIPT), additive-only.** Fresh READ-ONLY copy of the live ledger
(`...\pilot\ledger\v32_ledger.jsonl`, 102 rows) run through `python -m service.v32.report --ledger <copy>`
from PR head `cfbf10a` vs `origin/main`:
- `--days 4` table diff: **exactly one added line** — `amends=0 confirmed=0 failed=0 fallbacks(cancel+create)=0 fills_on_amend=0`.
- full `--json` structured diff: **exactly 5 added keys**, all under `totals`, all 0
  (`amends`, `amends_confirmed`, `amends_failed`, `amend_fallbacks`, `fills_on_amend`). Nothing else
  changed — scoreboard `n`=6, `replaces`=9733, all window values, all falsifier values identical.
  Confirms the counters are additive only and no existing value moved.

**Amend-first requote mechanics (task points 2, 4).**
- Same-bucket replace (|dn| >= tol and >= deb_ms) now emits `AMEND_REST` with `order_id` persisting and
  `count = _rest_size(params, st)` (the still-resting remainder). Probe P1 (`contracts` = 2, after a
  1-of-2 fill): `AMEND_REST` `count`=1, `order_id`=OID1 — the remainder, never `params.contracts`.
- `amend_in_flight` holds all further place/cancel/re-amend until `OrderAmended` (success) or the
  fallback's `OrderCancelled`. `_requote` bucket-change branch guarded `and not st.amend_in_flight`; the
  quote-end cancel and bucket-change are UNCHANGED (still cancel). `rest_live` is kept populated during
  the amend so a fill books at the resting price.
- The changed #62 test `test_partial_fill_wings_sized_to_fill_remainder_stays_resting` is a legitimate
  behaviour update (asserts `AMEND_REST` count=1 + `OrderAmended` + carry-forward + the pre-amend lot not
  re-booked), strengthened, not weakened.

**cancel_ctx x amend interplay (task point 3).** `cancel_ctx` is keyed by `order_id` (which persists
across the amend); `_remember_cancel_ctx` records `(rest_live.client_order_id, price)` = the CURRENT
(post-amend) coid. Probe P3: after amend (coid rotates a->b, `rest_booked_by_coid[b]`=1 via carry-forward),
a T-5 quote-end eager-clear cancel confirming cumulative filled=2 books exactly one more (delta = 2 - 1)
and takes wings — the missed lot is booked + hedged, not dropped, and the pre-amend lot is not re-booked.
Matches the builder's `test_amend_then_quote_end_cancel_books_missed_second_lot`.

**Executor merge / fallback (task point 5).** `_amend_rest` POSTs the sharded amend
(`amend_path(oid, exch)` = `POST .../{id}/amend?exchange_index=`, exchange_index also in the body). On ANY
non-2xx/timeout/missing-oid it journals `amend_failed` and runs `_amend_fallback`, which uses the #56/#50
**sharded** cancel (`cancel_path` -> `_resolve_cancel_success` / `_cancel_nonok`), and the CREATE goes
through the next tick's `_emit_place` -> `_place_rest` -> the #56 pre-PLACE phantom-filter invariant
(disjoint region from the amend code; auto-merged, phantom filter intact). The amend counts as a replace
on CONFIRM (`_apply_amended`), the fallback counts once at the create (`_emit_place`) — never double.

**Falsifier scoreboard unaffected by the amend money-math (task point 5).** The scoreboard reads
core-derived `wing_batch_sets` / `realized_lock` / `one_legged`, NOT `executor.fills`. So the amend
money-math de-dup (by `order_id`, all-or-nothing) affects only `realized_delta` / `cost` (the
reconciliation slot), never the falsifier verdict, at `contracts` = 1 or 2. (See N2.)

**Falsifier doc (task point 7).** The amend `MECHANICS CLARIFICATION` (2026-09-15) and the `#62`
`MECHANICS + MEASUREMENT CLARIFICATION` (2026-09-18) are BOTH present and chronological (09-15 before
09-18). No `[pin]`, threshold, the `n >= 30` count, the params sha
(`0ac697957c...`, quoted unchanged), or the `STATUS: FROZEN` line is touched. The "What is being judged"
line's in-place edit (`... never two live rests` -> `(amend-first, cancel -> confirm -> create as the
fallback; never two live rests)`) is disclosed inside the Registration entry and is consistent with the
precedent of the 09-15 shadow-window / MUST-CONFIRM in-place wording fixes. `test_v32_falsifier_pins.py`
adds 1 amend assertion, add-only (no existing assertion edited).

**Tests (task point 8).** Full suite on `cfbf10a`, fresh worktree, excluding the 5 data-file-dependent
files absent in a fresh checkout (`test_parity`, `test_shakedown`, `test_quintile`, `test_review_probes2`,
`reference_impl_review` — need `sim/out/census_train.csv` / `historical-data/15-minute/...`): **856 passed,
2 skipped, 0 failures**. The amend + interplay files alone (`test_v32_amend`, `test_v32_partial_fill`,
`test_v32_falsifier_pins`, `test_v32_core`, `test_v32_golden`): **111 passed**. Consistent with the
builder's 912 (= 856 + the ~56 tests in the data-file files, present in the builder's tree). The 2
pre-existing `test_quintile` errors the builder reports are the same environmental corpus absence.

---

## Nits

### N1 (elevated — must fix BEFORE `contracts` = 2 with the proxy amend-cap applied; not blocking today)
**A partial amend cross double-books at the core when the venue also echoes the cross on the WS `fill`
channel.** The build report's live-confirm item (b) claims this is "unreachable at `contracts` <= 2 (a
cross that leaves 0 remaining nulls `rest_live`, so the WS echo no-ops)." That is **wrong for
`contracts` = 2**: a 1-of-2 amend cross leaves `remaining` = 1, so `rest_live` is RETAINED under the new
coid, and a later WS `Fill` for the same crossed lot is attributed and booked AGAIN.

Reproduced (core-level, `contracts` = 3 so a 1-of-3 partial cross clearly leaves a remainder; identical
mechanism at `contracts` = 2 with a 1-of-2 cross):
```
after partial amend cross: rest_fills=1  booked={coid_b: 1}   (rest_live retained, count 2)
after ws echo of the crossed lot: rest_fills=2  booked={coid_b: 2}   *** DOUBLE-BOOK ***
```
Driver-level reachability: `on_fill` de-dups only by `trade_id` and `wing_coids`; the amend response
path (`_amend_rest`) books the cross via the amend channel and does NOT register the crossed trade's
`trade_id` in the driver's `_seen_trade_ids`, and `OrderAmended` carries no `trade_id`. So a WS frame for
the same taker cross (a new `trade_id`) is not recognized as a duplicate -> `_apply_fill` -> another
`_book_rest_delta`. `file:line`: `pilot/service/v32/core.py:_apply_fill` (books any tracked-order
`Fill.count` as a fresh delta with no per-trade guard) vs `pilot/service/v32/executor.py:_amend_rest`
(books the cross without seeding the driver's trade dedup).

**Why non-blocking now:** requires (a) the proxy amend-cap applied (today every amend 403s -> fallback,
never crosses), AND (b) `contracts` >= 2 (frozen at 1; raising it is a separate amendment with its own
pinned sha + Registration entry), AND (c) an amend that crosses PARTIALLY, AND (d) the venue echoing the
cross on WS. None hold in the current live config, and at `contracts` = 1 a cross fully fills (remaining
0 -> `rest_live` nulled -> the WS echo routes to `book_late_rest_fill` and is dropped by its guard). So
today's behaviour is correct.

**Recommended fix (do before contracts=2 + cap):** give the amend-cross a trade-level or order-cumulative
dedup against the WS echo — either seed the driver's `_seen_trade_ids` with the amend response's trade
id(s) if the response exposes them, or (more robust, mirrors the cancel path) make the post-amend WS rest
`Fill` for a crossed order delta-aware: skip up to the `fill_count` the amend already booked for that
`order_id`. Also correct the build report's item (b) wording from "unreachable at contracts <= 2" to
"unreachable at contracts = 1; reachable at contracts >= 2".

### N2 (documented; same class as #62 N4)
The executor's amend money-math append is de-duped by `order_id` (`booked_rest_oids`), all-or-nothing. At
`contracts` > 1, an amend cross on an `order_id` that already booked a lot (via a prior ws/poll rest
fill) would SKIP the executor `fills` append, so the amend cross's taker COST would be missing from
`executor.fills` and thus from `realized_delta` / `cost`. The CORE booking (strategy state, which the
falsifier reads) is correct via `_book_rest_delta`, so the verdict is unaffected — but the ledger's
`realized_delta` reconciliation could understate a ctx/amend-booked lot's cost at `contracts` > 1. Track
with #62 N4 for a reconciliation pass when `contracts` is actually raised. Non-blocking at `contracts` = 1.

### N3 (acknowledged unknowns; guarded)
`post_only`-on-amend and amend rate limits are undocumented (build report unknowns 1, 2). The
`fill_count` > 0 handling is defensive — if amends are maker-only the cross path never fires; amend 429s
route to the safe fallback. `average_fill_price` units are assumed YES-space normalized (unknown 4); the
`exec_price_mismatch` alarm and the first `amend_fill` journal are the live check, and booking is
conservative (a crossed amend fills at the amended price or better). All acknowledged and behaviour-neutral
until the cap is applied.

---

## Cleanup
Throwaway worktree `C:\Users\Brads\Python_stuff\dv3_wt_review_pr59` created for the suite run + probes and
removed after this review.
