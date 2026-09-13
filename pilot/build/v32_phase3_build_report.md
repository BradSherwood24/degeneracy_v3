# V3.2 Phase 3 build report — maker execution + money math + stops (`v32/phase3-exec`)

Branch `v32/phase3-exec` (base origin/main `ae81a4c` = Phases 1+2 merged). Builder: Opus 4.8.
Scope: the ARMED maker executor, the money math (real fills + settlement backfill), the arming/stops
gate, and the four Phase-2-review carry-forwards (P3-1..P3-4) plus P3-5 (private fill + status-confirmed
cancel). Every network edge is injected; the suite uses fakes only and NEVER dials the proxy. No
`.env`/`*.pem`/`sim/out/sealed_eval`/`pilot/journals`/`pilot/ledger` or 2026-08-20..29 date is read by
any code path or test (the test close 2026-09-13 is the current UTC day).

## Suite

```
cd pilot && python -m pytest -q
759 passed in ~26s
```

Phase-2 baseline was 708; +51 net (16 `test_v32_executor` incl. the driver-standdown add, 20
`test_v32_stops`, 10 `test_v32_phase3_ledger`, 5 `test_v32_phase3_wiring`; one Phase-2 test renamed in
place: `test_armed_degrades_to_dry_phase2` -> `test_effective_mode_passthrough_phase3`, net 0 there).

## What was built

* `pilot/service/proxy_writer.py` — `ProxyWriter`: `rest_post` (NEVER retried; idempotency = the
  body's `client_order_id`), `rest_delete` (bounded retry — DELETE is idempotent), `rest_get`
  (delegates to `ProxyAuth`). All edges injected; returns a `WriteResponse(status_code, body, ok,
  error)` so the caller classifies a cap/budget 403 vs a success without an exception on the order path.
* `pilot/service/v32/executor.py` — `LiveExecutor` (same `on_action(action, state, now) -> [event]`
  interface as `FrozenExecutor`), the `RestBook` (retained across cancel/replace, F-1), an
  `OrderStatus` parser, and `cancel_stale_open_orders` (startup safety). Deliverable A.
* `pilot/service/v32/stops.py` — S5 arming (`v32_arming_check` + `v32_caps_agree`), reconcile-first
  (`reconcile_positions_clean`), banded S4 (`v32_s4_decision`), S1_LEGGED day-latch
  (`record_legged_occurrence`/`v32_latched_stop_kind`), the separate day-guard path
  (`v32_day_guard_path` -> `ops/v32_stops_YYYY-MM-DD.json`), and the single arm-or-degrade gate
  `decide_v32_arming`. Every threshold is a named constant. Deliverable B.
* `pilot/service/v32/ledger.py` (extended) — money-math slots on the window row (real `fills`,
  `wing_fills`, `held_legs`, `realized_lock`, `one_legged`, `rests_placed/rejected`, `wing_batches`,
  `exec_price_mismatches`, `realized_unsettled`/`unsettled_legs`), plus the settlement backfill helpers
  `v32_set_floor_dollars`, `build_v32_backfill_row`, `v32_settlement_backfill_sweep`,
  `v32_pending_credit`. Defaults preserve the Phase-2 row shape exactly. Deliverable C.
* `pilot/service/run_v32.py` (wired) — `build_executor` (P3-1: the ONE place the executor kind is
  chosen), `filter_buckets_to_width` (P3-2), monotone `_stamp` (P3-3), the exec-price mismatch alarm +
  fill de-dup + `on_poll_fill` (P3-4/P3-5), the `_order_status_poll` task, `get_health` /
  `fetch_market_result_v32`, the armed arming resolution + startup cancel + settlement-backfill
  `prepare()` step + S1_LEGGED finalize, and money-math in `_finalize`. Deliverables A/C/D.
* Tests: `pilot/tests/test_v32_executor.py`, `test_v32_stops.py`, `test_v32_phase3_ledger.py`,
  `test_v32_phase3_wiring.py`. Deliverable E.

## Exact wire shapes used (and the source)

**Create paths** (`service.orders.envelope`, prod-proven in `degeneracy_v2/kalshi/rest.py` + the
proxy's `_ORDER_CREATE_PATHS`): single `POST /trade-api/v2/portfolio/events/orders`, batch `POST
/trade-api/v2/portfolio/events/orders/batched` (body `{"orders":[entry,entry]}`).

**Rest (maker) create body** — built via `translate.to_v2_order` (which passes `post_only`,
`time_in_force`, `exchange_index`) then the 4-dp price overridden:
```
{ ticker: <bucket KXBTC-...>, side: "ask",            # buy NO == sell YES == ask
  count: "1.00", price: "0.5500",                     # NO at n=0.45 -> YES-space 1-n
  time_in_force: "good_till_canceled", post_only: true,
  self_trade_prevention_type: "taker_at_cross",
  client_order_id: <coid>, exchange_index: 2,
  expiration_ts: <close_epoch - quote_end_s> }        # T-5; a crash leaves nothing resting past the window
```
Verified against `translate`/`envelope` (side/price mapping, whole-cent -> 4-dp) and the direction
port. `post_only`, `good_till_canceled`, and the NO-space price are unit-pinned
(`test_place_rest_wire_body_post_only_gtc_expiration`).

**Wing (taker) create entries** — `envelope.build_entry` (default TIF `immediate_or_cancel`, STP
`taker_at_cross`), one per pending `state.wing_legs` leg, with `exchange_index` per leg; 2 legs ->
`build_batch`, 1 leg (retry) -> single. The batch/single response is synchronous fill truth
(`parse_batch_response`/`parse_single_response`), and each slot is normalized to its leg's side via
`normalize_fill_to_side` (the units choke point — Kalshi reports a NO order's price in YES-space).

**Cancel** — `DELETE /trade-api/v2/portfolio/events/orders/{order_id}` (the `/events/orders/{id}`
namespace, NOT the legacy `/portfolio/orders/{id}`, which returns HTTP 410 `deprecated_v1_order_endpoint`
per `degeneracy_v2/kalshi/rest.py:cancel_order`; the proxy routes any DELETE under
`/portfolio/events/orders` to the orders host, uncapped/unbudgeted). This DEVIATES from the task's note
that "DELETE cancels under `/trade-api/v2/portfolio/orders/...`" — I used the prod-proven `events`
path; both are covered by the proxy's write prefixes but only `events` is live on Kalshi today.

**Order status** — `GET /trade-api/v2/portfolio/orders/{order_id}` (# UNVERIFIED-LIVE — see below),
parsed by `parse_order_status` (filled = `fill_count`, else `maker_fill_count + taker_fill_count`, else
`place_count - remaining_count`; fail-closed to unavailable). Used to confirm every cancel (the truth
for the fill/cancel race — never assumed 0) and by the 1 s belt-and-braces poll.

**Open-orders sweep** — `GET /trade-api/v2/portfolio/orders?status=resting` (prod-proven
`get_orders` shape); cancel any of ours in KXBTC* at armed startup.

## Fill de-dup design

A fill can surface on BOTH the private `fill` WS channel (`driver.on_fill`) AND the 1 s
`order_status` poll (`driver.on_poll_fill`); wing fills also arrive synchronously in the batch
response. De-dup:
* `driver._seen_trade_ids` (WS `trade_id`) — a repeated `trade_id` is dropped (`fill_dup_ignored`).
* `driver._rest_fill_booked_oids` (rest `order_id`) — the poll and WS agree on the same order; whichever
  arrives first books, the other is a no-op (tested both orders).
* `executor.wing_coids` — a WS echo of a taker leg already booked from the batch response is skipped
  (`wing_fill_ws_dup`), NOT flagged foreign.
* The pure core is independently idempotent (`rest_fill is None` guard; `_maybe_close_set` gated on
  `wings_needed`), so a slip past the driver de-dup still cannot double-book a set.

## The four review carry-forwards

* **P3-1** — `build_executor(effective_mode, ...)` is the ONLY place an executor kind is chosen; a
  `LiveExecutor` is never constructed unless `effective_mode == "armed"` (and requires a `ProxyWriter`).
  `FrozenExecutor.on_action` now RAISES on any real (non-`WOULD_*`) kind, and `LiveExecutor.on_action`
  raises on a `WOULD_*` twin — a mis-wire fails loud, never synth-fills or silently no-sends. Tested.
* **P3-2** — `filter_buckets_to_width` drops every bucket whose width != `params.bucket_width` from the
  map before the state is built (a mixed-width hour can no longer select a wrong-width spot bucket and
  break the $2 pin); an emptied map stands the window down. Tested.
* **P3-3** — `_stamp` keeps the freshness/tick clock MONOTONE (`max(last, new)`), so a later-arriving
  frame from the slower of the two connection clocks cannot step `server_now()` backwards and churn
  cancel/replace. The regressing frame is still folded (its book applied); only the tick clock refuses
  to regress. Tested.
* **P3-4** — every real rest fill is checked: the frame's NO-space executed price must equal the
  resting price (post_only maker); a mismatch raises an `exec_price_mismatch` alarm (journaled +
  captured in `executor.exec_price_mismatches`) and continues, booking the lock at the resting price
  (the conservative convention). Tested.
* **P3-5** — armed subscribes the private `fill` (+ `market_positions`) channel (`include_private=True`
  on the bucket connection) AND drives cancels through the status-confirmed path
  (`filled_count_before_cancel` read from `order_status`), feeding `Fill` only for a tracked-or-retained
  coid. Tested.

## Money math

* At completion each window books, per set: the real rest fill (at the resting price — maker fee 0),
  the wing fills (exec price + exec fee), the model `realized_lock = 2 - (n+fee(n)) - W_paid` for
  observability, and the `held_legs` (bucket-NO + each filled wing) marked `realized_unsettled` with a
  conservative floor already booked (`realized_delta = floor - cash_paid`, floor = `max(0, held-1)`
  dollars = $2 complete / $1 two-of-three / $0 lone leg).
* Settlement backfill (`prepare()` in `run_v32.main`, before discovery): every prior
  `realized_unsettled` window whose held tickers have all settled (`GET /markets/{ticker}` exact match)
  gets a `build_v32_backfill_row` appended (`realized_delta = settlement_payoff - floor`), reusing
  `service.ledger.settlement_payoff` (which pays $1/contract where the held leg's side matches the
  market result — a complete pin yields exactly $2 at every settlement, so a complete set corrects by
  $0 and a one-legged set by its true settlement minus the $1 floor). Idempotent + fail-closed
  (an unfinalized leg waits for a later wake). Tested.
* Banded S4: `v32_pending_credit` feeds the pilot's `s4_balance_decision` so a pending settlement never
  moves the number a latch is decided on (the 2026-08-28 incident pattern).

## Deviations / decisions

1. **Cancel path** uses `/portfolio/events/orders/{id}` (prod-proven), not the task-note's
   `/portfolio/orders/{id}` (dead / HTTP 410). Both route+pass through the proxy; only `events` is live.
2. **`effective_mode_and_degrade` is now a passthrough**; the armed->dry DEGRADE moved to
   `decide_v32_arming` (S5 + reconcile-first + day-latch + S4), run in `main` with the live /health,
   positions and balance. The Phase-2 test asserting the old `("dry","phase2_no_executor")` contract was
   updated in place.
3. **The blocking order path runs on the asyncio loop** (`on_action` does the synchronous POST/DELETE,
   the cancel confirm sleeps up to 3x200 ms). At ~80 replaces/hour this is well within budget and
   mirrors the box executor's synchronous design; the injected `sleep` makes tests instant.
4. **`RestRecord` is defined twice** (run_v32's FrozenExecutor keeps its own; the LiveExecutor's is in
   executor.py) — structurally identical, duck-typed by the driver. Chosen over unifying to avoid
   touching the merged Phase-2 FrozenExecutor.
5. **S1_LEGGED** records an occurrence in the SEPARATE v32 day guard when an armed window ends
   one-legged; the DAY latches at 2 occurrences (`v32_latched_stop_kind`), read by the next window's
   `decide_v32_arming`. One occurrence only stands the hour down (the core already does that).

## `# UNVERIFIED-LIVE` items (watch the first armed window)

* `expiration_ts` (integer Unix seconds) as the auto-expire field on the rest create body. The translate
  passthrough key is `expiration_time`; the documented V2 field is `expiration_ts`. We set `expiration_ts`
  directly. `run_v32.py` rest body / `executor._rest_body`.
* `GET /portfolio/orders/{order_id}` for single-order status — `degeneracy_v2/kalshi/rest.py` has only
  the LIST form (`get_orders`), so the single-GET path and its `fill_count`/`maker_fill_count`/
  `place_count`/`remaining_count`/`status` field names are read defensively and marked UNVERIFIED-LIVE.
* Open-orders list field for our resting orders (`ticker` vs `market_ticker`, `order_id`) — read both.

## Open items for Phase 4 (falsifier `[pin]` list — every threshold hard-coded here)

From `service.v32.stops` (mirror each as a `[pin]` in `ceremony/v32_falsifier.md`):
* `V32_S4_DAY_LOSS_CAP_DOLLARS = 3.00` — day balance-loss cap.
* `V32_MIN_ORDER_BUDGET_AT_ARM = 200` — creates left in today's budget required at :40.
* `V32_MAX_CONTRACTS_PER_ORDER = 2` — the proxy cap V3.2 requires (never exceeded).
* `V32_S1_LEGGED_LATCH_THRESHOLD = 2` — one-legged-below-floor occurrences before a DAY latch.
* `V32_RANGE_TICKER_PROBE = "KXBTC-"`, `V32_STRIKE_TICKER_PROBE = "KXBTCD-"` — the S5 startswith probes.

From `service.v32.executor`:
* `CANCEL_CONFIRM_POLLS = 3`, `CANCEL_CONFIRM_INTERVAL_S = 0.2` — cancel-confirm poll bound.
* `CONSECUTIVE_REJECT_STANDDOWN = 3` — rest rejections before an hour stand-down.
* `ORDER_POLL_INTERVAL_S = 1.0` (run_v32) — belt-and-braces status poll cadence.

From `policy/v32_params.json` (already sha-pinned): `E`, `tol`, `deb_ms`, `wing_margin`, `lock_floor`,
`no_orders_after_s_to_settle`, `freshness_max_age_s`, `n_min`, `replace_rate_alarm_per_min`,
`bucket_width`, `contracts`.

## Questions for Brad

1. **Cancel path** — confirmed `/portfolio/events/orders/{id}` is the live cancel namespace (v2 rest is
   read-only reference)? The task note said `/portfolio/orders/{id}`; I used `events` (prod-proven).
2. **`expiration_ts` vs `expiration_time`** — the first armed create will confirm which field Kalshi
   honors for a GTC-with-expiry maker order; if the venue rejects `expiration_ts`, the fallback is the
   translate passthrough `expiration_time`.
3. **Proxy prerequisites for arming** (unchanged): `ORDER_TICKER_PREFIXES` covering `KXBTC` (covers both
   series), `DAILY_ORDER_BUDGET >= 4000`, `MAX_CONTRACTS_PER_ORDER = 2`, and `ALLOW_ORDERS=true` — the
   S5 `/health` check refuses to arm otherwise (degrades to dry).
4. **S1_LEGGED / S4 day guard** live at `ops/v32_stops_YYYY-MM-DD.json` (separate from the box's
   `ops/stops_*.json`) — confirm that separation is what you want (a V3.2 stop never latches the box).
