# V3.2 Phase 4 review -- ceremony + report (`v32/phase4-ceremony`, e6053a4 on base ea601e7)

Reviewer: Opus 4.8 (Fable-delegated). Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`. House law
observed: no `.env`/`*.pem`, `sim/out/sealed_eval`, `pilot/journals`, `pilot/ledger`, or 2026-08-20..29
date read by any code path or test; `python` only; the live proxy and Kalshi were NEVER dialed; no PR
opened, `main` never pushed.

## VERDICT: APPROVE (two minor defects found and fixed; suite green)

The S4 floor-netting RULING is implemented exactly as specified, the pin/doc agreement is genuine, the
scoreboard verdict is correct, and the runbook is mechanically sound (hand-cancel path and sha one-liner
verified against the code). Two minor defects fixed: an ambiguous `lock_floor` phrasing in the falsifier
and a two-branch coverage gap in the scoreboard tests. One non-blocking observation on S4 latch
persistence recorded below.

## Probe findings

### 1. S4 band math -- CORRECT
`service/v32/stops.py::v32_s4_decision` folds the guaranteed floor F into `balance_now` for BOTH bounds
and passes only the upside `U = opt_credit - pess_credit` as the pilot primitive's optimistic pending
value. Inside `service/stops.py::s4_balance_decision` this yields, exactly as the probe posits:
`loss_pessimistic = B0 - (B1 + F)`, `loss_optimistic = B0 - (B1 + F + U)`; latch iff
`loss_optimistic >= cap`, clear iff `loss_pessimistic < cap`, else pending. Worked (B0=100, cap=3.00):
complete pin (F=2,U=0) always clears (its cash dip is credited back -- the RULING's point); two-leg
(F=1,U=1); lone leg (F=0,U=1); two unsettled sets sum the band. `decide_v32_arming` stands the window
down (degrade to dry) on `pending` and writes NO day-guard latch (`stops.py:279-286`) -- matches the
RULING and `s4_balance_decision` semantics. Double-count avoided: `v32_pending_credit`
(`ledger.py`) skips rows whose `close_time` is in the backfill `done` set and prior-day rows, and
`run_v32.py` runs the settlement-backfill sweep (:1316) BEFORE recomputing `pending_credit` from the
reloaded rows (:1403) each wake, so a set that settled earlier the same day is credited via its real
backfilled balance, not a second time via the floor.

### 2. Pin / code agreement -- CORRECT
Every `[pin]` in the falsifier maps to a named constant in `service.v32.falsifier_pins` (verdict /
retirement / promotion) or `service.v32.stops` (day stops). `tests/test_v32_falsifier_pins.py` parses
the doc and asserts each constant's value appears via f-string substitution (genuine agreement, not a
hard-coded mirror). Confirmed present in the doc: the 15 core params (match `policy/v32_params.json`),
executor reject count 3 (`executor.CONSECUTIVE_REJECT_STANDDOWN`), budget floor 200, S1_LEGGED latch 2,
S4 cap $3.00, max-contracts 2, and the roster sha.

### 3. Falsifier logic -- CORRECT
Thresholds sit conservatively below the quoted sim basis (fill 2.0 vs 3.6/day; mean +4.0c vs +9.2c;
%pos 80 vs 100; exec gap 3.0c vs measured ~0.1c). Verdict defined only at `n >= 30` (R4 is the sole
earlier gate, an explicit n>=15 execution-gap early kill). "No re-spec on the same evaluation window" is
stated (R1 + verdict section). The doc carries `STATUS: DRAFT` and is not frozen
(`test_falsifier_is_draft_not_frozen`). S5 checks `STATUS: FROZEN` at `ceremony/v32_falsifier.md`
(`run_v32.DEFAULT_FALSIFIER_PATH` -> `v32_arming_check` -> `service.stops.falsifier_is_frozen`).

### 4. Scoreboard -- CORRECT (coverage gap closed)
Verdict correct at n<30 / alive / kill. Execution gap sign = `shadow - live`
(`report.py`: `slock*100 - rlock*100`), matching the doc; a positive gap (venue ate the edge) kills.
`replaces_per_hour_mean` is the per-window replace count mean, consistent with the sim's 77-per-10-min-
window convention (one window per hour). FIX APPLIED: the builder tested only 3 of the 5 kill branches
(low mean lock, exec gap, one-legged); added `test_scoreboard_kill_on_low_fill_rate` and
`test_scoreboard_kill_on_low_pct_positive` to `tests/test_v32_report_scoreboard.py`, each isolating its
gate (all other pins pass).

### 5. Runbook (`ops/V32_ARMING.md`) -- CORRECT
ASCII throughout; PowerShell 5.1 valid. The hand-cancel `DELETE .../trade-api/v2/portfolio/events/
orders/<id>` matches the executor's `CANCEL_PATH_TMPL = "/portfolio/events/orders/{order_id}"` under
`ProxyWriter.rest_delete` (which prepends `/trade-api/v2`) -- the executor switched off the deprecated
`/portfolio/orders/{id}` (HTTP 410 since 2026-07-12); the stale PLAN Phase-3 text is superseded, the
runbook is right. The sha one-liner reproduces the pinned
`c6715fc7...bdfb92` exactly (verified by running it). Lever ordering (dry proof -> freeze -> mode flip)
matches `V32_DRY_RUN.md` and the falsifier checklist. Brad-only levers (`.env` prefixes/budget, freeze,
mode flip, task registration) are explicitly attributed to Brad; nothing instructs an agent to perform
one.

### 6. Doc / code contradictions -- ONE FOUND AND FIXED
`ceremony/v32_falsifier.md:122` read "`lock_floor` - 0.10 = [pin] -0.10", which parses as -0.20 by the
hyphen; the core gates at `lock_floor` itself (`core.py:634` `if projected_lock < params.lock_floor:`,
`lock_floor = -0.10`). Changed to "`lock_floor` = [pin] -0.10". All other checks consistent: deb_ms
5000, expiration at quote_end (T-5), one-set-per-hour, bucket_width 100.

## Non-blocking observation (not fixed -- design is sound)

S4 `latch` is not persisted to the day-guard file (`run_v32` records only S1_LEGGED occurrences via
`record_legged_occurrence`); the day-halt for a real S4 loss relies on re-computing the balance decision
each wake. This is functionally equivalent to a latch because `loss_optimistic` is monotonic
non-decreasing across settlements (a pending set finalizing pays <= its optimistic credit, so once the
best case still breaches the cap it stays breached), and the process is degraded to dry so no new
positions can recover the balance. The `S4_DAY_LOSS` branch in `v32_latched_stop_kind` is therefore
effectively dead but harmless. `V32_ARMING.md` section C's "S4 latches on a real balance loss (banded)"
slightly implies a persisted guard entry; a one-line clarification (that S4 halts by re-evaluation, not
a guard row) would remove the ambiguity. Left for Brad's call -- out of clear-defect scope.

## Suite

`cd pilot && python -m pytest -q` -> **789 passed** (787 as built + 2 new scoreboard kill-branch tests).
