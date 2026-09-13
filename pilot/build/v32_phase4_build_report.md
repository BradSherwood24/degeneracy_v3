# V3.2 Phase 4 build report -- ceremony + report (`v32/phase4-ceremony`)

Branch `v32/phase4-ceremony` (base origin/main `ea601e7` = Phases 1-3 merged). Builder: Opus 4.8.
Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`. House law observed: no `.env`/`*.pem`,
`sim/out/sealed_eval`, `pilot/journals`, `pilot/ledger`, or 2026-08-20..29 date read by any code path or
test I ran; `python` only; the live proxy (127.0.0.1:8642) and Kalshi were NEVER dialed. No PR opened,
`main` never pushed -- committed on the branch and stopped.

## Deliverables (all paths absolute under the worktree)

* `pilot/ceremony/v32_falsifier.md` -- the falsifier DRAFT (deliverable A). `STATUS: DRAFT`; Brad alone
  flips it to `STATUS: FROZEN` (the S5 gate `service.run_v32` passes `--falsifier` default
  `pilot/ceremony/v32_falsifier.md` to `v32_arming_check` -> `service.stops.falsifier_is_frozen`, which
  requires that exact line -- verified in code and by a test).
* `pilot/ops/V32_ARMING.md` -- the arming runbook (deliverable B), mirroring `BOX_ARMING.md`: SS A arm,
  SS B stand-down (incl. the hand cancel via `DELETE .../portfolio/events/orders/<id>`), SS C the
  SEPARATE v32 day-guard latch + manual repair, SS D the MUST-CONFIRM list -> where each shows up, SS E
  budget math.
* `pilot/service/v32/falsifier_pins.py` -- NEW constants module: every verdict/retirement/promotion
  `[pin]` threshold as a named constant, imported by `report.py` and asserted against the doc by a test.
* `pilot/service/v32/report.py` -- added the FALSIFIER SCOREBOARD section (deliverable D).
* `pilot/service/v32/ledger.py` + `pilot/service/v32/stops.py` + `pilot/service/run_v32.py` -- the S4
  floor-netting RULING (deliverable C).
* `pilot/PLAN_V32.md` -- appended "Status 2026-09-13" (deliverable E).
* `pilot/build/v32_phase3_review.md` -- the S4 floor-netting open item updated to RESOLVED BY RULING.
* Tests: `pilot/tests/test_v32_falsifier_pins.py` (5), `pilot/tests/test_v32_report_scoreboard.py` (8),
  band tests added to `pilot/tests/test_v32_phase3_ledger.py` (+2 net) and `pilot/tests/test_v32_stops.py`
  (+5).

## A. Falsifier (DRAFT)

Judges the CONTINUOUS-REQUOTE spot-bucket pump-fader at E=0.10, tol 0.02, deb 5000 ms, 1 contract, on
live fills, against the in-process shadow (E in {0.08, 0.10, 0.12}). Sim basis recorded verbatim
(forward 2026-08-30..09-04, 139 h, lagging-quote model: 21 fills, 3.6/day, mean lock +9.2c, p10 +5.5c,
min +3.4c, 100% positive, +33.5c/day; ideal E=0.10 21 fills +9.3c 95% positive). Structure mirrors
`box_falsifier.md`. The S5 path is verified: `run_v32.DEFAULT_FALSIFIER_PATH` = this file;
`v32_arming_check` -> `falsifier_is_frozen` requires the exact `STATUS: FROZEN` line.

**Pinned thresholds** (each `[pin]` in the doc, each a named constant in `service.v32.falsifier_pins`,
each asserted to agree by `test_v32_falsifier_pins.py`):

| gate | pin | constant |
|------|-----|----------|
| verdict at n | >= 30 completed sets | `V32_FALSIFIER_MIN_N` |
| mean realized lock | >= +4.0c | `V32_FALSIFIER_MIN_MEAN_LOCK_CENTS` |
| % positive | >= 80% | `V32_FALSIFIER_MIN_PCT_POSITIVE` |
| live fill rate | >= 2.0 sets/day | `V32_FALSIFIER_MIN_FILL_RATE_PER_DAY` |
| execution gap (shadow E=0.10 - live) | <= 3.0c | `V32_FALSIFIER_MAX_EXEC_GAP_CENTS` |
| one-legged sets | <= 2 of 30 | `V32_FALSIFIER_MAX_ONE_LEGGED` |
| R2 economics | 3 consecutive neg-realized days | `V32_R2_CONSECUTIVE_NEG_DAYS` |
| R3 legging | 2 S1_LEGGED latches / 7-day window | `V32_R3_LEGGED_LATCHES_PER_WEEK` |
| R4 early exec kill | gap > 5.0c at n >= 15 | `V32_R4_EXEC_GAP_CENTS`, `V32_R4_MIN_N` |
| promotion to 2 contracts | n >= 60 alive + >= 10 lots depth | `V32_PROMOTION_MIN_N`, `..._DEPTH_LOTS` |

Any threshold missed at n >= 30 = KILL, no re-spec on the same evaluation window. Power note recorded:
SE of the mean lock at sd ~5c, n=30 is ~0.9c (the +4.0c bar sits ~4.4 SE above 0). Alarms
(A_REPLACE / A_STALE / A_EXEC_PRICE / A_REJECT x3) and stops (S4 $3.00 banded, S5 arming, S1_LEGGED 2/day
latch, reconcile-first) documented exactly as coded. FIRST ARMED WINDOW MUST CONFIRM list copied verbatim
from the Phase 3 review. Registration empty (append-only). SO-1 (E=0.08/0.12 shadow), SO-2 (replaces/h vs
77), SO-3 (data-age p99 per connection) pre-registered.

## C. S4 floor-netting RULING (the one code change of substance)

The Phase 3 review's open item: `v32_pending_credit` netted only the optimistic upside, so an
unsettled-but-guaranteed pin's cash dip was not credited into the pessimistic bound -- biasing S4 toward a
spurious stand-down when two pins are unsettled at :40. RULING (coordinator), applied:

* `service/v32/ledger.py::v32_pending_credit` now returns a `(pessimistic, optimistic)` BAND. Per
  unsettled set this UTC day: pessimistic = guaranteed floor (`v32_set_floor_dollars`: 3 legs -> $2.00,
  2 legs -> $1.00, lone leg -> $0.00); optimistic = best-case payoff `min(#legs, 2)` (-> $2.00 / $2.00 /
  $1.00). So upside = 0 / $1.00 / $1.00, exactly the ruling.
* `service/v32/stops.py::v32_s4_decision` now consumes the band, netting the guaranteed floor into BOTH
  bounds by folding it into `balance_now` and passing only the upside to the pilot's
  `s4_balance_decision`: `loss_pessimistic = start - (now + pess)`, `loss_optimistic = start - (now +
  opt)`. **latch** iff loss_optimistic >= cap; **clear** iff loss_pessimistic < cap; else **pending**.
* `service/v32/stops.py::decide_v32_arming` now stands the window down (degrade to dry) on `pending` as
  well as `latch`, and writes NO day-guard latch for a `pending` (the next wake re-evaluates on the fresh
  balance) -- matching the ruling's "pending -> stand down without latch".
* `service/run_v32.py` call site unchanged in shape (`pending = v32_pending_credit(...)` is now a tuple,
  passed straight to `v32_s4_decision`).

Tests (all three leg counts + the band logic + the S4 decision + the pending arming path): see the suite
list below.

## D. Falsifier scoreboard (report.py)

`build_falsifier_scoreboard(rows)` folds the ledger's armed rows into: n completed sets (realized_lock
present), rest-fills total, one-legged count, armed days; realized lock mean/median/p10/min (cents,
nearest-rank percentiles); % positive; fill rate/day; shadow E=0.10 mean lock and the execution gap
(shadow - live, cents); replaces/hour mean; data-age p99 per connection (strike, bucket); and the
`verdict` line (`ALIVE-so-far` / `KILL: <which>` / `n<30 pending`) computed ONLY from the
`falsifier_pins` constants. Rendered under the totals block by `python -m service.v32.report`, and
included in `--json`. Dry rows (armed False) never count. Tested with a synthetic ledger
(`test_v32_report_scoreboard.py`): pending-below-n, the stat/percentile/gap math, ALIVE when all pins
pass, KILL on low mean lock / on exec gap / on too many one-legged, p99 per connection, and dry-row
exclusion.

## Suite

`cd pilot && python -m pytest -q` -> **787 passed** (767 baseline from Phase 3 + 20 net new:
+2 ledger band, +5 stops S4 band/pending, +8 scoreboard, +5 falsifier-pins agreement).

## What Brad must rule on / do (nothing an agent may do)

* Freeze `ceremony/v32_falsifier.md` (STATUS: FROZEN) on his verbatim go -- and confirm the proposed
  verdict thresholds are the ones he wants pinned (mean +4.0c, %pos 80, rate 2.0/day, gap 3.0c, legged
  2/30; R2 3 days, R3 2/7, R4 5c@15; promotion 60 + 10 lots).
* The proxy levers (`.env` prefixes + budget, restart), the two dry windows, the merge of Phase 4, and
  the mode flip -- all per `ops/V32_ARMING.md`.
