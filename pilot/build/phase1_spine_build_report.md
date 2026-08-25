# Phase 1 build report — market-data spine (opus48 BUILDER)

Built 2026-08-21. Location: `pilot/service/` (new package) + `pilot/tests/`. No files under
`sim/`, `historical-data/`, or `degeneracy-proxy/` were modified; no `.env`/`*.pem` read; no
sealed-day file read by any code path here (the spine reads only LIVE data via the proxy). All
Kalshi access is via the proxy at `http://127.0.0.1:8642`.

## Component inventory

| # | File | Component | Lines |
|---|------|-----------|-------|
| 1 | `service/proxy_auth.py` | ProxyAuth (WS auth mint + REST reads via proxy) | ~170 |
| 2 | `service/ws_client.py` | KalshiWebSocketClient (multi-ticker, market-keyed) | ~300 |
| 3 | `service/book.py` | BookMirror (full-depth per-market book, Decimal) | ~230 |
| 4 | `service/journal.py` | Journal (in-memory buffer -> JSONL at close) | ~120 |
| 5 | `service/wake.py` | WakeContext (leg discovery, ladder map, balance gate) | ~330 |
| 6 | `service/record_window.py` | Passive window recorder (runnable) + watchdog | ~330 |
| 7 | `service/replay.py` | Golden replay (deterministic book reconstruction) | ~70 |
| 8 | `pilot/tests/*` | 7 test modules | — |

## Tests

`python -m pytest tests/` -> **110 passed, 0 failed** (0.61s). Breakdown: ws_client 32, wake 26,
book 15, proxy_auth 12, record_window 11, replay 8, journal 6.

Coverage of the required additions: multi-ticker dispatch keying; missing-`market_ticker`
fail-closed (recorded, not dispatched); seq-gap -> resnapshot flow (incl. end-to-end
`handler` force-close); lag/silence watchdogs with a fake clock (the 07-12 lag signature carried
over verbatim); journal flush + reload round-trip; golden-replay determinism (same journal twice ->
byte-identical) and live-vs-replay parity (`recorder.book_tops == replay_books(journal)`); all six
discovery fixtures (normal hour, 06-09 missing-hourly -> StandDown, 21:00 $250, Friday 21:00 $500,
unexpected step -> alarm + strangle_disabled, dual-generation listing); ProxyAuth fresh-fetch-per-
dial and 503-unsigned handling.

## Ported vs new vs changed (specific)

### Ported nearly whole (V2 -> V3, semantics preserved)
- **ws_client.py lag/silence/seq-gap machinery** from `degeneracy_v2/kalshi/ws.py`: `_update_lag`,
  `_parse_server_ts` (ts_ms -> ts numeric -> ISO), `current_lag_seconds`, `silence_seconds`,
  `data_age_seconds = max(lag, silence)` with None-fail-closed, `force_close` baseline resets,
  `_check_seq` (forward-skip-only gap, rewind/dup ignored, malformed inert, per-sid, orderbook-only),
  `handler` force-close-on-gap. The V2 07-12 docstring lessons are kept in comments crediting the
  source. The V2 ws tests carried over (adapted for market-keying).
- **proxy_auth.py `rest_get` retry** from `degeneracy_v2/kalshi/rest.py::_request_with_retry`:
  bounded retry on 429/5xx/ConnectionError/Timeout, backoff 1s/2s/4s, 4 attempts, GET-only. POST is
  deliberately absent (order writes are the Phase 3 Executor's job; never retried — V2 law).
- **book.py snapshot/delta arithmetic + binary-identity ask** from
  `degeneracy_v2/signals/order_book.py`: snapshot drops zero-count levels; delta adds to a level and
  pops it at <= 0; ask = `1 - opposite best bid`, ask size = opposite bid's size; crossed books
  reported not hidden.

### New (no V2 antecedent)
- **ProxyAuth `ws_connect_params()`** — the F14 shim: GET `/ws-auth`, fresh every call, 503 ->
  `ProxyUnsignedError`, non-200 -> `ProxyError`, no retry (auth is time-sensitive).
- **WakeContext** — dynamic leg discovery + ladder-map check + balance/affordability gate. Entirely
  new; V2 had `get_current_market` (single next-to-close market by series) but no co-settling pair
  discovery, no generation grouping, no ladder-map.
- **Journal**, **replay.py**, **record_window.py**, **BookMirror multi-market keying + suspect
  flag** — all new for the pilot.

### Changed from V2 (deliberate departures)
- **Multi-ticker + market-keyed dispatch** (F14): constructor takes a list; every channel is
  subscribed with all tickers; callbacks receive `(market_ticker, payload)`. A frame missing
  `market_ticker` is fail-closed (journaled by the tap, NOT dispatched; counted in
  `dropped_no_market`). V2 was single-ticker and dispatched bare payloads.
- **Auth via ProxyAuth**, re-minted on every dial (`connect()` calls `ws_connect_params()` each
  time). V2 held a `KalshiAuth` and signed per request itself.
- **Channel set**: public = orderbook_delta, trade, ticker (always); market_positions + fill only
  when `include_private=True`. V2 subscribed ticker always and the rest under `include_private`; the
  pilot needs the book live in shakedown, so the book channel is public here.
- **Unknown-channel fall-through DROPPED**: V2 routed unknown channels carrying `price_dollars` to
  `on_ticker`. The pilot drops unknown channels (fail-closed; an unrecognized frame is not silently
  treated as a ticker).
- **Money is Decimal, one representation**: prices = Decimal DOLLARS (e.g. `Decimal("0.44")`,
  `Decimal("0.001")` for the deci-cent 15m ladder), sizes = Decimal CONTRACTS, coerced via
  `Decimal(str(x))`. V2's book used floats.
- **REST-authoritative BBO cache NOT ported** (`rest_*`, `authoritative_*`, `freshest_*`,
  `wsreal_*`): the pilot's mirror is WS-only; REST BBO reads (for σ̂ / wake) go through the proxy
  REST path, not this container.
- **Float-residue machinery NOT ported** (`_ws_real_ask`, `residue_topped_count`): it existed only
  to paper over float delta-arithmetic. Decimal arithmetic is exact, so a level returning to 0 is
  removed exactly (test `test_decimal_delta_arithmetic_is_exact_no_residue` pins this).
- **Malformed delta = suspect** (fail closed): V2 silently returned on a bad side; the pilot marks
  the book suspect until the next snapshot.

## CONFESSIONS (judgment calls and interpretations)

1. **Leg selection rule (dual-generation).** I implemented "smallest window duration among
   candidates sharing the close_time" as: group co-settling markets into ladders by
   `(event_ticker, open_time)` — each distinct open_time is a generation — then pick the ladder with
   the smallest `close - open` window (tie-break: then most strikes, then lowest event_ticker). On
   the verified 2026-07-31 21:00 UTC data this selects the NARROW $250 generation (opened the day
   before) over the WIDE $500 one (opened a week before). Since 07-31 was a Friday, the map expects
   $500, so the selected ladder DEVIATES -> `strangle_disabled=True` + alarm, sub-$1 continues. This
   is a literal reading of the commission's "select by smallest window ... validate the ladder the
   specific market pairs" and is fail-closed (a step we can't confirm stands the strangle down). If
   Brad intends the map hour/weekday to instead SELECT which generation to pair, that is a different
   rule and I did not assume it — flagged for review.

2. **Ladder-map hour/weekday keyed on the CLOSE time directly** (`close_dt.hour == 21`,
   `close_dt.weekday() == 4` in UTC), not the loader's `close-1s` day-assignment convention. 21:00
   is unambiguous; a 00:00 close is not a special hour, so the -1s midnight subtlety does not affect
   the map. Confessed in case the weekly $500 boundary ever interacts with a 21:00 close near a
   day boundary (it does not in the corpus).

3. **Active-status semantics.** `ACTIVE_STATUSES = {"active", "open"}`; a leg is active iff its
   close is in the future AND >= 1 of its markets reports an active status. The corpus files are all
   `"finalized"` (settled), so the live open-market status string is a **passive-run verification
   item**. I require >= 1 active (not all) because ladders can have early-settled wings; this avoids
   standing down a normal ladder while still refusing an all-settled one. Fail-closed on the time
   dimension (past close -> stand down).

   **RESOLVED by live observation 2026-08-21.** The allow-list was WRONG and stood every window down
   on the first live shakedown: 15M markets are listed days ahead as `"initialized"` and only flip to
   `"active"` at `open_time` (:45), so at the :40 wake the co-settling 15M leg is ALWAYS
   `"initialized"`. Fixed by making leg SELECTION status-agnostic (select by `close_time`; refuse
   only an ALL-dead leg via `DEAD_STATUSES = {"settled","finalized","closed","determined"}`),
   dropping the `status=open` param from `_fetch_series_markets`, and holding the WS dial in
   `run_window` until `(15M open_time − 5s)`. `ACTIVE_STATUSES` is retained only as an alias of the
   new `LIVE_STATUSES` (documentation); selection no longer uses an allow-list. See
   `phase4_harness_review.md` checklist item 3 for the observed facts and the regression tests.

4. **WS payload field names assumed identical to V2's** (`yes_dollars_fp`/`no_dollars_fp` snapshot
   pairs `[price_dollars, size]`; delta `side`/`price_dollars`/`delta_fp`; `market_ticker` on every
   dispatched frame; `ts_ms`/`ts` server timestamps). V2 consumed exactly these, but the live
   proxy's actual orderbook payload shape for these markets is a **first-passive-run verification
   item** — if the wire uses different keys, only `book.py` field lookups and `_parse_server_ts`
   need adjusting (the golden-replay tests use these names, so a mismatch surfaces immediately live).

5. **`ws_connect_params` is not retried.** A signature is ephemeral and a 503-unsigned is a proxy
   state, not a transient blip; the recorder's reconnect loop re-invokes it fresh instead. A dial
   that raises is journaled as an alarm and retried by the outer loop, throttled ~`poll_seconds` by
   the supervisor's sleep (no tight busy-loop).

6. **`rest_get` path convention**: `path` is the API path AFTER `/trade-api/v2` and must start with
   `/` (e.g. `/markets`). The full URL is `{base}/trade-api/v2{path}`.

7. **Series tickers hardcoded** `KXBTC15M` (15-minute) and `KXBTCD` (hourly), read off the corpus
   event tickers. The market fetch narrows with `min_close_ts == max_close_ts == target_epoch` and
   `status=open`, then filters exactly in `_group_ladders`; the exact Kalshi `/markets` param support
   (close-ts window, cursor pagination shape) is a **live-verification item**.

8. **`subscribe_tickers()` returns the 15m market + the FULL hourly ladder** by default (a ladder can
   be ~188 strikes). `record_window --max-hourly-strikes N` caps it. Phase 2 will instead select
   near-the-money strikes once an anchor exists. I chose full-ladder-by-default for spine
   completeness (Phase 2/3 need the ladder to map strikes / compute nearest-strike C); the WS volume
   of ~188 * 3 channels is an ops consideration flagged for the first passive runs.

9. **Watchdog force-close thresholds** (passive spine): lag 30s, silence 45s (V2's 45s watchdog).
   These are RECONNECT thresholds, not the tight entry-gate freshness (2s book-age / few-second lag)
   which is a Phase 2 `decide()` concern. Tunable via `run_recording` args.

10. **`suspect` after a seq gap is NOT reconstructable in replay.** sid/seq are consulted live but
    not journaled (byte-identity constraint). Replay cannot see the reconnect; it does not need to —
    the post-reconnect FRESH snapshot IS journaled and replaying it rebuilds the book wholesale
    (clearing suspect). So the replayed `suspect` flag reflects malformed DELTAS (journaled,
    replay-visible), not reconnects. Documented in `replay.py`.

11. **Journal is not thread-safe.** Phase 1 appends only from the single asyncio/WS thread. When the
    Phase 3 Executor/Reconciler thread also appends, the caller must serialize (noted in the
    docstring).

## Known limitations (validatable only against live WS traffic in the passive runs)

- The actual live orderbook/trade/ticker/fill **payload field names and shapes** (confession 4).
- The live open-market **status strings** and per-strike ladder status behavior (confession 3).
- The `/markets` and `/events` **endpoint param support** and pagination (confession 7), and the
  `/ws-auth` response shape and 503 body (proxy contract — the Phase 0 proxy work owns this).
- **Signature freshness tolerance** on the WS handshake (owed-verification in PLAN) — `connect()`
  re-mints fresh, but whether a mint survives a slow dial is a live measurement.
- Whether a full ~188-strike hourly subscription is comfortable on one socket (confession 8).
- `record_window` has NOT been run against the live proxy from this build (house discipline). It is
  smoke-tested with fakes: import + `--help` + a full `run_recording` run with a `FakeWsClient`
  (deadline exit, flush, summary) and a stale-lag force-close path.

## How to run (when Brad/ops chooses)

```
cd pilot
python -m service.record_window                 # next top-of-hour, full ladder
python -m service.record_window --close-time 2026-08-21T22:00:00Z --max-hourly-strikes 40
```
Journals land in `pilot/journals/<close>.jsonl`; one summary line per window in
`pilot/journals/summary.jsonl`. Stand-downs write a summary line and exit 0 (no socket opened).
