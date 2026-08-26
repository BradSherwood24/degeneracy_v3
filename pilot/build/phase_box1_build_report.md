# Phase box-1 build report — the wide box, PURE strategy core

Branch: `box/phase1-core` (off `main`). Additive files only; `signal.py`, `run_window.py`,
`ledger.py`, `stops.py`, `policy_params.json` untouched. No live wiring, no network, no clock
reads in the core.

## What was built

- **`pilot/service/box.py`** — the pure decision core for Brad's late-window deep-ITM box:
  - `BoxSelection` (frozen): the chosen pair at one instant — hourly/15M tickers, sides, observed
    asks/bids/mid, IOC limits, strike K, anchor A, fee-inclusive cost `C`, informational fee-free
    `C_mid`, `implied_pin`.
  - `NoBox(reason)` (frozen, falsy): why nothing qualified at an instant, for the journal.
  - `select_box(anchor_A, m15_ticker, m15_top, ladder, params) -> BoxSelection | NoBox` — PURE.
    Side logic (BTC below A -> 15M NO + hourly YES on K<A; BTC above A -> 15M YES + hourly NO on
    K>A), the `quotes()` spread rule on both legs, nearest-`target_mid` selection with a
    strike-nearer-A tie-break, the two ask filters, and C from observed asks + imported census fee.
  - `BoxState` (frozen) + `BoxState.new(...)` — per-hour window state (anchor, m15 ticker,
    subscribed ladder strikes, per-leg tops/ts, shakedown, entered/latched, fired_selection,
    standdown_emitted). Transitions via `dataclasses.replace`.
  - `decide_box(params, state, event) -> (state, actions)` — reuses `signal.BookUpdate /
    ClockTick / Action / LegOrder`. Folds each relevant book update, enforces the entry window
    (T-600..T-60), the one-pair-per-hour latch, the settle-cutoff STAND_DOWN (once), the
    freshness/not-suspect gate over the two CHOSEN legs (fail closed), and shakedown -> WOULD_FIRE.
  - `BoxParams` + `load_box_policy(path, expected_sha)` — sha-pinned, fail-closed loader mirroring
    `policy.load_policy`. `FROZEN_BOX_POLICY_SHA256` pinned; a plain `load_box_policy()`
    self-verifies the shipped JSON and refuses drift.
- **`pilot/policy/box_params.json`** — roster `box-v1`: `target_mid` 0.95, `hourly_ask_min` 0.90,
  `hourly_ask_max` 0.99, `min15_ask` 0.85, `max_spread` 0.10, `limit_margin` 0.03,
  `entry_start_s` 600, `entry_end_s` 60, `freshness_max_leg_age_s` 1.0,
  `no_orders_after_s_to_settle` 1, `contracts` 1, plus a description.
  Canonical sha256 = `a91bc569b8f38df31a5fe050cc07c2fd6f2642988c24f51ea870d11d80eed9f0`.
- **`pilot/tests/test_box.py`** — 33 unit tests.
- **`pilot/tests/test_box_golden.py`** — 2 golden-parity tests.

### Fee handling

C = `hourly_ask + fee(hourly_ask) + m15_ask + fee(m15_ask)`, where `fee` is
`service._simlaw.fee` (the audited census fee, imported — never retyped). `test_box.py`
recomputes C against `_simlaw.fee` directly.

### `limit_margin` (Brad's ruling, folded in)

Each `LegOrder.limit_price = min(observed_ask + limit_margin, 0.99)`. The FILTERS
(hourly ask in [0.90, 0.99], 15M ask >= 0.85) and C are computed from the OBSERVED asks, not the
limits. `BoxSelection` keeps both the observed asks (`hourly_ask`, `m15_ask` — the "decided ask"
the daily report compares fills against) and the limits (`hourly_limit`, `m15_limit`).

## Test counts

- New: **35** tests (33 in `test_box.py`, 2 in `test_box_golden.py`), all pass.
- Full pilot suite: **408 passed, 1 failed**. The one failure is **pre-existing and unrelated**:
  `tests/test_stops.py::test_falsifier_currently_draft_refuses_arming` asserts the falsifier is
  still DRAFT, but `pilot/ceremony/falsifier.md` is now FROZEN — the test is stale w.r.t. the
  frozen falsifier and has nothing to do with this phase (I was instructed not to touch
  `stops.py`). Baseline before this branch was 373 passed / 1 failed (same failure); this branch
  adds 35 passing tests (373 -> 408) and introduces no new failures. The 4 pre-existing `sim/`
  test failures noted in the brief live outside the pilot suite and were not run here.

## Golden-parity result

`test_box_golden.py` builds TopOfBook-equivalent quotes from the 1-minute candle close bid/ask at
T-600, T-540, ..., T-60 for every qualifying hourly close in the NON-SEALED candle record, and
compares `select_box` + the first-qualifying-minute scan against an INLINE Decimal reimplementation
of the scratch `wide_box.run` (TARGET=0.95, MIN15=0.85, start_min=10) as the oracle.

- **hours compared: 1289** (>= 200 required)
- **fires: 1024 | no-fire: 265**
- **mismatches: 0** — `select_box` + scan reproduces the oracle's `(minute, strike, side, C)` on
  every hour, and fires nowhere the oracle doesn't.
- **float-vs-Decimal selection divergences: 0** — a float replica of the scratch's raw arithmetic
  chose the same `(minute, strike, side)` on all 1289 hours; moving the domain to Decimal changed
  no fire.
- Second golden test: for all **1024** fired hours, `decide_box` fed the winning minute's full
  snapshot (ladder ticks first, the 15M tick last so no partial-ladder early fire) into a fresh
  `BoxState` fires exactly the scan's selection, with IOC limits = observed ask + margin (capped
  0.99). This exercises the window bound, freshness gate, latch, and action/limit construction on
  real data.

### SEAL discipline

The days **2026-08-02 .. 2026-08-18** (the 17-day sealed holdout, per `SEAL.md`) are physically
present as files in `historical-data/`, but the golden loader refuses them: `_SEALED_DAYS` is
excluded from day enumeration and `_load_candles` raises if ever handed a sealed day. The test
asserts no sealed-day file was opened. Days used: 2026-06-11 .. 2026-08-01 (train) and the
post-seal virgin days 2026-08-21 / 2026-08-22. No acknowledge flag is set anywhere.

## CONFESSIONS — where the live-book mapping differs from the candle backtest

1. **Candle close quotes vs. live ticks.** The backtest reads each leg's TopOfBook from the
   1-minute candle's `yes_ask.close_dollars` / `yes_bid.close_dollars` — the quote at the minute
   boundary only. Live, `decide_box` sees the continuous WS book and can fire at any instant. The
   golden test therefore validates the SELECTION LOGIC on a minute grid, not the intra-minute
   timing a live book would experience.

2. **Minute grid vs. continuous scan.** The scan/oracle evaluate exactly 10 instants per hour
   (T-600..T-60, 60s apart) and fire the first qualifying one. Live, `decide_box` evaluates on
   every event; the first qualifying MOMENT (not minute) fires. The chosen strike can change
   between instants before the fire (intended; unit-tested), and live it can change between ticks.

3. **Book assembly order / cross-minute state (decide_box only).** `select_box` is a pure
   snapshot function, so the primary golden comparison is confound-free. But `decide_box` carries
   book state across events: within a minute the book is assembled tick-by-tick, and a strike that
   has a candle at one minute but not a later one leaves a STALE top in state. Because the
   freshness gate rejects a chosen leg older than 1.0s, `decide_box` fed a naive continuous
   per-minute replay can fire a minute later than — or on a different strike than — the full-minute
   snapshot scan when ladder strikes appear/disappear across minutes. The second golden test
   sidesteps this by feeding each fired hour's WINNING minute as an isolated full snapshot; the
   general cross-minute continuous-replay behavior is the live semantics, not a bug.

4. **Fee domain / no clamp.** `box.py` calls the census `fee` directly on the observed Decimal ask.
   The scratch script wrapped fee in a float helper that clamped price to [0.001, 0.999] and
   rounded to 4dp. Within the filter ranges (hourly 0.90..0.99, 15M >= 0.85) the clamp never binds;
   the only boundary where they could differ is an ask of exactly 1.0 (fee 0 either way here). The
   golden oracle uses the census fee directly (matching the module), so C parity is exact.

5. **Numeric domain (float -> Decimal).** The scratch computed selection in float; `box.py` and
   the golden oracle use Decimal (house law). Measured divergence over 1289 hours: 0. Documented
   as a domain change, not a logic change.

6. **`C` fee-inclusive vs. the scratch's recorded C.** The scratch recorded `C` as the raw
   ask-sum WITHOUT fees (fees were applied only in its taker-EV column). `box.py`'s `C` is the real
   fee-inclusive limit cost; the fee-free mid is surfaced separately as `C_mid` (with
   `implied_pin = C_mid - 1`), mirroring the scratch's informational `imp`. The golden oracle
   computes the fee-inclusive `C` to match the module.

7. **`suspect` / freshness are live-only concepts.** Candle-derived tops are never `suspect` and
   are always "fresh" at their minute. `select_box` deliberately ignores `suspect`/age (mirrors the
   candle oracle); `decide_box` applies the freshness/not-suspect gate to the two CHOSEN legs
   (fail closed) — the live mapping of the same discipline `signal.py` uses.
