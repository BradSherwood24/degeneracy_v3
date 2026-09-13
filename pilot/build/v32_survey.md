# Pilot architecture survey for V3.2 (2026-09-13, read-only, Explore agent)

All paths relative to `pilot/`. Line numbers as of main `afcde6b`. Purpose: tell the V3.2 builders what to reuse and
what is hard-coded to the box/corridor pilots.

## 1. `run_window.py` control flow (2,713 lines)

**Process model.** One process == one window. Launched by Task Scheduler at UTC :40 via `ops/register_task.ps1`
(fixed local minute computed from UTC offset, `-RepetitionInterval 1h`, `-MultipleInstances IgnoreNew`). Entry point
`main()` at `service/run_window.py:2687`; `close_iso` defaults to `next_top_of_hour_iso(time.time())`.

**Levers.** `resolve_mode(cli, mode_txt_path)` at `:214` — CLI `--mode` wins, else `ops/mode.txt`; unknown ->
`"shakedown"` (fail closed). `resolve_strategy` at `:222` returns `(strategy, valid)` from `ops/strategy.txt`;
unknown -> `("corridor", False)` and the window runs corridor DRY, never armed. `VALID_STRATEGIES = ("corridor", "box")`
at `:152`.

**`WindowService.prepare()` — `:668`, the startup order:** (a) policy load with sha (`:700-718`); (b) S5 arming
(`:726-805`): `stops.arming_check(...)`, `_caps_agree(health)` at `:589` (proxy `max_contracts_per_order` >= pairs,
<= `PILOT_MAX_CONTRACTS_CEILING=2`, `caps["ticker_prefixes"]` must cover `TICKER_PREFIXES`), day-guard read; any
failure in armed mode -> `degrade_to_dry` (`:801`); (c) reconcile-first (`:812-857`): `GET /portfolio/positions`,
any inherited position in our series -> refuse the window; also `_settlement_backfill_sweep()` (`:1859`); (c2) S4
balance gate (`:866-1011`); (d) `WakeContext.sweep(close_time)` (`:1015`); `_build_armed_stack()` (`:1083`).

**Strategy invocation is callback-on-book-update, not a timer.** `BoxWindowRecorder._drive` (`box_runner.py:234`)
is called from `_on_snapshot`/`_on_delta`, parses `server_ts`, calls `driver.on_book_update(market, top, server_ts)`.
A frame with no server ts folds the book but never drives a decision (`box_runner.py:241-248`). No clock-tick pump is
wired in live mode — `BoxSignalDriver.on_clock_tick` exists (`box_runner.py:168`) but nothing calls it. **V3.2 needs a
clock source for its cutoffs and polls.**

**WS loop:** `_default_window_driver` -> `asyncio.run(_run_ws_window(...))` (`:2182-2219`): awaits the connect gate,
then `_dial_loop` (`:2234-2292`) — supervisor polls every `DEFAULT_POLL_SECONDS=0.5` calling `watchdog_action`;
force-close on lag>30s/silence>45s; re-dial with backoff 1->30s, `_MAX_CONSECUTIVE_REDIALS=8`.

**Timing (per window):** :40 wake -> prepare -> bounded journal gzip (<=3 files / <=60s, `journal_io.py:48-49`) ->
anchor poll until :45 -> WS dial at :44:55 -> decision events until `deadline = close_epoch + GRACE_SECONDS (10)`
(`record_window.py:38`) -> flush + ledger. Box entry T-600s..T-60s (`policy/box_params.json`); executor cutoff
`no_orders_after_s_to_settle=1`.

## 2. `ws_client.py` (329 lines)

- Channels: `_PUBLIC_CHANNELS = ("orderbook_delta", "trade", "ticker")` (`:60`); `_PRIVATE_CHANNELS =
  ("market_positions", "fill")` (`:61`) subscribed only when `include_private=True`. Optional `channels=` ctor arg
  (`:93`, `:104`) thins the set — `record_range.py:73` uses `("orderbook_delta","trade")`.
- Even when `include_private=True`, `WindowRecorder.callbacks` (`record_window.py:107-112`) sets only
  snapshot/delta/trade/ticker. `fill`/`market_positions` frames are journaled by the tap but never dispatched
  (`ws_client.py:243-244` maps them to `cb.on_position` / `cb.on_fill` if set). **V3.2 must set `on_fill`.**
- One connection covers all tickers; box subscribes ~189 tickers. Range hours are ~180-188 buckets.
- Auth: `ProxyAuth.ws_connect_params()` -> `GET http://127.0.0.1:8642/ws-auth` -> `(ws_url, headers)`, re-minted
  on every dial (`ws_client.py:129`, `proxy_auth.py:79`). 503 = proxy unsigned.
- Subscribe shape: `{"id", "cmd":"subscribe", "params":{"channels":[ch],"market_tickers":[...]}}` (`:186-190`).
- `on_message` (`:212`): stamp silence -> parse `{"type","msg"}` -> `_update_lag` -> seq-gap check (orderbook types,
  per-sid; gap -> `_reconnect_requested`) -> recorder tap with `{"type","msg"}` -> market-keyed dispatch on
  `payload["market_ticker"]`.
- Fully async on one asyncio loop; callbacks are synchronous and run on that loop — keep them fast.
- Gauges: `current_lag_seconds()` = `local_wall - server_ts`; `silence_seconds()`; `data_age_seconds()`.
  `_parse_server_ts` (`:305`) prefers `ts_ms`, falls back to `ts` numeric/ISO.

## 3. `book.py` (236 lines)

- `BookMirror` = one market. `yes_bids: dict[Decimal, Decimal]`, `no_bids`, `suspect: bool` (starts True),
  `malformed_delta_count`. Asks are derived: `best_yes_ask() = 1 - best NO bid` (`:192-204`).
- Snapshot fields: `yes_dollars_fp`, `no_dollars_fp` (`:114-115`). Delta fields: `side`, `price_dollars`,
  `delta_fp` (`:144-146`).
- No timestamps in the book; the driver carries server ts alongside (`BoxState.ts[ticker]`, `box.py:249`);
  freshness = `event.server_ts - state.ts[ticker] <= freshness_max_leg_age_s` (`box.py:291-302`).
- Accessors: `best_bid(side) -> Level|None`, `best_yes_ask()`, `best_no_ask()`, `depth_at(side, price)` (`:206`,
  bid-side depth only — no derived-ask depth accessor), `top_of_book() -> TopOfBook` (9 fields incl. `suspect`).
- Journal emission is in `WindowRecorder.tap` (`record_window.py:115-118`), not book.py.

## 4. Order stack — `executor.py`, `orders/envelope.py`, `orders/translate.py`, `proxy_auth.py`

- `Executor.execute(intent, t_minus_s=None, *, stop_authorized=False) -> ExecResult` (`executor.py:145`). Gates:
  arm; caps (`count>max_contracts`, ticker prefix); `exchange_index is None` -> refuse (`:177-185`);
  `t_minus_s < no_orders_after_s_to_settle`; **entry dedup: one `PURPOSE_ENTRY` per window** (`:194`);
  single-flight per `(window, side, purpose)` (`:198`); token budget 200 @ 10/leg (`:203-206`).
- `_dispatch` (`:221`): 1 leg -> `SINGLE_CREATE_PATH`, 2 legs -> `BATCH_CREATE_PATH`; POST never retried.
- `orders/envelope.py`: `SINGLE_CREATE_PATH = "/trade-api/v2/portfolio/events/orders"`, `BATCH_CREATE_PATH =
  .../batched` (`:45-46`). `build_entry(leg, *, tif=DEFAULT_TIF, stp=DEFAULT_STP)` (`:81`); leg exposes `ticker,
  side('yes'/'no'), action('buy'/'sell'), count, limit_price, client_order_id`, optional `reduce_only`,
  `exchange_index`. **`DEFAULT_TIF = "immediate_or_cancel"` (`:49`) — taker-only.** `wire_price` = 4-dp string.
- `translate.to_v2_order` (`translate.py:36`) maps side+action -> bid/ask, passes `post_only` (`:69-70`) and
  `_PASSTHROUGH_KEYS` (`reduce_only, cancel_order_on_pause, expiration_time, subaccount, order_group_id,
  exchange_index`), defaults TIF to `good_till_canceled` when unset. Maker support exists here but `build_entry`
  never sets `post_only` and hard-codes IOC.
- **No cancel/DELETE path and no amend path anywhere in `service/`.** `test_orders_proxy_compat.py:91` asserts the
  proxy does not treat `.../ORD-1/amend` as a create.
- Response parse: `parse_single_response(body, side)` / `parse_batch_response(body)` -> `OrderResponse(client_order_id,
  order_id, fill_count, remaining_count, average_fill_price, average_fee_paid, ts_ms, raw_reported_price, error,
  no_fill)`. `normalize_fill_to_side` (`:171`) is the units choke point (NO fills report in YES-space).
- Fill detection today: synchronous, from the create response only. No fills-channel consumer, no `/portfolio/fills`
  poller in the live path (`reconciler.py:304` has `poll_fills`; `tick` only diffs positions).
- FLATTEN: `stops.build_flatten_intent(...)` (`stops.py:307`) -> reduce-only SELL IOC at the observed bid, dispatched
  with `stop_authorized=True`. `HarnessExecutor.set_armed` latches: once disarmed, never re-armable in-process
  (`run_window.py:335-341`).
- `ProxyAuth` (`proxy_auth.py`): `ws_connect_params()`, `rest_get(path, params)` — prefixes `/trade-api/v2`, bounded
  retry 1->2->4s on 429/5xx/ConnectionError/Timeout, 4 attempts. GET only.

## 5. `box_runner.py` + `box.py` — the plug-in pattern to copy

- Protocol (`box_runner.py:6-8`): `decide_box(params, state, event: BookUpdate | ClockTick) -> (state, list[Action])`.
  Events/actions in `signal.py`: `BookUpdate(market, top, server_ts)` (`:69`), `ClockTick(server_ts)` (`:78`),
  `LegOrder(ticker, side, count, limit_price)` (`:88`), `Action(kind, source, legs, count, C, ev, t_minus_s, reason)`
  (`:99`).
- Adapter `BoxSignalDriver(params, state, journal, clock, on_action)` (`box_runner.py:111`) — `on_book_update` /
  `on_clock_tick` -> `_event` -> `decide_box` -> journals per kind (`_BOX_ACTION_KIND_TO_JOURNAL`, `:45`) -> `on_action`.
  Throttled `box_eval` heartbeat (`_HEARTBEAT_S=10.0`, `:178`).
- Recorder `BoxWindowRecorder(wake_result, journal, params, state, clock, capture_tops, on_action, on_book_event)`
  (`:210`) subclasses `WindowRecorder` (tap/watchdog/flush/replay inherited).
- WOULD_FIRE vs FIRE decided in the core: `kind = WOULD_FIRE if st.shakedown else FIRE` (`box.py:446`); `run_window`
  sets `shakedown=(not plan.armed)` (`:1508`); `_on_box_action` (`run_window.py:1610`) is a no-op unless
  `kind == FIRE and self.armed and self.executor is not None`.
- Policy: `policy/box_params.json`, loaded by `load_box_policy(path, expected_sha=FROZEN_BOX_POLICY_SHA256)`
  (`box.py:521`) with `canonical_sha256` = sha256 of `json.dumps(sort_keys=True, separators=(",",":"))`. Missing key
  -> KeyError -> fail closed.

## 6. `stops.py` (787 lines)

- `StopConfig` (`:73`): `slippage_alarm_dollars=0.02`, `daily_loss_cap_dollars=Decimal("3.00")`,
  `guard_trips_standdown=5`, ... `DAY_HALTING_STOPS = (S1,S2,S3,S4)` (`:89`).
- Day guard `ops/stops_YYYY-MM-DD.json` (`day_guard_path`, `:439`), shape `{"utc_day", "balance_start_dollars",
  "latched":[{kind,reason,window,ts}]}`. `read_day_guard` (`:444`), `record_latched_stop` (`:498`),
  `latched_stop_kind` (`:488`), temp+`os.replace` writes. Corrupt guard is never self-healed.
- S4: `parse_balance` (`:556`) requires `balance_dollars` and `balance` (cents) agreeing within $0.01;
  `s4_pending_value` (`:618`); `s4_balance_decision(start, now, pending, cap)` (`:647`).
- `StopController.trip(...)` (`:732`) -> `executor.set_armed(False)`. Arming refusal (S5) `arming_check(falsifier_path,
  health, policy_verified, *, strategy, expected_strategy)` (`:371`); `falsifier_is_frozen` requires a line exactly
  `STATUS: FROZEN` (`:338`).

## 7. Ledger / P&L / reconciliation

- `ledger.py`: `FEE_IS_TOTAL = False` (`:53`); `_FLOOR_SOURCES = (SUB_DOLLAR_FLIP, WIDE_BOX)` (`:65`).
- `run_window._build_box_ledger_entry` (`:2452`) appends one JSON line to `ledger/pilot_ledger.jsonl`.
- Settlement upside arrives as a separate backfill row via `_settlement_backfill_sweep` (`run_window.py:1859`) ->
  `_fetch_market_result` (`GET /markets/{ticker}`, exact-ticker match in `_parse_market_result`, `:2621`) ->
  `pilot_ledger.build_backfill_entry` (`:163`).
- `reconciler.py`: `PositionsReconciler.tick(state)` polls `/portfolio/positions`; not used by the box.

## 8. Journals

- `Journal` (`journal.py:38`): in-memory; `append(kind, obj, local_ts) -> idx`; `flush(path)` writes JSONL
  `{"idx","kind","local_ts","obj"}` sorted keys, Decimal->str, temp+`os.replace`.
- Files `journals/<YYYYMMDDThhmmssZ>.jsonl` + one line to `journals/summary.jsonl` (`record_window.flush`, `:162`).
  Rotation `journal_io.rotate_closed_journals` (`:151`): gzip level 6, min age 30 min, bounded 3 files / 60 s.
  `open_journal`/`journal_paths` read `.jsonl` and `.jsonl.gz` transparently.
- `record_range.py` has the memory-light alternative: `StreamJournal` write-through with the same record shape.

## 9. `wake.py` — discovery

- Time helpers: `parse_utc`, `close_epoch(iso)`, `expected_step(close_iso)` ($100; $250 at 21:00Z; $500 Friday 21:00Z).
  Hour alignment in `record_window.next_top_of_hour_iso` (`:52`).
- `WakeContext.sweep(close_iso)` (`:493`): paged `GET /markets?series_ticker=...&min_close_ts=T&max_close_ts=T&limit=1000`
  for `KXBTC15M` and `KXBTCD` -> `_group_ladders` by `(event_ticker, open_time)` -> `_select_smallest_window` ->
  `_leg_is_live` (`DEAD_STATUSES`) -> `ladder_check` -> `live_hourly_ladders` -> balance -> `WakeResult`.
  `WakeResult.exchange_index_by_ticker` (`:210`) is the shard map every dispatched leg is stamped from.
- `record_range.discover_range_markets` (`:216`) is the range-native analogue (`RangeDiscovery` with `floor`/`cap`
  per bucket; `RANGE_SERIES = "KXBTC"` at `:69`).

## 10. Arming ceremony + falsifier format

- `ops/BOX_ARMING.md` §A: clean tree on main; `python -m pytest -q` green; `/health` orders_enabled + caps;
  strategy lever; >= 2 shakedown windows with would-fires / full evals and no errors; Brad flips `STATUS: DRAFT` ->
  `STATUS: FROZEN` and appends the verbatim go under Registration; Brad sets mode armed. §B stand-down; §C day-guard.
- `ceremony/box_falsifier.md` structure to mirror: title -> `STATUS:` line -> freeze provenance (verbatim quote) ->
  What is being judged -> Policy (name + json path + sha) -> Alarms -> Stops (`[pin]` markers) -> One-legged fill
  handling -> Retirement R1-R4 (thresholds + power note) -> Promotion -> Pre-arming checklist -> Registration
  (append-only) -> Pre-registered shadow observations (SO-n) -> Amendment. Every tunable carries `[pin]` and is
  mirrored in code.

## 11. Reusable vs hard-coded

**Reusable as-is:** `ProxyAuth`, `KalshiWebSocketClient` (`channels=` seam), `BookMirror`/`TopOfBook`,
`Journal`/`ThreadSafeJournal`/`journal_io`, `StreamJournal`, `WindowRecorder` base + `watchdog_action` +
`run_recording`, `signal.BookUpdate/ClockTick/LegOrder/Action`, the decide protocol, driver + recorder + `on_action`
triple, `stops.py` day guard / S4 / arming, `pilot_ledger` append/backfill, `orders/translate.to_v2_order`.

**Hard-coded to box/corridor:** `run_window.VALID_STRATEGIES` and six `if strategy == "box"` branches; series
whitelist `run_window.TICKER_PREFIXES = ("KXBTC15M","KXBTCD")` (`:146`), `ExecutorConfig.ticker_prefixes`
(`executor.py:68`), `_ours()` (`:566`) — range buckets are series `KXBTC`, not covered, and the proxy's cap whitelist
is Brad-side `.env`; `WakeContext` mandates both legs; IOC hard-coded, no `post_only`, no cancel, no amend, no order
status read; entry dedup / single-flight / token budget; `PILOT_MAX_CONTRACTS_CEILING = 2` (`run_window.py:169`);
box-specific post-fill machinery (`:1640-1813`); `ledger._FLOOR_SOURCES`; 15M anchor coupling.
