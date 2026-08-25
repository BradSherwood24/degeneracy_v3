# Phase 3 build report — execution + safety layer (opus48 BUILDER)

Built 2026-08-21. Location: NEW modules only under `pilot/service/` + `pilot/service/orders/` +
`pilot/tests/`. No existing Phase-1/Phase-2 `service/` module was modified (concurrent reviewer owns
Phase 2). No `sim/` file modified; no `.env`/`*.pem` read; no sealed-day file read by any code path;
no live network in any test. All order traffic targets the local proxy base only
(`http://127.0.0.1:8642/trade-api/v2/portfolio/orders[/batched]`).

## Component inventory

| # | File | Component | ~Lines |
|---|------|-----------|--------|
| 1 | `service/orders/translate.py` | `to_v2_order` — VERBATIM port of V2's direction mapping | ~75 |
| 2 | `service/orders/envelope.py` | wire-envelope builder (single/batch) + response parser + paths | ~230 |
| 3 | `service/orders/__init__.py` | package re-exports | ~25 |
| 4 | `service/ledger.py` | position/intent ledger (pure data + transitions), fills record, crash rebuild | ~430 |
| 5 | `service/executor.py` | THE single dispatcher (order-authority mutex, caps, budget, no-retry) | ~300 |
| 6 | `service/reconciler.py` | imbalance protocol (pure) + positions polling shell (S3) | ~330 |
| 7 | `service/stops.py` | alarm/stop state machine + arming (S5) + StopController | ~380 |
| 8 | `pilot/tests/test_{orders_translate,orders_envelope,orders_proxy_compat,ledger,executor,reconciler,stops}.py` | 83 tests | — |

## Tests

`cd pilot && python -m pytest tests/` -> **267 passed, 0 failed** (~3.0s). Of these, **83 are new**
(this phase); the other 184 (Phase 1 spine + Phase 2 signal + both review-probe sets) are untouched
and still green. New-test breakdown: executor 16, reconciler 16, stops 17, ledger 12, orders_envelope
12, orders_proxy_compat 7, orders_translate 6.

Fault-injection coverage (all green):
- **orphan** (leg B no-fill, leg A filled) -> retry-buy path (`test_orphan_retry_buys_deficient_leg_within_ceiling`, `test_429_leg2_produces_orphan_handled_by_retry`).
- **partial** (A=2, B=1) -> retry, then sell-down rounding DOWN (`test_partial_then_retry_then_selldown_rounds_down`, `test_selldown_target_rounds_down_to_min`).
- **sell-down partials looping to the bound** (`test_selldown_partial_iterates`: 3->1 in two IOC steps).
- **retry budget exhaustion -> GiveUp -> S2** (`test_giveup_s2_when_no_quotes_and_retries_exhausted`).
- **ceiling exceeded mid-retry -> sell-down branch, NEVER a buy above the ceiling** — ceiling checked with ACTUAL fees from responses (`test_ceiling_exceeded_forces_selldown_never_buys_above`, which also proves a cheaper ask WOULD have bought).
- **429 on leg-2 -> NoFill -> imbalance** (`test_429_on_order_path_is_no_fill_never_retried` + the reconciler orphan test).
- **phantom fill** (poll shows a position the ledger doesn't) -> S3; **believed-fill not on exchange** -> S3 (`test_phantom_fill_poll_mismatch_is_s3`, `test_believed_fill_not_on_exchange_is_s3`).
- **S1 arithmetic violation -> halt** (`test_s1_fires_when_realized_negative`, scoped to sub-$1 flip).
- **daily loss -> S4** (`test_s4_trips_at_daily_cap`).
- **crash mid-order** -> rebuild ledger from journal records; reconcile-first flags the in-flight intent (`test_crash_mid_order_rebuild_flags_inflight`, `test_rebuild_from_journal_reconstructs_positions_and_flags_inflight`).
- **no-orders-after boundary** (`test_no_orders_after_cutoff_refuses`; reconciler `test_inside_1s_rides_to_settlement`).
- **single-flight under concurrent submit** + **mutex serialization** (`test_single_flight_refuses_key_already_in_flight`, `test_mutex_serializes_concurrent_dispatch` asserts max POST concurrency == 1).
- **batch payloads pass the ACTUAL proxy parser** (`test_orders_proxy_compat.py`, 7 tests — see below).

## The two PENDING-BRAD flags (review F4/F8 DEFERRED; falsifier still DRAFT)

Both live on `StopConfig` (`service/stops.py`), marked PENDING-BRAD in code, with these defaults:

- `hold_complete_floor_pairs_to_settlement = True` — a COMPLETE sub-$1 flip pair is HELD to expiry
  on a stop. It is strictly safe (a filled sub-$1 pair pays >= $1; flattening it realizes a needless
  loss).
- `flatten_unprotected_exposure = True` — strangle legs and any unpaired flip overhang are flattened
  (reduce_only IOC sells) to remove exposure the arithmetic floor does not protect.

Reading of "on a stop, freeze all order placement ALWAYS": strategy placement (entries + rebalances)
is frozen ALWAYS via `executor.set_armed(False)`. The position-policy flatten is stop-authorized RISK
REDUCTION, dispatched through the Executor's `stop_authorized=True` path — reduce_only, still
proxy-capped, single-flight, journaled — never a strategy order and never a blind market sell (a
flatten with no observed bid is held-and-alerted, not sold). **Both flags and this reading require
Brad's ruling before Phase 5.** F4 (in-window crash supervisor) is a Phase-4 ops concern; the crash
path here is limited to reconcile-first rebuild + in-flight flagging, which is built and tested.

## Batch-path determination (CONFESSION — divergence from V2)

The proxy at `degeneracy-proxy/proxy.py` caps AND routes to the orders host ONLY the two paths that
(a) start with `_ORDER_WRITE_PREFIX = "/trade-api/v2/portfolio/orders"` and (b) are in
`_ORDER_CREATE_PATHS`:
- single create: `/trade-api/v2/portfolio/orders`
- batch create:  `/trade-api/v2/portfolio/orders/batched`

V2's `kalshi/rest.py` posts to `/portfolio/events/orders[/batched]` (the events-namespaced V2 path
after the 2026-07-11 host migration). That path does NOT start with the proxy's order-write prefix,
so through THIS proxy it would NEITHER route to the orders host NOR be contract/ticker/budget-capped.
Per the Phase-3 commission the proxy is the contract we must pass, so the envelope uses the proxy's
capped paths (`SINGLE_CREATE_PATH` / `BATCH_CREATE_PATH` in `orders/envelope.py`). **The V2-vs-proxy
path divergence is a live-verification item**: before an order fires, someone must confirm which path
Kalshi's live create endpoint actually accepts and, if it is the events path, update the proxy's
`_ORDER_WRITE_PREFIX` / `_ORDER_CREATE_PATHS` (proxy owner) so the capped path and the live path agree.
I did not edit the proxy.

## Rate-budget model (F10)

Client-side token accounting in the Executor, independent of the proxy's own persisted daily budget
(defense-in-depth). Config on `ExecutorConfig`: `tokens_per_entry = 10`, `window_token_budget = 200`.
Each order ENTRY costs `10 * len(legs)` tokens (a 2-leg entry batch = 20; a 1-leg rebalance/flatten =
10), matching the API fact "10 tokens per order, billed per item". Tokens are reserved per WINDOW
under the mutex BEFORE the POST; when `used + cost > window_token_budget` the order is refused
(`rate_budget_exhausted`) — no POST. On the order path a proxy 429/5xx is NOT retried and is treated
as a no-fill (the imbalance protocol resolves it); POST is never retried at all (V2 law).

## Proxy payload compatibility — how it is proven (CONFESSION on import mechanics)

`test_orders_proxy_compat.py` proves the built payloads pass the ACTUAL proxy cap parser. A plain
`import proxy` would execute the module-level `CONFIG = Config()` -> `load_dotenv(PROXY_DIR/'.env')`
-> `_load_signer`, which READS the proxy's `.env` and loads the RSA PEM (key material) into the test
process — forbidden by house law. So the test parses `proxy.py` with `ast` and execs ONLY the pure
parser defs (`is_order_create`, `parse_count`, `parse_order_entries`, `check_order_caps`,
`BodyParseError`, and `_ORDER_CREATE_PATHS`) in an isolated namespace containing just
`json`/`Decimal`/`InvalidOperation`. This uses the REAL proxy source text (a parser drift breaks the
test) and provably cannot touch `.env`/`*.pem` (a test asserts no `Config`/`Signer`/`OrderBudget`/
`load_dotenv`/`SESSION` symbol leaked into the extracted namespace). This IS a "sys.path/import
trick" and is confessed as required.

## Mid-build handoff addressed (Phase-2 review F3 + preconditions)

- **F3 (fills-record honesty / bin-5).** The ledger's emitted fills record now ALWAYS carries per-leg
  entries keyed to the ACTUAL paired high/low tickers, each with the fee-free average fill price and
  the ACTUAL fee (`ledger.fills_record`), and an adapter (`ledger.to_window_fills`) maps them onto
  the Phase-2 `parity.WindowFills`/`LegFill`. With correct tickers, `parity._price_deltas` always
  finds >= 1 comparable leg when the pair filled, so the zero-comparison false-honesty path is not
  reached in practice. **I did NOT edit `parity.py` (file ownership).** The belt-and-suspenders
  one-line parity change is documented at the top of `ledger.py`'s fills section for the integrator:
  in `assign_bin`'s bin-5 branch, `if not deltas: return WindowParity(..., BIN_BOTH_NO_FILL, ...)`
  before certifying a match. LegFill has no fee slot today; per-leg fee rides in `fills_record` (and
  always reaches S1 via the ledger) — if the report wants it, the integrator adds
  `avg_fee: Decimal = Decimal(0)` to LegFill. The FLAGGED probe in `test_review_probes2.py` still
  asserts the current parity behavior and stays green (I did not change parity).
- **ShakedownRecorder precondition.** Noted: the live-fire path does NOT route through
  `ShakedownRecorder`. This phase builds the execution primitives (Executor/Reconciler/Stops/Ledger);
  the armed-mode window runner that composes them is a Phase-4 harness and will be its own path, not
  ShakedownRecorder.
- **Confirmed-unchanged interfaces** (signal Action/LegOrder, decide() signature, `load_policy()` sha
  `1b01fd98…3656`, stricter `sim_entry_for_window` bounds) — consumed as given; nothing here depends
  on the changed internals.

## Interfaces the integrator/harness must wire (Phase 4)

- `Executor.execute(intent, t_minus_s, *, stop_authorized=False)` — the ONLY dispatcher. `t_minus_s`
  is an ARGUMENT (event-derived), never a wall-clock read for a trading decision. Arm only via the
  S5 `arming_check`; disarm on any stop via `StopController.trip`.
- Reconciler: `propose_rebalance(state, params, t_minus_s, quotes, ceiling_per_pair)` is pure. The
  harness must (a) compute `ceiling_per_pair` = `params.imbalance.pair_cost_ceiling_sub1` for sub-$1
  flip or the strangle bucket-fair (`WindowState.fair_strangle_q`) for the strangle, (b) resolve
  `RebalanceQuotes` (held-outcome ask to buy / bid to sell) from the live book, (c) turn each
  RetryBuy/SellDown into an Intent and call `executor.execute`, and (d) on `GiveUp`/`RideToSettlement`
  with an unresolved imbalance at close, trip S2.
- Fee-from-response: `ledger.FEE_IS_TOTAL = True` — `average_fee_paid` is treated as the TOTAL fee on
  the fill (see CONFESSIONS). Positions-poll sign convention: held YES = +net, held NO = -net (a
  live-verification item).

## CONFESSIONS (judgment calls / interpretations)

1. **Proxy create-paths, not V2's events-paths.** Followed the proxy contract (`/portfolio/orders`
   [/batched]); confessed the V2 `/portfolio/events/orders` divergence as a live-verification item
   (see "Batch-path determination"). This is a registered-spec call: the proxy parser IS the spec I
   must pass, so I built to it and flagged the reconciliation Kalshi's live endpoint needs.
2. **Price precision override.** `to_v2_order` is ported VERBATIM (with its verbatim tests) and used
   for the direction (bid/ask). Its price is whole-cent (`int()`), which would lose the 15-minute
   ladder's deci-cent (0.001) resolution, so the envelope OVERRIDES `price` with a full-precision
   4-dp Decimal via `wire_price(side, limit)` using translate's own YES-cents convention (yes -> p,
   no -> 1-p, buy/sell-invariant). A test proves byte-identity with translate on whole-cent inputs.
3. **`average_fee_paid` treated as the TOTAL fee** (`FEE_IS_TOTAL=True`), added once. If it is a
   per-contract average, flip the constant. Over-counting only makes S1 more likely to HALT (fails
   closed toward halting); under-counting is the unsafe direction. Live-verification item — the
   paired-replay's actual-fee reconciliation resolves it on the first live fill.
4. **S1 uses a worst-case min payout of $1.00/pair.** A sub-$1 flip pair's settlement payout is
   >= $1 (min over the outcome; the mid can pay $2). At completion, before the outcome is known, S1
   compares `matched * $1.00 - net_cash_out(actual fees) < 0`. This is the arithmetic-floor check
   (n=1) and is scoped to sub-$1 flip only (the strangle has no such floor, so S1 excludes it).
5. **reduce_only=True on all sell-down / flatten intents.** A sell of the held outcome can only reduce
   the position with reduce_only set; it can never accidentally open the opposite side. Not mandated
   by the plan (plan says "sell-down is a fresh capped create") — added as a safety property. It is a
   passthrough field translate already supports and the proxy caps ignore it.
6. **Single-flight is a transient in-flight guard; entry-dedup is a separate persistent guard.** The
   mutex serializes ALL dispatch (so two POSTs never overlap — the mutex test asserts max concurrency
   1). Single-flight refuses a duplicate (window, side, purpose) that is still in flight (tested by
   seeding the in-flight set, since under the full mutex a genuine overlap cannot occur). Entry-dedup
   (`window_already_entered`) additionally guarantees at most one ENTRY per window even after the
   first resolved. Both refusal reasons are distinct and tested.
7. **StopController flatten dispatch bypasses the arm freeze via `stop_authorized=True`.** This is the
   only path that dispatches while unarmed and is restricted to `PURPOSE_FLATTEN` (a non-flatten
   stop-authorized call is refused). It exists to honor `flatten_unprotected_exposure=True`; it is
   PENDING-BRAD (F8) and clearly gated.
8. **Positions-poll sign + response shape.** `expected_positions` maps held YES -> +net / NO -> -net,
   and `parse_positions_response` reads `market_positions` (defensively `positions`) with a signed
   `position` field. Both are live-verification items (the exact Kalshi positions payload shape/sign
   is confirmed on the first poll).
9. **Journal timestamps use an injectable clock** (default `time.time`) for `local_ts` ONLY — never a
   trading-gate input. The executor's trading gates read `t_minus_s` (event-derived) exclusively.
10. **ledger imports `parity` lazily inside `to_window_fills`** (read-only, composition, not a
    modification) so a Phase-2 parity rename is traced here and the ledger has no import-time
    dependency on parity.

## Blockers

None. All 267 tests green. Open items are live-verification / Brad-ruling flags, not build blockers:
the V2-vs-proxy order path (confession 1), the two PENDING-BRAD F8 flags, `average_fee_paid`
semantics (confession 3), and the positions payload shape/sign (confession 8).
