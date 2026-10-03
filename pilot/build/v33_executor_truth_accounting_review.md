# Gate E review — executor-truth accounting (PR #123 `fix/v33-executor-truth-accounting`)

*Opus 4.8 adversarial review, 2026-10-03. Reviewer did not write the PR. Branch under review:
`fix/v33-executor-truth-accounting` (5 commits, head fd068f3). Fixes landed on `review/v33-executor-truth-accounting`.*

## Verdict: APPROVE WITH NITS — four DEFECTS found and FIXED on the review branch (with tests); two NITs recorded.

The core of the PR is sound: the reconcile engine (SUM increment sources / MAX cumulative sources / MAX across
sources), the alarm breakdown definition, and the 02:00Z rebuild are correct and verified against the real
journal. The four defects were all in the fail-safe edges and the rebuild/venue helpers — exactly where a
defence-in-depth gate must not be weakest. All fixed; suite green.

### Evidence that the headline is real
Ran `python tools/rebuild_v33_row.py <real 02:00Z journal>` and independently counted the journal:
- rebuild: `lots_filled 2, one_legged True, one_legged_contracts 2, exec_lots 2, hedged 0, alarms 10`
  (`by_name {rest_invariant_violation: 9, executor_standdown: 1}`, `executor_phantom 1`).
- journal truth (independent count): kind-`alarm` records = 10 (9 `rest_invariant_violation` + 1
  `executor_standdown`); `rest_invariant_violation` own-kind = 9 (each violation journals BOTH its own kind AND a
  kind-`alarm` copy via `_record_alarm`); `rest_invariant_phantom` own-kind = 1 (never routed through
  `_record_alarm`, so excluded from the headline on BOTH sides); `rest_fill` = 3 (#23 0.40+0.60, #27 1.00, all ws);
  `take_wings` = 0. The alarm DEFINITION agrees live-vs-rebuild (see finding 1 for the one exception, now fixed).

## Findings

### DEFECT 1 — `reconcile_failed` alarm written to the journal but counted by nothing (FIXED)
`run_v33.py` finally block (was ~1252). The except branch did `journal.append("alarm", {"alarm":
"reconcile_failed", ...})` but left `reconcile_alarms = 0` and bumped no driver/executor counter. That record is a
kind-`alarm` record, so a rebuild (which counts kind-`alarm` records) would see it while the LIVE
`alarms_breakdown.total` would not — live and rebuild disagree on the same event, the exact failure mode the gate
forbids. It also makes a reconcile failure invisible in the headline alarm count.
**Fix:** set `reconcile_alarms = 1` in the except so the `reconcile` bucket counts it (mutually exclusive with the
`ledger_reconcile_mismatch` success path, so no double count).

### DEFECT 2 — S1_LEGGED / the ledger row fall back to the BLIND core when reconcile raises (FIXED)
`run_v33.py` finally block. On a reconcile exception the code set `recon = None` and
`s1_one_legged = ... else driver.state.one_legged`. `driver.state.one_legged` is exactly what read **False** at
02:00Z with two naked lots — a mid-reconcile error silently reverts the pin to the pre-gate-E blindness. The task
pre-registered this: "the fallback must be the stricter of the two, never the core alone when reconcile fails
mid-way." With `recon = None` the ledger row also loses `one_legged_contracts`, so the falsifier gate table falls
back to the (02:00Z-blind) wing-batch sum.
**Fix:** new `reconcile_exec_truth_only(driver)` (reconcile.py) — executor truth vs completed-hedge coverage
ALONE, no core, no venue — is built in the except so the row still carries a conservative `one_legged_contracts`
and a naked fill is still surfaced. The S1 boolean is now the stricter
`driver.state.one_legged OR bool(driver._exec_truth_fills)` as a final belt if even that fallback raises. Tests:
`test_reconcile_exec_truth_only_surfaces_one_legged_without_core`.

### DEFECT 3 — the REBUILD sums poll records wrong: MAX over DELTAs under-counts lots (FIXED)
`reconcile.py` `rebuild_from_records` / `_REBUILD_SOURCE`. The poll `rest_fill` journal record carries a DELTA
(`run_v33.on_poll_fill` journals `count_out(delta)`), but the rebuild mapped `poll -> "poll"` which is a MAX
source. An order filled in two poll deltas (0.40 then 0.60) rebuilt to `max(0.40, 0.60) = 0.60`, not `1.00` — an
under-count, the direction that HIDES a naked lot in an audit, and a direct contradiction of the module's own
docstring ("the rebuild sums rest_fill counts regardless of path"). Tool-only (the LIVE poll path records the
CUMULATIVE with MAX, which is correct; the rebuild tool is not wired into any verdict or the daily job), but the
tool is what an operator would use to audit a window.
**Fix:** all rebuild `rest_fill` records share ONE sum bucket (`source = "journal"`), so ws increments and poll
deltas SUM (a lot is journaled by EITHER channel, never both, when the core books healthily; the only residual is
over-counting in the rare core-fail-both-channels case, the SAFE direction for the pin). The 02:00Z headline is
unchanged (all ws). Tests: `test_rebuild_sums_multiple_poll_deltas_on_one_order`,
`test_rebuild_mixed_ws_and_poll_increments_sum`.

### DEFECT 4 — venue-fills filter FAILS OPEN on an empty `_by_order_id` (FIXED)
`run_v33.py` `_fetch_venue_fills_v33`. The filter was `if oid is None or (known_oids and oid not in known_oids)`.
When `_by_order_id` is empty (we acked nothing this window) the `known_oids and ...` short-circuits falsy and the
filter is DISABLED — every venue fill is taken as ours. The account is shared across pilots (the code comment says
so), so this would ingest another pilot's fills into our executor truth -> false `unbooked` / `reconcile_mismatch`
/ `one_legged`, i.e. a possible FALSE KILL of a roster that owns nothing.
**Fix:** `if oid is None or oid not in known_oids` — always filter to our acked order ids; an empty map yields no
venue fills (we own nothing to reconcile; the ws/poll executor truth already carries anything of ours). Test:
`test_venue_fetch_fails_closed_on_empty_known_oids`.

### NIT A — the rebuild tool reports `lots 0` for DRY windows
Dry-sim fills are journaled under kind `dry_sim_fill`, not `rest_fill`, so `rebuild_from_records` (which reads only
`rest_fill`) returns `lots_filled 0` for a dry window that the dry ledger row shows with several sim lots
(confirmed: ran the tool on `20261003T180000Z.jsonl.gz`, 11 `dry_sim_fill`, rebuild read 0). This is arguably
correct for an *executor-truth* audit (dry sims are not real fills), but could mislead an operator into reading
"the ledger is wrong". The LIVE `reconcile_live` path is unaffected and correctly passes the core's dry lots
through (byte-identical row — verified by `test_reconcile_live_dry_passthrough_with_completed_batch`). Recommend a
one-line note in the tool/`rebuild_from_records` docstring and/or a `dry_sim` marker on the rebuilt row. Left as a
finding.

### NIT B — `one_legged_contracts` is an AGGREGATE subtraction; a print-through over-hedge could mask a naked lot
`reconcile.py`: `one_legged_contracts = max(0, lots_filled - hedged_lots)` where `hedged_lots = Σ leg_count` of
completed batches. For NORMAL batches `leg_count == total_count == the real rung fills`, so no excess. For a
PRINT-THROUGH batch `leg_count == taken_count` (pre-hedged), which can exceed the rung lots that actually filled;
that excess, summed into `hedged_lots`, could absorb a separate naked lot elsewhere in the aggregate subtraction
and read one-legged too LOW. This is latent: the print-through trigger is currently OFF (memory: `#94`, ticks>=2
needed), so every completed batch on the live roster is normal and the subtraction is exact. Recommend documenting
the aggregate assumption (or computing one-legged per order) before print-through is ever enabled. Left as a
finding.

## Items checked and found correct (no change)
- **Alarm routing (item 7):** every `_record_alarm` call bumps `counts['alarm']` AND journals kind `alarm`;
  `rest_invariant_violation` bumps its own counter + `counts['alarm']` (one alarm-kind record); `_driver_alarm`
  bumps `driver_alarm` + journals `alarm`; core `V33ActionKind.ALARM` bumps `counts['alarm']` via `_journal_action`;
  the shared `V32Recorder.record_alarm` bumps `counts['alarm']` + journals to the SAME window journal. No direct
  `journal.append("alarm", ...)` bypasses a counter except the two reconcile sites — both now counted (finding 1).
  Live `alarms_breakdown.total == journal kind-alarm count` by construction.
- **Reconcile aggregation (item 2):** ws echo of an amend cross -> once (MAX across sources); poll cumulative over
  ws partial -> once; venue overlap -> once; cancel-confirm cumulative -> once. ws intra-source dedup is handled
  upstream in `on_fill` (`_seen_trade_ids` / wing-echo) before the exec-truth append. Dry passthrough: empty
  executor truth -> `reconcile` passes the core through, no spurious mismatch (verified against the live code path
  and the dry test).
- **one_legged from reconciled truth (item 3), LIVE path:** a wing take sent/IOC-no-fill, a half-filled 2-leg take,
  and a `taken`-but-not-`completed` batch all leave their lots OUT of `hedged_lots` (completed-only) -> counted
  one-legged. The reconciled one_legged on the LIVE path cannot read LOWER than truth except via NIT B (print-through
  off) — see question 3 below.
- **S1 wiring (item 4):** exactly one `record_legged_occurrence` call site; the core never records S1 (the 02:00Z
  bug); the day guard counts OCCURRENCES (windows) and latches at 2; the KILL pin that PAUSES Test Fire #2 counts
  CONTRACTS via the falsifier gate table, now fed by `one_legged_contracts` (item 6).
- **Venue fetch (item 5):** armed-only; via `proxy.rest_get` (the signing proxy writer, never direct);
  `/portfolio/fills` path and the `order_id` / `count_fp`->`count` / `ticker` parse agree with the live-proven
  `async_executor._venue_wing_fills_async` (2026-10-02). Bounded blocking: `rest_get` is 4 attempts x 5 s +
  1+2+4 s backoff ~= 27 s worst case, well under the in-process hard stop (close+130 s) and the supervisor watchdog
  (close+grace 120 s) — cannot block the close past either. A failed GET / non-list body -> `venue_fills_unavailable`
  and the window still reconciles from ws/poll truth.
- **Report (item 6):** the falsifier gate table reads `one_legged_contracts` from the row when present and falls
  back to the wing-batch sum only for OLD rows (no double count: present field -> `continue`). 02:00Z-shape row
  (two contracts, zero wing_batch_sets) reads one-legged 2; `+1` -> KILL. Older rows render.

## Tests
Suite in this worktree (census CSV present): **1532 passed, 5 skipped** (baseline before fixes: 1528 / 5; +4 new
review tests). `tests/test_v33_reconcile.py`: 24 passed.
