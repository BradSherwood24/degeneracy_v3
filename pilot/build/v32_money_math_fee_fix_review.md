# Review — PR #72 `fix/money-math-fee-scaling` (head d19dd4e)

**Reviewer:** Opus 4.8 · **Date:** 2026-09-20 · **Base:** main (origin/main) · **Branch reviewed:** `review/pr72` ← `origin/fix/money-math-fee-scaling`
**Verdict: APPROVE WITH NITS** (all nits documentation/latent-path only; no code change required to land)

Diff scope (7 files, +281/-10): `service/_simlaw.py` (+3, re-export), `service/run_v32.py` (fee helpers + cost fix + docstring), `service/v32/ledger.py` (note + additive row key), `service/v32/executor.py` (docs-only), `PLAN_V32.md` (N2), `tests/test_v32_partial_fill.py` (+3 tests), `build/…report.md`. No files under `sim/` touched.

## 1. Fee-meaning trace — CONFIRMED

Read every fee-bearing path directly. `fee` on a fill record is per-contract on every taker path; the maker rest leg is 0:

| path | site | `fee` value | scale | verified |
|---|---|---|---|---|
| rest, WS `fill` | `run_v32.py::_record_fill` (`exec_fee = pf["fee_cost"]`, l.838/916) | venue per-FILL TOTAL; = 0 (post_only maker) | total, always 0 | yes |
| rest, 1 s poll | `run_v32.py::_record_fill(…, None, …, path="poll")` (l.965) | None → 0 | n/a | yes |
| rest, cancel-race | `executor.py::_finish_cancel` (`"fee": Decimal(0)`, l.819) | 0 (maker) | n/a | yes |
| rest, amend CROSS | `executor.py` amend-fill (`"fee": avg_fee`, l.934) | `average_fee_paid`, per contract | per contract | yes |
| wing IOC batch | `executor.py::_wing_events` (`"fee": r.average_fee_paid`, l.1080) | per contract | per contract | yes |

`average_fee_paid` is per-contract, corroborated three ways: (a) `orders/envelope.py:136` doc ("a dollar amount … venue-verified against Kalshi's realized"); (b) `service/ledger.py:25` comment ("the word 'average' makes per-contract") with `FEE_IS_TOTAL = False`; (c) the live 04:00Z row itself — stored 0.0103/0.0124 per contract × 2 ≈ the venue 0.0207/0.0248 per-fill totals. Builder's trace is accurate.

**Latent-path note (NIT N-a, not blocking):** the WS `fill` path stores the venue *total* (`fee_cost`) into the record's `fee` key, not a per-contract number. It is 0 today because the rest leg is a `post_only` maker. If a taker rest fill ever arrived via WS at count ≥ 2 with a nonzero `fee_cost`, `_fill_total_fee` would take the `count ≥ 2` branch and **recompute from the law** (`_fee_total(price,count)`) — so it does NOT multiply a venue total by count (no double-count; the money-math stays correct). But `_annotate_fee` would still copy that venue *total* into the `fee` key while the docstring labels `fee` "PER CONTRACT" — a latent mislabel on an impossible-today path. A one-line guard/comment ("WS rest `fee_cost` is a total; only ever 0 under post_only") would close it. Impossible under current post_only; documentation-only.

## 2. `_fee_total` rounding vs the venue — CONFIRMED

`_fee_total(p,count) = ceil(0.07·p·(1-p)·count·1e4)/1e4` — the $0.0001 ceiling applied ONCE to the whole fill. Reproduces the venue exactly on the live row: `_fee_total(0.82,2)=0.0207`, `_fee_total(0.77,2)=0.0248` (ran it). This is the venue's own rounding: per-contract-ceil×count would give 0.0104×2=0.0208 and average×count 0.0103×2=0.0206 — both wrong; the total-ceil-once gives 0.0207.

- Built on the frozen coefficient: `_simlaw.fee_rate = census.FEE_RATE` (`Decimal("0.07")`), the SAME constant `census.fee` (the per-contract law) uses. No reimplementation. `_fee_total(p,1) == _law_fee(p)` holds (asserted in the new unit test, and I re-ran it).
- `_simlaw.py` diff is +3 lines (one re-export). Importers of `fee_rate`: only `run_v32.py` (grepped `sim/` + `pilot/`). Nothing under `sim/` imports it; `census.py` only serialises `FEE_RATE` to its output JSON (unchanged). **No sim/law constant changed, no sim behaviour changed.**

**NIT N-b (awareness, not blocking):** for count ≥ 2 the money-math now trusts the frozen fee LAW (`_fee_total`) and *discards* the record's reported `average_fee_paid`; the venue's own per-fill total is not stored per-record for wings, so there is no venue number to "prefer." This is validated against ground truth — the reconstructed `realized_delta` = +0.2345 matches the actual balance move 53.3069→53.5414 exactly, whereas the pre-existing `ledger.py::_fee_of` convention (`average_fee_paid × fill_count` = 0.0206) would give +0.2346, off by $0.0001. So the new path is *more* accurate than the older V1.1 ledger convention. Two fee-total conventions now coexist in the tree (`ledger.py::_fee_of` avg×count vs `run_v32.py::_fill_total_fee` law-ceil-once); not a regression, flagged for future consolidation.

## 3. Regression tests — CONFIRMED, non-vacuous

`tests/test_v32_partial_fill.py` +3, all pass (ran isolated: `3 passed`). Adversarial mutation checks (scratch copy, restored after):
- **size-2 catches the bug:** reverting the cost loop to `cost += fee` once → `test_size2_0400z_realized_delta_uses_total_fees` FAILS with `Decimal('0.257300') == Decimal('0.2345')` — exactly the +$0.0228 over-statement.
- **size-1 non-vacuous:** the byte-identical test correctly *survives* the cost-loop revert (the fix is a no-op at count 1, which is the property it guards). To prove it is not vacuous I mutated the `count ≤ 1` branch (`per_d + 0.0001`) → `test_size1_single_lot_realized_delta_byte_identical_to_pre_fix` FAILS (`0.1208 == 0.1210`). It genuinely fails if count-1 behaviour changes.

**Full suite** (`cd pilot && python -m pytest -q`): **940 passed, 2 skipped, 2 errors, 0 failed** in ~11 s. The 2 errors and 2 skips are environmental (this worktree has no `historical-data/`):
- ERROR `tests/test_quintile.py::test_quintile_reproduction_exact` and `::test_head_of_corpus_insufficient_tape_is_noquintile` — `FileNotFoundError` on `historical-data/15-minute/markets/2026-06-11.jsonl`.
- SKIPPED `tests/test_box_golden.py:300` and `:351` — "historical-data absent".
(The builder's "944 passed" reflects an env with `historical-data/` present, where the 2 quintile tests pass; 940+2+2 = 944 collected. No test fails either way.)

## 4. Ledger row shape + scoreboard identity — CONFIRMED

- New keys are ADDITIVE only: `realized_delta_note` (ledger.py), `fee_total` + `fee_source` (per fill record via `_annotate_fee`, which returns a `dict(f)` COPY — no mutation of `executor.fills`). No existing key renamed/removed. `realized_lock` unchanged (0.1034/contract, core state) — the money-math return still sets it from `realized_lock`, untouched by the cost-loop edit.
- `report.py` reads `realized_lock` (l.214/222), never `realized_delta`. `git diff origin/main...HEAD -- service/v32/report.py` is **empty**. `ledger.py` diff touches only the `REALIZED_DELTA_NOTE` constant and one line inside `build_v32_ledger_row`; `load_v32_rows` is not in the diff (0 hits). So report output is provably identical to main given the same ledger.
- Ran `python -m service.v32.report --ledger <read-only copy of live v32_ledger.jsonl>` (131 rows copied to scratchpad). FALSIFIER SCOREBOARD:
  ```
  completed sets n = 8   (rest fills total = 8, one-legged = 0)   armed windows = 118
  realized lock: mean +11.0c  median +11.0c  p10 +10.1c  min +10.1c
  capture ratio = live 8 / shadow 14 = 57.1%
  shadow E=0.10: mean lock +10.5c   execution gap (shadow-live) -0.4c
  VERDICT: n<30 pending (n=8)
  ```
  Matches the expected baseline exactly: **n=8, mean +11.0c, capture 8/14, gap −0.4c.** Scoreboard identity holds.

## 5. Docs (N1 / N2) — CONFIRMED

- **N1** `executor.py::_amend_body` docstring: code is `"count": f"{int(action.count) if action.count else 1:.2f}"` → "2.00" at contracts=2, "1.00" default; docstring now describes exactly that. Amend-fill comment now describes the resting remainder (`parsed.remaining_count`) after a partial cross — accurate. executor.py diff is **100% docstring/comment** (no non-comment +/- lines).
- **N2** `PLAN_V32.md:210` sha `c6715fc7…` → `a2a58787…`. **Verified against ground truth:** `canonical_sha256(policy/v32_params.json)` computes to `a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c`, matching both `params.py::FROZEN_V32_PARAMS_SHA256` and the new PLAN line; the prior-freeze sha `0ac69795…` matches `PREVIOUS_V32_PARAMS_SHA256_2026_09_14`. `PLAN_V32.md:54` "Size: 2 contracts (AMENDMENT 1 …)" is correct. (The old `c6715fc7` was itself stale — it matched neither the 09-14 freeze nor current; the fix replaces it with the live value.)
- Frozen `ceremony/v32_falsifier.md` (l.32 "1 contract", l.145 "…JUSTIFY 2 contracts") — untouched, correct. `ceremony/box_falsifier.md` (different strategy) — untouched, correct.

**NIT N-c (optional):** `PLAN_V32.md:183` still reads "…1 contract; retirement R1-R4…" inside a Phase-4 description of the falsifier's own terms. Defensible (it mirrors the frozen falsifier, which is deliberately still 1 contract), but a reader could misread it as a current fact now that the pilot is armed at 2. A parenthetical ("as frozen; live is 2 per AMENDMENT 1") would remove all ambiguity. Not blocking.

## 6. Scope — CLEAN

No scope creep. Every hunk maps to the brief (money-math cost fix, additive fee keys, `realized_delta_note`, N1, N2, 3 tests, build report). No behavioural change outside `_compute_money_math`'s cost loop and the additive annotations; `realized_lock`, floor geometry, `lock_value`, and the falsifier read-path are untouched.

## Verdict

**APPROVE WITH NITS.** The bug is real, the fix is correct and validated against the live balance (+0.2345), size-1 history is provably unchanged, the falsifier scoreboard is byte-identical, and the docs (N1/N2) are accurate against ground truth. Nits N-a/N-b/N-c are documentation/latent-path only and do not require a change to land. Recommend a follow-up one-liner for N-a (WS `fee_cost` is a total) if/when a taker rest path is ever contemplated.
