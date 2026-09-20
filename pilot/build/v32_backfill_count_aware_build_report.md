# V3.2 ledger: count-aware settlement backfill + S4 pending-credit band

Builder: Opus 4.8 (Fable's delegate). Branch `fix/backfill-count-aware`. Brad's go 2026-09-20 ~12:45Z:
"Gotcha, yea go ahead and build it". Falsifier (`ceremony/v32_falsifier.md`) FROZEN and untouched; the
$3.00 S4 pin and floor-netting RULING stand. This is a mechanics fix in the band/backfill arithmetic.

## Root cause

Two count-blind (and bucket-leg-blind) computations, both traced to ONE dropped leg:

1. **The bucket-NO leg was silently dropped from the window row.** In `run_v32._compute_money_math`
   the bucket ticker was resolved as `bt = state.bucket_tickers.get(state.spot_Sd)`. But the
   reset-at-close nulls `state.spot_Sd` before `_finalize` runs (see `_capture_quote` / the close
   reset), so at ledger-write time `spot_Sd is None` -> `bt is None` -> the `if bt:` guard skipped
   appending the guaranteed bucket-NO leg to `held`/`unsettled_legs`. The floor loop still counted the
   bucket leg (`held_this` started at 1), so the row's `realized_delta` was internally consistent, but
   the leg list handed to the settlement backfill carried ONLY the two wings. Confirmed on the live
   ledger: every armed window row's `unsettled_legs` had 2 entries (the wings), never the `KXBTC-...-B`
   bucket-NO leg. The bucket ticker WAS available all along in the rest fill record itself
   (`fills[i]["ticker"]`, e.g. `KXBTC-26SEP2000-B80450` on the 04:00Z row) — captured at fill time,
   before the close reset.

2. **The floor was netted count-blind.** `v32_settlement_backfill_sweep` computed
   `floor = v32_set_floor_dollars(len(legs))` — `count` defaulted to 1, so EVERY set's backfill netted
   a $1 floor regardless of size or how many legs actually settled. Combined with (1) (payoff priced
   over wings only), a complete size-2 in-bucket pin backfilled as payoff $4.00 − floor $1.00 =
   **+$3.00** when the true correction is **$0.00** (a complete 3-leg pin pays exactly $2 × count and
   its $2 × count floor was booked, so the correction is always $0). `v32_pending_credit` had the same
   count-blind `v32_set_floor_dollars(n_legs)` / `min(n_legs, 2)`, giving a complete size-2 pending set
   the band `(1.00, 2.00)` against a guaranteed $4.00 — an S4 false-latch risk on a late settlement.

## The fix

- `run_v32._compute_money_math`: recover `bt` from the rest fill record's own `ticker` (then
  `rest_bucket_Sd`) when `spot_Sd` is nulled; always list the bucket-NO leg (per batch, count =
  `fill_count`); `held_this` now counts only legs actually listed (fail-closed). Return `floor_booked`
  = the count-aware floor actually netted (Σ per batch `v32_set_floor_dollars(held_this, fill_count)`).
  `realized_delta = floor − cost` semantics unchanged; the PR #72 total-fee fix stays.
- `ledger.build_v32_ledger_row`: additive `floor_booked` kwarg + row key (string; None on old rows).
- `ledger.v32_settlement_backfill_sweep` / `build_v32_backfill_row`: prices ALL legs on the row (now
  incl. the bucket leg) and nets the floor ACTUALLY BOOKED via `_v32_floor_booked_for_entry`
  (`floor_booked` when present; else reconstructed count-aware from `wing_batch_sets`; else the legs
  present — fail-closed as today for truly old rows). Added `legs_priced` + `backfill_note`. Row shape
  (`settlement_payoff` / `floor_netted` / `realized_delta`) unchanged; append-only law respected — no
  existing row rewritten.
- `ledger.v32_pending_credit`: count- and bucket-aware — reads per-batch held/fill_count from
  `wing_batch_sets` when present, else count from the legs. Docstring table updated with the count
  factor. `V32_S4_DAY_LOSS_CAP_DOLLARS = 3.00` [pin] and `v32_s4_decision` signature UNCHANGED.
- `report.py`: new **LEDGER RECONCILIATION** block (read-only) summing (window + corrected backfill)
  per set with the corrected count-aware math, printed next to the venue balance move. The FALSIFIER
  SCOREBOARD block is byte-identical before/after (it reads `realized_lock` / `wing_batch_sets`, not
  these fields) — verified by diff on a read-only ledger copy (11 scoreboard lines, IDENTICAL).

## Before / after (live ledger, read-only copy; ledger itself unchanged — append-only)

Corrected per-set total = the venue balance move. Size-2 corrected totals match the venue moves exactly.

```
close_time             sz cmpl  stored_win  stored_bf  stored_tot   corr_win   corr_bf  corr_tot
2026-09-20T04:00:00Z    2   Y     +0.2573    +3.0000     +3.2573    +0.2345   +0.0000   +0.2345
2026-09-20T08:00:00Z    2   Y     +0.2812    +3.0000     +3.2812    +0.2622   +0.0000   +0.2622
2026-09-20T09:00:00Z    2   Y     +0.3389    +3.0000     +3.3389    +0.3177   +0.0000   +0.3177
2026-09-20T12:00:00Z    2   Y     +0.2414    +3.0000     +3.2414    +0.2228   +0.0000   +0.2228
2026-09-19T22:00:00Z    1   Y     +0.1165    +1.0000     +1.1165    +0.1165   +0.0000   +0.1165
2026-09-18T04:00:00Z    1   Y     -0.8682    +1.0000     +0.1318    -0.8682   +1.0000   +0.1318
2026-09-18T00:00:00Z    1   Y     -0.8748    +1.0000     +0.1252    -0.8748   +1.0000   +0.1252
2026-09-17T22:00:00Z    1   Y     -0.8681    +1.0000     +0.1319    -0.8681   +1.0000   +0.1319
2026-09-16T05:00:00Z    1   Y     -0.8770    +0.0000     -0.8770    -0.8770   +1.0000   +0.1230
2026-09-15T17:00:00Z    1   Y     -0.8745    +0.0000     -0.8745    -0.8745   +1.0000   +0.1255
2026-09-15T00:00:00Z    1   Y     -0.8840    +0.0000     -0.8840    -0.8840   +1.0000   +0.1160
```

- The 4 size-2 sets: stored total over-credited by $3.00 each (count-blind $1 floor vs $4 booked);
  corrected totals **0.2345 / 0.2622 / 0.3177 / 0.2228** = the venue balance moves. The 08Z/09Z/12Z
  window rows were written by the PRE-#72 code (fee added once) so their STORED `realized_delta`
  (0.2812/0.3389/0.2414) differ from corrected (0.2622/0.3177/0.2228) by the 2nd-lot wing fee; the
  reconciliation recomputes cost with total fees (`fee_total` when present, else the law
  `ceil(0.07·p·(1−p)·count, $0.0001)`).
- The 7 size-1 sets: corrected totals are the cash truth (= `realized_lock × 1` + the ~1.4c maker-fee
  reserve): 0.1160 / 0.1255 / 0.1230 / 0.1319 / 0.1252 / 0.1318 / 0.1165. The 3 oldest (out-of-bucket,
  no `wing_batch_sets`) had their bucket-NO $1 dropped (stored total under-credited by $1); several
  newer in-bucket ones were "right by accident" (the missing bucket-NO paid $0 in-bucket).

## S4 false-latch scenario

Complete size-2 set pending settlement past the :40 wake, `start = 53.31`, `balance_now = 53.31 −
3.7655 = 49.5445`:

| band | `v32_pending_credit` | `v32_s4_decision` | loss_pessimistic |
|---|---|---|---|
| NEW (count-aware) | `(4.00, 4.00)` | not latch (clear) | `−0.2345` |
| OLD (count-blind) | `(1.00, 2.00)` | — | `+2.7655` |

The old band understated the guaranteed credit by exactly $3.00 → a $2.77 apparent loss that a larger
set or lower balance would drive across the $3.00 S4 cap spuriously (fail-safe direction, costs a day).

## Files touched (4 + 1 new test)

- `pilot/service/run_v32.py` — `_compute_money_math`: bucket-leg recovery + `floor_booked`.
- `pilot/service/v32/ledger.py` — `build_v32_ledger_row` (`floor_booked`), `v32_pending_credit`
  (count/bucket-aware + docstring), `_v32_floor_booked_for_entry` (new helper), `build_v32_backfill_row`
  (`legs_priced`/`backfill_note`), `v32_settlement_backfill_sweep` (nets the booked floor).
- `pilot/service/v32/report.py` — LEDGER RECONCILIATION block (`build_ledger_reconciliation` +
  `_render_reconciliation` + fee helpers); scoreboard untouched.
- `pilot/ops/V32_ARMING.md` — S4 count-aware floor-netting paragraph.
- `pilot/tests/test_v32_backfill_count_aware.py` — 11 new tests (a–f + helpers).

`ceremony/v32_falsifier.md` NOT touched (FROZEN). `stops.py` NOT touched (signature/pin unchanged).

## Suite

`cd pilot && python -m pytest -q` → **955 passed** (baseline 944 + 11 new). Zero failures.
Report verified on a read-only copy of the live ledger: FALSIFIER SCOREBOARD byte-identical
before/after; reconciliation prints the corrected per-set totals above; `--json` path OK.
