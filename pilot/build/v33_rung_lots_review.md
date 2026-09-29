# V3.3 L5 review — per-rung lot weights (`rung_lots`)

Opus 4.8 adversarial review of `feat/v33-rung-lot-weights` (5 commits on top of `main` f0d6c6d:
bb7ff16, 68ea456, 3fc8c7b, 6f15460, 713f539). Reviewed in worktree `dv3_wt_range`, detached at 713f539;
fixes committed on `review/v33-rung-lot-weights`.

## Verdict: APPROVE WITH NITS

The mechanism is correct. The uniform default (key absent) is byte-identical to pre-L5 (golden + core
suites pass untouched; shipped policy has `rungs=11, lots_per_rung=1, max_sets_per_hour=11, rung_lots
ABSENT` → resolved `(1,)*11`, allotment 11 = max_sets = rungs). Every disclosed decision was checked by
running the code, not just reading it. I implemented finding 1 (the arming-gap the builder flagged) with
tests; the rest are disclosed, safe-direction (under-fill) limitations reachable only at weight > 1, which
DRY never exercises.

Suite: **1352 passed / 5 skipped** at feature HEAD (matches the builder). After my +6 stops tests:
**1358 passed / 5 skipped** (`cd pilot; python -m pytest -q`). The 5 skips are environmental.

## Findings

### 1. [Medium — FIXED by reviewer] The S5 arming cap check lied for weights > 1 (concern H)
`stops.v33_caps_agree` / `v33_arming_check` / `decide_v33_arming` / `run_v33` passed only
`lots_per_rung` (=1) and `k_rungs`, so the cap band was `[lots_per_rung, K*lots_per_rung]`. With any rung
weight > 1 a rung is PLACED as ONE order of `w` lots, so the proxy `MAX_CONTRACTS_PER_ORDER` must cover
`max(rung_lots)`; the pre-L5 check would ARM at a proxy cap of e.g. 2 while the venue rejects a weight-3
PLACE_REST. The ceiling was `K*lots_per_rung` not the true largest coalesced take `sum(rung_lots)`. The
loader already enforces `max_contracts_per_order_hint >= max(rung_lots)` and V33_ARMING.md documents the
proxy requirement, but the automated gate did not — "the arming check must not lie."

**Fix:** threaded the resolved `rung_lots` vector through the three `stops` functions and `run_v33`; the
band is now `[max(rung_lots), sum(rung_lots)]` when the vector is supplied, and **identical** to the old
`[lots_per_rung, K*lots_per_rung]` when absent (uniform). Byte-identity at weight 1 is proved by
`test_caps_uniform_rung_lots_identical_to_absent`. +6 tests in `test_v33_stops.py`. V3.3 is DRY so this
was pre-arm, not a live gap — but it closes a gate that would have silently mis-armed a weighted config.

### 2. [Low — disclosed/inert] `max_sets_per_hour` semantics narrowed
`_place_all`'s budget base moved from `max_sets_per_hour - rungs_filled` to the CONTRACT allotment
`sum(rung_lots) - filled - resting`, and the latch dropped the old `not slots and …` guard (now plain
`rungs_filled >= max_sets_per_hour`). Inert at the shipped config (`max_sets == rungs == sum == 11`;
golden proves byte-identity). But under L5, `max_sets_per_hour` no longer caps how many rungs REST at
once / the initial placement count — it is purely a fill-EVENT latch. A future params combo with
`max_sets_per_hour < sum(rung_lots)` would rest more simultaneous rungs than `max_sets` implies (bounded
by the contract allotment, which is the true exposure). Recommend one doc line in V33_ARMING.md stating
`max_sets_per_hour` is a fill-event latch, not a resting-rung cap, under L5. Not a blocker.

### 3. [Low — disclosed, live-only] Partial fills inflate the `max_sets_per_hour` fill-event latch (concern B)
`rungs_filled` counts fill EVENTS (`_book_rung_fill` does `+1` per call); a weight-`w` rung filling over
several partials counts several toward the K=11 latch, so a weighted+partial window can latch
`rest_allotment_done` before the contract allotment is spent → under-fill. **Unreachable in DRY**:
`run_v33._simulate_ladder_fills` emits `Fill(count=o.count)` — the full rung weight in ONE event
(verified), so dry never partials and `rungs_filled` == distinct rungs, exactly as at weight 1.
Safe (under-fill), builder-disclosed (#3). Acceptable while dry; before arming weights, either latch on
contracts vs `sum(rung_lots)` or keep and accept the throttle.

### 4. [Low — disclosed] Bucket-change under-fill after a partial (concern D)
Verified a partial remainder is NEVER stranded at a stale price forever: every no-quote reason,
`past_quote_end`, stand-down, replace-rate alarm and `rest_allotment_done` path routes through
`_cancel_all`, which cancels the remainder (with `_remember_cancel_ctx` so a cancel-caught fill is still
booked/hedged). The `_converge` OUT-filter exclusion (`rest_booked_by_coid > 0`) only keeps the remainder
resting DURING quoting, never past these exits. The one gap: a bucket change `_cancel_all`s the remainder
and `_place_all` does not re-place it on the new bucket (state-2/consumed slot) → under-fill, safe,
live-only (weight > 1). Acceptable; an easy future fix is to re-open the consumed slot's residual weight
on the new bucket, but it trades safety (less exposure) for completeness.

### 5. [Nit] `RungFill.weight` records the fill-time margin's configured weight, not the order's placed count
`_book_rung_fill` sets `weight = rung_lots[derived_rung]` where `derived_rung` is from price-vs-`n_top`
at fill. Under smear the order's actual placed count may be a different rung's weight. This is the RIGHT
choice for the allocation table (keyed by fill margin) — it lets Brad SEE smear as `contracts` vs
weight-implied per level — and the docstring says "configured … at fill time." No change; noting the
name could be misread as the order's own size.

### 6. [Nit] "per PLACED margin" labeling
`build_allocation_table` / the report header say "per PLACED margin" but key on `E_rung` = the FILL-TIME
derived margin (`_margin_c` = `round(E_rung*100)`), which under smear differs from the placed margin.
This is the economically meaningful choice (where it rested when hit, matching builder note #1); the
"PLACED" wording is slightly imprecise. Cosmetic.

## Verified correct (ran the code, not just read it)

- **Remainder math (C).** `remaining = order.count - delta` is correct. `delta` is this event's
  increment; `order.count` is the CURRENT resting count (already reduced by prior partials via
  `_replace_order`); `booked[coid]` is CUMULATIVE and the old `order.count - booked[coid]` double-counts
  from the 2nd partial on (`(w-δ1) - (δ1+δ2)`). Identical at weight 1 (one fill event). Covered by the
  weight-3 partial + exposure-invariant tests.
- **Roll / re-size (A).** Keeping count on BOTH roll paths (amend re-prices via `order.count`;
  cancel→create fallback re-places `mover.count`) is correct. Re-sizing only the moving order does NOT
  fix the K-1 stationary orders whose rung labels shift on every 1c `n_top` drift (the real smear
  source), and re-sizing on the fallback-only would break amend/fallback end-state equivalence. So it is
  NOT a clear improvement — I did not implement a re-size, agreeing with the builder. Smear is bounded by
  `n_top` drift (a few cents ≈ a few rungs of the 11-rung 5–15c ladder); acceptable for a coarse 3-band
  allocation whose RESULT is measured by fill-time margin. (One caveat: within-window directional drift
  makes the smear biased, not mean-zero — the realised shape can lean one way vs the configured intent;
  the allocation table shows the truth.)
- **Exposure invariant (E).** Creates/re-places in `_place_all`, `_converge` vacant-create and the
  `_apply_cancelled` fallback all budget against `sum(rung_lots)`; the fallback drops the mover from the
  ladder BEFORE the `filled+resting+replace_count <= allotment` check (no double count); rolls add no
  exposure; a fill moves lots resting→filled (conserved). `check_invariants` (test-time oracle, run after
  every event in the `_feed` harness) asserts `filled + resting contracts <= sum(rung_lots)` and
  `1 <= count <= max(rung_lots)`. Print-through wings size by rung weight (`sum(o.count for o in cands)`),
  not 1; wing takes size by filled count per event (`RungFill.count`). Could not break it by hand.
- **Zero-weight rungs (F).** `V33State.new` builds a weight-0 rung as `margin_state` 0 ("no order"),
  never 1, so it is never placed/targeted and never stalls neighbour convergence (open span naturally
  non-contiguous, which the R4 price-paired convergence already supports). Report guards `None` weight
  and empty solved-lock lists (no div-by-zero).
- **Ledger back-compat (G).** Ran `service.v33.report` against a read-only copy of the real 144-row
  `v33_ledger.jsonl` (into scratchpad, live tree untouched): parses clean, allocation table renders,
  pre-L5 rows show `weight = "-"` (None), no crash.
- **Params loader.** `rungs` is parsed before the `rung_lots` length check; absent → uniform (never
  KeyErrors); validates list-ness, length, non-negativity, some > 0, and `hint >= max(rung_lots)`. sha
  pin and policy JSON byte-unchanged.

## Reviewer changes
- `pilot/service/v33/stops.py` — weight-aware cap band in `v33_caps_agree`; `rung_lots` threaded through
  `v33_arming_check` and `decide_v33_arming` (all optional, default None = old behaviour).
- `pilot/service/run_v33.py` — passes `rung_lots=params.rung_lots` to `decide_v33_arming`.
- `pilot/tests/test_v33_stops.py` — +6 tests (weighted band lower/upper bounds, uniform-vector ==
  absent, arming-check threading, and a proof the pre-L5 call still arms at the bad cap).
