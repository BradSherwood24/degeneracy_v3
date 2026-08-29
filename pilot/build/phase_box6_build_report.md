# Phase box-6 build report — roster box-v1.1 (implied-pin floor) + S4 pending-settlement band

Branch `box/v1.1-implied-floor` off `main @ e33d99a`. Built in worktree `../dv3_wt_v11`. Brad's go
(verbatim, 2026-08-28 ~23:30Z): "Go ahead with it. Full review and agent build as before. Let me
know when it's ready."

## Test count

`cd pilot && python -m pytest -q` → **604 passed** (baseline 571 + 33 new). Zero skips/xfails.

Note on the baseline: a fresh worktree does not carry the gitignored data dirs `historical-data/`
and `sim/out/` (untracked, main-tree only), so 7 data-dependent tests (parity/quintile/shakedown/
reference_impl_review/golden) fail to collect. I restored the 571 baseline by adding two read-only
directory **junctions** in the worktree pointing at the main tree's copies (`historical-data`,
`sim/out`) — both are gitignored, never staged, and the main tree is untouched. Without the
junctions the runnable subset is 597 passed (the 7 data tests error on missing files, unrelated to
this change).

## Part A — roster box-v1.1: implied-pin floor (literal skip-the-hour)

- `policy/box_params.json`: `roster_name` → `box-v1.1`; added `"min_implied_pin": "0.80"`; appended
  the skip sentence to `description`. Every other value byte-identical. New canonical sha
  **`cec4b1a29c5d46deac09fd7a46ec0e08b7603a1f6862758cdb60e97a477aa42c`**.
- `service/box.py`:
  - `BoxParams.min_implied_pin: Decimal` — REQUIRED loader key (fails closed via KeyError if absent;
    never defaulted).
  - Re-pinned `FROZEN_BOX_POLICY_SHA256` to the new sha; kept the old one as
    `BOX_V1_POLICY_SHA256 = "480d4634…b3eb"` (the report's roster partition uses it).
  - New order-free action kinds `BOX_SKIP`, `BOX_RESCAN_WOULD_FIRE`.
  - `BoxState` new fields: `skipped`, `skipped_selection`, `rescan_emitted`, `rescan_selection`.
  - `decide_box` step 7: at the first qualifying fresh instant, `implied_pin < min_implied_pin` →
    `BOX_SKIP` (sets `entered` + `skipped`; a skip is a skip even in shakedown), NO orders; `>=`
    (equality included) → FIRE/WOULD_FIRE as before. New rescan branch (runs before the "already
    entered" gate when `skipped and not rescan_emitted`): the first later instant re-qualifying
    `implied >= floor`, fresh, inside the entry window emits ONE paper `BOX_RESCAN_WOULD_FIRE`.
    `BOX_SKIP`/`BOX_RESCAN_WOULD_FIRE` both carry `legs` (informational — the legs a fire would have
    sent).
- `service/box_runner.py`: mapped `BOX_SKIP` → `box_skip_implied`, `BOX_RESCAN_WOULD_FIRE` →
  `box_rescan_would_fire`; each journals the SAME `selection` payload shape as `box_would_fire` (from
  `skipped_selection` / `rescan_selection`) plus `implied_pin` / `min_implied_pin` (`t_minus_s`
  already carried on the action).
- `service/run_window.py`: box summary row gains `box_skipped` / `box_rescan` (from the driver
  actions); neither counts in `fires`/`would_fires`/`signals`. `_on_box_action` already ignores
  non-FIRE, so the skip path touches no order plumbing, no ledger, no S1/A5/flatten (asserted in the
  wiring test: zero orders on the fake executor, `ledger_state is None`).
- `service/box_report.py`:
  - Roster partition: `build_window` tags each window `roster`/`policy_sha` (untagged → box-v1). The
    gates R1–R4/A1/A5 and the headline count ONLY the current roster (sha == the new pin);
    `legacy_roster_summary` renders/JSONs box-v1 as a frozen `legacy_rosters` line (computed n / pins
    / misses / realized).
  - SO-1 (`shadow_implied_rule`) now spans BOTH rosters and takes `paper_skips` / `rescans`. SKIPPED
    = real two-leg fills `< 0.80` + paper `box_skip_implied` windows, with `n_real` / `n_paper`; new
    paper `rescan` group. `build_paper_window` scores a paper skip/rescan on settlement via a
    backfill row keyed on close_time when present (`payoff − C_decision`), else reads unsettled.
  - Text render: `LEGACY box-v1` line, `SKIPPED - IMPLIED-PIN FLOOR` section tagged
    `[skipped-implied]`, `RESCAN` section, and the SKIPPED `[real=… paper=…]` split.
- `ceremony/box_falsifier.md`: appended `## Amendment box-v1.1 — 2026-08-29` (Brad's words, the two
  evidence tables, the policy delta, the S4 amendment, the gate restart, the new sha) and one
  Registration line. STATUS: FROZEN and everything above the "change NOTHING above this line" marker
  are untouched. `arming_check` on the amended file still returns armed (test
  `test_amended_box_falsifier_still_arms`).
- `ops/BOX_ARMING.md`: roster `box-v1` → `box-v1.1` and the pinned sha updated to the new value. See
  the deviation note below.

## Part B — S4 pending-settlement band

- `service/stops.py`: `S4Decision(kind, loss_pessimistic, loss_optimistic)`; `s4_pending_value(pending,
  utc_day)` (Σ $1 per unfinalized leg whose close_time is today; prior-day rows contribute 0 —
  commented as pre-existing safe-direction behaviour); `s4_balance_decision(start, now, pending_value,
  cap)` — latch iff `loss_optimistic >= cap`, clear iff `loss_pessimistic < cap`, else pending. Wraps
  the retained `s4_balance_breached`.
- `service/run_window.py`: `_settlement_backfill_sweep` now RETURNS the pending picture
  `{window: {"legs": [(ticker, side, count, result_or_None), …], "close_time": …}}` (journaling
  unchanged). The wake block computes `pending_value` and the decision, journals `s4_balance_check`
  with `pending_value_dollars` / `pending_legs` / `loss_pessimistic` / `loss_optimistic` / `decision`;
  `latch` → existing latch path (reason now quotes both losses); `pending` → journal
  `s4_pending_settlement` + degrade THIS window only (no latch, no day-guard write); `clear` → arm.
  Worked check (test): start 54.4745 / now 50.9292 / pending 1.00 → pessimistic 3.5453, optimistic
  2.5453 → pending; pending 0 → latch; now 51.9292 pending 0 → clear; now 49.50 pending 1.00 →
  optimistic 3.9745 → latch.
- B4 (LOW): `_day_totals` recomputed AFTER the sweep so `ledger_vs_balance_delta` reflects a
  just-backfilled credit (test asserts −0.82 + 1.00 = +0.18 in `ledger_realized_today`).

## New tests (33)

- `test_stops.py` (8): `s4_pending_value` day-scoping / count>1 / prior-day-zero; `s4_balance_decision`
  four documented cases + boundary equality.
- `test_box.py` (11): BOX_SKIP below-floor / no-fire-after-skip; FIRE at equality and above;
  skip→rescan-once; no-rescan-if-nothing-qualifies; rescan needs window/freshness; shakedown skip;
  loader new-sha + min_implied_pin; old-v1-sha refused; missing-min_implied_pin fails closed.
- `test_box_wiring.py` (10): armed skip → `box_skip_implied` + zero orders + no ledger row + summary
  flags; driver skip→rescan journaling; sweep returns the pending picture; S4 pending stands down w/o
  latch; S4 latch when no pending credit; `_day_totals` recomputed post-sweep; box_report shas mirror
  service.box; amended falsifier still arms.
- `test_box_report.py` (6): roster partition (legacy excluded from gates, present in legacy line);
  SO-1 across both rosters with paper skips (n_real/n_paper); paper skip unsettled w/o backfill;
  paper rescan group; render tags + legacy line; jsonify of paper skips/rescans.

Plus updated fixtures (behaviour changed, not masked): `_fire_below` / `_drive` / `_event_list` and
three decide_box tests now use floor-qualifying quotes (the observed asks/limits/costs are UNCHANGED
— only the mids were nudged up to clear 0.80, verified equal by computation); the golden decide_box
snapshot test now asserts FIRE-or-BOX_SKIP by the winning box's `implied_pin`; the loader-self-verify
test asserts the new roster/sha/floor. Added `_skip_below` helper (implied 0.76) for the skip path.

## Deliberate deviations / things I was unsure of

1. **`ops/BOX_ARMING.md` edited.** The blanket rule says "don't touch pilot/ops", but A7 explicitly
   instructs updating the roster name/sha there. BOX_ARMING.md is a static runbook doc (not runtime
   state the live pilot reads) and this is the worktree copy only, so the live main-tree run is
   unaffected. I judged the specific A7 instruction to govern the doc; I did NOT touch mode.txt /
   strategy.txt / stops_*.json / journals / reports / ledger.
2. **Journaling of the new kinds lives in `box_runner.py`, not `run_window.py`.** A5 said "run_window
   handles BOX_SKIP → journal box_skip_implied", but in the existing architecture the driver
   (`BoxSignalDriver` in box_runner) is where box actions are journaled (box_fire/box_would_fire).
   Per the "follow existing code conventions" tie-break I added the mapping there; run_window carries
   the summary flags and the order-free guarantee.
3. **Paper-skip settlement scoring.** A6(ii) says paper skips are "scored on settlement … the same
   convention the existing would-fire paper scoring uses" — but the existing code has NO would-fire
   paper scoring (would-fires get realized=None). The box report is pure over journals+ledger with no
   settlement source for markets we did not trade, and an un-traded skip produces no settlement
   backfill. I implemented `build_paper_window` to score via a backfill row keyed on close_time when
   one exists (`payoff − C_decision`, the fire convention), else read `unsettled` (counted in
   `n_paper` / `unsettled`, 0 to settled PnL). In practice forward paper skips read unsettled; their
   value is the count until/unless settlement data exists. Flagged per the brief's own hedge.
4. **SKIPPED-real scoping.** The brief scopes skipped-real to "v1 two-leg fills with implied < 0.80".
   I scoped it to ALL two-leg fills < 0.80 (any roster) so the existing shadow tests stay meaningful;
   under v1.1 no such fill exists (they skip), so this is functionally identical going forward and
   only ever picks up box-v1 legacy fills. Noted.
5. **`min_implied_pin` missing fails closed as `KeyError`** (a raw required-key read), consistent with
   the other required roster keys in `load_box_policy` (which use `raw["…"]`). I did not wrap it in a
   custom exception since the sibling keys don't.

## Confirmations

- No Kalshi/proxy calls; no reads of `.env`/`*.pem`/`sim/out/sealed_eval`. Tests use injected fakes.
- contracts=1, pair_cost_max $1.99, S4 cap $3.00, and every other falsifier pin unchanged.
- Main working tree never edited/run/checked out. Worktree only.
