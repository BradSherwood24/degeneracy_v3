# ADVERSARIAL REVIEW — `sim/unseal_runner15.py` (Rung 1.5 sealed-read runner)

Reviewer: opus48 (second, adversarial). Date: 2026-08-20. Reviewed against
`unseal15_commission.md`, `falsifier.md` SECTION 2, and the interfaces in
`loader.py` / `tape_sim.py` / `gate_fit.py` / `census.py`. No sealed byte was read
(deny rules + seal guard hold; all verification used TRAIN + synthetic data).

## VERDICT: APPROVE-WITH-FIXES

The frozen policy is implemented correctly — I reproduced every train reference
number **exactly** with an independent, grouping-based reimplementation written
from the commission text (not copied from the builder). The two most dangerous
traps (cross-direction `t_minus_s` ordering and window contiguity) are handled
correctly and I have empirical proof they matter and are resolved. No defect
threatens sealed-data safety, and none can corrupt the economic verdict clauses
(C1/C2/C3). The fixes below concern **one clause quantity (C4 entry-rate)** and
**one-shot crash integrity**; they should land before arming, but the read is not
*unsafe* if Brad chooses to arm as-is with the stated caveats.

---

## INDEPENDENT DRY-RUN — my numbers vs the pinned reference

My reimplementation streamed all 18,308,352 rows of `sim/out/full60/tape_points.csv`
and selected entries by an independent method: per contiguous window, filter fresh
rows (`max(leg ages) <= 1.0`), route by quintile, and pick the earliest-qualifying
event by **largest `t_minus_s`** across the routed sources (grouping approach, not
the builder's per-direction first-seen streaming).

| quantity | reference (commission) | builder dry-run | MY independent run | match |
|---|---|---|---|---|
| eligible windows | 1142 | 1142 | **1142** | ✓ |
| entered | 599 | 599 | **599** | ✓ |
| pooled mean (2dp) | +3.71¢ | 3.71 | **3.71** | ✓ |
| pooled mean (6dp) | 3.713072 | 3.713072 | **3.713072** | ✓ |
| pins among entered | 62 | 62 | **62** | ✓ |
| Q1-strangle | 88 | 88 | **88** | ✓ |
| Q5-flip | 201 | 201 | **201** | ✓ |
| sub$1-flip | 310 | 310 | **310** | ✓ |
| sub-$1 by quintile | 126/90/56/38/0 | 126/90/56/38/0 | **126/90/56/38/0** | ✓ |

Two independent implementations agree to the cent and to the exact entry
composition. The policy logic is correct.

### Trap probes (diagnostics from the same run)

- **Contiguity**: `noncontig_recurrences = 0` — every window's rows are contiguous
  in the file. The builder's streaming `_seen`-set guard (`feed_row`, line 214-219)
  is a correct fail-closed check; it will not false-positive on the real emission
  order.
- **Within-direction monotonicity**: `monotonic_violations = 0` — within each
  direction `t_minus_s` is strictly non-increasing, so the builder's "first fresh
  qualifying row seen = largest `t_minus_s`" shortcut is exactly equivalent to
  max-by-`t_minus_s`. Confirmed valid.
- **Ordering trap IS real and IS handled**: in **30** Q1 windows the source chosen
  by naive file-order-first (strangle block precedes flip block) differs from the
  correct `t_minus_s`-max winner. The builder's cross-direction `max(cands, key=
  t_minus_s)` (`_select`, line 189) resolves all 30 the same way my independent
  method does — hence the exact 88/310 split. A naive "first row in file" selector
  would have mis-scored 30 windows. The builder passes this trap.
- **`ev` boundary**: `fresh_ev == "0.050000"` count = 0, `== "0.100000"` = 0, and
  `float(ev) >= thr` vs `ev*100 >= thr*100` decision diffs = 0. The ev threshold is
  robust on train; the two conventions are indistinguishable here.
- **`C` boundary**: `fresh_flip_hf_vs_floatC_disagree = 0` — for every fresh flip
  row, `(float(C_4dp) < 1.0)` equals `hard_floor == "1"`. Reading the CSV `C`
  column (builder's choice, `feed_row` line 237) gives identical sub-$1
  classification to tape_sim's own full-precision riskless flag on train (see F4).

---

## FINDINGS (ranked by severity)

### F1 — MEDIUM. `eligible_windows` / `entry_rate` (C4 input) is derived from the CSV, not census truth; undercounts on thin sealed days.

`PolicyEvaluator.eligible_windows` counts only **windows that emitted ≥1 event row**
(`_finalize_window`, line 205 — a window is counted eligible only once `feed_row`
has seen a row for it). `run_sealed` (line 561-562) calls `_tape_sim.run([d], ...)`
but **discards its return value**, which carries the authoritative census
`n_eligible_windows`. It also does not read the per-day `tape_receipt.json`
(`n_eligible_windows`).

- On TRAIN this is invisible: distinct-close_times-with-events (1142) equals census
  eligible (1142) — every eligible train window happened to have a tape event.
- On SEALED data a census-eligible window (status OK or NO_PAIR) is **not**
  guaranteed to produce an evaluation event: it needs a same-side taker fill in the
  final 15 min on one leg while the other leg is live. On the SEAL.md-disclosed thin
  days (08-06, 08-13 short; 08-07, 08-14 one hour short) event-less eligible windows
  are plausible. Each such window is silently dropped from the denominator.
- Effect: `entry_rate` (line 501; `clause_inputs.C4_entry_rate`, line 534) is biased
  **upward** (denominator too small). The commission's C4 is explicitly "entry rate
  on **eligible** sealed windows" using the same census eligibility law as train.

**Bounded blast radius:** this affects only C4 (participation / VOID-REGIME band
[25%,75%]). It does **not** touch the entry set, so C1/C2/C3 (the actual DEAD /
SURVIVE decision) are unaffected. To flip C4 out of band you would need ~30%+ of
eligible windows to be event-less — unlikely on a tape with trades. But it is a
clause quantity computed wrong-in-principle while the correct value is free.

**Fix:** accumulate `sum(day_agg["n_eligible_windows"])` from each `tape_sim.run`
return, subtract the census-eligible burned windows, and use that as the C4
denominator; cross-check against the CSV-derived count and hard-fail if they diverge
by more than the burned count (defense-in-depth). This is the only finding I would
insist land before arming for a *clean* C4.

### F2 — MEDIUM. One-shot guard only refuses after a SUCCESSFUL prior run; a mid-run crash leaves the read re-runnable.

`preflight_sealed` refuses if `unseal15_result.json` already exists (line 458), and
`run_sealed` writes that file only at the very end (line 574). Between the first
sealed open (line 561, day 0) and the final write, **no marker exists**. If the run
aborts on, say, day 12 (an `IntegrityError` from `tape_sim`, a missing day-file, a
kill), `unseal15_result.json` is never written and a second invocation sails through
preflight and re-reads the sealed bytes. House law says build refusals that *enforce*
the rule rather than trusting the operator to "run once"; the current guard trusts
completion. (There is also a benign TOCTOU between the exists-check and the final
write, not a realistic concern for a single-operator ceremony, but the same fix
closes it.)

**Fix:** write an atomic **start marker** BEFORE the first sealed open — e.g. create
`unseal15_result.json` with `{"STATUS":"IN_PROGRESS"}` (or a sibling `.lock`) and
have preflight refuse if it exists. A crash then leaves the ceremony fail-closed:
re-running requires a deliberate human seal re-registration (removing the marker /
editing SEAL.md), which is exactly the gate a one-shot resource deserves. Correctly
fail-closed even at the cost of never auto-recovering.

### F3 — LOW. `run_dry_run_train` certificate is not bound to an actual execution — only to matching numbers + policy sha.

`_assert_dryrun_certificate` (line 397) accepts any JSON file whose numbers equal the
embedded `REFERENCE` and whose `policy_params_sha256` equals the current code's. A
hand-written cert with the (publicly-known) reference numbers and the deterministic
policy sha would pass without ever running the dry-run. Given the trusted-operator
model this is not an attack, but it defeats the cert's purpose (proving the evaluator
reproduces train) if the evaluator later drifts and the cert is stale.

**Fix / operational rule:** the bridge should regenerate the cert fresh
(`--dry-run-train`) immediately before arming, not trust a pre-existing file. (The
sealed run does re-derive and re-check `POLICY_PARAMS_SHA256` against the code, so a
policy-code change *is* caught — this only concerns evaluator/tape drift with a
stale cert.)

### F4 — LOW (sealed fidelity). Sub-$1 flip uses the 4dp-rounded CSV `C`, which can straddle the $1 boundary on sealed data.

`feed_row` reads `C = float(r["C"])` (4dp) and tests `C < 1.0` (line 237). On train,
`hard_floor` (tape_sim's full-precision `Decimal(C) < 1`) never disagrees with the
4dp float test (diagnostic = 0), so this is invisible in the dry-run. On sealed data
a true `C ∈ [0.99995, 1.0)` would print as `"1.0000"` → `float 1.0` → excluded by
the builder, while tape_sim's `hard_floor` column would mark it `"1"`. This is a
handful-of-rows fidelity gap that would make the builder slightly *more*
conservative (drop a marginal sub-$1). The commission literally says "flip at
C < $1.00" on the C column, so the builder is defensible per spec.

**Optional fix:** for flip sub-$1 detection read the `hard_floor` column (`r["hard_floor"]
== "1"`) instead of the rounded `C`, to match tape_sim's own riskless determination
exactly. Reproduces the train reference identically (diagnostic = 0) and removes the
sealed boundary ambiguity. Not required.

### F5 — LOW. Burned-hour exclusion count depends on the burned windows appearing in the CSV.

`burned_excluded` is populated only when a burned close_time is *seen* in the stream
(`_finalize_window`, line 202-204). If a burned window is census-eligible but
produced no tape events, it is never seen, and `burned_hour_exclusion_count`
(line 512) comes out < 2 — contradicting the commission's "exactly the two close_times
excluded and counted." The **safety** property is intact regardless (a burned window
can never enter the eligible/entry counts — if unseen it also contributes nothing),
but the receipt could under-report. Brad hand-traded both hours so events almost
certainly exist for them, but the count is not guaranteed.

**Fix (cheap):** cross-check the two burned close_times against census eligibility
for 08-18 (available from the tape_sim return) and assert the exclusion count is 2,
or report "expected 2, saw N" explicitly.

### F6 — LOW / cosmetic. Aggregates discipline: index-keying provides no real obfuscation; burned timestamps are emitted.

- `per_day_entry_means_by_index` (lines 486-497) keys per-day means by index 0..16
  over `sorted(SEALED_DATES)`. Since `SEALED_DATES` is public (loader/SEAL.md), index
  i deterministically = the i-th sealed date — the "not keyed by date" claim
  (confession #5) is cosmetic. **However**, per-day means are *explicitly sanctioned*
  by commission step 5 ("per-day means for the bootstrap"), so this is permitted
  output, not a leak. Worth stating plainly rather than implying obfuscation.
- `burned_close_times_excluded` (line 511) emits the two burned close_time strings.
  These are public constants (named in the commission and SEAL.md), so no new
  information escapes, but the commission said burned hours are "reported only as an
  exclusion count." Dropping the list (keeping the count) would honor the letter.
- Confirmed **clean**: no per-window rows, no individual trade timestamps, no prices,
  and no non-burned close_times appear anywhere in `unseal15_result.json` or stdout.
  The per-window sealed CSVs live under `sim/out/sealed_eval/` and that path IS
  deny-ruled (`.claude/settings.json` line 25: `Read(./sim/out/sealed_eval/**)`).

### F7 — INFORMATIONAL. Loader's seal-guard message still names the Rung 1 runner.

`loader._guard_seal` (loader.py line 133-134) says "Only sim/unseal_runner.py may
pass True." The sanctioned caller for this read is `unseal_runner15.py`. The loader
does not (and cannot easily) enforce *which* module calls it, so this is only a stale
message, not a functional gate; the commission explicitly names `unseal_runner15.py`
as the sole sanctioned caller. Harmless; note for tidiness.

---

## THINGS I CHECKED THAT ARE CORRECT

- **Per-day chunking vs a single run (memory law).** `run_sealed` runs `tape_sim.run`
  one day at a time and streams each day's CSV (line 555-569); only aggregate-sized
  evaluator state persists. I confirmed day-boundary windows are not lost or
  double-counted: the census grid for day D spans D 01:00 … D+1 00:00
  (`census.build_census` line 291-297), and the boundary market that closes
  D+1 00:00:00Z is **dual-filed** in both adjacent day-files (verified on train:
  `2026-06-14T00:00:00Z` appears in both the 06-13 and 06-14 markets files). So a
  per-day run of D loads its own D+1 00:00 boundary market and covers that window;
  the next day's grid starts at 01:00, so no double count. Per-day chunking
  reproduces the single-run eligible set.
- **Bootstrap.** `_day_clustered_bootstrap_cents` (line 268) mirrors
  `gate_fit.bootstrap_ci` exactly: `random.Random(26)`, `rng.choices(days,
  k=len(days))` over the **distinct days with entries**, pool with multiplicity,
  mean per draw, type-7 percentile at 2.5/97.5 (`gate_fit.percentile`), ×100 for
  cents. This matches the falsifier's "day-clustered, seed 26, 10,000 draws." Note
  the resample universe is days-with-entries (the codebase's canonical convention,
  identical to gate_fit) — if any sealed day has zero entries it is excluded from the
  resample; at ~200 entries over 17 days this is almost certainly moot. Degenerate
  cases handled: 1 day → CI collapses to the point estimate; 0 entries → (None, None)
  and `bootstrap_ci95_cents = [None, None]`, C2 input None for the bridge.
- **Preflight ordering / no sealed byte before the gates.** `run_sealed` calls
  `preflight_sealed()` first (line 550). Preflight reads only `falsifier.md`, hashes
  the TRAIN `census_train.csv`, checks `os.path.exists` on sealed paths (metadata
  only), and validates the cert — no loader call with `acknowledge_sealed_read=True`.
  Verified by `test_preflight_passes_reads_no_sealed_byte`. Defense-in-depth: even if
  preflight were skipped, `loader._guard_seal` → `_assert_falsifier_frozen` refuses
  every sealed open unless a line reads exactly `STATUS: FROZEN`.
- **Census sha pin.** Runner constant = `580d143f…bb247`, equals the on-disk
  `census_train.csv` sha (verified) and tape_sim's own pin; preflight checks it
  (line 437) and passes `census_csv=CENSUS_CSV` to `tape_sim.run` which re-verifies.
- **Freshness boundary.** `max(high,low) > 1.0` → skip (line 223) = inclusive ≤ 1.0,
  matching the reference; `test_freshness_boundary_one_second_ok` covers exactly 1.0.
- **Policy fingerprint.** `POLICY_PARAMS_SHA256` recomputed and re-checked against the
  cert (line 405); a policy-code change refuses the sealed read.
- **Test suite.** `pytest sim/tests/test_unseal15.py -q` → **29 passed**. They cover
  the Q1 race both directions (incl. sub-$1 winning despite later file order), Q2-Q4
  strangle-ignored, Q5 sub-$1-ignored / EV-10 enters, freshness, burned exclusion,
  one-entry-per-window, non-contiguity raise, and every refusal path.

## MISSING TESTS THAT MATTER

1. **Zero-event eligible window (F1).** No test exercises a census-eligible window
   that emits no tape rows — and *it cannot be expressed as a CSV fixture*, because a
   window with no rows is invisible to a CSV-reading evaluator. This is precisely why
   the eligible count must come from `tape_sim`'s return, not the CSV. The blind spot
   is structural, not an oversight in the fixtures.
2. **Mid-run crash re-run (F2).** No test asserts that a partial/aborted sealed read
   blocks a second invocation (there is no marker to assert on yet).
3. **Cross-day accumulation / boundary window (per-day chunking).** Tests feed a
   single synthetic CSV; none feed two day-files in sequence to confirm the last
   window of file N finalizes when file N+1's first row arrives and that a boundary
   close_time is counted exactly once. The logic is correct (I traced it and verified
   the dual-filing), but it is untested at the `feed_csv`-across-days seam.

---

## ARMING RECOMMENDATION

The policy math is correct beyond reasonable doubt (two independent implementations,
exact agreement, ordering + contiguity traps proven handled). Nothing here can leak
sealed data or corrupt the economic verdict (C1/C2/C3).

**Recommended:** land **F1** (eligible denominator from census truth) and **F2**
(one-shot start marker) before arming — both are small, and F2 in particular is the
kind of code-enforced fail-closed guard house law asks for on an unrepeatable
resource. Regenerate the dry-run certificate fresh (F3) as the last step before the
sealed run.

**If Brad chooses to arm as-is** (the fixes are refinements, not safety defects):
the read is safe to execute once **provided** (a) the bridge treats
`clause_inputs.C4_entry_rate` as an **upper bound** on the true participation rate
(denominator may be short by any event-less eligible windows), and (b) any mid-run
crash is treated as spending the read — a re-run requires deliberate human seal
re-registration, since the runner will not currently stop it.

Safe to arm **after F1 + F2**, on Brad's explicit freeze of `falsifier.md`
(`STATUS: FROZEN`).

---

# FIX-VERIFICATION PASS (2026-08-20) — post-fix re-review

The builder landed F1, F2, F4, F5 (F3 = operational, F6/F7 = no action). I re-read the
whole runner and the FIX LOG, verified each independently on TRAIN + synthetic data
only, and read no sealed byte. **VERDICT: APPROVE FOR ARMING.**

### F1 — eligible/entry-rate from authoritative census truth. VERIFIED CORRECT.
- **tape_sim contract confirmed:** `run()` returns `agg` carrying `n_eligible_windows`
  (`tape_sim.py` `_aggregate`, and `run_sealed` line 685-687 captures it), and it means
  census-eligible (`eligible = [r for r in rows if r["status"] in ("OK","NO_PAIR")]`,
  `n_eligible = len(eligible)`). The same field is in `tape_receipt.json`.
- **Dry-run source real:** `out/full60_chunks/c0..c7/tape_receipt.json` each carry
  `n_eligible_windows`; I summed them independently → **1142**, exactly matching the CSV
  with-events count and the pinned reference. `_train_eligible_from_receipts` (line 371)
  returns 1142 and the dry-run asserts authoritative == CSV (line 419).
- **Sealed denominator:** `eligible_from_tape − burned_seen` (line 595). I traced the
  accounting: every window with event rows is census-eligible, so
  `distinct-with-events ≤ Σ n_eligible_windows` always — hence `event_less_estimate ≥ 0`
  in every legitimate run. The `PolicyError` (line 600) fires **only** on the impossible
  `authoritative < observed-with-events` case, never on a legitimate thin-day gap. Test
  `test_f1_sealed_impossible_accounting_raises` confirms; `test_f1_sealed_eligible_from
  _tape_truth_minus_burned_seen` confirms the exact arithmetic (10−1=9, rate 2/9).

### F2 — crash-safe one-shot marker. VERIFIED CORRECT.
- Marker written with `open(STARTED_MARKER, "x")` at line 673 — **after** preflight
  (667, which reads no sealed byte) and **before** the day loop's first
  `_tape_sim.run(..., acknowledge_sealed_read=True)` (685). Preflight refuses on marker
  OR result (542-552).
- **Ordering test genuinely proves it:** `test_f2_marker_written_before_first_sealed
  _open` monkeypatches `_tape_sim.run` (the exact point of the first sealed open) to
  record `os.path.exists(STARTED_MARKER)` at first call, then raises before reading any
  byte — and asserts the marker already existed (`is True`) and that a second run is
  blocked by the leftover marker. This is a true before-first-sealed-open proof, and no
  sealed byte is read.
- **No sealed-read-without-marker path found within the runner:** `run_sealed` is the
  only sanctioned caller and always writes the marker first; the `"x"` mode closes the
  preflight→write TOCTOU; a marker-write exception aborts before the loop. A *direct*
  `tape_sim.run(..., acknowledge_sealed_read=True)` outside this runner would bypass the
  marker, but that is still gated by the loader's A3.7 (`_assert_falsifier_frozen` on
  every sealed open) — defense-in-depth intact.

### F4 — hard_floor authoritative for sub-$1. VERIFIED CORRECT.
- `_is_sub_dollar` (line 260) reads tape_sim's full-precision `hard_floor` when present
  (`"0"/"1"`), falls back to `float(C)<1.0` only if blank, counts divergences, and RAISES
  in strict mode (dry-run, line 402). Dry-run reports **0 divergences** → sub-$1 **310
  unchanged**. Three tests cover raise-in-strict, authoritative-and-counted, and
  `hard_floor='0'` blocking a low-float-C row.

### F5 — configured vs seen burned counts. VERIFIED CORRECT.
- Result emits `burned_close_times_configured_count` (2),
  `burned_close_times_seen_count`, and `burned_hour_exclusion_count` (lines 619-622);
  an under-report is now visible, not silent. Covered by the F1 sealed test.

### Confession-5 arithmetic (denominator over-count ≤2 → rate bias DOWN). I AGREE.
Denominator = `eligible_from_tape − burned_seen` leaves any *census-eligible but
event-less burned hour* (≤2) in the denominator, so it over-counts by k ∈ {0,1,2} and
biases `entry_rate` **DOWN** by at most `k/E`. At the train rate ~52.5% with E ≈ 388
non-burned eligible: reported ≥ true × E/(E+2) = 0.525 × 388/390 = **0.5223**
(a ≤0.51pp reduction). This can never push rate **above** 75% (down-bias only), and to
cross **below** 25% the true rate would have to already be ≈25.5% — a regime collapse
C4 would flag on its own. **At train-rate ~52.5% the ≤2-window bias cannot flip a
legitimate reading into VOID-REGIME.** Correct and safely disclosed via
`eligible_windows_event_less_estimate`.

### Suite + gate re-run (mine)
- `pytest sim/tests/test_unseal15.py -q` → **39 passed**; full suite → **137 passed**.
- Fresh `--dry-run-train` (streamed the 3.1 GB tape myself): eligible **1142**
  (source `full60_chunk_receipts`, csv-with-events 1142), divergences **0**, entered
  **599**, mean **3.71¢ / 3.713072**, pins **62**, sources **88/201/310**, sub-$1
  **126/90/56/38/0**, policy sha `3f249ee2…e2f6`, reference match OK.
- Confirmed fail-closed *now*: no `unseal15_STARTED.marker`, no `unseal15_result.json`;
  `preflight_sealed()` still refuses on `falsifier.md` not FROZEN.

### FIX VERDICT: APPROVE FOR ARMING
All four required/landed fixes are correct and complete; no regression to the frozen
reference; no new sealed-read path; the one-shot guard is now code-enforced and
crash-safe. The runner is safe to arm for the single sealed read the moment Brad
freezes `falsifier.md` (`STATUS: FROZEN`). No blocking findings remain.
