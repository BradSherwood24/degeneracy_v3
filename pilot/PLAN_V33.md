# PLAN V3.3 -- the rolling ladder + the host-shaped runtime (PLAN ONLY, 2026-09-22)

Status: SCOPE. Nothing built. Brad's words (2026-09-22 ~21:30Z), verbatim:

> "I'm thinking we combine a few things here into V3.3, first is the ladder but also the proxy / full
> codebase refacter for hosting. The requote is easier than you're making it, just need to submit an
> update to the price of one order. If we need to reprice down, we submit an update to the 5 cent order
> and reprice it to 16 cent profit. So the 5 profit level order becomes the 15 cent profit, and the 6 cent
> becomes the 5 cent. Obviously, flip it so 15 cent profit level becomes 4 when we reprice up."
> "Don't start building yet, just looking to get the plan in place first. Then well compact and get to
> building. Of course, the transition to hosted wont happen yet, just transition to running what we would
> run as a hosted service but locally"

Earlier the same day: "Say 1 contract order on the books at levels where profit is 5 through 15 cents.
Currently C = 10 cents ... any entry right now would then enter at that bucket price minus 5 cents through
as far as it walks the ladder."

## 1. What V3.3 is

Two things, shipped as one roster change:

**A. The rolling ladder.** Instead of one resting NO bid of `contracts` lots at the E = 10c level, rest
K one-lot NO bids on K consecutive cents, anchored at the top rung `n_top = n(E_min, W)` (solved exactly
as V3.2 solves n: largest cent with `n + fee(n) <= 2 - E_min - W`, floor `n_min`, cap `no_ask - 1c`).
Rung k sits at `n_top - k cents` (k = 0..K-1); its realised margin is `E_min + k` cents (give or take the
fee-reserve rounding). Default K = 11, E_min = 5 -> rungs at 5..15c. A YES-taker sweep walks the ladder
from the top: shallow pumps fill 1-4 rungs, full sweeps fill all K (measured: every one of the 12 live
V3.2 sets was a full sweep; sweep depth median 22c).

**The roll (Brad's requote).** When W moves so that `n_top` changes by one cent, the ladder does NOT
re-price K orders. It moves ONE order from the end that fell off to the end that opened up:
- W up (wings dearer) -> every rung must sit 1c lower -> the old TOP order (was E_min) is amended down to
  `bottom - 1c` and becomes the new deepest rung; the other K-1 orders are untouched and KEEP QUEUE.
- W down -> the old BOTTOM order is amended up to `top + 1c` and becomes the new shallowest rung.
- A 2c move = two rolls, strictly sequential. Amend-first (PR #59 path), cancel -> confirm -> create as
  the fallback. Bucket change (~1 per window): cancel all K, place all K on the new ticker.
- Order count is the SAME as today: today's replace = 1 cancel + 1 create per >= 2c move (~100/window,
  ~200 orders); the roll = 1 amend per 1c (or 1 cancel + 1 create without the amend cap). Queue position
  is the win: today every replace goes to the back of the book at the new price; the roll keeps K-1 rungs
  in place.

**B. The host-shaped runtime.** Run locally exactly what Render would run (see
`ops/RENDER_MIGRATION_PLAN.md` section 3): a supervisor process that wakes one window per UTC :40
(replacing the per-hour Task Scheduler entry), `DV3_DATA_DIR` for every writable path, `DV3_PROXY_BASE`,
the proxy configured by env (`PROXY_HOST`, key path, budget path), `requirements.txt` +
`.python-version`, Linux CI. The move itself is NOT in V3.3; after V3.3 the move is "create the services
and set the env vars".

## 2. Evidence (`pilot/build/mc/v33_ladder_ideal.*`, 167 armed windows 2026-09-14..21, ideal fills)

| configuration | total lock | contracts | windows | per contract |
|---|---|---|---|---|
| ladder 5..15c, 1 lot per rung | 1806c | 188 | 28 | 9.6c |
| flat 10 lots at 10c | 1550c | 150 | 15 | 10.3c |
| today: 2 lots at 10c | 310c | 30 | 15 | 10.3c |

Pumps are bimodal: shallow (stop at 5-8c, 1-4 rungs) or full sweeps to E_max median 22c (min 17, max 50).
The 10c rung absorbed >= 39 lots in every sweep (median ~300). First prints per rung: 1-2 lots -> one lot
per rung is the right unit. Ladder over flat-10 is only 1.17x; its case is entries (+13 windows), queue
position, and the deep rungs -- not raw size. Deeper rungs (to 20-25c) look profitable but need capital
($40 in flight at 5..25 vs $54 balance) and their absorption is inferred, not counted.

## 3. Decisions (Brad, 2026-09-22 ~22:00Z, verbatim: "Agree on all, including the observation-only
ladder, except one small tweak to 4." -- every [assumed] answer below is therefore DECIDED; Q4 carries
the tweak)

- **Q1 rung range.** 5..15c (K = 11, ~$21 in flight per full sweep) [assumed] -- or 5..20c (K = 16,
  ~$30) given sweep depth. Any range is a params value; the sha pins it.
- **Q2 wings per fill.** Coalesce rung fills that land within `wing_coalesce_ms` (default 150 ms) into
  ONE wing pair sized to the total filled (a full sweep prints all K rungs in ~90 ms; K separate pairs =
  2K taker orders in a burst) [assumed] -- or strictly one wing pair per rung fill (2K orders). Either way
  the count taken always equals the count filled (the 2026-09-18 ruling).
- **Q3 refills.** A filled rung is NOT re-placed inside the same window (max K lots per window; the risk
  cap is the ladder size) - **Q4 V3.2's record.** V3.2 KEEPS RUNNING ARMED through the whole V3.3 build (Brad, 2026-09-22
  ~22:00Z, verbatim: "Lets keep V3.2 running during this build out, so n=>12. Hopefully capture another").
  Its n keeps growing; the V3.3 dry shadow runs alongside it. Only when V3.3 arms does the V3.2 roster
  stop (one armed roster per bucket) and its falsifier close with a dated Registration line quoting the
  n, the record and "verdict not reached / superseded by V3.3". V3.3's 10c rung is NOT pooled into
  V3.2's n (different mechanics).
, 12/12, mean +11.2c, verdict
  not reached" [assumed]. V3.3's 10c rung is NOT pooled into V3.2's n (different mechanics).
- **Q5 stops.** S4 day-loss cap: $3.00 was sized for 1-2 lots; a one-legged rung costs ~$0.35 worst
  case, so a bad K-rung sweep could breach it in one window. Proposal: S4 = max($3.00, 0.5 x K x $0.35)
  [assumed $3.00 stays for K = 11 -> $1.93 < $3.00, so no change needed; revisit at K = 16+]. S1 legged
  latch stays per contract.
- **Q6 order budget + amend cap.** The roll needs the proxy to accept amends (`ops/proxy_amend_cap.md`,
  Brad's `.env` + restart) -- otherwise every 1c roll is a cancel + create and ~200 orders/window + K
  per bucket change ~ 4,600/day vs `DAILY_ORDER_BUDGET` 4,000. Proposal: apply the amend cap AND raise the
  budget to 8,000 [assumed]. `MAX_CONTRACTS_PER_ORDER` = 2 is untouched (one lot per rung).
- **Q7 falsifier shape** (section 6) -- confirm the gates.

## 4. Reuse (do not reinvent)

- `core.solve_n`, the W solver, freshness/spot-bucket discovery, `RestOrder`, the per-fill-event
  `WingBatch` machinery (PR #62: each rung fill is a fill event with its own batch -- the ladder is what
  that design was built for), `cancel_ctx`, delta booking, `amend_cross_pending` (PR #59), the startup
  cancel sweep (already loops over every resting `v32-*` order), reconcile-first, S1-S5, the day guard,
  the journal + ledger + report spine, the replay lab (`sim/v32_replay`), the shadow.
- `run_v32.py` stays the per-window process; the supervisor wraps it.

## 5. Code scope (estimates; Opus 4.8 builds + reviews as always; Brad merges)

### Phase H -- host-shaped runtime (behaviour-neutral for the live V3.2; ships FIRST) -- ~350 lines
- `service/supervisor.py`: loop = boot sweep (cancel stray `v32-*` rests) -> sleep to next UTC :40 ->
  `subprocess python -m service.run_v32` -> wait -> log line -> repeat; SIGTERM idle/busy handling; a
  `--once` flag for tests. Tests: next-:40 arithmetic (incl. DST irrelevance), boot sweep call, child
  exit codes, SIGTERM.
- `DV3_DATA_DIR`: `journals_v32`, `logs_v32`, `ledger`, `ops` (mode file, day guards) resolve from the
  env when set, else today's `_PILOT_DIR` defaults. Read-only inputs stay in the checkout.
- `DV3_PROXY_BASE` as the default for `--proxy-base` / `ProxyAuth`.
- Proxy: `PROXY_HOST` (default 127.0.0.1), `PROXY_BUDGET_PATH`, `.env` optional, optional
  `X-DV3-Token` shared secret for non-GET. Proxy tests.
- `requirements.txt` (repo root), `.python-version` = 3.12.10, GitHub Action running the pilot suite on
  ubuntu with corpus-dependent tests `skipif` on absent `historical-data/`.
- `ops/register_supervisor_tasks.ps1 -DryRun`: TWO tasks, proxy + supervisor, "run whether user is
  logged on or not", restart on failure, no battery restrictions (Brad registers; replaces
  `DegeneracyV3_2`). `ops/V33_RUNBOOK.md`.
- Leftover-raw journal rotation at wake (the call V3.2 never inherited; today's 632 MB orphan).

### Phase L1 -- ladder core (pure, `service/v33/core.py` forked from v32 core) -- ~600 lines + tests
- `V33State.ladder: tuple[RestOrder, ...]` (K rungs, each with coid/order_id/price/filled), replacing
  `rest_live`/`rest_remaining`; `ladder_top`; `_requote` becomes `_roll` (one amend per cent, strictly
  sequential, amend-first with cancel/create fallback; bucket change = cancel-all/place-all); fill on rung k
  -> fill event -> `WingBatch` (coalesced per Q2); `_rest_size` per rung = 1; n_min truncates the ladder
  from the bottom; the top rung honours the post-only cap. The ladder rests at n_top, n_top-1c ... so its
  realised margins span 5..(E_min+K-1)c, and at LOW W -- where the study's two deepest rungs collapse to
  one price -- the ladder's DISTINCT deepest rung realises up to (E_min+K)c (e.g. +16.03c at the golden
  W=1.4737, vs the label 15c). So "5..15c" is the nominal label range; the realised range is W-dependent,
  and L3 must compute each rung's SOLVED E from `lock_value(price, W_at_fill)`, not the integer label
  (built + reviewed L1 R2, 2026-09-22). Invariant: never more than K live rests, never
  two rests on one price. Golden tests: (a) a full-sweep replay from the 2026-09-20 04:00Z journal fills
  all 11 rungs with the ideal locks from the study; (b) a shallow pump fills 3; (c) a 1c W move rolls
  exactly one order and leaves K-1 order_ids untouched; (d) a 2c move rolls two, in order; (e) bucket
  change re-places K; (f) a fill during a roll (the moving order fills mid-amend -> `amend_cross_pending`
  path per rung).
- `policy/v33_params.json` + sha pin: `E_min` 0.05, `rungs` 11, `lots_per_rung` 1, `tol` 0.01,
  `deb_ms` 5000, `wing_coalesce_ms` 150, `refill_in_window` false, `max_sets_per_hour` 11,
  `replace_rate_alarm_per_min` (per ladder; propose 120), plus everything V3.2 carries.

### Phase L2 -- execution + money math -- ~400 lines
- Executor: K-order bookkeeping (already generic per order_id); roll = amend of one order (existing
  `_amend_rest`) with the fallback; coalesced wing batch; the venue-truth invariant reads "<= K of ours,
  no two on one price"; per-rung fill dedup (WS / poll / cancel_ctx) unchanged per order.
- Money math / ledger: `floor_booked`, `held_legs`, `wing_batch_sets` already per batch; add `rung` and
  `E_rung` to each fill event; `realized_lock` per contract per rung; ladder summary per row (rungs
  filled, shallowest/deepest, contracts, ladder lock).
- Stops: S4 per Q5; S1 per contract; replace-rate alarm per ladder.

### Phase L3 -- report + shadow + ceremony -- ~300 lines
- Report: LADDER SCOREBOARD (per-rung n, fills, mean lock vs solved E, shortfall), ladder capture ratio
  at the 10c rung (comparable to V3.2), pooled per-contract stats; the V3.2 blocks keep printing for the
  V3.2 rows.
- Shadow: the ideal ladder at the same K rungs (the study's fill rule, live), plus one deeper observation
  ladder (16..25c) as SO-3 to measure the deep end before anyone sizes it.
- `ceremony/v33_falsifier.md` DRAFT (section 6); `ops/V33_ARMING.md`; the V3.2 Registration close line
  (Q4).

Total ~1,650 lines + tests, 4-5 PRs. Suite target: current 961 + ~120.

## 6. Falsifier draft (V3.3 roster `DegeneracyV3_3`; frozen only on Brad's verbatim go)

Judged quantity: realised lock PER CONTRACT PER RUNG on live fills vs the in-process ideal ladder.
Proposed gates at n >= 30 rung-fills (n counts contracts; a full sweep contributes K):
- ladder mean true lock >= +6.0c (ideal 9.6c/contract; live V3.2 beat its shadow by 0.6c);
- per-rung shortfall (solved E - realised lock) <= 3.0c at every rung with >= 3 fills;
- % positive >= 80%;
- capture ratio at the 10c rung >= 0.50 (same definition as Registration 3, V3.2-comparable);
- one-legged <= 2 contracts;
- roll integrity: >= 90% of rolls move exactly one order (journal-counted).
Kill: mean lock < +2.0c at n >= 15, or one-legged > 2. Promotion (2 lots per rung, or rungs to 20c):
Brad's dated word after n >= 30, informed by SO-3's deep-end absorption.
Per Brad's sizing philosophy (2026-09-18): n answers slippage and edge-case questions, not a coin flip.

## 7. Order of operations

1. Brad: apply `ops/proxy_amend_cap.md`, raise `DAILY_ORDER_BUDGET` to 8,000, restart proxy (Q6).
2. Phase H PRs (behaviour-neutral); register the two tasks; V3.2 keeps running under the supervisor for
   >= 2 days -- the runtime is proven before the strategy changes.
3. Phases L1 -> L2 -> L3, each Opus 4.8 build + review, Brad merges. Live V3.2 untouched throughout
   (V3.3 lives in `service/v33/`, its own params/ledger/mode file).
4. V3.3 DRY with the K-rung shadow for >= 2 days alongside armed V3.2 (dry sends no orders).
5. Brad freezes the V3.3 falsifier; V3.2 closed (Q4); V3.3 mode -> armed in a :02-:33 window.
6. First armed windows: MUST CONFIRM list (K orders accepted, one-order rolls, coalesced wings sized to
   fills, hand reconciliation of the first full sweep against `/portfolio/fills`).

## 8. Not in V3.3

The Render move itself (env vars and service creation only, after V3.3), ETH or any second series, lots
per rung > 1, rungs deeper than 15c live (observation only), the 15M, refills (Q3), ETH recorder.
