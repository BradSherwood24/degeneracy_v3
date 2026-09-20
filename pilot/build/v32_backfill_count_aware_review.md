# Review PR #74 (head be6f17e): count-aware settlement backfill + S4 pending-credit band

Reviewer: Opus 4.8 (Fable's delegate). Branch `review/pr74`, base `origin/main` a637fd5, PR head
`be6f17e57bd0864a83edba012c5f1b7a69b54aa0`. Worktree `C:\Users\Brads\Python_stuff\dv3_wt_review`
(LIVE tree never touched; live ledger read-only copy in scratchpad). No Kalshi calls, no sealed read.

## VERDICT: BLOCK

The core fix (single-bucket, count-aware floor + bucket-leg recovery + S4 band) is correct,
well-tested, and reconciles to the venue balance to the cent on all 11 live sets. But the new
bucket-leg listing introduces a **reproduced, silent over-credit** in the multi-bucket window case
(finding #1). It is currently LATENT (0 live occurrences), so it may reasonably be fixed forward — but
it is an over-credit path (the unsafe direction) in a live-armed money ledger, which is the explicit
BLOCK bar and the exact "optimistic mirage" the house discipline exists to catch. One small, localized
fix clears it.

---

## Finding #1 (BLOCK) -- multi-bucket window: the 2nd batch's bucket-NO leg inherits the FIRST batch's ticker -> +$1/contract over-credit at settlement

`pilot/service/run_v32.py:1516-1551` (`_compute_money_math`). The bucket ticker `bt` is recovered ONCE
from the first rest-fill record and applied to EVERY batch's bucket-NO leg:

```
for f in fills:
    if f.get("leg") == "rest" and f.get("ticker"):
        bt = f["ticker"]; break
...
for b in getattr(state, "wing_batches", ()):
    if bt:
        held.append({"ticker": bt, "side": "no", "count": int(b.fill_count)})
```

When a window rests/fills on TWO different buckets in one hour -- a partial fill on bucket A
(remainder rests), spot crosses a $250 boundary (`core.py:1156-1169` cancels the A rest and re-places
on B), and the B rest also fills -- batch 1's bucket-NO leg is listed with **bucket A's** ticker
instead of B's. The wing legs are correct (each `WingLeg` carries its own ticker); only the bucket leg
is mislabeled.

**Reproduced** (hand-built two-batch/two-bucket state through `_compute_money_math`, then
`settlement_payoff`): batch 1 pins bucket B successfully and spot settles inside B. Bucket A is now
below spot, so A resolves `no` -> the mislabeled bucket-NO(A) leg WINS $1 when the true bucket-NO(B)
would LOSE. The three listed legs (bucket-NO(A)=win, B-Sd yes=win, B-Su no=win) pay $3, floor booked
$2 -> `realized_delta = +$1.00` per contract, versus the true $0 for a complete pin. The
backfill sweep does NOT fail closed here: bucket A is a real settled market, so `fetch_result`
returns a valid `no` and the over-credit is booked silently.

- This is a NEW path created by this PR: before, the bucket leg was dropped entirely, so no mislabeled
  bucket leg could be priced.
- The reconciliation report would NOT reveal it (`report.py:545` hardcodes `corr_payoff = 2 x count`
  for complete sets), so the operator's read-only view would show a correct $0 while the appended
  live backfill row carries +$1 -- the divergence masks the bug.

**Live exposure today: zero.** No live window has >1 batch or any partial remainder (checked all 143
ledger rows). But contracts=2 is armed now and partial fills are first-class/expected; a bucket change
during a partial is a plausible near-term event.

**Fix (small, localized):** resolve the bucket ticker PER BATCH. Rest fills and batches are appended
1:1 in order, so match the b-th rest-fill record's ticker to batch b, e.g.

```
rest_tickers = [f["ticker"] for f in fills if f.get("leg") == "rest" and f.get("ticker")]
...
for i, b in enumerate(getattr(state, "wing_batches", ())):
    bt_b = rest_tickers[i] if i < len(rest_tickers) else bt   # bt = the single-bucket fallback
    if bt_b:
        held.append({"ticker": bt_b, "side": "no", "count": int(b.fill_count)})
```

At contracts=1 and any single-bucket window this is byte-identical to the current code (one rest
ticker, all batches share it). A regression test for the two-bucket case (two rest fills, distinct
tickers) should assert each batch's bucket leg carries its own ticker and the complete-pin backfill
corrects by $0.

---

## Nits (non-blocking)

- **N1 -- `report.py:542` size fallback for old rows.** The `total_count` fallback
  `int(r.get("lots_filled") or 0) or sum(...counts...)//max(1,len(legs))` is only reached for rows
  with neither `wing_batch_sets` nor `lots_filled`; on the live ledger every set-bearing row has
  `wing_batch_sets`, so it never fires here. Harmless, but the integer-divide branch is fragile if a
  future old-shape row ever lands there. Consider dropping it or asserting.
- **N2 -- `_v32_floor_booked_for_entry` legs fallback (`ledger.py`).** Uses `max(counts)` across legs.
  Correct for the uniform-count pin, but if a malformed row ever mixed counts it would pick the
  largest (optimistic). Truly-old rows are count-1 so this is inert today; a `min` or a same-count
  assertion would be strictly safer.

---

## Adversarial checks that PASSED

- **Over-credit, single-bucket:** `v32_pending_credit` prefers `wing_batch_sets` (held count x
  fill_count) and ignores the 3-leg `unsettled_legs` list, so the added bucket leg never inflates the
  band. Truly-old rows (no `wing_batch_sets`, count-1 legs) keep the pre-count band exactly
  (`(1,2)` for a 2-leg set) -- verified. Lone leg -> floor $0. Bucket leg count is always
  `b.fill_count` (the filled count), never the allotment.
- **Fail-closed on unfilled rest:** `_compute_money_math` early-returns when `state.rest_fill is None`;
  bucket legs are only appended per batch, and batches exist only per rest FILL event -> a cancelled-
  unfilled rest lists no bucket leg. Confirmed by test (c) shape and by the guard.
- **`held_this` fail-closed:** starts at 0 and increments only for legs actually appended, so an
  unrecoverable bucket ticker books the wings alone rather than claiming a floor for an unnamed leg.
- **Consumer audit (below):** no consumer treats "2 legs" as complete or "3 legs" as impossible; the
  reconcile-first arming check reads LIVE proxy positions, not these lists; S1_LEGGED is driven by
  batch `one_legged` flags, not leg counts; the falsifier scoreboard reads
  `realized_lock`/`one_legged`/`wing_batch_sets`, never the leg list.

## Consumer audit (every reader of the held/unsettled leg lists, `len(legs)`, floor, one-legged)

| consumer (file:line) | len/count-sensitive? | 3 legs vs 2 changes behaviour? |
|---|---|---|
| `ledger.v32_settlement_backfill_sweep` (462-475) | yes -- prices ALL legs, count-aware | YES intended: bucket leg now priced. Correct single-bucket; **over-credits multi-bucket (finding #1)** |
| `ledger.settlement_payoff` (service/ledger.py:329) | yes -- $1 x count per matching leg | count-aware, correct; used by the sweep |
| `ledger.v32_pending_credit` (330-408) | prefers `wing_batch_sets` (count); legs only as old-row fallback | NO -- new rows use batch counts; 3-leg list not read for them; old rows unchanged |
| `ledger._v32_floor_booked_for_entry` (307-334) | prefers explicit `floor_booked`/`wing_batch_sets`; legs fallback count-aware | NO for new/partial rows; legs fallback inert on count-1 old rows (N2) |
| `report.build_ledger_reconciliation` (500-560) | reads `wing_batch_sets`/`floor_booked`; complete=`2 x count` | NO -- geometry-hardcoded for complete sets (also why it can't surface finding #1) |
| `report.build_falsifier_scoreboard`/`_set_events` (200-400) | reads `realized_lock`/`one_legged`/`wing_batch_sets` | NO -- never reads the leg list; **byte-identical before/after (verified)** |
| `stops.decide_v32_arming` / `reconcile_positions_clean` (248-330) | reads LIVE proxy positions | NO -- does not read ledger leg lists |
| `stops.v32_s4_decision` (181-204) | takes the `(pess,opt)` band tuple | NO -- signature unchanged; band supplied by `v32_pending_credit` |
| `stops` S1_LEGGED (count_legged, 205-230) | counts `one_legged` guard entries | NO -- independent of leg-list length |
| `run_v32._batch_set_records` (1388-1416) | `held_legs`=1+filled wings (a COUNT) | NO -- unchanged by PR; already 3 for a complete set in existing live rows |
| `pilot_ledger` / `box_report` / `run_window` unsettled_legs | V2/box path, separate rows | NO -- not the v32 window rows |

## Receipts

- Diff scope: exactly 6 files (`git diff --stat origin/main...HEAD`); working tree clean, no CRLF
  phantom changes leaked.
- Suite: `cd pilot && python -m pytest -q` -> **951 passed, 2 skipped, 2 errors**. The 2 errors are
  environmental (this worktree has no `historical-data/`: `test_quintile.py` FileNotFoundError on
  `historical-data/15-minute/markets/2026-06-11.jsonl`), unrelated to the PR. New file
  `test_v32_backfill_count_aware.py`: 11 passed. Builder's 955 = 951 + the 2 quintile + 2 skipped
  resolving as pass where data exists; the delta is purely the missing dataset.
- Mutation: reverting the count factor in `v32_pending_credit` (batch path) ->
  `test_e_pending_credit_complete_size2_via_batch_sets` FAILS (`Decimal('2') != Decimal('4.00')`).
  Restored.
- Floor reconstruction on live copy: `_v32_floor_booked_for_entry` -> 2026-09-20T04:00Z size-2 = **4**,
  2026-09-19T22:00Z size-1 = **2** (both `floor_booked` None, reconstructed from `wing_batch_sets`
  held_legs 3).
- Append-only: `v32_settlement_backfill_sweep` on the live copy yields **0** new rows (all 11 unsettled
  windows already carry a `backfill_of` row). No existing row rewritten.
- Scoreboard identity: FALSIFIER SCOREBOARD block **byte-identical** main vs branch on the live copy
  (11 lines, diff empty).
- LEDGER RECONCILIATION corrected per-set totals equal the ground truth exactly:
  04Z +0.2345, 08Z +0.2622, 09Z +0.3177, 12Z +0.2228 (sum 1.0372: 53.3069 -> 54.3441), the four size-2
  sets; the seven size-1 sets +0.1160/+0.1255/+0.1230/+0.1319/+0.1252/+0.1318/+0.1165. Stored total
  +11.9887 vs corrected +1.9071 (delta +10.0816 = 4 x $3 over-credit + the size-1 under/over-credits).
- `--json` valid; `reconciliation` key additive with 11 entries; `falsifier` unchanged.
- `ceremony/v32_falsifier.md`, `pilot/service/v32/stops.py`, `pilot/service/v32/core.py` **untouched**.
  `V32_S4_DAY_LOSS_CAP_DOLLARS = 3.00` [pin] and `v32_s4_decision` signature unchanged.
- `pilot/ops/V32_ARMING.md` paragraph accurate (count-aware band, bucket-leg recovery, floor_booked,
  pins/falsifier explicitly unchanged).

## S4 false-latch regression (verified)

`test_f_s4_no_false_latch_size2_pending`: start 53.31, now 49.5445, new band `(4.00, 4.00)` ->
`v32_s4_decision` not latch (`loss_pessimistic`/`optimistic` <= 0); old band `(1.00, 2.00)` shows
`loss_pessimistic > 2.7`; the difference is exactly $3.00. Passes.

---

# Round 2 (head 49785a5, delta be6f17e...49785a5) -- per-batch bucket ticker + N1/N2

Re-review of `fix/backfill-count-aware` head **49785a52fc8e623ffebeccd706a7ea8ff44cf970** (prev
be6f17e). Delta = exactly 5 files (`git diff --stat be6f17e...49785a5`): run_v32.py, ledger.py,
report.py, the test file, the build report. Working tree clean, no CRLF phantom leak. Same house
rules honored (python only, live tree untouched, no Kalshi calls, falsifier frozen/untouched).

## VERDICT (round 2): APPROVE WITH NITS

Round-1 BLOCK (FINDING #1) is **fixed**: the multi-bucket over-credit is gone in the reachable case.
N1 and N2 are fixed with the correct fail-safe (`min`) direction. Nothing on the 144 live rows
changes. Two non-blocking nits remain (a residual fragility in the finalize-time walker, and thin test
coverage for the new fallback/`min` paths) -- neither is a reachable reproduced over-credit.

## FINDING #1 -- FIXED

`run_v32._compute_money_math` now resolves the bucket-NO ticker PER BATCH by walking the rest-fill
records in order and consuming each record's `count` as batches are assigned (`run_v32.py:1534-1563`),
with the single recovered `fallback_bt` only for batches past the last record. Re-ran my own
two-bucket reproduction against the new head:

- **Clean two-bucket (case b):** batch 0 rests/fills on bucket A, spot crosses, batch 1 on bucket B.
  Held bucket legs now `{A, B}` (round-1 gave `{A, A}`). Coherent spot-in-B settlement:
  `settlement_payoff = $4`, floor `$4`, **correction $0** (round-1 mislabel booked `$5 -> +$1`). Same
  for settled-in-A. The three new tests (`test_round2_two_bucket_*`) assert exactly this, incl. each
  leg's own ticker and `count == fill_count` (1), and the sweep correction $0 both directions. Pass.

## Break attempts (a)-(e)

- **(a) same-order collapse -- SAFE.** One rest record `count=2` mapped to two `fill_count=1` batches:
  the walker's `_rem` consumes 1 per batch, both batches get that record's (single, correct) ticker,
  floor `$4`. Reproduced -> `['...BA','...BA'], floor 4`.
- **(a') same-order, records exhausted -- SAFE.** One record `count=1`, two batches: batch 1 exhausts
  the records and takes `fallback_bt` = the first record's ticker = the same (correct) bucket.
  Reproduced -> `['...BA','...BA'], floor 4`. (This is the real WS-delta-then-poll-dedup shape: the
  second delta is de-duped by `booked_rest_oids` in `_record_fill`, so only one record exists; both
  batches are the same order -> same bucket, so the fallback is correct.)
- **(b) distinct-order two-bucket -- SAFE (the fix).** See FINDING #1. A bucket change places a
  DISTINCT order (`core.py:1156-1169` cancels A, places new on B) -> distinct order_id -> distinct
  rest record with its own `ticker`/`bucket_Sd` (`run_v32._record_fill` + executor cancel-race/amend
  paths all stamp `ticker` and `bucket_Sd`). Records are 1:1 with batches in creation order.
- **(c) records out of batch order / extra record -- ONE fragile shape (NIT-1).** Records are appended
  in `_record_fill`/executor immediately BEFORE the batch-spawning Fill is pumped, so normal WS, poll,
  cancel-race and amend paths keep records in batch order. Verified an EXTRA record at the END is
  harmless (walker stops when batches are exhausted). BUT an extra record in the MIDDLE mislabels the
  following batch: injected `[A, C, B]` for two batches -> batch 1 got `C` instead of `B`. See NIT-1
  for the only path that could produce a mid-sequence record-without-batch and why it is not a
  reachable reproduced failure.
- **(d) cancel_ctx-booked lot -- SAFE.** The eager-clear/cancel-race booking DOES emit a rest record
  (`executor.py:818`, `_finish_cancel`) carrying `rec.ticker` + `rec.bucket_Sd`, and returns
  `OrderCancelled(filled_count)` which the core books as its batch -- record-then-batch, in order, with
  the correct ticker. No fallback, no mislabel.
- **(e) same-order maker-then-taker cross -- SAFE regardless of reachability.** The amend-cross taker
  fill (`executor.py:933`) is booked on the SAME order_id/bucket; whether or not the interleaved case
  is reachable at contracts=2, a same-order fill is the same bucket, so no bucket leg can be mislabeled.

## Nits (non-blocking)

- **NIT-1 -- the finalize-time walker is positional (count-consuming), so a rest record with NO
  corresponding wing batch inserted mid-sequence would shift every following batch onto the wrong
  record.** The only source of a record-without-batch is the late-fill path
  (`run_v32.on_fill` late branch calls `_record_fill` -- which appends a record and adds the order_id
  to `booked_rest_oids` -- THEN `book_late_rest_fill`, which no-ops via its `rest_fill is not None`
  guard once any fill is booked, `core.py:1291`). For that record to become a MID-sequence extra it
  must (i) be a late WS fill on a replaced/eagerly-cancelled order of a DIFFERENT bucket, (ii) arrive
  before that order's cancel-confirm books it (otherwise `booked_rest_oids` de-dups the record away),
  and (iii) be followed by a further batch-spawning fill -- all at contracts>=2 during a mid-hour
  bucket change. I could reproduce the mislabel only by hand-injecting the extra middle record; I did
  NOT drive the real fill pipeline to emit one (the `booked_rest_oids` de-dup + the cancel path's
  cumulative booking -- the documented N2 defense in `book_late_rest_fill` -- are designed to prevent
  it). It is therefore not a reproduced reachable over-credit, and it is NOT a regression: round-1
  mislabeled EVERY non-first batch (strictly worse). Live exposure is zero (no live window has >1 batch
  or a partial remainder across 144 rows). **Durable fix (recommended, not required to ship):** bind
  each batch to its bucket ticker at SPAWN time in the core (the Fill already carries the bucket
  context; `book_late_rest_fill` already takes `bucket_Sd`) and store it on `WingBatch`, eliminating
  the finalize-time reconstruction/walker entirely; or key the walk by `client_order_id` rather than
  positional count-consumption.
- **NIT-2 -- thin test coverage for the new paths.** The three added tests cover only the clean
  distinct-order two-bucket case. No test pins (a')'s fallback-to-first-record, the same-order collapse
  (a), or the N2 `min(counts)` fail-safe direction (a mutation `min -> max` in the legs fallback is
  currently caught by nothing). Add a same-order-collapse test and a malformed-mixed-count `min` test.

## N1 / N2 -- direction verified at every call site

- **N2** (`ledger.py`): `_v32_floor_booked_for_entry` and `v32_pending_credit` legs fallback changed
  `max(counts) -> min(counts)`. Correct fail-safe in BOTH sites: the floor-reconstruction min can only
  UNDER-net the backfill (conservative); the S4 pessimistic-bound min UNDER-states the guaranteed
  credit -> the day loss looks LARGER -> S4 more likely to latch (fail-safe). The optimistic bound also
  uses the min (understates upside -> `loss_optimistic` larger -> more likely to latch) -- also
  fail-safe. Well-formed sets share one count across legs, so exact on every real row.
- **N1** (`report.py:541-546`): the old-row set-size fallback is now `lots_filled` else
  `min(leg_counts)` (fail-closed), replacing the integer-divide that silently truncated a mixed-count
  row. Read-only reconciliation only; a smaller size can only under-state the corrected payoff.

## Receipts (round 2)

- Diff scope: exactly the 5 claimed files; working tree clean; no CRLF phantom leak.
- Suite `cd pilot && python -m pytest -q`: **954 passed, 2 skipped, 2 errors**. The 2 errors are
  environmental (this worktree has no `historical-data/`: `test_quintile.py` FileNotFoundError on
  `historical-data/15-minute/markets/2026-06-11.jsonl`). 954 + 2 quintile + 2 skips = 958 items =
  builder's "958 passed" where the dataset exists. New test file: **14 passed** (11 round-1 + 3 round-2).
- Reconciliation corrected size-2 totals UNCHANGED: 04Z +0.2345, 08Z +0.2622, 09Z +0.3177,
  12Z +0.2228 (= the venue balance move per set). FALSIFIER SCOREBOARD **byte-identical** main vs
  round-2 branch on the read-only live copy (144 rows). Append-only holds: sweep on the copy yields
  **0** new rows.
- Two-bucket fix reproduced independently: held bucket legs `{A, B}`, correction $0 both settlement
  directions (round-1 mislabel: +$1).
- `ceremony/v32_falsifier.md`, `stops.py`, `core.py` money-math signatures untouched;
  `V32_S4_DAY_LOSS_CAP_DOLLARS = 3.00` [pin] and `v32_s4_decision` signature unchanged.
