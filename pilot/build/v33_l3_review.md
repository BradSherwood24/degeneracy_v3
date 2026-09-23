# V3.3 Phase L3 review — report + gate table + shadow/SO-3 + falsifier DRAFT + pre-arm hardening

Reviewer: Opus 4.8 (Brad's mandate). PR #88, branch `feat/v33-l3-report-ceremony`, head 68c2657, base
origin/main 06e20f7. Reviewed in worktree `dv3_wt_review` (branch `review/v33-l3`). No PR code modified.
No order path exercised; no sealed/holdout read; proxy not dialed. `python` only.

## Verdicts

- **(A) Merge for the DRY side-by-side deploy: APPROVE WITH NITS.** The mechanics are sound, DRY sends no
  money, the falsifier is a DRAFT that S5 refuses to arm on (verified by running it), the report keeps
  dry strictly separate from realised, and the L2 MUST-DO-BEFORE-ARM items are closed and tested. The
  findings below are report-/doc-only (no economic path). None blocks a dry deploy.
- **(B) Arm-readiness: NOT YET — list at the end.** One report divergence (F1) should be resolved (or
  explicitly accepted by Brad in writing) BEFORE the falsifier is frozen, because the gate-table verdict
  is what Brad reads to decide the freeze.

## What I verified green (receipts)

- **Protected files 0-diff:** `git diff origin/main...HEAD` touches 17 files; `service/v33/core.py`,
  `service/v32/*`, `run_v32.py`, `ceremony/v32_falsifier.md`, `ops/v32_mode.txt`, `PLAN_V33.md` are all
  **0 changed lines** (confirmed per-file and as a group).
- **Suite:** `cd pilot && python -m pytest -q` → **1245 passed, 5 skipped** (13.3s). Builder reported
  1249/1; the 4-test delta is corpus-`skipif` (no `historical-data/` in this worktree) — total 1250 both
  ways. New files alone: 82 passed.
- **Falsifier three-way agreement:** `test_v33_falsifier_pins.py` asserts doc text ↔ `falsifier_pins.py`
  constants; `report.py` imports the SAME constants; params sha in the doc == `FROZEN_V33_PARAMS_SHA256`
  == `load_v33_params().sha256` = `c18197d0…39f36f3` (ran it). Every §6 gate/kill/threshold/n matches
  PLAN §6 verbatim in substance (mean ≥ +6.0c; per-rung shortfall ≤ 3.0c at ≥3 fills; %pos ≥ 80;
  capture @10c ≥ 0.50; one-legged ≤ 2; roll integrity ≥ 90%; kill mean < +2.0c at n ≥ 15; one-legged > 2;
  promotion n ≥ 30 + Brad's word). STATUS line is exactly `STATUS: DRAFT -- NOT FROZEN` (line 3).
- **Freeze is Brad-only, no code back-door:** `falsifier_is_frozen()` returns False on the DRAFT (ran it);
  `decide_v33_arming(resolved_mode="armed", …, params_verified=True, perfect /health, clean guard)` →
  `armed=False`, reason "falsifier STATUS line is not exactly 'STATUS: FROZEN'" (ran it). The only thing
  that flips it is the md STATUS line the pins test guards; no code path arms without it.
- **Scoreboard math** (read + adversarial probes): dry is NEVER pooled into realised (dry-only rows give
  gate `n_rung_fills=0`; scoreboard splits `_is_realised` vs `_is_dry`); per-margin `mean_solved` uses
  `lock_solved` (= `lock_value(price, W_at_fill)`), not the integer label — the label only buckets the
  row; shortfall sign is `solved − realised` (worst = max, gate ≤ 3.0c); n counts CONTRACTS (`sum(count)`,
  a full sweep contributes K=11); kill fires at n ≥ 15; backfill rows excluded (`_is_window_row`).
- **Capture @10c == Registration-3 semantics:** per-window 0/1 (denominator = armed+bucket realised
  windows with a valid E=0.10 shadow fill; numerator = those that also have a completed 10c-rung fill),
  with the identical below-`n_min`-shadow exclusion (`_shadow_below_min`, offer-derived n). Probes:
  1/2 = 0.5 with a shadow-only window; 0/None when the only shadow fill is below n_min.
- **Shadow ≡ dry_sim:** `test_shadow_equals_dry_sim_on_the_golden_prints` is **print-by-print** over the
  2026-09-20T04:00Z golden tape — for each print it asserts the driver's dry_sim fill count equals
  `len(dry_sim_equivalence_rungs(prices_before, yp))` and the full sweep fills all 11 rungs.
- **SO-3 observation-only in every mode:** `DeepObservationLadder` has NO writer/order path; `_observe_deep`
  is called in `on_trade` unconditionally (every mode), gated to the ladder's spot bucket and T-15..T-5,
  and only folds data; margins are 16..25 (`deep_obs_rungs`=10); recorded per row (`deep_obs`), aggregated
  in the report's DEEP END block.
- **Hardening (a–f) implemented + non-hollow tests:** reserve holds headroom for priority
  (`test_pacer_*`); 429 retried once + `rate_limited`, business-4xx passes through, a persistent 429
  never increments `_consecutive_rejects`/latches (`test_place_persistent_429_does_not_count_toward_standdown`);
  armed executor with unknown cap RAISES and `main` degrades to dry (belt in two places, wing_cap no
  longer defaults to the hint); weighted-average wing price blends original+retry chunks (0.55×2+0.57×2)/4
  = 0.56; batched poll covers B1 AND B2; stray-only cancels+proceeds, stray+overflow hard-stands-down.

## Findings

**F1 — NIT (verdict A) / SHOULD-FIX-BEFORE-FREEZE (verdict B). The deliberate divergence: capture=None at
n≥30 is treated as `n-too-small` (fail-OPEN), not FAIL.**
`pilot/service/v33/report.py:335-337` (`add()` sets status `n-too-small` when `value is None`), the
capture gate at `:348-349` passes `n_ok = cap_den > 0`, and the verdict at `:365-369` counts only
`status == "FAIL"` gates. So at n ≥ 30 with **zero** valid E=0.10 shadow fills across all armed+bucket
windows, the capture gate is `n-too-small` and the verdict reads **`ALIVE-so-far`** (reproduced live:
n=33, capture `n-too-small`, verdict `ALIVE-so-far`; and `test_gate_capture_n_too_small_when_no_shadow`
encodes exactly this). V3.2 does the opposite — `pilot/service/v32/report.py` (verdict block): `if
capture_ratio is None or capture_ratio < V32_CAPTURE_RATIO_MIN: fails.append(...)` — "a missing ratio …
is a fail — there is availability we cannot show we captured."
*Recommendation: match V3.2, fail-closed.* Arguments: (1) The doc's own ALIVE clause is "ALIVE iff ALL
of … capture ratio @ 10c ≥ 0.50" — you cannot assert ALL passed when one conjunct is unmeasured, so
`ALIVE-so-far` contradicts the registered definition. (2) House law leans fail-closed and this is exactly
the V1/V2 "green when unproven" mirage. (3) The shadow is byte-forked from V3.2 and writes a record every
window, so `cap_den==0` at n≥30 is essentially only reachable via a shadow-recording bug — precisely the
case where you want the verdict to go red and surface it, not green. This is report-only (it does not gate
arming — S5 does), so it does not block the dry merge; but it feeds Brad's freeze decision, so resolve it
(or get Brad's written acceptance of the divergence) before the freeze. A middle option that also
satisfies house law: emit a distinct non-ALIVE verdict ("capture unmeasurable — not ALIVE") rather than
`ALIVE-so-far`.

**F2 — NIT. The dry_sim fill predicate is duplicated, not the shared `ideal_rung_crosses`.**
`pilot/service/run_v33.py:338` inlines `trade.yes_price >= (Decimal(1) - o.price)`, while
`pilot/service/v33/shadow.py:ideal_rung_crosses` uses `yes_price + _EPS >= (1 - rung_price)` and its
docstring claims it "is the SINGLE source of truth for both the live-margin shadow (dry_sim) and the SO-3
deep ladder, so the two cannot drift." On cent-quantised Decimal prices the two are identical (the 1e-9
epsilon can only matter below a cent, which cent prints never hit), so there is **no live divergence** and
the equivalence test guards the count — but the "cannot drift" claim is overstated and a future edit to
one predicate at the boundary would not be caught by cent-only fixtures. Recommend `_simulate_ladder_fills`
call `ideal_rung_crosses` so the claim is literally true.

**F3 — NIT. The pacer reserve deadlock guard is cost-unaware.**
`pilot/service/v33/params.py` rejects `write_reserve_tokens >= write_bucket_size`, but a non-priority
write needs `cost + reserve ≤ size` (cost = COST_CREATE/COST_AMEND = 10). A reserve of, say, 95 with
size 100 passes validation yet a create (needs 105) could never proceed. Harmless at the pinned
reserve=30/size=100/cost=10, but the guard should be `reserve + max_non_priority_cost ≤ size`.

**F4 — NIT (cosmetic). Runbook section numbering skips 9.**
`pilot/ops/V33_RUNBOOK.md` now runs `## 8 … ## 10 … ## 11 … ## 12` — the old `## 9` header was replaced
without renumbering, so there is no section 9.

**F5 — QUESTION. Sustained 429 has no stand-down / soft cap.**
A persistent 429 (writer retries once, still 429) is exempted from the consecutive-reject stand-down
(correct — it executed nothing) and the rung re-solves + retries every tick. Under a sustained throttle
this loops indefinitely; no naked exposure results (nothing places; wings are priority-paced separately),
but there is no alarm/soft-cap surfacing "we are throttled and not placing." Acceptable given the write
pacer is designed to avoid 429s — flagging for Brad's call, not blocking.

**F6 — QUESTION / suggested test before arm. Stray-cancel trusts RestBook attribution.**
`_pre_place_invariant` classifies any resting `v33-*` order it cannot attribute via `attribute(order_id,
coid)` as a stray and (when no overflow/dup) cancels it and proceeds. If an amend rotates coid/order_id
and the venue momentarily lists the pre-amend order under an id not yet in the RestBook, it could be
classified a stray and cancelled. Mitigations exist (the 0.5s recheck + re-GET only run on a flagged
anomaly; `_filter_phantoms`; and cancelling a lost-track order then re-solving is a safe recovery), and
DRY never places — but there is no test for the amend-in-flight-vs-stray race. Suggest a targeted test
before the first armed windows. The "otherwise matches" test (checklist 6f) is `not overflow and not dup`
= (count ≤ K) AND (place price not duplicated); it is an upper-bound/price check, not a full set-equality
against the expected ladder, which is adequate for the stray-cancel decision (removing a not-ours order
while under K is safe) but is the reason F6's race is worth a test.

## (B) Arm-readiness list — what must still be true before Brad flips V3.3 to armed

1. **Falsifier FROZEN by Brad** — his verbatim go, STATUS line → exactly `STATUS: FROZEN`, Registration
   appended with the roster sha. Currently DRAFT; S5 refuses (verified). An agent never flips it.
2. **F1 resolved (or Brad's written acceptance)** — match V3.2 fail-closed on a None capture at n≥30, so
   the gate-table verdict Brad reads before freezing cannot show `ALIVE-so-far` on an unmeasured gate.
3. **Proxy levers applied by Brad + `/health` confirms**: amend cap (`ops/proxy_amend_cap.md`);
   `DAILY_ORDER_BUDGET` → 8000; `MAX_CONTRACTS_PER_ORDER` = 2 or 11 (both arm; 11 lifts the one-lot guard
   — weigh it); prefixes cover `KXBTC-` and `KXBTCD-`; `orders_enabled: true`; `orders_remaining_today`
   ≥ 500.
4. **Task registered** via `register_supervisor_tasks.ps1 -WithV33` and running DRY.
5. **≥ 2 dry days reviewed** with the side-by-side ("running exactly as expected"), the DRY scoreboard's
   per-rung locks and ~100% single-order-roll ratio sane, DEEP END populating, and a spot-check that the
   dry journals sent nothing (`would_*` / `dry_sim_fill` only) with `/health orders_remaining_today`
   unchanged across a window.
6. **Green suite + params sha** = `c18197d0…39f36f3`.
7. **First armed windows: the MUST CONFIRM list** (K orders accepted, one-order rolls, coalesced/chunked
   wings sized to fills, hand-reconcile the first full sweep vs `/portfolio/fills`, pacer never 429'd,
   per-rung bucket correct across a change).
8. *Suggested (not strictly blocking):* the F6 amend-in-flight-vs-stray test; decide the F5 sustained-429
   policy.

## Questions for Brad
- F1: accept the capture-None divergence from V3.2, or have the builder match V3.2 (fail-closed) before
  the freeze? (Reviewer leans match V3.2.)
- F5: is unbounded per-tick retry under a sustained 429 acceptable, or do you want a soft alarm/cap?
