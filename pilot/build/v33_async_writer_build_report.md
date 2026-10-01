# V3.3 async order writer — build report (2026-09-30)

Branch `feat/v33-async-writer` off `origin/main` (`fcf18e8`). **Dormant by default**: selected only by
`--async-writer` / `DV3_V33_ASYNC_WRITER` (armed only). The synchronous `V33LiveExecutor` stays the live
path until Brad shakes down the async path (dry-adjacent) and flips it — the `--batch-create` / amend-first
dormant-flag discipline. No `[pin]`, threshold, params sha, mode file, task, or the falsifier STATUS line
is touched; `degeneracy-proxy/` is untouched (a separate PROPOSAL doc, `ops/proxy_throttle.md`).

## The measured cause (what we are fixing)

First armed V3.3 day, 13:00Z journal: `service.run_v33.V33Driver._pump` runs on the ONE asyncio loop, and
every order create / amend / cancel / wing-IOC / status-poll went out as a **synchronous** `requests.post/
delete` (plus the write pacer's `time.sleep`) **inside the loop**. While the executor wrote, the websocket
reader could not drain — the books froze while the eval clock (`_last_server_ts` + wall elapsed) kept
advancing — so after `freshness_max_age_s` (1.0 s) the wings read stale → a `stale_or_missing_wing` hold →
the 1.5 s hold expired during the same burst → cancel-all → more blocking. Receipts: 75/77 feed gaps ≥ 1 s
held one of our own writes; a cancel-all of 11 rungs ≈ 3.4 s of dead loop; re-place of 11 ≈ 1.5 s; ~15–35
full ladder tear-downs per window; ladder out of the book 51–109 s of ~610 s.

## The design (which of the brief's two options, and why)

The brief offered `asyncio.to_thread`/`run_in_executor` **or** a dedicated writer thread + `call_soon_
threadsafe`. **Chosen: `run_in_executor`**, because this codebase is deliberately single-threaded (`run_v32`
L49: "both clients run on the ONE asyncio loop … the shared state needs no lock"). With `run_in_executor`:

- **Only the raw blocking `requests` call runs on a worker thread.** All orchestration and ALL executor /
  core state mutation happen on the loop thread, between `await`s. So there are **no locks and no
  cross-thread races** — state stays loop-confined. A dedicated writer thread touching executor state would
  have reintroduced locking into a codebase that has none.
- **Every executor pause becomes `await asyncio.sleep`** (cancel-confirm poll, cancel backoff, invariant
  recheck, write pacing) — these YIELD the loop instead of blocking it. This is why the async executor is a
  near-verbatim twin of the sync one (below): the confirm/retry protocols keep their exact logic; only the
  round trips and sleeps changed.

### Threading model (two sentences)

The event loop runs `decide_v33` and every executor-state mutation synchronously (decide never awaits, so
state is serialized on the single loop thread with no locks); the only thing off the loop is the raw
blocking `requests` POST/DELETE/GET, run on per-class `ThreadPoolExecutor`s via `loop.run_in_executor` and
`await`-ed, with results delivered back to the loop as ordinary coroutine returns that re-enter `decide_v33`.
Priority is by construction — wing / cancel / normal each draw from their own pool, so a wing never waits
behind a cancel-all and a cancel never waits behind a create; per-slot FIFO is a per-slot `asyncio.Lock`;
rolls/creates of several levels are dispatched concurrently with `asyncio.as_completed`.

## File-by-file

- **`service/v33/async_writer.py` (new) — `AsyncOrderWriter`.** The off-loop HTTP primitive. `post` /
  `delete` / `get` mirror `ProxyWriter` exactly (POST/DELETE → `WriteResponse`; GET → parsed dict). Each
  call runs `writer.rest_*` on its **lane's** pool (`wing` / `cancel` / `normal`) via `run_in_executor`.
  `X-DV3-Class` header on every write (localhost hop). Per-slot `asyncio.Lock` serializes same-slot writes.
  `WriterStats` (submitted / completed / max_inflight / latency p50·p99·max / class mix). `close()` shuts the
  pools at window end.
- **`service/v33/async_executor.py` (new) — `V33AsyncExecutor(V33LiveExecutor)`.** Each `*_async` method is
  the **line-for-line async twin** of a sync method (named in its docstring), with exactly three mechanical
  substitutions: `writer.rest_post/delete/get` → `await _apost/_adelete/_aget`; `self.sleep` / `pacer.
  acquire` → `await asyncio.sleep` / `await pacer.acquire_async`. Every PURE helper (body builders, parsers,
  `_finish_cancel`, `_resolve_cancel_from_status`, `_reject_place`, `_filter_phantoms`, `_invariant_verdict`,
  the K-aware invariant verdict, the unknown-outcome latch, weighted-average wing aggregation, `attribute`,
  `mark_filled`) is **inherited unchanged**. `self.writer` is a `_SyncGuardWriter` that RAISES on any sync
  `rest_*` — an un-async-ified path fails loud instead of silently blocking the loop. `_apost` carries the
  429 belt (retry once on the SAME lane). Also the **wing retry-storm belt** (one in-flight IOC per missing
  leg). `cancel_stale_open_orders_async` for completeness (the live startup sweep still uses the sync one —
  it runs before the loop, where blocking is harmless).
- **`service/v33/executor.py` — `WriteTokenBucket.acquire_async` (added).** The async twin of `acquire`;
  identical token accounting, but the wait is `await asyncio.sleep`, serialized by an `asyncio.Lock` so
  concurrent creates never overspend the bucket across the await. The existing `acquire` is byte-identical.
- **`service/proxy_writer.py` — optional per-call `headers`.** `rest_post`/`rest_delete` take an optional
  `headers` merged over the DV3 token header **on the default requests path only**; the injected-callable
  contract (`(url, body, timeout)` / `(url, timeout)`) is unchanged, so the two injector tests stay green.
- **`service/run_v33.py` — wiring (async path gated).** `async_writer_enabled(--async-writer |
  DV3_V33_ASYNC_WRITER)`; `build_executor_v33` builds `V33AsyncExecutor` iff an `AsyncOrderWriter` is
  passed; `V33Driver._pump_async` / `_dispatch_async` / `_ingest_async` (decide synchronously, dispatch
  order-actions concurrently off the loop, re-enter decide on completion); `feed_gap_max_s` tracking; the
  order-status poll awaits the async batched poll when async; the writer is created before the loop, bound on
  first `run_in_executor`, and `close()`d at window end.
- **`service/v33/ledger.py` — `writer_stats` row field.** Carries `feed_gap_max_s` (the direct proof), the
  writer summary, `async_errors`, `async_rate_limited`, `wing_retries_dropped`.
- **`service/v33/report.py` — OFF-LOOP WRITER section.** Per-window `feed_gap_max_s` + writer latency, and a
  worst-feed-gap sync-vs-async summary (the async column near the WS cadence = loop-blocking gone).

## Invariants and how each is enforced

- **Loop never blocks on a round trip** — every HTTP is `await run_in_executor`; every pace/poll/backoff is
  `await asyncio.sleep`. Proven: `test_loop_not_blocked_during_slow_write`,
  `test_async_driver_loop_free_while_creates_gated`, `test_async_driver_loop_not_blocked_and_placements_
  concurrent` (feed_gap < 50 ms, ladder < 110 ms for 11×12 ms creates).
- **Concurrency across distinct orders** — `_dispatch_async` gathers the tick's order-actions;
  `test_distinct_slots_run_concurrently`, the 11-create timing test.
- **Wing priority lane** — dedicated wing pool; a wing never waits behind a cancel-all:
  `test_wing_never_waits_behind_a_cancel_all`.
- **Per-slot FIFO** — per-slot `asyncio.Lock`; `test_per_slot_cancel_then_create_serialized`. NOTE: the
  cross-coid "cancel-then-create of the SAME rung" ordering is guaranteed by the CORE (the create is emitted
  only in response to the cancel's `OrderCancelled` event); the writer's slot lock is the belt that
  serializes operations on the same order_id.
- **Never more than K rests at the venue** — the inherited K-aware pre-place invariant reads venue truth;
  property test `test_property_never_more_than_k_rests_under_random_latencies` (12 random-latency seeds).
- **Unknown outcome never re-placed over** — inherited `_reject_place(unknown=True)` latch;
  `test_async_unknown_outcome_never_replaced_over` (503 create → stand down, coid recorded, never re-sent).
- **429 on a wing retried first** — `_apost` retries once on the same (wing) lane;
  `test_async_429_on_wing_retried_on_wing_lane`.
- **Wing retry storm cannot starve the lanes** — one in-flight IOC per missing leg, duplicates dropped +
  counted; `test_async_wing_retry_storm_belt_one_inflight_per_leg`, `..._allows_next_after_completion`.
- **Dry unchanged** — `async_writer=None` → the byte-identical sync path; `test_dry_driver_is_not_async_and_
  unchanged` + the whole existing golden dry suite still green.

## What a reviewer must scrutinize

1. **Loop-confinement claim.** Confirm no `*_async` method mutates executor/core state ON a worker thread —
   only `writer.rest_*` runs in `run_in_executor`; everything else is on the loop between awaits. Check that
   no two coroutines mutate the SAME `rest_book[coid]` concurrently (per-slot FIFO + the fact that the core
   emits one operation per rung at a time).
2. **Event-ordering under interleaving.** In the sync pump, an executor's result events are decided in the
   same `_pump` call before the next external event; in async they re-enter via `_ingest_async` later, so an
   external event may interleave. The core tolerates this (it models `pending` / `rolls_in_flight` /
   `awaiting_replace`), but a reviewer should confirm no decision path assumed synchronous completion.
3. **The `*_async` twins vs their sync originals.** Diff each against its named sync twin; the only
   differences must be the three mechanical substitutions. Watch the amend / cancel-nonok / confirm-poll
   decision trees especially.
4. **429 belt + pacer interaction.** `_apost` retry-once and `acquire_async`'s lock — confirm a wing 429
   retries on the wing lane and the pacer can't be overspent across an await.
5. **The `_SyncGuardWriter`.** Confirm it covers every sync HTTP entry point the async path could reach
   (place, cancel, amend, wing, poll, invariant, stray-cancel). If any un-async-ified path exists, the guard
   turns it into a loud `AssertionError` rather than a silent loop block.
6. **feed_gap_max_s definition.** It is a WALL gap measured only while the ladder quotes (live/pending
   rungs); confirm that matches "largest gap between consecutive WS messages while quoting".

## Things I am unsure about / did not do

- **True Kalshi batch-cancel endpoint.** The brief asked for batch cancel; the codebase encodes only batch
  CREATE, not batch cancel, and inventing an unverified venue endpoint for a live account violates
  "receipts, not assurances". The cancel-all is therefore **concurrent per-order sharded DELETEs** (the
  proven path, parallelized across the cancel pool) — same latency benefit without a new endpoint. A real
  batch-cancel is noted in `ops/proxy_throttle.md`'s spirit as a future proxy/venue item.
- **`X-DV3-Class` upstream forwarding is UNVERIFIED.** The proxy source is Brad's (not read). The proxy
  builds/signs its own upstream request, so a localhost-hop header is not forwarded by construction — but I
  could not confirm the proxy does not echo arbitrary client headers. Stated as unverified in the header
  docstring and the proxy proposal. The `poll` (GET) class header is not applied (GETs go through `ProxyAuth`
  and are uncapped today); only writes carry the class header.
- **Shakedown before arming.** This path has NOT run against the live proxy (house law). It must be shaken
  down dry-adjacent (DV3_V33_ASYNC_WRITER on a dry/armed side-by-side) before Brad arms with it, exactly as
  amend-first and batch-create were.

## Merge notes (coordinator, 2026-09-30 22:00Z retry-storm finding)

- A separate branch `fix/v33-fill-attribution` (worktree `dv3_wt_amend`, **core.py / run_v33 only**) adds
  the CORE-side retry floor (`WING_RETRY_MIN_INTERVAL_MS = 250`, one-in-flight-per-missing-leg at the source)
  and owns the fill-attribution / count code. **This branch does not touch core.py attribution/count.**
- My change is the **transport belt** on the wing priority lane (`V33AsyncExecutor._take_wings_async`): one
  in-flight IOC per missing `(batch, side)` leg, drop duplicate retries while one is in flight (counted
  `wing_retries_dropped`, no journal record each), class header `X-DV3-Class: wing` kept on retries. The two
  are complementary (source floor + transport belt); they should merge cleanly (disjoint files except
  run_v33, where my additions are the async dispatch / writer-stats and do not touch core attribution).
- If both branches land, re-run `pilot: python -m pytest -q` on the merge; the belt is defense-in-depth and
  harmless if the core floor already prevents duplicate emission (the drop counter simply stays ~0).

## Test line

`cd pilot && python -m pytest -q` → **1405 passed, 1 skipped** (main was 1385 + 1; this branch adds 20 in
`tests/test_v33_async_writer.py`). The 1 skip is the pre-existing v33 golden-fixture guard (fixture present,
so it runs — the skip is the absent-fixture branch, unrelated to this work).
