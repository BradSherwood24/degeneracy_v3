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

---

## Round 2 (Opus 4.8, 2026-09-30) — VERDICT: APPROVE

Re-reviewed `feat/v33-async-writer` as merged (HEAD `1788bd5`, the round-1 fix fast-forwarded in;
live-tree suite 1408/1). Focused pass on the coordinator's four items.

### (1) A1 and A2 re-verified against the merged branch — hold
- A1 fix present: `_pre_place_invariant_async(..., place_price=action.price)` (async_executor L220) and
  `place_price=a.price` (L701); `_invariant_verdict(first|reread, place_price)` (L309/L321); the inherited
  `_invariant_verdict(resting, place_price=None)` falls back to the instance field only for the sync path.
- A2 fix present: `acquire_async` reserves the cost atomically under `_alock` then sleeps with the lock
  released (executor L228).
- **New adversarial THREE-way interleaving** (`test_async_three_concurrent_places_one_true_dup_two_legit`):
  a rung rests at 0.30; three places dispatched concurrently — 0.30 (true dup), 0.44 and 0.45 (legit). With
  the fix: both legit rungs go live on their own price, the 0.30 dup is caught (no OrderAck, not in
  `rest_book`, `stand_down_reason=rest_invariant_violation`). Proven to have teeth against a FAITHFUL
  pre-fix simulation (verdict reads `self._pending_place_price`): pre-fix the true dup **slips through**
  (`B_dup_placed=True, stand_down=None`) — the dangerous "miss a real duplicate rung" (21-order) mode — so
  the test fails pre-fix and passes post-fix.

### (2) Instance-field audit — every field the async executor reads across an `await`
Method calls excluded; only mutable DATA fields. Verdict per field:
- `_pending_place_price` — **SAFE (A1 fixed).** Async path passes the price explicitly; the remaining read
  (L302) is the sync fallback and is before any await. The L196/L700 writes are now inert for the verdict.
- `_last_confirmed_gone_oid` — **BENIGN.** Read at L303 and again at L315 across the recheck `asyncio.sleep`
  as the *exclude-oid* for the venue read; a concurrent cancel (L408) may change it between. It only
  chooses which single just-cancelled oid to skip; correctness rests on `_filter_phantoms`
  (`cancel_confirmed_ts`), not this hint. Pre-existing single-value behavior (sync overwrites it too), not
  introduced by async.
- `_consecutive_rejects` — **BENIGN.** Set=0 on a successful place (sync), incremented in the inherited
  `_reject_place` (pure sync, no await between read and write). A best-effort stand-down counter, not a
  safety invariant; a raced miscount cannot naked-a-rung.
- `stand_down_reason` — **BENIGN.** `if ... is None: ... = <reason>` is check-then-set with no await between;
  two coroutines could both latch (last write wins). Any reason latches the hour identically; the exact
  string is cosmetic.
- `_wing_inflight_legs` — **SAFE (by design).** Claim is check-and-add with no await between (atomic on the
  loop); released in `finally`. It is the belt that also guarantees one `_take_wings_send` per leg key.
- `_wing_filled` / `_wing_notional` — **SAFE.** `remaining` is read before the pacer/HTTP awaits and
  mutated after; the belt guarantees no second `_take_wings_send` for the same `(batch,side)` runs
  concurrently, so no same-key read-stale/double-count.
- `rest_book[coid]`, `_by_order_id[oid]` — **SAFE.** Written after the create await under a coid/oid unique
  to that rung; no two coroutines touch the same key.
- `booked_rest_oids`, `fills`, `wing_coids`, all `+= 1` counters (`cancels_*`, `rests_placed`, `amends_*`,
  `rest_invariant_*`, `pt_*`, `wing_*`, `async_rate_limited`, `wing_retries_dropped`, `fills_on_amend`, …) —
  **SAFE.** Each is a single read-modify-write statement (or set/list/dict op) with no await in between;
  atomic on the single loop thread. Interleaving reorders but never tears them; all are telemetry/dedup
  keyed by id, not decision-carrying-across-an-await.
- Config/immutable (`wing_cap`, `k_rungs`, `COID_PREFIX`, `batch_create_max`, `clock`, `journal`, `_pacer`,
  `_aw`, `writer`, `sleep`) — **SAFE.** Read-only, never mutated in-window.

Conclusion: `_pending_place_price` was the only field whose cross-await read was a correctness bug (fixed);
no other field exhibits the same read-after-await-of-another-coroutine's-write hazard.

### (3) B1 note — added in code
`_pre_place_invariant_async` docstring now states the belt-not-gate caveat (the venue-truth invariant cannot
serialize concurrent first-time places across the GET await; the CORE is the real ≤K limiter).

### (4) Full suite — 1405 passed, 5 skipped (this worktree env; +1 round-2 test). No failures.

### fix/v33-fill-attribution
Still **not on origin** (`git ls-remote` shows no such ref). Belt composition unchanged from round 1: the
transport belt frees each `(batch,side)` leg when its IOC round trip returns, so it composes with a
core-side re-emission floor with no un-retried-until-cutoff gap and no double-gating. Re-flag if/when that
branch is pushed.

**Round-2 verdict: APPROVE.** The two round-1 findings are fixed and merged; the three-way interleaving and
the full instance-field audit surface no further cross-await state race; B1 is documented in code. Standing
caveat (the builder's own): shake the async path down dry-adjacent before arming with it.
