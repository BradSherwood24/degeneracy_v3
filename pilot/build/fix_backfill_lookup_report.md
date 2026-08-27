# Fix: settlement-backfill market lookup (single-market endpoint)

Branch: `fix/backfill-lookup`  ·  file: `pilot/service/run_window.py`

## Bug
`WindowService._fetch_market_result` fetched the settled result with
`self.proxy.rest_get("/markets", {"ticker": ticker})`. Kalshi's **list** endpoint IGNORES the
singular `ticker` param and returns 100 unrelated markets. `_parse_market_result` correctly requires
an exact-ticker match (F1 guard), so it returned `None` for every leg and the
`_settlement_backfill_sweep` silently never fired — settled, pinned box pairs stayed
`realized_unsettled` in `pilot/ledger/pilot_ledger.jsonl` forever, with no journal trace of the wait.

Live verification (read-only GETs to the proxy, `KXBTC15M-26AUG262100-00`):
- `GET /markets?ticker=<tk>`  -> `markets` shape, n=100, none matching (first = `KXMVECROSSCATEGORY-...`)
- `GET /markets?tickers=<tk>` -> `markets` shape, n=1, the exact market (`result: no`)
- `GET /markets/<tk>`         -> `market` shape, exact ticker, `result: no`

## Fix
`_fetch_market_result` now calls the **single-market** endpoint
`self.proxy.rest_get(f"/markets/{ticker}")`, whose `{"market": {...}}` shape the parser already
handles under its exact-ticker check. Fail-closed behavior is unchanged: any exception or a
not-yet-settled / foreign / list-shape payload still returns `None` and the sweep waits.

Added visibility: when the sweep finds a pending row it cannot yet settle (a leg unsettled or
unavailable), it now journals a compact `settlement_backfill_pending` record
`{window, tickers, unsettled_leg, settled_legs}` — one per pending window per wake (the sweep runs
once per wake) — so a silent wait is visible offline. No behavior change to the settle path.

## Tests (`pilot/tests/test_box_wiring.py`)
- `test_settlement_backfill_single_market_endpoint_backfills` — `{"market": {...}}` exact-ticker
  shape via the real proxy path -> pinned pair backfills (+$1); asserts the singular
  `/markets/{ticker}` path was used with NO list `?ticker=` param.
- `test_settlement_backfill_foreign_ticker_shape_waits` — `{"market": {...}}` with a foreign ticker
  -> F1 guard -> waits, nothing settled.
- `test_settlement_backfill_list_shape_100_unrelated_waits` — a 100-unrelated-market list payload
  (the old buggy shape) -> no `markets[0]` fallback -> waits.
- `test_settlement_backfill_pending_is_journalled` — one leg settled, one open -> a single
  `settlement_backfill_pending` record naming the unsettled leg; no backfill written.

## Result
`cd pilot; python -m pytest -q` -> **559 passed** (junctions: `historical-data`, `sim/out`;
removed with `rmdir` after the run).
