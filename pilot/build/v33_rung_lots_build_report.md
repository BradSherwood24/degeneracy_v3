# V3.3 L5 build report — per-rung lot weights (`rung_lots`)

Branch `feat/v33-rung-lot-weights` (off `origin/main` f0d6c6d). Mechanism only: give the V3.3 ladder a
per-rung lot-weight vector so Brad can "scale the edge — not 100% capital at 10c." Default (key absent)
is byte-identical to the pre-L5 uniform ladder. Brad sets the weights himself, after a dry week, via a
params PR that re-pins the sha. **`pilot/policy/v33_params.json` is byte-unchanged and the pinned sha
`2e60980762…995e` is untouched** (a concurrent builder owns the `E_min` params change).

## What `rung_lots` is
An OPTIONAL policy key: a list of exactly `rungs` non-negative ints, index k = rung k (k=0 = top rung at
margin `E_min`, k=K-1 = deepest). A `0` means that rung is never placed. Absent → resolved to
`[lots_per_rung] * rungs` (all-ones today). The hour's exposure cap becomes the CONTRACT allotment
`sum(rung_lots)`; `max_sets_per_hour` stays the coarse rung/fill-event gate.

## Design decisions

### The roll / re-size question (the load-bearing one)
The open margin slots are ANONYMOUS and the convergence pairs a live order to a vacant target by minimal
price movement (Brad's one-order roll that keeps queue). With unequal weights, an order's margin slot
changes as `n_top` drifts, so "exact weight per slot at all times" and "cheap one-order roll" are in
direct tension: maintaining exact per-slot weights after a 1c `n_top` move would require re-sizing every
resting order (the middle orders each shift one slot), i.e. K amends per cent — the churn the whole
margin-array convergence was built to avoid.

**Decision (matches the task's suggested default): a resting order carries the lots it was PLACED with
and KEEPS that count through every roll.** The AMEND re-prices, never re-sizes (`_emit_roll` sends
`order.count`); the amend→cancel→create fallback re-places the SAME remaining count, so both roll paths
share one end state. Re-sizing to a slot's weight happens ONLY when a FRESH order is created for that
slot — the initial `_place_all`, a bucket-change re-place, or a released-slot vacant-create in
`_converge`. Consequence: after `n_top` drifts, the middle orders keep their placed counts while their
slot labels shift ("weight smearing"); the INITIAL allocation (the full ladder laid down with per-rung
weights) dominates, and `n_top` moves only a few cents per window. `RestOrder.count` is the single source
of truth for an order's size (no separate weight field on the order).

Why not re-size on the roll? Re-sizing only the moving order does NOT fix the middle orders (they still
smear), so it buys nothing while breaking the amend/fallback end-state equivalence. Re-sizing ALL orders
restores exactness but is the K-amends-per-cent churn we reject. So "count == weight(current slot)"
cannot be a step invariant under any cheap-roll scheme; the asserted invariants are instead, per order,
`1 <= count <= max(rung_lots)`, and, per window, `filled + resting contracts <= sum(rung_lots)`.

### Zero-weight rungs
A weight-0 rung is margin-array state `0` ("no order"), never `1` ("order wanted"), built that way in
`V33State.new`. It is therefore never placed, never a convergence target, never a VACANT, never stalls
neighbour rolls — the open span is simply non-contiguous, which the R4 price-paired convergence already
supports (it never assumed contiguity as a step property).

### Partial fills (only reachable when a weight > 1)
Two correctness items surfaced, both latent at weight 1 (a weight-1 rung fills in exactly one event):
- **Remainder math.** `_book_rung_fill` computed the resting remainder as `order.count - booked[coid]`
  (cumulative), which double-counts across successive partials. Fixed to `order.count - delta` (this
  event's lots). Identical at weight 1; correct for weighted partials. The filled lots are winged
  (wings are already sized per fill event → `RungFill.count`), the remainder rests.
- **A partial remainder is never rolled.** A partially-filled rung's slot is marked consumed (state 2)
  on the first fill, so its price is no longer a target; its still-resting remainder would then be
  flagged OUT and rolled. `_converge`'s OUT filter now excludes orders with `rest_booked_by_coid > 0`
  (no-op at weight 1), so the remainder rests undisturbed until it completes or is cancelled at quote-end.

### Exposure cap moved to contracts
`check_invariants`' old `rungs_filled + len(ladder) <= K` was a rung-unit cap valid only at weight 1
(where `rungs_filled` = distinct filled rungs). With weights + partials, `rungs_filled` is a fill-EVENT
count. Replaced with the contract invariant `filled + resting contracts <= sum(rung_lots)` (identical to
the old assert at weight 1). `len(ladder) <= K` (price-slot count) is kept. `_place_all` and the
vacant-create in `_converge` now budget in contracts against the allotment; `max_sets_per_hour` is kept
as a separate coarse latch (unchanged behaviour, incl. the existing budget-zero test).

## File-by-file changes
- **`service/v33/params.py`** — `V33Params.rung_lots: tuple[int, ...]` (resolved). Loader resolves the
  optional key (never KeyErrors when absent) and validates: is-a-list, length == `rungs`, each >= 0, some
  > 0. The `max_contracts_per_order_hint` check now requires `>= max(rung_lots)` (identical to the old
  `>= lots_per_rung` when absent). Docstrings updated.
- **`service/v33/core.py`** — helpers `_weight_of_rung` / `_weight_of_margin` / `_allotment` /
  `_resting_contracts` / `_filled_contracts`; `V33State.new` skips 0-weight rungs; `check_invariants`
  contract exposure + `1 <= count <= max(rung_lots)`; `RungFill.weight` field; `_book_rung_fill`
  remainder fix + weight; `_place_one`/`_place_action` take a `count`; `_place_all` per-slot weight +
  contract budget; `_emit_roll` keeps `order.count`; `_apply_cancelled` fallback re-places the mover's
  remaining count + contract budget; `_converge` excludes partial-remainders from OUT and creates at slot
  weight under the contract budget; `_pt_book_taker_complete` sets weight. Module docstring L5 note.
- **`service/v33/ledger.py`** — `rung_fills` rows carry `weight` (backward compatible; absent on pre-L5
  rows). Docstring updated.
- **`service/v33/report.py`** — new `build_allocation_table` + `_render_allocation`: a per-PLACED-margin
  table (level c, weight, fills, contracts, mean solved lock/contract, %pos, total solved lock) over
  realised and dry rows, using `lock_solved` (wing-completion-independent → it scores the allocation
  itself). Wired into `build_v33_report` (`"allocation"`) and `_render`. Existing sections unchanged.
- **DRY twin** — `run_v33._simulate_ladder_fills` already emits a `Fill` of `o.count`, so the simulated
  fill fills the full rung weight in one event, identical to the live path (proven by a new test).
- **Docs** — `ceremony/v33_falsifier.md` (new "L5 amendment" section; sha lines untouched);
  `ops/V33_ARMING.md` (how to set `rung_lots`, incl. the proxy `MAX_CONTRACTS_PER_ORDER >= max(rung_lots)`
  arming note).

## Tests
`tests/test_v33_rung_lots.py` (18 tests): loader validation (absent→uniform; present; wrong length;
negative; all-zero; exceeds hint; not-a-list; sha unchanged), uniform default == pre-L5, weighted ladder
rests the right counts at the right prices, 0-weight rungs don't stall neighbour convergence, a roll
keeps the moving order's count (smearing), a partial fill of a weight-3 rung wings 1 and keeps 2 resting,
a partial remainder is never rolled, the contract exposure invariant under partial + rolls, the dry-sim
twin fills the full rung weight, and the report allocation table.

Full suite: **1352 passed, 5 skipped** (the 5 skips are environmental — historical-data absent /
POSIX-only SIGTERM — none in V3.3; baseline before this build was 1334 passed, 5 skipped). The untouched
golden suite (`test_v33_golden.py`, `test_v33_core.py`) proves the uniform default is byte-identical.

## The JSON Brad would add to set weights (illustration — NOT a recommendation)
For the shipped 11-rung ladder (margins 5c..15c), one shape putting ~half the lots on the deep rungs,
none on the two shallowest:

```json
"rung_lots": [0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3]
```

Sum = 20 contracts/hour. This is a params amendment: set the key, re-run the sha, re-pin
`FROZEN_V33_PARAMS_SHA256` with a dated Registration entry — Brad's hand, never an agent's.

## Things I'm unsure about / a reviewer should scrutinise
1. **Smearing acceptability.** After `n_top` drifts, middle orders keep their placed counts while their
   slot labels shift, so the instantaneous per-margin distribution deviates from `rung_lots` (the initial
   placement is exact). I judged this the right trade for the cheap one-order roll; confirm the study's
   coarse 3–4× deep-vs-shallow signal tolerates it. `RungFill.E_rung`/`rung` (and the report) key on the
   FILL-time margin, which is the economically meaningful "where it rested when hit."
2. **Bucket change after a partial fill.** A partially-filled rung's slot is state 2 (consumed); on a
   bucket change `_cancel_all` cancels the resting remainder and `_place_all` does NOT re-place it on the
   new bucket (state-2 slot). This UNDER-fills that rung (safe: less exposure), never over-fills. Only
   reachable at weight > 1. Flagged as a known limitation.
3. **`rungs_filled` vs `max_sets_per_hour` with partials.** `rungs_filled` counts fill EVENTS, so many
   partials could hit `max_sets_per_hour` (11) before the contract allotment is spent, latching
   `rest_allotment_done` early. Safe (under-fill), and the contract allotment is the true guard; kept in
   rung units per the spec. Worth a glance for pathological weight/partial combos.
4. **Arming cap check (out of this scope).** `stops.v33_caps_agree` / `run_v33` still pass
   `lots_per_rung` (not `max(rung_lots)`) to the proxy-cap agreement. The loader now enforces
   `max_contracts_per_order_hint >= max(rung_lots)`, but before ARMING with any weight > 1 the proxy
   `MAX_CONTRACTS_PER_ORDER` must also cover `max(rung_lots)` (documented in V33_ARMING.md). V3.3 is DRY,
   so this is a pre-arm follow-up, not a live gap; still, a reviewer may want the arming check tightened
   to `max(rung_lots)` in a later PR.
5. **`filled_at` on partials.** `_book_rung_fill` sets `filled_at[m]` to the latest fill on each partial
   of a rung (the last event wins). It's used only for the §6 per-margin key/report; each partial's
   `RungFill` is still recorded in `rest_fills`, so no accounting is lost — but the `filled_at` anchor is
   the last partial, not the first. Minor; flag if the falsifier's per-margin accounting wants the first.
