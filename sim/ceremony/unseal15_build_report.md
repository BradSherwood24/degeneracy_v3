# BUILD REPORT — `sim/unseal_runner15.py` (Rung 1.5 sealed-read runner)

Built by opus48 (Brad's standing mandate) per `sim/ceremony/unseal15_commission.md`
and `sim/ceremony/falsifier.md` SECTION 2. Awaiting adversarial review before the
dry-run certificate is trusted for a sealed read.

## What was built

- `sim/unseal_runner15.py` — the runner. Three parts:
  - **`PolicyEvaluator`** (streaming, no full-file memory): applies the frozen
    expanded quintile-conditional policy to tape_points rows. Groups by contiguous
    `close_time`; per direction tracks the first fresh qualifying event (rows are
    chronological within a direction, so first-seen = largest `t_minus_s` = earliest);
    per window selects the winner by largest `t_minus_s` across the routed sources.
    Retained state is aggregate-sized (per-entry records ≈ #windows, a seen-close_time
    set, small per-window scratch); the 18.3 M tape rows stream through.
  - **`--dry-run-train`** gate: runs the evaluator over `sim/out/full60/tape_points.csv`
    (train, no sealed access) and writes `sim/out/unseal15_dryrun.json`, verifying the
    frozen train reference numbers embedded as constants. Refuses (raises, after writing
    the certificate for debugging) if any number diverges.
  - **`--i-have-brads-explicit-go`** sealed path: fail-closed preflight → `tape_sim.run()`
    ONE SEALED DAY AT A TIME (out_dir `sim/out/sealed_eval/dYYYY-MM-DD/`, staleness 60,
    `acknowledge_sealed_read=True`) → evaluator (burned-hours excluded) → AGGREGATES ONLY
    to `sim/out/unseal15_result.json`.
- `sim/tests/test_unseal15.py` — 29 tests. Existing modules (`tape_sim.py`,
  `loader.py`, `unseal_runner.py`) were NOT modified.

## Policy implementation (as pinned)

- Freshness: `max(high_leg_age_s, low_leg_age_s) <= 1.0` (boundary inclusive).
- Q1 (quintile 0): strangle `ev >= 0.05` OR flip `C < 1.0`, earliest by `t_minus_s` wins.
- Q2–Q4 (quintiles 1,2,3): flip `C < 1.0` only.
- Q5 (quintile 4): flip `ev >= 0.10` only.
- One entry per window; records payoff, C, pin, quintile, source
  (`Q1-strangle` | `Q5-flip` | `sub$1-flip`), date.
- Burned close_times configurable; sealed set = `{2026-08-18T19:00:00Z,
  2026-08-18T20:00:00Z}`, dropped + counted, never eligible/entered.
- Eligible-window count = distinct non-burned close_times seen.

## Sealed-read preflight (fail-closed, in order — verified refusing NOW)

1. `falsifier.md` exists and is FROZEN (`loader.falsifier_is_frozen`; loader A3.7
   re-asserts before any sealed open).
2. `census_train.csv` sha256 == pinned `580d143f…7bb247`.
3. All sealed day-files present for both series × {markets, candles, trades}.
4. Train dry-run certificate present, numbers match the embedded reference, and its
   `policy_params_sha256` matches this build's policy fingerprint.
5. `unseal15_result.json` does NOT already exist (one-shot).

With the checked-in falsifier still DRAFT, `preflight_sealed()` refuses today with
`REFUSE: falsifier.md is not FROZEN …` — confirmed at runtime. No sealed byte is read
during preflight (only `os.path.exists` metadata checks).

## Dry-run output (verbatim)

```
TRAIN DRY-RUN — wrote C:\Users\Brads\Python_stuff\degeneracy_v3\sim\out\unseal15_dryrun.json
  eligible windows : 1142
  entered          : 599
  pooled mean      : 3.71 cents (3.713072 6dp)
  pins among entered: 62
  by source        : {'Q1-strangle': 88, 'Q5-flip': 201, 'sub$1-flip': 310}
  sub-$1 by quintile: {'0': 126, '1': 90, '2': 56, '3': 38, '4': 0}
  policy sha        : 3f249ee2056ddc1a532d59f8b6df6854cb94f25116fa58132aad347e76f4e2f6
  reference match  : OK (all frozen train numbers reproduced)
```

Every commission-B reference number reproduced exactly: eligible 1142, entered 599,
pooled mean +3.71¢ (6dp 3.713072), pins 62; by source Q1-strangle 88 / Q5-flip 201 /
sub-$1 310; sub-$1 by quintile 126/90/56/38/0. Certificate written and re-accepted by
`_assert_dryrun_certificate()`.

## Test results (full tail)

`python -m pytest sim/tests/test_unseal15.py -v` → **29 passed in 0.18s**. Coverage:
Q1 race both directions (incl. sub-$1 winning despite appearing later in file order),
Q1 strangle-alone, Q2–Q4 strangle-ignored + sub-$1-enters (parametrized), Q5 sub-$1
does-not-enter / flip-EV10-enters / strangle-ignored, freshness reject + 1.0s boundary,
burned exclusion + count, one-entry-per-window (earliest wins), two-windows-two-entries,
non-contiguous-window raises, and all refusals (falsifier missing/not-frozen, census
sha mismatch, cert missing/numbers-mismatch/policy-sha-mismatch, valid cert accepted,
result-exists guard, preflight-passes-without-sealed-read, main-requires-a-mode, policy
sha stability).

Full suite regression: `python -m pytest sim/tests/ -q` → **127 passed in 15.38s**.

## Confessions / judgment calls

1. **Preflight checks markets+candles too, not just trades.** The task's item-C wording
   says "both trades series"; the commission Procedure step 2 says
   "sealed trades/markets/candles day-files all present". I followed the commission
   (superset) because `tape_sim.run()` loads markets (pairing) and candles (census)
   for each sealed day — a missing one would crash mid-sealed-run, which is worse than a
   pre-read refusal. All 6 series×kind sets verified present (17 files each).
2. **`C < 1.0` read from the CSV `C` column** (float of the 4-dp value), literally per
   the commission, NOT the `hard_floor` column. This reproduces sub-$1 310 exactly, so
   the two agree on this data; they could differ only for a C that rounds to exactly
   1.0000 while the unrounded Decimal is < 1 — none such affects the train reference.
3. **Tie-break in the Q1 race:** on an exact `t_minus_s` tie the first-listed candidate
   wins (strangle before sub-$1). No such tie occurs in the train reference; documented
   in `_select`.
4. **Streaming contiguity assumption + guard.** The evaluator assumes each window's rows
   are contiguous (true of `tape_sim`'s emission: all strangle events, then all flip
   events, per window). It keeps a seen-close_time set and raises `PolicyError` if a
   finalized window recurs — fail-closed rather than silently under-counting. The set is
   aggregate-sized (≈1142 train / ≈390 sealed strings), preserving the no-full-file-memory
   law.
5. **Per-day means keyed by day INDEX (0..16 over sorted `SEALED_DATES`), not date**, per
   the aggregates-discipline instruction. Entry `date` is retained internally only for the
   day-clustered bootstrap; it never appears in `unseal15_result.json`.
6. **Bootstrap mirrors `gate_fit.bootstrap_ci`:** `random.Random(26)`, `rng.choices(days,
   k=len(days))` resampling DISTINCT days-with-entries, pooled per-entry mean per draw,
   type-7 `percentile` at 2.5/97.5 (reused `gate_fit.percentile`). Reported in cents.
7. **`pooled_se_cents` is the naive per-entry SEM** (sample sd/√n, ddof=1) — matches the
   falsifier's SE≈2.0¢ power disclosure. The day-clustered uncertainty is the bootstrap CI;
   both are emitted and labeled.
8. **Sealed run passes `census_csv=CENSUS_CSV`** explicitly to `tape_sim.run()` so the EV
   curve is built from the sha-pinned train census (tape_sim also re-verifies the sha).
9. **Dry-run writes the certificate even on mismatch** (with `reference_match:false` and a
   `reference_mismatches` list) so a broken build is debuggable, then `main` raises. The
   sealed gate independently re-checks every number against the embedded constants, so a
   bad certificate cannot arm a read.

## Untested-by-construction

The sealed code path (`run_sealed`) is never executed by the builder or the tests — it
runs exactly once, later, by the bridge, on Brad's frozen-falsifier go. Its preflight is
fully unit-tested; its `tape_sim.run(...)` and evaluator sub-parts are the same code the
dry-run and the 29 synthetic tests exercise.

---

# FIX LOG (post-review, 2026-08-20)

Adversarial review `sim/ceremony/unseal15_review.md` returned APPROVE-WITH-FIXES; my
dry-run numbers (incl. all 30 Q1 race-order windows) were independently reproduced. Landed:

- **F1 (MEDIUM, required) — eligible/entry-rate from authoritative eligible truth, not
  CSV distinct-close_times.** A census-eligible window that emits zero tape events is
  invisible to a CSV reader, which would undercount the C4 denominator on thin sealed days.
  - *Sealed path:* `run_sealed` now captures each `tape_sim.run([d], …)` return and
    accumulates `eligible_from_tape = Σ day_agg["n_eligible_windows"]` (census truth,
    includes event-less windows). The C4 denominator is `eligible_from_tape − burned_seen`
    where `burned_seen = len(burned_excluded)` (burned windows proven eligible because they
    emitted events). Event-less eligible burned windows (≤2, not individually knowable from
    aggregates) stay in the denominator, so `entry_rate` is a slight, bounded UNDER-estimate
    — the safe direction. A hard `PolicyError` fires only on an *impossible* accounting
    ordering (authoritative < observed-with-events), never on a legitimate thin-day gap
    (which would wrongly abort — and spend — the read). The result now carries
    `eligible_windows` (denominator), `eligible_windows_tape_truth_all`,
    `eligible_windows_with_events_nonburned`, and `eligible_windows_event_less_estimate`.
  - *Train dry-run:* eligible is now summed from the full60 chunk receipts
    (`out/full60_chunks/c*/tape_receipt.json` → `n_eligible_windows`), the tape run's own
    census truth (= 1142). It asserts authoritative == CSV-with-events (0 event-less train
    windows) and still cross-checks against the pinned reference 1142. If the chunk receipts
    are absent it falls back to the CSV count but still requires it to reproduce 1142
    (`eligible_windows_source` records which path was taken).
- **F2 (MEDIUM, required) — crash-safe one-shot guard.** `run_sealed` writes an atomic
  start marker `sim/out/unseal15_STARTED.marker` with `open(…, "x")` BEFORE the first
  sealed byte; `preflight_sealed` now refuses if EITHER the marker OR the result JSON
  exists. A mid-run crash therefore leaves the ceremony fail-closed — the read counts as
  SPENT until Brad clears the marker by hand after a post-mortem. Closes the earlier
  preflight→write TOCTOU as well ('x' fails if the marker somehow already exists).
- **F4 (LOW) — sub-$1 uses the authoritative `hard_floor` column.** For flip rows the
  sub-$1 test now reads tape_sim's full-precision `hard_floor` flag when present (falls back
  to `float(C) < 1.0` only if blank). It tracks disagreements with the 4dp `float(C) < 1.0`
  test; in the dry-run (strict mode) any disagreement RAISES (train has 0), and on the
  sealed run disagreements are COUNTED and reported (`hardfloor_floatc_divergences`) rather
  than silently resolved. Removes the sealed $1-boundary ambiguity; train reference
  unchanged (0 divergences).
- **F5 (LOW) — burned-hour count reports configured AND seen.** The result now carries
  `burned_close_times_configured_count` (always 2) alongside `burned_close_times_seen_count`
  and `burned_hour_exclusion_count`, so an under-report (a burned window eligible but
  event-less, hence unseen) is visible rather than silent.
- **F3 — no code change.** The bridge regenerates the dry-run certificate fresh
  (`--dry-run-train`) immediately before arming; I regenerated it as the final step here.
- **F6 / F7 — no action** (cosmetic index-keying already commission-sanctioned; loader
  message not my file).

### Verification after fixes

Fresh dry-run (regenerated `sim/out/unseal15_dryrun.json`), verbatim:

```
TRAIN DRY-RUN — wrote C:\Users\Brads\Python_stuff\degeneracy_v3\sim\out\unseal15_dryrun.json
  eligible windows : 1142 (source: full60_chunk_receipts; csv-with-events 1142)
  hard_floor/floatC divergences: 0
  entered          : 599
  pooled mean      : 3.71 cents (3.713072 6dp)
  pins among entered: 62
  by source        : {'Q1-strangle': 88, 'Q5-flip': 201, 'sub$1-flip': 310}
  sub-$1 by quintile: {'0': 126, '1': 90, '2': 56, '3': 38, '4': 0}
  policy sha        : 3f249ee2056ddc1a532d59f8b6df6854cb94f25116fa58132aad347e76f4e2f6
  reference match  : OK (all frozen train numbers reproduced)
```

Every frozen reference number reproduced exactly (eligible now from the authoritative
chunk receipts, cross-checked equal to CSV-with-events; hard_floor authoritative gives 0
divergences → sub-$1 310 unchanged). Policy sha unchanged.

Tests: `sim/tests/test_unseal15.py` **39 passed** (was 29; +10 for F1/F2/F4/F5) — added
F4 disagreement-raises / authoritative-and-counted / hard_floor='0'-blocks-low-float-C;
F1 chunk-receipt-sum, receipts-absent-None, dry-run authoritative≠CSV raises, sealed
tape-truth−burned-seen denominator (+F5 configured-vs-seen), sealed impossible-accounting
raises; F2 marker-refusal and marker-written-before-first-sealed-open (via a monkeypatched
`tape_sim.run` that records marker existence at the first would-be sealed open, then aborts
so no sealed byte is read, and confirms the leftover marker blocks a second run). Full
suite: **137 passed** (`python -m pytest sim/tests/ -q`, 15.15s).

Post-fix sealed path still fail-closed: `preflight_sealed()` refuses today
("falsifier.md is not FROZEN"); no start marker or result JSON exists; the fresh
certificate is re-accepted by the sealed certificate gate. No sealed byte was read during
any of this work.

### Confessions on the fixes

- **F1 dry-run couples to the `full60_chunks/` layout** for the authoritative eligible
  count. This is the only always-consistent census-truth source that survives without
  re-running the 3.1 GB tape; if the chunk dir is cleaned the dry-run falls back to the CSV
  count (still guarded by the reference cross-check). The reference constant 1142 remains
  the ultimate gate either way.
- **F1 sealed denominator can over-count by ≤2** if a burned hour is census-eligible but
  event-less (so unseen and un-subtractable from aggregate returns). This biases
  `entry_rate` DOWN by at most 2/≈390 and is reported via
  `eligible_windows_event_less_estimate`; both burned hours were hand-traded so events
  almost certainly exist and `burned_close_times_seen_count` is expected to be 2.
- **F2 never auto-recovers.** By design a crash spends the read; re-arming requires a
  deliberate human marker clear + SEAL.md re-registration. This is the intended house-law
  posture for a one-shot resource, not a usability gap.
