# V3.2 money-math fee-scaling fix — build report

**Date:** 2026-09-20 · **Branch:** `fix/money-math-fee-scaling` · **Builder:** Opus 4.8
**Brad's go (verbatim):** "Yea, lets dot our Is on that. Go ahead with that fix. Prevent any confusion in the future that we can."

## The bug (reproduced on the live ledger)

Live ledger row `close_time 2026-09-20T04:00:00Z` — the FIRST size-2 set:
- rest NO 2 @ 0.27, maker, fee 0
- wing YES `KXBTCD-26SEP2000-T80399.99` @ 0.82 × 2, per-contract fee 0.0103
- wing NO `KXBTCD-26SEP2000-T80499.99` @ 0.77 × 2, per-contract fee 0.0124
- settlement 04:02:34Z = $4.00; balance 53.3069 → 53.5414 = **+0.2345**

`service/run_v32.py::_compute_money_math` summed the cost as `cost += price*count; if fee: cost += fee`
— the per-contract `fee` was added ONCE instead of scaled by count. So the second lot's wing fees were
dropped, over-stating `realized_delta` as **0.2573** (over by **$0.0228** = 0.0103 + 0.0124).

## Fee-meaning trace (per fill path)

`fee` on a fill record is Kalshi's `average_fee_paid` (PER CONTRACT) on every fee-bearing path; the
maker rest leg is fee-free (0). Where a venue TOTAL is present it is `fee_cost` (WS frame), which for the
maker rest leg is 0. Summary, one line per path:

| path | code site | what `fee` holds | scale |
|---|---|---|---|
| rest, WS `fill` channel | `run_v32.py::_record_fill` (fee = frame `fee_cost`) | venue per-FILL total; = 0 (maker, post_only) | total (but always 0) |
| rest, 1 s status poll | `run_v32.py::_record_fill` (fee = None) | None → treated as 0 | n/a |
| rest, cancel-race booking | `executor.py::_finish_cancel` (fee = `Decimal(0)`) | 0 (maker) | n/a |
| rest, amend CROSS (taker) | `executor.py` amend-fill (fee = `average_fee_paid`) | per contract | per contract |
| wing batch (IOC taker) | `executor.py::_wing_events` (fee = `r.average_fee_paid`) | per contract | per contract |

Conclusion: `fee` is **per-contract on every fee-bearing (taker) path**; the only non-per-contract
source is the WS rest `fee_cost` (a venue total) which is always 0 because the rest leg is a post_only
maker. Corroborated by `service/ledger.py::_fee_of` (`FEE_IS_TOTAL = False` → multiplies by fill_count)
and by the venue `/portfolio/fills` truth on this row (0.0103×2 ≈ 0.0207, 0.0124×2 ≈ 0.0248).

The venue applies the $0.0001 ceiling ONCE PER FILL: `fee_total = ceil(0.07·p·(1-p)·count, $0.0001)`
(MEMORY kalshi-fee-exact, 169 fills). This is NOT `per_contract × count` — the per-contract fee already
rounded up once, so ×count mis-rounds (YES @0.82: law-per-contract 0.0104, venue total for 2 = 0.0207,
not 0.0208; the stored average 0.0103 ×2 = 0.0206). At count 1 the two coincide.

## The fix

`service/run_v32.py`:
- `_fee_total(price, count)` — venue per-fill fee `ceil(0.07·p·(1-p)·count·1e4)/1e4`, built on the frozen
  `FEE_RATE` re-exported from `service/_simlaw.py` (`fee_rate`); equals the frozen per-contract `_fee` at count 1.
- `_fill_total_fee(f)` → `(total, source)`: maker (`fee` 0) → `(0, "maker_zero")`; taker count 1 → the
  per-contract fee itself `(fee, "per_contract")` (keeps size-1 history byte-identical); taker count ≥ 2
  → `(_fee_total(price,count), "law_total")`.
- `_annotate_fee(f)` — a COPY of each fill record with `fee` (unchanged, per contract) + explicit
  `fee_total` (all lots) + `fee_source`. `fills` and `wing_fills` on the row now carry all three.
- `_compute_money_math` cost loop now adds `_fill_total_fee(f)[0]` (total) instead of `fee` once.

`service/v32/ledger.py`:
- `REALIZED_DELTA_NOTE` stamped on every armed row (`realized_delta_note`): "floor − total cost incl.
  all fees × count …; per-set truth = balance delta at settlement; falsifier reads realized_lock (per
  contract, core state), not this field."

`realized_lock`, the floor geometry (`v32_set_floor_dollars`), `lock_value`, and everything the
falsifier scoreboard reads are UNCHANGED (verified: `realized_lock` still 0.1034 on this row).

## 04:00Z reconstruction (regression test + live-row recompute)

```
contract cost = 2*(0.27+0.82+0.77) = 3.72
fees (venue per-fill)  = _fee_total(0.82,2)=0.0207 + _fee_total(0.77,2)=0.0248 = 0.0455
total cost = 3.7655 ; floor = v32_set_floor_dollars(3,2) = 4.00
realized_delta = 4.00 - 3.7655 = 0.2345   (== balance move 53.3069 -> 53.5414)
```
`test_size2_0400z_realized_delta_uses_total_fees` asserts `realized_delta == Decimal("0.2345")` and the
`fee`/`fee_total`/`fee_source` triple on each fill.

## Size-1 history unchanged (evidence)

`_compute_money_math` needs a live `V32State` + executor, so it cannot be driven directly from row JSON;
instead the ONLY changed quantity (the summed fee) was recomputed over the read-only live ledger copy
via `_fill_total_fee`, and the count-1 arithmetic proven a no-op:

```
close_time             cnt  old_feesum  new_feesum  delta_cost
2026-09-15T00:00:00Z     1    0.024000    0.024000   0.000000  (unchanged)
2026-09-15T17:00:00Z     1    0.024500    0.024500   0.000000  (unchanged)
2026-09-16T05:00:00Z     1    0.017000    0.017000   0.000000  (unchanged)
2026-09-17T22:00:00Z     1    0.018100    0.018100   0.000000  (unchanged)
2026-09-18T00:00:00Z     1    0.014800    0.014800   0.000000  (unchanged)
2026-09-18T04:00:00Z     1    0.028200    0.028200   0.000000  (unchanged)
2026-09-19T22:00:00Z     1    0.023500    0.023500   0.000000  (unchanged)
2026-09-20T04:00:00Z     2    0.022700    0.045500  +0.022800  <-- FIX
```
`test_size1_single_lot_realized_delta_byte_identical_to_pre_fix` re-derives the exact pre-fix cost
(per-contract fee added once) for a 1-lot set and asserts `realized_delta` is identical, and that every
count-1 `fee_total` equals its per-contract `fee`.

## N1 / N2 docs

- `executor.py::_amend_body` docstring: `count` now described as `action.count` (fixed-point, defaults
  to "1.00"), not a hard `"1.00"`.
- `executor.py` amend-fill comment: "with count 1 nothing remains resting" → describes the resting
  remainder (`parsed.remaining_count`) after a partial cross.
- `PLAN_V32.md:210`: sha `c6715fc7…` → `a2a58787…` with the AMENDMENT-1 / prior-freeze note.
- `PLAN_V32.md:54`: "Size: 1 contract" → "2 contracts (AMENDMENT 1 …; was 1 at the 2026-09-14 freeze)".
- Repo grep for `c6715fc7` (excluding `pilot/build/*.md`, `sim/out`): only `PLAN_V32.md` carried it.
- Repo grep for "1 contract" as current fact: `ceremony/box_falsifier.md` (BOX strategy — not V3.2,
  untouched), `ceremony/v32_falsifier.md` (FROZEN — untouched per the brief), `PLAN_V32.md:183` (a
  Phase-4 description of the falsifier draft's own terms — left, it correctly reports the frozen doc).

## Files touched

- `pilot/service/_simlaw.py` — re-export `fee_rate`
- `pilot/service/run_v32.py` — `_fee_total`, `_fill_total_fee`, `_annotate_fee`, cost fix, docstring
- `pilot/service/v32/ledger.py` — `REALIZED_DELTA_NOTE` + `realized_delta_note` row key
- `pilot/service/v32/executor.py` — N1 docstring + comment
- `pilot/PLAN_V32.md` — N2 sha + size line
- `pilot/tests/test_v32_partial_fill.py` — 3 regression tests

## Suite

`cd pilot && python -m pytest -q` → **944 passed** (baseline 941 + 3 new), 0 failures. Not touched:
`ceremony/v32_falsifier.md`, `policy/v32_params.json`, `params.py`.
