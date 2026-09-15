# Review — PR #52 armed-evaluation-days clarification + T-4 expiry wording

Reviewer: Claude Fable 5.1 (Opus 4.8 reviewer). Review worktree
`C:\Users\Brads\Python_stuff\dv3_wt_review` (NOT the live tree). Target branch
`falsifier/armed-days-clarification` @ origin, diffed against `origin/main` (3980318).

## Verdict: APPROVE WITH NITS

The measurement clarification is implemented correctly and self-consistently. No [pin] threshold,
no STATUS line, and nothing else in the frozen body is touched. The one item that needs Brad's eyes
is the **/23-vs-/24 divisor** (his spoken proposal said /23; the build directive and this build say
/24) — flagged below, not decided here.

---

## 1. Registered-specs integrity — PASS

- `STATUS: FROZEN` on line 3 is intact and unchanged.
- No `[pin]` threshold text changed. The `>= 2.0 sets/day`, `n >= 30`, mean-lock, %positive, exec-gap
  and one-legged pins are all untouched. The gate comparison in code
  (`< V32_FALSIFIER_MIN_FILL_RATE_PER_DAY`) is unchanged.
- The pins test diff (`test_v32_falsifier_pins.py`) is a **pure addition**: it inserts one new function
  `test_registration_carries_2026_09_15_clarification` and removes/edits no existing assertion. All
  existing pin assertions run unchanged and pass.
- The new Registration entry quotes Brad verbatim (`"Yea, I agree. Lets do option 2."`) and is dated
  `2026-09-15 ~13:30Z`. It records BOTH edits (the /24 measurement definition and the T-4 wording fix)
  and states explicitly that no threshold/pin/STATUS line is touched.
- The in-place MUST CONFIRM wording edit is confined to item 1 (the T-5/T-4 GTC-expiry item) and is
  recorded in the Registration line. No other MUST CONFIRM item, no other section of the frozen body,
  is modified. Diff to `v32_falsifier.md` is exactly two hunks: MUST CONFIRM item 1 wording, and the
  appended Registration entry.

## 2. Report correctness (`build_falsifier_scoreboard`) — PASS, with the /24 finding

Verified against `pilot/service/v32/ledger.py` and `pilot/service/run_v32.py`:

- **(a) One row per armed window.** Each window writes exactly one MAIN row carrying `effective_mode`
  — either via `_stand_down` (`run_v32.py` ~1472) or via the finalize path (`build_v32_ledger_row`,
  `run_v32.py` ~1395/1730). No window writes two main rows.
- **(b) Backfill rows excluded.** `build_v32_backfill_row` (ledger.py ~284) sets `"armed": True` but
  carries **no** `effective_mode` key and no fill markers. `armed_windows` filters on
  `effective_mode == "armed"`, so a backfill row is correctly excluded from the denominator (it would
  otherwise double-count the window it backfills). It is also excluded from `fills_total`
  (`realized_unsettled=False`, no `realized_lock`, no `one_legged`). This is precisely why the new code
  keys the denominator on `effective_mode == "armed"` rather than the `armed` truthy flag — a correct
  and load-bearing design choice.
- **(c) Dry / degraded / shadow rows excluded.** A window that resolves armed but degrades at the S5
  gate is written with `effective_mode = outcome.effective_mode` (= `"dry"`), `armed=False`
  (`run_v32.py` 1651/1660), so it does not count toward `armed_windows` OR `fills_total`. Numerator and
  denominator are therefore drawn from the SAME population (truly-live armed windows); `fills_total`'s
  rows (main armed rows with a fill) are a strict subset of `armed_windows`'s rows — no rate inflation.
- **(d) Stand-down-BEFORE-quoting windows DO write an armed row — so /24 is the consistent divisor.**
  The `$250/$500` hour (`bucket width != params.bucket_width`, run_v32.py ~1573) and the
  `no KXBTC range buckets` / `no strike ladder` stand-downs (~1562/1568) all occur BEFORE the S5
  degrade check, where `effective_mode = resolved_mode` still holds. `_stand_down` is called with
  `effective_mode=effective_mode`, so on an armed-mode hour these rows carry `effective_mode == "armed"`
  (with `armed=False`, since `_stand_down` does not pass `armed=`). **Therefore a full running UTC day
  writes 24 rows with `effective_mode == "armed"`, and `armed_windows / 24` is the internally
  consistent fraction-of-a-day.** Brad's spoken `/23` would be correct only if the always-stand-down
  21Z hour wrote no armed row — but it does write one. `/24` matches the code as built. (See the
  judgment nit below on the 21Z hour's structural conservatism.)
- `fill_rate = fills_total / (armed_windows/24)`, Decimal math throughout, `None` iff
  `armed_windows == 0`. Confirmed.

## 3. Tests — 851 passed, 2 skipped, 2 errors (both environmental)

`python -m pytest pilot/tests -q` in the review worktree:
**851 passed, 2 skipped, 2 errors** in ~11s. The 2 errors are
`test_quintile.py::test_quintile_reproduction_exact` and `::test_head_of_corpus_insufficient_tape_is_noquintile`
— both `FileNotFoundError` for `historical-data/15-minute/markets/2026-06-11.jsonl`, which is not
present in this review worktree (it lives in the live tree). The diff touches none of the loader/quintile
code, so these are pure data-availability errors, not regressions. The builder's tree
(`dv3_wt_v11`, which has the data) reported 855 passed; consistent.

The scoreboard + pins tests (`test_v32_report_scoreboard.py`, `test_v32_falsifier_pins.py`) pass 18/18
in isolation. The new `test_scoreboard_armed_days_is_windows_over_24` fake ledger (7 armed windows over
two UTC dates, 1 completed set) correctly yields `armed_windows=7`, `armed_days=7/24`, and
`fill_rate = 1/(7/24) = 24/7 = 3.43/day`, with `n_days=2` retained and verdict still `n<30 pending`.

## 4. Build-report honesty — PASS

The build report is honest and complete. It transparently flags the /23-vs-/24 wording difference in
its own section ("/23 vs /24 (resolved to /24)"). The before/after live-ledger numbers reproduce
exactly. Reconstructing the described live-ledger shape (9 armed windows, 1 fill, 2 calendar dates)
through the new code gives:

```
armed_windows = 9   armed_days = 0.375   n_days = 2
n = 1   fills_total = 1
fill_rate = 1 / (9/24) = 2.666.../day  ->  2.67/day
verdict = n<30 pending (n=1)
old denominator: 1 / 2 (calendar days) = 0.50/day
```

Matches the report's 0.50/day -> 2.67/day and unchanged `n<30` verdict.

## Nits (non-blocking)

1. **/23-vs-/24 is Brad's call, not the reviewer's.** The build faithfully executed the directive's
   `/24`; Brad's spoken word was `/23`. As shown in 2(d), `/24` is the divisor consistent with the
   actual ledger (a full day = 24 armed rows). The only real difference between `/23` and `/24` is
   whether the one structurally-guaranteed stand-down hour per day (the 21Z `$250/$500` hour) counts in
   the denominator. **Surfacing for Brad to confirm `/24`** — the code, doc, tests, and report are
   internally consistent either way once the divisor is fixed.
2. **Structural conservatism of counting the 21Z hour.** Because the 21Z hour always stands down (it
   can never produce a rest fill) yet counts as `1/24` of a day in the denominator, the theoretical max
   measurable fill rate over a fully-armed day is slightly below 24 sets/day. This mildly deflates the
   rate (conservative, in the strategy's disfavour). Negligible at the 2.0 gate, but it is the concrete
   thing the `/23` choice would have removed. Worth a one-line note to Brad; no code change needed.
3. **Variable reuse of `armed_days`.** In `report.py`, `armed_days` is first the accumulator `set` used
   to compute `n_days = len(armed_days)`, then immediately rebound to the `Decimal` fraction. It works
   (n_days is read before the rebind) and both comment lines are clear, but reusing the name for two
   different types in adjacent lines is a readability trap for the next editor. Consider renaming the
   set to `armed_day_set`. Optional.

No blocking issues. Recommend APPROVE WITH NITS; route the `/23` vs `/24` divisor to Brad for a
one-word confirmation.
