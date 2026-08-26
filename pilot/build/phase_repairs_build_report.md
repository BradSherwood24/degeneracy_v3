# Phase-3a instrument repairs — build report

Branch: `repairs/instruments` (off `main` @ ca9f8bd). Three instrument bugs in the pilot's fill
accounting, found in the 2026-08-23/24 armed campaign, plus Brad's 2026-08-26 ruling that S4 is now
sourced from the account balance. Ground truth throughout is the real journals in
`pilot/journals/2026082[34]*.jsonl` (the raw Kalshi order responses) and Kalshi's own realized
numbers. House law observed: no network calls, no sealed reads, no key/PEM/.env access, `python` only.

Test suite on this branch: **411 passed** (`cd pilot; python -m pytest -q`). Baseline before the work
was 406 passed + 1 pre-existing failure (see CONFESSIONS #1). The 4 sim/tests failures are outside the
pilot suite and were not touched.

---

## BUG 1 — units: NO-side prices reported in YES-space

**Symptom (real 8/23 14:00Z).** A NO buy with limit 0.97 that filled at 0.97 reported
`average_fill_price = 0.0300`; a NO reduce-only sell at limit 0.95 reported `0.0500` — both exactly
`1 − limit`. Kalshi's realized on the pair was −0.0255. Downstream this produced (a) a phantom A1
slippage alarm `|0.03 − 0.97| = 0.94`; (b) the NO leg booked at 0.03 notional, so `realized_min` was
overstated and S1 could mis-fire (8/23 15:00Z tripped a FALSE S1 for exactly this reason — see below);
(c) parity/report fills carried YES-space prices for NO legs.

**Fix — one choke point at the parse layer.** `service/orders/envelope.py`:
- `OrderResponse` gains `raw_reported_price` (the untouched venue value) alongside `average_fill_price`
  (now always the **order's side-space** value, directly comparable to the leg's `limit_price`).
- New `normalize_fill_to_side(resp, side)` is the single normalization function: `price_no = 1 −
  price_yes` for a NO order, pass-through for YES. It is **idempotent** — the normalized price is
  always re-derived from `raw_reported_price`, so it cannot double-flip.
- `parse_entry_response` captures the raw venue price into both fields; `parse_single_response(body,
  side)` and the executor's batch path (`Executor._align_batch`, `_dispatch` single path in
  `service/executor.py`) call `normalize_fill_to_side` with the leg's side. Every consumer
  (ledger, stops, parity, reports) therefore sees a side-space price with no per-consumer logic.
- The journal preserves the raw venue value: `ledger.response_to_record` writes both fields.
- **Legacy journal migration.** `ledger.rebuild_from_journal` detects a pre-fix record (no
  `raw_reported_price` KEY) and normalizes it using the matching intent leg's side (intents are
  journaled before their responses, so the side is known). New journals carry the key and are left
  untouched — verified there is no double-flip.

**Fee is side-INDEPENDENT.** Checked against Kalshi's realized: 14:00Z is
`(0.95 − 0.0034) − (0.97 + 0.0021) = −0.0255`, exactly Kalshi's number, using the reported fees as
plain dollar amounts. A side-dependent fee would not reconcile. Fees are never flipped.

**Verification (real 8/23 14:00Z journal, `rebuild_from_journal`):**
```
high(NO) KXBTCD-26AUG2310-T77499.99  avg_buy_price 0.9700  sold@ 0.9500
slippage vs 0.97 limit = 0.0000       realized_cashflow = -0.025500
```
The NO leg books at 0.97 and slippage is 0.0000, as required.

---

## BUG 2 — unbooked losses (realized_delta = 0 for real cash flows)

**Symptom.** `fills_record` set `realized_payoff = None` when `matched_pairs == 0`, and `run_window`
booked 0 for those windows. So: (a) an unmatched-leg flatten (14:00Z: buy NO 0.97, sell NO 0.95 →
real −0.0255) booked 0; (b) naked legs that rode to settlement (05:00Z YES@0.48, 11:00Z YES@0.31,
and 00:00Z NO@0.01) booked 0. The S4 daily leash was blind to all of it.

**Fix — a realized number for EVERY window with any fill** (`service/ledger.py`,
`service/run_window.py`):
- `LedgerState.realized_cashflow()` = `−pair_net_cash_out()` — the settlement-INDEPENDENT net cash
  from actual fills (complete P&L for a closed/flattened position).
- `LedgerState.realized_at_close()` = cash flow **plus** the sub-$1 flip's guaranteed ≥$1/pair floor
  on matched pairs (a genuine settlement floor → conservative and correct). Strangle/naked held legs
  contribute only their cash OUTLAY here — the safe direction, never an assumed win.
- `LedgerState.unsettled_legs()` = the held legs whose settlement payoff is still pending (sub-$1:
  naked overhang only, since matched pairs are floor-booked; other sources: every held leg).
- `run_window._build_ledger_entry` now books `realized_at_close()` for any window with a fill and
  records `unsettled_legs` in the ledger row.

**Settlement backfill (part c)** — the held legs' payoff, once the market result is known:
- Pure `ledger.settlement_payoff(unsettled_legs, results)`: a contract pays $1 iff its held side
  matches the market result. Fail-closed on a missing/invalid result. Because the outlay was already
  booked at close, the payoff is purely additive: a losing leg adds $0 (the conservative close loss
  stands); a winning leg adds count×$1.
- CLI: `python -m service.pilot_ledger backfill --window <close> --result TICKER=yes|no
  [--results-file f.json]` appends a separate backfill ledger row (`fires=0`, so promotion counters
  ignore it; only `realized_delta` feeds `s4_running_loss`). Idempotent (refuses a second backfill
  unless `--force`). Explicit results were chosen as the **simplest correct** source and are fully
  testable offline; the auto-step alternative (read the result from the proxy `/markets` helper at the
  next window's reconcile-first) is deliberately NOT wired so this module keeps opening no socket.
  Also fixed a latent argparse bug where the shared-parent `--ledger` default clobbered a value given
  before the subcommand.

**Re-derived armed span (rebuilt from the journals with the repaired code):**

| window (UTC)      | source      | realized @ close | backfill | final    | unsettled leg |
|-------------------|-------------|-----------------:|---------:|---------:|---------------|
| 2026-08-23T14:00Z | sub$1-flip  |        −0.025500 |    —     | −0.025500| — (flattened) |
| 2026-08-23T15:00Z | sub$1-flip  |        +0.000700 |    —     | +0.000700| — (floor pair)|
| 2026-08-24T00:00Z | sub$1-flip  |        −0.010700 |     0    | −0.010700| no ×1 (naked) |
| 2026-08-24T02:00Z | Q1-strangle |        −0.041800 |    —     | −0.041800| — (flattened) |
| 2026-08-24T03:00Z | sub$1-flip  |        −0.025400 |    —     | −0.025400| — (flattened) |
| 2026-08-24T04:00Z | sub$1-flip  |        −0.025500 |    —     | −0.025500| — (flattened) |
| 2026-08-24T05:00Z | sub$1-flip  |        −0.497500 |     0    | −0.497500| yes ×1 (naked)|
| 2026-08-24T06:00Z | sub$1-flip  |        −0.012100 |    —     | −0.012100| — (flattened) |
| 2026-08-24T07:00Z | sub$1-flip  |        −0.012100 |    —     | −0.012100| — (flattened) |
| 2026-08-24T11:00Z | Q1-strangle |        −0.325000 |     0    | −0.325000| yes ×1 (naked)|
| 2026-08-24T12:00Z | sub$1-flip  |        −0.001500 |    —     | −0.001500| — (flattened) |
| **TOTAL**         |             |    **−0.976400** |    0     |**−0.976400**|             |

**−0.9764 ≈ −$0.98** (Kalshi's number). Note the reconciliation is **not circular**: the at-close
total books each naked leg at its worst case (cash outlay, i.e. leg loses). That the at-close total
already equals Kalshi's realized is itself the proof that none of the three naked legs won — a win
would have moved the total up by ≈$1. The backfill, applied with all three legs lost, adds $0 and
leaves the total unchanged.

(8/23 15:00Z: the complete sub-$1 pair now books its true +0.0007 floor and does NOT trip S1. Under
BUG 1 the NO leg was booked at 0.992 (YES-space), inflating net cash-out to 1.9833 and mis-tripping
S1 — that false S1 is gone.)

---

## BUG 3 — stops don't latch across windows + S4 from balance

**Symptom.** S1 tripped once and S2 three times during the armed span, yet subsequent windows kept
arming: each window is a fresh :40 process, and `StopController.trip` only froze the in-process
executor — nothing persisted. The falsifier says a stop halts the DAY.

**Fix — day-scoped guard file** `pilot/ops/stops_YYYY-MM-DD.json` (UTC day), `service/stops.py`:
- Holds `latched` (day-halting stops S1–S4) and `balance_start_cents` (the S4 baseline).
- `StopController.trip` persists any S1–S4 trip via `record_latched_stop` (alarms do not latch).
- `run_window.prepare` reads the guard at arming: a latched S1–S4 → refuse to arm (degrade to dry,
  loud journal record); a **corrupt** guard → fail closed (refuse). A new UTC day is a new path →
  a fresh, un-latched guard (a stop halts the DAY, not forever).

**S4 from ACCOUNT BALANCE** (Brad's ruling 2026-08-26 — this account runs no other strategy, so
balance delta == P&L):
- First wake of the UTC day snapshots `balance_start` from the proxy `/portfolio/balance` (read-only)
  into the guard file. Every wake: `loss = balance_start − balance_now`; `loss ≥ cap` → latch S4
  (refuse to arm, degrade to dry, loud record).
- The endpoint returns **cents as integers**; `parse_balance_cents` / `balance_loss_dollars` convert
  explicitly to Decimal dollars (tested).
- If positions are open at wake (reconcile-first sees them), the balance is not clean cash → record
  `balance_check_skipped_open_positions`, do not compare, do not snapshot a dirty baseline.
- Missing/failed balance read at wake → fail closed (do not arm).
- The repaired ledger realized stays as the SECONDARY figure: `pilot_ledger.s4_running_loss` is
  unchanged and each wake journals `ledger_vs_balance_delta` so a venue-vs-ledger divergence is
  visible. The old ledger-based S4 day-lock was removed; A4 (guard trips) stays ledger-based.
- Cap comes from `StopConfig.daily_loss_cap_dollars`, **default changed $5.00 → $3.00** (a constant
  for now; the box falsifier will pin it).

---

## Tests added (all green; 411 total on this branch)

- `tests/test_orders_envelope.py`: `normalize_fill_to_side` (NO flip / YES pass-through / idempotent /
  None), `parse_single_response(side=...)`.
- `tests/test_executor.py`: batch normalizes a NO leg to NO-space (YES leg pass-through), single
  reduce-only NO sell normalized.
- `tests/test_instruments_repairs.py` (new): BUG-1 legacy-journal rebuild normalization (real 14:00Z,
  realized = Kalshi's −0.0255) + new-journal no double-flip + `response_*_record` round-trip preserves
  `raw_reported_price`; BUG-1a phantom-A1 gone; BUG-2 realized/unsettled primitives (flatten / naked /
  sub-$1 matched / strangle matched) + no-fill zero + `settlement_payoff` win/lose/fail-closed; backfill
  CLI (losing→0, winning→+1, idempotent); BUG-3 day-guard round-trip / missing / corrupt /
  latch-append-over-corrupt / next-day-clears, `ensure_balance_start` first-wake+reuse+new-day,
  `parse_balance_cents`, `balance_loss_dollars`, `s4_balance_breached` at/over/under cap, controller
  persists S1–S4 latch (alarms don't).
- `tests/test_run_window.py`: armed refused by S4 balance loss / latched stop / corrupt guard /
  balance-read failure; first-wake snapshot arms + journals `first_wake`+`ledger_vs_balance_delta`;
  open-positions skip; strangle books conservative cashflow (never the optimistic win); `_svc` now
  injects a balance; the F3 ledger-S4-lock test was replaced by the balance-based equivalents.
- `tests/test_stops.py`, `tests/test_review_probes3.py`: S4 cap default updated to $3.00 (explicit caps
  where the arithmetic must hold); the stale draft-falsifier test rewritten against a temp file.

Files changed: `service/orders/envelope.py`, `service/orders/__init__.py`, `service/executor.py`,
`service/ledger.py`, `service/stops.py`, `service/run_window.py`, `service/pilot_ledger.py`, and the
tests above (`test_ledger.py` itself was NOT modified — its would-be additions live in
`test_instruments_repairs.py`). `parity.py` needed no change — parity reads the ledger's
already-normalized positions.

---

## CONFESSIONS

1. **Pre-existing red test, now fixed forward.** On the baseline the pilot suite had 1 failure:
   `test_stops.py::test_falsifier_currently_draft_refuses_arming` asserted the live ceremony falsifier
   is DRAFT, but it is now FROZEN (someone else froze it; I must not edit it). I rewrote that test to
   assert the invariant against a temp draft file (decoupled from the live file's status). This was not
   one of my three bugs; I fixed it because I own the file and a stale red test is bad hygiene.

2. **Settlement direction inferred, not queried.** I have no network access, so I did not fetch the
   venue settlement results for the three naked legs. I assert all three lost. This is not a guess: the
   at-close booking is the worst case (leg loses) and its total already equals Kalshi's independently
   reported −$0.98; a win on any naked leg would have raised the total by ≈$1, so the match is itself
   the proof. The backfill machinery is built and tested, and will apply the true result when run with
   `--result` (or, later, an auto-step reading the proxy `/markets` result).

3. **Balance payload shape is a best-effort guess.** `parse_balance_cents` accepts `balance`,
   `available_balance`, `available`, `portfolio_value` as integer cents (Kalshi documents `balance` in
   cents). I could not observe a real `/portfolio/balance` payload on disk (none is journaled). If the
   live field name differs, add it to `parse_balance_cents` — an unrecognized shape fails closed (no
   arm), which is the safe direction.

4. **`realized_at_close` books a naked leg's outlay as a conservative loss, marked unsettled.** For a
   sub-$1 flip this is only the naked OVERHANG (matched pairs are floor-booked); for a strangle it is
   every held leg. This intentionally over-counts loss until the backfill corrects it up — the honest,
   safe direction. It means the pre-backfill ledger can show a loss larger than the eventual realized
   if a held leg wins; the `realized_unsettled` flag and `unsettled_legs` mark exactly those rows.

5. **S4 in-process backstop retained.** `stops.on_realized` (realized-based S4) still exists and resets
   per process; it is now only an in-window backstop. The cross-window S4 truth is the balance check at
   wake. `test_review_probes3.py`'s R3 marker documented the old cross-window gap; I updated its note to
   say the gap is now closed by the day-scoped guard, and pinned its cap explicitly so its arithmetic is
   independent of the new $3.00 default.

6. **`FEE_IS_TOTAL` unchanged.** It stays `False` (fail-closed multiply-by-fill_count). At the pilot's
   n=1 size every fill has `fill_count == 1`, so it did not affect any armed-span number; the
   difference only appears at 2 pairs.
