I have completed my investigation. Writing the review document.

# ADVERSARIAL REVIEW B ΓÇö Rung 1 (statistics ┬╖ economics ┬╖ artifacts)

**Reviewer:** opus48, independent, no authorship stake. **Session:** fresh; no sealed file (2026-08-02..18) opened by any means ΓÇö the corpus stops at 2026-08-01 and I confined all scratch to `_reviewB_tmp/`.
**Verdict: CONDITIONAL PASS ΓÇö do not perform the frozen train run until B1, B2, and B3 are resolved.** The economic core is correct and reproduces to the cent; the fee goldens, complement identity, EV identity, type-7 percentile, day-clustered bootstrap, determinism, and A2.6 labeling all survived adversarial checks. But I found one **build-blocker in the freeze mechanism** (fail-closed, but it deadlocks the sealed read), one **spec deviation that silently drops OK hours** (A2.5), and one **un-ratified degree of freedom** (╧â╠é window) that the ceremony forbids leaving open ΓÇö plus several mediums.

Everything below is receipts, not assurances.

---

## What I verified GREEN (hand-checked / executed)

| Claim | Evidence |
|---|---|
| Fee goldens | `census.fee` ΓåÆ `0.0172, 0.0174, 0.0128, 0.0069` reproduce exactly |
| C10 EV identity | Algebra: `(1ΓêÆp)(1ΓêÆC╠ä) ΓêÆ p┬╖C╠ä Γëí (1ΓêÆp) ΓêÆ C╠ä Γëí mean(per-row payoff)`; verified on smoke bucket 0 (`EV_base 0.005986 = 1 ΓêÆ 0 ΓêÆ 0.994014`) |
| Type-7 percentile | Matches `numpy.percentile(...,method='linear')` to <1e-9 at 2.5/25/50/75/97.5; `quintile_edges` matches `np.quantile` |
| Economics, both legs | `C = leg + fee(leg)` per leg; NO leg uses its own traded price `1ΓêÆyes_bid` and `fee(1ΓêÆyes_bid)`; WORST = `ask.high` and `1ΓêÆbid.low`; no BASE/WORST or 15M/1H candle cross-contamination |
| Payoff signs | escape ΓåÆ `+(1ΓêÆC)`, pin ΓåÆ `ΓêÆC`, per column |
| Impossible-outcome hard fail | For G ΓëÑ $0.01 the `H:yes Γêº L:no` combination is genuinely unreachable in both orientations ΓåÆ hard fail cannot fire on legal data |
| A2.6 notice **verbatim** | Lands identically in `gate.json` (`descriptive_ci_notice`) and `gate_report.md` (blockquote) ΓÇö confirmed by running gate_fit on the smoke census |
| Determinism | Re-ran gate_fit twice on identical input ΓåÆ byte-identical (`caseA_gate.json` sha `58a2ba72ΓÇª` before==after); my rerun of the smoke gate == the build's `gate_smoke.json` |
| Gate mechanics | Synthetic Case A ΓåÆ gate correctly stops at `[0,1]`, `g*=edges[1]`; Case C (all pins) ΓåÆ empty, reported plainly |
| Day-clustering bites correctly | Synthetic marginal gate `[0,1]` (pooled EV 0.30, LB 0.10) ΓåÆ **adding one all-pin day collapses it to empty** ΓÇö the LB moves the right way |
| `acknowledge_sealed_read=True` | Exactly one production path: `unseal_runner.py:106`. (The only other hit, `test_loader.py:77`, uses a synthetic `data_root`.) |
| No thresholds in the runner | `unseal_runner.run()` computes C1ΓÇôC5 *quantities* only; no `[ΓêÆX]/K/N/margin` literals ΓÇö verdicts are left to the bridge against frozen `falsifier.md` |
| Quintile boundaries | Computed on OK rows only (`read_ok_rows` filters `status=="OK"`); excluded/NO_PAIR hours correctly cannot enter bucketing |
| Test/fee reproduction | `42 passed in 0.47s` on my machine |

---

## BLOCKERS

### B1 ΓÇö The freeze is a self-referential deadlock; the passing test masks it. (fail-closed, but the sealed read can never start)

`unseal_runner.preflight()` treats the falsifier as frozen iff the substring `"NOT FROZEN"` is **absent**. But `falsifier.md` contains that substring on **two** lines:

- line 3 (`STATUS: **DRAFT ΓÇö NOT FROZEN.**`)
- line 6 (`sim/unseal_runner.py refuses to run while this line reads NOT FROZEN.`)

The natural freeze ΓÇö editing the STATUS line ΓÇö leaves line 6 intact. I simulated exactly that against the real file (copied to scratch, original untouched):

```
occurrences of 'NOT FROZEN' after naive freeze: 1
PREFLIGHT REFUSED -> REFUSE: falsifier.md reads NOT FROZEN...
```

So the documented freeze path does not work: whoever freezes must know to scrub *every* occurrence of the string, including the sentence that documents the mechanism. This is **fail-closed** (it refuses when it shouldn't run ΓÇö the safe direction, never a false sealed read), so it is not a seal breach. But it will block the one-shot read at go-time, and ΓÇö worse ΓÇö the test suite **falsely certifies** the freeze path: `test_preflight_passes_when_all_present_and_frozen` uses clean synthetic text (`"STATUS: FROZEN 2026-09-01\nclauses..."`) with no self-reference, so it never exercises the real file's second occurrence.
**Fix:** key the check on a dedicated positive line (e.g. a `STATUS:` line equal to `FROZEN`), not a whole-file substring scan; and add a test that freezes the *actual repo file* and asserts preflight passes.

### B2 ΓÇö WORST-column availability determines pair inclusion, contradicting A2.5. (unconfessed; silently drops OK hours)

A2.5 is explicit: "WORST-column values ΓÇª never determine pair inclusion." But `build_census` (line ~391) drops the hour to `NO_PAIR` when **`cw is None`**, and `cw` is None whenever a WORST field (`yes_ask.high_dollars` or `yes_bid.low_dollars`) is absent ΓÇö even if BASE close is fully quoted. Demonstrated:

```
ya_b 0.30  ya_w None  na_b 0.45  na_w 0.60
quoted_base(BASE) = True   C_base = 0.7821   C_worst = None
Inclusion decision -> NO_PAIR (dropped)
```

A BASE-quoted, economically valid hour is excluded because a *fidelity-bounded descriptive* field is missing. This is unconfessed (C4 covers only the `ask>0` bound). It is a **selection effect**: if WORST-field absence correlates with anything (thin/no-trade minutes), the census EV is biased, and the `NO_PAIR` receipt cannot distinguish "BASE unquoted at TΓêÆ5" from "WORST field missing" ΓÇö both share one reason code.
**Fix:** decide inclusion on `quoted_base(ya_b, na_b)` and `cb is not None` only; if `cw is None`, keep the row OK and write WORST as blank/`n/a` (it is descriptive). If you instead intend to require WORST present, that is a spec change and needs an amendment + a distinct reason code.

### B3 ΓÇö The ╧â╠é window is an un-ratified degree of freedom the ceremony forbids leaving open. (confessed C1; not rank-invariant)

`sigma_hat` uses the 9 anchors at close-times `T, TΓêÆ15m, ΓÇª, TΓêÆ120m` (8 diffs). Two things are unpinned by A2.1 and were resolved *by the builder*, not the commission:

1. **Whether A(T) is included.** The newest anchor is `A(T)` itself ΓÇö the same anchor that forms the numerator `G = |KΓêÆA(T)|`. Including it (known at TΓêÆ15 ΓåÆ no lookahead, verified) vs starting at `A(TΓêÆ15)` both yield 9 anchors/8 diffs and shift the window one 15-min step.
2. **"2h15m span."** A2.1 says "(2h15m span)". The implemented window spans **2h** of close-times (TΓåÆTΓêÆ120m); its underlying observations reach `TΓêÆ15m ΓÇª TΓêÆ135m` ΓÇö i.e. the *oldest observation is 2h15m back from T*, but the *span* of the window is 2h00m. C1 rationalizes it as the reaches-back reading; under the literal "span" reading it is off by one step.

C1 itself flags this as "the single most consequential interpretation ΓÇª NOT rank-invariant ΓåÆ can move quintile boundaries." In a ceremony whose entire premise is "no degree of freedom is chosen after (or without) explicit pre-commitment," an unpinned, boundary-moving DOF baked silently into code is precisely the failure mode to stop. It errs safe only in that the choice is made *before* the run ΓÇö but it was never ratified in the commission.
**Fix:** the bridge must state, as a written amendment before the frozen run, exactly which 9 anchors define ╧â╠é (include/exclude A(T); span 2h vs 2h15m), so the boundaries are not a builder artifact.

---

## MEDIUM

### B4 ΓÇö `EXCL_SIGMA_TAPE` fires mid-tape in the train run, deviating from A2.1, and conflates two causes under one reason code. (confessed C6)

A2.1: "Exclusion only at the genuine head-of-corpus." But the smoke already shows `EXCL_SIGMA_TAPE` at **2026-07-16 10:00/11:00** ΓÇö a mid-tape gap, not head-of-corpus ΓÇö and 07-16 is a **TRAIN** day, so this **will** fire in the frozen run, not just at 06-11. Excluding (vs hard-failing) is the sane choice, but (a) A2.1 as frozen does not authorize it, so per the ceremony's "deviations require a written amendment before the run" rule this belongs in the commission, not only in the build report; and (b) head-of-corpus and mid-tape gaps share the single code `EXCL_SIGMA_TAPE`. They are auditable *only* because each hour is itemized by date ΓÇö `status_counts` and any consumer keying on the reason code cannot tell them apart.
**Fix:** ratify the mid-tape behavior as an amendment and split the reason code (e.g. `EXCL_SIGMA_HEAD` vs `EXCL_SIGMA_GAP`).

### B5 ΓÇö Degenerate quintiles are unguarded; heavy G/╧â╠é ties silently turn the gate into "play everything." (unconfessed)

`quintile_edges` is value-based, so ties are not distributed into equal-count buckets. With heavy ties the interior edges collapse and low buckets go empty. Synthetic (18 rows at gos=1.0, 2 at 5.0):

```
edges       [1.0, 1.0, 1.0, 1.0]
bucket ns   [0, 0, 0, 0, 20]
gate        [0, 1, 2, 3, 4]  empty=False  g_star=5.0
```

The gate reports the **full range** with four empty buckets ΓÇö i.e. *no gating* ΓÇö with no warning, no receipt, no minimum-n or non-degenerate-quintile guard. In the sealed application (`bucket_of(gos, edges)` with collapsed edges) every hour would be gated. On the real train data ╧â╠é is a continuous stdev, so full collapse is improbable (smoke edges are distinct: 0.106/0.226/0.366/0.573) ΓÇö hence MEDIUM, not blocker ΓÇö but a *partial* collapse (two adjacent equal edges ΓåÆ one empty bucket, one double-mass bucket) is plausible and equally silent.
**Fix:** assert edges are strictly increasing (or emit a degeneracy receipt) and fail-closed/flag if any bucket is empty.

### B6 ΓÇö The pooled-prefix gate rule is non-monotone: a bleeding top bucket can be admitted by dilution. (spec-faithful; interpretation caveat)

`fit_gate` scans `k=5ΓåÆ1` and returns the **first** (largest) prefix whose *pooled* LB>0. If the full pool clears, the gate is `[0,1,2,3,4]` even when the top bucket individually is deeply ΓêÆEV but diluted by the low buckets. This is a literal reading of ┬º8 ("largest contiguous set from the low end whose pooled LB>0"), and on real data EV is expected to decrease monotonically in G/╧â╠é (so it coincides with extend-while-positive) ΓÇö but the bridge should read `gate_buckets=[0,1,2,3,4]` as *unconditional play*, and know that the pooled LB>0 can be carried by the low buckets while the top bucket loses money. Consider requiring each included prefix (or each bucket) to individually clear, or at least surfacing per-bucket LBs alongside the pooled verdict (they already are in the table ΓÇö good).

### B7 ΓÇö Preflight requires the train census on disk but never verifies it matches the frozen gate. (nominal reproducibility)

`gate.json` records `census_csv_sha256`, but `preflight()` only checks `census_train.csv`/`census_receipt.json` **exist** ΓÇö it never reads them or cross-checks their sha against the frozen gate's recorded hash (and the runner never uses the train CSV for the read; it rebuilds sealed census fresh). So a stale or mismatched train census would pass. The stated guarantee ("the gate is not reproducible" otherwise) is nominal.
**Fix:** in preflight, hash `census_train.csv` and require it equals `gate.json["census_csv_sha256"]`.

---

## LOW / notes for the bridge

- **B8 ΓÇö `unseal_result.json` embeds the full per-hour sealed exclusion inventory** (`census_receipt_exclusions`, itemized as `"<date> <T>"` per excluded sealed hour). It does **not** leak gated pin/escape/payoff (those stay aggregate: `EV_base`, `pin_rate`, `days_negative` are day/aggregate level ΓÇö verified), but a strict "aggregates only" read would reduce exclusions to per-reason counts rather than hour lists.
- **B9 ΓÇö C8's skip-empty-draws cannot bias the CI because it never triggers.** `by_day` days always hold ΓëÑ1 row, so a resampled pool is never empty; `boot_draws_used_*` is always `n_draws` (10000, confirmed in smoke). Good ΓÇö but that also means `boot_draws_used` is a constant, not a diagnostic. The skip branch is effectively dead code; harmless.
- **B10 ΓÇö `preflight()`'s return value (falsifier text) is discarded** in `run()`. The docstring says the runner "reads its clauses at runtime"; it reads the file only to check frozen status and never parses thresholds (correct behavior ΓÇö bridge applies thresholds ΓÇö but the docstring overstates).
- **B11 ΓÇö expiration_value integrity is a *string* compare across legs (C16).** A pure formatting difference (`64512.33` vs `64512.3300`) would hard-fail and abort the frozen run. Fail-closed and safe, but a formatting drift in the API would abort on a non-issue; consider comparing as Decimal for the identity check too.
- **B12 ΓÇö `census._is_top_of_hour` is dead after the C7 grid change** (confessed C15) ΓÇö noted so it is not mistaken for a live path.

---

## Bottom line

The numbers that reach an artifact are, where I could execute them, correct: fees, EV identity, percentile/quantile, day-clustered bootstrap, determinism, and the verbatim A2.6 label all hold. The risks are not wrong arithmetic but **(B1)** a freeze mechanism that will refuse the very read it guards and whose test masks it, **(B2)** a silent A5-violating drop of OK hours on a descriptive field, and **(B3)** an unpinned ╧â╠é window that moves the quintile boundaries the ceremony exists to fix in advance. B1ΓÇôB3 are cheap to resolve and none require retuning an economic constant ΓÇö they are specification/mechanism tightenings that must land **before** the bridge's one frozen run.

*Scratch (synthetic CSVs, generators, freeze/worst demonstrations) left in `_reviewB_tmp/`; nothing outside it was modified.*
