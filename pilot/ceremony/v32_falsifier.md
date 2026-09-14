# V3.2 FALSIFIER -- continuous-requote spot-bucket pump-fader (maker rest, taker completion)

STATUS: FROZEN

This file is FROZEN (see Registration). It was a DRAFT until then. Brad ALONE flips the line above to exactly `STATUS: FROZEN` on his verbatim go,
and appends the go under Registration. Until then V3.2 cannot arm: the S5 arming gate
(`service.v32.stops.v32_arming_check` -> `service.stops.falsifier_is_frozen`, wired in
`service.run_v32` at `--falsifier` default `pilot/ceremony/v32_falsifier.md`) requires this exact path
to carry a line that is exactly `STATUS: FROZEN`. An agent NEVER flips it. From the freeze line down,
nothing in this document may change except appended verdicts in Registration.

Every pre-registered threshold below is marked `[pin]` and is MIRRORED as a named constant in code
(`service.v32.falsifier_pins` for the verdict/retirement/promotion gates; `service.v32.stops` for the
day-stop pins). A unit test (`tests/test_v32_falsifier_pins.py`) fails the suite if this document and
those constants disagree -- the doc does not merely describe the thresholds, the code ENFORCES them.

## Freeze provenance

- Build: pilot V3.2, phases 1-4 on Opus 4.8, disclosed branches `v32/phase<N>-<name>`, reviewed and
  merged to `main` by Brad (Phases 1-3 = PRs #31, #32, #33; Phase 4 pending).
- Policy: roster `DegeneracyV3_2`, `pilot/policy/v32_params.json`, canonical sha
  `0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc` (pinned in code as
  `service.v32.params.FROZEN_V32_PARAMS_SHA256`; the loader self-verifies and refuses drift).
- Provenance of the numbers below: the 2026-09-13 sim (mailbox entries + `claudes-corner/
  the_flutter_that_wasnt_2026_09_13.md`); scratchpad `journals/pf_ms_requote2.py`,
  `pf_ms_depth.py`. The seal (2026-08-02..18) and the holdout (2026-08-20..29) were NEVER read; the
  training corpus (06-22..08-01) has no ms strike books, so this candidate was measured only on the
  post-holdout forward tape and is now judged on LIVE fills.

## What is being judged

The CONTINUOUS-REQUOTE spot-bucket pump-fader at E = 0.10, tol = 0.02, deb = 5000 ms, 1 contract, on
the KXBTC hourly RANGE buckets, on LIVE fills, against an in-process shadow (the ideal, no-lag fill
rule) run on the SAME feed at E in {0.08, 0.10, 0.12}. The strategy is fully specified in
`pilot/PLAN_V32.md` ("What V3.2 is" + "Requote policy"): rest a bucket-NO bid at the largest whole-cent
n with `n + fee(n) <= 2.00 - E - W`, continuously re-solve/replace it (cancel -> confirm -> create,
never two live rests), and on fill immediately TAKE both pin wings as a taker; the $2 pin pays $2.00 at
every settlement. Lock = `2.00 - (n + fee(n)) - W_paid`.

The number two measurements decide it: the realized lock per completed set on live fills, and the gap
between that live lock and the in-process shadow's ideal lock (does the venue's latency eat the edge?).

Sim basis (forward 2026-08-30..09-04, 139 h, lagging-quote model, exact fees):
- E = 0.10 requote (tol 2c, deb 5000 ms): 21 fills, 3.6/day, mean lock +9.2c, p10 +5.5c, min +3.4c,
  100% positive, +33.5c/day, ~77 replaces/h.
- Ideal (no-lag) at E = 0.10: 21 fills, +9.3c, 95% positive.
- Wing depth at completion (+1.5 s): thinner wing median 409 lots, p10 26, min 19 -> 1-2 contracts
  never bound.

## Policy (roster `DegeneracyV3_2`, `pilot/policy/v32_params.json`, sha-pinned; loader refuses drift)

sha `0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc`. Values: E 0.10, tol 0.02,
deb_ms 5000, quote_start_s 900 (T-15), quote_end_s 300 (T-5), contracts 1, wing_margin 0.02, lock_floor
-0.10, no_orders_after_s_to_settle 1 (T-1 s hard cutoff), freshness_max_age_s 1.0,
bucket_freshness_max_age_s 30.0 [pin], max_sets_per_hour 1,
n_min 0.05, replace_rate_alarm_per_min 60, bucket_width 100 ($250/$500 hours stand down),
shadow_Es {0.08, 0.10, 0.12}. One completed set per hour; hold to settlement.

## Proposed pre-registered thresholds (the verdict)

Judged on LIVE fills. The verdict is decided only once there are `n >= 30` [pin] live completed sets
(both wings taken); before that the scoreboard prints `n<30 pending`. At `n >= 30`:

- ALIVE iff ALL of:
  - mean realized lock >= +4.0c [pin], AND
  - % positive >= 80% [pin], AND
  - live fill rate >= 2.0 sets/day [pin] over the armed evaluation days, AND
  - mean execution gap (shadow E=0.10 lock - live lock) <= 3.0c [pin], AND
  - one-legged sets <= 2 of 30 [pin].
- KILL iff ANY of those five thresholds is missed at `n >= 30`. No re-spec on the same evaluation
  window (the registered-specs rule): a miss is a kill, not a re-parameterisation.

The report computes this verdict (`ALIVE-so-far` / `KILL` / `n<30 pending`) directly from the `[pin]`
constants in `service.v32.falsifier_pins` -- see the FALSIFIER SCOREBOARD block in
`python -m service.v32.report`.

Power note: at sd ~= 5c and n = 30 the SE of the mean lock is ~0.9c, so the +4.0c bar sits ~4.4 SE
above zero -- this gate catches a broken premise (the edge does not survive real fills), not a marginal
few-tenths-of-a-cent shortfall. The sharp instrument for a small edge is the execution-gap gate.

## Alarms (notify + protect the hour; the day keeps running)

- A_REPLACE: replaces in a trailing 60 s above `replace_rate_alarm_per_min` (60) -> the pure core
  cancels the resting order and stands the HOUR down (no more quoting this window); the driver journals
  the alarm. (`service.v32.stops.A_REPLACE`.)
- A_STALE: a strike (wing) book older than `freshness_max_age_s` (1.0 s), OR the spot-bucket
  (range) book older than `bucket_freshness_max_age_s` (30.0 s) [pin] -> the core cancels the rest
  and does not re-place until fresh (a lagging strike feed poisons W; a stalled bucket feed poisons
  spot selection + the cap, reason `stale_bucket`). The bucket bound is SEPARATE and larger because
  range buckets are thin and tick far less often than the strike books. (`A_STALE`.)
- A_EXEC_PRICE (exec price mismatch): a leg's executed price != the resting/decided price beyond the
  wing margin is flagged per fill and its running mean reported (this feeds the execution-gap gate).
- A_REJECT x3: three consecutive rest rejections (a definite 4xx: post_only cross, cap, or
  `daily_order_budget` 403) latch a `stand_down` for the hour (`executor.CONSECUTIVE_REJECT_STANDDOWN`
  = 3).

## Stops (halt the UTC day; latched in `ops/v32_stops_YYYY-MM-DD.json` -- SEPARATE from the box guard)

- S4: daily loss >= $3.00 [pin] (`V32_S4_DAY_LOSS_CAP_DOLLARS`), measured on the ACCOUNT BALANCE
  (start-of-UTC-day snapshot at the first clean wake, compared each wake), via the pending-settlement
  BAND so a pending settlement never moves the number a latch is decided on. FLOOR-NETTING RULING
  (coordinator 2026-09-13, applied Phase 4): the pending credit is banded per unsettled set --
  pessimistic = guaranteed floor, optimistic = guaranteed floor + upside, where guaranteed floor =
  $2.00 for a complete 3-leg pin / $1.00 for any 2-leg subset / $0.00 for a lone leg, and upside =
  $0.00 / $1.00 / $1.00 respectively. loss_pessimistic = start - (now + pessimistic);
  loss_optimistic = start - (now + optimistic); LATCH iff loss_optimistic >= cap; CLEAR iff
  loss_pessimistic < cap; else PENDING -> stand the window down (degrade to dry), no day latch, the
  next wake re-evaluates. (`service.v32.ledger.v32_pending_credit` -> `service.v32.stops.v32_s4_decision`.)
- S5 (arming refusal): this file's STATUS line not exactly `STATUS: FROZEN`, the params sha unverified,
  proxy `/health` `orders_enabled` not true or caps absent, caps not covering BOTH `KXBTC-` and
  `KXBTCD-` (via the proxy's own startswith test), fewer than `V32_MIN_ORDER_BUDGET_AT_ARM` (200) [pin]
  creates left in today's budget, `max_contracts_per_order` outside [contracts, 2] [pin]
  (`V32_MAX_CONTRACTS_PER_ORDER` = 2), a latched stop / corrupt guard for today, or an inherited
  un-settled KXBTC* position (reconcile-first) -> never fires an order (degrade to dry).
- S1_LEGGED: a completed set left one-legged below the lock floor at T-1 s stands the HOUR down (core);
  the DAY latches after 2 [pin] such occurrences (`V32_S1_LEGGED_LATCH_THRESHOLD` = 2).
- reconcile-first: any KXBTC* position with non-zero size at :40 refuses to arm (a fresh window never
  arms on top of an unknown open position).

## One-legged fill handling

Any TWO of the three legs are a $1 floor (bucket-NO + a wing pays >= $1 everywhere), so a missing wing
leg is bounded, not naked. On a rest fill the INITIAL both-wings take is UNCONDITIONAL. If a wing leg
is missed, retry the missing leg every tick until the T-1 s cutoff (`no_orders_after_s_to_settle` = 1),
subject to the retry lock floor: never pay for the last leg if it would push the set's lock below
`lock_floor` = [pin] -0.10 (retry at better prices only); else hold the two-leg $1-floor
position to settlement and flag `one_legged`. A lone bucket-NO never hedged is flagged `one_legged` at
the cutoff too (so S1_LEGGED counts it).

## Retirement (pre-committed; the report computes them)

- R1 THRESHOLDS: the verdict gate above. Any of the five thresholds missed at `n >= 30` -> KILL, report,
  redesign. No re-spec on the same evaluation window.
- R2 ECONOMICS: 3 consecutive [pin] UTC days of NEGATIVE realized (settled) P&L -> KILL. (Real money,
  settlement-backfilled, not the conservative floor.)
- R3 LEGGING: 2 S1_LEGGED [pin] day-latches within a 7-day [pin] window -> KILL (the taker completion is
  systematically failing; we are trading a different, unpriced thing).
- R4 EXECUTION GAP (early kill): execution gap > 5.0c [pin] already at `n >= 15` [pin] -> KILL without
  waiting for n=30 (the venue's latency has plainly eaten the edge; no point spending 15 more sets).
- Power note (repeat): SE of the mean lock ~0.9c at n=30; R1's mean-lock bar is a premise test, the
  execution-gap gate (R4 early, R1 at n=30) is the sharp instrument.

## Promotion

Nothing in this document promotes past 1 contract automatically. What would JUSTIFY 2 contracts (Brad's
word, dated): ALIVE at `n >= 60` [pin] completed sets AND a thinner-wing depth check from the journals
showing >= 10 lots [pin] available at every completion (`V32_PROMOTION_MIN_DEPTH_LOTS` = 10) so a
2-contract taker completion never binds. The proxy cap `MAX_CONTRACTS_PER_ORDER` = 2 stands regardless.

## Pre-arming checklist (Brad's levers; mirrored mechanically in `pilot/ops/V32_ARMING.md`)

1. Proxy `.env` `ORDER_TICKER_PREFIXES` includes `KXBTC` (covers both `KXBTC-` buckets and `KXBTCD-`
   strikes via startswith); Brad edits `.env` and restarts the proxy.
2. `DAILY_ORDER_BUDGET` >= 4000 (the requote policy needs ~1,900 creates/day; 2x margin). Brad sets it.
3. Proxy restarted; `curl 127.0.0.1:8642/health` shows `orders_enabled: true` and the caps block
   (`max_contracts_per_order`, `ticker_prefixes`, `daily_order_budget`) and
   `orders_remaining_today` >= 200.
4. >= 2 dry windows with `would_place_rest` records, shadow records, and NO discovery errors (see the
   dry-run runbook).
5. Clean live tree on `main` (V3.2 phases merged); `cd pilot && python -m pytest -q` green.
6. Brad freezes THIS file (STATUS: FROZEN) on his verbatim go and appends it under Registration.
7. Brad writes `armed` to `pilot/ops/v32_mode.txt`.
8. Brad registers the `DegeneracyV3_2` task (or flips `v32_mode.txt` if already registered).

## FIRST ARMED WINDOW MUST CONFIRM (copied verbatim from the Phase 3 review)

1. A GTC rest carries `expiration_time` (Unix seconds) and the venue ACCEPTS it (auto-expires at T-5).
2. A live `GET /portfolio/orders/{id}` returns `fill_count_fp`/`remaining_count_fp`/`initial_count_fp`
   and a `status` the confirm treats as terminal (`status not in ("resting", None)`), so the
   poll/cancel see fills.
3. The `DELETE` reply carries `reduced_by`; `placed - reduced_by` agrees with the WS fill on a real
   cancel race.
4. A NO (bucket) rest fill reports `purchased_side="no"` with `yes_price_dollars` (validated against
   `fill_frame.json`; the NO-space paid price = 1 - yes) -- the `exec_price_mismatch` alarm stays quiet.
5. `/health` exposes `orders_enabled`, `caps.max_contracts_per_order`, `caps.ticker_prefixes`,
   `orders_remaining_today` in the shapes `v32_caps_agree` reads; positions/balance shapes match
   reconcile/S4.
6. No requote-overlap double-fill (now sequential by R-OVERLAP); no unknown-POST stand-down under
   normal latency.
7. Reconcile-first sees ZERO size for the previous hour's SETTLED positions by :40 (a settled KXBTC*
   position reports position 0 / is absent, so a fresh window is not blocked from arming by a stale
   settled row).

## Registration (append-only; the freeze line, Brad's verbatim go, and every verdict go here)

- 2026-09-14 ~18:58Z -- FROZEN on Brad's order. Brad, verbatim (2026-09-14, after reading the judged quantity, the five gates and the one/two-leg handling): "Okay, go ahead and freeze it. That sounds good" (earlier the same day: "Go ahead and flip what of those levers you can and let me know what needs me ... Lets get it set up for the next wake up"). Thresholds frozen AS PROPOSED above. Roster `DegeneracyV3_2`, params sha 0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc. Preconditions at freeze: two dry windows (closes 17:00Z, 18:00Z; window 2 clean post-hotfix PR #38, 81 replaces, no alarm), scheduler wake 18:40Z confirmed, proxy restarted 18:20Z with ORDER_TICKER_PREFIXES incl. KXBTC and DAILY_ORDER_BUDGET 4000, `ops/v32_mode.txt` = armed (written 18:53Z on Brad's "Go ahead and run the command"). Mechanical edit performed by Claude on Brad's explicit order; merged to main as the freeze act.

## Pre-registered shadow observations (observational; change NOTHING above this line)

### SO-1 -- edge-ladder shadow (E = 0.08 and E = 0.12), registered with this draft
The in-process shadow re-solves and scores E in {0.08, 0.10, 0.12} every tick on the live tape. At each
report, record the shadow fills, mean/median/p10/min lock, and % positive for E=0.08 and E=0.12
ALONGSIDE the live E=0.10 statistics. Sim reference (139 h forward): E=0.08 -> 24 fills, +7.6c, 100%
pos; E=0.12 -> 15 fills, +11.1c, 100% pos. This observes, without changing the live policy, whether a
different edge would have paid more per day or per fill -- an amendment candidate only on Brad's dated
word.

### SO-2 -- requote count per hour vs sim
Record replaces/hour (mean and p99) per armed window. Sim reference at the chosen gate (tol 2c, deb
5000 ms) = 77 replaces/h. A live count far above 77 signals a noisier live book (or a gate bug) and a
larger `DAILY_ORDER_BUDGET` need; far below signals a frozen/stale feed. Observational.

### SO-3 -- data-age p99 per connection
Record the p99 data-age (lag) for the strike connection and the bucket connection separately per window
(the two-connection topology's gauges). A rising strike-connection p99 is the leading indicator that
A_STALE and the execution gap will bite. Observational.

## Amendment

Amendments (a new roster sha, a threshold change, a promotion) follow the box precedent: Brad's dated
verbatim go, a full Opus 4.8 review + agent build, a new pinned sha the loader self-verifies, and an
appended Registration line. Nothing above the "change NOTHING above this line" marker changes except the
appended Registration entry.
