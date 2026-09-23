# V3.3 FALSIFIER -- rolling K-rung ladder spot-bucket pump-fader (maker rest ladder, taker completion)

STATUS: DRAFT -- NOT FROZEN

This file is a DRAFT. It becomes FROZEN only when Brad ALONE, on his verbatim go, changes the line above
to exactly `STATUS: FROZEN` and appends the go under Registration. An agent NEVER flips it. Until then
V3.3 cannot arm: the S5 arming gate (`service.v33.stops.decide_v33_arming` ->
`service.v33.stops.v33_arming_check` -> `service.stops.falsifier_is_frozen`, wired in `service.run_v33` at
`--falsifier` default `pilot/ceremony/v33_falsifier.md`) requires this exact path to carry a line that is
exactly `STATUS: FROZEN`. From the freeze line down, once frozen, nothing in this document may change
except appended verdicts / clarifications in Registration.

Every pre-registered threshold below is marked `[pin]` and is MIRRORED as a named constant in code
(`service.v33.falsifier_pins` for the verdict/kill/promotion gates; `service.v33.stops` for the day-stop
pins). A unit test (`tests/test_v33_falsifier_pins.py`) fails the suite if this document and those
constants disagree -- the doc does not merely describe the thresholds, the code ENFORCES them. And while
this file is a DRAFT, that same test asserts the STATUS line is NOT `STATUS: FROZEN` and that S5 refuses
to arm on it (an agent can never arm V3.3 by editing code).

## Provenance

- Build: pilot V3.3, phases H / L1 / L2 / L3 on Opus 4.8, disclosed branches `feat/v33-*`, reviewed and
  merged to `main` by Brad (Phase H #82-#84, L1 #85, L2 #87; L3 this branch). Live V3.2 untouched
  throughout (V3.3 lives in `service/v33/`, its own params / ledger / mode file).
- Policy: roster `DegeneracyV3_3`, `pilot/policy/v33_params.json`, canonical sha
  `c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3` (pinned in code as
  `service.v33.params.FROZEN_V33_PARAMS_SHA256`; the loader self-verifies and refuses drift).
- Evidence: `pilot/build/mc/v33_ladder_ideal.*` (167 armed windows 2026-09-14..21, ideal fills): the
  ladder 5..15c at 1 lot per rung = 1806c over 188 contracts / 28 pump windows (9.6c/contract); every one
  of the 12 live V3.2 sweeps was a FULL sweep (median depth 22c). The seal (2026-08-02..18) and the
  holdout (2026-08-20..29) were NEVER read.

## What is being judged

The ROLLING K-rung ladder pump-fader (roster `DegeneracyV3_3`): rest K = 11 [pin] one-lot bucket-NO bids
on consecutive cents, anchored at the top rung `n_top = n(E_min, W)` (E_min = 0.05), rung k at
`n_top - k` cents (realised margin `E_min + k` = 5..15c). On each rung fill, take both pin wings as a
taker sized to the coalesced fill (Q2, `wing_coalesce_ms` 150). As W moves, a CONVERGENCE rolls the
ladder one order per 1c (amend-first, cancel->create fallback), up to `max_amends_in_flight` = 3 at a
time; the other rungs keep queue. Full spec: `pilot/PLAN_V33.md` sec 1.

Judged quantity: the REALISED lock PER CONTRACT PER RUNG on LIVE fills vs the in-process ideal ladder.
Each rung fill's SOLVED reference is `lock_value(price, W_at_fill)` (the price/W economic value at the
fill), NOT the integer `E_rung` label -- the label is the rung's nominal position; the true solved margin
is W-dependent (at the golden W the deepest rung's label is 15c while its realised lock is +16.03c). Per
`n` counts CONTRACTS: a full K-rung sweep contributes K toward `n`.

## Policy (roster `DegeneracyV3_3`, `pilot/policy/v33_params.json`, sha-pinned; loader refuses drift)

sha `c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3`. Values: E_min 0.05, rungs 11
(K), lots_per_rung 1, tol 0.01, deb_ms 5000 (start-of-convergence debounce), max_amends_in_flight 3,
quote_start_s 900 (T-15), quote_end_s 300 (T-5), wing_margin 0.02, lock_floor -0.10,
no_orders_after_s_to_settle 1, freshness_max_age_s 1.0, bucket_freshness_max_age_s 30.0 [pin],
wing_coalesce_ms 150, refill_in_window false (Q3 no re-place inside the window), max_sets_per_hour 11,
n_min 0.05, replace_rate_alarm_per_min 120, bucket_width 100 ($250/$500 hours stand down),
max_contracts_per_order_hint 11, write_tokens_per_s 100, write_bucket_size 100, write_reserve_tokens 30,
order_poll_batched true, deep_obs_rungs 10 (SO-3, observation only), shadow_Es {0.08, 0.10, 0.12}.

## Proposed pre-registered thresholds (the verdict)

Judged on LIVE fills. The verdict is decided only once there are `n >= 30` [pin] realised rung-fills (n
counts contracts; a full sweep contributes K); before that the scoreboard prints `n<30 pending`. At
`n >= 30`:

- ALIVE iff ALL of:
  - ladder mean true lock (realised, per contract) >= +6.0c [pin], AND
  - per-rung shortfall (solved E - realised lock) <= 3.0c [pin] at every rung with >= 3 [pin] fills, AND
  - % positive >= 80% [pin], AND
  - capture ratio at the 10c margin >= 0.50 [pin] (= 50%; same definition as V3.2 Registration 3 --
    live completed 10c-rung sets / ideal-shadow E=0.10 fills inside the quoting window, over armed+bucket
    windows). FAIL-CLOSED: at `n >= 30` an UNMEASURABLE capture (no valid ideal-shadow E=0.10 availability
    to measure execution against, ratio None) is a MISS, not a pass -- exactly as V3.2's verdict logic
    treats a None ratio. (Below `n >= 30` the gate reads `n-too-small`.) AND
  - one-legged <= 2 [pin] contracts, AND
  - roll integrity: >= 90% [pin] of rolls move exactly one order (journal-counted).
- KILL iff ANY of those thresholds is missed at `n >= 30`. No re-spec on the same evaluation window
  (the registered-specs rule): a miss is a kill, not a re-parameterisation.

The report computes this verdict (`ALIVE-so-far` / `KILL` / `n<30 pending`) directly from the `[pin]`
constants in `service.v33.falsifier_pins` -- see the FALSIFIER GATE TABLE + LADDER SCOREBOARD blocks in
`python -m service.v33.report`.

Per Brad's sizing philosophy (2026-09-18): every completed ladder set pays $2/contract at settlement, so
the lock is STRUCTURAL; `n` answers slippage / edge-case questions, not a coin flip. The mean-lock bar
catches a broken premise (the deep rungs' edge does not survive real fills); the per-rung shortfall is
the sharp instrument for latency eating a rung's edge.

## Alarms (notify + protect the hour; the day keeps running)

- A_REPLACE: rolls (amends) in a trailing 60 s above `replace_rate_alarm_per_min` (120, per LADDER) ->
  stand the HOUR down. (`service.v33.stops.A_REPLACE`.)
- A_STALE: a strike (wing) book older than `freshness_max_age_s` (1.0 s), OR the spot-bucket book older
  than `bucket_freshness_max_age_s` (30.0 s) [pin] -> the core cancels + does not re-place until fresh.
- A_REJECT x3: three consecutive rest rejections (a definite business 4xx) latch a `stand_down` for the
  hour (`executor.CONSECUTIVE_REJECT_STANDDOWN` = 3). A rate-limit (HTTP 429) is NOT a business rejection
  (it executed nothing): it is retried once and does NOT count toward this stand-down (L2 review R2-N1).

## Stops (halt the UTC day; latched in `ops/v33_stops_YYYY-MM-DD.json` -- SEPARATE from V3.2 + the box)

- S4: daily loss >= $3.00 [pin] (`V33_S4_DAY_LOSS_CAP_DOLLARS`), on the ACCOUNT BALANCE via the
  pending-settlement BAND, count + bucket aware, identical law to V3.2. Q5: at K=11 a worst-case
  one-legged K-rung sweep is ~$1.93 < $3.00, so the V3.2 cap stands; revisit at K >= 16.
- S5 (arming refusal): this file's STATUS line not exactly `STATUS: FROZEN`, the params sha unverified,
  proxy `/health` `orders_enabled` not true or caps absent, caps not covering BOTH `KXBTC-` and
  `KXBTCD-`, `max_contracts_per_order` outside `[lots_per_rung, K*lots_per_rung]` = `[1, 11]` [pin] (so a
  proxy cap of 2 OR of 11 both arm; wings chunk to <= cap), fewer than 500 [pin]
  (`V33_MIN_ORDER_BUDGET_AT_ARM`) creates left in today's budget, a latched stop / corrupt guard for
  today, or an inherited un-settled KXBTC* position (reconcile-first) -> never fires an order (degrade to
  dry). BELT (R2-N2): an armed window whose `/health` contract cap is unreadable degrades to dry rather
  than sizing wings against a guess.
- S1_LEGGED: a rung set left one-legged below the lock floor at the cutoff stands the HOUR down; the DAY
  latches after 2 [pin] such occurrences (`V33_S1_LEGGED_LATCH_THRESHOLD` = 2). S1 is per contract.
- reconcile-first: any KXBTC* position with non-zero size at :40 refuses to arm.

## One-legged fill handling

Identical geometry to V3.2 (bucket-NO + YES@Sd + NO@Su; any two legs are a $1 floor). On each rung fill
the initial coalesced both-wings take is UNCONDITIONAL; a missed wing leg is retried every tick to the
T-1 s cutoff subject to the retry lock floor `lock_floor` = [pin] -0.10; else hold the two-leg floor to
settlement and flag `one_legged`.

## Kill (early / immediate)

- mean lock < +2.0c [pin] (`V33_KILL_MEAN_LOCK_CENTS`) already at `n >= 15` [pin]
  (`V33_KILL_MIN_N`) -> KILL without waiting for n=30 (the ladder's premise is broken on real fills).
- one-legged > 2 [pin] contracts -> KILL (the taker completion is systematically failing at K rungs).

## Promotion

Nothing here promotes automatically. What would JUSTIFY 2 lots per rung, OR rungs deeper than 15c live
(Brad's dated word, informed by SO-3's measured deep-end absorption): ALIVE at `n >= 30` [pin]
(`V33_PROMOTION_MIN_N`) completed rung-fills. The SO-3 deep observation ladder (16..25c, observation only)
measures the deep end's absorption before anyone sizes into it (sec 8, PLAN_V33). The proxy cap
`MAX_CONTRACTS_PER_ORDER` (2 or 11) stands regardless.

## Dry / armed side-by-side protocol (Q4 refined, Brad 2026-09-23)

V3.2 KEEPS RUNNING ARMED through the whole V3.3 build; V3.3 runs DRY alongside it (`ops/v33_mode.txt` =
dry, missing -> dry). Both rosters write a ledger row every hour and the report prints them SIDE BY SIDE.
The lever is the MODE FILES, not the params (both shas are pinned). At the flip, in a :02-:33 window and
by Brad's hand, `v33_mode.txt` -> armed and `v32_mode.txt` -> dry: only ONE roster is armed per bucket,
and V3.2 keeps running/reporting with no orders so the comparison continues in the other direction. Dry
proves the MECHANICS (rung prices, one-order rolls, coalesced wings, stops, accounting), NOT venue
acceptance or queue position -- the MUST CONFIRM list below stands for the first armed windows.

## FIRST ARMED WINDOW MUST CONFIRM

1. K orders ACCEPTED: the first placement lays up to K = 11 `place_rest` records the proxy returns 201
   for (at cap 2 each is one lot; the K-aware pre-place invariant never flags the healthy ladder).
2. ONE-ORDER ROLLS: a 1c W move issues exactly ONE `amend_rest` (or one cancel+create fallback), the
   other K-1 rungs keep their `order_id`; the ledger's single-order-roll ratio stays 1.0 (>= 90% [pin]).
3. COALESCED / CHUNKED WINGS SIZED TO FILLS: a full sweep prints K rungs within `wing_coalesce_ms` and
   takes ONE wing pair sized to the total, chunked into `ceil(count/cap)` IOC orders at the proxy cap; no
   rung is left naked; the count taken equals the count filled.
4. HAND-RECONCILE THE FIRST FULL SWEEP against `GET /portfolio/fills`: the money-math per-rung
   `realized_lock` agrees with the venue fills (the falsifier reads CORE state -- `rest_fills` /
   `wing_batch_sets` -- not the money-math, but confirm the two agree by hand for the first full sweep).
5. PACER NEVER 429'd: no `rate_limited` records under normal latency; a `write_paced` wait is fine (the
   Basic-tier bucket working). The priority cancel/wing burst always finds `write_reserve_tokens` (30)
   headroom.
6. PER-RUNG BUCKET CORRECT ACROSS A CHANGE: a rest-and-fill spanning a mid-window bucket change lands the
   held bucket-NO leg on the RIGHT market (`RungFill.bucket_ticker`), and the batched order poll covers
   BOTH buckets (R2-N4) so a prior-bucket rung's fill is never missed.

## Registration (append-only; the freeze line, Brad's verbatim go, and every verdict go here)

(empty -- awaiting Brad's freeze. Brad ALONE flips `STATUS: DRAFT -- NOT FROZEN` to exactly
`STATUS: FROZEN` on his verbatim go and appends it here with the roster sha
`c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3`. An agent never flips it.)

## Pre-registered shadow observations (observational; change NOTHING above this line)

### SO-1 -- edge-ladder shadow (E = 0.08 and E = 0.12), inherited from V3.2
The in-process shadow re-solves and scores E in {0.08, 0.10, 0.12} every tick inside the quoting window
T-15..T-5 (the V3.2 shadow, forked byte-identically). Recorded per report alongside the live ladder.

### SO-3 -- deep-end observation ladder (16..25c), registered with this draft
`deep_obs_rungs` = 10 observation-only rungs BELOW the live ladder (margins 16..25c). For every
spot-bucket YES-taker print inside the quoting window, record per deep rung whether the tape REACHED it
(a print at/through the rung's NO price while the rung is placeable, solved n >= n_min), the IDEAL lock
there (`lock_value(deep_price, W_at_reach)`), and the ABSORPTION (lots the tape printed at/through the
rung). It NEVER places an order and NEVER holds a position -- it MEASURES the deep end (whose absorption
the ideal study could only INFER) before anyone sizes into it, informing the Promotion clause. Surfaced
in the report's "DEEP END (SO-3, observation only)" block.
