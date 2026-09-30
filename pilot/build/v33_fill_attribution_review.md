# V3.3 fill-attribution fix — adversarial review (D1–D4)

Reviewer: Opus 4.8 (Fable-directed). Branch under review: `fix/v33-fill-attribution` @ `9102c17`
(commits `9135e27`, `73f5efa`, `9102c17` on `dcf2af2`). Review branch: `review/v33-fill-attribution`
from that HEAD. Worktree `dv3_wt_range`, detached→review branch. Full suite before my changes:
1389 passed / 5 skipped; after (my +3 executor tests): **1392 passed / 5 skipped** (`cd pilot && python -m pytest -q`).

## VERDICT: REQUEST CHANGES

The D1/D2/D4 core fixes and the D3 core/driver-WS/ledger/report work are **correct and well-tested** — I
verified each against the incident fixture and the adversarial cases. I fixed the executor **wing-SEND**
D3 gap the builder flagged (F1 below) on this review branch. But reviewing "as if money rides on it and
it re-arms after this lands", the deliverable is **not yet safe to re-arm** because of **F2**: a second
D3 truncation on the incident's own **cancel-confirm** discovery path that the core fix does not reach.
F2 is out of my assigned scope (it lives in the V2 executor shared with the LIVE V3.2 pilot), so I report
it rather than patch it. F3 is an overstated claim in the build report. F4–F6 are nits/notes.

---

## Findings

### F1 — [FIXED ON THIS BRANCH] executor wing-SEND truncated fractional hedges (the builder's flagged gap)
`service/v33/executor.py::_take_wings` sized the hedge with `int(lg.count)` and `to_v2_order` wrote the
wire `count` as `f"{int(count):.2f}"`. A 1.44-lot fill would send **1** wing lot and leave **0.44 NAKED**
once V3.3 re-arms. Same truncation in `_take_bucket_no` (print-through complete), `_unwind_wings`,
`_pt_taker_entry`, `_wing_chunk_entry`, and the `_wing_filled`/`agg_count` accumulators.

Fix (surgical, to those functions only): counts carried as Decimal; chunk as `ceil(count/cap)` with the
**LAST chunk fractional**; `Σ chunk counts == filled Decimal` asserted; wire `count` written via a new
`_count_body_str` that overrides `to_v2_order`'s int-count (exactly as `price` is already overridden) —
the venue accepts a 2dp decimal string and the proxy's `max_contracts_per_order` compares numerically
(per `pilot/ops/proxy_phase_h.md`: over-cap is a numeric `count:"9" > 2` → 403). Whole lots stay
byte-identical (`"2.00"`). New tests `tests/test_v33_wing_fractional_exec.py` (3) prove: fractional wire
body (`"1.44"` not `"1.00"`), full 1.44 hedge (not the int-truncated 1), and `[2.00, 1.44]` multi-chunk
with the last chunk fractional summing to 3.44. Existing executor/print-through/golden suites still green.

### F2 — [BLOCKING for re-arm; NOT fixed here — shared V2 executor / out of scope] fill-DISCOVERY still truncates count_fp to int
D3 is **not** end-to-end. The inherited V2 executor parses the venue's fractional `fill_count_fp` with
`_i(v) = int(Decimal(str(v)))` (`service/v32/executor.py:240`) and stamps `OrderStatus.filled_count: int`
(`:219`, `:256`, `:269`); the cancel path does `_finish_cancel(..., int(st.filled_count), ...)` →
`OrderCancelled(filled_count_before_cancel=Decimal(filled))` (`:805`, `:827`). `run_v33.py` then `int()`s
again at the poll call sites (`:603`, `:614`). So:
- **poll path** (`on_poll_fill`): a 0.44 poll-discovered fill → `0` → not booked; 1.44 → `1` → under-booked.
- **cancel-confirm path** (`_apply_cancelled`): the core's D3 Decimal handling receives an
  already-int-truncated `filled_count_before_cancel`. **This is the incident's own path** — the fills
  surfaced via the cancel confirm (the WS fill arrived 109 s late, and `_apply_fill` drops a fill whose
  order is no longer in the ladder, so in the armed incident the ONLY booking of the fractional leg is the
  cancel-confirm). In production the 0.44 leg would arrive as `Decimal(0)` and be lost.

The core test `test_late_fill_via_cancel_confirm_...` injects `OrderCancelled(..., Decimal("0.44"))`
directly, bypassing the truncating executor — so it proves the core, not the armed pipeline. The build
report's "the driver parses count_fp (WS fill, **poll delta, cancel-confirm fill**)" holds only for the
WS path.

Why I did not patch it: `_i`/`order_status`/`_finish_cancel` are in `service/v32/executor.py`, **shared
with the live-armed V3.2 pilot**, and the parent scoped me to the wing-SEND transport, not fill-discovery.
Changing the V2 parse blindly risks V3.2. **Required before re-arm**, on the executor-owning branch: a
v33-scoped path (override `order_status`/the cancel resolution, and `poll_orders_for_bucket`'s
`dict[str,int]`) that preserves `fill_count_fp` as Decimal end-to-end, and drop the `int()` at
`run_v33.py:603,614`. Same severity class as F1, arguably higher (the incident's own path).

### F3 — [build-report claim overstated; not a code defect] the report does NOT flag the bucket mismatch
The build report says "`_ladder_summary`/report note a row where `rung_fills` bucket differs from
`held_legs` bucket via the shared `_bucket_ticker_for_fill` path." No such flag exists — `grep` for
mismatch/differ logic in `report.py`/`ledger.py` is clean, and `_bucket_ticker_for_fill` only *derives*
the held-leg ticker, it never compares. I ran `service.v33.report` READ-ONLY on a scratchpad copy of the
live ledger (168 rows): the incident row **renders** correctly —
`2026-09-30T22:00:00Z armed KXBTC-26SEP3018-B83750 … [REALISED (armed, real money)]` — but is **not
flagged**. It cannot be from the row alone: the pre-fix `rung_fills` carry no `bucket_ticker`, and the
row's `held_legs`/`spot_bucket_ticker` are on the wrong bucket B83750 (Sd 83700) while the fills were on
B83650 (Sd 83600). The fix itself is sound; only the monitoring claim is unsubstantiated. (An operator
should not rely on the report to catch a repeat.)

### F4 — [nit] `_pt_wing_asks` is now dead code
D2 replaced the `_print_through_step` call to `_pt_wing_asks` with `_wing_prices_for_bucket(st, rest_sd,
rest_su, …)`. `_pt_wing_asks` (`core.py:1481`) is now defined but never called (grep clean). I confirmed
the two are **behaviourally identical in the gated path** (`_pt_wing_asks` was just
`return _wing_prices(st,…)`, and `_wing_prices_for_bucket` is `_wing_prices(replace(st, spot_Sd=rest_sd,
spot_Su=rest_su),…)` which is a no-op when `rest_sd == spot_Sd`, i.e. the gated path) — so D2 changed no
print-through pricing behaviour and is correct when spot drifts. Suggest deleting the dead function.

### F5 — [nit] `run_v33._count_out`/`_count_dec` defined between import statements
They sit between `from service.v33.core import …` and `from service.v33.executor import …` (module-level
imports interleaved with defs, PEP8 E402). Works (Decimal/Any are imported above), but belongs with the
other module helpers.

### F6 — [note, item 4] async twin needs the same F1 fix; retry composition is safe
`feat/v33-async-writer:pilot/service/v33/async_executor.py` (fetched read-only, NOT merged) carries the
identical `int(lg.count)` truncation in `_take_wings_send` (`:618`, `:623`, `:658`, `:671`), plus
`int(action.count)` in place (`:245`), `int(action.count)` amend (`:516`), and `want = int(action.count…)`
in `_take_bucket_no` (`:788`). The F1 fix must be ported at integration. The transport-side **WING
RETRY-STORM BELT** there (one in-flight IOC per `(batch,side)`; drop a retry for a leg already in flight,
release on IOC return — `:121`, `:583`) **composes safely** with the core D4 250 ms floor: the belt only
drops while a real IOC is outstanding and releases on return, while the core re-emits at ≥250 ms whenever
`status == "unfilled"` — no double-gate stall (a dropped retry is re-emitted by the core next floor tick),
no deadlock. Confirm at integration with both gates live.

---

## What I verified as CORRECT (receipts)

- **D1 attribution chain**: `_book_rung_fill` resolves bucket as order.bucket_Sd (live) → `retained_Sd`
  (cancel_ctx 5-tuple / live order) → fill's own ticker inverted (`_sd_for_bucket_ticker`) → **None ⇒
  fail-closed**: RungFill booked with `bucket_Sd=None`, **no wings**, `bucket_unknown` latched, hour stood
  down with `STAND_DOWN reason=fill_bucket_unknown`. The pre-fix `rest_bucket_Sd or spot_Sd` fallback is
  **gone** from `_book_rung_fill` (grep). The residual `rest_bucket_Sd…else spot_Sd` at lines 1501/1535/
  1633/1761 are the print-through pre-hedge paths, now primary-keyed by `batch.bucket_Sd` and gated to
  spot==rest — defensible. `cancel_ctx` 5-tuple: one producer (`_remember_cancel_ctx`), one consumer
  (`_apply_cancelled`), grep clean. `V33Fill(Fill)` subclass keeps `isinstance(_, Fill)`.
- **D2**: wings solved on the FILL's bucket strikes (`STK_SD`/`STK_SU` for B83650 = T83599.99/T83699.99),
  sized 1.44, batch `bucket_Sd==79600`; per-batch skip of a stale-book bucket does not starve other
  batches (single-bucket dry identical). Matches the V3.2 `_wing_prices` law reused via `replace(st, spot_Sd=…)`.
- **D3 core/ledger/report**: Decimal through RestOrder/RungFill/WingLeg/WingBatch, exposure invariant
  relaxed to `_ZERO < count ≤ max(rung_lots)` (fractional partial can rest), `_q_count`/`_count_out`/`_co`/
  `_count_num` keep whole counts **bare ints** (dry byte-identity), report tables count-WEIGHTED, old int
  rows still parse. Driver WS `on_fill` reads raw `payload["count_fp"]` as Decimal.
- **D4**: `WING_RETRY_MIN_INTERVAL_MS=250`, `last_retry_ts` gate, one-in-flight via the `status=="unfilled"`
  guard, lock-floor + T-`no_orders_after_s_to_settle` cutoff preserved. Test: ≤3 retries over 30×10 ms
  ticks (vs the incident's 967).
- **Dry byte-identity**: no golden fixture changed (`git diff --stat` clean of goldens); golden suite green.
- **Registration**: appended a single dated CORRECTION under `## Registration` (append-only; original
  21:50:55Z entry and everything above untouched) noting the count_fp/coid pairing is reversed — the
  journal has coid **-312 = 1.00**, **-313 = 0.44** (total 1.44 unchanged), the entry had them swapped.

## House law
`python` only; no `.env`/`*.pem`; no `sim/out/sealed_eval/**`; the SEAL (2026-08-02..18) untouched; no
network/proxy; no process kills; params JSON/sha, falsifier STATUS and everything above `## Registration`
untouched; the only live-tree access was READ-ONLY (ledger copied to scratchpad; `async_executor.py`
fetched read-only, not merged). No push to `main`.
