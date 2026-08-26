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
