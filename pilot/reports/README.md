# pilot/reports — wide-box daily report artifacts

The wide-box report (`python -m service.box_report`) writes its machine artifacts here. It is the
instrument that turns fills into knowledge: for every fired box window it records the decision (from
the journals) joined to the actual fills (from the repaired pilot ledger), and it computes — purely
and mechanically — the exact numbers `pilot/ceremony/box_falsifier.md` retires the strategy on.

## Running it

```
python -m service.box_report [--day YYYY-MM-DD | --all] \
    [--journal-dir DIR] [--ledger PATH] [--ops-dir DIR] [--out DIR]
```

- `--all` (default) reports every UTC day found; `--day` reports one day.
- Defaults: journals `pilot/journals/`, ledger `pilot/ledger/pilot_ledger.jsonl`, guards
  `pilot/ops/stops_YYYY-MM-DD.json`, output `pilot/reports/`.
- It prints a plain-text table to stdout AND writes JSON.
- Read-only: it opens no socket, places no order, reads no sealed file, touches no key/PEM/.env.

## Artifacts

- `box_YYYY-MM-DD.json` — one per UTC day that has box activity or balance/guard data. Fired
  windows (with per-leg fills + slippage), the would-fire shadow, the day aggregates, and the day's
  S4.
- `box_cumulative.json` — the aggregates over ALL box fires across the journal set (always spans all
  days, even when `--day` filters the per-day output), plus the per-day S4 summary.

These JSON files are generated artifacts (regenerated on each run) and are not source; only this
README is tracked.

## What the numbers mean

Every retirement / alarm line prints a mechanical status — `NOT YET (n/N)` (the gate count is not
reached), `HOLDING` (reached, the trip condition is not met), or `TRIPPED` (the trip condition is
met). No judgment text. The thresholds mirror the falsifier (Brad's pins 2026-08-26):

- **R1 SLIPPAGE** — after 30 two-leg fills, mean (fill − decided ask) summed over both legs (¢),
  with SE; trips if the mean > +1.0¢. The sharp instrument.
- **R2 ECONOMICS** — after 60 fills (any fill), mean realized per FIRE (¢) with SE and a
  deterministic bootstrap 95% CI; trips if the per-fire mean < −3¢. One-legged flatten losses are
  INCLUDED (they are real money); the two-leg-only "per pair" mean is shown beside it but does not
  drive the stop (coordinator ruling 2026-08-26).
- **R3 PIN RATE** — after 60 fills, pin rate (pinned/settled) vs 0.80; trips if < 0.80. Shown beside
  the 0.90 backtest and the mean implied pin.
- **R4 MATCH** — none of R1–R3 tripped by 100 fills → the candle number stands live-confirmed at 1
  contract. Here `TRIPPED` denotes that positive confirmation, not a stop.
- **A1** — every filled leg whose (fill − decided ask) > 2¢, listed, with a running mean.
- **A5** — one-legged rate over the rolling last 20 box fires vs 0.10.
- **S4** — today's balance_start / latest balance / loss vs $3.00 (from the day-guard file + the
  `s4_balance_check` wake records), whether any stop is latched today, and the
  `ledger_vs_balance_delta`.
- **LEVEL BUMPS** — legs whose fill > the decided ask (Brad: "it's okay if an order bumps up a
  level, we'll note it"), with the distribution of bump sizes.
- **CANDLE STALENESS** — per fire, C paid vs C_mid at decision (paid-vs-mid only on both-filled
  pairs; the decision-time C − C_mid gap for every fire).

Slippage is SIGNED and in side-space: `(fill − decided ask) × 100¢`. The decided ask is the observed
ask the box selected at decision (`box_fire` selection), NOT the margin-adjusted IOC limit.
