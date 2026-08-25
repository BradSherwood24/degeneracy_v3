# `sim/replay` — engine-time L2 replayer + maker-flip backtest

A reusable order-book **replayer** over the pilot's WS journals (`pilot/journals/2026*.jsonl`),
and a **maker-flip backtest** on top of it. Everything runs in **engine time** (`ts_ms`, the
Kalshi matching-engine clock — verified equal to our own private-fill ts to the millisecond).

## What the replayer guarantees

- **Integer folding.** Prices are carried as **mils** (`round(dollars*1000)`) and sizes as
  **hundredths** of a contract (`round(contracts*100)`). Integer arithmetic is exact: a level
  that returns to zero is removed exactly, so no float dust (a prior float book resurrected
  swept levels via 1e-12 residue). Any level whose magnitude is `< 0.005` contracts is zero.
- **Kalshi binary book semantics**, identical to the authoritative Decimal `pilot/service/book.py`
  `BookMirror`: `yes_dollars_fp` = resting **YES bids**, `no_dollars_fp` = resting **NO bids**;
  **YES ask = 1 − best NO bid** (size = that NO bid's size), **NO ask = 1 − best YES bid**. The
  integer book is asserted equal to `BookMirror` top-of-book on synthetic streams and on a real
  20k-frame excerpt (`sim/tests/test_replay_*`).
- **Snapshot handling.** Snapshots carry no `ts_ms`; a snapshot is an authoritative reset whose
  engine time is back-filled from the next ts-bearing frame.
- **Engine ordering.** Frames are processed in journal (arrival) order; `ts_ms` is used for all
  time math. `ts_ms` is ~99.8% monotone (observed max backstep ~1024 ms), so the chase-ask
  timeline used for arbitrary-engine-time queries is sorted by `ts_ms` before bisect.
- **Selfcheck** (`selfcheck.Reconciler`): every `trade` print is reconciled against a coincident
  (same `ts_ms`, same ticker) **negative** delta — taker_side `yes` at yes_price *p* ↔ negative
  `no`-side delta at `1−p`; taker_side `no` at *p* ↔ negative `yes`-side delta at *p*. Per-window
  reconciliation rate is reported and windows `< 99%` are flagged. Receipt lag
  (`local_ts*1000 − ts_ms`, p50/p99) is printed for information (it includes receipt-clock drift).

## Fill rules (both run; STRICT is a floor)

Legs: **H** = buy NO on `high_ticker`; **L** = buy YES on `low_ticker`. Resting bids sit one cent
under each ask: NO bid on H at `no_ask_H − 0.01`, YES bid on L at `yes_ask_L − 0.01`.

- **STRICT (trade-through).** Our NO bid at *q* fills iff a taker_side `yes` print at yes_price *p*
  has `(1 − p) < q` (the taker paid strictly *through* our level). Our YES bid at *q* fills iff a
  taker_side `no` print at yes_price *p* has `p < q`. Only provable through-trades fill — a lower
  bound on fills.
- **LENIENT (queue).** Additionally, our NO bid fills once cumulative taker_side `yes` prints
  *at* yes_price `1 − q` reach `queue_ahead + 1` contracts (symmetric for the YES bid at prints
  *at* yes_price `q`). `queue_ahead` = displayed size on our side at our price at placement
  (0 when we improve the best bid).

Fill price is our resting price *q* (maker). Taker fee = `fee(price)` **imported** from
`sim/census.py` (never retyped); maker fee = `maker_mult × fee(price)`, `maker_mult ∈ {0.25, 0.0}`.

## Policy (one pair per window, the pilot's sub-$1 FLIP)

Taker-model `C(t) = no_ask_H + fee(no_ask_H) + yes_ask_L + fee(yes_ask_L)`. While active and no
pair has completed: if `C(t) ≤ θ` rest both bids (replace on price change, keep queue position on
no change); if `C(t) > θ` cancel both. On the **first** resting fill (leg A at `t_A`, price `q_A`),
for each `Δ` in the grid — evaluated as counterfactual branches from the same event:
- if leg B's resting bid also fills by `t_A + Δ` → **both-maker**: `cost = q_A + mfee + q_B + mfee`;
- else **chase**: take leg B at its best ask at engine time `t_A + Δ` →
  `cost = q_A + mfee(q_A) + ask_B + fee(ask_B)`, recording `chase_gap = ask_B(t_A+Δ) − ask_B(t_A)`
  and `ask_B(t_A+Δ) − (rest_B + 0.01)`. If `ask_B` is absent → **unhedged**.

Active period: engine time from the first frame where **both** legs have a snapshot until
`close_time − 1 s`. Results are reported for the **full** active period and for the last
`WINDOW_S` seconds (imported from `sim/tape_sim.py`; the pilot only fires in its final window).

## Caveats (what the sim does *not* model)

- **Instant replace/cancel** with no exchange latency; queue position is reset on any price change.
- **No market impact / no partial fills** on the taker chase — leg B's best ask at `t_A+Δ` is taken
  for 1 contract if present (ask size is recorded but not gated on).
- **STRICT is a floor**, LENIENT an optimistic queue model; the truth sits between them.
- **Both-maker is counterfactual**: after leg A fills we freeze placement and let leg B's resting
  order continue; a `C > θ` excursion after `t_A` does not retroactively cancel leg B.
- Nothing here reads `historical-data/` or anything sealed; inputs are WS receipts only, outputs go
  to `sim/out/replay/` (gitignored).

## Run

```
python -m sim.replay.maker_flip --workers 8            # all 2026*.jsonl windows
python -m sim.replay.maker_flip --only 20260824T050000Z --workers 1   # one window (sanity)
python -m pytest sim/tests/test_replay_*.py -q
```

Outputs: `sim/out/replay/maker_flip_<timestamp>.csv` (per-window × θ × Δ × fill_rule × maker_mult ×
period) and `.md` (selfcheck table, aggregate by TRAIN/HOLDOUT, 05:00Z sanity anchor). TRAIN =
`close_time < 2026-08-23T13:00Z`; HOLDOUT ≥ that. Parameters are never chosen on HOLDOUT.
