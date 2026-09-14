# V3.2 cancel-shard hotfix — first-armed-window incident (2026-09-14)

Branch `v32/fix-cancel-shard` off `origin/main` @ 0e602bf. URGENT hotfix after a live incident in the
first armed maker window (close 22:00Z, 1 contract).

## Incident timeline (from `pilot/journals_v32/20260914T220000Z.jsonl`, our own data)

All times UTC, 2026-09-14. The window armed and began quoting a bucket-NO rest at T-15.

| time | event | detail |
|------|-------|--------|
| 21:44:59.726 | place_rest `…Z-1` | KXBTC-26SEP1418-B78850, `exchange_index: 2`, n=0.54 → create **201**, order rests |
| 21:45:00.947 | cancel_rest `…Z-1` | DELETE `/portfolio/events/orders/01a0a1e1-6b60-…710` (NO shard param) |
| 21:45:01.594 | cancel_confirmed | **delete_status 404**, reduced_by None, **filled_before_cancel 0** → executor believed "gone" |
| 21:45:01.598 | place_rest `…Z-2` | the core placed the NEXT rest on the (false) confirmation |
| … | … | the loop repeats every ~2–10 s |
| 21:47:15.544 | place_rest `…Z-21` | 21st rest placed; operator kills the process here |

Receipts (counts over the whole journal):

- **21** unique rests placed (`v32-2026-09-14T22:00:00Z-1 … -21`), all routed on `exchange_index: 2`
  (the window's 186 buckets are all shard 2).
- **20** cancels issued (coid `-21` was killed before its cancel); **20** `cancel_confirmed` records,
  **every one** `delete_status == 404` and `filled_before_cancel == 0`.
- No fills (`fill`/`take_wings` never fired); balance unchanged. The 21 live rests were hand-cancelled
  by the operator after the kill.

## Root cause (confirmed from the journal + operator's live verification)

Kalshi exchange sharding: crypto markets live on `exchange_index: 2` (in force since 2026-08-24; see
memory *kalshi-exchange-sharding*). The cancel endpoint requires the shard as a query param:

- `DELETE /portfolio/events/orders/{id}?exchange_index=2` → **200** `{"order_id":…,"reduced_by":"1.00",…}` (operator-verified live)
- `DELETE /portfolio/events/orders/{id}` (no param) → **404** `{"error":{"code":"not_found"}}` — **while the order is still live and resting**
- `GET /portfolio/orders/{id}` works WITHOUT the param and returns the full order incl. `exchange_index`, `status`, `fill_count_fp`, `remaining_count_fp`
- legacy `DELETE /portfolio/orders/{id}` → 410 deprecated

The executor sent the un-sharded DELETE, got 404, and treated a DELETE 404 as terminal-success
(`cancel_confirmed`, `filled_before_cancel 0`). The core then placed the next rest — violating the
"never two live rests" invariant *on the venue* while internal state showed one order, cancelled. 21
rests stacked in ~2.5 min.

### Why the review's docs check missed it

The `create-order-v2` docs page (verified during the Phase-3 build) *does* carry `exchange_index`, so
creates were sharded correctly (that is why creates 201'd). The **`cancel-order-v2` docs page does not
mention `exchange_index` at all** — nothing in the reference told us the DELETE needed it, and a 404
reads as "already gone", not "wrong shard". The shard requirement on cancels is undocumented and was
only found by the operator probing the live venue during the incident.

## The fix (`pilot/service/v32/executor.py`, `run_v32.py`, `ledger.py`)

1. **Shard-carrying cancel path.** `CANCEL_PATH_TMPL = "/portfolio/events/orders/{order_id}?exchange_index={exchange_index}"`,
   composed via new `cancel_path(order_id, exchange_index)`. `RestRecord` retains each order's
   `exchange_index` (from the discovery map at place time). The startup sweep
   (`cancel_stale_open_orders`) routes each listed order's OWN `exchange_index` from the open-orders
   list.
2. **A DELETE 404 is NOT "gone".** `_cancel_rest` → on any non-2xx DELETE, `_cancel_nonok` GETs the
   order status (no param): a terminal status (`canceled`/`executed`/`expired`) resolves it (filled
   from `fill_count`); a still-`resting` status triggers up to `CANCEL_RETRY_ATTEMPTS = 3` shard-aware
   DELETE retries; if it STILL rests → journal `cancel_failed`, latch the executor stand-down
   (`stand_down_reason = "cancel_failed"`), mark the record `cancel_failed` (NOT "cancelled"), and
   return a filled-0 confirm — the stand-down (applied by `V32Driver._apply_executor_standdown` before
   the queued `OrderCancelled` is decided) blocks any replacement place.
3. **Pre-PLACE venue-truth invariant (belt over the braces).** Before every create, `_pre_place_invariant`
   GETs `/portfolio/orders?status=resting`, filters to our `v32-` coid prefix (excluding the one just
   confirmed gone this replace cycle). If any of ours still rests → do NOT place: cancel them
   shard-aware, journal `rest_invariant_violation` with the count, alarm, and stand down. Internal
   state can never again disagree with the venue by more than one order. (Unreadable venue → proceed;
   the working cancel path is the primary guard, so a transient read never self-DoSes the strategy.)
4. **Ledger counters.** Fixed the 0/0 bug: `run_v32._compute_money_math` early-returned `{"fills":…}`
   on a no-fill window (exactly the 20:00Z armed row: 3 rejected creates, no fills), dropping
   `rests_placed`/`rests_rejected`. The operational counters now ride BOTH exits. Added
   `cancels_attempted`, `cancels_confirmed`, `cancel_404s`, `rest_invariant_violations` to the executor,
   `_compute_money_math`, and `build_v32_ledger_row`.

## Tests

New `pilot/tests/test_v32_cancel_shard.py` (11 tests) + fixture
`pilot/tests/fixtures/v32/incident_20260914T220000Z_orderpath.jsonl.gz` (40 order-path records, 8
cycles, 902 bytes):

- cancel URL composition carries `?exchange_index=2` (final URL recorded by a fake transport)
- DELETE 404 + status `resting` → 3 shard-aware retries → `cancel_failed` + stand-down, **no PLACE**
- DELETE 404 then a shard-aware retry succeeds → clean confirm
- DELETE 404 + status `executed` → the race Fill is routed (booked once, cancel_race) and the core
  turns filled>0 into the hourly entry (wings owed)
- pre-PLACE venue invariant: one of ours resting → no create, straggler cancelled shard-aware,
  `rest_invariant_violation` recorded, stand-down; foreign order ignored → place proceeds
- startup sweep routes each order's own `exchange_index`
- **incident replay**: the same order-path sequence pre-fix (un-sharded cancels, 404 misread) stacks
  to N = number of places (>1); post-fix through the real executor + a sharded fake venue → **≤ 1**
  live rest at any time, 0 cancel_failed, 0 invariant violations, nothing left resting.

Updated `tests/test_v32_executor.py` and `tests/test_proxy_writer_url.py` to the sharded cancel path.

`cd pilot && python -m pytest -q` → **845 passed** (baseline 833 + 12 new). Ruff clean on all changed
files.

## FIRST ARMED WINDOW MUST CONFIRM — checklist addition

Add to the arming checklist: **before arming, confirm a cancel round-trips on the live shard** — i.e.
`GET /portfolio/orders?status=resting` returns the order's `exchange_index`, and a
`DELETE …/{id}?exchange_index=<that>` returns 200 with `reduced_by` (not 404). A cancel that 404s is a
shard/param problem, never proof the order is gone. The ledger row's `cancel_404s` and
`rest_invariant_violations` must both be 0 on a healthy window; any `cancel_failed` alarm stands the
hour down by design.
