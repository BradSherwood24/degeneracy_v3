# V3.3 Phase L3 build report — report + shadow (SO-3) + ceremony + pre-arm hardening

Builder: Opus 4.8 (Brad's mandate). Branch `feat/v33-l3-report-ceremony` off `origin/main` 06e20f7 (Phase
H + L1 + L2 merged). Date 2026-09-23. Scope: the last build phase before V3.3 runs DRY alongside the live
ARMED V3.2. Live V3.2 is UNTOUCHED (its core/params/run_v32/executor/ledger/stops/falsifier/mode file are
byte-identical). V3.3 has NO mode file and no registered task yet — it does not run anywhere. `python`
only; no sealed/holdout read; no order path exercised (all tests fake the proxy); no proxy dialed.

## Files delivered (absolute under `C:/Users/Brads/Python_stuff/dv3_wt_v11`)

New:
- `pilot/service/v33/shadow.py` — the observation ladders: `ideal_rung_crosses` /
  `dry_sim_equivalence_rungs` (the ONE ideal fill predicate the dry_sim + SO-3 share), and
  `DeepObservationLadder` (SO-3, 16..25c, observation only).
- `pilot/service/v33/falsifier_pins.py` — the §6 [pin] constants mirrored for the report + the pins test.
- `pilot/ceremony/v33_falsifier.md` — the V3.3 falsifier DRAFT (`STATUS: DRAFT -- NOT FROZEN`).
- `pilot/ops/V33_ARMING.md` — the mechanical arming runbook (prereqs, the flip, MUST CONFIRM, the V3.2 Q4
  close-line TEMPLATE, rollback).
- Tests: `test_v33_falsifier_pins.py` (9), `test_v33_shadow.py` (10), `test_v33_report_scoreboard.py` (20),
  `test_v33_hardening.py` (17).

Modified:
- `pilot/service/v33/report.py` — LADDER SCOREBOARD (per margin, DRY separated from realised, pooled +
  per-day, capture ratio @ 10c), FALSIFIER GATE TABLE (§6, realised only), extended SIDE-BY-SIDE
  (per-day + entered-vs-not lists), DEEP END (SO-3) block. L2's per-window lines + totals + side-by-side
  shape preserved (the L2 report tests stay green).
- `pilot/service/v33/executor.py` — the pre-arm hardening (below).
- `pilot/service/run_v33.py` — the armed-cap belt, the multi-bucket batched poll, the SO-3 wiring, the
  reserve passthrough.
- `pilot/service/v33/ledger.py` — `deep_obs` on the window row (`build_v33_ledger_row`).
- `pilot/service/v33/params.py` + `pilot/policy/v33_params.json` — `write_reserve_tokens` (30) +
  `deep_obs_rungs` (10); sha re-pinned.
- `pilot/ops/V33_RUNBOOK.md` — sec 9 (report + dry-period review checklist + L3 hardening list), sec 10
  (pointer to V33_ARMING.md).
- `pilot/tests/test_v33_executor.py` — the stray-handling test updated to the new decision (+1 test).

**`service/v33/core.py` was NOT modified** (the L1 ladder core stays frozen; L3 uses it read-only).

## Test counts
- Before (full suite): **1192 passed, 1 skipped**.
- After: **1249 passed, 1 skipped** (+57). New tests: pins 9, shadow 10, report scoreboard 20, hardening
  17, executor stray split +1. L1 core + golden + params: **87 green** (unchanged). L2 executor/ledger/
  run/report/paths/supervisor: green. `check_invariants` still runs after every core event.

## Report (deliverable 1)
`python -m service.v33.report [--days N] [--json]` (runnable like v32's), smoke-run clean on empty
ledgers. New blocks:
- **LADDER SCOREBOARD**: per margin (5..15c) n contracts, mean solved E (`lock_solved` =
  `lock_value(price, W_at_fill)`, the L1 R4 contract), mean realised lock, shortfall (solved − realised),
  %positive; pooled per-contract stats (contracts, ladder lock, rolls + single-order ratio, creates/
  amends/cancels per window); per-day. **DRY (`dry_sim`) rows are a SEPARATE section, NEVER pooled with
  realised.** The **capture ratio @ 10c** uses the Registration-3 definition (live completed 10c-rung
  sets / valid ideal-shadow E=0.10 fills over armed+bucket windows), V3.2-comparable, with the same
  below-`n_min` shadow exclusion.
- **FALSIFIER GATE TABLE (§6)** computed live from REALISED rows only: each gate → value/threshold/
  PASS/FAIL/n-too-small, the n≥30 rung-fill counter, verdict + kill conditions.
- **SIDE-BY-SIDE** (extends L2): per hour V3.2 vs V3.3, day totals, running totals, AND the windows where
  V3.3 (dry_sim or realised) ENTERED and V3.2 did not, and vice versa.
- **DEEP END (SO-3, observation only)**.
- The V3.2 report is unchanged; v33 report imports v32 `load_v32_rows` read-only for the side-by-side.

## Shadow / SO-3 (deliverable 2)
- **The ideal K-rung shadow at the live margins is NOT rebuilt** — in DRY it IS the `run_v33` driver's
  `dry_sim`, and in ARMED the realised-vs-ideal comparison is the per-rung `lock_solved` vs
  `realized_lock` the ledger already records. `shadow.ideal_rung_crosses` is the single predicate that
  fill rule uses; a test asserts the driver's dry_sim fills EXACTLY the rungs the predicate says
  (`test_shadow_equals_dry_sim_on_the_golden_prints`) — the SHADOW == DRY_SIM equivalence, print by print
  on the 2026-09-20 04:00Z golden fixture.
- **SO-3 deep observation ladder** (`deep_obs_rungs` = 10 → margins 16..25c), observation only in EVERY
  mode, wired in `run_v33.V33Driver._observe_deep` (gated to the spot bucket + T-15..T-5), summarised onto
  the ledger row (`deep_obs`) and aggregated in the report's DEEP END block: per deep rung the windows
  reached, mean ideal lock, and absorption (lots at/through). The existing golden fixture already prints
  to yes 0.98, so the sweep reaches all 10 deep rungs — **no fixture extension was needed** (< 2 MB
  unchanged).

## Ceremony (deliverable 3)
- `ceremony/v33_falsifier.md` DRAFT: `STATUS: DRAFT -- NOT FROZEN`; roster `DegeneracyV3_3`; params sha
  `c18197d012bea8251982e4fdb948bf85846a453a9873fbd8007a7df9639f36f3`; §6 gates and kill; the dry/armed
  side-by-side protocol (Q4 refined); the FIRST ARMED WINDOW MUST CONFIRM list; SO-1 + SO-3 shadow
  observations. Pinned by `falsifier_pins.py` + `test_v33_falsifier_pins.py` (gates asserted against the
  md text; STATUS asserted NOT frozen; `decide_v33_arming` asserted to REFUSE while DRAFT — L2's wiring
  verified).
- `ops/V33_ARMING.md` mirrors V32_ARMING.md: prerequisites (falsifier FROZEN, amend cap,
  `DAILY_ORDER_BUDGET` 8000, `MAX_CONTRACTS_PER_ORDER` 2 or 11 both supported, task registered, ≥2 dry
  days reviewed), the flip (`v33_mode.txt` → armed + `v32_mode.txt` → dry, :02–:33, Brad's hand), the MUST
  CONFIRM list, and rollback.
- The **V3.2 Q4 Registration close line is a TEMPLATE inside V33_ARMING.md section D — NOT appended to
  `v32_falsifier.md`** (appended only at the flip, by the arming step, by Brad). `v32_falsifier.md` is
  UNTOUCHED.

## Pre-arm hardening (deliverable 4)
- **(a) pacer headroom reserve** (`write_reserve_tokens` = 30): `WriteTokenBucket.acquire` makes a
  non-priority create/amend wait until the bucket holds `cost + reserve`, so a priority cancel-all / wing
  burst always finds room; a priority write ignores the reserve. Loader fails closed on
  `reserve >= write_bucket_size`.
- **(b) 429 vs business rejection**: new `_RateLimitWriter` wraps the writer (so every inherited POST path
  gets the belt). A 429 POST — a DEFINITIVE non-execution, safe to re-send the same coid — is retried
  ONCE after `Retry-After` (body) / pacer estimate / `RATE_LIMIT_RETRY_WAIT_S`, journaled `rate_limited`;
  business 4xx / 5xx / timeout pass straight through. `V33LiveExecutor._reject_place` exempts a persisting
  429 from the 3-consecutive-reject stand-down (journals `rate_limited_reject`, no counter bump).
- **(c) `build_executor_v33` armed belt**: raises if `wing_cap` is None; `run_v33` passes `wing_cap=None`
  when the `/health` cap is unreadable and degrades that armed window to dry (never sizes wings against a
  guess). Dry is unaffected.
- **(d) R2-N3 weighted average** across the original + retry wing chunks: `_wing_notional` accumulates
  price·count cumulatively per (batch, side), so a leg completed over two takes reports the true blended
  price (proved: 0.55×2 + 0.57×2 → 0.56).
- **(e) R2-N4 multi-bucket poll**: `_order_status_poll_v33` (batched) polls EVERY bucket with a live rung
  (the current bucket AND any prior-bucket rung still resting after a mid-window bucket change), so a
  prior-bucket fill is never missed.
- **(f) stray handling DECISION**: a lone unattributable `v33-*` stray with venue truth OTHERWISE matching
  (no overflow past K, no dup at the place price) is now CANCELLED + alarmed and the place PROCEEDS (the
  other rungs keep working); an OVERFLOW or DUP (with or without a stray) keeps the HARD whole-window
  stand-down. **Justification**: every `v33-*` coid is minted by our core and recorded on place, so a true
  stray is never a live rung of ours — it is a crashed-process leftover the startup sweep raced, or a
  stale list entry; sacrificing K working rungs for one such anomaly is heavier than cancelling it, while
  an overflow/dup means our accounting genuinely disagrees with the venue (the 21-rest incident class) and
  must stop the hour. The prior L2 test (`stray is violation`) was replaced by two tests encoding the new
  decision.
- Params re-pinned: `FROZEN_V33_PARAMS_SHA256 = c18197d0...` (previous `415b63da...` kept as
  `PREVIOUS_V33_PARAMS_SHA256_L2_R2`). New fail-closed loader checks tested.

## The exact Brad-lever list (nothing here is Claude's to pull)
- Freeze `ceremony/v33_falsifier.md` (`STATUS: FROZEN`) on his verbatim go + append the go under
  Registration with the sha `c18197d0...`.
- Proxy `.env` + restart: `ORDER_TICKER_PREFIXES` incl. `KXBTC`; `DAILY_ORDER_BUDGET` → 8000; the amend
  cap (`ops/proxy_amend_cap.md`); `MAX_CONTRACTS_PER_ORDER` = 2 (wings 6+6 IOC chunks) OR 11 (wings 1+1) —
  both arm.
- Register `DegeneracyV3_3` (`register_supervisor_tasks.ps1 -WithV33`); run it DRY ≥ 2 days.
- The FLIP (:02–:33, his hand): `ops/v33_mode.txt` → armed, `ops/v32_mode.txt` → dry; append V3.2's Q4
  close line (the template) at the flip.
- `DV3_DATA_DIR` / `DV3_PROXY_BASE` (where the roster reads/writes).

## Dry-period review checklist (before the flip; full text in V33_RUNBOOK sec 9)
SIDE-BY-SIDE tracks V3.2 (watch the "V3.3 entered / V3.2 did NOT" set — the shallow-rung windows the
ladder adds; a "V3.2 entered / V3.3 did NOT" window is a red flag); the SCOREBOARD DRY per-rung locks
match the study + single-order-roll ratio ~100%; the DEEP END absorption grows (Promotion evidence); the
dry journal sends nothing (`would_*`/`dry_sim_fill` only) and `/health` budget is unchanged. The GATE
TABLE reads `n<30 pending` / `n-too-small` throughout dry (no realised fills) — expected.

## What remains before the flip (not this phase)
- Brad's proxy levers (amend cap + budget 8000) + the freeze + the task registration + ≥2 dry days.
- The Render move itself (env vars + service creation only), lots per rung > 1, rungs past 15c live,
  the 15M, refills — all out of V3.3 (PLAN_V33 sec 8).

## Open notes for the reviewer
- The capture-ratio gate treats an unmeasurable capture (no valid shadow availability) as `n-too-small`,
  NOT a FAIL (V3.2 failed on a None ratio). At n≥30 rung-fills a sweep to 10c implies the E=0.10 shadow
  fills, so `n-too-small` here is effectively unreachable in practice; flagged as a deliberate divergence
  from V3.2's stricter treatment — happy to fail-on-None instead if preferred.
- SO-3's deep rung price is `n_top - (m - E_min_c)` (the ladder's own geometry off the live `n_top`),
  first-reach anchored; absorption counts every in-window YES print at/through the current deep price.

---

# Round 2 (2026-09-23) — addressing PR #88 review (APPROVE WITH NITS for dry)

All six items closed. Suite after R2: **1253 passed, 1 skipped** (+4 over R1's 1249).

- **F1 (must) — capture None at n>=30 → FAIL (fail-closed).** `build_falsifier_gate_table` now special-cases
  the capture gate: below `n>=MIN_N` it reads `n-too-small`; AT `n>=MIN_N` a None ratio (no valid shadow
  availability to measure execution against) is a **FAIL**, matching V3.2's verdict logic and the doc's
  "ALIVE iff ALL of ..." clause — so the verdict is KILL, not ALIVE. `v33_falsifier.md` now spells the
  None case out explicitly on the capture bullet. Tests: `test_gate_capture_none_fails_closed_at_n_over_30`
  (FAIL + verdict KILL) and `test_gate_capture_none_is_n_too_small_below_min_n` (below MIN_N stays
  n-too-small). The pins stay three-way consistent (constants unchanged; doc + report agreement asserted).
- **F2 — single source of truth is literal.** `run_v33.V33Driver._simulate_ladder_fills` now calls
  `shadow.ideal_rung_crosses(trade.yes_price, o.price)` instead of inlining the comparison, so the
  SHADOW==DRY_SIM equivalence claim is literally one predicate. (The equivalence test already pinned it.)
- **F3 — cost-aware reserve guard.** The loader now fails closed unless
  `write_reserve_tokens + COST_CREATE(10) <= write_bucket_size` (a create must fit above the reserve, not
  merely below the bucket). New `_MIN_NONPRIORITY_WRITE_COST = 10` in params.py. Test:
  `test_loader_fails_closed_when_reserve_plus_cost_exceeds_bucket` (reserve 95, bucket 100 -> fail).
- **F4 — runbook numbering.** `V33_RUNBOOK.md` renumbered contiguous 1..11 (was 1..8 then 10,11,12).
- **F5 — 429 alarm signal (doc only).** Under the runbook's 429 hardening bullet: an occasional
  `rate_limited` is fine, but SUSTAINED / repeated `rate_limited` records are Brad's signal to check the
  Kalshi rate tier + `DAILY_ORDER_BUDGET`.
- **F6 — amend-in-flight vs stray race: NO race found; proven.** An amend rotates the coid (v33-a ->
  v33-b) but KEEPS the order_id; the executor retains the old-coid record (status `amended`) and remaps
  `_by_order_id[oid]` to the new coid. So `attribute(order_id, coid)` resolves the amended order whether
  the venue lists it under the OLD or NEW coid, and whether or not the amend ack has been processed
  (order_id is the stable key). The pre-place check therefore never flags an amending order as a stray.
  Tests: `test_amend_in_flight_order_not_cancelled_as_stray` (post-amend, venue under new coid) and
  `test_venue_ahead_new_coid_attributed_by_order_id` (venue ahead of our RestBook — attributed by the
  stable order_id). No code change was needed.

The R1 "open note" about the capture-None treatment is now RESOLVED by F1 (fail-closed).
