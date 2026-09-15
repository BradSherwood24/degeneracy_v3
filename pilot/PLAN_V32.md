# PILOT V3.2 PLAN — continuous-requote spot-bucket pump-fader (maker rest, taker completion)

Drafted 2026-09-13 on Brad's go: "Build a new pilot, call it V3.2 ... Same building techniques, Opus 4.8 agents.
Lets give it a live order switch as well. Well do a couple dry runs just to check functionality."
This is the build plan. The binding ceremony documents (`ceremony/v32_falsifier.md`, `ops/V32_ARMING.md`) are
Phase 4 deliverables and gate arming. Nothing here overrides them. v1.1 (`run_window`, the box) is NOT touched.

## What V3.2 is

A live measurement of one maker strategy on the KXBTC hourly RANGE buckets, judged against a shadow that runs in the
same process on the same feed. The strategy:

* Every hour, in the quoting window **T-15..T-5** (:45:00 to :55:00 UTC of the hour that closes at :00), identify the
  **spot bucket** B = [Sd, Sd+100) = the live $100 range bucket with the highest YES mid.
* Price the **pin wings** as a taker from the live hourly-strike books:
  `W = ask_yes(Sd) + fee(ask_yes(Sd)) + ask_no(Su) + fee(ask_no(Su))`, Su = Sd+100, exact taker fee
  `0.07*p*(1-p)` per contract rounded up to $0.0001 (maker fee on crypto = 0, verified 2026-09-05).
  `ask_no(Su) = 1 - bid_yes(Su)`.
* **Rest one bucket-NO bid** (= offer bucket-YES at 1-n) at the largest whole-cent `n` with
  `n + fee(n) <= 2.00 - E - W`, capped at `ask_no(B) - 0.01` (never cross; the order is `post_only`).
  E is the edge parameter (start E = 0.10). Re-solve `n` on EVERY wing or bucket book tick; **replace** the resting
  order per the requote policy below (the sim shows the whole loss at low E was arm-minute staleness, not sub-second
  flutter — `mailbox/messages.md` 2026-09-13, `claudes-corner/the_flutter_that_wasnt_2026_09_13.md`).
* **On fill of the resting order, immediately TAKE both wings**: buy YES@Sd and buy NO@Su, IOC limit at the live ask
  plus a margin, count = filled count, as ONE batch create. Position = a $2 pin (YES@Sd + NO@Su + bucket-NO) that
  pays $2.00 at EVERY settlement. **Lock = 2.00 - (n + fee(n)) - W_paid**. Hold to settlement. One completed set
  per hour (then stop quoting for that hour).
* Any TWO of the three legs are a $1 floor (bucket-NO + YES@Sd pays >= $1 everywhere; bucket-NO + NO@Su likewise), so
  a missed wing leg is bounded, not naked: keep retrying the missing leg every tick until T-1 s subject to a lock
  floor (below), else hold to settlement.

Sim basis (forward 2026-08-30..09-04, 139 h, ms strike books + 1-s range tape, exact fees; scratchpad
`journals/pf_ms_requote.py`, `pf_ms_depth.py`, `pf_ms_requote2.py`; summarized in the 2026-09-13 mailbox entries):
E=10 continuous requote: 21 fills, 3.6/day, mean lock +9.3c, p10 +5.8c, min -1.4c, 95% positive, all 6 days positive.
Wing depth at completion (+1.5 s): thinner wing median 409 lots, p10 26, min 19 -> 1 or 2 contracts never bound.
Through-prints land 1 tick above our offer in most fills. Train (06-22..08-01) has no ms strike books; holdout
08-20..29 untouched.

## Decisions already made (Brad, 2026-09-13)

* **Versioning**: this is pilot **V3.2** (the box was V1.1). Package `service/v32/`, process `service.run_v32`,
  task `DegeneracyV32`, levers `ops/v32_mode.txt` + `policy/v32_params.json`, ceremony `ceremony/v32_falsifier.md`.
* **Separate process, one job** (like `record_range`): v1.1's `run_window` is taker-only, one-entry-per-window,
  15M-anchored and 2.7k lines; the maker lifecycle (resting order, cancel/replace, fills channel) is orthogonal.
  V3.2 subscribes to BOTH the hourly strike ladder (KXBTCD) and the range buckets (KXBTC) and journals every raw
  frame, so it supersedes the v1.1 dry-mode recorder and the range recorder as the data source once it runs hourly.
* **Live order switch**: `ops/v32_mode.txt` = `shakedown` | `dry` | `armed` (unknown/absent -> shakedown, fail
  closed). `armed` requires `ceremony/v32_falsifier.md` to carry `STATUS: FROZEN` (S5) — Brad alone freezes it and
  flips the mode. The proxy's `ALLOW_ORDERS` is the outer switch (Brad's `.env`).
* **Dry runs first** ("a couple dry runs just to check functionality"): `dry` runs the full loop, including the
  shadow fill rule, with a frozen executor that logs every would-be create/cancel; `armed` sends them.
* **Builders/reviewers on Opus 4.8**, disclosed branches `v32/phase<N>-<name>`, code PRs merged only after an Opus 4.8
  review pass; `python` only.
* **Size**: 1 contract (proxy cap 2 per order stands). E = 0.10 to start.

## Reuse (do not reinvent) — see the 2026-09-13 architecture survey in `pilot/build/v32_survey.md`

`service.proxy_auth.ProxyAuth` (REST GET with bounded retry, `/ws-auth`), `service.ws_client.KalshiWebSocketClient`
(+ `WsCallbacks.on_fill`, `channels=` seam, `include_private`), `service.book.BookMirror`/`TopOfBook`,
`service.journal` + `journal_io` (gzip rotation) or `record_range.StreamJournal` (memory-light raw frames),
`service.record_window.watchdog_action` / `run_recording` / `next_top_of_hour_iso`, `service.wake` helpers
(`close_epoch`, `_group_ladders`, `_leg_is_live`, `MARKETS_PATH`, `exchange_index_by_ticker` pattern),
`service.record_range.discover_range_markets`, `service.orders.translate.to_v2_order` (passes `post_only`,
`expiration_time`, `exchange_index`), `service.orders.envelope` (paths, `wire_price`, response parsers),
`service.stops` day-guard primitives (`read_day_guard`, `ensure_balance_start`, `s4_balance_decision`,
`falsifier_is_frozen`, `_caps_agree` pattern), `service.pilot_ledger` append/backfill pattern, `service._simlaw`
(`KALSHI_FEE_EXACT`). The `decide(params, state, event) -> (state, actions)` protocol from `box.py`/`box_runner.py`
(driver + recorder + `on_action` triple; WOULD_FIRE vs FIRE decided in the pure core by `state.shakedown`).

Hard-coded things V3.2 must NOT reuse blindly: `run_window.TICKER_PREFIXES` / `ExecutorConfig.ticker_prefixes`
(range series is `KXBTC-...`, not covered), `envelope.build_entry` (IOC hard-coded), `Executor` entry-dedup /
single-flight / token budget (hostile to requoting), `WakeResult` (mandates a 15M leg).

## Proxy prerequisites (degeneracy-proxy; Brad's levers unless noted)

1. `ORDER_TICKER_PREFIXES` must cover the range series: add `KXBTC` (Brad edits `.env`, restarts proxy). Dry mode
   needs nothing. V3.2's S5 check reads `/health caps.ticker_prefixes` and refuses to arm otherwise.
2. `DAILY_ORDER_BUDGET` (creates/day, default 100): every requote is cancel (DELETE, uncapped, unbudgeted) + create
   (budgeted). Required budget = replaces/day from the requote policy x 2 margin + completions. Number in the
   requote-policy section; Brad sets it in `.env`. Amend (`.../orders/{id}/amend`) is refused by the proxy today
   (uncappable body) — NOT used by V3.2; cancel+create only.
3. Proxy caps unchanged: `MAX_CONTRACTS_PER_ORDER=2`.

## Requote policy

**Replace = AMEND-FIRST, cancel -> confirm -> create as the fallback; never two live rests**
(R-OVERLAP ruling 2026-09-13; amend-first Brad 2026-09-15, verbatim: "Use the cancel and recreate flow as a
backup if our post to ammend the order fails. Go ahead and build that"): a same-bucket requote emits
`AMEND_REST` — Kalshi Amend Order V2 (`POST /portfolio/events/orders/{id}/amend?exchange_index=<idx>`, the
`exchange_index` also in the body), same `order_id`, a price change forfeits queue position EXACTLY as
cancel+create did. The core holds all requotes while `amend_in_flight` and updates the resting price + coid
IN PLACE on `OrderAmended`; a fill during the amend (`fill_count > 0`, a TAKER fill at `average_fill_price`)
is routed into the wings exactly like a fill-before-cancel. On ANY non-2xx/timeout the executor journals
`amend_failed` and FALLS BACK to the strictly-sequential cancel -> confirm -> create (the sharded DELETE with
PR #50 backoff/status-truth, then a PLACE_REST through the pre-PLACE venue invariant). Bucket changes (a
different ticker — an amend cannot change the ticker) and the quote-end cancel are unchanged. This removes the
~0.7 s/replace (60-90 s/busy hour) the old sequential cancel left with NOTHING resting — the cause of the
2026-09-15 14:00Z missed fill. An amend IS a replace for `replace_count`, the A_REPLACE alarm, and the ledger
`replaces` counter; it reaches the new price one RTT sooner and never leaves a no-rest gap, so its lock is
`>=` the cancel+create lock. No fillable old rest is ever left beside a new one in flight.

From `pf_ms_requote2.py` (lagging-quote model: replace only when |dn| >= TOL and >= DEB ms since the last replace;
new quote live +200 ms; fill = print > 1 - n_resting; completion +1.5 s; one per hour; forward 139 h):

| E  | TOL | DEB ms | fills | /day | mean lock | p10   | min   | % pos | c/day | replaces/h | replaces/day |
|----|-----|--------|-------|------|-----------|-------|-------|-------|-------|------------|--------------|
| 10 | 1c  | 0      | 22    | 3.80 | +8.9c     | +5.5  | -5.3  | 95%   | +33.9 | 597        | 14,319       |
| 10 | 1c  | 2000   | 20    | 3.45 | +9.5c     | +5.8  | +2.3  | 100%  | +32.8 | 216        | 5,192        |
| 10 | 2c  | 2000   | 20    | 3.45 | +9.6c     | +7.8  | +3.4  | 100%  | +33.2 | 118        | 2,844        |
| 10 | 2c  | 5000   | 21    | 3.63 | +9.2c     | +5.5  | +3.4  | 100%  | +33.5 | 77         | 1,860        |
| 10 | 3c  | 5000   | 23    | 3.97 | +8.0c     | +4.3  | +2.2  | 100%  | +31.9 | 51         | 1,229        |
| 8  | 2c  | 5000   | 24    | 4.14 | +7.6c     | +3.5  | +3.3  | 100%  | +31.7 | 76         | 1,829        |
| 12 | 2c  | 5000   | 15    | 2.59 | +11.1c    | +7.6  | +7.3  | 100%  | +28.8 | 78         | 1,877        |

Ideal continuous requote (no lag, no gate) at E=10 was 21 fills, +9.3c, 95% positive. The P&L is insensitive to the
gate: a 5-second debounce with a 2-cent tolerance loses nothing (the drift that mattered was the 60-second
arm-minute staleness). Replaces/h is counted over the 10-minute quoting window (77/h = one every ~8 s).

**Chosen defaults: E = 0.10, TOL = 0.02, DEB = 5000 ms** -> ~80 replaces per hour, ~1,900 creates per day.
Proxy `DAILY_ORDER_BUDGET` must therefore be raised to **4,000** (2x margin) before arming; dry mode consumes none.
Exchange write rate ~0.15/s, far under Kalshi limits. Full grid in scratchpad `journals/pf_ms_requote2.out`.

## Phases

### Phase 1 — pure core (`service/v32/core.py`, `params.py`, tests) — branch `v32/phase1-core`
* `V32Params` frozen dataclass loaded from `policy/v32_params.json` with a pinned canonical sha (same scheme as
  `box.load_box_policy`): `E`, `tol`, `deb_ms`, `quote_start_s` (900), `quote_end_s` (300), `contracts` (1),
  `wing_margin` (0.02), `lock_floor` (-0.10: never pay for a wing leg that would push the set's lock below this;
  keep retrying at better prices), `no_orders_after_s_to_settle` (1), `freshness_max_age_s` (1.0),
  `max_sets_per_hour` (1), `n_min` (0.05), `replace_rate_alarm_per_min`, `bucket_width` (100; 21Z $250/$500 hours
  -> stand down unless params say otherwise).
* `V32State` (immutable-update): per-market tops + server ts, spot bucket, wing prices, desired n, resting order
  (client id, exchange id, price, count, placed ts, live flag), pending replace, fills, completion legs, sets done,
  shakedown flag, shadow state (shadow resting price, shadow fills by the print rule, shadow completion at ask).
* `decide_v32(params, state, event) -> (state, list[Action])`, events `BookUpdate(market, top, server_ts)`,
  `Trade(market, price, side, count, server_ts)`, `Fill(order_id, count, price, server_ts)`, `OrderAck/Cancelled`,
  `ClockTick(server_ts)`. Actions: `PLACE_REST(ticker, n, count, expiration)`, `CANCEL_REST(order_id)`,
  `TAKE_WINGS(legs)`, `RETRY_WING(leg)`, `STAND_DOWN(reason)`, `WOULD_*` twins when `state.shakedown`.
  Spot-bucket selection, W, n solve, cap, requote gate, fill handling, wing completion pricing, cutoffs, one-set
  rule, freshness (stale wing or bucket book > freshness -> CANCEL_REST, do not re-place until fresh), all pure.
* Shadow: on every bucket `Trade` with side yes and price > 1 - n_shadow (the continuously re-solved n, no
  lag) record a shadow fill; shadow completion = wing asks at that tick; shadow lock. This IS the sim's fill rule
  running live, so dry mode yields the sim's statistic on live data.
* Golden tests: synthetic ladders + one real hour replayed from `historical-data/tob/` (top-of-book) + range tape
  must reproduce `pf_ms_requote2.py`'s fill for that hour (E=10) to the cent. Property tests: never crosses
  (n < ask_no(B)), never places after `quote_end_s`, never takes a wing after T-1 s, lock accounting exact-fee.

### Phase 2 — process spine, dry mode, task — branch `v32/phase2-spine`
* `service/run_v32.py`: at :40 read `ops/v32_mode.txt` + params (sha) -> discover the hourly ladder (KXBTCD, all
  live generations, `exchange_index` map) and the range buckets (KXBTC) for the next close -> connect gate T-15 min
  -5 s -> two WS connections (strikes: orderbook_delta + trade; buckets: orderbook_delta + trade; `fill` +
  `market_positions` on the bucket connection when armed) -> stream raw frames to `journals_v32/<close>.jsonl`
  (StreamJournal, memory-light) plus decision records -> deadline close + 10 s -> gzip, summary line, ledger row.
  Watchdog: reuse `watchdog_action`; on lag > freshness the core cancels the rest (Phase 3) and stands down until
  fresh.
* `ops/register_v32_task.ps1` / `unregister_v32_task.ps1` (copy of `register_task.ps1`: UTC :40 hourly, single
  instance, log `logs_v32/scheduler.out`). Registration is Brad's lever.
* Dry mode complete: frozen executor logs `would_place`/`would_cancel`/`would_take` with prices; shadow fills;
  per-window summary; `v32_report.py` (fills, locks, replaces/hour, shadow vs live once armed).

### Phase 3 — maker execution + money math — branch `v32/phase3-exec`
* `service/v32/executor.py`: `place_rest` (POST single create: side no, action buy, count, price n, `post_only`
  true, `time_in_force good_till_canceled`, `expiration_time` = EXPIRATION_GRACE_S past the quote end (T-4; crash
  backstop, the quote-end cancel is the primary path) so a crashed process leaves nothing
  resting past the window, `client_order_id`, `exchange_index`), `cancel(order_id)` (DELETE
  `/trade-api/v2/portfolio/orders/{order_id}`; confirm via GET order status), `take_wings` (batch create, 2 legs,
  IOC, limit ask + margin, `exchange_index` per leg), `order_status(order_id)`. Fills from the `fill` WS channel
  AND a 1-s order-status poll while a rest is live (belt and braces). Never retry a POST blindly; idempotency by
  client_order_id. Replace = cancel -> confirm -> create (never two live rests).
* Money math: `ledger/v32_ledger.jsonl` one row per window (mode, params sha, bucket, Sd/Su, rests placed, replaces,
  fills [price, fee, ts], wings [price, fee], lock, one-legged flag, shadow fills/locks, alarms, stops) +
  settlement backfill row (payoff 2.00 for a complete set; 1.00-floor sets by result) reusing the
  `settlement_backfill` pattern (GET `/markets/{ticker}` exact match).
* Stops/alarms (all `[pin]`-tagged in the falsifier): S4 day loss cap $3.00 on balance (reuse), S5 arming check
  (frozen falsifier, `/health` orders_enabled + caps cover `KXBTC` and `KXBTCD`, params sha), reconcile-first (no
  inherited un-settled positions), A_REPLACE (replaces/min above alarm -> cancel rest, stand down this hour),
  A_STALE (data age > freshness -> cancel rest), S1_LEGGED (a set left below the lock floor at T-1 s -> latch the
  day after 2 occurrences), crash safety (expiration on every rest; startup cancels any open order of ours).

### Phase 4 — ceremony + report — branch `v32/phase4-ceremony`
* `ceremony/v32_falsifier.md` (STATUS: DRAFT; Brad freezes): cell E=10 requote; judged on live fills at n >= 30:
  proposed thresholds mean lock >= +4c, % positive >= 80%, fill rate >= 2.0/day, shadow-vs-live lock gap <= 3c
  mean; 1 contract; retirement R1-R4 with power note; SO-n shadow observations (E=8 and E=12 shadows run alongside).
* `ops/V32_ARMING.md` mirroring `BOX_ARMING.md` (clean main, pytest green, /health, >= 2 dry windows with
  would_place + shadow records and no discovery errors, Brad freezes, Brad flips mode, proxy prefixes/budget).
* Build reports in `pilot/build/v32_phase<N>_build_report.md`, reviews `..._review.md`.

## Open questions for Brad (asked 2026-09-13)
See the mailbox entry for 2026-09-13 (V3.2 build start). Defaults if unanswered are marked in each phase.

## Status 2026-09-13

Phases 1-4 built on Opus 4.8, disclosed branches, reviewed.

* **Phase 1** (pure core) -- MERGED (PR #31).
* **Phase 2** (process spine, dry mode, task) -- MERGED (PR #32).
* **Phase 3** (maker execution + money math + stops) -- MERGED (PR #33); review
  `build/v32_phase3_review.md` (765 -> 767 tests, three wire bugs fixed, R-OVERLAP sequential replace).
* **Phase 4** (ceremony + report) -- BUILT on `v32/phase4-ceremony`, pending Brad's review + merge.
  Deliverables: `ceremony/v32_falsifier.md` (STATUS: DRAFT -- Brad alone freezes), `ops/V32_ARMING.md`,
  the S4 floor-netting RULING (banded `v32_pending_credit`), the falsifier scoreboard in
  `service/v32/report.py`, and `service/v32/falsifier_pins.py` (the `[pin]` constants, doc/code
  agreement test). Suite 787 passed.
* **15M recording** (`v32/record-15m`) -- `run_v32` also subscribes + tapes the co-settling `KXBTC15M`
  market on the bucket connection (RECORDING ONLY; the decision core never classifies a 15M ticker),
  so V3.2 is the single tape recorder and the disabled v1.1 pilot leaves no 15M data gap. Absence of a
  15M market journals `m15_missing` and continues (not a stand-down); ledger/report carry
  `m15_tickers` + `m15_frames`. See `build/v32_m15_build_report.md`.

Params sha (roster `DegeneracyV3_2`): `c6715fc7fd8339e0cc8877bd39bb78b04239eda9c490bde71a53333a48bdfb92`
(E=0.10, tol=0.02, deb_ms=5000).

**What remains before a live arm** (all Brad's levers, none an agent may do):
1. Proxy `.env`: `ORDER_TICKER_PREFIXES` includes `KXBTC`; `DAILY_ORDER_BUDGET` >= 4000; restart proxy;
   `/health` shows `orders_enabled: true` + caps + `orders_remaining_today` >= 200.
2. >= 2 dry windows with `would_place_rest` + shadow records, no discovery errors.
3. Clean live tree on `main` (Phase 4 merged); `pytest -q` green.
4. Brad freezes `ceremony/v32_falsifier.md` (STATUS: FROZEN) on his verbatim go + Registration line.
5. Brad writes `armed` to `ops/v32_mode.txt`; Brad registers/confirms the `DegeneracyV3_2` task.

The falsifier's verdict gate (n >= 30 completed sets): mean lock >= +4.0c, % positive >= 80%, fill rate
>= 2.0 sets/day, execution gap <= 3.0c, one-legged <= 2 of 30 -- any miss = KILL, no re-spec.
