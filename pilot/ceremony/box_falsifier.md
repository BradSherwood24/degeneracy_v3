# BOX FALSIFIER — late-window wide box, single pair, taker

STATUS: FROZEN

Frozen 2026-08-26 23:1xZ on Brad's explicit go (verbatim, this session): "Lock it in. Lets send'r
live. And 1 contract pair you mean, right?" — confirmed: 1 contract per leg. The box arming gate
— S5 — checks THIS file, not the corridor's falsifier. From this line down, nothing in this
document may change except appended verdicts in Registration.

Rulings this document encodes (Brad, 2026-08-26, verbatim):
- "Lets make this into a strategy. Try just single pairs takers, keep it simple and see how it
  goes. Crawl before we walk you know."
- "Naw, lets just run it. No two weeks of shadow, no 'Today, no code'. … we only really learn
  anything from trades placed and orders filled. Nothing in this world is free, especially not
  knowledge."
- "Daily cap should be based on account balance btw. Best single source of truth for PnL.
  There's no other strategies ran on this account. All rules look good there, except the
  one-leg rate. Lets take that down to >10%. Also, max price for these should be set well above
  best ask. It's okay if an order bumps up a level, well note it and figure out our way around
  it."

## What is being judged

Whether the +2.6¢/trade candle backtest (n=1,310 hours, pin 0.902 vs implied 0.859, all months
and regimes positive, fragile to 1¢/leg) survives real fills. Two numbers the service measures on
every fill decide it: per-leg fill price vs the ask we decided on, and realized per pair.
Everything else is recording we keep because it is cheap.

## Policy (roster `box-v1`, `pilot/policy/box_params.json`, sha-pinned; loader refuses drift)

15M deep-ITM side (ask ≥ 0.85) + hourly strike behind BTC whose leg mid is nearest 0.95 (tie →
widest gap from A) with ask in [0.90, 0.99]; entry window T−600s..T−60s; first qualifying instant
fires; ONE pair per hour;
1 contract per leg; IOC with limit = observed ask + 0.03, capped 0.99 (a level bump is noted,
not fought). Payoff $2 if BTC settles between the two strikes, $1 otherwise — the pair has a
guaranteed $1 floor; the loss branch is (cost − $1).

## Alarms (notify, keep running)

- A1: fill − decided ask > 2¢ on a leg — visibility; each flagged in the daily report with
  running mean (this IS the measurement).
- A2: ladder-map deviation — alarm only; the box does not stand down on it.
- A5: one-legged fills > **10%** [pin — Brad] of the last 20 box fires. The strategy we priced
  assumed both legs fill; if they don't, we are trading a different thing.
- A_FLATTEN_NO_BID: a one-legged position with no bid to flatten into — held to settlement,
  reported.

## Stops (halt the UTC day; latched in `pilot/ops/stops_YYYY-MM-DD.json`; refuse to arm)

- S1 (arithmetic, n=1): any fill worse than its own limit (impossible for an IOC — a units
  tripwire), or a filled pair whose booked cost with fees > **$1.99** [pin] (`pair_cost_max`).
- S3: any reconciliation mismatch between exchange truth and the local ledger.
- S4: daily loss ≥ **$3.00** [pin — Brad], measured on the ACCOUNT BALANCE: start-of-UTC-day
  snapshot at the first clean wake, compared at every wake (`balance_dollars`, cross-checked
  against the cents field; any parse doubt = refuse to arm). The ledger's realized figure is
  secondary and reported beside it (`ledger_vs_balance_delta`).
- S5 (arming refusal): box roster sha mismatch, this file's STATUS line not exactly
  `STATUS: FROZEN`, proxy /health caps absent or orders disabled, a latched stop or corrupt
  guard file for today, or `strategy.txt` not exactly `box` → never fires an order.
- (No S2. There is no imbalance protocol: no retries of the entry, no rebalance, no I1 ceiling.)

## One-legged fill (the dominant live failure of the corridor: 10 of 11 fires)

Flatten the filled leg immediately, reduce-only, at the current best bid, at any price (Brad's
standing ruling), up to 3 attempts at fresh bids; then hold and alarm. Loss booked to realized
as a round-trip. That flatten is the ONLY order after the entry — a box window emits at most
2 entry legs + 3 flatten attempts.

## Retirement (pre-committed; the daily report computes them)

- R1 SLIPPAGE: after **30** [pin] two-leg fills, mean (fill − decided ask) summed over both legs
  > **+1.0¢** [pin] → the edge is gone at the venue; stop, report, redesign as maker.
- R2 ECONOMICS: after 60 fills (any fill), mean realized per FIRE — one-legged flatten losses
  included — < **−3¢** [pin] → stop. (Coordinator ruling 2026-08-26: one-legged flatten losses
  are real money and belong in the economics; the STATUS is the per-FIRE mean over ALL fires with any
  fill, two-leg + one-legged. The two-leg-only "per pair" mean stays reported beside it but does not
  drive the stop. R1 and R3 stay two-leg-only.)
  Power note: per-pair SD ≈ 28¢ → SE ≈ 3.6¢ at n=60; R2 catches a broken premise, not a small
  edge. The sharp instrument is R1.
- R3 PIN RATE: after 60 fills, pin rate < **0.80** [pin] against the 0.90 backtest → stop, read.
- R4 MATCH: none of R1–R3 by **100** [pin] fills → the candle number stands as live-confirmed
  at 1 contract. Sizing is a new commission.

## Promotion

None inside this document. 1 contract until R4 or Brad's word.

## Pre-arming checklist (mechanical; the runbook repeats it)

1. Working tree on `main`, `git status` clean (the scheduled task runs the checkout, not a tag).
2. `python -m pytest -q` green in `pilot/`.
3. `curl 127.0.0.1:8642/health` → `orders_enabled: true`, caps present.
4. `pilot/ops/strategy.txt` = `box`; two shakedown windows with a `box_would_fire` or a full
   window of `box_eval` records and no `strategy_invalid` / `wake_error`.
5. This file's STATUS line frozen on Brad's verbatim go; roster sha matches the pinned constant.
6. Brad: `pilot/ops/mode.txt` → `armed`.

## Registration

The freeze line, Brad's verbatim go, and every verdict get appended here and nowhere else.

- 2026-08-26 23:1xZ — FROZEN. Brad (verbatim): "Lock it in. Lets send'r live. And 1 contract pair
  you mean, right?" Preceded the same day by: "Yea, lets get crawling here before gettin fancy
  with it" (no stop-loss rule; per-leg <$0.10 stop tested Δ +0.04¢ ± 0.05, not adopted).
  Shakedown: 2 windows (22:00Z would-fire pinned +17.8¢ paper; 23:00Z would-fire missed −83.1¢
  paper), 189 tickers each, zero errors, zero orders. Main @ 275ae55, 544 tests green. Roster sha
  480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3.
- 2026-08-29 — AMENDED to box-v1.1 (implied-pin floor 0.80, S4 pending-settlement band). Brad
  (verbatim): "Go ahead with it. Full review and agent build as before. Let me know when it's
  ready." Roster sha cec4b1a29c5d46deac09fd7a46ec0e08b7603a1f6862758cdb60e97a477aa42c. Tests 604
  green. Details in `## Amendment box-v1.1 — 2026-08-29` below.

## Pre-registered shadow observations (observational; change NOTHING above this line)

### SO-1 — implied-pin floor, registered 2026-08-28 ~15:00Z (Brad: "freeze that finding")
- **Observation that prompted it (post-hoc, 27 live fills):** the tick scan enters at the cheapest
  qualifying instant, so 26% of live boxes had `implied_pin = C_mid − 1 < 0.80` at decision vs 11%
  in the candle corpus. All 5 live misses had implied ≤ 0.78; all 20 boxes at ≥ 0.785 pinned. The
  corpus bucket 0.78–0.80 (n=84) is the only implied bucket with negative EV (−4¢, pin 0.77).
  The split was found in the table, not pre-specified — hence this registration.
- **Rule under observation:** skip a box whose `implied_pin < 0.80` at decision (the pilot would
  keep scanning; a later, dearer instant may qualify). Live behaviour is UNCHANGED.
- **Pre-committed comparison (the report computes it; `SHADOW RULE` block):** at each report, PnL
  and pin rate of (a) all two-leg fills, (b) fills with implied ≥ 0.80 ("kept"), (c) fills with
  implied < 0.80 ("skipped"), plus skipped share. Judged at R2 (60 fills) alongside the primary
  gates. Note the skip-version understates the rule (the live rule may re-enter later at ≥ 0.80);
  a tape replay of skipped hours is the honest upper bound and is a separate commission.
- **Baseline at registration (27 fills):** all −$0.46 (22 pins / 5 misses); kept 16: +$2.33
  (16/0); skipped 11: −$2.79 (6/5); skipped share 41%. Corpus cost of the rule: EV +2.37 → +2.21¢,
  entry rate unchanged (candle sampling).
- **What would make it a roster amendment:** at R2, kept-vs-all PnL difference still favouring
  kept AND kept pin ≥ 0.90 AND the difference not explained by 1–2 fills. Brad's word, dated.
- **What would kill it:** skipped-group pin rate ≥ 0.85 at R2, or kept-group misses appearing at
  the same rate as skipped.

## Amendment box-v1.1 — 2026-08-29 (Brad's go, verbatim)

SO-1 crossed the "roster amendment" bar. Brad (verbatim, 2026-08-28 ~23:30Z): **"Go ahead with it.
Full review and agent build as before. Let me know when it's ready."** This section records the
amendment; nothing above the "change NOTHING above this line" marker changed except the appended
Registration line.

### Evidence (do not re-derive)

Live, 28 two-leg fills (the armed campaign):

| group          | fills | pins/miss | realized |
|----------------|-------|-----------|----------|
| implied ≥ 0.80 |   16  |   16 / 0  |  +$2.33  |
| implied < 0.80 |   12  |    6 / 6  |  −$3.60  |

Candle corpus (1,289-hour backtest, per-fill cents):

| variant                         | fills | ¢/fill |
|---------------------------------|-------|--------|
| all fills (v1)                  | 1,024 | +2.37  |
| literal skip-the-hour (≥ 0.80)  |   912 | +2.61  |
| keep-scanning (re-enter ≥ 0.80) | 1,017 | +2.21  |

Literal skip-the-hour is the better-supported variant and is what Brad approved.

### Policy delta (roster `box-v1` → `box-v1.1`)

- New pin **`min_implied_pin` = 0.80** in `pilot/policy/box_params.json`. A box whose
  `implied_pin = C_mid − 1` (fee-free mids) is **below** the floor at the FIRST qualifying instant
  is **SKIPPED for the hour** — literal skip-the-hour, **no orders** (a skip is a skip, even in
  shakedown). Equality (implied == 0.80) FIRES.
- The skip is journaled `box_skip_implied` with the full selection payload (side / K / A /
  implied_pin / C_decision) plus `implied_pin` / `min_implied_pin` / `t_minus_s`, so the report
  **counts** it live as a paper skip. NOTE: SETTLEMENT SCORING of a paper window requires a
  settlement source for markets we did NOT hold (an un-traded skip has no ledger row and so no
  backfill row) — a follow-up commission (a read-only, report-side resolver that fetches the settled
  result for the selection's markets). Until then the SO-1 paper groups show COUNTS and `unsettled`;
  the scoring plumbing (`payoff − C_decision`) is in place for the moment such a source exists.
- **Rescan paper record:** after a skip, the FIRST later instant that re-qualifies (`implied ≥ 0.80`)
  inside the entry window emits ONE paper `box_rescan_would_fire` — never an order. It RECORDS, on
  live tape, that the skipped hour would have re-qualified (the literal-skip-vs-keep-scanning
  question, SO-1's open caveat); its settlement PnL waits on the same paper-scoring source as skips.
- New roster sha **`cec4b1a29c5d46deac09fd7a46ec0e08b7603a1f6862758cdb60e97a477aa42c`**
  (old box-v1 sha `480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3` retained in code
  as `BOX_V1_POLICY_SHA256` for the report's roster partition). The loader self-verifies the new sha
  and requires `min_implied_pin` (fails closed if absent).

### S4 measurement amendment (pending-settlement band)

The 2026-08-28 **16:40Z** false latch: a box had PINNED but its 15M leg
(`KXBTC15M-26AUG280800-00`, window 12:00Z) stayed unfinalized at Kalshi from 12:40Z to 16:40Z. The
account balance — and the ledger, which books only the $1 floor until backfill — both read **$1
worse than truth**, so raw loss 3.5453 > cap 3.00 latched S4. Real loss at that instant was $2.55 <
cap: a **measurement error, not a strategy loss**.

Fix (never latch on a number a pending settlement can still move): S4 now bounds the loss under
EVERY resolution of the unfinalized legs. `pending_value` = optimistic $1 per unfinalized leg whose
`close_time` is today. `loss_pessimistic = start − now`; `loss_optimistic = start − (now +
pending_value)`. **latch** iff `loss_optimistic ≥ cap` (breached under every resolution); **clear**
iff `loss_pessimistic < cap`; else **pending** — stand down THIS window (degrade to dry), journal
`s4_pending_settlement`, write **no** day-guard latch; the next wake re-evaluates. The 16:40Z case
(pessimistic 3.5453, optimistic 2.5453) is now `pending`, not a latch. Bundled LOW fix:
`_day_totals` is recomputed AFTER the settlement-backfill sweep so `ledger_vs_balance_delta` no
longer shows the backfill timing artifact. The S4 cap ($3.00) and every other pin are unchanged.

### Gate restart

- **R1–R4, A1, A5** count **box-v1.1** fills from the first v1.1 window. **box-v1** closes at its
  final counts as a **LEGACY** line in the report (frozen; not counted in the live gates).
- **SO-1** continues over BOTH rosters and now includes the v1.1 **paper skips** (the SKIPPED group
  reports `n_real` = v1 fills with implied < 0.80, `n_paper` = v1.1 skipped windows — counted live;
  their settlement PnL awaits the paper-scoring source noted above) and a paper **rescan** group.
- Nothing else about the box changed: 1 contract per leg, `pair_cost_max` $1.99, the entry window,
  the one-legged flatten, the S1/S3/S4/S5 pins, and the R1–R4/A5 thresholds all stand.
