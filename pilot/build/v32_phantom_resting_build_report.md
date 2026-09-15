# V3.2 build report — venue-truth invariant vs phantom resting orders (read-path lag)

Branch: `fix/phantom-resting` (off `origin/main` @ 9c048b3)
Scope: `pilot/service/v32/executor.py` (invariant path only), `ledger.py`, `run_v32.py`,
`service/v32/report.py`, `pilot/ops/V32_ARMING.md`, tests + one fixture. No policy JSON, no falsifier
edit, no live tree touched, no proxy write. Suite: **804 passed, 2 skipped** on the touched surface
(`python -m pytest pilot/tests -q`); five test files error at collection in this fresh detached
worktree because they read uncommitted data artifacts (`sim/out/census_train.csv`, `historical-data/**`)
— not this change, and green in the live tree that holds those artifacts.

## The incident (2026-09-15T18:00:00Z window, armed, no money at risk)

The pre-PLACE venue-truth invariant (`_pre_place_invariant`, added PR #46) GETs
`/portfolio/orders?status=resting` and refuses to PLACE while any of ours rests. At 18:00Z it fired a
**false positive** and stood the hour down on a *phantom* — an order the matching engine had already
removed but the LIST read still showed. Journal evidence (live tree
`pilot/journals_v32/20260915T180000Z.jsonl.gz`, read-only; sliced into the fixture below):

- `17:54:58.207` `cancel_rest` coid `…-184`, order `01a0a635-2d50-7c88-b5c7-39cbc43de655` (sharded DELETE)
- `17:54:58.886` `cancel_confirmed` `delete_status 200`, `reduced_by "1.00"`, `via delete` — **engine truth: -184 is off the book**
- `17:54:58.887 / .961` `place_rest` `…-185`; `17:54:59.028` `cancel_rest` -185; `17:54:59.698` `cancel_confirmed` 200
- `17:54:59.699` `place_rest` `…-186` intent
- `17:54:59.776` `rest_invariant_violation` `{"coid_attempted": "…-186", "resting": [{"client_order_id": "…-184", "order_id": "01a0a635-2d50-…"}]}` — **the LIST still showed -184, 0.89 s after its confirmed cancel**
- `17:54:59.821` `rest_invariant_cancel` `{"order_id": "01a0a635-2d50-…", "status": 404}` — the "stray" was **already gone**
- `17:54:59.821` `alarm` `executor_standdown` reason `rest_invariant_violation`; `17:54:59.822` `stand_down` "stood_down"
- `17:56:15.486` a spot-bucket print filled the shadow (E=0.10 lock **+10.0c**) while live stood down — **a missed set**.

The confirmed-gone order -184 was **not** the single `_last_confirmed_gone_oid` the invariant already
excludes (that had advanced to -185's order after its own confirm at 17:54:59.698), so the one-order
exclusion could not catch it. The measured phantom age (confirm → re-listing) was **0.890 s**.

## Root cause

Kalshi's read path (the orders LIST) lags the matching engine by up to ~1 s — the **same
eventual-consistency class** fixed in PR #50, where a T-5 status GET said "resting" for an order the
engine had already expired. A DELETE 2xx whose `reduced_by` covers the order's count is engine truth; a
later LIST read that still shows the order is a phantom, not a second live rest. The invariant trusted
the lagging read over the engine-truth confirm it had already recorded 0.89 s earlier.

## Changes (executor.py, invariant path only — minimal and localized)

New code constants (not in the sha-pinned `v32_params.json`, which is untouched):
`CANCEL_SETTLE_S = 5.0`, `INVARIANT_RECHECK_S = 0.5` (injectable via `sleep`).
New `RestRecord.cancel_confirmed_ts` — set in `_finish_cancel` (the single choke point every
confirmed-gone path routes through: 2xx-DELETE `_resolve_cancel_success` and status/expired
`_resolve_cancel_from_status`). RestBook already retains cancelled orders (F-1), so this is the only
new field.

`_pre_place_invariant` now resolves a raw LIST hit in three graded steps before it will declare a
violation:

1. **Phantom filter** (`_filter_phantoms`) — drop any listed order our RestBook shows cancel-CONFIRMED
   (status `cancelled`/`filled`) within `CANCEL_SETTLE_S`; journal `rest_invariant_phantom`
   `{order_id, coid, age_s, via:"book_confirm"}`. This is the exact incident fix: -184 is filtered, the
   list empties, PLACE proceeds. If nothing survives → proceed.
2. **Re-read once** — if an unknown survivor remains (not in our recently-cancelled book), `sleep`
   `INVARIANT_RECHECK_S` and re-GET the list once (`rest_invariant_rechecks++`); a survivor that cleared
   was lag → proceed. An unreadable re-read → proceed (never self-DoS on a transient read, matching the
   existing invariant philosophy).
3. **Confirm each survivor at cancel time** — a stray whose DELETE returns 404 with a terminal/not-found
   status GET (`_status_confirms_gone`, PR #50 status-truth), or a 2xx that pulled nothing
   (`reduced_by <= 0`), is a phantom (zero rests) → journal `rest_invariant_phantom`
   `{via:"cancel_confirm", delete_status}` and PLACE proceeds. Only a stray still **genuinely resting**
   (2xx `reduced_by > 0`, or status still `resting`) is a REAL violation → cancel, alarm, stand down
   (PR #46 behaviour preserved). `rest_invariant_violations` now counts REAL violations only.

Counters (`.get`-default-0, additive): `rest_invariant_phantoms`, `rest_invariant_rechecks` added to the
executor, threaded through `run_v32._compute_money_math` → `build_v32_ledger_row` (new kwargs, defaults
preserve the row shape) and surfaced in `report.py` totals + render (`rest_invariant: violations=…
phantoms=… (read-path lag, no stand-down)`), next to violations, wired exactly like PR #50/#54.

`pilot/ops/V32_ARMING.md` — MUST-CONFIRM item 7 added: invariant phantom handled without stand-down
(phantoms counter > 0 acceptable, rechecks normal; violations counter stays 0 in a clean window).

## Tests (`pilot/tests/test_v32_phantom_resting.py`, fakes only — no network/proxy/holdout)

- **(a)** `test_incident_phantom_confirmed_cancel_filtered_place_proceeds` — reproduces the -184/-185/-186
  chain; the LIST still shows -184 (age 1.9 s) at the -186 place → phantom filtered, PLACE acked,
  `phantoms == 1`, `violations == 0`, no alarm, no stand-down, no recheck sleep.
- **(b)** `test_unknown_stray_rechecked_then_genuine_stands_down` — unknown order persists through the
  re-read and cancels 2xx `reduced_by 1.00` (genuinely resting) → one `INVARIANT_RECHECK_S` sleep,
  `violations == 1`, cancelled shard-aware, alarm + stand-down, no PLACE (PR #46 preserved).
  Plus `test_unknown_stray_clears_on_reread_place_proceeds` (survivor gone on re-read → proceed).
- **(c)** `test_unknown_stray_cancel_404_status_gone_is_phantom_place_proceeds` and the
  `…status_not_found…` variant — 404 stray-cancel + terminal/not-found status → phantom, PLACE proceeds,
  no stand-down. Plus `test_stray_cancel_404_but_status_still_resting_is_a_violation` (404 + still
  resting → real violation).
- **(d)** `test_invariant_sleep_sequence` — a phantom-only pass sleeps `[]`; an unknown-survivor pass
  sleeps exactly `[INVARIANT_RECHECK_S]`.
- **(e)** `test_incident_fixture_slice_present_and_shows_phantom_signature` — the fixture
  `pilot/tests/fixtures/v32/incident_20260915T180000Z_phantom.jsonl.gz` (46 records, **3,499 bytes** <
  200 KB) carries the real incident signature: a `cancel_confirmed` order re-appearing in a later
  `rest_invariant_violation` resting list, the `rest_invariant_cancel` status **404**, and a
  confirm→re-listing age of **0.890 s** (0 < age < `CANCEL_SETTLE_S`).
- `test_confirmed_cancel_older_than_settle_is_not_a_phantom` — a confirmed order stale beyond
  `CANCEL_SETTLE_S` that is genuinely resting is NOT swallowed → recheck + real violation.

Existing invariant/ledger/report tests stay green (`test_v32_cancel_shard.py`, `test_v32_report_*`,
`test_v32_phase3_ledger.py`, `test_v32_quote_end_race.py`); `test_v32_falsifier_pins.py` untouched;
`v32_falsifier.md` and `v32_params.json` not edited.

## Note — relation to the amend-first replace

The amend-first replace (separate PR, `feat/amend-first-replace`) will reduce the number of
cancel/replace cycles and therefore the *rate* of phantoms, but it does **not remove the class**: any
resting-list read can still lag the engine after a cancel/amend. This invariant fix is the durable
guard; the two changes are independent and localized to keep that rebase easy.
