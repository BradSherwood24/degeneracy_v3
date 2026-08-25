# Phase 2 build report — signal engine + parity harness (opus48 BUILDER)

Built 2026-08-21. Location: new files only under `pilot/policy/`, `pilot/service/`, `pilot/tests/`.
No existing Phase-1 file (or its tests) was modified; no `sim/` file modified; no `.env`/`*.pem`
read; **no sealed-day file read by any code path** here (the sim law enforces its own seal, and the
quintile reproduction test loads only TRAIN market files 2026-06-11..21). All the frozen law is
IMPORTED from `sim/` via one bridge module, never reimplemented.

## Component inventory

| # | File | Component | ~Lines |
|---|------|-----------|-----|
| 1 | `policy/policy_params.json` | frozen pilot roster (sha-pinned config) | 25 |
| 2 | `service/policy.py` | policy loader: canonical-sha, Decimals, fail-closed refusal | 150 |
| 3 | `service/_simlaw.py` | single import choke-point to the frozen sim law | 75 |
| 4 | `service/signal.py` | **the pure core** `decide(params, state, event)` | 300 |
| 5 | `service/quintile.py` | σ̂ + G/σ quintile at wake, reproducing census (A3.2) | 200 |
| 6 | `service/parity.py` | five-bin paired-replay harness | 320 |
| 7 | `service/parity_report.py` | CLI over `run_parity` (manifest → JSON + text) | 120 |
| 8 | `service/shakedown.py` | WouldFire-only runner composing WindowRecorder | 160 |
| 9 | `pilot/tests/test_{signal,policy,quintile,parity,shakedown}.py` | 49 tests | — |

## Tests

`cd pilot && python -m pytest tests/` → **165 passed, 0 failed** (~2.9s). Of these: Phase-1 spine
110 + reviewer probes 6 (both untouched by me) + **49 new**: signal 19, parity 17, policy 6,
shakedown 4, quintile 3.

New-test inventory:
- **signal (19):** fee golden literals; flip/strangle C hand-computed exact; missing-ask→None;
  sub-$1 **strict-<** boundary (C==C_max → no fire; C_max+ε → fire); EV−5 boundary (ev==0.05 →
  fire, ev==0.0499 → no fire); strangle only in q0; strangle stood down by ladder; unknown-age
  fail-closed; stale-leg fail-closed; suspect-book fail-closed; future-ts fail-closed; sub wins
  same-event tie; window entered once then done; no-orders cutoff → StandDown (once); warmup
  (t>WINDOW_S) no-fire; unrelated leg ignored; shakedown → WouldFire; **golden determinism** (same
  stream twice → identical actions).
- **parity (17):** streaming close_time filter + header check; sim-side sub-$1, strangle-q0, stale
  rows, earliest-moment / sub-tie; live-side flip fire + no-fire; bins **1** (sim-only, F15
  FAILURE via book-vs-prints), **2** (live-only, FAILURE), **3/4/5** (no-fill / diff-price /
  match), **imbalance**, **no-signal-both**, **both-fired** + source-mismatch flag; mixed
  aggregate passed=False.
- **policy (6):** default self-verify sha + Decimals; roster shape (q0 both, q1–q4 sub only,
  OOR→()); imbalance bounds; canonical-sha key-order/whitespace invariance; wrong-sha refusal;
  tampered-file refusal + `expected_sha=None` bypass.
- **shakedown (4):** state seeding; SignalDriver WouldFire + Decimal-lossless journal record;
  ShakedownRecorder composes WindowRecorder (books built + signal fires) without modification;
  unrelated ladder strike ignored.
- **quintile (3):** edges == gate.json; **≥20-window exact reproduction across all 5 quintiles**;
  head-of-corpus insufficient tape → NoQuintile.

## The frozen roster as config (`policy/policy_params.json`)

Canonical sha256 = `1b01fd98e1c76748261fbe80f961d9ae8a55853c7807de71af508eece8203656` (over
`json.dumps(sort_keys=True, separators=(",",":"))`; whitespace/key-order invariant). `load_policy()`
defaults `expected_sha` to this pin and **refuses** (`PolicyShaMismatch`) on any drift — the S5
discipline mirrored from the census-sha refusal. Roster: `sub_dollar_C_max="1.00"`,
`q1_strangle_ev_min="0.05"`, `freshness_max_leg_age_s=1.0`, `staleness_s=60`, routing q0 =
{Q1-strangle, sub$1-flip}, q1–q4 = {sub$1-flip} (NO flip-EV anywhere). Imbalance bounds carried in
the SAME file for Phase 3: `pair_cost_ceiling_sub1="1.0320"`, `max_retries_per_side=5`,
`no_rebalance_after_s_to_settle=3`, `no_orders_after_s_to_settle=1`.

## The sim → live semantic mappings (each is a named live/sim delta)

1. **C from ASKS, not prints (structural delta).** The sim priced C from the most-recent same-side
   taker PRINT (honest-fills law). Live prices C from the current best ASK we can cross:
   flip C = (high-leg NO-ask + fee) + (low-leg YES-ask + fee); strangle C = (high-leg YES-ask + fee)
   + (low-leg NO-ask + fee). `fee` and the EV curve are IMPORTED from `sim/census.py` /
   `sim/tape_sim.py` (no retyped constants). "high leg" = higher-strike market. This book-vs-prints
   C is the headline structural delta the parity harness reports; a window where it flips a
   fire/no-fire decision is an **F15 FAILURE**, not a note (bin 1 / bin 2).
2. **A15.9 / A15.10 → freshness + not-suspect.** Cross-side refutation and dwell are PRINT-tape
   concepts (a carried price dies when refuted; dwell = how long it stood). A live book has no
   carried price — the ask is current by construction. Faithful mapping: a fire requires the two
   legs SIMULTANEOUSLY FRESH (both ages ≤ `freshness_max_leg_age_s` = 1.0s) and neither book
   `suspect`. Stated in `signal.py` and flagged as a named delta.
3. **Staleness horizon.** The sim's 60s leg-live horizon is the outer bound; the 1s freshness gate
   binds and subsumes it for firing. Unknown age (a leg never updated) or a future-stamped frame →
   age unknown → STALE → never fires (fail closed).
4. **Window.** Only the final `WINDOW_S` (900s, imported) is an entry window; earlier events seed
   book state but never fire, exactly as the tape sim evaluates only T-900..T. Confessed as the
   half-open `[T-900, T)` with the settle cutoff at t-1s.
5. **Entry per window (mutual exclusion).** First qualifying source fires (WouldFire in shakedown,
   Fire live), then the window is DONE for BOTH sources — "both sources can't fire; first
   qualifying wins (race by event order)". Same-event tie → sub-$1 flip priority (the arithmetic
   floor). The parity sim-side mirrors this over the tape rows (earliest t_minus_s wins; tie→sub).

## σ̂ reproduction (A3.2) + verification numbers

The sim's σ̂ anchor tape is NOT candles — it is the 15M-market FLOOR-STRIKE sequence at the 9
contiguous epochs T, T-900, …, T-7200; σ̂ = sample stdev (ddof=1) of the 8 consecutive diffs
(`census.sigma_hat`, imported). G = `hole_G(A,K)` with K the NEAREST hourly floor strike to A
(ties → excluded, as census does); quintile = `bucket_of(G/σ̂, edges)` with edges =
`quintile_edges` over census_train OK rows. `quintile.compute_window_stats` reproduces all of this
from `/markets`-shaped inputs.

Verification (`test_quintile.py`, TRAIN markets 2026-06-11..21, windows 06-13..06-20):
- edges = `[0.1014128, 0.20857860000000003, 0.3440502, 0.5661136]` — **exactly** gate.json's
  `quintile_edges_gos`.
- ≥20 windows spanning all five quintiles: quintile assignment, G (2 dp), σ̂ (6 dp) and G/σ̂ (6 dp)
  each match `census_train.csv` **exactly** (string-equal on the CSV's formatted fields). In a
  25-window prototype 24/25 matched; the 25th (2026-06-13T01:00Z) correctly returned NoQuintile
  when the trailing tape wasn't loaded — the test loads from 06-11 so every tested window has its
  T-7200 anchor, and head-of-corpus insufficient tape is asserted separately to yield NoQuintile.

## Parity harness (five bins + F15 neutrality gate)

`parity.run_parity` compares, per window: the LIVE decision (decide() over the Phase-1 journal
replayed through `BookMirror`, the exact book live saw — reusing the frozen snapshot/delta
semantics) vs the SIM decision (pilot roster applied to a `tape_points.csv` slice for that
close_time, streamed once — the 3GB full-run file is handled by a streaming filter with a header
check against `tape_sim.TAPE_FIELDNAMES`). Bins: 1 sim-only, 2 live-only, 3 both/no-fill, 4
both/diff-price, 5 both/match, + imbalance + no-signal-both; bins 3–5 require a per-window fills
record (Phase 3 supplies it; absent ⇒ shakedown, bins 1–2 only, both-fired left "pending fills").
**F15 gate:** any bin-1/bin-2 fire/no-fire disagreement — including one caused by the documented
book-vs-prints C delta — is reported as a `neutrality_ok=False` FAILURE, and the harness `passed`
iff there are none. `parity_report.py` drives it from a manifest (TRAIN/synthetic only) and writes
JSON + a human text block; smoke-run end-to-end (both-fired window → passed, exit 0).

## CONFESSIONS (judgment calls)

1. **σ̂ input is floor-strikes, not candles.** The task framed the σ̂ input as "REST candle data",
   but census's A3.2 anchor tape is the 15M floor-strike sequence, so the faithful reproduction
   reads `/markets` (floor_strike per co-settling 15M window), NOT candles. For a live wake the 8
   trailing 15M markets have already settled; whether `/markets` returns them by close-ts after
   settlement (or needs a status-less query) is a **live-verification item**, and fetching that
   trailing tape is a Phase-4 wiring point (WakeContext fetches only the co-settling close).

   **UPDATE (live finding 2026-08-21, shakedown run 2 — σ̂/anchor TIMING):** the σ̂ tape and the
   anchor A(T) do NOT become available at the same time. The 8 TRAILING anchors (T-900..T-7200) are
   present with strikes at the :40 wake, but A(T) — the current window's anchor — is BTC spot at
   window open and does not exist until the co-settling 15M market flips `"initialized"→"active"` at
   `open_time` (~:45); an `"initialized"` market carries NO `floor_strike`. So `compute_window_stats`
   / `sigma_feed.assign` CANNOT be run at :40: `_anchor_at` returns None → `EXCL_NO_ANCHOR`, which as a
   single-phase :40 call stands every window down. Phase 4 resolved this by making the wake TWO-PHASE
   (see `phase4_harness_review.md` item 3a): PHASE A at :40 PREFETCHES the trailing tape via
   `SigmaFeed.fetch_trailing_15m` (the settled/near-settled anchors this confession worried about ARE
   returned by `/markets` by close-ts — confirmed live), and PHASE B at leg open POLLS the current 15M
   market for its strike, then calls `sigma_feed.assign(..., current_15m_markets=<polled>,
   trailing_markets=<prefetched>)`. The `trailing_markets` kwarg is the one backward-compatible
   addition to `sigma_feed.assign` (when None it fetches as before — the single-phase path used by the
   parity/quintile tests is unchanged). `quintile.compute_window_stats` itself was NOT modified; its
   `nearest_hourly_strike` already reproduces census `hole_G`/nearest-threshold selection (candidates
   both below and above A, ties → `EXCL_NEAREST_TIE`), verified on the live example anchor 77315.17 →
   77299.99 on a $100 ladder.
2. **Same-event tie-break = sub-$1 flip priority.** When both sources qualify on one event (only
   possible in q0), the arithmetic floor (sub-$1 flip) wins. Chosen because it's the riskless-if-
   filled leg; documented and tested.
3. **Window-level mutual exclusion.** I read "both sources can't fire — first qualifying wins" as
   at-most-one entry per window (you place one pair; strangle+flip together would be all four legs).
   Once any source fires, the window is `entered` and done for both. The sim-side parity mirrors it.
4. **Entry window `[T-900, T)` + cutoff.** decide() gates firing to `t_minus ≤ WINDOW_S` and
   `t_minus ≥ no_orders_after_s_to_settle` (1s); inside 1s → StandDown (emitted once). The sim only
   walks T-900..T, so this matches; the exact `<`/`≤` on the 900 edge is a confessed choice.
5. **Freshness (1s) binds; 60s staleness is the outer sim horizon.** Both are enforced conceptually
   but the 1s gate is strictly tighter, so it decides firing. `staleness_s` is carried in the state
   and config for completeness / Phase-3 reconcile use.
6. **StandDown scope.** StandDown is emitted only for the settle-time cutoff. Freshness / staleness
   / suspect / missing-ask failures produce NO action (silent, transient — a later fresh update may
   qualify), matching "fail closed → no order" without noise.
7. **Depends on a Phase-1 private helper.** `parity.py` and `shakedown.py` import
   `ws_client._parse_server_ts` to recover a frame's server ts for freshness. This is a read/import
   (composition), not a modification of any Phase-1 file; noted so a future rename is traced here.
8. **Live-side server_ts fallback.** When a journal frame carries no server ts, the harness falls
   back to the record's `local_ts`. Real orderbook deltas carry `ts` (V2-verified), so this is a
   defensive fallback only.
9. **Fills schema is minimal.** `WindowFills`/`LegFill` (filled / legs[avg_price] / imbalance /
   realized_payoff) is a placeholder for the Phase-3 fill record; bins 4/5 compare live avg fill
   vs the sim tape's per-leg price by ticker. Phase 3 will supply the real record and
   `average_fee_paid`.

## Interface blocks against Phase 1 code

None. Phase-1 interfaces were sufficient: `BookMirror`/`TopOfBook` (asks + suspect flag),
`Journal`/`load_journal` (replay records), `replay` semantics (mirrored, not modified), `WakeContext`
`Leg`/`WakeResult`/`LadderCheck` (state seed + shakedown compose), and `ws_client._parse_server_ts`.
The one wiring gap (trailing-15M fetch for σ̂) is a Phase-4 concern, confessed above, not a Phase-1
blocker.

## How to run

```
cd pilot
python -m pytest tests/                       # 165 passed
python -m service.parity_report --manifest <manifest.json> --out-json <report.json> --out-text <r.txt>
```
Manifest schema is documented in `service/parity_report.py`. Shakedown wiring (`ShakedownRecorder`)
composes with `record_window` and runs WouldFire-only; Phase 4 supplies the trailing-tape fetch and
the Task-Scheduler window lifecycle.
