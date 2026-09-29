# V3.3 ladder shift 5..15c -> 8..18c (L4) -- adversarial review

Reviewer: Opus 4.8. Date: 2026-09-29. Worktree: `C:\Users\Brads\Python_stuff\dv3_wt_review`, detached at
`origin/feat/v33-ladder-8-18` (27d3a57, on top of `origin/main` f0d6c6d). Review commit adds one
end-to-end test + stale-band label corrections on branch `review/v33-ladder-8-18`.

## Verdict: APPROVE WITH NITS

The change is exactly what it claims: a single-key policy shift `E_min` 0.05 -> 0.08 (ladder 8..18c,
deep-obs 19..28c), the sha re-pinned and self-consistent everywhere, the DRAFT falsifier + ARMING amended
honestly (gates unchanged, STATUS untouched, 5..15c evidence kept and labelled as history), and the test
suite green. No mode/ALLOW_ORDERS/task/holdout/seal/proxy touched. The nits are documentation/receipts
drift the builder's params change left behind in runtime source (the deep-band label read "16..25c" after
the shift moved it to "19..28c"), including one user-facing printed report header -- all fixed in the review
commit -- plus one missing end-to-end assertion, which the review commit adds.

## What I verified (receipts)

- **Sha, recomputed independently** with the ARMING recipe
  `sha256(json.dumps(o,sort_keys=True,separators=(",",":")))` =
  `295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def`. Equals `FROZEN_V33_PARAMS_SHA256`
  (params.py), `load_v33_params().sha256`, the falsifier doc (provenance + policy + freeze + registration),
  and ARMING (S5 + recipe expected output). The old sha `2e6098...` is kept as
  `PREVIOUS_V33_PARAMS_SHA256_PRINT_THROUGH`.
- **JSON diff is exactly one key**: `"E_min": "0.05" -> "0.08"`, every other byte identical (the sha proves
  it).
- **Loader runtime** (in-worktree): `E_min` 0.08, `rungs` 11, `lots_per_rung` 1, `max_sets_per_hour` 11 (=
  rungs invariant holds), `deep_obs_rungs` 10, `shadow_Es` (0.08, 0.10, 0.12) all inside the new live range
  [0.08, 0.18], `n_min` 0.05 (a NO-price placeability floor, independent of E_min -- confirmed), sha match
  True. No exception.
- **Deep-obs band computed, not hard-coded**: `DeepObservationLadder(load_v33_params(), ...).margins` =
  [19..28], derived from `e_min_c + rungs .. + deep_obs_rungs - 1`. Logic is correct at the shipped params.
- **Old-sha / stale-policy grep** (repo, mailbox excluded): every `2e6098...` hit is a correctly-labelled
  history/constant reference. All `5..15c` / `E_min 0.05` hits are either (a) the builder's pinned mechanism
  tests (with a WHY comment), (b) the falsifier's explicitly-labelled 5..15c history, or (c) historical
  study/build-report files. The `16..25c` hits in runtime source were the real drift (see nits).
- **Doc<->code sha agreement enforced**: `test_v33_falsifier_pins.py` asserts `FROZEN_V33_PARAMS_SHA256 in
  doc` AND `== load_v33_params().sha256` -- now against the NEW sha. `test_v33_hardening` asserts PIN = new
  sha, PREVIOUS_..._PRINT_THROUGH = old sha, PIN distinct from all previous, and `E_min == 0.08`.
- **Ceremony honesty**: STATUS line still `DRAFT -- NOT FROZEN` (untouched). Gate pins (mean +6.0c, n>=30,
  kill +2.0c at n>=15, %positive 80, capture 0.50, one-legged 2, roll ratio 0.90) all unchanged. L4
  amendment states the 5..15c dry sample (22 windows / 161 fills) and the 5..15c ideal MC study are kept as
  history and NOT mixed into the 8..18c gate counting. The +16.03c / 1806c figures are labelled pre-shift
  history, not re-labelled -- honest receipts.
- **Full suite**: `python -m pytest -q` -> 1336 passed, 5 skipped (the +1 vs the builder's 1335 is my new
  test). The 5 skips are environment-only in this worktree (4 = `historical-data` corpus absent for
  box/quintile; 1 = POSIX-only SIGTERM); none touch V3.3. The task's "1339 pass / 1 skip" was measured in the
  live tree where the corpus is present; the total (1340/1341) reconciles.

## The load-bearing decision (E_min-pinned mechanism/parity tests) -- ACCEPTED

The builder pinned `E_min=0.05` inside the controlled-ladder helpers (`test_v33_core._params`,
`test_v33_golden._sweep_params`, `test_v33_print_through._params` + one direct construction,
`test_v33_run._dry_driver`) rather than rewrite ~55 absolute-price assertions. I checked each concern:

- **(a) Does a pin hide a real divergence via the sha/policy file?** No. Each pin is
  `replace(load_v33_params(), E_min=Decimal("0.05"), ...)`. The loader's sha self-check already ran against
  the REAL 0.08 file at load; `replace` only overrides E_min on the already-validated object for the
  mechanism replay. None of these helpers re-assert the sha, so the pin masks nothing about policy identity
  (identity is asserted separately in params/hardening/falsifier_pins).
- **(b) Is the SHIPPED policy exercised end-to-end at 8..18c?** Partially, before this review: the shadow
  golden test `test_driver_deep_obs_reached_and_absorption_on_golden_sweep` runs the REAL driver at E_min
  0.08 but asserts only the DEEP-observation band (19..28c), not the LIVE rested rung prices.
  `test_l4_ladder_shift_8_to_18` asserts params values only. **No test asserted the live rested prices at
  the shipped E_min are 3c below the 5..15c ladder.** I added one (see review commit): loads the REAL policy
  (asserting its frozen sha), brings the ladder up through the real core on the same book the goldens use,
  and asserts n_top 0.47 = 0.50 - 3c and rungs 0.47..0.37 (each exactly 3c below the golden 0.50..0.40),
  count 11, margins 8..18. Verified independently: shipped n_top 0.47, ladder 0.47..0.37.
- **(c) Print-through E_min-dependent constant masked by the pin?** No. `print_through_ticks`/`slack_c` are
  cent offsets RELATIVE to a rung's offer; the offer moves with E_min but the offsets do not, so the
  mechanism is E_min-invariant and the pin masks no shipped constant.
- **(d) Doc<->code sha for the NEW sha asserted?** Yes -- `test_v33_falsifier_pins.py:80-81` and
  `test_v33_hardening`, both against the new sha.

The pin choice is defensible: the goldens/dry-run parity checks are against the 2026-09-20 5..15c ideal
study (`mc/v33_ladder_ideal.json`), which has no 8..18c counterpart, so shifting them would leave them
checking nothing. With the added end-to-end test, the shipped ladder is now covered at the real params too.

## Findings (numbered)

1. **[NIT -> FIXED] Missing end-to-end coverage of the shipped ladder** (`tests/test_v33_core.py`).
   Nothing exercised the REAL policy (E_min 0.08) through the core for its RESTED prices. Added
   `test_shipped_policy_rests_ladder_3c_below_the_5_15c_golden_end_to_end`, which loads the real policy
   (asserts its frozen sha) and proves the live ladder rests 3c lower end-to-end (0.47..0.37, margins 8..18).

2. **[NIT -> FIXED] User-facing printed report header stale** (`service/v33/report.py:662`). The DEEP END
   block header was the hard-coded literal `"... -- 16..25c; ..."` while the rows it heads now print margins
   19..28c -- a receipts inconsistency in the dry report. Fixed to DERIVE the band from the observed margins
   (`{margins[0]}..{margins[-1]}c`, fallback "below the live ladder" when empty) so it never goes stale on a
   future shift. `test_v33_report_scoreboard` only substring-matches "DEEP END (SO-3" so it stays green.

3. **[NIT -> FIXED] Doc/code drift in the pins module** (`service/v33/falsifier_pins.py:49`). The promotion
   comment still said "rungs deeper than 15c live" while the amended ceremony doc says "deeper than 18c" AND
   the live ladder now already rests at 16/17/18c (so "deeper than 15c" was self-contradictory). Fixed to
   "18c". This is a comment, not an enforced [pin] constant, so the agreement test did not catch it.

4. **[NIT -> FIXED] Stale deep-band / live-band labels in runtime source comments** (all describing the
   CURRENT default, now wrong): `shadow.py` (live margins "5..15c" -> 8..18c; "16..25c" -> 19..28c; "15 (the
   deepest LIVE rung)" -> 18), `run_v33.py` (x3 "16..25c"), `report.py` (x2 docstrings), `ledger.py`,
   `params.py` (x2). All logic is derived-from-E_min and correct; these were misleading labels only. Updated
   to the shipped values. The two remaining "16..25c" strings are explicitly labelled history ("was 16..25c"
   / the review note in report.py) -- left as honest history.

5. **[NIT -> FIXED] Design-spec current-default drift** (`PLAN_V33.md:26`, `:224`). Line 26 stated the
   CURRENT "Default ... E_min = 5 -> rungs at 5..15c"; line 224 the promotion lever "rungs deeper than 15c
   live" -- both now wrong. Fixed to E_min 8 / 8..18c and 18c (with a dated note). The other PLAN_V33 5..15c
   references (lines 61, 75, 149) are the ideal-study table and the original design assumption -- historical,
   left intact.

6. **[OBSERVATION -- no change] `PREVIOUS_..._PRINT_THROUGH` constant name.** The task suggested
   `..._FLAP_R3`; the builder named it for the regime that froze that sha (the 2026-09-26 print-through
   re-pin), which is the file's convention and is historically accurate (FLAP_R2 already holds the last flap
   sha). I agree with the builder's choice; no change.

7. **[OBSERVATION -- pre-existing, no change] Chained `!=` distinctness** (`test_v33_hardening.py`). `assert
   PIN != PREV_PRINT_THROUGH != ... != PREV_L2` does not check every pair, but the builder ADDED `assert PIN
   not in (all previous)`, which fully covers PIN's uniqueness (the one that matters here). The remaining
   constants' mutual distinctness is a pre-existing gap, not introduced by this change.

## House-law compliance

No `.env`/`*.pem` read; no `sim/out/sealed_eval/**`; no network / proxy / Kalshi API; no holdout/seal date
read by any changed code path; no mode file, `ALLOW_ORDERS`, task, or sha pin flipped; nothing pushed to
`main`. V3.3 STAYS DRY -- this is a params-only re-pin.

## Review commit

Branch `review/v33-ladder-8-18` off HEAD (27d3a57): the new end-to-end test + the finding-2..5 label
corrections + this document. The feature branch itself is not modified.
