# Review — PR #70 `amend/contracts-2` (head 0f96011): V3.2 AMENDMENT 1, contracts 1 -> 2

Reviewer: Opus 4.8. Worktree `C:\Users\Brads\Python_stuff\dv3_wt_review` on branch `review/pr70`
(= `origin/amend/contracts-2` @ `0f960114d5a7d5f99fc782d0de3636a553a1048e`), base `origin/main` `8543ad4`.
`python` only. No order writes; the only proxy call was a read-only `curl -s 127.0.0.1:8642/health`.
`historical-data/`, the seal, `.env`/`*.pem`, and `sim/out/sealed_eval/**` were never read.

## VERDICT: APPROVE WITH NITS

The amendment is a clean, minimal, correctly-enforced size step. The freeze rule is honoured (pure
append), the sha re-pin is real and self-verifying, both of Brad's quotes are verbatim, every
Registration factual claim reproduces against code, the size-1 regression suite is preserved
byte-identically, and the size-2 runtime is size-aware everywhere that touches money. My independent
5-scenario probe against the REAL params (`contracts=2`) passed. Two documentation nits (below), neither
a blocker, neither in code that runs money.

---

## What I verified (receipts)

### 1. FREEZE RULE — PASS
`git diff origin/main...HEAD -- pilot/ceremony/v32_falsifier.md` is a single hunk, ZERO deletions
(`grep '^-' | grep -v '^---' | wc -l` = 0). The entry is appended inside `## Registration` (line 188,
append-only), at line 212 — after `MEASUREMENT CLARIFICATION 3` (line 203) and before
`## Pre-registered shadow observations` (line 283). Nothing above the "change NOTHING above this line"
marker moved. Line 3 is exactly `STATUS: FROZEN`.

### 2. SHA — PASS
- Recomputed `sha256(json.dumps(obj, sort_keys=True, separators=(",",":")))` of
  `pilot/policy/v32_params.json` = `a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c`
  = `FROZEN_V32_PARAMS_SHA256`. Match.
- `load_v32_params()` returns `contracts == 2` and `.sha256 == FROZEN_V32_PARAMS_SHA256` (self-verifies).
- `PREVIOUS_V32_PARAMS_SHA256_2026_09_14` = `0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc`. Match.
- `git diff -- pilot/policy/v32_params.json` is exactly the one line `"contracts": 1,` -> `"contracts": 2,`.

### 3. REGISTRATION CONTENT — PASS (every claim checked against code)
- Both of Brad's quotes are word-for-word verbatim (the 2026-09-18 one is markdown line-wrapped, all
  words in order; collapsed and diffed against the source — identical). Both test-asserted substrings
  present exactly once.
- Both shas recorded (old `0ac6979…` -> new `a2a5878…`); the ONLY stated value change is `contracts`.
- Promotion (`n >= 60` / `10 lots`) superseded FOR THIS STEP by pointer, pins kept DEFINED in code
  (`V32_PROMOTION_MIN_N == 60`, `V32_PROMOTION_MIN_DEPTH_LOTS == 10`); the Promotion section text is not
  edited. The five verdict gates listed UNCHANGED (they live above the freeze line; the diff touches none).
- Proxy cap 2 == `params.contracts`; `v32_caps_agree` refuses a `contracts` of 3.
- Measurement continuity claims verified in code: `service.v32.report` has ZERO `params_sha` references
  (pools every armed row into one `n`); `service.v32.ledger.build_v32_ledger_row` records
  `params_sha = params.sha256` per row (line 190) and never filters; sets = per rest-fill event, lock per
  contract (core batch logic). MUST CONFIRM (a)-(e) present in the entry.

### 4. TESTS — PASS
- `cd pilot && python -m pytest -q` in THIS worktree: **937 passed, 2 skipped, 2 errors**. The 2 skips
  (`test_box_golden.py`) and 2 errors (`test_quintile.py`) are ALL "historical-data absent" — the review
  worktree has no `historical-data/` (a feature, not a bug). None is amendment-related. The build report's
  **941 passed** was measured in `dv3_wt_v11`, which has `historical-data/` present, so those 4 pass there
  (937 + 4 = 941). The 146 v32 tests pass here with zero failures.
- No test deleted. Every changed assertion is a legitimate mirror of the amendment and is listed in the
  build report:
  - The one edited value assertion (`test_partial_fill_contracts_lever_unchanged_at_one`,
    `test_v32_falsifier_pins.py`) flips `contracts == 1` -> `== 2`, keeps the `sha256` check — not vacuous.
  - Size-1 regressions preserved via a new `_params1()` helper / explicit `contracts=1` overrides in
    `test_v32_partial_fill.py`, `test_v32_core.py`, `test_v32_amend.py`, `test_v32_executor.py`. The
    golden (`test_v32_golden.py`) pins `contracts=1` and reproduces the reference exactly: it asserts
    `r["lock"] == Decimal("0.1036")` and `take.lock == ref["lock"] == Decimal("0.1036")` (+10.36c) — a
    hard value, not vacuous (5 passed).
  - `test_both_batches_complete_counts_two_sets` correctly switches to the REAL `_params()` (now = 2) and
    ADDS `assert p.contracts == 2 and p.sha256 == load_v32_params().sha256` — a strengthening, not a
    weakening.
  - The ADD-ONLY `AMENDMENT 1` section (6 tests) hard-codes both sha literals, checks Brad's exact words,
    `STATUS: FROZEN`, the promotion pins, `v32_caps_agree` (max 2 / c 2 ok; max 1 / c 2 refuse; c 3 refuse
    both ways) and `v32_arming_check(contracts=2)`.
- Mutate-and-check (scratch copies / overrides; tree clean afterward, verified `git status`):
  - `v32_caps_agree(health(max 2), contracts=3)` -> refuse `"proxy max_contracts_per_order 2 < params.contracts 3"`;
    `v32_caps_agree(health(max 3), contracts=3)` -> refuse `"… 3 > V3.2 ceiling 2"`.
  - `load_v32_params(path=<corrupted copy contracts=3>)` -> raises `V32ParamsShaMismatch`; a tol-drift copy
    -> same. The real file still loads `contracts 2`. No stray edits left.

### 5. RUNTIME AT SIZE 2 — PASS (the money-critical part)
Read `core.py`, `executor.py`, `run_v32.py`, `stops.py`. Every path that sizes an order or a hedge reads
`params.contracts` (never a hard-coded 1):
- `core._rest_size` returns `params.contracts` until a fill, then `rest_remaining`; PLACE_REST/AMEND_REST
  bodies size on `_rest_size` (core lines 1008, 1042); wing batches size on the fill delta
  (`int(delta)` / `int(count)`); rest fills book from the venue EVENT count (core lines 1332-1333), not a
  record.
- `executor` create body (line 419), rest wire body (line 477) and amend wire body (line 960) all use
  `int(action.count)`; `_compute_money_math` is called in production only at `run_v32.py:1527` with
  `contracts=params.contracts`; the arming path passes `contracts=params.contracts` (line 1781).
- Cancel resolution's `filled_delete = max(0, rec.count - reduced_by)` is size-aware via the record's
  `count` AND is cross-checked with `max(…, status-GET filled)` so a fill is never under-counted.
- The `count=1` at `executor.py:449` is the UNKNOWN-OUTCOME (timeout/5xx) reject record only; a late fill
  on it books from the venue event count, and the cancel `filled_delete` is backstopped by the status-GET
  max — it can only over-cancel-protect, never under-hedge. Not a money bug.
- S4 day cap `$3.00` is a size-invariant dollar cap. Worst realistic single size-2 set loss (a naked
  one-legged rest at n≈0.35 = 2 × $0.35 = $0.70, or an F-2 gated wing-fail) stays well under $3.00, so the
  cap still latches correctly (it simply latches after fewer bad sets at size 2, which is intended).
- One-legged (S1) is counted PER BATCH; two batches at size 2 are handled independently.

Independent probe (`scratchpad/review70/probe70.py`) drove `decide_v32` at the REAL params
(`load_v32_params()`, contracts=2; tol/deb relaxed as engine levers, size under test unchanged). All five
required scenarios PASSED:
1. full 2-lot fill in one print -> one wing batch count 2, allotment done, no remainder;
2. 1+1 across two prints -> two batches / two sets, distinct wing coids (no double-book);
3. 1 fill then quote-end -> CANCEL_REST pulls the remainder;
4. 1 fill (WS) then the 2nd lot only on a cumulative cancel/ctx -> lot 2 booked via `cancel_ctx`;
5. amend cross partial + WS echo -> delta booked once, echo de-duped, `amend_cross_pending` guard consumed.
Existing tests already cover all five (`test_v32_partial_fill.py`: `test_both_batches_complete_counts_two_sets`,
`test_second_fill_spawns_second_batch…`, `test_quote_end_cancels_partial_remainder`,
`test_poll_backstops_missed_second_lot_contracts2` / `test_quote_end_cancel_books_missed_second_lot`,
`test_amend_cross_books_per_amend_delta_and_latches_allotment` /
`test_partial_amend_cross_ws_echo_not_double_booked`).

### 6. DOCS — PASS (with nit N2)
`pilot/ops/V32_ARMING.md` item 6 now expects the NEW sha `a2a5878…` and records the prior frozen sha
`0ac6979…`; the correction from the stale pre-freeze `c6715fc7…` is right. MUST CONFIRM item 10 is present
with (a)-(e), mirroring the falsifier entry.

### 7. /health (read-only) — PASS
`orders_enabled: true`, `caps.max_contracts_per_order: 2`, `ticker_prefixes: [KXBTC15M, KXBTCD, KXBTC]`,
`daily_order_budget: 4000`, `orders_remaining_today: 4000`.

---

## Findings

**N1 (NIT, docs-in-code, no runtime effect) — `pilot/service/v32/executor.py`.**
The `_amend_body` docstring (≈ line 883) says `count fixed-point "1.00"` and the amend-fill inline comment
(≈ line 921) says "with count 1 nothing remains resting". Both describe pre-size-2 behaviour; the code
correctly emits `f"{int(action.count) …}"` (verified: an amend of the full allotment carries count 2 in my
probe). Fix: update the two strings to say the count is `action.count` (the still-resting remainder).
Cosmetic — not a blocker.

**N2 (NIT, out of this PR's scope) — `pilot/PLAN_V32.md:210`.**
Still carries `Params sha … c6715fc7fd8339e0cc8877bd39bb78b04239eda9c490bde71a53333a48bdfb92` — the
pre-freeze Phase-4 sha, stale since before the 2026-09-14 freeze (it was never updated to `0ac6979…`
either). Not touched by this PR; flag for a future doc sweep to `a2a5878…`. (The `c6715fc7…` strings in
`pilot/build/*.md` are historical review records and correctly frozen in time.)

No BLOCK findings: no reproduced failure, no wrong Registration claim.
