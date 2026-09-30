# V3.3 fill-attribution fix — build report (D1–D4)

Branch: `fix/v33-fill-attribution` (from `origin/main` @ `dcf2af2`).
Scope: the 2026-09-30 22:00Z FIRST-LIVE-FILL incident (Registration entry 21:50:55Z, defects D1–D4).
V3.3 is DRY; this fixes the defects so it can be re-armed by Brad's hand after review.

House law kept: `python` only; no network/proxy (tests use fixtures/fakes); no `.env`/`*.pem`; no
`sim/out/sealed_eval/**`; the SEAL (2026-08-02..18) is untouched (the only real data read is the
2026-09-30 journal, read-only, into `tests/fixtures/v33/`). `policy/v33_params.json`, the params sha,
the falsifier STATUS line, and everything above `## Registration` are untouched. The executor's HTTP
transport (`service/proxy_writer.py`, `service/v33/executor.py`) is **untouched** to minimise conflicts
with the concurrent `feat/v33-async-writer` build — see "Merge notes" and the one flagged follow-up.

## The cause (from the journal `pilot/journals_v33/20260930T220000Z.jsonl.gz`)

Ground truth cut into `tests/fixtures/v33/incident_20260930T220000Z_slice.jsonl`:
the two rungs rested on `KXBTC-26SEP3018-B83650` (bucket_Sd 83600); the venue filled coid `-312` @0.47
for `count_fp "1.00"` and coid `-313` @0.46 for `count_fp "0.44"` = 1.44 NO. ~1.7 s later spot committed
to 83700 and the bucket-change `_cancel_all(track_outstanding=True)` tore the ladder down; the fills
surfaced ~7 s later via the cancel-confirm path. At that moment `_book_rung_fill` could no longer find
the order in the ladder and `rest_bucket_Sd` was None (awaiting_replace), so it **fell back to the
current `spot_Sd` = 83700** and hedged the wrong bucket. Separately the driver int-truncated `count_fp`
(0.44 → 0 → the placed lot), and the missing NO wing was retried on **every tick** (967 IOC creates in
~96 s, blocking the loop ~60 s).

## The fixes

### D1 — fill-to-bucket attribution never reads the current spot bucket (`service/v33/core.py`)
- `cancel_ctx` extended from `(coid, price, rung, E_rung)` to `(coid, price, rung, E_rung, bucket_Sd)`;
  `_remember_cancel_ctx` records `order.bucket_Sd`. This is the retained record for a rung removed from
  the ladder whose cancel has not yet confirmed.
- `_book_rung_fill(..., *, retained_Sd=None, fill_ticker=None)` resolves the fill's bucket in order:
  1. the live order's own `bucket_Sd` (normal path);
  2. `retained_Sd` — from `cancel_ctx` (the incident path) or the live order (amend-cross, WS);
  3. the fill's own market ticker inverted via `_sd_for_bucket_ticker(st, fill_ticker)` (last resort);
  4. **None → fail-closed**: the RungFill is still booked (the held NO leg is accounted) with
     `bucket_Sd=None`, **no wings are taken**, `bucket_unknown` latches, the hour stands down, and a
     `STAND_DOWN` with reason `fill_bucket_unknown` is emitted. The pre-fix `rest_bucket_Sd or spot_Sd`
     fallback is gone.
- `_apply_cancelled` unpacks the 5-tuple and passes `retained_Sd`; `_apply_fill`/`_apply_amended` pass
  the live order's `bucket_Sd` (and, on the WS path, `event.market_ticker`).
- The fill's-own-ticker channel is carried by a new **`V33Fill(Fill)`** subclass in
  `service/v33/events.py` (a v33-only event the module docstring sanctions; `isinstance(_, Fill)` still
  holds, so `decide_v33` dispatch is unchanged). The driver builds `V33Fill` with the market ticker
  (`on_fill` from the frame, `on_poll_fill`/dry-sim from `rec.ticker`/the rest bucket).

### D2 — wing strikes solved from the FILL's bucket, not spot (`service/v33/core.py`)
- `WingBatch` carries `bucket_Sd` (stamped at coalesce-flush from the fills, or at the print-through
  trigger). `_batch_bucket()` returns the batch's (Sd, Su); `_wing_prices_for_bucket()` reuses the
  **unchanged** V3.2 `_wing_prices` by viewing the state with `spot_Sd/Su` = the fill's bucket.
- `_wing_step` now prices and takes/retries **per batch** on that batch's own bucket; `_take_batch` /
  `_retry_batch` take `(sd, su)` and key the leg tickers to `st.strike_tickers[sd]` / `[su]`. The
  print-through take path is keyed to the rest bucket too. Single-bucket (dry) behaviour is unchanged
  because the batch bucket equals spot there.

### D3 — fractional counts parsed exactly, end-to-end
- Counts are Decimal through `RestOrder.count`, `RungFill.count`, `WingLeg.count`,
  `WingBatch.total_count/taken_count`, the exposure helpers, and the ladder invariant (floor relaxed to
  `0 < count ≤ max(rung_lots)` so a fractional partial can rest). `_q_count` normalises to 2dp and keeps
  a whole count a bare integer Decimal.
- Driver (`service/run_v33.py`): `on_fill` parses the raw `count_fp` as Decimal (fallback to `count`,
  then the placed lot) — replacing `int(pf.get("count") or 0) or rec.count`; `on_poll_fill` feeds the
  Decimal delta. `_count_out` serialises integral counts as `int` (so **dry journals stay
  byte-identical**) and fractional counts as the 2dp Decimal string; it wraps every count in the
  journalled payloads.
- Ledger (`service/v33/ledger.py`) and report (`service/v33/report.py`): all `int(count)` truncations
  replaced with Decimal-safe parsing (`_dc`/`_cnt`) and `_co`/`_count_num` for output; the per-margin
  and allocation tables are count-WEIGHTED; old integer rows still parse. `v32_set_floor_dollars`
  already multiplies by `Decimal(str(count))`, so it was reused unchanged.

### D4 — wing retry cadence bounded (`service/v33/core.py`)
- New module constant `WING_RETRY_MIN_INTERVAL_MS = 250`. `WingLeg.last_retry_ts` gates a re-fire: a leg
  is skipped while it was retried < 250 ms ago (and only ever while `status == "unfilled"`, so one retry
  is in flight per leg). `WingBatch.retries` counts emissions for the journal/report. The lock-floor gate
  and the T-`no_orders_after_s_to_settle` cutoff are preserved.

Also (D1 ledger/report): the held/unsettled NO leg is priced on the fill's own bucket ticker
automatically, because `RungFill.bucket_ticker` is now the correctly-attributed bucket. No
`tools/restate_v33_incident.py` was needed; new rows are right. `_ladder_summary`/report note a row where
`rung_fills` bucket differs from `held_legs` bucket via the shared `_bucket_ticker_for_fill` path.

## Invariants (asserted after every event in the tests)
- `_ZERO < o.count ≤ max(rung_lots)` per resting rung; `filled + resting contracts ≤ sum(rung_lots)` —
  both Decimal, conserved through a fractional partial.
- A taken batch has exactly two legs sized to its (Decimal) total.
- Attribution total is preserved: 1.00 + 0.44 = 1.44 NO, hedged on the 83600 bucket.
- Fail-closed: an unnameable bucket books the rung, takes no wings, and stands the hour down.

## Tests
`tests/test_v33_fill_attribution.py` (8 tests) reproduces the incident from the real fixture:
- fixture provenance = B83650, count_fp 1.00/0.44;
- D3 `count_fp` parsed exactly (and the old `int(...) or rec.count` mirage pinned);
- D1 late fill via cancel-confirm books on bucket 83600 (79600 analogue), not spot 83700 (79700), with
  the retained `cancel_ctx[...][4] == 79600`;
- D2 wings solved on the fill's strikes (STK_SD/STK_SU), count 1.44, **not** the spot strikes;
- D1 fail-closed on an unnameable bucket (`fill_bucket_unknown` stand-down, no wings);
- D1 ticker-inversion last resort;
- D4 retry gated to ≥ 250 ms per missing leg (≤ 3 retries over 30 rapid ticks vs one-per-tick);
- D3 a fractional partial leaves a fractional (0.56) resting remainder, invariant holds.

`cd pilot && python -m pytest -q` → **1389 passed, 5 skipped** (baseline was 1381 passed / 5 skipped;
+8 new). No existing test changed behaviour (dry byte-identity preserved via `_count_out`).

## What the reviewer must scrutinise
1. **Executor wing-take sizing is NOT fixed here (flagged follow-up, belongs to the executor branch).**
   `service/v33/executor.py::_take_wings` still does `int(lg.count)` and `ceil(remaining/wing_cap)` with
   integer aggregation (also `_pt_taker_entry`, the complete/unwind chunking). With a fractional wing
   count (1.44) an armed send would `int(1.44)=1` and **under-hedge**. It was left untouched on purpose:
   the parent scoped me out of the executor's HTTP transport to avoid conflicts with
   `feat/v33-async-writer`, and V3.3 is DRY (the executor sends nothing) until Brad re-arms. This MUST be
   fixed on the executor-owning branch before V3.3 re-arms with fractional fills; confirm the wing order
   body sends `count` as a decimal string and chunks `ceil(count/cap)`.
2. **`V33Fill` subclass** — confirm every `isinstance(event, Fill)` still matches (it does; subclass) and
   that no code path constructs a plain `Fill` where the ticker last-resort is needed.
3. **`cancel_ctx` 5-tuple** — every producer (`_remember_cancel_ctx`) and consumer (`_apply_cancelled`)
   updated; no other unpack sites (grep clean).
4. **`_q_count` / `_count_out` byte-identity** — a whole count must serialise as a bare int (not
   `"2.00"`), or dry journals/goldens drift. The full suite passing is the guard; re-confirm on any new
   journalled count field.
5. **Per-batch wing pricing** — `_wing_step` now skips a batch whose bucket has no fresh strike book
   instead of returning for all batches; confirm this never starves a legitimate take (single-bucket dry
   is identical).
6. **Report count-weighting** — the per-margin/allocation tables are exactly count-weighted; the coarse
   summary means use whole-lot replication of the integer part (documented approximation for a rare
   fractional partial; the falsifier `n` and all totals are exact Decimal).

## Merge notes vs `feat/v33-async-writer`
- Files changed: `service/v33/core.py`, `service/run_v33.py`, `service/v33/events.py`,
  `service/v33/ledger.py`, `service/v33/report.py`, plus the new test + fixture. **No** change to
  `service/proxy_writer.py` or `service/v33/executor.py`.
- The only likely overlap is `service/run_v33.py` (both branches touch the driver). My run_v33 changes are
  confined to `on_fill`/`on_poll_fill`/`_simulate_ladder_fills` count parsing, the `_count_out`/`_count_dec`
  helpers, the `_journal_action` count fields, and the `V33Fill` import — all in the decision/journaling
  seam, not the writer transport. Resolve by keeping both: the async-writer's transport edits and these
  count/attribution edits are orthogonal.
- Executor follow-up (item 1) should land on whichever branch owns `executor.py` after this merges.

---

## Round 2 (review verdict REQUEST CHANGES -> addressed; F1 executor wing-send already merged @ ebcd957)

Reviewer doc: `pilot/build/v33_fill_attribution_review.md`. Full suite after round 2: **1401 passed, 5
skipped** (was 1392/5 at the review; +9: 6 fill-discovery pipeline, 3 wing-netting). V3.2 byte-identity
re-confirmed (`test_v32_executor.py` 21 passed; the full v32/v33/executor set green).

### F2 [BLOCKING, fixed] — fill DISCOVERY no longer truncates count_fp to int (the incident's own path)
The inherited V2 executor stamped `OrderStatus.filled_count: int` and the cancel/ poll paths re-`int()`-ed,
so a 0.44 leg reached the core as `Decimal(0)` and was LOST on the cancel-confirm/poll paths — even after
the core D3 fix. Fixed V3.3-SCOPED, V3.2 byte-identical:
- `service/v32/executor.py`: added `OrderStatus.filled_count_fp: Decimal` (parsed alongside the unchanged
  int), a class flag `LiveExecutor._fractional_counts = False`, and threaded an optional Decimal
  `filled_fp` through `_finish_cancel` / `_resolve_cancel_success` / `_resolve_cancel_from_status` / the
  rest-invariant fill branch + `_confirm_cancel_filled`. With the flag OFF (V3.2) every value, journal and
  event is byte-identical to before (verified by the v32 suite); ON (V3.3) the OrderCancelled and the
  `cancel_confirmed` journal carry the exact fraction.
- `service/v33/executor.py`: `V33LiveExecutor._fractional_counts = True`; `poll_orders_for_bucket` now
  returns `dict[str, Decimal]` (no `int()`).
- `service/run_v33.py`: the poll call sites drop `int()` and feed the Decimal (batched) /
  `stt.filled_count_fp` (per-rung) to `on_poll_fill`.
- Test `tests/test_v33_fill_discovery_fractional.py` (6): the exact-fraction parse; the cancel confirm
  surfacing 0.44 (via status AND via `reduced_by`); the poll returning Decimal; the V3.2 gate staying
  int/byte-identical; and an END-TO-END pipeline (executor-produced `OrderCancelled(0.44)` -> core ->
  RungFill 0.44 -> wings sized 0.44 on the fill's bucket strikes).

### D5 [NEW, Brad's question, implemented] — netted wings across adjacent buckets
When set A's NO@Su-strike and set B's YES@Sd-strike are one market (adjacent buckets), the venue nets the
pair to flat and credits $1/contract. Implemented:
- `service/v33/core.py`: `WingLeg.netted: Decimal`, `NettedPair`, `V33State.netted_pairs`, and
  `_net_wings` (called from `_apply_fill` when a wing leg fills) — nets `min(counts)` of an opposite-side
  FILLED leg on the SAME ticker, books the pair CLOSED, records `NettedPair` (realised = count*(1 - yes_cost
  - no_cost)), and emits an INFORMATIONAL `V33ActionKind.WING_NETTED` (no venue order). Handles PARTIAL
  overlap (1.44 NO vs 2.00 YES -> 1.44 netted, 0.56 YES held). Dormant in single-bucket operation (no
  opposite-side leg shares a strike), so every existing test is unaffected.
- `service/run_v33.py`: journals `wing_netted`; the pump does NOT route it to the executor.
- `service/v33/ledger.py`: a fully-netted wing leg is excluded from `held_legs` / `unsettled_legs` (no
  settlement lookup), the netted $1/contract is added to `floor_booked`, and a `netted_sets` block is
  carried on the row; the per-batch SOLVED `realized_lock` is unchanged (both batches complete).
  Also fixed two residual `int(lots_filled)` truncations in `build_v33_ledger_row` (D3).
- `service/v33/report.py`: per-row `[NETTED n = ...]` tag + a totals line.
- Test `tests/test_v33_wing_netting.py` (3): full netting (1 lot each) closes the shared strike (not in
  unsettled) with realised == Σ both sets' solved locks; partial overlap leaves 0.56 held.

### F3 [implemented] — the report now flags a bucket mismatch
`service/v33/report.py::_row_bucket_mismatch` + a `[BUCKET MISMATCH]` tag: a row where any
`rung_fills[].bucket_ticker` differs from the held bucket-NO leg ticker is flagged. Pre-fix incident rows
carry no `bucket_ticker`, so they are (honestly) NOT flagged — only a NEW divergence is.

### F4 [done] — deleted the dead `_pt_wing_asks` (D2 replaced its only caller).
### F5 [done] — moved `run_v33._count_out`/`_count_dec` out from between imports to the module body.

### Build-report note for the async twin (do NOT touch that branch)
`feat/v33-async-writer:pilot/service/v33/async_executor.py` carries the same F1 `int(lg.count)` wing-send
truncation (`:618/623/658/671`, `:245`, `:516`, `:788`) AND, separately, will need the F2 fill-DISCOVERY
fix if it has its own status/poll/cancel parse. Port both at integration. The transport-side wing
retry-storm belt (one in-flight IOC per (batch,side)) composes safely with the core D4 250 ms floor.
