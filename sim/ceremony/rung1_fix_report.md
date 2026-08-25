# FIX REPORT — Rung 1 (Amendment A3 implementation)

**Fixer:** opus48 (Opus 4.8, per Brad's V3 mandate). **Date:** 2026-08-19.
**Ceremony position:** commission → design review → build → adversarial review A & B
(findings adjudicated into Amendment A3) → **THIS FIX PASS** → bridge verification → one
frozen TRAIN run by the bridge.
**Scope:** implement A3.1–A3.11 (commission §11) EXACTLY — nothing more, nothing less.
Every judgment call is confessed below.
**Verdict:** FIX-COMPLETE. 55/55 unit tests pass (42 prior + 13 new). The A1 crash is
gone: the three reviewer-A repro days and two other untouched windows build with zero
diffs against the prior economic engine. No sealed-day byte was read by any means; the
frozen 52-day run remains the bridge's to perform.

Everything below is receipts (counts, shas, measured values), not assurances.

---

## 1. What each amendment clause required, and exactly what I changed

### A3.1 — strike-less "up/down" products (the A1 CRITICAL)
- `census.py` anchor-tape loop: `r.get("floor_strike")`; `None` → `continue` (skip, not an
  anchor). A present-but-NaN float still hard-fails (genuine corruption of a real strike).
- `census.py` `m15_by_ct`: when multiple 15M records share a close_time, prefer the
  strike-bearing one (so a slot is only `EXCL_NO_ANCHOR` when NO strike-bearing 15M market
  occupies it). In the observed corpus each close_time has a single 15M record; the
  preference is defensive.
- `census.py` pairing path: `a_val = m.get("floor_strike")`; `None` → `EXCL_NO_ANCHOR`
  with receipt (was a bare `m["floor_strike"]` KeyError at old line 229/278).
- **Receipt (real data, the 3 repro days):** `EXCL_NO_ANCHOR` fires at exactly
  `2026-06-15 23:00` and `2026-06-23 04:00` — the two top-of-hour instances reviewer A
  enumerated. The three non-top-of-hour instances (06-23 03:45, 07-22 18:45, 07-22 18:30)
  are correctly skipped in the tape and never occupy a grid slot.

### A3.2 — σ̂ window RATIFIED (no code change)
`sigma_hat` already reads the 9 anchors at T, T−15m, …, T−120m INCLUDING A(T). The
commission now pins this; I updated the module docstring to record the ratification and
left the code and `test_sigma.py` unchanged (they already assert the 9-contiguous-anchor
window).

### A3.3 — WORST never gates inclusion
- `census.py`: the NO-PAIR test dropped the `cw is None` condition — inclusion is decided
  on BASE quoting + `cb is not None` only. When `cw is None` the row stays OK with
  `C_worst`/`payoff_worst` blank.
- `census.py` receipt: new `ok_rows_worst_blank` count (A3.3 receipt flag).
- **Downstream consequence (confessed, see §2 C-A):** `gate_fit.py` and `unseal_runner.py`
  now treat WORST columns as `Optional[float]` so a blank WORST on an OK row cannot crash
  the frozen run. BASE (the inferential column) is untouched.

### A3.4 — split EXCL_SIGMA_TAPE into HEAD / GAP
- `census.py`: on `InsufficientTape`, classify by whether the oldest required anchor
  (`T − 8·900 = T−7200`) precedes the corpus's earliest loaded anchor (`min_anchor_ep`):
  before-corpus → `EXCL_SIGMA_HEAD`, otherwise interior hole → `EXCL_SIGMA_GAP`.
- **Receipt (contiguous 5-day smoke 07-14..18):** the prior single `EXCL_SIGMA_TAPE=3`
  now reads `EXCL_SIGMA_HEAD=1` (07-14 01:00) + `EXCL_SIGMA_GAP=2` (07-16 10:00/11:00, the
  06:45→09:15 gap). For the frozen contiguous run this makes HEAD exactly the 2026-06-11
  opening hours, as A3.4 states.

### A3.5 — expiration_value robustness
- `census.py`: EMPTY EV on either leg (`None`/blank) → `EXCL_EV_MISSING` with receipt
  (fail-closed, no crash). Present-but-unequal print compared as **Decimal** → hard fail
  (so `64512.33` == `64512.3300`). The `result` recompute runs only when the print is
  present. New `_ev_empty` helper.

### A3.6 — freeze mechanism (line-based, fixes B1 deadlock)
- `loader.py`: `falsifier_is_frozen(text)` — True iff some line strips to exactly
  `STATUS: FROZEN` (parsed as a line, not a whole-file substring). `unseal_runner.preflight`
  now uses it. Prose that merely mentions FROZEN/NOT FROZEN can no longer flip the state.
- `falsifier.md`: the STATUS marker is now a clean structured line `STATUS: DRAFT — NOT
  FROZEN`; the self-referential "refuses to run while this line reads NOT FROZEN" sentence
  (the B1 cause) is replaced with an accurate description of the line-based mechanism. **The
  file remains a DRAFT** (see §2 C-B for why editing it was in scope).
- New test freezes a COPY of the real repo file and asserts preflight passes (A3.6-mandated).

### A3.7 — seal defense-in-depth
- `loader.py`: `_guard_seal` now, when `acknowledge_sealed_read=True`, asserts the falsifier
  is FROZEN (`_assert_falsifier_frozen`, same line-based check) BEFORE opening any sealed
  file. `FALSIFIER_MD` is a module global (overridable) so tests can point at a frozen copy.
- Now BOTH the loader and `unseal_runner.preflight` enforce the freeze — a direct
  `build_census(SEALED_DATES, acknowledge_sealed_read=True)` call (the A4 concern) refuses.

### A3.8 — degenerate quintiles
- `gate_fit.py`: `DegenerateQuintiles` exception; `fit_gate` hard-fails with a degeneracy
  receipt (edges, bucket_ns, tied/empty indices, n) if edges are not strictly increasing or
  any bucket is empty. No silent play-everything gate.

### A3.9 — artifact binding
- `unseal_runner.preflight`: reads `gate.json["census_csv_sha256"]`, hashes
  `census_train.csv`, and refuses on absence or mismatch.

### A3.10 — aggregates only
- `unseal_runner.run`: `census_receipt_exclusions` (per-hour `"<date> <T>"` lists) →
  `census_receipt_exclusion_counts` (per-reason COUNTS). `sealed_days_with_rows`
  (date list) → `n_sealed_days_with_rows` (count). `days_negative_EV_base` (date list)
  removed; only the count `n_days_negative_EV_base` (C3) remains. No per-hour sealed rows,
  no sealed timestamps or date lists in the result artifact.

### A3.11 — dilution surfaced honestly
- `gate_fit.py`: `fit_gate` records `gate_top_bucket`,
  `gate_top_bucket_individual_lb_base`, `gate_top_bucket_lb_le_zero`; `render_report`
  prints a **DILUTION ADMISSION (A3.11)** blockquote when the top gated bucket's individual
  day-clustered BASE LB ≤ 0 while the pooled prefix clears. The gate rule is unchanged.

---

## 2. CONFESSIONS (judgment calls with no explicit letter-for-letter pin)

- **C-A — A3.3's downstream robustness in gate_fit/unseal.** A3.3 pins the census behavior
  (row stays OK, WORST blank, receipt count) but is silent on the consumers. Left literal,
  a blank WORST on an OK row would crash `gate_fit.read_ok_rows`/`bucket_stats`/bootstrap
  (and `unseal_runner`) on `float("")` — reintroducing an A1-class *non-starting run*, the
  exact defect this pass exists to remove. I therefore made WORST columns `Optional[float]`
  end-to-end: BASE is never affected; WORST stats/CI simply exclude blank rows and report an
  `n_worst` count. On real data (smoke + all verification windows) `ok_rows_worst_blank=0`,
  so this path is currently dormant; it is fail-closed insurance, not a behavior change to
  any observed number.
- **C-B — I edited `sim/ceremony/falsifier.md` (STATUS line + B1 sentence).** The build
  report declined to touch it ("ceremony forbids"). A3.6 is a commission amendment that
  *mandates* a freeze keyed on a dedicated `STATUS:` line and a test that freezes "a COPY of
  the real repo file" — both of which presuppose the real file carries a clean, freezable
  STATUS line. I changed only the STATUS marker to `STATUS: DRAFT — NOT FROZEN` and rewrote
  the one self-referential sentence that caused B1. **I did not freeze it** and did not touch
  any clause. `test_real_repo_falsifier_is_still_a_draft` still passes (and now asserts
  non-frozen via the line-based check, not just the substring).
- **C-C — A3.4 HEAD/GAP rule = "oldest required anchor precedes the corpus's earliest
  anchor," not `day == 2026-06-11`.** A3.4's parenthetical says HEAD is "2026-06-11 head
  only." I implemented the *semantic* (head-of-corpus vs interior gap) via `min_anchor_ep`
  rather than hardcoding the date. For the frozen contiguous run the two coincide exactly
  (HEAD ⊆ 06-11 opening hours). The semantic rule is more robust: an interior gap on the
  head day is correctly GAP, and a non-contiguous partial load classifies its own earliest
  opening as HEAD. Verified on non-contiguous days (06-15/23/07-22): HEAD = only the single
  earliest opening (06-15 01:00); everything else GAP.
- **C-D — A3.10 reduced the day-level date lists, not only the exclusion inventory.** B8/
  A3.10 explicitly names the exclusion inventory, but A3.10's clause says "no per-hour
  sealed rows or timestamps in any artifact." Sealed date lists are timestamps, so I also
  reduced `sealed_days_with_rows` and `days_negative_EV_base` to counts. The falsifier's C3
  needs only the count (K of 17); no clause needs the specific sealed dates.
- **C-E — I added machine-readable A3.11 fields to `gate.json`** (`gate_top_bucket*`).
  A3.11 mandates the flag in `gate_report.md`; I also surfaced the booleans/LB in the JSON
  so the bridge can key on them and the render is testable. This is additive, aggregate,
  and carries no per-hour data.
- **C-F — `EXCL_NO_ANCHOR` / `EXCL_EV_MISSING` receipts use the same `"<date> <T>"` itemized
  form as the existing train-side exclusion inventory.** This is the TRAIN census receipt
  (not a sealed artifact); A3.10's aggregates-only rule governs the *sealed* `unseal_result`,
  which is reduced to counts. Train-side itemization is unchanged from the build.
- **C-G — 1H (hourly) leg is left strike-required (bare `float(r["floor_strike"])`).** A3.1
  scopes the strike-less product to the 15M series. Read-only scan of all TRAIN 1H markets:
  **0 of 228,886** lack `floor_strike`. All TRAIN strike-bearing markets (15M and 1H) also
  carry `result` and `expiration_value` keys (0 missing), so no other bare-subscript
  KeyError lurks in the frozen run. I made no out-of-scope 1H change.

## 3. Files touched

| file | change |
|---|---|
| `sim/census.py` | A3.1 tape+pairing, A3.4 HEAD/GAP split, A3.5 EV robustness, A3.3 inclusion+receipt, `_ev_empty` |
| `sim/gate_fit.py` | A3.3 Optional WORST, A3.8 `DegenerateQuintiles`, A3.11 top-bucket LB + report flag |
| `sim/loader.py` | A3.6 `falsifier_is_frozen`, A3.7 `_assert_falsifier_frozen` in `_guard_seal`, `FALSIFIER_MD` global |
| `sim/unseal_runner.py` | A3.6 line-based freeze, A3.9 sha binding, A3.10 aggregates-only, A3.3 Optional WORST |
| `sim/ceremony/falsifier.md` | A3.6 structured STATUS line; B1 self-reference rewritten (still DRAFT) |
| `sim/tests/conftest.py` | corpus `anchor_strikeless` / `ev_h` / `ev_l` / `worst_missing` options |
| `sim/tests/test_census_integration.py` | A3.1, A3.3, A3.5 (empty / unequal / decimal-equal) |
| `sim/tests/test_gate_fit.py` | A3.8, A3.3 blank-worst, A3.11 (flag present / absent) |
| `sim/tests/test_loader.py` | A3.7 (frozen allows / not-frozen refuses under acknowledge) |
| `sim/tests/test_unseal_refusal.py` | A3.6 line-based + copy-freeze, A3.9 sha mismatch, sha-embedded staging |

**NOT written (the bridge's frozen run owns them):** `sim/out/census_train.csv`,
`census_receipt.json`, `gate.json`, `gate_report.md`. Regenerated smoke artifacts:
`census_smoke.{csv,json}`, `gate_smoke.{json,md}` (execution proofs only).

## 4. Verification receipts

- **Tests:** `55 passed` (42 prior + 13 new A3 tests). No prior test removed; freeze/seal
  tests updated to the A3.6/A3.7/A3.9 mechanisms.
- **A1 gone:** `python census.py --dates 2026-06-15 2026-06-23 2026-07-22` → 72 rows, no
  crash; `EXCL_NO_ANCHOR=2` (06-15 23:00, 06-23 04:00), `EXCL_SIGMA_HEAD=1`,
  `EXCL_SIGMA_GAP=7` (non-contiguous days → many interior gaps, correct).
- **Engine unchanged (reviewer A's window):** `--dates 2026-06-25 2026-06-26 2026-06-27` →
  `OK=65, EXCL_NO_1H_LEG=1, EXCL_NO_15M_LEG=3`, split `EXCL_SIGMA_HEAD=1 + EXCL_SIGMA_GAP=2`
  — identical to reviewer A's independent replication (which had `EXCL_SIGMA_TAPE=3`).
- **Smoke reconciles:** `--dates 2026-07-14..18` → 120 rows, `OK=111`, HEAD=1/GAP=2,
  `EXCL_NO_15M_LEG=3`, `EXCL_NO_1H_LEG=1`, `NO_PAIR=2`, `ok_rows_worst_blank=0` — matches
  the build report's counts with the sigma code split applied. `gate_fit` runs clean; gate
  empty on 5 days (expected). A3.8 did not false-trigger on real continuous σ̂.
- **Seal:** no file dated 2026-08-02..18 was opened by any means; the corpus stops at
  2026-08-01. The full 52-day census was NOT run (that is the bridge's one frozen run;
  `census.main` still refuses >7 days without `--authorize-full-run`).

## 5. Handoff to bridge verification

The fixes are specification/mechanism tightenings; no economic constant was retuned. Open
items for the bridge's read: (a) the frozen 52-day run will exercise the real 06-11 HEAD and
the 07-16 GAP under the split codes; (b) at freeze time, edit `falsifier.md`'s STATUS line to
read exactly `STATUS: FROZEN` (the loader and preflight both key on that line); (c) `gate.json`
now carries `census_csv_sha256`, which the unseal preflight binds (A3.9).
