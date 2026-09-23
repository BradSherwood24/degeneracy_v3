# V3.3 Phase L2 build report — execution wiring + money math + roster (dry side-by-side)

Builder: Opus 4.8 (Brad's mandate). Branch `feat/v33-l2-exec` off `origin/main` 7c92292 (PR #85 merged).
Date 2026-09-22. Scope: take the approved L1 ladder core (`service/v33/core.py`, unchanged here) and make
it a runnable per-window process alongside the live V3.2, starting in DRY. Nothing changes the live V3.2
(its core/params/run_v32/executor/ledger/stops are untouched). `python` only; no sealed/holdout read; no
live order path exercised (all tests fake the proxy).

## Files delivered (absolute under `C:/Users/Brads/Python_stuff/dv3_wt_v11`)
- `pilot/service/v33/executor.py` — `V33LiveExecutor` (a SUBCLASS fork of `service.v32.executor.LiveExecutor`)
  + `cancel_stale_open_orders` (v33-* only).
- `pilot/service/v33/ledger.py` — count/rung-aware money math + row builder + pending credit + backfill.
- `pilot/service/v33/stops.py` — v33 day-guard file, S4 $3.00, S1_LEGGED, S5 caps, `decide_v33_arming`.
- `pilot/service/v33/report.py` — LADDER block + SIDE-BY-SIDE block (vs V3.2).
- `pilot/service/run_v33.py` — the per-window process (mode resolution, DRY ladder-fill simulation, arming
  gate, journal + ledger row, reused two-connection recorder/run-loop).
- `pilot/service/paths.py` — ADDED v33 paths (`journal_dir_v33`, `log_dir_v33`, `ledger_path_v33`,
  `mode_path_v33`, `ops_dir_v33`, `falsifier_path_v33`); v32 paths byte-identical.
- `pilot/service/supervisor.py` — ADDED `--roster {v32,v33}` (default v32, byte-identical); v33 spawns
  `service.run_v33`, sweeps `v33-*`, reads `v33_mode.txt`, logs to `logs_v33/`.
- `pilot/ops/register_supervisor_tasks.ps1` — ADDED `-WithV33` switch (registers `DegeneracyV3_3` dry).
- `pilot/ops/V33_RUNBOOK.md` — ADDED §8 "V3.3 dry ladder alongside V3.2" + the flip procedure.
- Tests: `test_v33_executor.py` (18), `test_v33_ledger.py` (14), `test_v33_stops.py` (17),
  `test_v33_paths.py` (5), `test_v33_run.py` (11), `test_v33_report.py` (6), `test_v33_supervisor.py` (11)
  = **82 new tests**.

## Test counts
- Before (full suite on this worktree): **1097 passed, 1 skipped**.
- After: **1179 passed, 1 skipped** (+82). The L1 core suite (85: params 8, core 68, golden 9) is
  byte-for-byte unchanged and green. `check_invariants` still runs after every event in every core test.
- `core.py` was NOT modified (the O-1/O-2 NITs are handled at the executor/driver/ledger layer — below).

## Brad's decisions recorded (verbatim in the module docstrings + runbook)
- Dry side-by-side: "run it along side V3.2 without it trading, then flip V3.3 to contracts 10 and V3.2 to
  0. Just to watch and compare. Make sure V3.3 is running exactly as expected before $20+ are on the line."
- Mechanism = MODE FILES not params. V3.3 gets `ops/v33_mode.txt` (missing → **dry**, never armed),
  `ledger/v33_ledger.jsonl`, `journals_v33/`, `logs_v33/`, `ops/v33_stops_<day>.json`, all via `service.paths`.
- In dry: run the full core against the live feed, journal every WOULD-send, SIMULATE ladder fills with the
  IDEAL rule (spot-bucket YES-taker print at `yes_price >= 1 − rung price` fills that rung; wings priced
  from the live book at fill time as the V3.2 shadow does), book as `dry_sim` (never realised), send NOTHING.
- Q2 coalesce 150 ms; Q3 no refills (core); Q5 S4 $3.00 stays for K=11; Q6 amend cap + budget are Brad's;
  `max_amends_in_flight` 3 (core).

## Decisions I made (and why)

1. **Executor = a subclass fork, not a copy.** The V3.2 executor is already per-ORDER (every path keys on
   coid/order_id + a retained RestBook), so `V33LiveExecutor(LiveExecutor)` inherits place/amend/cancel/
   fallback/coalesced-wings/fill-dedup/cancel-race UNCHANGED and overrides ONLY: the coid prefix
   (`v33-`), the venue read (`_venue_resting_ours` scoped to `v33-`), the K-aware pre-place invariant, and
   the optional batch create. This minimises drift with the frozen V3.2 wire law.

2. **O-2 (venue reconciliation expects a fresh order at a previously-FILLED price).** The K-aware pre-place
   invariant reads LIVE venue truth (resting orders only). A filled rung is off the book, so it never
   appears in the resting list; placing a fresh order at that same price is therefore NOT flagged as a
   double-book. It attributes each resting order's price via the RestBook (we placed it) and flags only:
   OVERFLOW (venue already holds ≥ K of ours), DUP-PRICE (one of ours already resting at the place price),
   or an unattributable stray. Test: `test_invariant_o2_fresh_order_at_previously_filled_price_ok` plus the
   overflow/dup/partial/stray cases.

3. **O-1 (premature allotment latch when a partially-placed ladder fully fills).** DECISION: keep the core's
   latch as built (it latches `rest_allotment_done` only when `rungs_filled >= max_sets` OR the ladder is
   empty with nothing in flight), and PROVE it at the driver level: a shallow sweep fills 3, leaves 8 live,
   allotment OPEN; a later deep print fills the rest → latched. No premature latch. Test:
   `test_o1_partial_sweep_does_not_latch_allotment_until_full`. No core change was needed.

4. **DRY money math is STATE-DERIVED, not executor.fills-derived.** V3.2's `_compute_money_math` reads
   `executor.fills`, which a dry FrozenExecutor never populates. Brad wants a dry_sim ladder row, so
   `compute_ladder_money_math` derives everything from STATE (`rest_fills` + `wing_batches` + `wing_legs`),
   which the dry simulation + the FrozenExecutor's synthetic wing fills populate. This one function serves
   both dry_sim and armed (the core books rung fills + takes wings in both). Per fill: `lock_solved =
   lock_value(price, W_at_fill)` (the L1 R4 contract note — the price/W value, NOT the integer `E_rung`
   label) and `realized_lock` (per contract, wings actually paid); per batch: held legs + count-aware floor;
   per row: the LADDER summary + `dry_sim` flag.

5. **Fallback-first is the default until the amend cap.** The proxy amend cap is NOT yet applied, so amends
   are blocked at the proxy today. The inherited `_amend_rest` already falls back to cancel→confirm→create
   per order on any non-2xx, so the roll runs correctly as cancel/create by default. Flipping to amend-first
   is a proxy/env value, not code. Batch create is a `--batch-create` flag (default OFF; armed only; the
   FrozenExecutor has no batch path, so dry never batches).

6. **Supervisor `--roster` (minimal, tested, default byte-identical).** Rather than a second supervisor
   module, one `--roster {v32,v33}` flag selects the child module (`service.run_v33`), the boot-sweep coid
   prefix (v33-*, never v32-*) + mode file (`v33_mode.txt`), the journal dir to rotate, and the log path.
   Default (v32) is unchanged. The `-WithV33` ps1 switch registers `DegeneracyV3_3` = `python -m
   service.supervisor --roster v33`. Running it alongside the V3.2 supervisor is expected — "never two
   drivers" is about two drivers of the SAME roster.

## Dry-run receipt
A LIVE dry window was NOT run: at build time UTC was **02:57Z**, inside the forbidden **:38–:59** proxy band
(house law), and `run_v33` with no `--close` would connect at the already-passed gate (T−905s of the 03:00
close) and stream the live feed through the band — not permitted. Instead the **golden-fixture replay
through the real `V33Driver` in dry** is the receipt (`test_v33_run.py`):
- `test_dry_run_full_sweep_books_11_dry_sim_fills_with_study_locks`: 11 `dry_sim` fills, solved per-rung
  locks match the study (E5 top +5.89c … E14 +15.01c) with the deeper distinct 11th rung at +16.03c, two
  coalesced wing batches (3 + 8).
- `test_dry_run_sends_nothing_only_would_and_sim_records`: the dry journal contains ONLY `would_*` +
  `dry_sim_fill` records and **NO** `place_rest`/`amend_rest`/`cancel_rest`/`take_wings`/`retry_wing` — i.e.
  it sends nothing. (In armed mode the same driver routes to `V33LiveExecutor`; that path is proven by
  `test_v33_executor.py` with a fake proxy.)

## Brad's levers (nothing here is Claude's to pull)
- `ops/v33_mode.txt` (missing → dry; `armed` requires the file). `ops/v32_mode.txt` on the flip.
- The proxy amend cap (`ops/proxy_amend_cap.md`) + `DAILY_ORDER_BUDGET` → 8000 (Q6).
- `register_supervisor_tasks.ps1 -WithV33` (registers `DegeneracyV3_3` dry) and the flip in a :02–:33 window.
- `DV3_DATA_DIR` / `DV3_PROXY_BASE` (route where the v33 roster reads/writes; same rules as V3.2).

## Open questions for the reviewer
- **L2-Q1 (venue invariant strays).** The v33 pre-place invariant treats an UNATTRIBUTABLE surviving stray
  (a `v33-*` order not in our RestBook) as a conservative violation (cancel-via-inherited-path + alarm +
  stand down), WITHOUT the full V3.2 fill-booking-on-stray machinery (which prices a stray that left the
  book by FILLING). This ultra-rare path is armed-only and never reached in dry; L3 can harden it if we
  want per-stray fill booking at K. Acceptable for L2?
- **L2-Q2 (armed poll cost).** `_order_status_poll_v33` polls EVERY live rung once/second (K GETs/s). At
  K=11 that is ~11 GETs/s on the market-data host — fine on Basic, but confirm before arming.
- **L2-Q3 (bucket ticker per rung fill).** `RungFill` carries no bucket ticker; `compute_ladder_money_math`
  resolves the held bucket-NO leg's ticker from `rest_bucket_Sd`/`spot_Sd`. A mid-window bucket change that
  fills on TWO buckets in one window would label both batches with the current bucket. V3.2 walks per-batch
  rest-fill records for this; L3 can add the per-rung bucket if a bucket-change-mid-sweep proves common.

## Deferred to L3 (correctly out of scope)
The V3.3 falsifier draft (`ceremony/v33_falsifier.md`, `STATUS: FROZEN` required before S5 arms),
`ops/V33_ARMING.md`, the LADDER SCOREBOARD / per-rung falsifier scoreboard in the report, the SO-3 deep
observation ladder (16..25c), the full report, and the V3.2 Registration close line (Q4).

---

# Round 2 (2026-09-23) — addressing PR #87 review (APPROVE WITH NITS for dry; MUST-FIX-BEFORE-ARM)

Review verdict was APPROVE for the dry side-by-side with a MUST-FIX-BEFORE-ARM list. Brad's word:
"Make sure V3.3 is running exactly as expected before $20+ are on the line" -> all four MUST-FIX items +
the NITs are fixed NOW in this PR. Suite after Round 2: **1192 passed, 1 skipped** (+9 over R1's 1183;
net new v33 L2 tests ~91). The L1 core suite stays **85 green** (the one named core change — a new
optional `RungFill` field — is additive; no L1 test asserts on it).

## MUST-FIX-1 — chunked coalesced wing take (naked-leg fix) — FIXED
`V33LiveExecutor._take_wings` now splits EACH wing leg into `ceil(remaining / wing_cap)` IOC chunks of
`<= wing_cap` and issues all chunks of both wings in one priority-paced burst. `wing_cap = min(params
.max_contracts_per_order_hint = 11, the live proxy /health cap)`, read at window start. The chunk fills
are AGGREGATED back into ONE Fill per original leg (the core still sees one leg — money math unchanged):
a FULL aggregate -> leg filled at the weighted-avg price; a PARTIAL (a rejected/IOC-unfilled chunk) ->
leg reported unfilled so the core emits RETRY_WING, and `self._wing_filled[(batch, side)]` makes the
retry (a new coid) re-chunk ONLY the remaining count — never over-hedging. S5 `v33_caps_agree` now
accepts a proxy cap in `[lots_per_rung, K*lots_per_rung]` (so cap 2 -> 6+6 chunks AND cap 11 -> 1+1 both
arm). Tests: cap 2 -> 12 chunks aggregated to 11; cap 11 -> 2 orders; a rejected chunk retried for the
remainder only; a CAP-ENFORCING fake replaces the permissive one.

## MUST-FIX-2 — pre-place invariant no longer stalls the steady state — FIXED
`_pre_place_invariant` now decides overflow/dup/stray on the FIRST venue read (via a `_invariant_verdict`
helper + RestBook price attribution). The 0.5 s blocking recheck (sleep + 2nd GET) runs ONLY when the
first read flags an anomaly (which may itself be read-path lag). A healthy partial/full resting ladder —
the V3.3 steady state — proceeds on ONE GET with NO sleep. Tests: 10 healthy rungs + a fresh-price create
-> exactly one GET, zero sleeps, zero rechecks; a dup price -> 2 GETs + 1 sleep (recheck path).

## MUST-FIX-3 — per-rung bucket ticker (settlement correctness) — FIXED (named core change)
`RungFill` (core) gains `bucket_ticker` / `bucket_Sd` / `bucket_Su`, captured at FILL time in
`_book_rung_fill` from the filling order's own `bucket_Sd`. `ledger._bucket_ticker_for_fill` uses
`rf.bucket_ticker`, so a rest-and-fill across a bucket change lands two batches with DIFFERENT bucket-NO
tickers and the settlement backfill prices each against its own market. Additive (defaults None) — no L1
test broke. Test: fills before/after a bucket change -> held bucket-NO legs carry the two distinct buckets.

## MUST-FIX-4 — Basic-tier write-token pacer — FIXED
New `WriteTokenBucket` (rate `write_tokens_per_s`=100, size `write_bucket_size`=100; cost create/amend 10,
cancel 2, batch create 10*n). `V33LiveExecutor` overrides `_place_rest`/`_amend_rest`/`_cancel_rest`
(and paces `place_batch` + the chunked wing take) to `acquire` before sending; a non-priority write waits
(injected sleep) until the bucket has room, journaling `write_paced`. Cancels + wing takes are PRIORITY
(served immediately, never queued behind ladder creates). Tests: 11 creates never overdraw the bucket
(wall-clock via an advancing injected clock); a priority cancel after a depleted-by-creates bucket waits 0.

## NITs
- NIT-3 (open coalesce group floor): fixed the misleading comment — an un-batched fill has a GENUINELY $0
  floor (a lone bucket-NO is directional), so accruing its rest cost with no floor is conservative/correct,
  not an understatement. Added `test_close_time_flush_takes_wings_for_last_coalesce_group` proving the core
  flushes `coalesce_open` + takes wings by close so no rung is left un-batched.
- NIT-4 / 5(b) (sweep 404): the v33 startup sweep now treats a 404 on OUR `v33-*` order as "already gone"
  (terminal) regardless of the shard, per the coordinator; the per-order `expiration_time` backstops the rest.
- NIT-6 / 5(c) (ps1 cosmetics): reverted — `-DryRun` WITHOUT `-WithV33` is byte-identical to main (the
  "not requested" line removed; the warning restored to "never two.").
- NIT-1 / 5(d) (batched poll): added `params.order_poll_batched` (default ON) + `poll_orders_for_bucket` —
  one `GET /portfolio/orders?ticker=<bucket>` per tick instead of K per-rung GETs, per-order dedup kept.
- 5(e) (runbook flip): the flip section now lists all four MUST-FIX items as CLEARED and Brad's proxy
  levers (MAX_CONTRACTS_PER_ORDER 2 vs 11 with the wing-chunk consequence, the amend cap, budget 8000).

## Params re-pin
Added `max_contracts_per_order_hint` (11), `write_tokens_per_s` (100), `write_bucket_size` (100),
`order_poll_batched` (true) to `policy/v33_params.json`; re-pinned
`FROZEN_V33_PARAMS_SHA256 = 415b63daa2ff9dd7efa0193409b0e367b545c2ce2334fe44229484cb5395b3c2` (R4 sha kept
as `PREVIOUS_V33_PARAMS_SHA256_L1_R4`). New fail-closed loader checks (hint >= lots_per_rung; write
tokens/bucket >= 1) with tests.

## Brad's lever choices to record
- Proxy `MAX_CONTRACTS_PER_ORDER`: 2 (wings as 6+6 IOC chunks) OR 11 (wings as 1+1, but lifts the
  one-lot-per-rung guard) — both arm; the executor's wing_cap follows the live /health cap.
- Amend cap (fallback cancel->create until applied) + `DAILY_ORDER_BUDGET` -> 8000.

## Still deferred to L3 (unchanged)
The V3.3 falsifier draft + STATUS: FROZEN, `V33_ARMING.md`, the LADDER SCOREBOARD / per-rung falsifier,
SO-3 deep observation ladder, the full report, the V3.2 Registration close line. NIT-2 (a stray stands the
whole window down) is accepted for L2 and noted for L3 hardening.
