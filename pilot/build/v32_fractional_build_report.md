# V3.2 fractional contract counts — build report

Branch: `fix/v32-fractional-counts` (from `origin/main` @ `e240311`, which includes #105 + #106).
Scope: give the LIVE V3.2 pump-fader (`service/v32/`, `service/run_v32.py`, armed size 2) the same
fractional-count treatment #106 gave V3.3, registered as a MECHANICS CLARIFICATION under the FROZEN
`v32_falsifier.md`. V3.2 is DRY right now and re-arms after this lands — correctness over speed.

House law kept: `python` only; no network/proxy (tests use fakes); no `.env`/`*.pem`; no
`sim/out/sealed_eval/**`; the SEAL (2026-08-02..18) and the 2026-08-20..29 holdout are untouched (the only
live-tree access was a READ-ONLY copy of `pilot/ledger/v32_ledger.jsonl` into the scratchpad to read the
current falsifier set count). `policy/v32_params.json`, both params shas, the falsifier STATUS line, and
everything ABOVE `## Registration` in `pilot/ceremony/v32_falsifier.md` are untouched (the falsifier diff
is a pure 48-line insertion in Registration). No push to `main`; no PR (Fable opens it).

## The cause

Kalshi crypto contracts are fractional: the venue reports `count_fp` at 0.01 granularity and a resting
maker order fills in sub-lot amounts (the 2026-09-30 22:00Z incident filled 0.44 of a lot on V3.3). V3.2
int-truncated on EVERY fill path:
- WS fill handler (`run_v32.on_fill`): `count = int(pf.get("count") or 0) or rec.count` — a 0.44 fill
  truncated to 0, then the `or rec.count` fell back to the FULL placed lot => **over-hedge**.
- status poll (`on_poll_fill`) and cancel-confirm (`_finish_cancel`): parsed the venue `fill_count_fp`
  with `int(...)` (`_fractional_counts` was `False` for V3.2), so a 0.44 fill became 0 => **fill LOST**,
  a naked unhedged bucket-NO rung.

## The fix, per path

### executor (`service/v32/executor.py`, shared base `LiveExecutor`)
- `_fractional_counts` flipped to `True` by DEFAULT (both rosters; `V33LiveExecutor` still sets it True
  explicitly). Nothing now needs `False`, but the flag is KEPT so a future int-only consumer/test can opt
  out, and the int `OrderStatus.filled_count` field is RETAINED unchanged (cancel-race arithmetic + any
  whole-lot reader). The executor scaffolding for the fractional cancel-confirm (`fill_count_fp`,
  `_resolve_cancel_success` fp, `_finish_cancel`'s `filled_fp`, `_last_confirm_status_fp`) was already in
  place from #106 — flipping the flag activates it for V3.2.
- `_dc` + `_count_body_str` added to the base class (reused for the fractional wing wire body). I ADDED
  them to the base and LEFT `V33LiveExecutor`'s identical overrides untouched (zero V3.3 risk; the base
  copy only serves V3.2). They are now redundant with the base and could be deleted in a later DRY pass.
- `_finish_cancel` normalises the journal + money-math count via a new module `_count_out` (integral ->
  bare int, fractional -> 2dp Decimal), so a WHOLE cancel-confirm fill is byte-identical (`1`, not `"1"`);
  a 0.44 is `"0.44"`. The `OrderCancelled` event keeps the raw Decimal.
- rest-invariant stray-fill branch: gated on `fill_count_fp > 0` when fractional (an `int`-only gate
  mis-classified a 0.44 stray fill as a phantom and dropped it naked).
- amend-cross fill: `fill_count` parsed Decimal (`self._dc`); money/journal via `_count_out`;
  `OrderAmended.fill_count` carries the Decimal.
- wing SEND (`_wing_entry`): `body["count"] = self._count_body_str(leg.count)` — "1.44" not the
  int-truncated "1.00"; "2.00" for a whole lot (byte-identical). V3.2 NEVER chunks: a wing count is
  <= `contracts` (2) <= the proxy `max_contracts_per_order` cap (2), so one IOC order per leg always
  carries the full hedge (unlike V3.3's ladder, whose summed wing counts can exceed the cap).
- `_wing_events` money-math count via `_count_out`.

### core (`service/v32/core.py`)
- `_q_count` + `_COUNT_Q` added (mirror of `service.v33.core`). `RestOrder`/`RestFill`/`WingLeg`/
  `WingBatch` counts, `rest_remaining`, `rest_booked_by_coid`, `amend_cross_pending` are now Decimal.
- `_book_rest_delta`, `_apply_cancelled`, `_apply_fill`, `_apply_amended`, `book_late_rest_fill` quantise
  via `_q_count`; the cumulative->delta arithmetic and the per-fill wing batches are fractional-safe. A
  0.44 fill of a 2-lot rest -> a 0.44 wing batch and `rest_remaining` 1.56 (the "never two live rests" and
  size-N partial-fill invariants hold with fractional booked amounts).
- RESTS stay WHOLE on the wire (**fail-closed, #106 N1**): `_rest_size` returns the EXACT Decimal
  remainder (exposure/arithmetic truth), but a new `_rest_place_count` FLOORS it to whole lots for the
  PLACE/AMEND action count (the executor place/amend bodies are integer-only). `_requote` skips the
  place/amend entirely when `_rest_place_count < 1` (a sub-lot-only remainder), so a count-0 order is
  NEVER sent; a sub-lot remainder keeps resting on the original venue order, or is dropped off the book if
  a requote tears that order down (under-exposed, never naked or over-sized). **I did NOT make rests
  fractional on either roster** — the executor's `_rest_body`/`_amend_body` stay int on both bases, per
  N1. The decision mirrors V3.3 exactly.

### driver (`service/run_v32.py`)
- `_count_out`/`_count_dec` added (import `_q_count` from core). `on_fill` parses `payload["count_fp"]`
  as Decimal (fallback to the parsed frame count, then the placed count); `on_poll_fill` takes the Decimal
  cumulative and feeds the Decimal delta; `_record_fill` is Decimal; the status poll call site feeds
  `st.filled_count_fp` (not the int). `parse_fill` keeps the `count` field Decimal (the shared parser no
  longer manufactures the int mirage).
- `_journal_action` wraps PLACE/AMEND/TAKE_WINGS/RETRY counts in `_count_out` (whole -> bare int ->
  byte-identical journal).
- money-math: `_fee_total` and `_fill_total_fee` are Decimal/count-weighted (a 0.44 fill's fee is
  `ceil(0.07*p*(1-p)*0.44)`; the count==1 shortcut stays for whole-1 byte-identity); the batch-bucket
  walk, held legs, `v32_set_floor_dollars`, cost, `rest_fills`, `lots_filled`/`lots_unfilled`,
  `fills_on_amend` are all Decimal + count-weighted and serialised bare-int when whole.

### ledger (`service/v32/ledger.py`) / report (`service/v32/report.py`)
- `_dc`/`_co` (ledger) and `_cnt` (report) parse persisted counts Decimal-safe; the floor, the S4
  pessimistic/optimistic band (`v32_pending_credit`), `_v32_floor_booked_for_entry`, and the report
  reconciliation stay count-weighted. Old integer ledger rows still parse; whole counts serialise bare
  int. `settlement_payoff` (shared `service/ledger.py`) was ALREADY `Decimal(str(count)) * $1` — just
  fed the now-fractional held-leg counts.

## Invariants (asserted in the tests)
- A 0.44 fill of a 2-lot rest -> `TAKE_WINGS` count 0.44 (both legs 0.44), `rest_remaining` 1.56,
  `rest_live.count` 1.56, not allotment-done.
- Fractional fills summing to the allotment latch `rest_allotment_done` (1.56 - 0.56 ... reaches 0).
- A cumulative cancel-confirm books only the fractional DELTA over `rest_booked_by_coid`.
- A sub-lot-only remainder never emits a count-0 (re)place/amend (`_rest_place_count` floors to 0 ->
  `_requote` skips).
- WHOLE counts serialise as bare ints everywhere (journal, ledger, money-math) — byte-identical.

## Tests
`tests/test_v32_fractional_counts.py` (15 new): core partial/allotment/cancel-delta/sub-lot-fail-closed;
driver WS fill + poll (fraction booked + journalled); fake-proxy executor cancel-confirm via status fp AND
reduced_by; the wing wire body "1.44"/"2.00"; `parse_order_status` keeps the int field; money-math
count-weighted (floor 0.88 for a 0.44 pin) and bare-int for whole lots.
Updated `tests/test_v33_fill_discovery_fractional.py::test_v32_cancel_confirm_stays_int_byte_identical`
(asserts the NEW law: flag True, WHOLE cancel-confirm journals a bare int) and the shared-parser
assertion in `tests/test_v33_fill_attribution.py` (now that `parse_fill` returns the exact fraction;
run_v33's `on_fill` reads `payload['count_fp']` directly so its behaviour is unchanged).

`cd pilot && python -m pytest -q` -> **1452 passed, 1 skipped** (baseline was 1437 passed / 1 skipped;
+15 new). No existing whole-lot test changed behaviour (byte-identity preserved via `_count_out`/`_co`).

## Registration (appended, append-only, below `## Registration`)
`2026-10-01 -- MECHANICS CLARIFICATION (fractional contract counts)`, Brad verbatim "Then start the build
on the fractional.", registered at **n=23** (current live-ledger completed-set count, read read-only from
a scratchpad copy). No `[pin]`, no threshold, no sha change, no STATUS change. Mirrored in
`pilot/ops/V32_ARMING.md` item 11.

## What the reviewer must scrutinise
1. **Whole-count byte-identity** — the whole point. `_count_out`/`_co`/`_count_num` must render an integral
   count as a bare `int` (not `"2.00"`), or dry/armed journals, ledger rows, parity and golden tests
   drift. The full suite passing (1452/1, same whole-lot behaviour) is the guard; re-confirm any newly
   journalled count field.
2. **The fail-closed sub-lot rest decision** (`_rest_place_count` + the `_requote` guard). Confirm a
   fractional remainder is NEVER sent as a fractional or count-0 (re)place/amend, is never left naked, and
   never over-exposed. This is the one deliberate design choice (matches #106 N1); if whole-lot resting is
   ever relaxed to fractional rests, `_rest_body`/`_amend_body` on BOTH sync bases must change together.
3. **`_fee_total` / `_fill_total_fee` fractional branch** — a sub-lot (0.44) fill now uses the law total
   `ceil(0.07*p*(1-p)*count)`, not the whole-contract per-contract `fee` (the `count <= 1` shortcut became
   `count == 1`). Confirm whole-1 is byte-identical and the fractional fee is right.
4. **`_fractional_counts` default True on the shared base** — V3.3 (`V33LiveExecutor`) still sets it True,
   so no V3.3 behaviour changes; `FrozenExecutor` (dry) does not read it. The duplicate `_dc`/
   `_count_body_str` left on `V33LiveExecutor` are now redundant (base copy serves V3.2); I left them to
   avoid touching the frozen V3.3 executor — confirm that is acceptable or delete them.
5. **The two cross-roster test edits** — the byte-identity test now asserts the new law, and the shared
   `parse_fill` assertion in `test_v33_fill_attribution` was updated because the shared parser is now
   fractional. Confirm run_v33's `on_fill`/`on_poll_fill` behaviour is genuinely unchanged (it reads
   `payload['count_fp']` / `filled_count_fp` directly; the `pf['count']` fallback now also returns the
   fraction, consistent).
6. **Economics count-weighting end to end** — floor, cost, fees, settlement backfill, and the S4 band all
   scale by the fractional count; a complete fractional pin's guaranteed floor = `(held-1) * count`.
   Confirm no path multiplies a per-contract value by an int-truncated count.
