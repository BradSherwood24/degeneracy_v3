# V3.3 Phase L2 review — execution wiring + money math + roster (dry side-by-side)

Reviewer: Opus 4.8 (Fable's delegated reviewer). PR #87, branch `feat/v33-l2-exec`, head `8d64935`,
base `origin/main` `1453a5f`. Reviewed in worktree `dv3_wt_review` on branch `review/v33-l2-exec`.
Date 2026-09-23.

## VERDICT: APPROVE WITH NITS (for the DRY side-by-side deploy) — DO NOT ARM until the MUST-FIX list clears

The PR's stated scope is the **dry** ladder running alongside the live V3.2 (Brad's "watch and compare").
For that scope the layer is sound: **dry cannot send an order by construction**, neutrality of the V3.2
roster is proven byte-level, the money math is correct and count-aware, and `dry_sim` money is quarantined
from every real-money path (pending credit, settlement backfill, S4). The full suite is green here (1175
passed, 5 skipped — 4 more skips than the builder's 1179/1, accounted for by this worktree's corpus gaps;
total 1180 both ways).

However, this code, once ARMED, rests up to 11 live orders and takes wings with real money. Two
armed-only defects (never reached in dry, so they do not block merging/running the dry watch) WILL cause
naked legs and/or blow the roll cadence the moment V3.3 is flipped to armed. They are listed under
**MUST-FIX-BEFORE-ARM**. The flip to armed must be gated on them, in addition to the L3 falsifier.

---

## Neutrality (proven, not asserted)

- `git diff origin/main...HEAD` touches **0 lines** of `pilot/service/v32/**` and `pilot/service/v33/core.py`.
- `paths.py` diff is **additive only** — the v32 helpers (`journal_dir_v32`, `mode_path_v32`, …) are byte-identical.
- Supervisor default: `Supervisor(roster default)._roster == "v32"` → module `service.run_v32`;
  `_log_path == supervisor_log_path()` (unchanged); a bogus roster falls back to `v32`. The v33 roster
  routes to `service.run_v33`, `logs_v33/supervisor.out`, `journals_v33/`, `v33_mode.txt`, `v33-*` sweep.
  (Verified by direct instantiation.)
- `register_supervisor_tasks.ps1 -DryRun` **without** `-WithV33`: the registration command lines
  (proxy + supervisor) are **byte-identical** to `origin/main` (`Compare-Object` empty). Only two
  informational `Write-Output` lines changed (see NIT-6). `-WithV33` adds a third task `DegeneracyV3_3`
  = `python -m service.supervisor --roster v33`.

## Dry can never send (by construction)

- The `ProxyWriter` (the only object that POSTs/DELETEs) is built **only** when `resolved_mode == "armed"`
  (`run_v33.py:665`). In dry it stays `None`.
- `build_executor_v33` returns `R.FrozenExecutor` for any non-armed effective mode; the FrozenExecutor has
  **no writer and no `rest_post`**, and **raises** on a REAL action kind (P3-1). Verified: dry executor
  type `FrozenExecutor`, `hasattr(writer)=False`, real `PLACE_REST` → `AssertionError`; `armed` executor
  without a writer → `ValueError`.
- Every send-capable path is gated on `armed`: the startup `cancel_stale_open_orders` (`:718 if armed`),
  the order-status poll (`order_poll=armed`), and the wing take/cancel run only through the LiveExecutor.
- The **degrade-to-dry** path is safe too: when `resolved_mode=="armed"` but `decide_v33_arming` refuses,
  a real `ProxyWriter` was constructed but is used **only for GET reads** (positions/balance/health); the
  executor is `FrozenExecutor` and no send path receives the writer.
- `--mode armed` on the CLI overrides a missing mode file → `"armed"` — this **matches `run_v32`
  exactly** (`resolve_v32_mode("armed", missing) == "armed"`), and even so `decide_v33_arming` degrades
  to dry today because the L3 falsifier isn't `STATUS: FROZEN`. The mode **file** remains the lever for the
  scheduled task. No regression vs the frozen V3.2 rule.

The dry "sends nothing" test (`test_v33_run.py:155`) is substantive — it asserts each of
`place_rest/amend_rest/cancel_rest/take_wings/retry_wing` is absent from the journal kinds.

---

## MUST-FIX-BEFORE-ARM (armed-only; not reached in dry; do not block the dry PR)

### 1. [BLOCKING-FOR-ARM] Coalesced wing take is sent as ONE order of up to K=11 contracts; the proxy cap (2, which S5 pins ≤2) rejects it → filled rungs left NAKED

`service/v33/core.py:908/918/936` sizes both wing legs to `count = b.total_count` (the coalesced batch
total, up to K=11). The inherited `_take_wings` → `_wing_entry` (`service/v32/executor.py:1034`) sends
`"count": int(leg.count)` in a **single** create with **no chunking**. Meanwhile the proxy today has
`MAX_CONTRACTS_PER_ORDER = 2`, and S5's own `v33_caps_agree` (`stops.py:86`) **refuses to arm** if the
proxy cap is above `V33_MAX_CONTRACTS_PER_ORDER = 2`. So a wing order covering more than 2 rung fills is
guaranteed to be rejected by the proxy — the two constraints (rungs need cap ≥ 1 lot; a coalesced wing
needs cap ≥ batch-total) cannot both be met at cap 2.

Failure scenario (armed): a pump coalesces, say, 8 rung fills into one batch. The wing take posts two IOC
orders of count 8. The proxy rejects both (8 > 2). `_wing_events` books count-0 → the core emits
`RETRY_WING`, which is also count 8 → rejected again → the batch reaches the wing cutoff **one-legged**.
Result: 8 bucket-NO rungs filled with **no wing protection** — a naked directional position and an
S1_LEGGED latch, at scale, with real money. This is exactly the naked-leg disaster the pin geometry exists
to prevent.

Receipt (probe against a fake proxy, K=11 batch): both wing orders in the batch body carry
`count == "11.00"`. No chunk to ≤ 2 anywhere.

`test_coalesced_wing_take_two_legs_sized_to_total` (`test_v33_executor.py:150`) validates the un-chunked
count-3 take **against a permissive fake that accepts count 3** — it proves the intended behavior, which is
precisely what breaks against the real cap.

Fix before arming: chunk each wing leg into ⌈count / MAX_CONTRACTS_PER_ORDER⌉ IOC takes (per side, still
coalesced/priced together), OR the arming design must raise the cap AND lift S5's ceiling in lockstep — but
raising the per-order cap also removes the "one lot per rung" guard, so chunking the wing take is the clean
fix. The runbook's flip section documents the amend cap + budget but is **silent on this**; add it.

### 2. [BLOCKING-FOR-ARM] The pre-place invariant recheck (0.5 s blocking sleep + a 2nd GET) fires on EVERY armed create in the ladder's normal state

`executor.py:170-185` (v33 `_pre_place_invariant`): if `_venue_resting_ours` returns any survivor after the
phantom filter, the code **unconditionally** does `sleep(INVARIANT_RECHECK_S=0.5s)` and a second GET before
it can conclude "healthy partial ladder, proceed." `_filter_phantoms` only drops *recently-cancelled* orders
— it does **not** drop our healthy resting rungs. In V3.2 a resting order of ours was an anomaly (it rests
exactly one), so this recheck was rare. In V3.3 a partial/full ladder resting is the **normal steady state**,
so the recheck fires on essentially every create.

Because amends are blocked at the proxy today, the default roll is cancel→create per order (`run_v33`
docstring + build report §5). Every rung roll therefore pays 0.5 s of **blocking** sleep + 2 GETs. A single
1c-W move that rolls all 11 rungs ≈ up to **5.5 s** of blocked WS reading (the executor blocks the WS reader
— execution-physics memory) + ~22 GETs. That throttles the "one order-roll per 1c W move" cadence the whole
V3.3 design rests on: during a fast pump the ladder cannot keep up, rolls land late, and live diverges from
the dry_sim the side-by-side just validated.

Fix before arming: run the overflow / dup-price / stray determination on the **first** read; only perform the
recheck (sleep + re-GET) when the first read actually flags an anomaly. A clean partial ladder must proceed
without the 0.5 s stall. (This also fixes most of the L2-Q2 GET-cost blow-up.)

### 3. [QUESTION → MUST-FIX-BEFORE-ARM] Per-batch bucket ticker (L2-Q3) drives settlement resolution, not just a label

`ledger.py:_bucket_ticker_for_fill` resolves the held bucket-NO ticker from `state.rest_bucket_Sd` /
`spot_Sd` (current), because `RungFill` carries no bucket. The builder frames a mid-window bucket change as a
cosmetic mislabel. It is not cosmetic once armed: `held_legs`'s bucket-NO ticker is what
`v33_settlement_backfill_sweep` → `settlement_payoff` fetches. If a batch that actually rested on bucket A is
labelled with the current bucket B, the backfill either never completes (B never resolves for that leg) or
settles against the wrong market → wrong realized money. Acceptable to defer only if you can show the ladder
never rests-and-fills across two buckets in one window; otherwise carry the bucket on `RungFill` before
arming. Verdict on L2-Q3: **defer for dry, MUST-FIX or prove-impossible before arming.**

---

## Findings (dry-scope)

### NIT-1 — L2-Q2 verdict: batch the order poll before arming/scaling
The 1/s per-rung status poll is ~11 GETs/s at K=11, under the Basic read budget (~20 GETs/s). But combined
with MUST-FIX-2's per-create pre-place GETs, the read rate spikes hard during rolls. Recommend a single
batched `GET /portfolio/orders?ticker=` poll (and applying the same batched read to the pre-place invariant)
before arming or adding rungs/series. Not a dry blocker.

### NIT-2 — L2-Q1 verdict: stray handling acceptable for L2, note the blast radius
Treating an unattributable `v33-*` stray as a conservative violation (cancel + alarm + stand down, without
V3.2's fill-on-stray booking) is fine for L2 (armed-only, ultra-rare, fail-safe). Note it stands down the
**whole window**, discarding the other (healthy) rungs' roll ability for a single stray. Acceptable; harden
in L3.

### NIT-3 — money math: a rung fill still in the OPEN coalesce group at close has rest cost but no floor
`compute_ladder_money_math` accrues `cost` from every `rest_fill` (`ledger.py:218`) but accrues `floor` only
per `wing_batch` (`:156-173`). A rung fill still in `coalesce_open` (not yet flushed to a `WingBatch`) at
window close contributes its rest cost but no floor → `realized_delta` understated. Direction is conservative
(never optimistic), so not a money-safety issue, but confirm the core always flushes `coalesce_open` and takes
its wings by close so no rung is left un-batched (the comment at `:148-149` promises a "phantom batch" that the
code does not actually create).

### NIT-4 — `cancel_stale_open_orders` (v33) with a missing exchange_index
`executor.py:338-339`: a 404 DELETE is treated as "already gone" only when `exch is not None`; with
`exch is None` it counts as an error. The sweep result is not gated into `decide_v33_arming`, so a stale
`v33-*` order whose `exchange_index` the venue didn't echo could survive startup and the window arms anyway.
This mirrors V3.2 and the per-order `expiration_time` backstops it, so NIT not blocking — but worth a belt in L3.

### NIT-5 — hollow-assertion scan: clean
The new tests exercise real state and assert real values (study locks E5=0.0589 / 11th rung=0.1603, batches
[3,8]; fee-ceiling-once-per-fill; `lock_solved == lock_value(price, W)` ≠ `E_rung`; count-aware floor=$4 and
delta; dry_sim quarantine in pending-credit/backfill). No hollow assertions found. The one caveat is
`test_coalesced_wing_take…` validating the un-chunked take against a permissive fake (see MUST-FIX-1).

### NIT-6 — ps1 console text (cosmetic)
Without `-WithV33`, the registration commands are byte-identical, but the dry-run **console output** added a
`v33 task: (not requested…)` line and changed the warning text `"never two."` → `"never two V3.2 drivers."`.
No functional impact.

---

## Items verified green
- Money math hand-check (2-rung fixture): held=3 legs @ count 2 → floor `v33_set_floor_dollars(3,2)=$4`;
  cost = 0.89 (rests, maker fee 0) + 0.57×2 + fee_total(0.57,2)=0.0344 + 0.09×2 + fee_total(0.09,2)=0.0115
  = $2.2559; `realized_delta = 4 − 2.2559 = $1.7441`. Matches the code. Fee ceiling applied once per fill,
  not per contract (`_fee_total`).
- `dry_sim` never counted as realised: `realized_unsettled=False` for dry_sim rows (`ledger.py:234`);
  `v33_pending_credit` skips dry_sim (`:415`); `v33_settlement_backfill_sweep` skips dry_sim (`:477`); S4
  reads balance + pending-credit (dry_sim excluded). No path sums a dry_sim `realized_delta` as money.
- Stops: S4 `$3.00` banded via reused `s4_balance_decision`; S1_LEGGED per-contract, day latches at
  threshold 2; v33 day-guard file `v33_stops_<day>.json` separate from v32's and routed via
  `paths`/`DV3_DATA_DIR` (`_resolve_v33_guard_path`); consulted in `decide_v33_arming` and read at arming.
- Side-by-side report joins by close hour, labels V3.2 realised vs V3.3 dry_sim, running totals, and does
  not crash when one roster lacks a row (`report.py:134 continue`).
- O-1 (no premature allotment latch on a partial sweep) and O-2 (fresh order at a previously-filled price
  not a double-book) are handled at the driver/invariant layer with real tests.

## Questions for Brad
1. Before any flip to armed: is the plan to **chunk wing takes to ≤ MAX_CONTRACTS_PER_ORDER** (recommended),
   or to raise the proxy per-order cap? The latter conflicts with S5's `≤2` ceiling and the "one lot per
   rung" guard. (MUST-FIX-1)
2. Can the ladder ever rest-and-fill across two range buckets within one window? If yes, `RungFill` must carry
   its bucket before arming, else settlement backfill can mis-settle. (MUST-FIX-3)

## Test receipt
`python -m pytest -q` (pilot): **1175 passed, 5 skipped** in ~14s. (Builder reported 1179/1 with the full
corpus; the 4-test delta is this worktree's corpus gaps, total 1180 both ways.)

---

# ROUND 2 RE-REVIEW (head 6eabe7f atop 8d64935) — 2026-09-23

Re-reviewed the delta `8d64935..6eabe7f` (760 insertions across 15 files) adversarially with cap-enforcing
fakes. **All four MUST-FIX-BEFORE-ARM items from Round 1 are CLOSED and verified.** Verdicts below.

## DRY-MERGE VERDICT: APPROVE
Every Round-1 dry-safety property still holds. All the new machinery (write pacer, wing chunking, the
reworked pre-place invariant, the batched poll) lives on `V33LiveExecutor` — the **armed** path. Dry still
uses `R.FrozenExecutor` (build_executor_v33 dry branch unchanged) and still sends nothing by construction.
Full suite here: **1188 passed, 5 skipped** (builder's 1192/1 modulo this worktree's 4 corpus-gap skips;
1193 total both ways). L1 core (68) + golden (9) byte-unchanged and green; params 8→10 (the RungFill change
is additive, defaults None). Params sha self-verifies to the re-pinned `415b63da…`; R4 sha retained as
`PREVIOUS_V33_PARAMS_SHA256_L1_R4`; new fail-closed loader checks (hint >= lots_per_rung; write tokens/bucket
>= 1) present.

## ARM-READINESS: the four MUST-FIX are closed (verified with fakes)

### MUST-FIX-1 (wing chunking / naked-leg) — CLOSED
`V33LiveExecutor._take_wings` splits each leg into `ceil(remaining/wing_cap)` IOC chunks and aggregates the
chunk fills back into ONE Fill per leg. Verified against a **cap-enforcing** fake (rejects count>cap):
- cap 2, coalesced count 11 → **12 chunk orders** (`[2,2,2,2,2,1]` per leg), all <=2, aggregated to `Fill(yes,11)` + `Fill(no,11)`.
- cap 11 → `[11,11]` (1 order per wing).
- Partial: a chunk filling 1-of-2 plus a full chunk → leg got 3 of 4 → `Fill(count=0)` (core retries); the
  retry re-chunks **only the remaining 1**, `_wing_filled[(0,'yes')]` ends at 4, never over-hedged.
`v33_caps_agree` ceiling is now `K*lots_per_rung` (`[1,11]`), so proxy cap 2 AND 11 both arm; cap 12 refused.
**wing_cap fail-closed on unreachable /health:** verified `health in {None, {}, no-caps}` → `v33_caps_agree`
False → **not armed → dry** (wing_cap's default-11 is never used because dry uses FrozenExecutor); whenever
caps_agree passes, `_proxy_max_contracts` returns the same numeric cap, so `wing_cap = min(11, cap)` is the
real cap when armed. See R2-N2 for a defense-in-depth belt.

### MUST-FIX-2 (pre-place stall) — CLOSED
`_invariant_verdict` decides overflow/dup/stray on the FIRST read; the 0.5 s recheck (sleep + 2nd GET) runs
ONLY on a flagged anomaly. Verified: 10 healthy rungs + a fresh-price create → **1 GET, 0 sleeps, 0
rechecks, proceed**; a dup price → **2 GETs, 1 sleep**, violation + `rest_invariant_dup_price` bump. The
per-create WS-reader stall in the ladder's steady state is gone; fail-safe direction (stand down on a
confirmed anomaly) preserved.

### MUST-FIX-3 (per-rung bucket / settlement) — CLOSED (named core change, additive)
`RungFill` gains `bucket_ticker/bucket_Sd/bucket_Su` (None defaults); `core._book_rung_fill` captures them
at FILL time from the placed rung's own `RestOrder.bucket_Sd` (core RestOrder already carries `bucket_Sd`),
falling back to `rest_bucket_Sd`/`spot_Sd`. `ledger._bucket_ticker_for_fill` reads `rf.bucket_ticker` first,
so a rest-and-fill across a bucket change books two batches with distinct bucket-NO tickers and the backfill
prices each against its own market (`test_per_batch_bucket_ticker_from_rungfill`). Falls back sanely for old
rows (getattr → None → state fallback). L1 core/golden unbroken.

### MUST-FIX-4 (write-token pacer) — CLOSED (with a residual, R2-N1)
`WriteTokenBucket` (100/s, size 100; create/amend 10, cancel 2, batch 10·n). Overrides pace
`_place_rest`/`_amend_rest`/`_cancel_rest`, `place_batch`, and the chunked wing take. Verified: 11
non-priority creates step 90→0 and the 11th **waits** (never < 0 → the local bucket, i.e. the venue budget,
is never overdrawn by creates); a priority cancel after depletion **waits 0** and overdraws the LOCAL bucket
to −2. Cancels + wing takes are priority. Batched poll (`poll_orders_for_bucket` → one
`GET /portfolio/orders?ticker=`) replaces the K per-rung GETs; per-order dedup kept in `on_poll_fill`.

## Round-2 NITs / arm-readiness (none block the dry merge)

- **R2-N1 [arm-readiness] the pacer reserves NO headroom for the safety-critical priority burst.** Verified
  a priority write drives the LOCAL (⇒ venue) bucket negative (tokens → −2). Argument on real risk: LOW and
  fail-safe. The T-5 cancel-all fires minutes after creates stop (venue refilled by then); during
  cancel→create rolling the cancels are cheap (2) and precede/interleave the creates. And any venue 429 that
  slips through is fail-safe — a 429'd **create** is treated as a rejection so the rung is simply not placed
  (no naked bucket-NO); a 429'd **wing chunk** is treated as unfilled → RETRY_WING re-chunks the remainder
  (verified path); a 429'd **cancel** is backstopped by the per-order `expiration_time`. RECOMMEND before
  arming, though: (a) reserve headroom (floor non-priority creates at ~30 tokens) so a priority cancel-all /
  120-token full-sweep wing take can't be venue-429'd right after a create burst; (b) distinguish a 429
  response from a business rejection so a transient rate-limit doesn't trip the 3-consecutive-reject stand
  down. Note the pacer models a **single-writer** venue bucket — correct because only one roster is armed at
  a time, but be mindful the flip window shares the account.
- **R2-N2 [arm-readiness] wing_cap fail-closed is emergent, not local.** The fail-closed-on-unreadable-/health
  is enforced by the arming gate coupling (v33_caps_agree refuses), NOT by `build_executor_v33`, whose armed
  branch would default `cap = params.max_contracts_per_order_hint (=11)` if ever handed `wing_cap=None`. Safe
  today; add a belt: in the armed branch raise/stand-down if the proxy cap is unknown.
- **R2-N3 [ledger accuracy] wing price on a partial→retry take.** The aggregated leg Fill uses `count=total`
  but `price = the retry chunks' weighted avg only` (not weighted across the original + retry fills), slightly
  mispricing the ledger wing cost for a retried leg. The actual hedge is fully placed (no position error) and
  `self.fills` holds each chunk's true price+fee. Rare (needs a mid-sweep chunk rejection); ledger-accuracy
  NIT.
- **R2-N4 [belt] batched poll scope.** `poll_orders_for_bucket` polls only the CURRENT bucket ticker; rungs
  still resting on a prior bucket after a mid-window change aren't polled (WS fill channel is primary; those
  rungs should have been cancelled on the change). Minor.
- Prior **NIT-2** (an unattributable stray stands the WHOLE window down) remains accepted for L2, noted for
  L3. Prior NIT-3 (open-coalesce floor — clarified: a lone bucket-NO floor is genuinely $0, so accruing rest
  cost with no floor is conservative; `test_close_time_flush_takes_wings_for_last_coalesce_group` proves the
  core flushes + hedges the last group by close so `floor_booked > 0`), NIT-4 (sweep 404 → gone regardless of
  shard), and NIT-6 (ps1 `-DryRun` without `-WithV33` now **byte-identical to origin/main** — verified by
  running both) are CLOSED.

## MUST-DO-BEFORE-ARM (updated)
1. L3 falsifier `ceremony/v33_falsifier.md` carrying `STATUS: FROZEN` (out of L2 scope, known).
2. RECOMMENDED (R2-N1): pacer headroom reserve + 429-vs-rejection distinction. Fail-safe as-is, but this is
   the safety-critical hedge/flatten path — harden it before real money.
3. Brad's proxy levers (his hands): `MAX_CONTRACTS_PER_ORDER` (2 → 6+6 wing chunks, or 11 → 1+1), the amend
   cap, `DAILY_ORDER_BUDGET → 8000` — all now documented in the runbook flip section.

## Round-2 test receipt
`python -m pytest -q` (pilot): **1188 passed, 5 skipped**. L1 core+golden+params: **87 passed** (68 core + 9
golden unchanged; 10 params). New adversarial tests reviewed and match my independent probes (cap-2 6+6,
cap-11 1+1, retry-remainder, one-GET-no-sleep, dup-recheck, pacer-no-overdraw, priority-not-delayed,
batched-poll, would-twin-raises). No hollow assertions found.
