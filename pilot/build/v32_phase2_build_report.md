# V3.2 Phase 2 build report — process spine, dry mode, task (`service/run_v32.py`)

Branch `v32/phase2-spine` (base origin/main `264672c`, which carries Phase 1). Builder: Opus 4.8.
Scope: the process spine that wires the pure Phase-1 core `decide_v32` onto the live recorder
pipeline in shakedown/dry, plus the scheduled-task scripts, the ledger, the report, and tests. No
orders are sent (the maker executor is Phase 3). Every network edge is injected; the suite uses fakes
only and never dials the proxy. No sealed/holdout date is read by any code path here (the one date
used in tests, 2026-09-13, is the current UTC day).

## What was built

All paths under the worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`:

* `pilot/service/run_v32.py` — the spine (deliverable A). Mode lever, discovery, connect gate, the
  two-connection WS run loop, the `V32Driver` + `FrozenExecutor` + `RestBook`, the ClockTick pump,
  finalize (gzip + summary + ledger), and `main()`.
* `pilot/service/v32/ledger.py` — `build_v32_ledger_row` / `append_v32_ledger_row` / `load_v32_rows`
  (deliverable C).
* `pilot/service/v32/report.py` — `python -m service.v32.report [--days N]` (deliverable D).
* `pilot/service/v32/core.py` — ONE additive function `book_late_rest_fill` (documented below);
  the pure decision law is otherwise unchanged. `__init__.py` re-exports it.
* `pilot/ops/v32_mode.txt` (contains `shakedown`), `pilot/ops/register_v32_task.ps1`,
  `pilot/ops/unregister_v32_task.ps1` (deliverable B).
* `pilot/ops/V32_DRY_RUN.md` — the dry-run runbook (deliverable G).
* `pilot/tests/test_run_v32.py` (21 tests), `pilot/tests/test_register_v32_task.py` (2 tests)
  (deliverable E).
* `.gitignore` — added `pilot/journals_v32/` and `pilot/logs_v32/`; `pilot/ledger/` (hence
  `v32_ledger.jsonl`) is already ignored, matching how `journals`/`logs`/`ledger` are handled today
  (deliverable F). `v32_mode.txt` is intentionally TRACKED (the committed default; a later flip shows
  as a working-tree change, exactly the visibility Brad wants on the live switch).

## WS topology decision + rationale — TWO connections

`run_v32` opens **two** WS connections on the one asyncio loop:

* strikes (KXBTCD, ~188 markets): `orderbook_delta` + `trade`.
* buckets (KXBTC, ~180 markets): `orderbook_delta` + `trade` (+ `fill` + `market_positions` only
  when armed — which never happens in Phase 2, see degrade).

Rationale (and what I measure to justify it): a lagging STRIKE feed poisons `W` (the taker-wing
price) while a lagging BUCKET feed poisons spot selection — two connections give an INDEPENDENT
`current_lag_seconds()` per stream, recorded as `strike_lag_seconds` / `bucket_lag_seconds` in every
summary and ledger row and rendered as `sLag`/`bLag` in the report, so an operator sees which stream
aged. The private channels belong only on the bucket connection (our resting order is a bucket-NO),
so a strike-book seq-gap re-dial never churns a private re-subscribe. Cost: two dial loops — each
driven by the proven `run_recording` supervisor via a thin `_ConnRecorder` adapter that delegates all
book-folding/journaling to one shared `V32Recorder`. Because both clients' synchronous callbacks run
on the single loop, the shared driver state needs no lock. A single ClockTick pump task drives the
cutoffs. All three run under one `asyncio.gather`, held until the connect gate
(`close − quote_start_s − 5 s`).

## FrozenExecutor simulation semantics (dry/shakedown)

The `FrozenExecutor` journals the WOULD_* order intents and SIMULATES the exchange's
acknowledgements so the requote state machine actually cycles without sending anything (all counters
tagged `synth`):

* `WOULD_PLACE_REST` -> record the order in the `RestBook` (status `live`, synthetic order_id
  `dry-<coid>`) and return an `OrderAck` -> the pending rest becomes live next event.
* `WOULD_CANCEL_REST` -> mark the RestBook entry `cancelled` (RETAINED, not deleted — F-1) and return
  an `OrderCancelled` (filled_count_before_cancel = 0; a dry cancel never fills).
* `WOULD_TAKE_WINGS` / `WOULD_RETRY_WING` -> return a `Fill` for each still-pending wing leg at its
  own limit, so a (synthetic or late) rest fill completes the $2 pin and `sets_done` advances.

The driver's `_pump` drains these synthetic events to quiescence (bounded by `_PUMP_GUARD`), so a
single book tick that triggers a requote2 replace produces `place -> ack -> replace(cancel+place) ->
cancelled -> ack` end to end. In ORDINARY dry the resting maker order never fills — a maker fill
cannot be honestly synthesized, so the SHADOW (the public-print fill rule, per E) answers "would we
have filled"; the wing-fill synthesis is reached only by a real fill (Phase 3) or the F-1 late-fill
path (tested). This is clearly labeled a simulation of the acks; it sends nothing and books no money.

## F-1 handling (retained cancel context — the Phase-1 review's #1 pre-arm item)

The order-tracking layer is the `RestBook` inside `FrozenExecutor`: `client_order_id ->
{order_id, price, count, ticker, bucket_Sd, placed_ts, status}`, RETAINED across cancel/replace for
the whole window (a cancelled entry stays, status `cancelled`). `V32Driver.on_fill` attributes every
fill through it:

* not ours (unknown coid/order_id) -> journal `foreign_fill_ignored`, drop (fail-closed).
* our currently-tracked coid -> a normal `Fill` into the core.
* our RETAINED (replaced/eagerly-cancelled) coid -> journal `late_fill` and book it through the
  additive core hook `core.book_late_rest_fill`, which sets `rest_fill` at the RestBook's RETAINED
  price (never the drifted `desired_n`), latches the one-set rule, forces the spot context to the
  FILLED bucket (`bucket_Sd`), and takes the wings. Idempotent once a rest fill is booked. This closes
  the exact gap the review named: a fill on a just-replaced order is no longer a silent untracked
  unhedged bucket-NO + a double-entry. Phase 2 subscribes the private `fill` channel only when armed
  (which degrades to dry), so no real fill arrives yet — the path exists and is tested with fakes,
  ready for Phase 3 to drive cancels through a status-confirmed path (the review's belt-and-braces).

## Deviations / decisions

1. **`armed` degrades to dry (Phase 2, by design).** `effective_mode_and_degrade("armed") -> ("dry",
   "phase2_no_executor")`; a `degrade_to_dry` record is journaled and the ledger row carries
   `mode=armed, effective_mode=dry, degrade="phase2_no_executor", armed=false`. There is no order
   path to arm yet.
2. **`state.shakedown = True` for BOTH shakedown and dry.** Both route through the FrozenExecutor and
   emit WOULD_* twins (the task's requirement). The two are distinguished in the ledger/journal by
   `effective_mode`, not by the action kind. Only Phase 3's armed sets `shakedown=False`.
3. **Additive core change.** `core.book_late_rest_fill` is the only core addition (F-1 hook); the pure
   `decide_v32` law is untouched. It forces the spot context to the retained bucket so the wings price
   off the FILLED bucket even after the live spot moved on — the safe reading; a fuller
   retained-bucket wing-retry that survives a subsequent `_recompute_context` overwrite is a Phase-3
   refinement (noted in Open items).
4. **Stand-down is stricter than the task's minimum.** The task requires standing down when no
   buckets are found and on the 21Z $250/$500 hours (bucket width != `params.bucket_width`). I ALSO
   stand down when the strike ladder is empty (`no KXBTCD strike ladder…`) — the pin wings are
   structural, so an all-strikeless window is a clean no-op rather than a window that quotes nothing.
5. **Trade-frame parser is best-effort/fail-closed.** `_trade_event` builds the YES-space price from
   `yes_price`, else `1 - no_price`, else `price` (all cents), and requires `taker_side` in
   {yes,no}; an unparseable trade is journaled `v32_trade_unparsed` and dropped. The exact live trade
   payload shape is a Phase-3 confirm item (no live trade was replayed here — the Phase-1 golden
   fixture's trade shape is `[ts, yes_price_cents, side, count]`, which this matches).
6. **ClockTick clock source.** The pump uses `driver.server_now()` = last observed server ts + local
   elapsed; no tick is driven before the first timestamped frame (fail-closed, no machine-clock truth
   source), matching the box_runner F5 discipline for book frames.

## pytest

```
$ cd pilot && python -m pytest -q
700 passed in 24.22s
```

(Phase-1 baseline was 674; +26 = 21 `test_run_v32` + 2 `test_register_v32_task` + shifts.) The new
tests cover: mode resolution (CLI/file/fail-closed), the shipped `v32_mode.txt`, armed->degrade,
params sha refusal, strike + bucket discovery adapters (floor map, exchange_index fail-closed,
dead-generation drop, half-populated-bucket drop, 21Z $250-width detection), connect-gate math,
FrozenExecutor cycling (place->ack->true-requote replace->cancelled->place with F-1 retention), F-1
late-fill attribution to a retained coid (booked, one set completes) + foreign-fill drop, ClockTick
cutoff (rest cancelled past quote_end in dry) + `server_now` None-before-first-frame, journal record
shapes, ledger row shape + shadow summary + append/load, report build over rows, both stand-down
rows (no buckets; degrade recorded), and a two-connection integration run (gather of two fake WS
clients + the clock pump) through `_finalize` (gzip + summary + ledger).

## Open items for Phase 3

* **Exact create/cancel/status payloads.** `book_late_rest_fill` and the FrozenExecutor assume
  PLACE_REST -> `{side:no, action:buy, price:n, count, post_only, TIF good_till_canceled,
  expiration_time = close - quote_end_s, exchange_index, client_order_id}`; CANCEL -> DELETE
  `/portfolio/orders/{order_id}` confirmed via GET status; TAKE_WINGS -> batch create, 2 IOC legs at
  ask+margin with per-leg `exchange_index`. These must be wired to the real proxy envelope in Phase 3.
* **Fill / market_positions channel shapes.** `_fill_event`/`on_fill` assume `{client_order_id,
  order_id, count, yes_price|no_price|price, side, ts}`. Confirm against a live private frame; drive
  cancels through a status-confirmed path so a partial-before-cancel arrives as
  `OrderCancelled.filled_count_before_cancel` (handled) and feed `Fill` only for a tracked-or-retained
  coid (both handled here).
* **Late-fill wing-on-old-bucket.** `book_late_rest_fill` forces the spot context to the filled
  bucket, but the next `_recompute_context` will re-select the live spot; Phase 3 should retain the
  completion's target bucket independently of live spot selection so a slow retry stays on the filled
  bucket.
* **Proxy prerequisites for arming (unchanged from PLAN_V32):** `ORDER_TICKER_PREFIXES` must cover
  `KXBTC` + `KXBTCD`; `DAILY_ORDER_BUDGET` >= ~4000; `MAX_CONTRACTS_PER_ORDER=2`. Phase 4's S5 check
  reads `/health` for these.

## Questions for Brad

1. **`deb_ms` in the frozen policy is 5000** (the plan's chosen default, applied by ruling M-1 on the
   Phase-1 review) — confirm this is the value the dry runs should represent before the falsifier is
   frozen in Phase 4.
2. **Trade/fill payload confirmation:** a single captured live `trade` frame and one `fill` frame
   from the proxy would let Phase 3 pin the exact field names instead of the best-effort parser here.
3. **`v32_mode.txt` is committed** (unlike the box's git-ignored `mode.txt`) so the shipped default is
   in the repo; flipping it will show as a tracked change. Say the word if you'd rather it be
   git-ignored like `mode.txt` (then run_v32 still defaults to shakedown when absent).
