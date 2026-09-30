# V3.3 async order writer — adversarial review (Opus 4.8, 2026-09-30)

Branch under review: `feat/v33-async-writer` @ `32d9350` (9 commits on `dcf2af2`).
Fixes + regression tests on: `review/v33-async-writer` (from `32d9350`).
Full suite before fixes: **1401 passed, 5 skipped** (this worktree env). After fixes: **1404 passed, 5
skipped** (+3 review regressions). The 5 skips are pre-existing worktree fixture-absence branches (the live
tree reports 1 skip), unrelated to this change.

## VERDICT: REQUEST CHANGES

The design is sound and the twin discipline is faithful, but the feature branch as submitted carries **one
HIGH-severity concurrency correctness bug** (A1) that would kill a live armed window and can mask the exact
duplicate-rung incident class the pre-place invariant exists to catch, plus **one MEDIUM** priority
inversion (A2) that partially defeats the wing/cancel priority the change is built to protect. Both are
**fixed on `review/v33-async-writer`** with regression tests; fold those in (or an equivalent) before the
async path is shaken down / armed. Everything else — loop-confinement, the sync/async twin diffs, the 429
belt, cancel-all sharding + exchange_index, the `_SyncGuardWriter`, dry byte-identity, the ledger/report
telemetry, the Registration entry, and the proxy-throttle proposal — checks out.

---

## Findings

### A1 (HIGH) — concurrent PLACE_REST coroutines share `_pending_place_price` across the GET await
`service/v33/async_executor.py` `on_action_async`/`_place_rest_async`/`_place_one_chunk_async` →
`service/v33/executor.py:685` `_invariant_verdict`.

The K-aware pre-place dup check reads the **instance field** `self._pending_place_price`.
`on_action_async` sets it synchronously per action, but the value is *read* only after
`_place_rest_async` awaits the pacer and then `_pre_place_invariant_async` awaits the open-orders GET. Two
PLACE_REST coroutines dispatched concurrently by `V33Driver._dispatch_async` (`as_completed`) interleave
across that GET await: the second coroutine's `self._pending_place_price = action.price` **clobbers** the
first's before the first reads it, so a place runs its dup check against **another rung's price**.

Reproduced deterministically (a legit new rung at 0.45 dispatched with a concurrent place at a
genuinely-resting 0.30): pre-fix the 0.45 place was **falsely flagged a dup → `OrderCancelled` + the whole
hour latched `stand_down_reason=rest_invariant_violation`**, and the legit rung was dropped. The mirror
case (a real dup read as a non-resting price) would **miss a true duplicate rung** — the 21-order incident
class the invariant guards.

The builder's "no cross-thread races" reasoning covers *thread* races; this is a *coroutine-interleaving*
race on the loop thread (shared mutable state read/written across an `await` by two coroutines), which
loop-confinement does **not** cover. The K-rests property test does not catch it (its concurrent extras all
share one price and are caught by overflow, not dup).

**Fix (applied):** thread the place price **explicitly** — `_invariant_verdict(resting, place_price)` (new
optional arg, sync path falls back to the instance field, byte-identical) and
`_pre_place_invariant_async(coid, now, place_price=action.price)` from both async place callers. Test:
`test_async_concurrent_places_use_own_price_not_shared_field`.

### A2 (MEDIUM) — pacer priority inversion: a priority write blocks behind a non-priority pacing sleep
`service/v33/executor.py:202` `WriteTokenBucket.acquire_async`.

The async pacer held `_alock` across `await asyncio.sleep(wait)`. A non-priority create/amend that must
pace (up to ~0.4–0.6 s at the pinned `rate=100, reserve=30`) holds the single shared lock across its sleep,
so a **priority** `acquire_async(..., priority=True)` — a wing IOC take or a T-5 cancel-all, the
safety-critical hedge/flatten — **blocks for the full non-priority wait** even though priority writes are
supposed to be served immediately. The per-lane thread pools give HTTP-dispatch priority, but the executor
pacer is a separate serialization point that reintroduces the delay. Reproduced: a priority wing waited
**0.406 s** behind a drained-bucket create. This does **not** re-block the loop (it yields), so the primary
2026-09-30 fix holds — but it undercuts the build report's claim that "a wing IOC take never waits behind a
queued cancel/create," and wing latency is adverse-selection-relevant on a rung that already filled.

**Fix (applied):** hold `_alock` only for the token arithmetic; **reserve the cost atomically under the
lock (deduct-then-sleep)**, then `await asyncio.sleep` with the lock released. No double-spend (every token
deducted exactly once under the lock — integral identical to the serial sync path), progress guaranteed
(one bounded sleep, no re-check spin that a frozen/slow clock could livelock), and a priority write is
served immediately. The sync `acquire` is untouched (pinned byte-identical). Tests:
`test_pacer_priority_not_blocked_behind_nonpriority_sleep`, `test_pacer_async_no_double_spend_under_concurrency`.

### B1 (LOW / documented) — the venue-truth K-invariant cannot serialize concurrent first-time places
`_pre_place_invariant_async` reads venue truth (GET) then places; the read→place window is not atomic
across the await, so two concurrent first-time places could both see venue `< K` and both place. This is a
**belt**, not the gate: the **core** is the real K-limiter (it emits ≤ K place actions), so total resting
stays ≤ K in practice. No fix needed, but the invariant should not be relied on to *catch* a concurrency
overflow — only a core/venue disagreement that is already resting. Worth a one-line note in the build
report.

### B2 (LOW) — `_dispatch_async` drops an action's result events on exception
`service/run_v33.py` `_dispatch_async` counts + `continue`s on any `on_action_async` exception, discarding
its result events. If a place's create landed at the venue but the method then raised before returning the
`OrderAck`/`Fill`, that fill is momentarily unbooked. Backstop exists: the batched
`poll_orders_for_bucket_async` reconciles venue fills on the next tick, and the core re-emits on the next
decide, so it is recovered — but the swallow is broader than the sync path (which lets on_action raise).
Acceptable given the poll backstop; consider narrowing the except or journaling the lost events' kinds.

### B3 (INFO) — redundant `X-DV3-Token`
`AsyncOrderWriter._headers` sets `X-DV3-Token` from env, and `ProxyWriter._default_post/_default_delete`
merge it again over `_dv3_token_headers()`. Harmless (same value), noted for cleanup.

---

## Checklist results

1. **Loop confinement** — PASS. Only `writer.rest_*` runs in `run_in_executor`; all executor/core mutation
   is on the loop thread. No same-`rest_book[coid]` concurrent mutation (coids are unique per rung; per-slot
   `asyncio.Lock` serializes same-order ops). The one across-await shared-state race found is A1 (fixed).
2. **Event ordering under interleaving** — state is `@dataclass(frozen=True)`, replaced (not mutated) each
   `decide_v33`; the dispatch snapshot is the emit-tick state, matching sync semantics. Fill→wing, cancel↔
   fill (status-truth booking de-duped by `booked_rest_oids` on the loop thread), quote-end vs wing (separate
   lanes) all hold. Added interleave-style regression via A1's concurrent-dispatch test.
3. **Sync/async twin diff** — PASS. `_place_rest`, `_amend_rest`/`_amend_fallback`, `_cancel_rest`/
   `_cancel_nonok`/`_resolve_cancel_success`/`_confirm_cancel_filled`, `_take_wings`, `_pre_place_invariant`,
   `place_batch`/`_place_one_chunk` twins differ only by the three mechanical substitutions (+ the wing belt,
   which is intentional). Amend/cancel-nonok/confirm-poll trees match.
4. **Pacer** — `acquire_async` no-overspend holds; A2 (priority inversion) fixed; wing 429 retries on the
   wing lane (`_apost` same-lane retry); no busy-wait.
5. **Wing retry belt** — one in-flight IOC per `(batch, side)` leg; claim is check-and-add with no await
   (atomic on the loop); released in `finally` on every path incl. exceptions. Composes with the CORE floor:
   the belt releases on each round-trip return, so no leg is held un-retried until cutoff. NOTE: branch
   `fix/v33-fill-attribution` is **not on origin** (could not fetch/read); composition assessed from the
   build report's merge-notes + the belt's release-on-return design — sound, no double-gating deadlock.
6. **Cancel-all** — concurrent per-order sharded DELETEs (no invented batch-cancel). Every DELETE carries
   `?exchange_index=` via `cancel_path` (09-14 incident). `reduced_by`/status-truth booking via inherited
   `_finish_cancel`; confirm polls run off-loop; a cancel returning "filled" routes the SAME booking path as
   sync. PASS.
7. **`_SyncGuardWriter`** — wraps `rest_post`/`rest_delete`/`rest_get` (the only HTTP verbs) and raises; all
   async-reachable paths use `self._aw`; the startup sweep (`cancel_stale_open_orders`) runs pre-loop on the
   raw ProxyWriter. PASS.
8. **Dry byte-identical** — `async_writer=None` → `_async=False` → the `_pump` guard is skipped; golden dry
   suite green; `test_dry_driver_is_not_async_and_unchanged` passes. PASS.
9. **`feed_gap_max_s` / `writer_stats` / report** — feed-gap tracked only while quoting; ledger row field
   present; ran `service.v33.report` read-only on a scratchpad COPY of the live ledger (167 rows, no writer
   rows → section silent, no crash) and confirmed the OFF-LOOP WRITER section renders correctly on synthetic
   telemetry (sync-vs-async worst-gap summary). PASS.
10. **Registration entry** — appended under `## Registration` (line 235), above the shadow-observations
    marker, pure insertion (zero deletions) → STATUS `FROZEN`, all `[pin]`s, and params sha
    `295590…f532def` untouched. Wording = MECHANICS CLARIFICATION, no threshold moved, all mirrored modules
    named, and it states the shakedown-before-arm requirement (caveat c). PASS.
11. **`ops/proxy_throttle.md`** — tier model (RATE=100, SIZE=100, create/amend=10, cancel=2, batch=10·N,
    GET=0) matches the `executor.py` pacer constants; PROPOSAL only, does not touch `degeneracy-proxy/`. PASS.
12. **Full suite** — 1404 passed, 5 skipped after fixes (no failures).
