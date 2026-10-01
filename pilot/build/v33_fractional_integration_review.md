# V3.3 fractional + async-writer integration — adversarial review (Opus 4.8, Fable-directed)

Reviewed `fix/v33-fill-attribution` @ `4b443ee` (HEAD = merge `e0f8a7e` of `8e759de` [PR #106
fill-attribution, D1–D5] + `da9859c` [PR #105 async writer], then `86759a8` async port, `7b0073c`
tests, `4b443ee` report). Worktree `dv3_wt_review`, detached. This review is the INTEGRATION only (the
merge resolution + the async-twin port); the two feature branches were APPROVED on their own
(`v33_async_writer_review.md`, `v33_fill_attribution_review.md`). Fixes + regression test on
`review/v33-fractional-integration` from `4b443ee`. Suite before my change: **1432 passed / 5 skipped**;
after: **1433 passed / 5 skipped** (`cd pilot; python -m pytest -q`).

## VERDICT: REQUEST CHANGES — one MEDIUM money-correctness bug on the DORMANT async path (fixed on this branch); everything else APPROVED

The merge is clean (both parents' behaviour survives in every conflict file + the auto-merged
executor), the async-twin port is faithful (every Decimal/count line matches its sync source of truth),
netting and dormancy hold on the async path, and the full suite is green. BUT reviewing "as if money
rides on it and the async path is the re-arm candidate," the port reintroduced the **A1 shared-field
concurrency class** on a NEW field (`_last_confirm_status_fp`) that did not exist at the async-writer
A1 audit: concurrently-dispatched cancels can cross-contaminate a fractional cancel-confirm fill,
booking a **phantom fractional fill** on an order that never filled. **Fixed on this branch** (`F1`)
with a teeth-proven regression test. It touches ONLY the dormant async path (sync/live is byte-identical
and untouched), so it does not threaten the current live DRY/sync build — but it must be folded in
before the async path is armed, exactly as A1/A2/R2-F1 were.

---

## Findings

### F1 — [BUG, fixed on this branch] async concurrent cancels cross-contaminate the fractional cancel-confirm fill
`service/v33/async_executor.py` `_confirm_cancel_filled_async` / `_resolve_cancel_success_async`.

The fractional cancel resolution computes `filled_fp = max(fp_delete, self._last_confirm_status_fp)`
(L421). `_last_confirm_status_fp` is a **shared instance field** written inside
`_confirm_cancel_filled_async` *across its off-loop status-GET awaits* (L469) and read by the resolver
*after* the confirm returns. On the sync path this is safe (one cancel at a time). On the ASYNC path a
cancel-all dispatches N cancel coroutines **concurrently** (`run_v33._dispatch_async` →
`asyncio.as_completed`), and two confirm loops interleave across the `order_status_async`
`run_in_executor` await, both writing that one field. A resolver whose own final status poll is
unreadable (loop-exhaust, no re-establish) then reads a **sibling cancel's** fp.

This is structurally identical to the async-writer review's A1 (`_pending_place_price` read across an
await by two coroutines). The A1 round-2 instance-field audit could not have covered it —
`_last_confirm_status_fp` was introduced by PR #106 and ported here, so it is new surface.

Impact (armed async path): order A truly fills 0 (`reduced_by` = full count → `fp_delete = 0`) while a
concurrent cancel B surfaces 0.44; A reads B's 0.44 and resolves `OrderCancelled(filled_before_cancel =
0.44)`. The core books a **phantom 0.44 RungFill** on A → takes real-money wings to hedge a position A
never held, and the window's held-leg/settlement accounting is wrong. The batched poll backstop only
ADDS newly-discovered fills (positive delta); it never UN-books an over-booking, so this does not
self-correct.

Reproduced deterministically (A entered confirm, B wrote the shared field 0.44 during A's unreadable
poll): pre-fix `A.filled_before_cancel = Decimal("0.44")` (phantom); post-fix `= Decimal(0)` (A's own
truth), `B = 0.44` (correct).

**Fix (applied, surgical, async-only — sync byte-identical):** `_confirm_cancel_filled_async` now
RETURNS `(filled, status_fp)` (a per-call local); `_resolve_cancel_success_async` reads `status_fp` from
the return instead of the shared field. The field is still written (sync-twin parity) but no longer
read on the async path. Only two async methods change; the sync `_confirm_cancel_filled` /
`_resolve_cancel_success` and every other module are untouched. Test:
`test_v33_async_fractional.py::test_async_concurrent_cancels_do_not_cross_contaminate_status_fp`
(fails pre-fix with `AssertionError: Decimal('0.44') == Decimal('0')`, passes post-fix).

### N1 — [informational, NOT a regression, identical on both paths] a fractional roll remainder is dropped (not re-rested) at the cancel→create fallback
`service/v33/core.py:1000` `replace_count = int(mover.count)` (the amend-fallback roll re-place). Per
the task's fractional-remainder question I traced both roll int() sites:
- **Amend-first roll** (`_emit_roll`, `core.py:2196` `count=int(order.count)`): **unreachable with a
  fractional count.** The convergence `OUT` set excludes any order with `rest_booked_by_coid > 0`
  (`core.py:2328`), so a partially-filled rung is never a roll mover; `order.count` is always a whole
  placed weight. SAFE.
- **Cancel→create fallback** (`core.py:999-1013`): IS reachable with a fractional remainder — if the
  roll's cancel-confirm surfaces a partial fill (booked at `core.py:985-989`, reducing `mover.count` to
  e.g. 0.56), then `replace_count = int(0.56) = 0` → `still_resting = False` → the 0.56 remainder is
  dropped, not re-placed.

This is **not a port asymmetry** (core is shared by both sync and async) and **not a money/safety bug**:
the filled 0.44 is booked and hedged, the 0.56 was already cancelled off the venue, and the executor's
place body (`_rest_body`, `v32/executor.py:515`) is `int(action.count)` — a fractional rest cannot be
placed anywhere end-to-end. Dropping it is the fail-closed choice (never over-exposed), consistent with
whole-lot resting. Flagging only so the sub-1-lot under-placement is a known, documented behaviour; if
whole-lot resting is ever relaxed to fractional rests, these int() sites and `_rest_body`/`_amend_body`
must change together on the sync base first (as the build report's F1 reviewer note already states).

### N2 — [confirmed by design, no action] place/amend stay int on both paths
The async `_place_rest_async`/`_amend_rest_async` match the sync base (`int(action.count)`,
`int(parsed.fill_count)`), which #106 left int. Verified the sync base is genuinely int post-#106
(`v32/executor.py:457,515,965,979,1024`) and that PLACE_REST/AMEND_REST actions always carry whole
counts (N1). Twin-fidelity, not a miss.

---

## Checklist results

1. **Merge fidelity** — PASS. `git diff 8e759de 4b443ee` vs `git diff da9859c 4b443ee`, hunk by hunk:
   - **falsifier** (append-only): both diffs are pure insertions (no deletions); nothing above
     `## Registration` moved; all three entries present and ordered incident → MECHANICS CLARIFICATION
     (#105) → CORRECTION (#106), matching the build report.
   - **ledger.py**: vs #106 adds ONLY `writer_stats` (#105); vs #105 adds all of #106's D3/D5 with
     `writer_stats` as context — both sides survive; the R2-F1 netted-credit backfill fix is present
     (`v33_settlement_backfill_sweep`).
   - **report.py**: vs #106 adds ONLY `_render_writer` + the `writer` field (#105), with
     `bucket_mismatch`/`netted_sets`/`netted_realised` (#106) as context — both survive.
   - **run_v33.py**: the ledger-row call carries BOTH `netted_sets=` and `writer_stats=` (L850-851);
     `_pump` routes to `_pump_async` under `self._async`.
   - **executor.py** (auto-merge): vs #106 shows ONLY #105's `acquire_async` (A2 fix) + `_invariant_verdict`
     (A1 fix); vs #105 shows all #106 D3 — both survive, non-overlapping.
2. **Async-twin port** — PASS (with F1). Normalized against the sync originals, `_take_wings_send`,
   `_take_bucket_no_async`, `_unwind_wings_async`, `poll_orders_for_bucket_async`,
   `_resolve_cancel_success_async`, `_confirm_cancel_filled_async` differ only by mechanical
   substitutions / docstrings / the retry-belt wrapper split; **every Decimal/count/chunk-sum line is
   identical** to its sync twin. Fractional-remainder analysis in N1. The one real divergence hazard was
   F1 (fixed).
3. **`filled_fp = max(placed − reduced_by, status_fp)`** — PASS (post-F1). `reduced_by` absent →
   `fp_delete=0` → status fp wins; status fp absent → `_last_confirm_status_fp` reset to `Decimal(0)` at
   confirm entry (and base `__init__`, `v32/executor.py:358`) → `fp_delete` wins; both present/disagree →
   `max`; cancel 404 → `_cancel_nonok_async` → inherited `_resolve_cancel_from_status` (`filled_fp =
   st.filled_count_fp`). The stash is set before read on every path; the concurrency hole (sibling read)
   is F1, fixed by threading the fp through the return.
4. **End-to-end on the async path (fake proxy)** — PASS. The async fractional tests drive `FracAsyncProxy`
   through `AsyncOrderWriter.run_in_executor` (NOT injected events): cancel-confirm (2xx DELETE
   `reduced_by 0.56` + status GET fp 0.44) → `OrderCancelled(0.44)`; a 1.44 fill hedged 1.44 per wing;
   multi-chunk `[2.00, 1.44]` (Σ=3.44); `poll_orders_for_bucket_async` → `{oid: Decimal("0.44")}`. The
   core half (OrderCancelled → RungFill → wings on the FILL's bucket) is shared with the sync path and
   covered by the fill-attribution r2 core tests.
5. **Netting on the async path** — PASS. `WING_NETTED` is NOT in `_ORDER_ACTION_KINDS` (run_v33 L148),
   so the async `order_actions` filter (L528) never dispatches it; it is journaled (`wing_netted`) but
   not sent on BOTH paths (sync L500/L505 explicit guard). The R2-F1 netted-$1 backfill fix lives in
   `ledger.py` and runs on ledger rows (`netted_sets` written at run_v33 L850 from
   `compute_ladder_money_math`), path-independent — the async path produces identical `netted_pairs` on
   state (decide_v33 is shared), so the credit survives the backfill identically.
6. **Dormancy** — PASS. `async_writer_enabled` default OFF (`DV3_V33_ASYNC_WRITER` / `--async-writer`;
   not a params field); `_pump` runs the sync `V33LiveExecutor` unless `self._async`; no golden fixture
   changed (only the new `incident_20260930T220000Z_slice.jsonl` fixture added by #106); params sha
   `295590ce…f532def` untouched (`params.py:82`); STATUS line and everything above `## Registration`
   untouched vs both parents.
7. **Full suite** — PASS. 1432 passed / 5 skipped (reproduces the builder) → 1433 / 5 after the F1
   regression test. The 5 skips are the pre-existing worktree fixture-absence branches.

## House law
`python` only; no `.env`/`*.pem`; no `sim/out/sealed_eval/**`; SEAL (2026-08-02..18) untouched; no
network/proxy (the only "proxy" is the in-process `FracAsyncProxy` fake); no process kills; params
JSON/sha, falsifier STATUS and everything above `## Registration` untouched. The only code change is
`service/v33/async_executor.py` (the dormant async twin) + its test — NOT the live-shared V2 executor,
NOT core/ledger/report/run_v33, NOT sync `V33LiveExecutor`. Worktree left detached + clean. Pushed to
`review/v33-fractional-integration`.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SK9t6jbZD2ZdDBeq6Lqno6
