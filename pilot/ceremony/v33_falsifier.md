# V3.3 FALSIFIER -- rolling K-rung ladder spot-bucket pump-fader (maker rest ladder, taker completion)

STATUS: FROZEN

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
  `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def` (pinned in code as
  `service.v33.params.FROZEN_V33_PARAMS_SHA256`; the loader self-verifies and refuses drift).
  - 2026-10-03 RE-PIN (stale-wing liveness bounds, after gate D / PR #120): ADDED `strike_feed_dead_s` 4.0 and
    `wing_book_max_age_s` 30.0 explicitly (the measured defaults, values unchanged; everything else
    byte-identical) so the loader enforces them. New canonical sha
    `f403708257fe7b3a91583882aa98e0f107289c851247e5d3c841bca5e61da1b1`; prior sha `295590ce...` kept as
    `PREVIOUS_V33_PARAMS_SHA256_L4`. See the Registration entry of the same date.
  - 2026-09-29 RE-PIN (L4 ladder shift 5..15c -> 8..18c, Brad's rung-allocation ruling): changed ONLY
    `E_min` 0.05 -> 0.08 (everything else byte-identical); the ladder now rests its 11 rungs at margins
    8..18c. See the "L4 amendment (2026-09-29)" section below for the reason. Prior sha (print-through)
    `2e60980762ea6531b707c1c0bc93d69577fd3257295238e63f122d53afdd995e` kept in code as
    `PREVIOUS_V33_PARAMS_SHA256_PRINT_THROUGH`. V3.3 STAYS DRY -- params-only re-pin, not a mode flip.
  - 2026-09-26 RE-PIN (print-through wings, Brad's idea): added the print-through levers
    (`print_through` false, `print_through_ticks` 1, `print_through_slack_c` 0, `print_through_stall_ms`
    1500, `print_through_min_lock_c` 0, `print_through_policy` complete_else_unwind). `print_through` ships
    FALSE so the shipped ladder is byte-identical to the pre-feature one; flipping it to true (a params
    change + this sha re-pin, Brad's hand) arms the early-hedge trigger. Prior sha (FLAP-R2)
    `20188bbe76b592198f2f3aa2f1b8ff8857b12cc6b5ad9f5d3f5d75f76030cc78` kept in code as
    `PREVIOUS_V33_PARAMS_SHA256_FLAP_R2`. DRAFT falsifier -- frozen only on Brad's verbatim go.
- Evidence (original build, PRIOR 5..15c ladder): `pilot/build/mc/v33_ladder_ideal.*` (167 armed windows
  2026-09-14..21, ideal fills): the 5..15c ladder at 1 lot per rung = 1806c over 188 contracts / 28 pump
  windows (9.6c/contract); every one of the 12 live V3.2 sweeps was a FULL sweep (median depth 22c). These
  are the pre-shift (5..15c) numbers, kept as history (see the L4 amendment); they are NOT re-labelled to
  the 8..18c ladder. The seal (2026-08-02..18) and the holdout (2026-08-20..29) were NEVER read.

## What is being judged

The ROLLING K-rung ladder pump-fader (roster `DegeneracyV3_3`): rest K = 11 [pin] one-lot bucket-NO bids
on consecutive cents, anchored at the top rung `n_top = n(E_min, W)` (E_min = 0.08), rung k at
`n_top - k` cents (realised margin `E_min + k` = 8..18c). On each rung fill, take both pin wings as a
taker sized to the coalesced fill (Q2, `wing_coalesce_ms` 150). As W moves, a CONVERGENCE rolls the
ladder one order per 1c (amend-first, cancel->create fallback), up to `max_amends_in_flight` = 3 at a
time; the other rungs keep queue. Full spec: `pilot/PLAN_V33.md` sec 1.

Judged quantity: the REALISED lock PER CONTRACT PER RUNG on LIVE fills vs the in-process ideal ladder.
Each rung fill's SOLVED reference is `lock_value(price, W_at_fill)` (the price/W economic value at the
fill), NOT the integer `E_rung` label -- the label is the rung's nominal position; the true solved margin
is W-dependent (in the prior 5..15c ideal study, at the golden W the deepest rung's label was 15c while
its realised lock was +16.03c -- the same label < realised-lock relationship carries to the 8..18c
ladder; the concrete +16.03c is the 5..15c-era number and is not re-labelled to the new ladder). Per
`n` counts CONTRACTS: a full K-rung sweep contributes K toward `n`.

## Policy (roster `DegeneracyV3_3`, `pilot/policy/v33_params.json`, sha-pinned; loader refuses drift)

sha `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def`. Values: E_min 0.08, rungs 11
(K), lots_per_rung 1, tol 0.01, deb_ms 5000 (start-of-convergence debounce), max_amends_in_flight 3,
quote_start_s 900 (T-15), quote_end_s 300 (T-5), wing_margin 0.02, lock_floor -0.10,
no_orders_after_s_to_settle 1, freshness_max_age_s 1.0, bucket_freshness_max_age_s 30.0 [pin],
wing_coalesce_ms 150, refill_in_window false (Q3 no re-place inside the window), max_sets_per_hour 11,
n_min 0.05, replace_rate_alarm_per_min 120, bucket_width 100 ($250/$500 hours stand down),
max_contracts_per_order_hint 11, write_tokens_per_s 100, write_bucket_size 100, write_reserve_tokens 30,
order_poll_batched true, deep_obs_rungs 10 (SO-3, observation only), shadow_Es {0.08, 0.10, 0.12}.

## L4 amendment (2026-09-29) -- ladder shift 5..15c -> 8..18c (DRAFT edit; gates unchanged)

Brad's rung-allocation ruling. The ONLY change is `E_min` 0.05 -> 0.08 in the policy JSON (everything
else byte-identical, sha re-pinned to `295590ce...`); the ladder still rests 11 rungs at 1 lot each, now
at margins 8..18c (was 5..15c), and `deep_obs_rungs` 10 now observes 19..28c (was 16..25c, observation
only). shadow_Es {0.08, 0.10, 0.12} remain inside the new live range [0.08, 0.18].

Reason: a 2026-09-29 study of 161 dry V3.3 rung fills measured return per lot-window by PLACED margin --
14-16c rungs +0.77c (86% positive), 8-10c +0.21c, 5-7c +0.11c -- while wing slippage placement->fill is
~-5c median at EVERY depth, so the shallow 5-7c rungs were coin flips whose thin edge the slippage ate.
Shifting the whole ladder 3c deeper drops those lowest-edge rungs and keeps the depth and allotment, to
scale the edge rather than the capital.

History NOT mixed: the L1 5..15c dry sample (22 entry windows, 161 rung fills, 2026-09-23..09-29) and the
5..15c ideal MC study (167 windows, `mc/v33_ladder_ideal.*`) are KEPT as history and are NOT combined with
the 8..18c sample for the `n >= 30` verdict / kill / promotion gate counting; the gate counters count only
8..18c fills from here. The gates themselves (mean lock +6.0c, per-rung shortfall, % positive, capture
ratio, one-legged, roll integrity, kill and promotion pins) are UNCHANGED. V3.3 stays DRY.
_(Dated L4 record: the `n >= 30` verdict n and the +6.0c mean-lock bar named in this paragraph are
SUPERSEDED by L6, 2026-09-30 -- `n >= 15` and +4.0c -- and the verdict n again by L7, 2026-10-02 --
`n >= 45`; see the L6 and L7 amendment sections below.)_

## Proposed pre-registered thresholds (the verdict)

Judged on LIVE fills. The verdict is decided only once there are `n >= 45` [pin] realised rung-fills (n
counts contracts; a full sweep contributes K); before that the scoreboard prints `n<45 pending`. At
`n >= 45`:

- ALIVE iff ALL of:
  - ladder mean true lock (realised, per contract) >= +4.0c [pin], AND
  - per-rung shortfall (solved E - realised lock) <= 3.0c [pin] at every rung with >= 3 [pin] fills, AND
  - % positive >= 80% [pin], AND
  - capture ratio at the 10c margin >= 0.50 [pin] (= 50%; same definition as V3.2 Registration 3 --
    live completed 10c-rung sets / ideal-shadow E=0.10 fills inside the quoting window, over armed+bucket
    windows). FAIL-CLOSED: at `n >= 45` an UNMEASURABLE capture (no valid ideal-shadow E=0.10 availability
    to measure execution against, ratio None) is a MISS, not a pass -- exactly as V3.2's verdict logic
    treats a None ratio. (Below `n >= 45` the gate reads `n-too-small`.) AND
  - one-legged <= 2 [pin] contracts, AND
  - roll integrity: >= 90% [pin] of rolls move exactly one order (journal-counted).
- KILL iff ANY of those thresholds is missed at `n >= 45`. No re-spec on the same evaluation window
  (the registered-specs rule): a miss is a kill, not a re-parameterisation.

The report computes this verdict (`ALIVE-so-far` / `KILL` / `n<45 pending`) directly from the `[pin]`
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
  (`V33_KILL_MIN_N`) -> KILL the moment `n >= 15` regardless of the other gates (the ladder's premise is
  broken on real fills).
- one-legged > 2 [pin] contracts -> KILL (the taker completion is systematically failing at K rungs).
- S4 day loss >= $3.00 [pin] latched on any armed day -> KILL (Brad 2026-09-30: the strategy should not
  lose; a day at the cap is not an exception to explain, it is the campaign's stop). The report reads the
  V3.3 day-guard files (`ops/v33_stops_YYYY-MM-DD.json`) over its day range and treats an S4 latch as a
  kill entry "S4 day-loss latched on YYYY-MM-DD" (`V33_KILL_ON_S4_DAY_LOSS` = True [pin]); this fires at
  any n, including n=0.

## Promotion

Nothing here promotes automatically. What would JUSTIFY 2 lots per rung, OR rungs deeper than 18c live
(Brad's dated word, informed by SO-3's measured deep-end absorption): ALIVE at `n >= 45` [pin]
(`V33_PROMOTION_MIN_N`) completed rung-fills. The SO-3 deep observation ladder (19..28c, observation only)
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

## L6 amendment (2026-09-30, pre-freeze) -- Brad's verbatim words

DRAFT edit, pre-freeze. Brad's verbatim words 2026-09-30: "Lets drop that realised lock to +4.0c, and
lets add that daily loss of $3.00 as a early kill. Then lets also not lock any decision to an n over 15
fills."

Three changes (old -> new):

1. Verdict mean true-lock bar: `V33_FALSIFIER_MIN_MEAN_LOCK_CENTS` +6.0c -> +4.0c. The early-kill line
   (mean < +2.0c at `n >= 15`) is UNCHANGED. Rationale: the completed set pays $2/contract at settlement,
   so the lock is STRUCTURAL -- +4.0c sits above the +2.0c early kill and below the 8c shallowest rung's
   label after ~5c placement->fill slippage, so it catches a broken premise without demanding the full
   rung label.
2. S4 day loss >= $3.00 (`V33_S4_DAY_LOSS_CAP_DOLLARS`, UNCHANGED) becomes an EARLY / IMMEDIATE campaign
   KILL, not only a day halt: an S4 latch on ANY armed UTC day of the campaign forces the report's verdict
   to KILL, regardless of n (`V33_KILL_ON_S4_DAY_LOSS` = True [pin]). The S4 day-stop under Stops still
   halts the day exactly as before; this adds the campaign verdict on top. Rationale: a $3.00 day is the
   campaign's stop, not a data point to explain away.
3. Verdict / promotion n: `V33_FALSIFIER_MIN_N` and `V33_PROMOTION_MIN_N` 30 -> 15 (`V33_KILL_MIN_N` stays
   15). Rationale: the lock is structural, so 15 realised contracts answer the execution / slippage
   question the verdict asks; no decision is held for an n above 15 fills.

The per-rung shortfall's "`>= 3` fills per rung" pin, the % positive (80%), the capture ratio (0.50 at the
10c margin), the one-legged tolerance (<= 2), the roll-integrity (>= 90%) and the S4 cap dollars ($3.00),
budget and S1_LEGGED pins are all UNCHANGED. The 5..15c and 8..18c history stays as recorded (L4/L5). V3.3
stays DRY -- this is a falsifier amendment, not a mode flip.

## L7 amendment (2026-10-02, between Test Fire #1 and Test Fire #2) -- Brad's verbatim words

Brad's verbatim words 2026-10-02 (morning after Test Fire #1's frozen KILL): "Id like the same falsifier as
test fire 1, I think. Maybe we should up n from 15 to 30 now. Thoughts?" -> (Claude: agree; the n-gated
mean-lock verdict is the only rule n touches; the one-legged and S4 kills fire at any n) -> "I agree with
30,000 budget and raising above n=30." -> "Lets go with n=45 for test fire 2."

One change (old -> new):

1. Verdict / promotion n: `V33_FALSIFIER_MIN_N` and `V33_PROMOTION_MIN_N` 15 -> 45. The report prints
   `n<45 pending` below it. Rationale (Brad's): at 1 lot per rung and ~2 entries a day, 45 contracts is
   about two armed days; 15 is one active hour (Test Fire #1 reached n=26 in three windows only because spot
   was moving), too small to call a mean on.

UNCHANGED, explicitly: the early kill `mean < +2.0c at n >= 15` (`V33_KILL_MIN_N` stays 15 -- a broken
premise still kills after one active hour; Claude's reading of "the same falsifier", flagged to Brad), the
mean true-lock bar +4.0c, one-legged <= 2 contracts (the rule that returned Test Fire #1's KILL), the S4
$3.00 day-loss campaign kill, the S1_LEGGED day latch at 2 occurrences, and every other [pin]. Params sha
unchanged (`295590ce...`). This is a falsifier amendment on Brad's word before the Test Fire #2 arm, not a
re-spec of a running evaluation: Test Fire #1 was closed under the L6 rules (KILL, one-legged 14 > 2) and
its record stands; Test Fire #2 opens a NEW evaluation window under L7.

## Registration (append-only; the freeze line, Brad's verbatim go, and every verdict go here)

- 2026-09-30 ~03:26Z -- FROZEN on Brad's order. Brad, verbatim (2026-09-30, after reading the full layout of the
  judged quantity, policy, verdict, alarms/stops, kill, promotion, flip protocol and MUST CONFIRM, and after his
  three L6 amendments landed on main as PR #101): "You have my go to freeze dat hoe" (earlier the same night:
  "All looks good, let me know when ready"; the L6 amendment words: "Lets drop that realised lock to +4.0c, and
  lets add that daily loss of $3.00 as a early kill. Then lets also not lock any decision to an n over 15 fills.").
  Thresholds frozen AS AMENDED by L6: verdict at `n >= 15`, mean true lock >= +4.0c, S4 day loss >= $3.00 = campaign
  KILL, promotion at `n >= 15`; every other [pin] as proposed above. Roster `DegeneracyV3_3`, params sha
  `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def` (L4 ladder 8..18c, 11 rungs, 1 lot per rung;
  `rung_lots` ABSENT = uniform). Preconditions at freeze: live tree main ff385df (1384 passed / 1 skipped); smoke
  window 2026-09-30 01:00Z ran DRY at 8..18c (sha match, n_top 0.44 at W 1.4585, 11 rungs of count 1, 184/184
  single-order rolls, only `would_*` records, no fills); proxy restarted 01:16Z (pid 25712) with
  DAILY_ORDER_BUDGET 8000, MAX_CONTRACTS_PER_ORDER 2, ticker prefixes incl. KXBTC, orders_enabled true; crypto-side
  (exchange_index 2) balance $42.60 at 01:35Z (gate 4c >= $35 met; a full 11-rung sweep ties ~$20.57); V3.2 stays
  ARMED at size 2 until the flip; `ops/v33_mode.txt` = dry at the freeze. The STATUS line was edited by Brad's OWN
  typed command (a python one-liner on this branch's copy of the file, 03:25:51Z), not by an agent; the test
  changes and this entry are Claude's mechanical work on the go above. The flip (v33 -> armed, v32 -> dry, in ONE
  :02-:33 window) remains Brad's separate lever and gets its own dated entry here.

- 2026-09-30 12:30:26Z -- THE FLIP (Brad's hand). Brad ran, as typed commands in the session, `Set-Content ops/v33_mode.txt armed`
  and `Set-Content ops/v32_mode.txt dry` (both read back; inside the 12:02-12:33 window). V3.3 ARMED from the 13:00:00Z close
  (launch 12:40Z; the window log reads `mode=armed effective=armed`, i.e. S5 passed on the frozen doc, sha 295590ce..., proxy
  /health orders_enabled with cap 2 / prefixes incl. KXBTC / budget 8000, clean v33 guard, no inherited KXBTC* position).
  V3.2 runs DRY beside it from the same close (its 13:00Z journal reads resolved_mode dry). Preconditions at the flip: live
  tree main af0981b (1385 passed / 1 skipped); crypto-side balance $42.60; V3.2's last armed set 2026-09-29 23:00Z (12
  armed windows since with 0 sets); V3.3's overnight DRY rows 05:00Z..12:00Z: 6 rung fills in 3 windows, dry realised
  +2.3c / -10.6c / -9.5c / -2.0c / -1.0c / 0.0c (margin labels at fill 6c, -7c, -6c, 1c, 2c, 3c: wings had moved before
  the bucket print reached the rung) -- disclosed to Brad before the first armed window. First-window observations (venue
  order list, 12:47-12:53Z): 11 single-lot NO rests accepted (must-confirm 1), always on ONE bucket; spot trended
  84,750 -> 85,350 in ~5 min so the ladder was cancelled and re-placed on each bucket change (11 creates per switch,
  ~450 creates in the window's first 9 quoting minutes; the daily create budget, not money, is the binding limit at this
  rate); no fills by 12:54Z. The window's ledger/journal accounting against the six MUST CONFIRM items gets its own entry.

- 2026-09-30 21:50:55Z -- FIRST LIVE FILL = INCIDENT; MUST CONFIRM item 6 FAILED; V3.3 -> DRY 22:02:07Z (Brad, verbatim:
  "Set it back to dry, and lets address what weve learned"). Window 22:00Z. Chain, from the journal: (1) 21:50:54.9Z a
  stale-wing cancel-all (the self-inflicted loop registered above) tore the ladder down and the resume re-placed 11 rungs
  on KXBTC-26SEP3018-B83650 (bucket_Sd 83600) into a sweep in progress; rungs -312 (0.47) and -313 (0.46) filled at the
  venue within ~0.1 s of placement, FRACTIONALLY (count_fp 0.44 and 1.00 = 1.44 NO). (2) ~1.7 s later the effective spot
  bucket committed to 83700 and the bucket-change cancel-all removed every rung (incl. the two already-filled ones) from
  the ladder state; the cancel-confirm path surfaced the fills at 21:51:04Z. (3) `_book_rung_fill` (core.py ~L1034) could
  no longer find the order in the ladder, `rest_bucket_Sd` was None (awaiting_replace), and it FELL BACK to the CURRENT
  `spot_Sd` = 83700: the RungFill was attributed to B83750 and the wings were solved for Sd/Su 83700/83800 -> YES 2 @0.40
  on T83699.99 (21:51:04Z) and NO 2 @0.96 on T83799.99 (21:52:40Z, after 967 per-tick IOC retries at limit 0.95 that
  also blocked the event loop for ~60 s; the WS fill notice for the rungs arrived 109 s late for that reason). The held
  NO leg was on [83600,83700) while the wings hedged [83700,83800): NOT a $1 floor. Payoff by settle: <83600 +$0.05;
  [83600,83700) -$1.39; [83700,83800) +$2.05; >=83800 +$0.05. BTC settled in [83700,83800): revenue $5.44 on $3.39 cost,
  +$2.05 (balance $42.60 -> $44.60). Luck, not the lock. Ledger shows realized_lock +27.6c on the wrong bucket; the
  falsifier counts these 2 contracts as n=2 with the TRUE realised lock to be restated once the attribution fix lands
  (the report must price the NO leg on B83650). (4) fractional fills: `run_v33.on_fill` parses count as int (0.44 -> the
  rung's full lot) so wings were sized 2 for 1.44 filled. DEFECTS REGISTERED (no [pin]/threshold/sha/STATUS touched):
  D1 fill-to-bucket attribution must come from the ORDER'S OWN bucket (retained through cancel-all until the cancel is
  confirmed) or from the fill's market ticker, never from the current spot bucket; D2 wing strikes keyed to the filled
  rung's bucket; D3 fractional counts parsed exactly (count_fp), wings sized to the filled amount; D4 wing retry cadence
  bounded (not every tick). Fix branch + review to follow; V3.3 stays DRY until merged and re-armed by Brad's hand.
- 2026-09-30 -- MECHANICS CLARIFICATION (async order writer). Brad, verbatim: "I'd vote everything should be async, that we reprices of multiple levels can be sent without delay." MEASURED CAUSE (first armed V3.3 day, 13:00Z journal): `service.run_v33.V33Driver._pump` runs on the ONE asyncio loop and every order create/amend/cancel/wing-IOC/status-poll went out as a SYNCHRONOUS `requests.post/delete` (plus the write pacer's `time.sleep`) INSIDE the loop, so while the executor wrote the websocket reader could not drain -- the books FROZE while the eval clock (`_last_server_ts` + wall elapsed) kept advancing, so after `freshness_max_age_s` (1.0 s) the wings read STALE -> a `stale_or_missing_wing` HOLD -> the 1.5 s hold expired during the SAME burst -> cancel-all -> more blocking. Receipts: 75 of 77 feed gaps >= 1 s had one of OUR OWN writes inside them; a cancel-all of 11 rungs = ~3.4 s of dead loop (11 sequential `_cancel_rest`, each ~0.39 s incl. CANCEL_CONFIRM polls); re-place of 11 = ~1.5 s; ~15-35 full ladder tear-downs per window; the ladder was OUT of the book 51-109 s of ~610 s. THE FIX: every proxy round trip now runs OFF the loop on a worker thread via `loop.run_in_executor` and is `await`-ed; every executor pause (cancel-confirm poll, cancel backoff, invariant recheck, write pacing) becomes `await asyncio.sleep` (which YIELDS the loop). Rolls/creates of several levels dispatch CONCURRENTLY ("reprices of multiple levels can be sent without delay"); wings draw a DEDICATED priority pool so a wing IOC take never waits behind a queued cancel-all, and cancels a pool separate from creates; per-slot FIFO (a cancel and a create for one rung never overlap) via a per-slot lock; a 429 on any POST is a definitive non-execution retried ONCE on the SAME lane (a wing 429 on the wing lane). The event loop never blocks on a round trip. WING RETRY-STORM BELT (2026-09-30 22:00Z finding -- a missing wing leg retried EVERY TICK, 967 IOC creates in ~96 s): the wing lane holds ONE in-flight IOC per missing (batch,side) leg and DROPS a duplicate retry for a leg already in flight (counted `wing_retries_dropped`, never a journal record each), so a retry storm cannot starve the cancel/roll lanes or the reader; the CORE-side re-emission floor (fix/v33-fill-attribution branch, core.py) is the source fix and this is the transport belt -- no `core.py` touched here. This is a MECHANICS CLARIFICATION, NOT a threshold/param/sha change: NO `[pin]`, the `n >= 15` verdict count, the mean-lock `+4.0c`, the S4 `$3.00` day kill, the params sha `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def`, or the STATUS line is touched. STATE STAYS LOOP-CONFINED: only the raw blocking `requests` call runs on a worker; all executor/core mutation happens on the loop thread between awaits, so there are NO locks and NO cross-thread races (the codebase's single-threaded discipline is preserved). MIRRORED IN CODE (armed path), keeping the pure decision logic byte-identical to the SYNCHRONOUS `V33LiveExecutor` (each `*_async` method is the line-for-line async twin of its sync twin; every pure helper -- body builders, parsers, `_finish_cancel`, `_resolve_cancel_from_status`, `_reject_place`, `_filter_phantoms`, `_invariant_verdict`, the K-aware pre-place invariant, the unknown-outcome latch, weighted-average wing aggregation -- is INHERITED unchanged): `service.v33.async_writer.AsyncOrderWriter` (+ `X-DV3-Class` localhost-hop header for Brad's future proxy wing-whitelist throttle), `service.v33.async_executor.V33AsyncExecutor` (+ `cancel_stale_open_orders_async`), `service.v33.executor.WriteTokenBucket.acquire_async` (additive; existing `acquire` byte-identical), `service.run_v33` (`async_writer_enabled`, `build_executor_v33` async branch, `V33Driver._pump_async`/`_dispatch_async`/`_ingest_async`, `feed_gap_max_s`, async batched poll), `service.proxy_writer` (optional per-call `headers` on the default requests path only), `service.v33.ledger` (`writer_stats` row field), `service.v33.report` (OFF-LOOP WRITER section). DORMANT BY DEFAULT: selected only by `--async-writer` / `DV3_V33_ASYNC_WRITER` (armed only; default OFF, a params/env value NOT a params field, so the pinned sha is untouched) -- the synchronous path stays byte-identical and remains the live path until Brad shakes down the async path (dry-adjacent) and flips it, exactly as `--batch-create` and amend-first were built dormant then flipped by Brad. Proven WITHOUT a network (a fake in-process proxy with deterministic latency) in `tests/test_v33_async_writer.py`: loop never blocked during an in-flight / gated write; wing runs concurrently with a blocked 11-cancel cancel-all; per-slot cancel->create serialized; unknown outcome never re-placed over; 429 on a wing retried on the wing lane; PROPERTY test -- never more than K rests at the venue across random-latency seeds; the full ladder places end-to-end off the loop with `feed_gap_max_s` near the WS cadence; DRY unchanged. Design + reviewer checklist in `pilot/build/v33_async_writer_build_report.md`; the proxy-side leaky-bucket throttle Brad's header enables is PROPOSED (not built) in `pilot/ops/proxy_throttle.md`.
- 2026-09-30 (review, `review/v33-fill-attribution`) -- CORRECTION to the 21:50:55Z entry (original left intact
  above; this appends only): the per-coid count_fp pairing is reversed. The journal (fixture
  `tests/fixtures/v33/incident_20260930T220000Z_slice.jsonl`) shows coid -312 @0.47 filled count_fp **1.00** and
  coid -313 @0.46 filled count_fp **0.44** -- i.e. -312=1.00, -313=0.44. The entry's "(count_fp 0.44 and 1.00)"
  has the two the wrong way round; the total (1.44 NO) and every other fact in the entry are unchanged.

- 2026-10-01 17:16:07Z -- RE-ARM (Brad's hand) after the 09-30 incident fixes. Brad ran, as a typed command,
  `Set-Content ops/v33_mode.txt armed` (read back armed; V3.2 stays dry beside it; inside the 17:02-17:33 window).
  Code at re-arm: main 77a75e4 (1453 passed / 1 skipped) = #105 off-loop async order writer, #106 fill attribution
  by the rung's own bucket + fractional counts + 250 ms wing retry floor + netted wings, #107 V3.2 fractional counts.
  Levers pulled on Brad's verbatim "Go for both" (17:14Z): the proxy AMEND CAP (ops/proxy_amend_cap.md) applied to
  degeneracy-proxy/proxy.py (amends capped like a single create + counted against the budget; proxy suite 112
  passed; backups kept) and the proxy restarted (budget 8000, cap 2, prefixes incl. KXBTC); the V3.3 supervisor
  restarted with DV3_V33_ASYNC_WRITER=1 so the first armed windows ARE the async writer's shakedown (dry cannot
  exercise it) at 1 lot per rung, read window by window. Feed on both processes healthy at 16:00Z/17:00Z
  (lag ~0.8 s mean, p99 < 2.2 s, zero alarms) after a 14:00Z-15:00Z backlog caused by local build/test load, now a
  standing rule: no heavy local work in the :40-:00 band while armed. Crypto-side balance $44.60. MUST CONFIRM for
  the first windows: 11 rests 201'd; amend_confirmed 2xx (not amend_failed fallback); one-order rolls; wings sized
  to the filled count on the FILL's bucket; feed_gap_max_s small with writer_stats.async_writer true; no 429s.
  The incident's two contracts (09-30 22:00Z row) remain in the ledger pending Brad's ruling (exclude vs restate)
  before n approaches 15.

- 2026-10-02 03:1xZ -- TEST FIRE #1 (Brad's name for the armed campaign 2026-10-01 17:16:07Z .. 2026-10-02
  02:04:59Z) -- VERDICT BY THE FROZEN RULES: **KILL** (one-legged contracts 14 > 2 [pin]). Recorded as the
  machinery states it (`python -m service.v33.report`, 02:3xZ): realised rung-fills n = 26; mean true lock
  +8.32c (>= +4.0c PASS); single-order-roll ratio 1.00 PASS; one-legged contracts 14.00 (<= 2) FAIL;
  `VERDICT: KILL: one-legged 14 > 2`. Realized money over the campaign +$8.43 (crypto balance 44.60 -> 53.04);
  as-designed (locked) money about +$2.70. The mechanism failed; the account was lucky. Facts, window by window:
  * 09-30 22:00Z (pre-campaign incident, 2 contracts, +$2.05): unchanged, still pending Brad's exclude/restate ruling.
  * 10-01 23:00Z: 2 rungs (B84650 @0.35/0.34), wings 2+2 filled within 300 ms, both sets locked, +$0.11 (3.4c/4.4c).
  * 10-02 00:00Z: 8 rungs (B84750 @0.26..0.33) swept in one tick, wings 8+8 filled, lock +64c. Then 3 rungs on the
    NEXT bucket (B84850 @0.34..0.36); its YES@T84799.99 wing (3 @0.46) NETTED against the first batch's NO on the
    same strike (the first live D5 netting, $3 credited) and its NO@T84899.99 wing (3 @0.96) FILLED at the venue
    (fills endpoint, 23:54:06.199Z). BUG: `run_v33._journal_action` read `.price` on a `LegOrder` while journaling
    the netted pair -> the exception unwound the pump with the NO-wing Fill still queued -> the ledger booked the
    batch ONE-LEGGED (false), S1_LEGGED occurrence #1 on ops/v33_stops_2026-10-02.json, settlement backfill $19
    vs real $22. CORRECTION (ledger row left intact; this appends only): batch 1 was COMPLETE and locked, about
    +20c/contract; the window's realised money +$1.27; the row's `one_legged: true` and 3 of the report's 14
    one-legged contracts are FALSE. Fixed in PR #110 (merged 03:07Z, 58e2a16).
  * 10-02 02:00Z: 11 rungs (B84550 @0.26..0.37, $4.07) filled 01:46:00Z; the wing take (11 lots/leg at the proxy's
    2-contract cap = 12 orders in one batch = 120 Kalshi write tokens) was 429'd 292/292 times -- Kalshi Basic =
    100-token bucket, 10 per order, a batch must FIT WHOLE (docs.kalshi.com/getting_started/rate_limits). The 11 NO
    sat NAKED to settlement (bucket settled NO, +$6.93 by luck). These 11 one-legged contracts are REAL and alone
    exceed the pin: the kill stands after the correction (11 > 2). S1_LEGGED occurrence #2 latched the day.
  Brad's words: "After this run, now we should switch on back to dry lol / Were running her a little too hot. Lets
  shut it down and assess damage"; "Shut it down for the 11 o'clock one if you haven't already" -> ops/v33_mode.txt
  = dry at 02:04:59Z (Claude's hand on Brad's word); "Lets start calling these test fires. We got the rocket to
  light, we got an engine to work. This was Test Fire #1, number 2 is next."
  Fixes merged in PR #110: netted-leg journal attribute + per-action/per-result guards; wing venue-confirm via
  /portfolio/fills before any leg is reported unfilled (additive, order_ids recorded -- review finding); amend
  404 -> status resolve; wing takes as sub-batches of <= min(8, bucket//10) orders paced to FIT the bucket (sync
  and async); wing 429 = definitive, backoff 0.5 s doubling to 4 s; Decimal-safe summary. Suite 1468 passed.
  OPEN (Brad's levers, before TEST FIRE #2 is registered): remove the false S1 occurrence from the 10-02 guard
  file; rule on the 09-30 pair (exclude/restate); proxy max_contracts_per_order 2 -> 11 (one order per wing
  leg) and/or Kalshi Advanced tier (POST /trade-api/v2/account/api_usage_level/upgrade; scope to the crypto
  instance unverified); and the re-registration terms for Test Fire #2 (same frozen thresholds unless amended
  on his word). V3.3 stays DRY until then.
- 2026-10-02 ~13:40Z -- TEST FIRE #2 REGISTERED (pre-arm; the arm itself is Brad's mode-file flip, recorded when it
  happens). Brad, verbatim: "Think we're ready to flip V3.3 back on?"; "Lets go ahead and raise the proxy budget, I can
  do that in the env if you dont mind restarting it after."; "Id like the same falsifier as test fire 1, I think. Maybe
  we should up n from 15 to 30 now."; "I agree with 30,000 budget and raising above n=30."; "Lets go with n=45 for test
  fire 2." Terms: the L6-frozen rules AS AMENDED BY L7 -- verdict / promotion at `n >= 45`, early kill mean < +2.0c at
  `n >= 15`, mean true lock >= +4.0c, one-legged <= 2 contracts, S4 $3.00 campaign kill, S1_LEGGED day latch at 2 --
  on the PR #110 engine (wing venue-confirm, sub-batched wings paced to the bucket, 429 definitive + backoff, amend-404
  status resolve, netting-journal fix). Evaluation window = the armed windows from the Test Fire #2 flip forward; Test
  Fire #1's windows (10-01 23:00Z .. 10-02 02:00Z) are CLOSED under L6 and are not pooled. Preconditions met at
  registration: Kalshi account tier ADVANCED (write 300/s, bucket 900, read 13:11Z 10-02); proxy restarted 13:15Z
  (pid 22952) with DAILY_ORDER_BUDGET 30000 (Brad's .env edit; 7253 used today -> 22747 remaining; the counter persists
  across restarts), MAX_CONTRACTS_PER_ORDER 2, orders_enabled true, same key fingerprint; venue clean (0 positions, 0
  resting, balance $66.15 total / $53.04 crypto instance); 10 overnight dry windows (03Z..12Z) clean (0 alarms, 5
  dry-sim entries, lock 3.5-5.3c/lot). Pending Brad's levers before the flip: (a) remove the FALSE S1_LEGGED occurrence
  (window 2026-10-02T00:00:00Z, the netting-bug false positive) from `ops/v33_stops_2026-10-02.json` -- with two
  occurrences the day is latched and every 10-02 window would degrade to dry; (b) merge of this amendment PR + live-tree
  pull in a :02-:33 band; (c) `ops/v33_mode.txt` -> armed. Pacer params stay 100/100 (sha-pinned; conservative under
  the 900 bucket). The 09-30 incident pair ruling remains open and does not gate the arm.
- 2026-10-02 13:54Z -- TEST FIRE #2 ARMED on Brad's order. Brad, verbatim (13:5xZ, after PR #113 merged f6940b5 by his
  own hand `! gh pr merge 113 --merge` and the live tree pulled to f6940b5 at 13:33Z, 1468 passed / 1 skipped, pins
  45 / 15 / 45 loaded): "Good to go on both. Lets run it!" -- "both" = (a) the FALSE S1_LEGGED occurrence (window
  2026-10-02T00:00:00Z, the netting-bug false positive) removed from `ops/v33_stops_2026-10-02.json` by Claude's hand
  at 13:54:41Z (the real 02:00Z occurrence and balance_start 57.8264 kept; count 1 < latch threshold 2; the pre-edit
  file is preserved in the session scratchpad), and (b) `ops/v33_mode.txt` dry -> armed, Claude's hand on Brad's word,
  13:54:41Z. `ops/v32_mode.txt` stays dry (never two armed). The 14:00Z close (spawned 13:40Z) runs DRY on the
  already-read mode; the FIRST ARMED WINDOW of Test Fire #2 is the 15:00Z close (spawn 14:40Z), subject to the arming
  gates at spawn (S5 incl. orders_remaining_today 22747 >= 500, reconcile, day latch count 1, S4 vs start 57.8264
  with balance $66.15). Engine: live tree f6940b5 (PR #110 fixes + L7), async writer ON (supervisor env), proxy pid
  22952 budget 30000 / cap 2, Kalshi Advanced tier. Rules in force: L6 as amended by L7 (verdict / promotion n >= 45,
  early kill mean < +2.0c at n >= 15, lock >= +4.0c, one-legged <= 2, S4 $3.00 campaign kill, S1_LEGGED day latch at 2).
- 2026-10-02 16:05Z -- TEST FIRE #2, WINDOWS 1-2 + INCIDENT (recorded by Claude). 15:00Z close (first armed window):
  NO fills, 0 contracts, venue clean; 11 alarms. Kalshi was slow for ~15 min (6 of 17 creates 1.9-2.5 s upstream;
  strike WS dropped 11 times 14:46-14:58Z, code 1006; none in the ten dry windows before, none in the 16:00Z window).
  The strike feed stalled -> stale-wing HOLD at 14:45:07Z -> the core cancelled the ladder before any create had a
  venue id -> the async executor treated each cancel as a no-op and REPORTED it cancelled -> the core re-placed the
  slots (4 flaps in 20 s, 61 place / 44 cancel requests) -> 17 orphan rests landed at the venue (14:45:15-22Z) ->
  the venue pre-flight rest-count invariant caught 17 > K=11 at 14:45:43Z -> executor stand-down for the hour (the
  belt held). The 17 orphans sat unmanaged at 0.60-0.73 until their 14:56:00Z expiry; 0 fills (luck). Verdict
  impact: none (n=0; one-legged 0). Fix = PR #115 (Brad: "Build the fix, lets try getting it in before the wake
  up"; merged b1b1afa at 16:03Z on his typed "! gh pr merge 115 --merge"; live tree pulled 16:03Z, 1471 passed /
  1 skipped): a CANCEL_REST for an in-flight create is DEFERRED (no event, slot stays owned) and executed the
  instant the ack lands, through the existing cancel-confirm path. Opus 4.8 review APPROVE WITH NITS; N3 (flag
  leak on a raised POST) fixed in the PR. RESIDUALS (open, not gating): (a) the batch-create path (dormant;
  batch_create defaults off) has the same original hole; (b) a fill landing in the ack->DELETE gap of a
  deferred-cancelled slot is dropped by the core's ladder (executor book sees it; no wing placed; bounded to one
  lot per slot) -- proper fix = keep the slot in the core until the cancel confirms. 16:00Z close (old code,
  spawned 15:40Z before the merge): armed, 0 alarms, 171 rolls, no entry, writer p50 78 ms. FIRST WINDOW ON THE
  FIXED CODE = 17:00Z close (spawn 16:40Z). Mode stays armed on Brad's standing word.
- 2026-10-03 03:30Z -- TEST FIRE #2 STOOD DOWN on Brad's order. Brad, verbatim (03:2xZ, after Claude's read of the
  02:00Z window): "Looks like we got a problem. Lets stand down" -> `ops/v33_mode.txt` = dry by Claude's hand at
  03:30:34Z; venue clean (0 resting, 0 positions); the 04:00Z close onward runs dry. Test Fire #2 is NOT killed by the
  frozen rules (one-legged 2 <= 2, n = 4 < 45, no S4 latch); it is PAUSED for engineering. The record, restated from
  EXECUTOR + VENUE truth where the ledger is wrong:
  * Armed closes: 10-02 15:00Z .. 10-03 03:00Z = 13 closes; 21:00Z DID NOT RUN (supervisor "close_already_run" on a
    pre-existing ledger row for that close -- a dedupe defect, item 6 below). Realized: 00:00Z 1 lot at NO 0.07,
    wings 0.90/0.93, lock 8.45c, settled +$0.089; 01:00Z 3 lots at NO 0.13/0.12/0.11, wings 0.86/0.90, locks
    8.7-10.8c, settled +$0.316; 02:00Z 2 lots NAKED (orders #23: 0.40 + 0.60 at NO 0.22; #27: 1.00 at NO 0.18;
    no wings), bucket resolved YES, settled -$0.400. Net +$0.005. Hedged n = 4 (mean realized lock 8.7c, 4/4
    positive); ONE-LEGGED = 2 CONTRACTS (at the pin; the next one is a KILL). The 02:00Z ledger row says lots 0 /
    one_legged False / alarms 0 -- FALSE: the executor journaled three `rest_fill` records (ws path) and ten
    alarms. The S1_LEGGED occurrence for 10-03 was NOT recorded by the engine (count by hand: 1).
  * The 02:00Z chain: 5 stale-wing hold/resume flaps in 40 s -> real stand-down cancel of 11 rests at 01:46:04Z
    (venue ids known; DELETEs sent) -> the wing feed returned and the core RE-PLACED 11 rests 230 ms later, before
    the cancel confirms -> the pre-place venue invariant saw 9 old orders still resting -> rest_invariant_violation
    x9 -> EXECUTOR STAND-DOWN -> two creates (#23, #27) had already passed the check and rested at the venue ->
    the stand-down cancels nothing it owns and the stood-down core issues no actions -> #23/#27 filled at
    01:50:21/23/58Z (fractional 0.40 + 0.60, then 1.00), seen by the executor, never hedged. NOT the #115 path
    (no `cancel_deferred_unacked` / `cancel_after_ack` records).
  * Executor stand-downs in 3 of 12 run windows: 15:00Z (17 orphans, expired unfilled), 02:00Z (2 orphans, FILLED
    naked), 03:00Z (1 violation, 12 holds, 0 WS drops, no fills). Stale-wing holds were common all evening (22:00Z
    39, 23:00Z 36) -- the hold fires without WS drops. The ledger `alarms` field read 0 in all three incident
    windows.
  * BUILDS REQUIRED BEFORE ANY RE-ARM (pre-registered here; each a reviewed PR Brad merges): (1) an executor
    stand-down must CANCEL EVERY REST IT OWNS and the core must keep hedging fills on anything still live until
    flat; (2) after a stale-wing cancel the core must NOT re-place inside the cancel-confirm window (hold placement
    until outstanding cancels confirm); (3) one-legged / rung-fill accounting for the ledger, the day guard and
    the report must come from EXECUTOR truth (`rest_fill`), and the ledger `alarms` counter must include executor
    alarms; (4) stale-wing hold sensitivity reviewed (holds without feed drops); (5) the #115 residuals (batch
    create path; a fill in the ack->DELETE gap of a deferred-cancelled slot is dropped by the core ladder); (6)
    supervisor dedupe skipped a live close on a pre-existing ledger row. Items 1-3 gate the re-arm; 4-6 are
    recorded for the same PR series or the one after. Thresholds unchanged (L6 as amended by L7).
- 2026-10-03 04:55Z -- BUILD BRIEF for the re-arm gates: `pilot/build/v33_naked_fill_2026_10_03.md` (Brad: "Write up the
  full explanation so it's not lost. Why those wings didn't fire, and what needs to happen to fix it."). The exact
  mechanism of the 02:00Z naked fill: (1) a cancel event with `order_id None` is attributed to the first pending
  order by `None == None` in `core._apply_cancelled`, so nine pre-flight rejections evicted nine innocent pending
  orders; (2) the #115 in-flight guard starts AFTER the pre-flight venue GET, so the cancel for #27 was a no-op, and
  an executor stand-down cancels nothing it owns; (3) `core._apply_fill` returns silently on a fill whose order is
  not on the core's ladder -- the venue filled #23 / #27, the executor journaled all three fills, the core discarded
  them, no wing was ever SENT. Gates A-D (hedge every executor-owned fill even when stood down; cancel identity by
  client_order_id; in-flight guard from the top of the place call + stand-down cancels every owned rest + stood-down
  executor still takes wings; no re-place inside the cancel-confirm window) supersede items 1-3 of the 03:30Z entry
  as the pre-registered re-arm conditions; E-H (executor-truth accounting, hold sensitivity measured, #115
  residuals, supervisor dedupe) follow in the same series. Thresholds unchanged.
- 2026-10-03 20:24Z -- RE-ARM GATES A-D BUILT AND REVIEWED, NOT MERGED; both rosters DRY. Brad, verbatim, on the
  stale-wing question ("Should we add logic to check for that condition or remove the stale wing cancel since
  'stale' just means no trades, right? Thats not a WS problem on either ours or kalshis side"), after the finding
  below: "Agreed, go ahead and build using the normal processes. Opus 4.8 builder agents and reviewer agents".
  * FINDING (02:00Z journal, measured): "stale wing" = a wing STRIKE book with no `orderbook_delta` for more than
    `freshness_max_age_s` = 1.0 s. Not trades, not the WebSocket: a quiet deep strike. The no-leg wing T84699.99
    had 143 inter-delta gaps > 1 s (26 > 2.5 s) in the window, the yes-leg T84499.99 284 > 1 s, zero WS drops.
    The fatal hold: T84699.99 quiet 2.9 s while T84599.99 pushed 44 frames in the same 1.6 s -> 1.5 s hold ->
    UNTRACKED `_cancel_all` -> resume 230 ms later -> `_place_all` with 11 cancels unconfirmed. The bucket-change
    path tracked its cancels and waited; the stale path did not. The wing TAKE already freshness-gates itself at
    fill time, so the rest-level stale cancel protected nothing extra. Item (4) of the 03:30Z entry ("hold
    sensitivity reviewed") is hereby MEASURED: 85 journals 09-30..10-03, 1,747 journaled holds; under the old
    predicate replayed over full windows 3,140 holds / 589 cancels; every hold in the 73 live-feed windows was a
    quiet-book artefact (`pilot/build/v33_stale_wing_measurement_2026_10_03.md`).
  * GATE D IS A REWRITE, not a wait (clarifies the 04:55Z wording; thresholds and judged quantities unchanged):
    the rest-level stale predicate becomes wing book MISSING or SUSPECT, or strike FEED dead (`strike_feed_ts` =
    latest `book_ts` on ANY of the ~188 strike books; dead when older than `strike_feed_dead_s`), or a wing book
    older than `wing_book_max_age_s`; a single quiet strike is never stale. The wing-take gate keeps the loose
    per-strike bound. EVERY cancel-all that can be followed by a re-place tracks its cancels and places only when
    `outstanding_cancels == 0` (the 04:55Z "no re-place inside the cancel-confirm window", now on all paths).
    v32 byte-identical; the shadow keeps 1.0 s. Under the new predicate: 02:00Z -> 0 holds / 0 cancels (old
    replay 47 / 5); the REAL 10-02 15:00Z strike stall -> still stands down (20 holds / 11 cancels); all 395
    remaining holds fall inside the 12 stall/lag windows, none in the 73 live-feed windows.
  * MEASURED DEFAULTS, RECORDED HERE (registered values; provenance = the measurement md): `strike_feed_dead_s` =
    4.0 s (live-feed strike inter-frame gap p99.9 0.13 s, max 0.97 s; max live feed age vs eval clock 2.96 s;
    stalls 8-75 s); `wing_book_max_age_s` = 30.0 s (quiet wing books on a live feed reach 13.8 s, p99.99 11.15 s;
    a take refused at this bound is a naked fill). Shipped as OPTIONAL keys with code defaults; the params JSON
    and its sha 295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def are UNCHANGED. OPEN FOR BRAD:
    re-pin both values in `pilot/policy/v33_params.json` (new sha, loader-enforced) vs leave optional. Claude
    recommends re-pin; the reviewer recommends at minimum this line.
  * PR #119 `fix/v33-hedge-owned-fills` = gates A, B, C, G (head d5d099c): `_apply_fill` books a fill whose coid
    is on no ladder order as an `orphan` RungFill from the event's own ticker/price/count, alarms
    `orphan_rung_fill_hedged`, and its wing batch fires TAKE_WINGS INCLUDING when the core is stood down;
    `OrderCancelled` carries `client_order_id` and `_apply_cancelled` never matches `None == None`
    (`cancel_unattributed` alarm); in-flight registration from the TOP of the single and batch create paths (a
    cancel during the pre-flight skips the POST); executor stand-down runs `standdown_sweep_async` (DELETE every
    live owned rest, confirm, emit the cancel events so a racing fill is hedged); the stood-down executor still
    answers TAKE_WINGS / RETRY_WING / cancels and refuses PLACE / AMEND. Golden replay of the 02:00Z event order
    from a committed fixture: the driver stand-down landed BEFORE the first rejection, so the ladder empties and
    #23 / #27 arrive as ORPHANS -- gate A, not B, is what hedges them: TAKE_WINGS 2.00 lots on T84599.99 yes /
    T84699.99 no, three orphan alarms (0.40, 0.60, 1). 13/14 new core tests fail on the pre-fix core. Opus 4.8
    REVIEW: APPROVE WITH NITS (`pilot/build/v33_hedge_owned_fills_review.md`); one DEFECT fixed on the branch
    (an unpriced owned orphan fill raised on the ingest path; now `orphan_rung_fill_unpriced` fail-closed).
    Residuals recorded: sync executor has B but not the sweep (live runs async, journal-confirmed); orders with
    an unknown POST outcome stay unowned (listed by the sweep); orphan lots can exceed the allotment, unenforced
    live; the ledger counts orphan lots but does not label them and `alarms` still excludes executor alarms
    (gate E). Suite 1439 passed / 10 skipped (1492 / 5 with the census CSV present).
  * PR #120 `fix/v33-stale-wing-liveness` = gate D rewrite + gate F measurement (head d2583f3). Opus 4.8 REVIEW:
    APPROVE WITH NITS, no code change (`pilot/build/v33_stale_wing_liveness_review.md`); mutation-checked (the
    untracked cancel reproduces "PLACE_REST while the venue still holds 11 of our rests"); measurement reproduced
    independently. MERGE COMPATIBILITY with #119 clean (disjoint functions). The one residual on #120 ALONE --
    `outstanding_cancels` is a count and any `OrderCancelled` decremented it -- is closed by #119's "only a
    counted cancel decrements", in either merge order. RE-ARM REQUIRES BOTH PRs MERGED. NIT recorded: a dead-feed
    stand-down shares the journal reason `stale_or_missing_wing` with missing/suspect/too-old (sub-cause
    annotation = follow-up). Suite 1483 passed / 5 skipped (1487 / 1 with the census CSV).
  * PROCESS DISCLOSURE: both BUILDERS ran on Opus 5.5, not Opus 4.8 -- the orchestrator passed a model override
    that replaced the agent definition's 4.8 pin (PR #120's commit trailers say Opus 5.5; PR #119's say Fable
    5.1 as instructed, written by Opus 5.5). The first #119 reviewer was launched the same way and was killed
    before pushing; BOTH REVIEWS then ran on true Opus 4.8. Recorded in memory so it does not recur. OPEN FOR
    BRAD: whether the 4.8 reviews are sufficient cover or either PR is to be rebuilt.
  * STATUS OF THE 03:30Z / 04:55Z CONDITIONS: A, B, C, D built and reviewed (G folded into #119; F = the
    measurement in #120). E (executor-truth accounting) and H (supervisor dedupe) remain, same series. Re-arm =
    #119 and #120 merged by Brad + E or Brad's explicit waiver of E + a Registration line + Brad's word. Test
    Fire #2 stays PAUSED at one-legged 2 = the pin; thresholds unchanged (L6 as amended by L7).
- 2026-10-03 20:43Z -- PARAMS RE-PIN (stale-wing liveness bounds), on Claude's recommendation recorded 20:24Z; lands on
  Brad's merge of the params PR, which is his ruling. `pilot/policy/v33_params.json` now writes `strike_feed_dead_s`
  = 4.0 and `wing_book_max_age_s` = 30.0 EXPLICITLY (the measured defaults from
  `pilot/build/v33_stale_wing_measurement_2026_10_03.md`; values unchanged; every other key byte-identical), so the
  loader's sha check ENFORCES them (registered-specs rule) instead of trusting an absent key. New canonical sha
  `f403708257fe7b3a91583882aa98e0f107289c851247e5d3c841bca5e61da1b1`; prior `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def` kept
  in code as `PREVIOUS_V33_PARAMS_SHA256_L4`. Params-only; no judged quantity, threshold or lever changes; BOTH
  ROSTERS STAY DRY. Re-arm conditions as stated 20:24Z (gate E, Registration line, Brad's word) unchanged.
- 2026-10-03 22:00Z -- GATES A-E MERGED; DRY SHAKEDOWN ORDERED, NO RE-ARM. Brad, verbatim: "Go ahead and merge once
  everything is reviewed and ready. Dont go live though. Lets run it dry with the new code as a shake down".
  Merged to main on his word, in order: #119 (A/B/C/G, a9bbc7a), #120 (D rewrite + F, 2703265), #122 (params
  re-pin, 6839214; "Go ahead and merge the re-pin"), #123 (E, 296f776; Opus 4.8 APPROVE WITH NITS, four defects
  fixed on the branch: `reconcile_failed` counted; the reconcile-exception fallback is executor-truth-only, never
  the blind core, with S1 additionally latched on any executor-truth fill; rebuild poll deltas SUM not MAX; venue
  fills filter fails closed). The reviewer's answer to "can the pin under-see a naked contract after this PR?":
  not on the live path with print-through OFF; the one latent path is the aggregate one-legged subtraction under a
  print-through over-hedge -- per-order one-legged is REQUIRED before print-through is ever enabled (recorded).
  Live tree = main 296f776, pulled 21:57Z, dry; full suite 1536 passed / 1 skipped on the live checkout; params
  sha f403708257fe7b3a91583882aa98e0f107289c851247e5d3c841bca5e61da1b1 enforced by the live loader. Gate E was
  built AND reviewed on Opus 4.8 (the A-D builders ran on Opus 5.5, disclosed 20:24Z; Brad merged with that known).
  * RE-ARM CONDITIONS STATUS: A, B, C, D, E merged (G in #119, F in #120); H (supervisor dedupe) outstanding and
    NOT a re-arm gate. Remaining for re-arm: this dry shakedown read clean + a Registration line + Brad's word.
    Test Fire #2 stays PAUSED at one-legged 2 = the pin.
  * SHAKEDOWN (dry, the supervisor spawns `python -m service.run_v33` per close, so the first window on the new
    code is the 23:00Z close; the 22:00Z window was already running on 2ed9ff5-era code when the pulls landed).
    What a clean read means, pre-stated: (i) `stand_down_hold` / `stand_down_cancel` ~0 on live-feed windows
    (the 10-02/10-03 dry windows ran 12-39 holds each under the old predicate); (ii) every ledger row carries
    `alarms_breakdown`, `reconcile`, `one_legged_contracts`; dry rows reconcile byte-identically (no spurious
    `ledger_reconcile_mismatch`); (iii) no `reconcile_failed`, no uncaught exception, no `venue_fills_*` kinds in
    dry; (iv) the venue invariant never stands the executor down on a live-feed window (the old 3-of-12 rate was
    the re-place race). Anything else is a finding before re-arm.
- 2026-10-03 23:04Z -- SHAKEDOWN WINDOW 23:00Z READ CLEAN; PRE-FLIGHT CLEAN; BRAD'S VOTE TO RE-ARM FOR THE 00:00Z CLOSE
  (his "8PM local window"). Brad, verbatim: "Sounds good, lets build the labeling fix. Meanwhile, id vote to take
  it off dry for the 8PM local window". The flip itself is Brad's hand (V33_ARMING section B: :02-:33 UTC window,
  never :38-:59; the :40 process reads `v33_mode.txt` fresh) and is recorded by its own line when it happens.
  * 23:00Z WINDOW (first on main 296f776+/418ca69, dry, params sha f4037082 in `window_meta`) against the 22:00Z
    pre-stated criteria: (i) holds: ONE `stand_down_hold` -> `stand_down_cancel` at window t+555 s (T-644 s),
    11 tracked `would_cancel_rest`, ZERO re-place after -- and it was NOT staleness: strike feed alive (strike vs
    bucket server-ts lag max 0.09 s all window, inter-frame gap max 0.93 s), both wing books ticked within 0.2 s
    of the hold, no suspect. Cause by book reconstruction: the YES-leg wing strike `KXBTCD-26OCT0319-T84699.99`
    (yes 0.97-0.99 all window) had its last NO bid (0.01) lifted at t+555, so `yes_ask` = None -- NO OFFER on the
    hedge leg at any price; the NO side stayed empty through the close. Standing the rests down is the CORRECT
    response (never rest a rung whose hedge cannot be bought); the old predicate would have done the same.
    (ii) ledger row carries `alarms_breakdown` {total 0}, `reconcile`, `one_legged_contracts` 0, `unbooked_fills`
    [], `reconcile_mismatch` False -- a byte-clean dry reconcile. (iii) no `reconcile_failed`, no `venue_fills_*`,
    no exception. (iv) no `rest_invariant_*`, no executor stand-down. VERDICT: CLEAN on all four; one
    diagnosability finding -> the sub-cause label build (`fix/v33-stale-wing-subcause`, Opus 4.8, in flight; a
    LABEL, no behaviour change; NOT to be pulled into the live tree while a window is armed outside :02-:33).
  * PRE-FLIGHT at 23:04Z (read-only through the proxy): balance $66.1517 (exchange_index 2: $53.0399); resting
    orders 0; unsettled positions 0; proxy `orders_enabled` true, `max_contracts_per_order` 2, prefixes KXBTC15M /
    KXBTCD / KXBTC, daily budget 30000; falsifier `STATUS: FROZEN`; live loader sha f4037082 verified; live suite
    1536 passed / 1 skipped; V3.2 already dry (so the flip touches `v33_mode.txt` only). Supervisor pid 11588.
  * EXPOSURE STATED BEFORE THE FLIP: Test Fire #2 is PAUSED at one-legged 2 = the pin; the NEXT naked contract is a
    KILL. Known residual path: a rung FILL landing inside the 1.5 s stale-wing hold (or before the tracked cancels
    confirm) while the hedge leg has no offer (tonight's `wing_no_ask` case) -> the wing batch WAITS for a priced
    book; if none returns by the cutoff, that lot is one-legged. Gate E now guarantees the ledger SEES it. The
    hold length (`stand_down_hold_ms` 1500) is the dial; unchanged tonight.
  * RE-ARM CONDITIONS (03:30Z / 04:55Z / 20:24Z / 22:00Z entries): A-E merged; shakedown read clean; this line;
    remaining = Brad's word and hand on `v33_mode.txt`. Thresholds unchanged (L6 as amended by L7).

## Pre-registered shadow observations (observational; change NOTHING above this line)

### SO-1 -- edge-ladder shadow (E = 0.08 and E = 0.12), inherited from V3.2
The in-process shadow re-solves and scores E in {0.08, 0.10, 0.12} every tick inside the quoting window
T-15..T-5 (the V3.2 shadow, forked byte-identically). Recorded per report alongside the live ladder.

### SO-3 -- deep-end observation ladder (19..28c), registered with this draft
`deep_obs_rungs` = 10 observation-only rungs BELOW the live ladder (margins 19..28c). For every
spot-bucket YES-taker print inside the quoting window, record per deep rung whether the tape REACHED it
(a print at/through the rung's NO price while the rung is placeable, solved n >= n_min), the IDEAL lock
there (`lock_value(deep_price, W_at_reach)`), and the ABSORPTION (lots the tape printed at/through the
rung). It NEVER places an order and NEVER holds a position -- it MEASURES the deep end (whose absorption
the ideal study could only INFER) before anyone sizes into it, informing the Promotion clause. Surfaced
in the report's "DEEP END (SO-3, observation only)" block.

## L5 amendment (2026-09-29) -- per-rung lot weights (`rung_lots`)

MECHANISM ONLY; changes NOTHING in the frozen quantities above. The ladder core now accepts an OPTIONAL
policy key `rung_lots`: a list of `rungs` non-negative ints, index k = rung k (k=0 top rung at margin
`E_min`, k=K-1 deepest); a 0 means that rung is never placed. When ABSENT (as in the currently shipped
policy) it defaults to `[lots_per_rung] * rungs` -- ALL ONES -- and the core is byte-identical to the
pre-L5 ladder (the existing goldens are untouched and still pass). The hour's exposure cap becomes the
CONTRACT allotment `sum(rung_lots)`; `max_sets_per_hour` stays the coarse rung/fill-event gate.

The weights are BRAD'S LEVER: he sets them (after a dry week, per his "scale the edge -- not 100% capital
at 10c") by editing `pilot/policy/v33_params.json` and RE-PINNING the canonical sha in a params PR (the
loader refuses any drift, S5). No agent sets the weights. The falsifier's per-rung SOLVED-lock and
`E_rung` quantities are unchanged; the ledger's `rung_fills` rows now also carry the rung's `weight`, and
the report gains an "ALLOCATION TABLE (per placed margin)" so the weighted allocation's result is
readable. The roll/re-size rule (a roll KEEPS the moving order's count; re-sizing to a slot's weight
happens only on a fresh placement) is documented in `service/v33/core.py`'s module docstring and
`pilot/build/v33_rung_lots_build_report.md`.
