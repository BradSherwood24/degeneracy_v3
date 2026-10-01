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

---

# Round 2 review (fix @ `7d37c0f`; review branch `review/v33-fill-attribution-r2`)

Reviewed b100da3 (F2 fill-discovery Decimal), 64ac380 (D5 netting + F3/F4/F5), 7d37c0f (tests + report).
My r1 fix (`ebcd957`) is merged. Full suite on my r2 branch: **1402 passed / 5 skipped** (builder was
1401/5; +1 my D5 backfill regression test).

## VERDICT: REQUEST CHANGES — one verified money bug in D5 (fixed on this branch), everything else APPROVED

F2, F3, F4, F5 and the V3.2 byte-identity are all correctly addressed. The NEW D5 netting feature has a
settlement-accounting bug that loses exactly the netted $1/contract at backfill. I fixed it on
`review/v33-fill-attribution-r2` with a regression test; it must be merged before D5 is trusted (armed).

### R2-F1 — [BUG, fixed on this branch] D5 netted $1 is lost at the settlement backfill
`compute_ladder_money_math` adds the netted credit to `floor` (→ the row's `floor_booked`), but the
netted market is EXCLUDED from `held_legs`/`unsettled_legs`. The backfill books the correction
`realized = settlement_payoff(held_legs) - floor_booked`, and since `floor_booked` includes the $1 while
`payoff` does not, the correction silently removes it. I traced a 1-lot adjacent-bucket net end to end:
- close `floor_booked = 3` (two 2-leg boxes @ $1 + $1 netted), `realized_delta(close) = -1.0004` (= 3 − cost 4.0004);
- held-leg settlement payoff = **3.00**, backfill `floor_netted = 3`, so `realized_delta(bf) = 3.00 − 3 = 0.00`;
- window total = close + correction = **−1.0004**, but the TRUE economics are held $3 + netted $1 − cost $4.0004 = **−0.0004**. Off by exactly the netted $1.

Fix (`service/v33/ledger.py::v33_settlement_backfill_sweep`): add the row's `netted_sets` count back to
the settlement payoff before the correction (the netted markets never settle for us, so the credit is
fixed, not a lookup). After the fix the same trace gives `realized_delta(bf) = 1.00`, total = **−0.0004**
— correct. Regression test `tests/test_v33_wing_netting.py::test_netted_dollar_survives_the_settlement_backfill`
drives the state through close + backfill and asserts the backfill payoff includes the netted credit and
the window total = `held_payoff + netted − cost`.

### F2 — [APPROVED] fill DISCOVERY now Decimal end-to-end, V3.2 byte-identical
- `parse_order_status` adds `filled_count_fp: Decimal` alongside the UNCHANGED `filled_count: int`; I
  verified the int `filled` resolution block is textually unchanged from main (the diff only appends the
  fp block + field) so `filled_count` is identical for every input (fractional "0.44"→int 0, "1.00"→1,
  old-shape `fill_count` with no `_fp`→2, "1.44"→1). Old-shape (no `fill_count_fp`) falls back correctly.
- `_fractional_counts` gate: FALSE on `LiveExecutor` (V3.2), TRUE on `V33LiveExecutor`. With it OFF,
  `_finish_cancel`'s `val = filled_fp if not None else filled` is the bare int and the OrderCancelled is
  `Decimal(filled)` — byte-identical to main (I read the journal branch by hand; the v32 suite's
  `test_v32_cancel_confirm_stays_int_byte_identical` asserts `isinstance(...,int)`; full v32 suite green).
  A V3.2 cancel carrying `fill_count_fp "0.44"` yields `filled=0` → OrderCancelled(0), journal 0 — exactly
  main's behaviour.
- `poll_orders_for_bucket` returns `dict[str, Decimal]`; `run_v33` poll call sites drop `int()` and feed
  `fc` / `stt.filled_count_fp`. The end-to-end test drives the fraction from a FAKE PROXY response (not an
  injected event) through the executor's cancel → OrderCancelled(0.44) → core → RungFill 0.44 → wings 0.44
  on the FILL's bucket strikes. Thorough.

### D5 netting — other checks (APPROVED beyond R2-F1)
- (a) Partial overlap: `_net_wings` nets `min(avail_L, avail_M)`; 1.44 NO vs 2.00 YES → 1.44 netted, 0.56
  YES held (test + ledger `held_ct = count − netted`).
- (b) A netted leg keeps `status == "filled"` (only the separate `netted` field grows), so the one-leg
  retry (`_retry_batch`, gated on `status == "unfilled"`) and print-through complete/unwind never re-take
  it, and `_maybe_close_set` counts it filled. No path reads `netted` to re-take. Netting only ever fires
  across ADJACENT buckets (Su_A == Sd_B, opposite sides) — within one bucket YES@Sd and NO@Su are
  different tickers — so it is dormant single-bucket (every existing test unaffected; full suite green).
- (c) Fees: the taker fee on BOTH wing buys is in `cost` (`_fee_total`, exact per-fill), so the realised
  money (`floor − cost`, now correctly incl. the netted $1) is net of fees. The informational
  `NettedPair.realised = q*(1 − yes_cost − no_cost)` includes a per-contract `fee()` approximation; the
  money truth is `floor − cost` — no double-count of the netted legs' fees.
- (d) Reconcile/stops: netted markets are position-0 and excluded from `held_legs`/`unsettled_legs`, so
  the backfill never looks them up and `v33_pending_credit` (the S4 band) never counts them as pending (a
  netted $1 is realised, not pending — correct). There is no V3.3 venue-position reconcile that would flag
  a flat market as inherited/unknown (grep: the only "reconcile" refs are core rung/n_top, not positions).
- (e) Window boundary: `netted_pairs` lives on the per-window `V33State` (fresh each window) and both sets
  are in the SAME window (bucket moved mid-window); hours settle at close, so no cross-window netting.

### F3 / F4 / F5 — [APPROVED]
- F3: `report._row_bucket_mismatch` + `[BUCKET MISMATCH]` flags a row whose `rung_fills[].bucket_ticker`
  is not among the held NO bucket tickers, with an honest caveat that pre-fix rows carry no bucket_ticker
  so only a NEW divergence is flagged. It does not false-flag D5 netting (the bucket-NO range legs are
  never netted, only the strike-wing legs).
- F4: `_pt_wing_asks` deleted (grep clean).
- F5: `_count_out`/`_count_dec` moved below the `logger` definition, out from between the imports.

House law kept (r2): `python` only; no `.env`/`*.pem`/`sealed_eval`; SEAL untouched; no network/proxy; no
kills; the only fix touches `service/v33/ledger.py` (V3.3 backfill) + a test — not the live-shared V2
executor path, not params/sha/STATUS. Worktree left detached + clean; pushed to
`review/v33-fill-attribution-r2`.
