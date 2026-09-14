# V3.2 Replay Lab (`sim.v32_replay`)

A research tool that re-runs the V3.2 pump-fader strategy and the forward sim's pricing assumptions
over the millisecond journals `run_v32` records every hour, and reports **optimistic / base /
pessimistic** performance estimates that firm up as journals accumulate.

```
python -m sim.v32_replay.lab --journals <dir> [--since YYYY-MM-DD] [--out <dir>]
                             [--calibrate-from <dir>] [--apply-to-forward] [--range-loader <dir>]
```

* `--journals` default `pilot/journals_v32` (relative to the repo root). Reads **only** `*.jsonl.gz`
  — never the live `.jsonl` a window is still writing.
* `--since YYYY-MM-DD` keeps only windows whose close date is on/after that day.
* `--out` default `sim/out/v32_replay/` (gitignored). Writes `report_<YYYYMMDD>.md`,
  `per_window.jsonl`, `fills.jsonl`, `calibration.json` (and `forward.json` with `--apply-to-forward`),
  and prints the report to stdout.
* `--calibrate-from <dir>` the journals dir used for the calibration stage (default `--journals`).
* `--apply-to-forward` also runs the corrected sim over the forward 139 h (2026-08-30..09-04) —
  reads `historical-data/tob/` + `historical-data/1-hour-range/` via the guarded scratchpad
  `range/rangelab.py`. The forward days are post-holdout; the loader guard refuses 2026-08-20..29 and
  the lab never passes `oos=True`.
* `--range-loader <dir>` path to the scratchpad `range/` dir (holds `rangelab.py`) for the apply stage.

## What it does

Per window it reconstructs, **from the raw frames only** (every decision record is ignored except
`window_meta`), a per-strike / per-bucket / 15M `BookMirror` and the trade stream, then:

1. **Book metrics** (T-15..T-5): bucket-book tick rate (median inter-update gap, overall and for the
   spot bucket), spot-bucket spread + top depth, strike-book tick rate, bucket-trade counts, and the
   15M frame share.

2. **Two fill models on the ms feed**, both the rules of `scratchpad/journals/pf_ms_requote2.py` but
   sourced from the **ms bucket book** instead of the minute candle:
   * *ideal / no-lag* (`IdealModel`, the OPTIMISTIC shadow): re-solve `n` every book tick, no requote
     gate; a spot-bucket YES print strictly above the offer `1 − n` fills once; completion at the wing
     asks **at the trade tick**.
   * *lagging executor* (`LaggingModel`): replace only when `|Δn| ≥ TOL` and `≥ DEB ms` since the last
     replace; the new quote goes live `+LAT ms`; fill on a spot-bucket YES print above `1 − n_rest`;
     completion at the wing asks at **trade + 1.5 s**. Grid `E ∈ {0.08, 0.10, 0.12}`,
     `TOL ∈ {0.01, 0.02, 0.03}`, `DEB ∈ {0, 2000, 5000}`, `LAT = 200 ms`. Each fill also carries a
     `book_swept` flag (the bucket's best YES ask before the print was ≤ our offer) and the
     through-print size.

   Spot selection, `W`, `cap` and `n` mirror the live core (`_select_spot` / `_compute_W` /
   `_bucket_cap` / `solve_n`); the money math is the **pinned law** (`service.v32.core.solve_n` /
   `wing_cost` / `lock_value`, `service._simlaw.fee`) — never retyped.

3. **SIM-vs-MS comparison**: the OLD pricing model (spot + cap sampled at each **minute boundary**, the
   minute-candle equivalent; wings from ms strike books, completion at print − 1 s) runs at the base
   cell alongside the MS base cell. The report gives how often the ms-continuous spot differs from the
   last minute sample, the cap drift (cents), base-vs-old fill/lock differences, and the wing-drift
   residual `W(trade) − W(trade + 1.5 s)`.

4. **Estimates**:
   * **OPTIMISTIC** = the ideal no-lag rule (E=0.10), completion at the ask.
   * **BASE** = the lagging base cell (E=0.10, TOL=0.02, DEB=5000), **book-swept** fills only.
   * **PESSIMISTIC** = BASE − 1 tick per wing at completion (2c off the lock), dropping fills whose
     through-print size < 2 lots, plus a replace-budget haircut (a window's fill is dropped when its
     base-cell replaces exceed the per-window proxy budget, `DAILY_ORDER_BUDGET / 24`).

   Each reports fills/day, mean/median/p10/min lock (cents), % positive, c/day, `n`, the number of
   windows, and how many windows are needed for a ±2c band on the mean (SE from the observed sd; 3.6
   fills/day assumed when n < 5).

## Calibrate + apply (the ms books as a calibration set)

The ms bucket books calibrate the sim's **bucket-side** assumptions (the sim priced buckets off minute
candles). `sim.v32_replay.calibration` pools, from every journal window (T-15..T-5), at each
spot-bucket YES trade: (a) spot-bucket agreement (candle proxy vs ms), (b) cap error
`cap_ms − cap_candle`, (c) `P(swept | print rule)` (of yes prints above the offer, the fraction where
the bucket's best YES ask was already ≤ the offer) + the print-size distribution, (d) the wing residual
`B = W(trade + 1.5 s) − W(trade)` on live timing, and (e) replaces/window vs the sim's ~77.

`sim.v32_replay.forward` then re-runs `pf_ms_requote2.py`'s lagging model over the forward 139 h at
E ∈ {0.08, 0.10, 0.12}, TOL 0.02, DEB 5000, and reports **OPTIMISTIC** (uncorrected sim), **BASE**
(cap shifted by the mean cap error, fills thinned by `P(swept)`, lock reduced by the mean live B minus
the sim's own tape B) and **PESSIMISTIC** (cap at the p10 error, fills thinned by the p10 of `P(swept)`
and a 2-lot minimum print, lock reduced by the p90 B and one extra tick per wing). Re-runnable: the same
command tomorrow, with more journal windows, tightens every correction.

## Design notes / deliberate deviations

* **No freshness gate.** The lab reproduces the sim's rule set (`pf_ms_requote2` reads the nearest
  book with no freshness gate) so the MS-vs-OLD comparison isolates the *data source*. Book freshness
  (tick rates) is **reported as a metric**, not used to suppress quotes. The live core's freshness
  gate is a separate, stricter safety layer that this measurement tool intentionally does not apply.
* **Completion by streaming lookback.** Each fill's completion `W` is priced as-of its target ts from a
  short rolling history of strike tops (≈12 s), matching the sim's `at()` (last book ≤ target). A fill
  is only accepted when its wings are priceable at the trade tick (the sim's "skip if W2 is None").
* **Maker-fee conservatism.** `lock_value` charges the audited taker fee on the resting (maker) leg to
  stay bit-identical to the pinned sim; Kalshi crypto maker fee is 0, so the *realized* lock is ~fee(n)
  (~1.7c) HIGHER than reported — the conservative direction.
* **Armed windows** are replayed and flagged; our own prints would be excluded from the fill rule by
  client-order id when identifiable (the current journals are all dry/shakedown — no real orders).
* **Memory.** One journal at a time, one record at a time; 15M books are counted, never folded (they
  are ~35–47% of frames). A full window is ~1.3 M records / 25 MB gz and replays in ~90 s.

## Tests

```
python -m pytest -q sim/v32_replay/tests
```

Uses the committed head fixture `pilot/tests/fixtures/v32/live_window_20260914T170000Z_head.jsonl.gz`
(no network): frame reading + sealed-date refusal, spot/W/cap/solve_n parity with the core, the fill
models' gate/promotion/fill rule, a deterministic end-to-end replay, the estimate stats, and the CLI.
