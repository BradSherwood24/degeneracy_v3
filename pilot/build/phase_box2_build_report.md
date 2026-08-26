# Phase box-2 build report — wiring the wide box into the live pilot service

Branch: `box/phase2-wire` (merge of `box/phase1-core` + `repairs/instruments`). Wires the pure
wide-box core (`service/box.py`, phase box-1) into the `run_window` service so the pilot can trade it:
single pair, 1 contract per leg, taker IOC, straight-to-live-after-review (Brad's ruling 2026-08-26).
House law observed: `python` only; no network in tests; no sealed reads; no key/PEM/.env access; the
corridor's behavior is untouched when `strategy == corridor` and the whole prior suite stays green.

---

## What changed

### New files
- **`pilot/service/box_runner.py`** — the box's live/replay wiring (mirrors `shakedown.SignalDriver`
  + `ShakedownRecorder` for the corridor):
  - `BoxSignalDriver` — the strategy adapter over the generalized `decide(params, state, event) ->
    (state, actions)` protocol, bound to `decide_box`. Journals the decision actions
    (`box_fire`/`box_would_fire`/`box_stand_down`, each FIRE/WOULD_FIRE carrying the full
    BoxSelection) and the throttled `box_eval` fluff; drives an optional `on_action` hook.
  - `BoxWindowRecorder(WindowRecorder)` — folds every subscribed book frame into BookMirror (tap +
    watchdog + flush + golden capture inherited) and drives `decide_box` on EVERY subscribed ticker
    (the full ladder + the 15M leg), not just two paired legs. Same F5 no-server-ts fail-closed as
    `LiveWindowRecorder`.
- **`pilot/ops/strategy.txt`** — the strategy lever, shipped `corridor` (Brad flips to `box` at
  Phase 4 alongside mode.txt).
- **`pilot/tests/test_box_wiring.py`** — 29 wiring tests.

### `run_window.py` — the corridor/box split (specific)
- **Strategy lever (spec 1).** `resolve_strategy(cli, strategy.txt) -> (strategy, valid)`, read
  fresh each wake; `--strategy` overrides, `--strategy-file` points elsewhere. Unknown/missing value
  fails CLOSED: run the **corridor** decision core, **DRY (never armed)**. `prepare()` journals
  `strategy_selected` (always) and `strategy_invalid` (on the fail-closed path), and adds the
  strategy-invalid reason to the arming `extra` list so an invalid strategy can never arm.
- **Policy load (a).** Branches: `box` loads its sha-pinned roster via `load_box_policy()` (roster
  `box-v1`); corridor loads the pilot roster. Only the selected strategy's policy is loaded per
  window. `policy_loaded` journaled with the roster + sha in both cases.
- **Phase A (e).** The σ̂ trailing-anchor prefetch is corridor-only (the box has no quintile), so the
  box skips it entirely.
- **`execute()` split.** `execute()` now branches: `_run_corridor_window(plan)` (the former body,
  byte-for-byte behavior) vs `_run_box_window(plan)`. Both set `self._recorder` and return the
  recorder; `execute`'s `finally` flushes via `recorder if recorder is not None else self._recorder`
  so the crash-flush law (recorder built, then window driver raises) is preserved across the extract.
- **Box PHASE B.** `_resolve_box_anchor(plan)` polls the 15M leg at open for its floor_strike and
  returns `(anchor_A Decimal, 15M ticker)` — **no σ̂/quintile**. `_run_box_window` then builds
  `BoxState.new(close_time, T, anchor_A, m15_ticker, ladder={hourly_ticker: floor_strike Decimal},
  shakedown=(mode!=armed))` from the **selected** hourly generation's wake market records (F1 pooled
  logic does NOT apply to the box). The ladder-map deviation is journaled as an A2 alarm only, never a
  box stand-down.
- **Subscription.** `_box_subscription_tickers` = the FULL selected hourly ladder + the 15M
  market(s) + the anchor ticker, **regardless of `--max-hourly-strikes`**.
- **Event routing.** The `BoxWindowRecorder` drives `decide_box` on BookUpdates for every subscribed
  ticker (decide_box internally ignores non-subscribed markets). ClockTicks are handled by the
  adapter (used in tests/replay). NB (confession): no synthetic live ClockTicks are injected — live
  firing is BookUpdate-driven, same as the corridor, to keep the server-ts freshness law intact.

### Ledger slot mapping (documented)
The ledger's `high_ticker`/`low_ticker` are two storage slots with no inherent ordering. For the box:
**`high` = the hourly leg, `low` = the 15M leg**. `new_ledger(window, WIDE_BOX, high_ticker=hourly,
low_ticker=m15)`; `record_intent` captures `high_side = hourly_side`, `low_side = m15_side` from the
entry legs. There is no strike ordering between a $-strike hourly market and the 15M anchor market, so
the slot names are labels only.

### FIRE → orders (spec 3)
`_on_box_action` (armed only; dry/shakedown emit WouldFire) builds an entry Intent, source
`box.WIDE_BOX`, one leg per box leg, **count 1 each** (never `× pairs`; the box is a single pair),
limits already margin-adjusted by `decide_box`. Batched IOC via the existing executor path.

### Post-fill policy (spec 4) — REPLACES the corridor rebalance protocol entirely
`_box_post_entry`: **no retries, no rebalance, no I1 ceiling.**
- **Both legs filled → hold to settlement.** A matched box pair has a guaranteed **$1 floor** (15M
  deep-ITM side + hourly strike behind it: exactly one leg pays outside the pin region, both inside →
  $2 pinned). `ledger.realized_at_close()` now floor-books the box exactly like a sub-$1 matched pair
  (`_FLOOR_SOURCES = (SUB_DOLLAR_FLIP, WIDE_BOX)`): booked `1 − cost`, marked `realized_unsettled`
  with BOTH legs pending; the settlement backfill adds **+$1 iff pinned** (see below).
- **Exactly one leg filled → immediately flatten** that leg reduce-only at the current best bid
  (Brad's standing ruling: **at any price**). One attempt; on an IOC miss, retry at the NEW bid up to
  3 more times, then hold + journal `giveup_hold`. No bid → `A_FLATTEN_NO_BID` alarm + hold. The
  flatten is the ONLY order after the entry. A successfully-flattened leg is booked as a round-trip
  (net → 0, out of `unsettled_legs`) via `_fold_flatten_response` — the same folding path the
  corridor's stop-authorized flattens now use (repairs/instruments F1).
- **S1_box** (`stops.check_s1_box`): trips if any fill price > its own limit (units tripwire) OR the
  pair booked cost (both legs, fees in) > `pair_cost_max` = **1.99** from the roster. On a trip it
  freezes the DAY (`stops.trip(S1)` with no `ledger_state`, so the corridor position_policy does NOT
  flatten the held floor pair). The corridor `check_s1` (its `realized_min < 0` would trip on every
  normal box pair) is scoped to sub-$1 flip and returns None for the box — the box uses `check_s1_box`
  only.
- **A5** one-legged-entry alarm: over the rolling last 20 box fires (this window included), if
  one-legged/fires > 0.10 → alarm (notify + journal, keep running). Counter
  `pilot_ledger.box_one_legged_rate`; each box ledger row carries `box_one_legged` + `fired_source`.
- S4 (balance) and the day-guard latching from `repairs/instruments` apply unchanged (the box builds
  the same armed stack, minus the corridor positions-reconciler — the box has no rebalance/S3).

### Settlement backfill automation (spec 5)
`_settlement_backfill_sweep()` runs at each wake's reconcile-first: for every prior ledger row still
`realized_unsettled`, it fetches each held ticker's settled `result` via the proxy `/markets`
(read-only, `_fetch_market_result` → `_parse_market_result`) and runs the existing backfill,
journaling `settlement_backfill`. Idempotent (skips already-backfilled windows) and fail-closed (an
absent/unsettled result waits for a later wake; never breaks startup). Mocked in tests via
`market_result_getter`. `pilot_ledger.build_backfill_entry` now **nets the `floor_booked`** amount
recorded on the window row, so the box's $1 floor is not double-counted: payoff $2 pinned → +$1, $1
not-pinned → +$0. Corridor/sub-$1 rows carry no `floor_booked` (== 0) so their backfill is unchanged.

### Roster (`box_params.json`) re-pinned
Added `pair_cost_max: "1.99"` (the S1_box booked-cost ceiling — spec pins the value, so it lives in
the sha-pinned roster and is enforced, not trusted from a flag). Canonical sha re-pinned
`a91bc569…` → `480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3`
(`box.FROZEN_BOX_POLICY_SHA256` + `BoxParams.pair_cost_max` + loader updated; the box roster is not
yet ceremonially frozen — a box falsifier comes later). The corridor roster is untouched.

### Phase-1 review carry-overs (spec 6)
- (a) `test_box_golden.py`: present-but-short is now a **FAILURE, not a skip** — when historical-data
  is present (the module skipif already gates absence) but < 200 hours are found, both golden tests
  assert `>= 200`.
- (b) Documented (no code change): a **stale nearest strike suppresses the fire** and fails closed —
  `decide_box`'s freshness gate rejects a chosen leg whose top is older than `freshness_max_leg_age_s`
  (1.0s) or `suspect`, so a strike that stopped updating cannot be entered; the window simply does not
  fire that instant (and re-selects on the next fresh tick), which is the safe direction.

---

## New journal record kinds
`strategy_selected`, `strategy_invalid`, `box_phase_b_start`, `box_phase_b_anchor`,
`box_phase_b_timeout`, `box_phase_b_no_strikes`, `box_state`, `box_eval`, `box_would_fire`,
`box_fire`, `box_stand_down`, `box_post_fill`, `box_flatten`, `box_s1`, `box_a5`,
`settlement_backfill`, `ws_frame_no_server_ts` (box path). Reused: `policy_loaded` (box roster),
`connect_gate`, `alarm` (A5, A_FLATTEN_NO_BID via the StopController).

## Test counts
`cd pilot; python -m pytest -q` → **all green** (475 before the `repairs/instruments` merge; see the
merge note for the post-merge count). New this phase: 29 in `test_box_wiring.py`, plus the golden
skip→fail carry-over. Corridor suite unchanged and green.

## CONFESSIONS
1. **Box roster sha re-pinned.** I added `pair_cost_max` to `box_params.json` (the spec pins C_max =
   1.99 "from the roster"), which changes the canonical sha. The box roster is not ceremonially
   frozen yet, so this is legitimate pre-ceremony build work — but it is a real re-pin: the phase
   box-1 report's `a91bc569…` is now stale. The unit test asserts the new field + sha (via the
   `FROZEN_BOX_POLICY_SHA256` constant it imports).
2. **`WIDE_BOX` is duplicated as a local constant in `ledger.py`.** `ledger` is imported very early by
   `executor`/`stops`; to avoid any import-cycle fragility I kept a local `WIDE_BOX = "wide-box"`
   there rather than importing from `service.box`. A test asserts `box.WIDE_BOX == ledger.WIDE_BOX`.
3. **No synthetic live ClockTicks.** The adapter accepts ClockTick (tests/replay use it), but the
   live box path drives `decide_box` on BookUpdates only — feeding machine-clock ticks would let a
   fire evaluate on machine time under clock skew, the opposite of the fail-closed freshness law. Same
   choice the corridor makes.
4. **Golden determinism is exercised at the driver level.** `test_box_decision_is_golden_deterministic`
   replays a fixed BookUpdate/ClockTick event list through two fresh `BoxSignalDriver`s and asserts
   identical action sequences + final state (the meaningful guarantee: `decide_box` is pure). It does
   not reconstruct a raw WS-frame journal through `replay.py` (that harness's determinism is already
   pinned for the corridor); the box decision inherits BookMirror's determinism unchanged.
5. **The box uses one recorder class for all modes.** Unlike the corridor (ShakedownRecorder for
   shakedown, LiveWindowRecorder for dry/armed), `BoxWindowRecorder` serves all three: shakedown/dry
   seed `shakedown=True` (WouldFire only), armed seeds `shakedown=False` (FIRE → `on_action`). The
   `on_box_action` guard (`kind != FIRE or not self.armed`) makes dry a no-op regardless.
6. **Market-result parsing is a best-effort shape guess** (`_parse_market_result` reads
   `{"market": {...}}` or `{"markets": [...]}`, field `result` ∈ {yes,no}), like the balance-payload
   guess in repairs/instruments. An unrecognized/empty result fails closed (waits for a later wake).
