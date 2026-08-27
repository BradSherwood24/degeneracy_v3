# Fix report: explicit `exchange_index` routing (crypto exchange sharding)

Branch: `fix/exchange-index`  •  Date: 2026-08-27  •  Suite: 555 passed (544 baseline + 11 new)

## Cause

On 2026-08-27 00:00Z the FIRST armed wide-box fire had **both IOC legs rejected** by Kalshi with
`{'code': 'market_not_found'}`. Kalshi sharded its matching engine (changelog 2026-08-24: "Crypto…
provisioned on dedicated exchange instances"). Market records for KXBTC15M and KXBTCD now carry
`"exchange_index": 2`; the 2026-08-23 markets that filled were `exchange_index: 0`. Our order entries
(`orders/envelope.build_entry` → `orders/translate.to_v2_order`) omitted `exchange_index`, so the
orders routed to shard 0 where the tickers do not exist.

Docs (docs.kalshi.com/api-reference/orders/create-order-v2): `exchange_index` (int, optional),
"-1 or ≥0. If omitted, auto-routes when ticker is provided; otherwise defaults to 0." **Empirically
omission did NOT auto-route** — both legs 404'd on shard 0. So routing is now ALWAYS explicit.

## Evidence (read-only, live journal `pilot/journals/20260827T000000Z.jsonl`)

- `box_fire` (idx 750042): legs `KXBTCD-26AUG2620-T79199.99` (no @0.99) + `KXBTC15M-26AUG262000-00`
  (yes @0.88), source `wide-box`.
- `order_intent` (idx 750043): both legs, **no `exchange_index` field**.
- `order_response` (idx 750044/750045): both
  `error: "{'code': 'market_not_found', 'details': '', 'message': 'market not found'}"`,
  `fill_count 0`, `no_fill true`.

## Fix

1. **wake.py** — `WakeResult.exchange_index_by_ticker` property builds `{ticker: exchange_index}` from
   BOTH legs (15M leg + the full hourly ladder pool, all live generations). A market whose record
   lacks the field maps to **None** (fail closed) via `coerce_exchange_index` (rejects None / non-int
   / bool / non-integral float).
2. **ledger.IntentLeg** — new field `exchange_index: int | None = None` (default None → pre-fix
   journals rebuild unchanged; `rebuild_from_journal` never dispatches). Serialized in
   `intent_to_record` / `_leg_from_record`.
3. **envelope.build_entry** — adds `"exchange_index": int(...)` to the legacy dict when the leg has
   one; `translate.to_v2_order` already passes it through (`_PASSTHROUGH_KEYS`). Omitted when None.
4. **executor** — new gate (2b), after caps, before the settle cutoff: if ANY leg's `exchange_index`
   is None, journal `order_refused_no_exchange_index` and REFUSE (no-fill) — never send an unrouted
   order. Even one None leg in a batch refuses the whole intent.
5. **run_window** — captures the wake map into `self._exchange_index_by_ticker` in both
   `_run_box_window` and `_run_corridor_window`, and stamps `exchange_index=self._xi(ticker)` on
   every dispatched IntentLeg: box entry (`_on_box_action`), corridor entry (`_on_signal_action`),
   corridor rebalance (`_maybe_rebalance`), box flatten (`_box_flatten_attempt`). All three
   position-policy `stops.trip(...)` calls pass `exchange_index=self._exchange_index_by_ticker`.
6. **stops.build_flatten_intent** — new `exchange_index` parameter, set on the flatten IntentLeg;
   `StopController.trip` gained an `exchange_index` map param and looks up per-ticker.

The proxy forwards the request body **bytes unchanged** (`proxy.py` reads the body once and passes
`data=body` to the upstream `requests` call; `parse_order_entries` is a read-only cap check that
ignores extra keys), so `exchange_index` reaches Kalshi intact. Verified against the REAL proxy cap
parser (`test_shard2_entry_with_exchange_index_passes_proxy_parser`).

## Positions / fills / balance finding (docs.kalshi.com, fetched 2026-08-27)

**No code change is required for reads** — our GETs already see shard-2 positions/fills:

- `getting_started/exchange_sharding`: "Programmatic traders must preallocate collateral on a given
  exchange shard before order placement." Balance: "[Get Balance] provides a breakdown of account
  balances across exchange indexes."
- `portfolio/get-positions` — `exchange_index` query param: **"Filter results by exchange shard.
  Omit to return results from all exchange shards."**
- `portfolio/get-fills` — identical: `exchange_index` param, **"Omit to return results from all
  exchange shards."**
- `portfolio/get-balance` — `exchange_index` param: "If omitted, both [balance and portfolio_value]
  include all exchange indexes." Response adds a `balance_breakdown` array (`IndexedBalance` per
  shard).

`reconciler.poll_positions` / `poll_fills` and `run_window._read_positions` / `_fetch_balance_payload`
all call with `params = {}` (no `exchange_index`), so per the docs they already return results across
ALL shards, including shard 2. The S4 balance gate reads the omitted-param aggregate (`balance`),
which now spans all indexes — unchanged behavior for the gate. Left as-is by design.

## OPERATOR action (not code)

Brad's collateral is **$52.97 on shard 0, $0 on shard 2**. Even with correct routing, a shard-2 order
needs preallocated shard-2 collateral ("Programmatic traders must preallocate collateral on a given
exchange shard before order placement"). Until collateral is moved to shard 2, a routed order will be
rejected for insufficient funds. **This is an operator preallocation step, outside this code fix.**

## Tests (11 new; 555 total, 544 baseline preserved)

- `test_orders_envelope`: wire body carries `exchange_index:2` for BOTH box orientations; flatten
  carries it; omitted when None; the exact tonight two-leg bodies after the fix.
- `test_executor`: refusal + `order_refused_no_exchange_index` journal when None (no POST); one None
  leg refuses the whole batch; a shard-2 entry routes to shard 2 in the wire body.
- `test_ledger`: `intent_to_record`/`intent_from_record` round-trips `exchange_index`; a pre-fix
  journal (leg dicts with NO `exchange_index` key) still rebuilds.
- `test_wake`: `exchange_index_by_ticker` maps both legs (real tonight record shape), fails closed to
  None on a missing field; `coerce_exchange_index` junk/bool/float rejection.
- `test_box_wiring`: `_box_wake()` markets carry `exchange_index:2`; box entry legs + flatten single
  route to shard 2 end-to-end.
- `test_orders_proxy_compat`: a shard-2 entry passes the REAL proxy cap parser and retains the field.
- Updated dispatch fixtures in `test_executor`, `test_stops`, `test_review_probes3`, `test_run_window`
  to supply `exchange_index` (the gate now requires explicit routing).

## Exact wire bodies for tonight's two legs AFTER the fix

```json
{"ticker": "KXBTCD-26AUG2620-T79199.99", "side": "ask", "count": "1.00", "price": "0.0100", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross", "client_order_id": "ab69dc5b-74a3-4188-ab6d-a4d80fa1d39e", "exchange_index": 2}
{"ticker": "KXBTC15M-26AUG262000-00", "side": "bid", "count": "1.00", "price": "0.8800", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross", "client_order_id": "45f261c9-0f66-4e2a-be9c-69ff130b39fe", "exchange_index": 2}
```

## CONFESSIONS

- **The refusal gate changes live semantics.** A leg the wake could not resolve to a shard is now
  REFUSED, not sent. This is strictly safer than the prior behavior (an unrouted order 404s anyway),
  but it means a wake map gap silently converts a fire into a no-fill (journaled as
  `order_refused_no_exchange_index`). That is the intended fail-closed direction.
- **I did not verify against a live order.** No order was placed (house law). The wire body is proven
  by unit tests + the real proxy cap parser + the docs, not by a live 200 from Kalshi. The first live
  re-test is Brad's call and REQUIRES shard-2 collateral first (see OPERATOR action).
- **The balance-shape change is noted but not hardened.** `get-balance` now returns an aggregate plus
  a `balance_breakdown` array. The S4 gate still reads the aggregate `balance` and behaves as before,
  but I did not add per-shard balance parsing — if a future check needs per-shard cash (e.g. to
  confirm shard-2 collateral before arming), that is a follow-up.
- **`exchange_index` source of truth is the wake sweep only.** The box anchor 15M ticker is assumed
  stable between the :40 wake and the phase-B poll (only its floor_strike materializes later); tonight
  it was. If a ticker ever differs, the leg gets None → refused (fail closed), never mis-routed.
- **Auto-route (`-1`) is deliberately NOT used.** The docs offer `-1` auto-routing, but omission
  already failed to auto-route tonight, so I route explicitly to the discovered shard rather than
  trust any auto-route path.
