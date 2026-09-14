# V3.2 — co-settling KXBTC15M recording (branch `v32/record-15m`)

**Goal.** Make `service.run_v32` ALSO record the co-settling `KXBTC15M` (15-minute) market so V3.2 is
the single data recorder and the disabled v1.1 pilot leaves no 15M data gap. RECORDING ONLY — V3.2
never trades the 15M leg and the decision core never classifies a 15M ticker.

## What changed

- **Discovery** (`run_v32.py`): new `M15Discovery` dataclass + `discover_co_settling_15m(proxy,
  close_iso, now)`. Reuses the existing status-agnostic, close-ts-narrowed paged GET
  `/markets?series_ticker=KXBTC15M&min_close_ts=T&max_close_ts=T&limit=1000` (`_fetch_series_markets`)
  through the injected REST edge; liveness per market via `wake._leg_is_live([m], now)` (close in the
  future AND not a dead status). Takes ALL live 15M markets (usually one). Absence is **not** a
  stand-down.
- **Subscription** (`main`): the bucket connection now subscribes `sorted(set(bucket_map) |
  set(m15_disc.tickers))` on the same public channels (`orderbook_delta` + `trade`; no ticker/private
  channel for 15M). The strike connection is unchanged. `exch_map` is NOT extended with 15M (never
  routed to the executor).
- **Recording, not deciding** (`V32Recorder`): constructor takes `m15_tickers` and holds an
  `m15_frames` counter. `on_snapshot`/`on_delta` fold the 15M frame into its `BookMirror` then return
  before `_drive_book`; `on_trade` returns before `driver.on_trade`. So a 15M frame is tapped into the
  same `journals_v32/<close>.jsonl` stream (via the WS `record` hook, which runs before dispatch) and
  counted, but produces no `BookUpdate`/`Trade` core event — no recompute churn and no
  foreign/unknown spam. (A 15M ticker starts `KXBTC15M-`, so `parse_strike_ticker` returns None and it
  is not in `bucket_map` — `classify_ticker` -> None even if it were driven.)
- **Journal**: at discovery `main` appends one `m15_recording` (tickers) record, or `m15_missing`
  (`{series, close_time}`) + an INFO log when none co-settle, and continues. `window_meta` gains
  `m15_series` + `m15_tickers`.
- **Ledger/summary** (`ledger.py`, `run_v32._finalize`): the per-window row and the summary line carry
  `m15_tickers` (list) and `m15_frames` (count). Defaults `[]` / `0` keep the row schema stable for
  stand-down / legacy rows.
- **Report** (`report.py`): a per-window `m15` column shows the recorded 15M frame count.
- **Watchdog/lag semantics**: the bucket connection's `current_lag_seconds()` is a per-CONNECTION
  gauge (last frame server-ts vs now). Adding a very liquid 15M market to the same socket cannot make
  the connection look staler — it only ever supplies additional fresh frames — so the lag/watchdog
  meaning is unchanged.

## Docs

- `ops/V32_DRY_RUN.md`: replaced the stale "`armed` degrades to dry" Phase-2 limitation with the
  Phase-3 truth (armed runs the LiveExecutor only when `ceremony/v32_falsifier.md` is `STATUS: FROZEN`
  and the S5/reconcile/day-latch/S4 gates pass; else degrades to dry with a journaled reason). Added
  the one-line note that the window also records the co-settling KXBTC15M (recording only).
- `PLAN_V32.md` Status: one line for this change.

## Tests

New `tests/test_run_v32_m15.py` (10 tests): discovery over a fake `/markets` payload (one live 15M +
one dead; none; all-dead -> empty); the bucket-connection subscription set contains the 15M ticker; a
15M orderbook+trade frame is journaled + counted (`m15_frames`) but drives no core event and no
stand-down; strike/bucket quotes still place a rest alongside a 15M frame; the ledger row carries
`m15_tickers`/`m15_frames` (and defaults empty); the report renders the `m15` count; a window with no
15M runs normally.

Full suite: **799 passed** (baseline 789 + 10). Fakes only — no proxy, no socket, no sealed/holdout
date read.
