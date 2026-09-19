# Review — PR #66 capture-ratio gate (`feat/capture-ratio-gate` -> `main`)

Reviewer: Opus 4.8 (Fable's delegated review agent). Adversarial review at Brad's request. Do NOT merge;
Brad merges. No live tree touched, no proxy dialed, no `.env`/`*.pem`/sealed read, no Kalshi API calls.
Review branch `review/pr66-notes` off `origin/main` `5f22880`. PR head `73ad5fc`. Fast-forward mergeable.

## VERDICT: APPROVE WITH NITS

The change is sound and well-tested: the falsifier's fill-rate gate is replaced (at n >= 30) by a
capture ratio (live completed sets / ideal-shadow fills), the falsifier doc is append-only and touches no
pin/threshold/STATUS/sha, the pins test is add-only, the verdict None-handling is correct, and the report
diff is exactly the intended additive change. The current value is `7/13 = 53.8%` on a fresh ledger copy
(the builder's `7/12 = 58.3%` was a 124-row snapshot; the ledger has since grown one shadow-available
window), still >= the proposed 0.50 pin; the verdict stays `n<30 pending`.

One substantive nit (N1): the capture-ratio **numerator counts set EVENTS, the denominator counts
WINDOWS** — a mismatch that is harmless at `contracts` = 1 (verified: identical to a per-window count on
the live ledger today) but permits a ratio > 1 and **inflates ~2x at `contracts` = 2** (a window with two
completed sets counts 2 against a 1-per-window denominator). It must be fixed before `contracts` is
raised; it does not block this merge. Plus one process note: the 0.50 pin is Claude's proposal and the
code pins it now — Brad confirms the number at merge (clearly disclosed).

---

## N1 (nit — fix before `contracts` = 2; NOT blocking today)

**File:** `pilot/service/v32/report.py` — `build_falsifier_scoreboard`, the numerator increment
(`if capture_window: capture_live_sets += 1`, inside the `for ev in events:` completed-set loop, ~report.py:282)
vs the denominator (`if capture_window and slock is not None: capture_shadow_fills += 1`, once per row,
~report.py:270).

**The mismatch.** The denominator counts each armed+bucket window **at most once** (the shadow fills once
per window by the T-15..T-5 window gate). The numerator increments **per completed set EVENT**. These
align only while a window can hold at most one set:

- **`contracts` = 2 inflation (material, latent).** #62/#59 make partial fills the common case, so a
  window can complete **two** rest-fill sets. That window adds **2** to the numerator but **1** to the
  denominator, so the ratio inflates toward ~2x and can exceed 1 — the gate becomes too easy exactly when
  execution (double hedging, amend crosses) is hardest. A KILL-worthy window could pass.
- **ratio > 1 permitted even at `contracts` = 1.** A window where the live path filled but the ideal
  shadow did **not** record a fill (e.g. the live maker rest was hit by a print the shadow's
  window/fresh-cap gate did not score) adds to the numerator but not the denominator, pushing the total
  numerator above the paired denominator. A "capture ratio" > 1 is conceptually wrong (you cannot capture
  more pumps than the shadow proved available) and only ever eases the gate.

**Why not blocking now.** Measured on a fresh READ-ONLY copy of the live ledger (125 rows) with the PR's
own `_row_set_events`:
- armed+bucket windows the shadow filled (denominator): **13**
- numerator under the builder's **set-events** count: **7**
- numerator under a **windows-with-a-set** count: **7**
- numerator under a **windows-with-a-set-AND-shadow** (bounded per-window capture) count: **7**
- armed+bucket windows with a live set but NO shadow fill (would push ratio > 1): **0**
- windows with > 1 completed set (the `contracts` = 2 double-count): **0**

So all three candidate definitions give the same **7/13 = 53.8%** today, and the reported value is
correct. The divergence is purely latent (`contracts` = 1, no multi-set or live-only windows exist yet),
and the gate does not decide until n >= 30 (currently n = 7).

**Recommended fix (small).** Count the numerator as **windows with >= 1 completed live set that also had
a shadow fill** — a per-window 0/1 intersection with the denominator, i.e. a true capture fraction bounded
to [0, 1]. Concretely, in the row loop track a per-row boolean `row_has_set` (any `ev["lock"] is not None`)
and do `if capture_window and slock is not None: capture_shadow_fills += 1; capture_live_sets += 1 if
row_has_set`. This leaves the current value unchanged (7/13) and future-proofs `contracts` = 2. (If Brad
instead wants to credit live-only windows, the ratio should still be capped at 1 and the denominator
widened to `shadow-fill OR live-set` windows — but the strict intersection is the honest "capture"
reading and matches Registration 3's wording "how much of the pump availability the shadow proves was
there did we actually capture.")

**A failing scenario for N1 (contracts = 2), to add to `test_v32_report_scoreboard.py`:**
```python
def test_capture_ratio_does_not_double_count_two_sets_in_one_window():
    """A single armed+bucket window that completed TWO rest-fill sets (contracts=2 partial fills) vs one
    shadow fill must count as ONE captured window, not two -- else the ratio inflates above the true
    per-window capture and can exceed 1."""
    row = {
        "armed": True, "effective_mode": "armed",
        "spot_bucket_ticker": "KXBTC-TWOSET-B0",
        "close_time": "2026-09-20T16:00:00Z",
        "shadow": {"0.10": {"filled": True, "lock": "0.10"}},
        "wing_batch_sets": [  # two completed sets in one window (per PR #62 schema)
            {"realized_lock": "0.09", "one_legged": False},
            {"realized_lock": "0.09", "one_legged": False},
        ],
        "realized_unsettled": True,
    }
    sb = build_falsifier_scoreboard([row])
    assert sb["capture_shadow_fills"] == 1
    assert sb["capture_live_sets"] == 1      # FAILS on 73ad5fc: builder counts 2
    assert sb["capture_ratio"] <= Decimal(1)  # FAILS on 73ad5fc: 2/1 = 2
```
(Note `n` still counts both sets for the other gates — only the capture-ratio numerator should be
per-window. The fix must keep `n += 1` per set while incrementing `capture_live_sets` at most once/window.)

---

## What I verified clean

**Item 1 — falsifier integrity.** `git diff --numstat` on `pilot/ceremony/v32_falsifier.md` = **8 added,
0 deleted** — purely an appended `MEASUREMENT CLARIFICATION 3` Registration entry. `STATUS: FROZEN`
(line 3) unchanged; the params sha `0ac697957c...` unchanged and still present; every existing `[pin]` and
all threshold text above the Registration section untouched (zero deletions). The
"Proposed pre-registered thresholds" fill-rate bullet is left in place, with the supersession pointer only
in the Registration entry — which matches the doc's own freeze rule ("From the freeze line down, nothing
may change except appended verdicts in Registration") and the precedent of MEASUREMENT CLARIFICATION 1
(which likewise left the fill-rate bullet and registered the change). The Registration entry, the
`falsifier_pins.py` comment, and the build report all state plainly that 0.50 is **Claude's proposal, the
number to be confirmed by Brad at merge**.

**Item 2 — add-only tests + the rename.** `test_v32_falsifier_pins.py` diff is purely additive: 3 new
tests appended (`test_capture_ratio_pin_value`, `test_registration_carries_2026_09_19_capture_ratio_clarification`,
`test_verdict_uses_capture_ratio_not_fill_rate`); **no existing assertion edited or deleted**. The renamed
test in `test_v32_report_scoreboard.py` (`test_scoreboard_kill_on_low_fill_rate` ->
`test_scoreboard_low_fill_rate_no_longer_kills`) is **meaningful, not neutered**: it keeps the same lean
fixture (30 sets over 400 armed windows = 1.8 sets/day, below the old 2.0 pin) and now asserts that
because the 370 no-fill windows had no shadow availability the capture ratio is 30/30 = 100% and the
window is **ALIVE-so-far** with `"fill rate" not in verdict` — i.e. it positively verifies the
supersession rather than deleting the coverage. The new capture tests (KILL on low ratio, counting +
exclusions, None, legacy pre-#62 rows, render) are strong.

**Item 4 — verdict logic.** The `n < MIN_N -> pending` and `ALIVE-so-far / KILL` scaffolding is intact
(report.py:327-345); only the fill-rate fail line was replaced by
`if capture_ratio is None or capture_ratio < V32_CAPTURE_RATIO_MIN: fails.append("capture ratio ...")`.
`capture_ratio is None` (no shadow-fill armed+bucket window) is a **fail**, not a false ALIVE (verified by
`test_capture_ratio_none_when_no_shadow_availability`: None -> KILL, no crash). The mean-lock, %positive,
exec-gap and one-legged gates are unchanged; no early-kill path is in `build_falsifier_scoreboard` and none
was disturbed.

**Item 5 — report diff (byte-identity NOT expected).** On a fresh read-only ledger copy (125 rows), PR
head vs `origin/main`:
- TEXT: exactly the fill-rate label change (`... (info, superseded as a gate by Registration 3; pin was
  2.0/day)`) + one added line `capture ratio = live 7 / shadow 13 = 53.8%  (>= 50% [pin] Registration 3)`.
- JSON structured diff: exactly **3 added keys** (`falsifier.capture_live_sets`=7,
  `falsifier.capture_shadow_fills`=13, `falsifier.capture_ratio`=0.5384…). Nothing else changed; verdict
  unchanged (`n<30 pending (n=7)`). Matches the design intent (only the capture line, the fill-rate label,
  and the additive JSON keys differ).

**Item 6 — suite.** Fresh worktree, excluding the 5 corpus-dependent files absent in a fresh checkout
(`test_parity`/`test_shakedown`/`test_quintile`/`test_review_probes2`/`reference_impl_review` —
`sim/out/census_train.csv` / `historical-data/15-minute/...`): **866 passed, 2 skipped, 0 failures**. The
capture-ratio + pins files alone: **32 passed**. Consistent with the builder's 926 (= 866 + the ~60 tests
in the data-file files present in the builder's tree).

---

## Other observations (non-blocking)

- **Metric sensitivity at low n (informational).** At n = 7 the ratio is fragile: the denominator grows
  with every shadow-available window whether or not live captures it, so one more missed shadow-available
  window takes 7/13 -> 7/14 = 50.0% (exactly the pin) and two -> 7/15 = 46.7% (< pin). This is fine — the
  gate does not decide until n >= 30 — but worth stating that the current 53.8% is close to the proposed
  pin and will move as the pilot runs. Not a defect.
- **Pin confirmation flow.** The code pins `V32_CAPTURE_RATIO_MIN = 0.50` now; if Brad chooses a different
  number he edits `falsifier_pins.py` + the Registration entry at/before merge (`test_capture_ratio_pin_value`
  will need the same number). The verdict does not fire until n >= 30, so no live decision rides on the
  exact number today. Disclosed correctly as a proposal.

## Cleanup
Throwaway worktree `C:\Users\Brads\Python_stuff\dv3_wt_review_pr66` created for the suite run + ledger
analysis and removed after this review.

---

# Round 2 — N1 fix + n_min gate delta re-check (new head `8dc4399`)

Verdict: **APPROVE — N1 RESOLVED.** The capture-ratio numerator is now a bounded per-window fraction
(N1 fixed and my round-1 failing test passes), and a new n_min gate correctly excludes the one
below-min shadow fill (09-19 23:00Z) that was never a live-reachable counterfactual, restoring 7/12 =
58.3%. Falsifier/pins integrity intact; report diff vs main is exactly the intended change. No remaining
blocking or must-fix items. Delta since `73ad5fc`: `report.py` (per-window numerator + n_min exclusion),
`core.py`/`actions.py`/`run_v32.py`/`ledger.py` (the SHADOW_FILL_BELOW_MIN gate + counter),
`v32_falsifier.md` (Registration 3 refined — its own unmerged entry), and tests
(`test_v32_report_scoreboard.py`, `test_v32_core.py`). `test_v32_falsifier_pins.py` untouched this round.

## 1. N1 fixed — bounded per-window fraction (verified)
`report.py` now counts, per row: `if capture_window and shadow_valid: capture_shadow_fills += 1; if
row_has_set: capture_live_sets += 1`. Numerator = armed+bucket windows with BOTH a valid shadow fill AND
>= 1 completed live set; denominator = armed+bucket windows with a valid shadow fill; both 0/1 per window,
so the ratio is bounded to [0, 1]. `n` for the economic gates still counts every set (the `n += 1` is
inside the per-event loop, independent of `capture_window`/`shadow_valid`).
- **My round-1 failing test now PASSES** (probe): a single window with `wing_batch_sets` of two completed
  sets + one shadow fill -> `capture_shadow_fills` = 1, `capture_live_sets` = 1, `capture_ratio` = 1
  (<= 1), and `n` = 2. A live-set-without-shadow window contributes 0/0 (neither), so the ratio can never
  exceed 1. The builder added equivalent coverage in `test_v32_report_scoreboard.py`.

## 2. n_min gate (verified)
- **(a) core suppression.** `core._shadow_on_trade` inserts, before the fill, `if sub.n < params.n_min:
  emit SHADOW_FILL_BELOW_MIN; continue` (strict `<`). Probe confirms the boundary: an offer 0.95
  (n = 0.05 == n_min) FILLS (not excluded); offer 0.96/0.97 (n 0.04/0.03) is suppressed. `run_v32`
  journals `shadow_fill_below_min`; `ledger` counts `shadow_fills_below_min` (additive, `.get` default 0);
  the scoreboard renders `shadow fills below n_min (suppressed, live n_below_min) = N`.
- **(b) report exclusion for existing rows.** `_shadow_below_min(sub, n_min)` derives n = `1 - offer`
  from the shadow record's stored `offer` and excludes below-min fills from the capture DENOMINATOR and
  from the shadow mean-lock / exec-gap stats. Verified the record shape carries `offer` (top-level `n` is
  `null`, as the code comment states): e.g. the 23:00Z record is
  `{"filled": true, "offer": "0.96", "print": "0.9700", "lock": "0.1072", "n": null, "count": "16.00"}`.
  No rounding trap: `_dec` uses `Decimal(str(v))` (exact), and Decimal compares by value
  (`0.05` == `0.0500`); the boundary is `<`, so n == n_min fills.
- **Does NOT drop a live set.** Probe: a row with a live completed set AND a below-min shadow keeps the
  live set in `n` (=1) and the live-lock stats, and only excludes the shadow side (`capture_shadow_fills`
  0, `shadow_mean_lock_c` None for that fill, `shadow_fills_below_min` 1). The live economics are never
  touched by the shadow n_min exclusion.
- **The 23:00Z 09-19 row is the one excluded** (verified on a fresh read-only 125-row ledger copy):
  bucket B81250, shadow filled at offer 0.96 (n = 0.04 < 0.05) on a 16-lot 0.97 print, NO live set. It
  was in the denominator (13) and is now excluded -> 12 valid shadow windows; numerator 7 unchanged ->
  **7/12 = 58.3%** (matches the builder). New-row upstream counter and existing-row derived exclusion are
  mutually exclusive per window (a suppressed new row has no filled shadow record), so no double-count.

## 3. Falsifier + pins integrity (verified)
`v32_falsifier.md` vs `origin/main`: **9 added, 0 deleted** — the entire Registration 3 (2026-09-19)
entry is appended; the in-place refinements this round (bounded-fraction DEFINITION wording + the new
N_MIN GATE bullet + the CURRENT VALUE update) are all WITHIN this PR's own not-yet-merged Registration
entry, so relative to the frozen doc nothing above the Registration section, no prior registered entry,
no `[pin]`, the params sha `0ac697957c...` (still present), or `STATUS: FROZEN` (line 3) is changed.
`test_v32_falsifier_pins.py` vs main: **95 added, 0 deleted** (add-only), and **unchanged this round**
(byte-identical to `73ad5fc`).

## 4. Report diff vs main (verified — expected changes only)
Fresh read-only ledger copy, PR head vs `origin/main` (pre-#66):
- TEXT: the fill-rate label (`(info, superseded ...)`), one added `capture ratio = live 7 / shadow 12 =
  58.3%  (>= 50% [pin] Registration 3)` line, and one added `shadow fills below n_min (suppressed ...) =
  1` line. Nothing else.
- JSON: 4 added keys (`capture_live_sets`=7, `capture_shadow_fills`=12, `capture_ratio`=0.5833,
  `shadow_fills_below_min`=1) PLUS one **intended** CHANGE — `shadow_mean_lock_c` 10.4877 -> 10.4683
  (+10.49c -> +10.47c), the disclosed effect of excluding the below-min fill (lock +10.72c, above the
  mean) from the shadow stats (item 2b). `exec_gap_c` is UNCHANGED (-0.60c): the below-min window had no
  live set, so it never entered the gap. Verdict unchanged (`n<30 pending (n=7)`). All differences are
  expected and match the builder's round-2 summary.

## 5. Suite (fresh worktree)
Excluding the 5 corpus-dependent files absent in a fresh checkout: **875 passed, 2 skipped, 0 failures**;
capture/pins/core files alone: **104 passed**. Consistent with the builder's 935 (= 875 + the ~60 tests
in the data-file files present in the builder's tree).

## Residual (non-blocking, carried from round 1)
- The 0.50 pin remains Claude's proposal; the code pins it now and Brad confirms the number at merge
  (disclosed in the Registration entry, the pins comment, and the build report). The verdict does not
  fire until n >= 30.
- Metric sensitivity at low n is unchanged in kind but the n_min gate helps: the current 7/12 = 58.3% is
  a touch above the pin; each future shadow-available window it misses lowers it, each it captures raises
  it, and below-min windows no longer dilute the denominator.

**Round-2 verdict: APPROVE — N1 resolved, n_min gate correct, integrity intact.** Merge decision (and the
0.50 number) remain Brad's. Note: the PR is no longer a clean fast-forward only because `main` gained the
docs merges (#67, #68) since the branch's base — a trivial rebase, no code conflict; that is Brad's to do.
