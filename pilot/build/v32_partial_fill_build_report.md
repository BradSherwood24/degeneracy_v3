# V3.2 build report — PARTIAL-FILL WINGS (wings per fill event, sized to the fill)

Branch: `feat/partial-fill-wings` (off `origin/main` be2faf3). Build agent: Opus 4.8. Do NOT merge; Brad
merges code PRs. No live tree touched; no proxy dialed; no sealed/holdout date read.

## Why

Live `params.contracts` = 1; Brad wants to double to 2. The prints that fill our resting bucket-NO are
mostly 1 lot (6/6 live fills), so at `contracts` = 2 most fills are PARTIAL. A probe against the
pre-partial core (`_params(contracts=2)`, `_bring_up_live_rest`, then `Fill(oid, coid, Decimal(1), ...)`)
confirmed the bug: wings went out for count 1 (good) but the state set `rest_live=None` /
`rest_pending=None`, ORPHANING the still-resting second lot — a later Fill for it was ignored (no wings,
no booking) and no cancel was emitted at quote end. An unhedged, unbooked contract.

Brad's ruling (verbatim, 2026-09-18 ~18:30Z): "We should only send orders for the wings on the singal
that our maker order filled, for the size it filled at. So if max sizing is 10, a taker fills 8, then we
open 8 wings and leave the 2 unfilled ... Hopefully another taker comes and fills the remainder. Id like
to play around with different levels, but thats a problem at a different sizing."

## What changed

### `service/v32/core.py` (the heart; surgical, batch-additive)
- New `WingBatch` dataclass (one per rest fill event: `fill_price`, `fill_count`, `taken`, `completed`,
  `one_legged`) and a `batch` index field on `WingLeg`. `wing_legs` stays a FLAT tuple across all
  batches (the executor's `_take_wings` — which sends every `pending` leg — is UNCHANGED); the per-leg
  `batch` tag is how the core attributes a leg to its batch.
- New `V32State` fields (additive): `wing_batches`, `rest_fills`, `rest_remaining`,
  `rest_allotment_done`, `rest_booked_by_coid`, `next_batch_index`, `partial_fills`. The existing
  scalars `rest_fill` / `wings_needed` / `wing_taken` / `one_legged` are kept as MIRRORS, re-derived
  from the batch state by `_sync_wing_mirrors` on every transition — so the driver, ledger and every
  existing test read the same values, and at `contracts` = 1 (one batch) they carry the pre-partial
  single-batch values exactly.
- `_apply_fill` (rest branch): a ws `Fill.count` is a PER-FILL delta; each delta spawns its own wing
  batch via `_book_rest_delta`, the remainder stays resting (`rest_live` kept at the reduced count),
  and `rest_booked_by_coid` records the cumulative lots booked for the order.
- `_apply_cancelled`: `filled_count_before_cancel` is CUMULATIVE — books only the DELTA over
  `rest_booked_by_coid[coid]` (the nastiest bug class: a ws lot then a cancel poll reporting filled 2
  books exactly ONE more). Keeps the eager-clear fallback (book at `desired_n` once) for
  bucket-change/stand-down cancels.
- `_wing_step` rewritten to iterate EVERY incomplete batch: `_take_batch` (unconditional initial take,
  ruling F-2, sized to the batch fill) and `_retry_batch` (per-batch lock-floored single-leg retry).
  The T-1 s cutoff flags each incomplete batch `one_legged` independently. `_maybe_close_set(index)`
  completes and counts ONE set per batch, gated on the batch not already completed (dup-fill safe).
- `_emit_place` / `_place_action` place `_rest_size(params, st)` = the still-resting remainder (full
  allotment until a fill reduces it).
- `_requote` latch changed from `rest_fill is not None` to `rest_allotment_done` (a PARTIAL fill no
  longer stops quoting — the remainder keeps being requoted; only a full allotment stops). The
  `sets_done >= max_sets_per_hour` no-quote branch was REMOVED (it would wrongly stop the remainder at
  `contracts` > 1, and at `contracts` = 1 the allotment latch already fires at the fill before any set
  completes, so it was dead code there).
- `book_late_rest_fill` (F-1) books the late fill as one batch and latches the allotment (single-batch
  for a replaced-order tail; byte-identical at `contracts` = 1).

### `service/run_v32.py` (driver money math)
- `_compute_money_math(state, executor, contracts=1)` now derives `held_legs`, the floor, the
  first-completed-set `realized_lock`, and the new per-set slots PER BATCH. New helper
  `_batch_set_records` emits one record per batch with its per-contract lock, completion and one_legged.
  Added keys: `rest_fills`, `wing_batch_sets`, `lots_filled`, `lots_unfilled_at_quote_end`,
  `partial_fills`. Existing keys (`fills`, `wing_fills`, `held_legs`, `realized_lock`, `one_legged`,
  `realized_delta`) keep their exact values at `contracts` = 1. `_finalize` passes
  `contracts=params.contracts`.

### `service/v32/ledger.py`
- `build_v32_ledger_row` gains additive kwargs + row keys `rest_fills`, `wing_batch_sets`,
  `lots_filled`, `lots_unfilled_at_quote_end`, `partial_fills` (defaults `[]`/0). NB the per-set LIST is
  named `wing_batch_sets` to avoid colliding with the existing INT `wing_batches` count key.

### `service/v32/report.py`
- The falsifier scoreboard counts SETS from `wing_batch_sets` when present (new helper
  `_row_set_events`), else falls back to the legacy single-set scalar (`realized_lock` / `one_legged`)
  — so the scoreboard over an EXISTING ledger is byte-identical to the pre-partial report. A completed
  set = one rest-fill event with both wings filled; realized lock is per contract; fill rate = set
  events / armed day (`fills_total` now counts rest-fill events).

### `ceremony/v32_falsifier.md` (Registration, append-only — no `[pin]`/threshold/STATUS touched)
- Appended `2026-09-18 ~18:30Z — MECHANICS + MEASUREMENT CLARIFICATION (partial fills / sizing step)`:
  Brad's verbatim ruling, the mechanics (wings per fill event sized to the fill; remainder stays
  resting; cumulative→delta booking), the measurement (set = rest-fill event with both wings filled;
  lock per contract; fill rate = set events/armed day), registered at n=6 live sets before any size
  change, and that `params.contracts` stays Brad's lever (the params-sha `[pin]` gets its own
  Registration entry when he sets 2).

### Tests
- `tests/test_v32_partial_fill.py` (new, 12 tests): 1-of-2 fill sizing + remainder-stays-resting +
  remainder requotes at count 1; second fill → second batch + allotment done + quoting stops; both
  batches complete → 2 sets; cumulative→delta on cancel books exactly one more; cancel full-fill at
  once → single batch of 2; duplicate wing-fill dedupe across batches; quote-end cancel of the
  remainder; per-batch one_legged at cutoff; `contracts` = 1 single-batch mirror regression; ledger +
  scoreboard per-set (n=2, per-contract lock) and one-legged (n=1, legged=1) and the `contracts` = 1
  additive-keys single-set row.
- `tests/test_v32_falsifier_pins.py` (ADD-ONLY, 2 tests): the 2026-09-18 Registration entry carries
  Brad's verbatim ruling + mechanics + measurement; `contracts` stays 1 / sha unchanged. No existing
  assertion edited.

## Ledger schema additions (additive; existing keys unchanged)

| key | type | meaning |
|---|---|---|
| `rest_fills` | list | `{price, count, server_ts}` per rest fill event |
| `wing_batch_sets` | list | `{index, fill_price, fill_count, completed, one_legged, realized_lock, held_legs}` per batch (realized_lock is PER CONTRACT) |
| `lots_filled` | int | total lots filled this window |
| `lots_unfilled_at_quote_end` | int | allotment − lots_filled |
| `partial_fills` | int | rest fills that left a resting remainder |

At `contracts` = 1: `rest_fills` = 1 entry, `wing_batch_sets` = 1 entry, `lots_filled` ∈ {0,1},
`partial_fills` = 0.

## Report-diff result at contracts = 1 (acceptance test)

Ran `python -m service.v32.report` on a read-only COPY of the live ledger
(`C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v32_ledger.jsonl`, 98 rows) from this branch
vs a throwaway `origin/main` worktree:
- `--days 4` table: **IDENTICAL**
- `--days 4 --json`: **IDENTICAL**
- full `--json`: **IDENTICAL**

Live scoreboard unchanged: completed sets n = 6, mean lock +11.3c, %positive 100, fill rate 1.69/day,
exec gap −0.7c, VERDICT `n<30 pending (n=6)`.

## Tests

`cd pilot && python -m pytest -q` → **876 passed** (862 baseline + 12 partial-fill + 2 falsifier-pins).
`tests/test_v32_falsifier_pins.py` add-only (no existing assertion changed).

## #56 / #59 conflict hunks (for the later rebase)

PR #56 (`fix/phantom-resting`) touches ONLY `executor.py`; I made NO executor.py changes, so **#56 does
not conflict**.

PR #59 (`feat/amend-first-replace`, core + executor amend path) overlaps `core.py`. Expected conflicts
and the intended resolution:
1. `V32State` fields — #59 adds `amend_in_flight` in the resting-order-lifecycle block; I add the
   partial-fill fields in the completion/wings block. Different locations → likely auto-merges (keep
   both).
2. `_apply_cancelled` — OVERLAP. #59 adds `amend_in_flight` to the in-flight-clear line
   (`if matched or st.cancel_in_flight or st.amend_in_flight:` + clearing both); I rewrote the
   fill-booking block below it into cumulative→delta booking. Resolution: keep #59's `amend_in_flight`
   clear AND my delta-booking block.
3. `_apply_fill` — OVERLAP. #59 adds `amend_in_flight=False` to the rest-fill `replace(...)` that I
   DELETED (my rest branch now calls `_book_rest_delta`). Resolution: move the `amend_in_flight=False`
   clear into `_book_rest_delta`'s allotment-done branch (next to `cancel_in_flight=False`).
4. `_requote` — OVERLAP. #59 adds the amend path; I changed the latch to `rest_allotment_done` and
   removed the `sets_done` no-quote branch. Resolution: keep the `rest_allotment_done` latch and merge
   #59's amend/replace decision into the place/replace tail.
5. `_emit_place` / `_emit_amend` — my `_emit_place` count → `_rest_size`; #59 adds `_emit_amend` after
   it. Not a textual conflict, but a SEMANTIC follow-up: #59's amend of the remainder must amend at
   `_rest_size(params, st)` (the remaining count), not `params.contracts`.
6. `decide_v32` dispatch — #59 adds an `OrderAmended` branch; I did not touch the dispatch → clean.

## Open nits / known limitations

- **Executor is remaining-count-correct via the action count** — no executor.py change was needed: the
  core carries `count = _rest_size(...)` on `PLACE_REST`, so `RestRecord.count` is the placed
  remainder; wing legs carry the batch fill count; the cancel confirm returns the CUMULATIVE
  `filled_count_before_cancel` and the CORE does the delta. This keeps the diff off #56/#59's executor
  file.
- **Driver poll path stays single-shot per order** (`on_poll_fill` retains its existing
  `_rest_fill_booked_oids` gate) to preserve `contracts` = 1 byte-identity. At `contracts` > 1 a
  poll-only second lot would not top up the CORE via the poll; hedging of extra lots comes from the ws
  `fill` channel and the cancel-race (both fully wired). The cumulative→delta mechanism IS implemented
  and tested via the OrderCancelled path (the executor's cancel confirm). If Brad wants the belt-and-
  braces poll to top up at `contracts` > 1, that is a small follow-up (feed the poll's cumulative and
  let the core dedup) — deferred to avoid churning the byte-identical poll path.
- **`realized_delta` / `fills` money-math capture** still comes from `executor.fills` (exec-price
  reconciliation slot); at `contracts` > 1 the executor's rest-leg capture may under-list lots that
  never reach `_record_fill`, but the FALSIFIER measurement (per-set locks, set counting, held legs)
  is derived from CORE STATE and is correct.
- **`max_sets_per_hour`** is unchanged in policy (1) but is no longer a quoting gate; the "one
  allotment per hour" reading is enforced by `rest_allotment_done`. Documented in the Registration
  entry and in `_requote`.
- `params.contracts` remains 1 (frozen sha unchanged). Raising it is Brad's amendment with its own
  pinned sha + Registration entry.
