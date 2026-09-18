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

1. A GTC rest carries `expiration_time` (Unix seconds) and the venue ACCEPTS it. The executor's
   quote-end cancel at T-5 is the PRIMARY path that removes the rest; the venue's own expiry is set to
   T-4 (`EXPIRATION_GRACE_S` = 60, PR #50) as the crash backstop only. A rest that survives to the
   by-design T-4 venue auto-expiry is therefore NOT a failure of this item (see the 2026-09-15
   Registration clarification).
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
- 2026-09-14 ~22:25Z -- ARMED WINDOWS 1-3 and RE-ARM authority. Window 20:00Z: 3 creates -> venue 404 (doubled `/trade-api/v2` prefix; hotfix PR #43); executor stood down after 3 rejections, nothing reached the book. Window 21:00Z: stand-down by design ($250 hour). Window 22:00Z: creates 201, but every un-sharded cancel -> 404 and the executor treated 404 as gone -> 21 rests stacked in 2.5 min; Claude killed the process 21:47:17Z, hand-cancelled all 21 with `?exchange_index=2`, set the mode file to `dry`; no fills, balance $51.997 unchanged (hotfix PR #46: shard-aware cancel, 404 != gone, venue-truth invariant before every PLACE; incident replay pins <= 1 live rest). Full order-path audit `pilot/build/v32_order_path_audit_2026-09-14.md`: 5 VERIFIED-LIVE, 5 PROD-PROVEN, 0 DOCS-ONLY, 2 UNVERIFIED (wing batch/single-retry RESPONSE shapes; parser prod-proven by v1.1) -> MUST CONFIRM. Brad, verbatim (2026-09-14): "Lets go ahead and run another review over all orders we might send. Just double check weve crossed our Ts, dotted our Is" then "If you think its ready, go ahead and arm it. After these fixes of course". Claude's re-arm decision after the audit: bar met; mode file -> `armed` after main 8f4ba3e is pulled into the live tree (23:00Z window), first re-armed window = 23:40Z wake, close 2026-09-15T00:00:00Z, 1 contract, E=0.10. Added to MUST CONFIRM: first cancel returns 200 with the shard; the venue's resting list never shows more than one of ours.
- 2026-09-15 ~13:30Z -- MEASUREMENT CLARIFICATION (Brad, verbatim: "Yea, I agree. Lets do option 2."). Context: after the 2026-09-15 05:30Z Windows Update reboot cost 7 windows (06:00Z..12:00Z) and the 09-14 day ran only 3 armed windows (two broken by wire bugs PR #43/#46), Brad asked whether a one-legged bug window should count against the falsifier; his chosen fix (option 2) was to clarify the measurement BEFORE the verdict, not to change any threshold. Two edits, both authorised by the verbatim go above:
  - (a) "armed evaluation days" in the fill-rate gate (the `>= 2.0 sets/day` [pin] threshold, unchanged) is defined as (number of armed windows the pilot actually ran, i.e. ledger rows with `effective_mode` = `armed`, whatever their stand-down reason) / 24. An hour the box was dark (reboot, proxy down, task not started) leaves NO ledger row and so contributes 0 windows -- it costs a 24th of a day, not an entire day and not nothing; a calendar day on which only 3 windows armed counts as 3/24 of a day, not a full day. This is a MEASUREMENT DEFINITION, not a threshold change: the `>= 2.0 sets/day` bar, the `n >= 30` count, and every other [pin] are untouched. It is registered here at n=1 (well before n reaches 30), so the registered-specs rule (no re-spec on the same evaluation window) is honoured -- the denominator is pinned before the window it will judge closes. Mirrored in code: `service.v32.report.build_falsifier_scoreboard` now reports `armed_windows` and `armed_days = armed_windows / 24` and computes `fill_rate = fills_total / armed_days`; the gate comparison against `V32_FALSIFIER_MIN_FILL_RATE_PER_DAY` is unchanged.
  - (b) WORDING FIX to MUST CONFIRM item 1 above: it previously read that the GTC rest "auto-expires at T-5". Corrected in place -- the executor's quote-end cancel at T-5 is the primary path that removes the rest; the venue's own `expiration_time` is set to T-4 (`EXPIRATION_GRACE_S` = 60, PR #50) as a crash backstop, and a rest that reaches the by-design T-4 venue expiry is NOT a failure of that confirm item. No threshold, pin, or the STATUS line is touched by either edit; only this Registration entry, MUST CONFIRM item 1's wording, and the report's measurement code change.
- 2026-09-15 ~18:10Z -- MEASUREMENT CLARIFICATION 2 (shadow window; Brad, verbatim: "Yea, I agree."). The shadow (the ideal no-lag rule, EVERY E in {0.08, 0.10, 0.12}) records a fill ONLY on prints inside the live quoting window T-`quote_start_s`..T-`quote_end_s` (T-15..T-5), evaluated on the SAME eval clock the live path uses (`t_to_close = close_epoch - trade.server_ts`). A qualifying print OUTSIDE that window is journaled as `shadow_fill_outside_window` and counted in the ledger (`shadow_fills_outside_window`) but never fills the shadow -- rationale: the shadow is the no-lag counterfactual of a LIVE fill, and the live path cannot quote (bucket books connect ~T-20; the rest is cancelled at T-5) and so could never have taken such a print. This is a MEASUREMENT DEFINITION, not a threshold change: no [pin], the `n >= 30` count, or the STATUS line is touched. Registered at n=2 live sets (well before the n>=30 verdict), so the registered-specs rule (no re-spec on the same evaluation window) is honoured. Evidence: the 2026-09-15 04:00Z window's shadow fill was the 1-lot 0.90 print at 03:55:51Z (T-4:09, `t_to_close` 248.8 s, past T-5) -- corrected 2026-09-15 shadow count 4 (was 5), live 2. Mirrored in code: `service.v32.core._shadow_on_trade` window gate + `SHADOW_FILL_OUTSIDE_WINDOW` action, journaled/counted by `run_v32`, surfaced by `service.v32.ledger` and `service.v32.report`. PR #54.

- 2026-09-18 ~18:30Z -- MECHANICS + MEASUREMENT CLARIFICATION (partial fills / sizing step). Brad, verbatim (2026-09-18 ~18:30Z): "We should only send orders for the wings on the singal that our maker order filled, for the size it filled at. So if max sizing is 10, a taker fills 8, then we open 8 wings and leave the 2 unfilled. Make sense? Hopefully another taker comes and fills the remainder. Id like to play around with different levels, but thats a problem at a different sizing". Context: live params `contracts` = 1 and the prints that fill our resting bucket-NO are mostly 1 lot, so raising `contracts` above 1 makes most fills PARTIAL; a probe against the pre-partial core showed a 1-of-2 fill ORPHANED the still-resting second lot (no wings, no cancel) -- an unhedged, unbooked contract. This entry registers the mechanics and the measurement BEFORE any size change (registered at n=6 live sets, well before the n>=30 verdict, so the registered-specs rule -- no re-spec on the same evaluation window -- is honoured). It is a MECHANICS + MEASUREMENT DEFINITION, not a threshold change: NO `[pin]`, no threshold, the `n >= 30` count, the params sha, or the STATUS line is touched.
  - MECHANICS: on EACH rest fill EVENT, take BOTH pin wings as a taker for EXACTLY the count that filled (sized to the fill), each fill event getting its own wing batch (its own two leg client_order_ids, tracked/retried/completed independently, ruling F-2's unconditional initial take and lock-floored single-leg retry applying per batch). The still-resting REMAINDER stays on the book and keeps being requoted under the normal tol/deb rule at the reduced count (cancel->confirm->create, and, when the amend path lands, amend, both with count = remaining); a later fill of the remainder spawns its own wing batch. The hour's ALLOTMENT is `contracts` lots; quoting stops only when the whole allotment has filled (the "one allotment per hour" reading of `max_sets_per_hour` = 1). `filled_count_before_cancel` and the status-poll `fill_count_fp` are CUMULATIVE per order, so the core books only the DELTA over lots already booked for that order (a ws Fill lot then a cancel poll reporting filled 2 books exactly one more). Ladders/levels ("play around with different levels") are explicitly OUT of scope here -- a later problem at a larger sizing.
  - MEASUREMENT: a COMPLETED SET for the falsifier scoreboard = one rest-fill EVENT (a wing batch) whose BOTH wings filled, regardless of the fill count; the realized lock is reported PER CONTRACT (`lock_value` is already per contract) so per-set stats stay comparable to the size-1 history; the live FILL RATE (the `>= 2.0 sets/day` [pin] gate, unchanged) counts SET EVENTS per armed evaluation day (armed_windows / 24, per the 2026-09-15 clarification). A one-legged SET is a rest-fill event flagged `one_legged` at the T-1 s cutoff. Over an existing ledger (rows with no per-set list) the scoreboard is byte-identical to the pre-partial report (verified: `python -m service.v32.report` diff empty at `contracts` = 1).
  - LEVER: `params.contracts` remains BRAD'S lever and stays 1 here. Raising it is an amendment: the params sha `[pin]` gets its OWN dated Registration entry (a new pinned sha the loader self-verifies) when Brad sets it to 2, alongside the promotion conditions already registered (ALIVE at n >= 60 AND thinner-wing depth >= 10 lots). Mirrored in code: `service.v32.core` (per-fill wing batches + resting remainder + cumulative->delta booking), `service.v32.ledger`/`service.v32.report` (per-set `wing_batch_sets`, per-contract lock, set-event fill rate), asserted in `tests/test_v32_falsifier_pins.py` and `tests/test_v32_partial_fill.py`.

## Pre-registered shadow observations (observational; change NOTHING above this line)

### SO-1 -- edge-ladder shadow (E = 0.08 and E = 0.12), registered with this draft
The in-process shadow re-solves and scores E in {0.08, 0.10, 0.12} every tick on the live tape (inside the quoting window T-15..T-5; see Registration 2026-09-15 ~18:10Z). At each
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
