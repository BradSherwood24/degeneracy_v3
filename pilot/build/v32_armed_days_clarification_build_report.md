# V3.2 build report — armed-evaluation-days measurement clarification + T-4 expiry wording

Branch: `falsifier/armed-days-clarification` (off `origin/main` @ 3980318, which includes PR #50:
`EXPIRATION_GRACE_S` 60 → venue expiry T-4, quote-end cancel at T-5 the primary path).
Worktree: `C:\Users\Brads\Python_stuff\dv3_wt_v11` (NOT the live tree).
Date: 2026-09-15.

## Why

Brad, verbatim (2026-09-15 ~13:30Z): **"Yea, I agree. Lets do option 2."**

Context he gave first: *"Should we really count this one-leg against the falsifier? It was more of a
bug than a strike against the market / strategy, right?"* — the 2026-09-15 05:30Z Windows Update reboot
cost 7 windows (06:00Z..12:00Z), and the 09-14 day ran only 3 armed windows, two of them broken by the
wire bugs fixed in PR #43/#46.

Option 2 (Claude's proposal, which Brad approved verbatim): *"Clarify before the verdict. Add a dated
Registration line defining 'armed evaluation days' as quoted windows divided by 23 [built as /24 — see
note below], so a dark hour costs a 23rd of a day rather than nothing or everything. This is a
measurement definition, not a threshold change, and it lands before n reaches 30, so it does not violate
the no-re-spec rule. I would pair it with the T-4 expiry wording fix in the same line."*

`pilot/ceremony/v32_falsifier.md` is FROZEN (STATUS: FROZEN, Brad's lever). This edit changes NO [pin]
threshold and NO STATUS line — it defines how the fill-rate gate's denominator is measured, and lands at
n=1 (well before n=30), so the registered-specs rule (no re-spec on the same evaluation window) is
honoured.

## /23 vs /24 (resolved to /24)

Brad's spoken proposal said "divided by 23"; the build directive specifies /24 consistently (an hour is
a 24th of a UTC day; a day has 24 hourly closes). I built /24 as directed. The doc, code, tests, and
this report all use /24. Flagging the wording difference for the record — the intent (a dark hour costs a
fraction of a day, not a whole day) is identical; only the divisor's exact value differs, and /24 is the
literal fraction-of-a-day.

## What changed

### 1. Falsifier doc — `pilot/ceremony/v32_falsifier.md`
- `STATUS: FROZEN` on line 3 unchanged. No [pin] threshold text changed.
- **MUST CONFIRM item 1** (was: *"the venue ACCEPTS it (auto-expires at T-5)"*): reworded in place —
  the executor's quote-end cancel at T-5 is the PRIMARY path that removes the rest; the venue's own
  `expiration_time` is set to T-4 (`EXPIRATION_GRACE_S` = 60, PR #50) as the crash backstop only; a rest
  that survives to the by-design T-4 venue auto-expiry is NOT a failure of that item.
- **New Registration line** (2026-09-15 ~13:30Z), appended after the 09-14 freeze + re-arm entries and
  before `## Pre-registered shadow observations`, recording Brad's verbatim go and both edits:
  (a) "armed evaluation days" = (armed windows the pilot actually ran, i.e. ledger rows with
  `effective_mode` = `armed`, whatever their stand-down reason) / 24 — a dark hour (reboot/proxy-down/
  task-not-started) leaves no row and costs a 24th of a day, and a calendar day with 3 windows counts as
  3/24 of a day; measurement definition, not a threshold change; registered at n=1;
  (b) the T-4 expiry wording fix.

Both edits below the freeze line are performed on Brad's dated, verbatim, explicit authorisation
(the same authority under which the freeze itself was a Claude mechanical edit), and are recorded in
Registration.

### 2. Report — `pilot/service/v32/report.py` (`build_falsifier_scoreboard`)
- New `armed_windows` = count of ledger rows with `effective_mode` == `"armed"` (all stand-down reasons;
  backfill rows carry no `effective_mode`, so they never double-count).
- New `armed_days` = `Decimal(armed_windows) / 24`.
- `fill_rate` = `fills_total / armed_days` (None only when 0 armed windows). Previously
  `fills_total / n_days` where `n_days` = distinct armed UTC calendar days.
- `n_days` key retained (still distinct calendar days) for backward compatibility; `armed_windows` and
  `armed_days` added to the scoreboard dict / JSON.
- Scoreboard line now prints `armed windows = N   armed days = N/24 = x.xx` in place of `armed days = 2`.
- The gate comparison (`< V32_FALSIFIER_MIN_FILL_RATE_PER_DAY`) is UNCHANGED.

### 3. Tests — `pilot/tests/`
- `test_v32_report_scoreboard.py`: `_set_row` now carries `effective_mode: "armed"`; new
  `_armed_no_fill_row` helper (armed window that ran but never filled). Updated
  `test_scoreboard_alive_when_all_pins_pass` and `test_scoreboard_kill_on_low_fill_rate` to the new
  denominator; updated `test_scoreboard_ignores_dry_rows`; added
  `test_scoreboard_armed_days_is_windows_over_24` (7 armed rows over two UTC dates, 1 set →
  armed_windows 7, armed_days 7/24, fill rate 24/7 = 3.43/day).
- `test_v32_falsifier_pins.py`: added `test_registration_carries_2026_09_15_clarification` (asserts the
  new "MEASUREMENT CLARIFICATION" / "Lets do option 2" / "armed_windows / 24" line, and that MUST
  CONFIRM no longer says "auto-expires at T-5"). Existing pin assertions untouched.

## Test result

`python -m pytest pilot/tests -q` from the worktree root: **855 passed** (0 failed, 0 skipped, 0
errored) in ~23s. `test_quintile` / `test_box_golden` did NOT skip or error for missing historical-data
in this worktree — all green.

## Before / after — live ledger (read-only)

Read-only against `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ledger\v32_ledger.jsonl` (15 rows, 9
with `effective_mode` armed). The report opens no socket, places no order, and writes nothing to that
path — it only reads.

| quantity | before (calendar-day denom) | after (armed_windows/24) |
|---|---|---|
| completed sets n | 1 | 1 |
| rest fills total | 1 | 1 |
| one-legged | 0 | 0 |
| denominator | n_days = 2 (calendar days) | armed_windows = 9 → armed_days = 9/24 = 0.375 |
| **fill rate** | **1 / 2 = 0.50 / day** | **1 / (9/24) = 2.67 / day** |
| verdict | n<30 pending (n=1) | n<30 pending (n=1) |

After-scoreboard line (new code, live ledger):

```
  completed sets n = 1   (rest fills total = 1, one-legged = 0)   armed windows = 9   armed days = 9/24 = 0.38
  %positive = 100.0   fill rate = 2.67/day
  VERDICT: n<30 pending (n=1)
```

The clarification raises the measured fill rate from 0.50/day to 2.67/day (9 armed windows ran; only 2
distinct calendar dates were touched, so the old denominator over-penalised). No verdict changes: n=1 is
far below the n>=30 the gate is judged at, exactly as intended for a measurement definition registered
before the window it will judge.

## House-law receipts

- No `.env` / `*.pem` read; no `sim/out/sealed_eval/**` read; no sealed holdout path touched.
- No write to the proxy (127.0.0.1:8642); no network at all (report is read-only).
- Live ledger opened read-only; no bytes written to the live tree.
- STATUS: FROZEN preserved; no [pin] threshold text altered; the falsifier-pins test passes unchanged
  (existing assertions) plus the new clarification assertion.
