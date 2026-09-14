# V3.2 order-path audit — every request the pilot can send

**Date:** 2026-09-14 · **Auditor:** Opus 4.8 · **Branch:** `audit/order-path` (= PR #46 `v32/fix-cancel-shard` @ 09993f2, carrying PR #43's prefix fix)
**Scope:** the COMPLETE order path of pilot V3.2 — `service/proxy_writer.py`, `service/proxy_auth.py`, `service/v32/executor.py`, `service/run_v32.py`, `service/v32/stops.py`, `service/wake.py` / `service/record_range.py` discovery GETs, `service/ws_client.py`. No network was dialed; the two live incidents of 2026-09-14 (20:00Z doubled-prefix create-404; 22:00Z un-sharded cancel-404 stacking 21 rests) are the evidence base, cross-read against the incident journal `journals_v32/20260914T220000Z.jsonl`.

Money is Decimal end-to-end; every economic price/fee travels in the leg's own side-space through the `normalize_fill_to_side` choke point. `python -m pytest -q` in `pilot/`: **843 passed, 2 skipped, 2 errors** (the 2 errors are `test_quintile.py` `FileNotFoundError` on `historical-data/` absent from the review worktree — a data-fixture gap, not order-path code; builder count 845 = 843 + those 2 with data present).

Legend for **Live-evidence status**: `VERIFIED-LIVE` (observed today) · `PROD-PROVEN` (same envelope proven in degeneracy_v2 / the v1.1 box; code-cited) · `DOCS-ONLY` (docs.kalshi.com only) · `UNVERIFIED` (must go on the first re-armed window's MUST CONFIRM list).

---

## 1. The request table (one row per request)

Composition is idempotent through `compose_rest_url` (`proxy_auth.py:54`): a relative path gets exactly one `/trade-api/v2` prefix; an already-prefixed path is not re-prefixed; the final URL is asserted to carry EXACTLY ONE prefix (kills the 20:00Z doubled-prefix bug at the choke point). All writes go through `ProxyWriter`; all reads through `ProxyAuth.rest_get` (bounded retry 1→2→4 s, 4 attempts).

### Write 1 — single create (resting bucket-NO)
- **Method / URL:** `POST http://127.0.0.1:8642/trade-api/v2/portfolio/events/orders` (`executor.REL_SINGLE_CREATE`, pinned `== SINGLE_CREATE_PATH[len(prefix):]` at import).
- **Body (units/types):** `ticker` (str, `KXBTC-…` range bucket) · `side` `"ask"` (V2 book side: a NO bid at n = a YES ask at 1−n, via verbatim `translate.to_v2_order`) · `count` `"1.00"` (fixed-point STRING) · `price` `"0.4600"` for n=0.54 (4-dp YES-space dollar STRING = `1 − n`, `_wire_price_no`) · `time_in_force` `"good_till_canceled"` · `post_only` `true` · `self_trade_prevention_type` `"taker_at_cross"` · `client_order_id` `"v32-<close>-<seq>"` · `exchange_index` `2` (int) · `expiration_time` (int Unix SECONDS = `close_epoch − quote_end_s`).
- **Idempotency:** `client_order_id` in the body; **POST is NEVER retried** (`proxy_writer.rest_post`).
- **Proxy handling:** matches `_ORDER_CREATE_PATHS` (single) AND `_ORDER_WRITE_PREFIXES`; capped (ticker-prefix + `count ≤ max_contracts`) → daily budget consumed (1 entry) → routed to the **orders host**.
- **Venue response / parse:** `{"order": {...}}` (bare accepted); `parse_single_response(body, side="no")` → `order_id`, `fill_count` (Decimal), price normalized to NO-space. `parsed.order_id is None` or `error` ⇒ reject.
- **Non-2xx handling:** 403 cap/budget, 4xx, parse-error → `_reject_place` (journal `rest_rejected`, `OrderCancelled(filled 0)`; 3 consecutive ⇒ hour stand-down). **status None (timeout) or ≥500 ⇒ UNKNOWN OUTCOME:** record the coid (order_id None, status `unknown`) so a later fill is attributable + hedged via the late-fill path, and latch `post_unknown_outcome` stand-down (no second rest); the per-order `expiration_time` bounds the leak.
- **Shard:** carried (`exchange_index: 2`).
- **Status:** **VERIFIED-LIVE** — incident-2 create returned 201 with exactly this body (`side "ask"`, `price "0.4600"`, `count "1.00"`, `post_only true`, `good_till_canceled`, `expiration_time`, `client_order_id`, `exchange_index 2`); reproduced by `test_executor_rest_bucket_no_body_capped_only_under_range_prefix`.

### Write 2 — batch create (both wings)
- **Method / URL:** `POST …/trade-api/v2/portfolio/events/orders/batched` (`REL_BATCH_CREATE`).
- **Body:** `{"orders": [entry, entry]}`; each entry = IOC taker: `side` (`"bid"` for a YES wing at Sd, `"ask"` for a NO wing at Su) · `count` `"1.00"` · `price` 4-dp YES-space (`yes` → limit; `no` → 1−limit) · `time_in_force` `"immediate_or_cancel"` · `self_trade_prevention_type` `"taker_at_cross"` · `client_order_id` (fresh `v32-` coid per leg) · `exchange_index` `2`. Count = the **filled rest count only** (`_wing_step`, ruling F-2). No `post_only`.
- **Idempotency:** per-leg `client_order_id`; POST not retried.
- **Proxy handling:** matches `_ORDER_BATCH_PATHS` → `is_batch=True`; each entry capped; budget consumes `len(entries)`; routed to the orders host.
- **Venue response / parse:** `parse_batch_response` expects `{"orders": [slot,…]}`; each slot → `fill_count`, `average_fill_price` (YES-space, normalized per-leg via `normalize_fill_to_side`), `average_fee_paid` (dollars, side-independent). Batch response is treated as **synchronous fill truth**.
- **Non-2xx handling:** `resp.ok` False ⇒ `parsed=[]` ⇒ every leg emits `Fill(count 0)` + `wing_no_fill` journal ⇒ core issues `RETRY_WING` next tick.
- **Shard:** carried per leg.
- **Status:** **PROD-PROVEN** (path prod-proven in `degeneracy_v2/kalshi/rest.py`; envelope cross-checked against the REAL proxy parser by `test_batch_entry_passes_proxy_parser`) **/ response shape UNVERIFIED** — incident-2 fired NO wings (no fills), so the live `{"orders":[…]}` batch-create response + its synchronous-fill semantics are not observed. **→ MUST CONFIRM.**

### Write 3 — single create (single wing retry, RETRY_WING)
- **Method / URL:** `POST …/portfolio/events/orders` (single path; `_take_wings` sends the 1-leg case as a single create, not a 1-element batch).
- **Body:** one IOC taker entry (as Write 2's entry). Parsed with `parse_single_response(body, side=leg.side)` (normalized once at parse, re-normalized idempotently in `_wing_events`).
- **Everything else** as Write 2 (single-endpoint caps/budget=1).
- **Status:** **PROD-PROVEN / response UNVERIFIED** (IOC taker completion proven in V2; live single-leg fill truth not observed today). **→ MUST CONFIRM.**

### Write 4 — cancel (DELETE)
- **Method / URL:** `DELETE …/trade-api/v2/portfolio/events/orders/{order_id}?exchange_index={exch}` (`CANCEL_PATH_TMPL`); no-shard fallback `…/orders/{id}` ONLY when the shard is unknown.
- **Body:** none. **Idempotency:** DELETE is idempotent → bounded-retry on 429/5xx/connection blips (`rest_delete`).
- **Proxy handling:** DELETE under `/portfolio/events/orders` ⇒ `is_order_write_path` True (query stripped) ⇒ routed to the **orders host**; `is_order_create` False ⇒ **uncapped, unbudgeted**.
- **Venue response / parse:** 2xx `{"order_id","reduced_by":"1.00","ts_ms"}` → `reduced_by` (Decimal); `filled = max(0, count − reduced_by)`, cross-checked against the order-status GET (**take the MAX** so a race fill is never under-counted); a race fill is booked at the resting price (maker fee 0, de-duped by order_id). `_last_confirmed_gone_oid` set (excluded from the next pre-place invariant).
- **Non-2xx handling:** **a 404 is NEVER "already gone"** — GET order-status; a terminal status resolves it (`fill_count`); a still-resting status ⇒ retry the DELETE **shard-aware** up to `CANCEL_RETRY_ATTEMPTS`(=3); still resting ⇒ `cancel_failed` journal + alarm + hour stand-down (never frees a place). 410 legacy path is not used. 429/5xx retried inside `rest_delete`.
- **Shard:** carried (the 22:00Z fix; a live crypto order 404s without it and 200s with it).
- **Status:** **VERIFIED-LIVE** — DELETE `{id}` → 404, DELETE `{id}?exchange_index=2` → 200 `{order_id,reduced_by,ts_ms}` (both observed); the 404≠gone misread (pre-fix `cancel_confirmed delete_status 404` in the incident journal) is now fixed. Pinned by `test_v32_cancel_shard.py` incident replay (`max_concurrent ≤ 1`).

### Read 5 — order status
- **Method / URL:** `GET …/portfolio/orders/{order_id}` (**no shard param**). Routed to the market-data host (GET).
- **Parse:** `parse_order_status` reads documented fixed-point fields FIRST — `fill_count_fp` / `remaining_count_fp` / `initial_count_fp` (Decimal→int) + `status`; legacy `fill_count`/`remaining_count`/`maker/taker_fill_count` only as fallback; `filled = fill_count_fp` else `initial − remaining`. Fail-closed to `available=False, filled 0`.
- **Non-2xx handling:** any exception/non-2xx (via `rest_get`) → `available=False` → caller journals + treats as no-fill.
- **Status:** **VERIFIED-LIVE** — GET `{id}` (no shard) → 200 with exactly the observed key set (`fill_count_fp`, `initial_count_fp`, `remaining_count_fp`, `status:"resting"`, `order_id`, `exchange_index`, …).

### Read 6 — open orders (pre-place invariant + startup sweep)
- **Method / URL:** `GET …/portfolio/orders?status=resting`.
- **Parse:** `body["orders"]` list; filtered to our `v32-` coid prefix (invariant) / `KXBTC*` + our-or-coidless (startup); each row's `order_id`, `client_order_id`, `exchange_index` (fallback to the ticker's shard).
- **Non-2xx handling:** unreadable ⇒ invariant returns None → PROCEED (working cancel path is the primary guard; never self-DoS the strategy); startup sweep records `startup_open_orders_read_failed` and cancels nothing.
- **Shard:** not needed on the GET (lists shard-2 orders).
- **Status:** **VERIFIED-LIVE** — "GET `/portfolio/orders?status=resting` lists shard-2 orders."

### Read 7 — positions (reconcile-first)
- **Method / URL:** `GET …/portfolio/positions`. **Parse:** `reconcile_positions_clean` — any `KXBTC*` row with non-zero `position`/`position_fp` ⇒ NOT clean ⇒ refuse to arm (degrade to dry). Malformed/None ⇒ fail-closed not-clean.
- **Non-2xx handling:** exception → `positions=None` → reconcile fails closed → degrade to dry.
- **Status:** **PROD-PROVEN** (`run_v32.main` line ~1626; same portfolio read the box uses) **/ payload shape DOCS-ONLY** (`market_positions[]`). Low risk (fail-closed). **→ MUST CONFIRM** the exact key.

### Read 8 — balance (S4 day-loss)
- **Method / URL:** `GET …/portfolio/balance`. **Parse:** `parse_balance` requires BOTH `balance_dollars` (str) and `balance` (int cents) and that they agree to tol; never `int()`s a dollar field; unexpected shape ⇒ not-ok ⇒ no S4 latch (arm proceeds on the other gates).
- **Non-2xx handling:** exception → `s4=None` (S4 skipped, other gates still apply).
- **Status:** **PROD-PROVEN** (box S4 uses the identical `parse_balance`).

### Read 9 — markets discovery (strikes / buckets / 15M)
- **Method / URL:** `GET …/markets?series_ticker={KXBTCD|KXBTC|KXBTC15M}&min_close_ts={t}&max_close_ts={t}&limit=1000[&cursor=…]` (paged ≤ `_MAX_PAGES`). Public read.
- **Parse:** `resp["markets"]`; per-ticker `exchange_index` via `coerce_exchange_index` (fail-closed None), strike floor via `parse_strike_ticker`, bucket (floor,cap). Empty strike/bucket universe ⇒ stand down; 15M failure is recording-only (`discover_co_settling_15m_safe` never stands the window down).
- **Non-2xx handling:** `rest_get` bounded retry then raise ⇒ strike/bucket discovery failure stands the window down (correct); 15M swallowed.
- **Status:** **PROD-PROVEN** (same `/markets` discovery as `wake`/`record_range`, run all week).

### Read 10 — settlement backfill
- **Method / URL:** `GET …/markets/{ticker}` (exact-ticker). **Parse:** `fetch_market_result_v32` → `market.result ∈ {yes,no}` else None (fail-closed). Idempotent, never raises out of `prepare()`.
- **Status:** **PROD-PROVEN** (mirror of `run_window._fetch_market_result`).

### Read 11 — /health (arming caps)
- **Method / URL:** `GET http://127.0.0.1:8642/health` (proxy's own endpoint, NOT under `/trade-api/v2`; unsigned-safe). **Parse:** `get_health` → `v32_caps_agree`: `orders_enabled` true; `max_contracts_per_order ∈ [contracts, 2]`; ticker_prefixes cover BOTH `KXBTC-` and `KXBTCD-` (via `probe.startswith(p)`, the proxy's own gate); `orders_remaining_today ≥ 200`. Any miss → degrade to dry.
- **Non-2xx handling:** non-200/exception → `{}` → arming refused.
- **Status:** **VERIFIED-LIVE** (arming reached armed in incident-2, so /health passed) **+** now cross-checked to the real proxy cap parser (`test_arming_gate_prefix_acceptance_implies_proxy_accepts_both_series`).

### Read 12 — /ws-auth (WS mint, fresh every dial)
- **Method / URL:** `GET http://127.0.0.1:8642/ws-auth`. **Parse:** `{ws_url, headers}` on 200; 503 ⇒ `ProxyUnsignedError`; other ⇒ `ProxyError` (fail-closed, NOT retried — the caller's reconnect re-mints fresh). No orders are ever sent over the WS; private `fill`/`market_positions` channels are subscribed only when armed.
- **Status:** **PROD-PROVEN** (V2 WS envelope, adapted F14).

**Summary counts:** VERIFIED-LIVE **5** (Writes 1, 4; Reads 5, 6, 11) · PROD-PROVEN **5** (Reads 7, 8, 9, 10, 12) · DOCS-ONLY **0** standalone · UNVERIFIED **2** (Writes 2, 3 — batch/single wing-create *response* shape). Positions payload key (Read 7) is a soft DOCS-ONLY sub-item on the confirm list.

---

## 2. Invariants — enforcement + the test that pins it

| Invariant | Enforced by | Pinned by |
|---|---|---|
| Never two live rests (venue-truth + internal) | Sequential replace (cancel→await `OrderCancelled`→place, `core._requote`); pre-place venue GET `_pre_place_invariant`; `cancel_failed` stand-down never frees a place | `test_v32_cancel_shard.test_incident_replay…` (`venue.max_concurrent ≤ 1`), `test_pre_place_invariant_blocks…` |
| post_only can never cross | `_rest_body` always `post_only=True`, `good_till_canceled` (maker); a post_only reject → `_reject_place` | `test_place_rest_wire_body_post_only_gtc_expiration` |
| Expiration always set and ≤ quote end | `_place_action` / `_rest_body`: `expiration_time = close_epoch − quote_end_s` (int seconds); fallback identical | `test_place_rest_wire_body_post_only_gtc_expiration` |
| Wings taken unconditionally on fill, only for the filled count | `_wing_step` initial take is unconditional (F-2), `count = rest_fill.count` | core suite `test_v32_core*`; `test_take_wings_batch_body_ioc_and_fills` |
| Fill de-dup across ws / poll / cancel-reply | trade_id `_seen_trade_ids`; money-math `booked_rest_oids` (exec) + `_rest_fill_booked_oids` (driver); core single latch `rest_fill is None` (in `_apply_fill`, `_apply_cancelled`, `book_late_rest_fill`) | `test_fill_dedup_ws_then_poll`, `test_fill_dedup_poll_first_then_ws`, `test_delete_404_status_executed_routes_fill` |
| POST timeout = unknown outcome | `_place_rest`: status None/≥500 ⇒ record coid + `post_unknown_outcome` stand-down, no re-place | `test_post_unknown_outcome_records_coid_and_stands_down`, `test_post_5xx_is_treated_as_unknown_not_clean_reject` |
| 404 on cancel ≠ gone | `_cancel_nonok`: status GET → terminal resolves, else shard-aware retry, else `cancel_failed`+stand-down | `test_delete_404_still_resting_retries_then_cancel_failed_stands_down` |
| FrozenExecutor unreachable in armed | `build_executor` (ONE selection site); `LiveExecutor.on_action` raises on `WOULD_*`, `FrozenExecutor.on_action` raises on real kinds | `test_frozen_executor_refuses_real_kind`, `test_live_executor_refuses_would_twin`, `test_build_executor_selects_by_effective_mode` |
| Budget/prefix caps agree with the proxy | `v32_caps_agree` mirrors the proxy's `startswith` gate; envelope + REST body cross-checked against the REAL proxy source (ast-extracted, no .env/PEM) | `test_orders_proxy_compat.py` incl. **new** `test_executor_rest_bucket_no_body_capped_only_under_range_prefix`, `test_arming_gate_prefix_acceptance_implies_proxy_accepts_both_series` |
| Day guard | `decide_v32_arming` refuses on corrupt guard or a latched S4/S1_LEGGED (separate `v32_stops_*.json`) | `test_v32_stops.py` |
| Startup sweep scope (ours only) | `cancel_stale_open_orders`: only `KXBTC*`, only our `v32-` coid or coid-less; shard-aware; foreign coid skipped | `test_startup_cancel_skips_foreign_coid…`, `test_startup_sweep_routes_each_orders_shard` |
| Price units per side on every leg | REST NO buy @ n → V2 `side "ask"`, price `1−n` (`translate` + `_wire_price_no`); wings `yes`→limit / `no`→1−limit; fills normalized to side-space at the `normalize_fill_to_side` choke point | `test_orders_translate.py`, `test_place_rest_wire_body…`, new REST-body assertion (side "ask", price "0.4600" for n=0.54) |
| Shard on every write | REST body `exchange_index`; cancel `?exchange_index`; each wing leg `exchange_index`; startup cancel per-order shard | `test_executor_cancel_sends_sharded_delete`, `test_shard2_entry_with_exchange_index_passes_proxy_parser`, new REST-body assertion |

---

## 3. Defects found

1. **[TEST GAP — FIXED]** The proxy-compat suite proved only strike/15M creates against the real proxy cap parser; the **resting bucket-NO on a `KXBTC-` RANGE ticker — the primary order (~40 creates/window)** — was never cross-checked, and the proxy's DEFAULT prefixes (`KXBTC15M,KXBTCD`) do **not** cover `KXBTC-` (verified: `check_order_caps` rejects `KXBTC-26SEP1418-B78850` under the default set). No functional bug — the arming gate `v32_caps_agree` fail-closes on exactly that (default set ⇒ degrade to dry), and the two directions agree in the safe sense (gate acceptance ⇒ proxy acceptance for the real ticker). Added `test_executor_rest_bucket_no_body_capped_only_under_range_prefix` + `test_arming_gate_prefix_acceptance_implies_proxy_accepts_both_series` to pin it.

2. **[MINOR — noted, not changed]** `_reject_place` records the UNKNOWN-outcome placeholder RestRecord with a hardcoded `count=1`. Benign at the frozen `params.contracts=1`, and any real fill takes its count from the frame's `count_fp` (never the placeholder), so it cannot mis-size a hedge today; it would only matter if `contracts` were raised to 2 AND a fill frame arrived without `count_fp`. Left as-is to keep the change surface minimal; flagged for the next params-widening review.

3. **[OBSERVATION — proxy, out of scope]** The proxy forwards an *unrecognized* order-write POST uncapped when the path is not under a write prefix (the 20:00Z doubled-prefix POST went to the market-data host uncapped → venue 404). The client fix (`compose_rest_url` single-prefix assertion) removes the only way V3.2 can produce such a path, which is the correct fix location; a proxy-side defense-in-depth (reject any `/trade-api/v2/trade-api/…` path) is noted for the proxy owner but not actionable in this worktree.

---

## 4. MUST CONFIRM on the first re-armed window

Confirm each against the named journal record the moment it first appears:

1. **Batch wing-create response shape (Write 2).** First `take_wings` with 2 legs → the venue reply must parse as `{"orders":[slot,slot]}` with per-slot `order_id`, `fill_count`, `average_fill_price` (YES-space), `average_fee_paid`. **Proof record:** a `wing_fill` (or `wing_no_fill`) journal line whose booked side-space price + fee are finite and match the leg's limit within margin. If the reply is not `{"orders":[…]}`, every wing books `count 0` and the set never completes — watch for a `retry_wing` storm.
2. **Single wing-create response (Write 3, RETRY_WING).** First 1-leg retry → `parse_single_response` must yield a real `fill_count`/`order_id`. **Proof record:** `wing_fill … "path":"batch"` on a single-leg take.
3. **Positions payload key (Read 7).** Confirm the live `/portfolio/positions` carries `market_positions[]` with `ticker` + `position`/`position_fp`. **Proof record:** the window arms (reconcile-first passed) with a `degrade_to_dry` ABSENT for a "positions" reason on a genuinely flat account.
4. **Cancel-race `reduced_by` on a partial (Read/Write 4 interaction).** If a fill ever slips in during a cancel, confirm `reduced_by` < `count` and `filled = count − reduced_by` matches the order-status `fill_count_fp`. **Proof record:** `cancel_confirmed … "reduced_by": "<1", "filled_before_cancel": >0, "via":"delete"` cross-agreeing with a `rest_fill` on the same `order_id`.
