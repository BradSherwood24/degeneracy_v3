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
