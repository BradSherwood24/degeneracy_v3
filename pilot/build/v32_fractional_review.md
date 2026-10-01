# V3.2 fractional contract counts — adversarial review

Reviewer: Claude Opus 4.8 (adversarial). Under review: `fix/v32-fractional-counts` @ `4870de1`
(3 commits on `origin/main` @ `e240311`). Review branch: `review/v32-fractional-counts`.

## VERDICT: APPROVE WITH NITS

The change does exactly what it claims: every V3.2 fill path now carries the EXACT Decimal `count_fp`,
sub-lot fills are booked and hedged at their true size, rests stay WHOLE on the wire (fail-closed), and
whole-lot history stays byte-identical. One real deviation from `main` surfaced — a display-only JSON
field — fixed on the review branch with a regression test. Core correctness, economics, fail-closed
behaviour, cross-roster safety, and the Registration/ceremony are all sound.

Full suite on the review branch (post-fix): **1449 passed, 5 skipped** (worktree env; builder's
live-tree env = 1452/1; the 5-vs-1 skip delta is env-gated tests, total 1453 both ways; +1 is my new
regression test).

## Findings

### N1 (NIT — FIXED on the review branch) — `--json` report `size` drifted to a string for whole counts
`build_ledger_reconciliation` set `"size": total_count`, and `total_count` is now a `Decimal` (via
`_cnt`). For a WHOLE set the `--json` report (`report.py:695`, `json.dumps(..., default=str)`) therefore
emitted `"size": "2"` (a JSON string) where `main` emitted `"size": 2` (a JSON number). Proven by
rebuilding the full report over the real 23-set live ledger on both `e240311` and `4870de1`: the ONLY
diff in the entire report (text + JSON) was this `size` field (`"1"`/`"2"` vs `1`/`2`). The human-facing
TEXT table (`_render_reconciliation`, the operational artifact) was byte-identical; the value is
preserved; no golden test, tripwire, ledger row, journal, or money-math field is affected (every other
whole count already serialises as a bare int via `_count_out`/`_co`). Severity: nit — but a genuine
deviation from `main` for the frozen-history report, against the build report's "byte-identical
everywhere" claim.
- Fix: added `report._co` (mirrors `ledger._co`) and wrapped `"size": _co(total_count)` — whole -> bare
  int (JSON number, byte-identical to main), fractional -> Decimal (str-encoded like the other recon
  fields). Re-diffed the full report JSON over the real ledger: now **byte-identical to main**; a
  fractional recon row still reports `size == Decimal("0.44")`.
- Test: `test_report_recon_size_whole_is_bare_int_fractional_is_decimal` (whole int/"2"/"2.00" rows ->
  bare int + JSON number; fractional -> Decimal).

### O1 (observation — not a defect) — place-while-cancel-in-flight is pre-existing, not introduced
The adversarial "two live rests" probe (0.44 WS fill, then a stand-down requote) emits `CANCEL_REST`
then `PLACE_REST` while `cancel_in_flight` is still True. Reproduced the IDENTICAL sequence on `main`
(`e240311`) at `contracts=2` — it is the pre-existing stand-down/recover sequencing, reconciled by the
executor's cancel-confirm cumulative->delta booking (`rest_booked_by_coid`). "Never two live rests"
holds in the core model (amend-in-place keeps one order; the sequential cancel->place serialises at the
executor, and any race fill books via the cumulative delta). On `main` the same 0.44 fill was LOST
(int-truncated to 0, rest_remaining stayed None, count stayed 2 = over-hedge) — the exact bug this PR
fixes. Out of scope for this change.

### O2 (observation — acceptable) — redundant `_dc`/`_count_body_str` on `V33LiveExecutor`
The base `LiveExecutor` now carries `_dc`/`_count_body_str`; `V33LiveExecutor` keeps identical overrides.
They are idempotent (quantize of an already-2dp value), so zero behaviour change and no double-conversion
(confirmed: full suite green, V3.3 tests unchanged). Builder left them to avoid touching the frozen V3.3
executor — acceptable; a later DRY pass can delete them.

## What I verified (receipts)

**Byte-identity (whole counts).** Full report over the real 23-set live ledger: text byte-identical on
both commits; JSON byte-identical after N1 fix. `build_ledger_reconciliation`, `v32_pending_credit`,
`_v32_floor_booked_for_entry` produce identical values on the real 381-row ledger. Serialization
primitives (`run_v32._count_out`, `ledger._co`, `executor._count_out`, `core._q_count`, `report._cnt`)
all return a bare `int` for 1/2/`Decimal('2.00')`/`'2'`/`'1.00'` — `json.dumps` => `2`, not `"2.00"`.
Wing wire body `_count_body_str(2)` == `"2.00"` == `to_v2_order`'s `f"{int(count):.2f}"`. Replay/tripwire
(`sim/v32_replay`, `tools/daily_replay.py`) parse only venue market-data frames (`count_fp` via
`_to_dec`), never our journalled fill/action counts — unaffected.

**Fail-closed sub-lot rests.** `_rest_place_count` = `int(_rest_size)` floors (1.56 -> 1); `_requote`
returns before any place/amend when `_rest_place_count < 1` (never a count-0 wire order). Probed: a 0.44
fill of a 2-lot rest -> requote places WHOLE 1 (<= 1.56, never 0); a 1.56 fill leaving 0.44 -> no
amend/place emitted (kept on the original order, or dropped off the book on a real no-quote cancel —
under-exposed, never naked/over-sized). A dropped sub-lot is accounted as `lots_unfilled_at_quote_end`
(= contracts − lots_filled), never a held/naked leg.

**Partial-fill wings + dedup.** 0.44 fill -> `TAKE_WINGS` 0.44 (both legs 0.44), 1.56 rests; a later fill
hedges only its own delta. Cumulative->delta via `rest_booked_by_coid` dedups the SAME fractional fill
across WS + poll + cancel-confirm (`test_core_fractional_cancel_confirm_books_delta`,
`test_driver_poll_fractional_delta_books_exactly`).

**Fees (KALSHI_FEE_EXACT).** whole-1 -> `per_contract` = `_fee(p)` (byte-identical); 0.44 -> `law_total`
`ceil(0.07*p*(1-p)*0.44)` = 0.0065 at p=0.30; 1.44 -> 0.0212; 2 -> 0.0294 — all equal the law. The
`count <= 1` -> `count == 1` shortcut change is correct (only fractional sub-1 now takes `law_total`).

**Economics.** `v32_set_floor_dollars(3, 0.44)` = 0.88, `(2,0.44)`=0.44, `(1,0.44)`=0. `settlement_payoff`
= `Decimal(str(count)) * $1` (fractional-safe). S4 band (`v32_pending_credit`) and backfill count-weighted.
`reconcile_positions_clean` parses `position`/`position_fp` as Decimal and fails closed on any non-zero
(fractional-safe; file unchanged by this PR).

**Cross-roster.** `_fractional_counts` True on the base; `V33LiveExecutor` still sets True explicitly;
`run_v33.on_fill`/`on_poll_fill` read `payload["count_fp"]` / `filled_count_fp` directly (normal path
unchanged; the shared-parser edge path is strictly more accurate). The two test edits assert the NEW law
honestly (not weakened): `test_v33_fill_attribution` now asserts the shared parser returns `Decimal('0.44')`;
`test_v32_cancel_confirm_stays_int_byte_identical` asserts `_fractional_counts is True` AND still a bare-int
whole cancel-confirm journal.

**Registration / ceremony.** `v32_falsifier.md` diff vs `main` = a single 48-line insertion below
`## Registration` (line 188); STATUS `FROZEN` (line 3), the params sha, the `n >= 30` count, and
everything above Registration untouched. Brad's verbatim ("Then start the build on the fractional.")
present; MECHANICS CLARIFICATION, no `[pin]`/threshold/sha/STATUS move. Registered n=23 verified against
a read-only scratchpad copy of the live ledger: `sum(sets_done)` over armed rows = 23, and armed rows
with `realized_lock` set and not `one_legged` = 23. `ops/V32_ARMING.md` item 11 consistent and accurate.

House law kept throughout: `python` only; no network/proxy; no `.env`/`*.pem`; no `sim/out/sealed_eval/**`;
the SEAL and the 2026-08-20..29 holdout untouched; the live tree was only READ (read-only ledger copy to
scratchpad to verify n). Worktree left detached and clean.
