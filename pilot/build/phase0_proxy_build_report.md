# Phase 0 build report — Kalshi signing-proxy extensions

Builder: opus48. Date: 2026-08-21. Target repo: `C:\Users\Brads\Python_stuff\degeneracy-proxy`.

**These changes take effect only when Brad restarts the proxy** (`python proxy.py` /
`run.ps1`). The service reads config once at startup; nothing hot-reloads.

## Files touched

| File | Before | After | Note |
|---|---|---|---|
| `degeneracy-proxy/proxy.py` | 233 lines | 506 lines | extended (see below) |
| `degeneracy-proxy/tests/conftest.py` | — | 29 lines | new (path shim + fake_signer) |
| `degeneracy-proxy/tests/test_proxy.py` | — | 425 lines | new (61 tests) |

No other files created or modified. `.env` and `*.pem` were never read, written, or
referenced by build code (deny-ruled; confirmed). No live network calls in tests.

## What changed in `proxy.py`

New imports: `threading`, `datetime`/`timezone`, `decimal.Decimal`/`InvalidOperation`.

New module constants:
- `_ORDER_CREATE_PATHS` — frozenset of the two POST create paths
  (`/trade-api/v2/portfolio/orders`, `/trade-api/v2/portfolio/orders/batched`).
- `_WS_URLS` (prod/demo wss URLs) + `_WS_PATH` (`/trade-api/ws/v2`).
- Cap defaults: `_DEFAULT_MAX_CONTRACTS="2"`, `_DEFAULT_TICKER_PREFIXES="KXBTC15M,KXBTCD"`,
  `_DEFAULT_DAILY_BUDGET="100"`, `_BUDGET_FILENAME="order_budget.json"`.

New pure functions (no sockets / no shared state — unit-tested directly):
- `is_order_create(method, path) -> bool` — only POST to a create path (query +
  trailing slash stripped) qualifies for caps.
- `parse_count(value) -> Decimal` — fixed-point string/number → Decimal; rejects
  `None`, bool, and non-numeric.
- `parse_order_entries(body) -> list[dict]` — single `{ticker,count}` or batch
  `{orders:[...]}`; raises `BodyParseError` (new exception) on any other shape.
- `check_order_caps(entries, max_contracts, allowed_prefixes) -> dict | None` — per-entry
  ticker-prefix whitelist + per-entry count cap; returns a `{error,cap,detail}` violation
  dict or `None`.
- `build_ws_auth_response(signer, env) -> (status, dict)` — 200 `{ws_url, headers}` where
  `headers = signer.headers("GET", "/trade-api/ws/v2")`; 503 in unsigned mode. No key
  material in the body.
- `health_payload(config) -> dict` — original `/health` shape plus the additive fields.

New class `OrderBudget` — thread-safe, restart-persistent daily counter. A `threading.Lock`
guards every read and mutation; the JSON file (`order_budget.json`, keyed by UTC date) is
the source of truth and is reloaded under the lock on each op, so a mid-day restart resumes
the count instead of resetting it (review F12). Writes are atomic (`tmp` + `os.replace`).
Methods: `try_consume(n) -> (allowed, used_after, remaining_after)`, `snapshot() -> (used, remaining)`.

`Config.__init__` additions (startup-read via `os.getenv`, matching the existing pattern):
`max_contracts_per_order`, `ticker_prefixes` (tuple), `daily_order_budget`, and
`order_budget = OrderBudget(PROXY_DIR/"order_budget.json", daily_order_budget)`.

`Handler._handle` changes:
- `/health` now returns `health_payload(CONFIG)` (additive only).
- New `GET /ws-auth` branch, placed before the order gate so it works regardless of
  `ALLOW_ORDERS`.
- Body is now read once for all non-GET; on an order-create request the caps run BEFORE
  any upstream contact: parse (400 fail-closed on unparseable) → count/prefix caps (403) →
  budget consume (403). Only after all pass is the request forwarded. One structured log
  line per block and per accepted create.

Preserved unchanged: 127.0.0.1-only bind; read-only-by-default 403 on non-GET; two-host
routing (`_ORDER_WRITE_PREFIX` → orders_base); KALSHI-ACCESS-* stripping from client
headers; key material never logged/returned; unsigned mode stays read-only.

## Config knobs + defaults

| Env var | Default | Meaning |
|---|---|---|
| `MAX_CONTRACTS_PER_ORDER` | `2` | max `count` per single order / per batch entry |
| `ORDER_TICKER_PREFIXES` | `KXBTC15M,KXBTCD` | comma-separated allowed ticker prefixes |
| `DAILY_ORDER_BUDGET` | `100` | max order-CREATE entries per UTC day (batch of N costs N) |

All three are read once at startup. Only the budget COUNTER is dynamic state
(`order_budget.json`). No new secrets; existing knobs untouched.

## /health additive fields

Original keys (`status`, `env`, `signed`, `orders_enabled`, `key_fingerprint`) unchanged.
Added: `caps {max_contracts_per_order, ticker_prefixes, daily_order_budget}`,
`orders_used_today`, `orders_remaining_today`.

## Test inventory + result

`python -m pytest tests/ -q` → **61 passed** (no failures, no skips), ~4s, no network.

- `is_order_create`: 9 param cases (create/batch/query/trailing-slash true; GET/DELETE-cancel/batch-cancel/amend/other false).
- `parse_count`: 5 accept (incl. `"2.00"`, ` 1.00 `, int, float) + 7 reject (None, bool, "", non-numeric, object).
- `parse_order_entries`: single, batch, str+bytes, + 8 fail-closed cases.
- `check_order_caps`: pass single/batch; count over limit; fractional over limit; non-positive; missing count; bad prefix; missing ticker; batch first-violation index.
- `OrderBudget`: consume/refuse; **persistence across simulated restart (tmp_path)**; refusal-doesn't-mutate; corrupt-file recovery; **thread-safety (200 threads on a Barrier, exactly `budget` succeed)**.
- `build_ws_auth_response`: prod shape; demo url; **no private-key material in body**; unsigned 503; signs exactly `GET /trade-api/ws/v2`. Uses a **throwaway RSA key generated in the fixture** — never a real credential.
- `health_payload`: additive-fields shape (signed) + unsigned.
- Handler smoke (real ThreadingHTTPServer on ephemeral port, only short-circuiting paths — never forwards upstream): `/health` 200; `/ws-auth` 200; **read-only 403 on non-GET (existing behavior intact)**; prefix cap 403; count cap 403; unparseable 400; budget-exhausted 403.
- Module-import smoke: all pure helpers + classes exposed.

## CONFESSIONS (spec interpretations I had to make)

1. **Create-path detection is a whitelist of two POST paths.** Caps apply to
   `POST /trade-api/v2/portfolio/orders` and `.../orders/batched` only. DELETE cancels
   (single and batch) are correctly exempt from caps but still `ALLOW_ORDERS`-gated.
   **Amend/decrease POSTs (`.../orders/{id}/amend`, `.../decrease`) are NOT contract/prefix/
   budget-capped** — their body shape differs from create and the pilot's Executor never
   issues them (sell-down is a fresh create POST, which IS capped). This is a theoretical
   oversize hole via amend; flagged for Brad. If you want it closed, say so and I'll add an
   amend-count cap.

2. **400 vs 403 split.** Structural body failures (invalid JSON, top-level not an object,
   `orders` not a list of objects, empty body) → **400** `{error,cap:"body_parse",detail}`.
   Semantic cap failures (bad/missing ticker, missing/unparseable/non-positive/over-limit
   count, budget exhausted) → **403** `{error,cap,detail}`. Both fail closed and never
   forward; only the status code differs. Spec said "unparseable body → 400" and "violations
   → 403"; a missing/unparseable `count` inside an otherwise-valid JSON entry I classified as
   a 403 cap violation (`cap:"max_contracts_per_order"`), not a 400 — it's structurally
   parseable, just cap-failing.

3. **Budget counts ATTEMPTS, consumed pre-forward.** `try_consume(len(entries))` runs before
   forwarding. If upstream later rejects (or the network fails → 502), the budget is still
   spent. This is the safe direction (fails toward fewer orders) and avoids a
   forward-then-untangle race, but it means `orders_used_today` counts placement attempts,
   not confirmed fills.

4. **Budget cost = number of entries, not sum of contract counts.** Read "max order-create
   entries per UTC day. Batch of N costs N" as N = entry count. A 2-entry batch of 2
   contracts each costs 2 budget, not 4. The per-contract limit is enforced separately by
   `MAX_CONTRACTS_PER_ORDER`.

5. **Non-positive and fractional counts.** `count <= 0` is rejected (cap
   `max_contracts_per_order`, "non-positive"). A fractional count is compared as a Decimal
   against the integer max (`2.50 > 2` → rejected); I did not round.

6. **`order_budget.json` keeps all past UTC dates** (one small key per day). No pruning —
   negligible growth, and the history is auditable. Say the word if you'd rather it keep
   only today.

7. **Empty batch (`{"orders": []}`) is allowed** (0 entries, 0 budget) and forwards; upstream
   rejects it. Not treated as a parse error.

8. **Test-import note (not a code change):** importing `proxy` in the test process runs its
   module-level `CONFIG = Config()`, i.e. the proxy's own `load_dotenv`/`_load_signer`
   startup — the sanctioned key-loading mechanism. The tests themselves never read `.env`
   or any `*.pem`, never reference key material, and use only throwaway generated RSA keys.
