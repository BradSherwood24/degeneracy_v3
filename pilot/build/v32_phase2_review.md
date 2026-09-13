# V3.2 Phase 2 review — process spine, dry mode, task (`service/run_v32.py`)

Reviewer: Opus 4.8 (Fable's delegated reviewer). Branch `v32/phase2-spine`, PR #32, commit `e7de9f7`
on base `264672c`. Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`. Reviewed as an adversary
because Phase 3 will hold REAL resting orders + taker completions with Brad's money. No network, no
proxy dialed, no `.env`/`*.pem`/holdout/seal read. The live frames replayed are from a 2026-09-01
(non-holdout) journal.

## VERDICT: APPROVE WITH FIXES

The spine is well-built and faithful to the mirrors: two WS connections on one loop with independent
per-stream lag gauges, a memory-light `StreamJournal`, a server-ts-only clock, fail-closed discovery
+ stand-downs, and a `FrozenExecutor` that cycles the requote state machine without sending anything.
F-1 (the Phase-1 review's #1 pre-arm item) is genuinely closed: the `RestBook` retains
cancelled/replaced orders, a late fill on a retained coid is booked at the RETAINED price and
**takes the wings** (`book_late_rest_fill` -> `_wing_step`), and a coid not in the RestBook is dropped
+ journaled.

But the two live-frame rulings exposed a real defect: the trade parser was written for a cents-shaped
payload the exchange **does not send**, so every real trade would have been dropped and the shadow —
the entire point of a dry run — would never have filled. That is fixed here, the fill parser is added
and pinned, and `v32_mode.txt` is now git-ignored. Remaining items are Phase-3 arming gates, recorded
below.

Suite: `cd pilot && python -m pytest -q` -> **708 passed** (was 700; +8 `test_v32_live_frames`; the
mode-file test was rewritten in place, net 0 there).

---

## Rulings applied

### R1 — live-frame parsers pinned (four fixtures copied to `pilot/tests/fixtures/v32/live_frames/`)

* **H-1 (safety/measurement) — the trade parser dropped every real trade.**
  `run_v32.py:_trade_event` read `yes_price`/`no_price`/`price` **in cents** (`/100`). The real WS
  `trade` frame (`trade_frame.json`) carries DOLLAR strings `yes_price_dollars` ("0.5700") /
  `no_price_dollars` and the size in `count_fp` ("10.00") — none of the fields the parser looked for.
  Repro before the fix: `_trade_event(real_frame) -> None`, i.e. journaled `v32_trade_unparsed` and
  dropped. Since the shadow fills only on bucket `Trade` events, **the shadow would never fill and the
  dry-run statistic (the sim's edge measured on live data) would be dead** — a silent failure that
  passes every existing test (the two-connection integration test feeds only orderbook snapshots, no
  trades). Fixed: read `yes_price_dollars` (dollars, no `/100`), else `1 - no_price_dollars`, else the
  legacy cents fields as a last-resort fallback; size from `count_fp`. Pinned by
  `test_trade_parser_pinned_to_live_frame` (yes_price == 0.57, count == 10, server_ts from `ts_ms`).

* **H-2 (money) — no real fill parser; `on_fill` misread count and never converted NO-space price.**
  `on_fill` read `payload.get("count")` (live sends `count_fp`), so count always fell back to the
  placed size, and it never parsed the fill's price/fee at all. The real `fill` frame is the YES-space
  trap: a NO purchase reports `side: "yes"` with `yes_price_dollars: "0.0400"`, `purchased_side/
  outcome_side: "no"` — the NO-space price actually paid is `1 - 0.04 = 0.96`. Added `_fill_event`
  (converts off `purchased_side`/`outcome_side`, never `side`; carries `count_fp`, `order_id`,
  `client_order_id`, `is_taker`, `fee_cost`), wired `on_fill` to it (count from `count_fp`; a coid not
  in the RestBook -> foreign, dropped + journaled), and it now journals `exec_price`/`exec_fee` on both
  the tracked (`rest_fill`) and late (`late_fill`) paths for Phase-3 reconciliation. Pinned by
  `test_fill_parser_pinned_to_live_frame` + `test_on_fill_real_frame_{foreign,tracked}`.
  (The lock is still booked at the RESTING price `rec.price` — a `post_only` maker fills at its limit,
  the Phase-1 convention — with the executed price journaled beside it. See LOW note below.)

* Delta: the real `orderbook_delta` (`delta_frame.json`: `price_dollars`, `delta_fp`, `side`,
  `ts_ms`) already folds correctly through the proven `service.book.BookMirror` (the same recorder the
  live pilots use); pinned by `test_delta_folds_into_bookmirror` so a regression is caught.

### R2 — `v32_mode.txt` git-ignored (Brad's flips must never dirty the live tree)

`git rm --cached pilot/ops/v32_mode.txt`; added `pilot/ops/v32_mode.txt` to `.gitignore` (beside
`pilot/ops/mode.txt`, line 15); the on-disk `shakedown` copy is retained (now untracked). Verified the
fail-closed behavior the ruling asks for: `read_v32_mode_file` returns `""` on a missing file, which
`resolve_v32_mode` maps to `shakedown` (the no-orders rung). `V32_DRY_RUN.md` now has a prerequisite
step telling Brad to create it (or pass `--mode dry`). Test `test_shipped_mode_file_is_shakedown` was
replaced by `test_mode_file_is_git_ignored_and_absent_fails_closed` (asserts git-ignored + untracked +
absent-fails-closed), since a tracked assertion would fail on a fresh clone where the file is absent.

---

## Findings NOT changed in code (Phase-3 arming gates) — ranked

### HIGH — must be closed before Phase 3 arming

* **P3-1 — the `FrozenExecutor` also matches the REAL action kinds and would synth-fill live money.**
  `FrozenExecutor.on_action` branches on
  `k in (WOULD_PLACE_REST, PLACE_REST)`, `(WOULD_CANCEL_REST, CANCEL_REST)`, and
  `(WOULD_TAKE_WINGS, TAKE_WINGS, RETRY_WING)` — i.e. it synthesizes an ack/cancel/**fill** for a REAL
  order kind too. In Phase 2 this is unreachable (armed degrades to dry, `shakedown=True` is hardcoded
  at `run_v32.py:895`, so the core only ever emits `WOULD_*` twins), but in Phase 3 a real
  `TAKE_WINGS` routed to this object would be silently completed with fake `Fill`s (books a phantom $2
  pin, no order sent). The executor must be selected by `effective_mode` in **exactly one place**, and
  the real executor must not share this synth path. Recommend: make `FrozenExecutor.on_action` assert
  `action.kind` is a `WOULD_*` kind (raise on a real kind), so a mis-wire in Phase 3 fails loud
  instead of booking phantom money.

### MEDIUM

* **P3-2 — mixed bucket widths slip past the 21Z stand-down.** `observed_bucket_width` returns the
  MODAL width and `main` stands down only when that modal != `params.bucket_width`. A window that
  mixes $100 and $250 buckets keeps its modal at 100, so `build_bucket_map` leaves the 250-wide
  buckets in the map; `_select_spot` (highest YES mid, all buckets) can then pick a 250-wide bucket
  whose Su the core computes as `Sd + bucket_width (100)` — a strike INSIDE the bucket, so the "$2 at
  every settlement" pin structure breaks (it can pay $1 in the [Sd+100, Sd+250) sliver). Not observed
  today (Kalshi hours are uniformly 100 or uniformly 250/500), so not a Phase-2 blocker, but for real
  money Phase 3 should either DROP every bucket whose width != `params.bucket_width` from the map, or
  stand down if ANY bucket's width mismatches.

* **P3-3 — two server clocks funnel into one `server_now()`; near a cutoff it can regress -> cancel
  churn.** `V32Driver._stamp` overwrites `_last_server_ts` on every strike OR bucket frame, and
  `server_now()` (used by the ClockTick pump) = that last stamp + local elapsed. If the two
  connections' server clocks differ (or frames interleave out of order), a later-arriving frame from
  the slower clock makes `server_now()` step backwards; `core._fresh` treats a book with `age < 0` as
  stale (fail-closed) -> W None -> `CANCEL_REST` then re-place on the next in-order frame = churn that
  burns the order budget and can trip `A_REPLACE`. This is the Phase-1 review's L-2 carried into the
  two-connection topology. Recommend feeding a monotonic-max clock (`max(last_stamp, new_ts)`) or
  keeping the freshness clock per-stream. Fail-closed today (no bad order is sent), so MEDIUM.

### LOW / notes

* **P3-4 — tracked fills are booked at `rec.price`, not the frame's `exec_price`.** Correct for a
  `post_only` maker (fills at its resting limit), and `exec_price`/`exec_fee` are now journaled beside
  it — Phase 3 should ASSERT `exec_price == rec.price` (or reconcile the lock) so a divergence, which
  would mean our model of maker fills is wrong, is caught rather than silently mispriced.
* **P3-5 — the private `fill` channel is subscribed only when armed** (`include_private=False` in
  Phase 2), so no real fill can arrive yet; the F-1 path is exercised only with fakes. Phase 3 must
  turn it on AND drive cancels through a status-confirmed path so a partial-before-cancel arrives as
  `OrderCancelled.filled_count_before_cancel` and a `Fill` is fed only for a tracked-or-retained coid
  (both handled).

---

## Probe-by-probe (the task's 10 axes)

1. **Two connections / one loop.** A slow one cannot stall the other: each `_ConnRecorder` runs its
   own `run_recording` supervisor under `asyncio.gather`; callbacks are synchronous on the one loop so
   the shared `V32Recorder` needs no lock. A re-dial marks suspect **only that connection's tickers**
   (`_ConnRecorder.mark_all_suspect -> shared.mark_suspect(self._tickers)`), so a strike re-dial never
   souring bucket books, and vice versa. Snapshots after resubscribe are full replacements
   (`BookMirror.apply_snapshot` clears `suspect`). Seq-gap reconnect is per client (each
   `KalshiWebSocketClient` owns its own). OK.
2. **ClockTick clock.** `server_now = last server ts + local elapsed`, None before the first frame
   (fail-closed); across a re-dial it keeps advancing by wall time (forward). It CAN regress when two
   server clocks interleave -> P3-3. Fail-closed today.
3. **FrozenExecutor semantics.** Chosen in one place in Phase 2 (only a FrozenExecutor exists;
   `shakedown=True` hardcoded), and no synth ack/fill path exists outside it. But its `on_action`
   matches real kinds too -> P3-1 for Phase 3.
4. **RestBook (F-1).** Retains cancelled/replaced orders for the window; late fill booked at the
   retained price, latches the one-set rule, forces the filled bucket context, and **takes the wings**
   (`book_late_rest_fill` calls `_wing_step`) — R4's HIGH concern is satisfied. Foreign fill dropped +
   journaled. Idempotent (`rest_fill is None` guard). OK.
5. **Discovery.** All live generations kept; `exchange_index` fail-closed to None; dead generations
   dropped; half-populated buckets dropped; 21Z pure-$250 stood down by the modal-width check (mixed
   widths -> P3-2). Ticker parsing verified on the real shapes (`KXBTCD-...-T78899.99 -> 78900`;
   `KXBTC-...` classified as a bucket with precedence). OK.
6. **Timing.** Connect gate = `close - quote_start_s - CONNECT_MARGIN_S` (unit-tested); deadline =
   `close + GRACE_SECONDS` (10 s, confirmed); a late/`no-ladder`/`no-buckets`/`width-mismatch` start
   stands down and exits 0 with a summary line + ledger row. OK.
7. **Journals.** `StreamJournal` flushed + gzipped crash-safe in `main`'s `finally` (Ctrl+C caught);
   `_gzip_journal` never loses the raw file on a gzip error; record shape `{idx,kind,local_ts,obj}`
   matches the pilot readers (`test_journal_record_shapes`); raw frames streamed, no unbounded
   in-memory accumulation beyond the per-market books. OK.
8. **Ledger/report.** Exactly one row per window: a stand-down writes its row and returns before the
   run loop; a normal (or exception) path writes one row in `finally` via `_finalize`. Report handles
   zero rows (verified). Shadow locks carried per E. OK.
9. **PowerShell.** `register`/`unregister` ran with `-DryRun` on this box: exit 0, ASCII only, PS 5.1
   constructs, task name `DegeneracyV3_2`, log dir created at registration (not DryRun), `-Mode`-less
   action reads `v32_mode.txt` at run time. OK.
10. **Tests.** No network, no socket, no holdout/seal date (fixtures 2026-09-01/2026-09-04; test close
    2026-09-13 = today). The F-1 behavior and the frame parsers are now pinned. 708 passed.

## PHASE 3 MUST (carry-forward)

* **P3-1** Select the executor by `effective_mode` in exactly ONE place; make `FrozenExecutor.on_action`
  refuse a real (non-`WOULD_*`) action kind so a mis-wire fails loud, never synth-fills live money.
* **P3-2** Drop non-`params.bucket_width` buckets from the map (or stand down on ANY width mismatch),
  so a mixed-width hour cannot select a wrong-width spot bucket and break the $2 pin.
* **P3-3** Feed a monotonic freshness clock (`max(last, new)`) or per-stream clocks so a two-connection
  clock skew cannot cause spurious cancel/replace churn (budget + `A_REPLACE`).
* **P3-4** Assert `exec_price == rec.price` (post_only maker) on every real fill, or reconcile the lock;
  `exec_price`/`exec_fee` are already journaled for this.
* **P3-5** Subscribe the private `fill` channel when armed and drive cancels through a status-confirmed
  path (partial-before-cancel as `OrderCancelled.filled_count_before_cancel`; `Fill` only for a
  tracked-or-retained coid).
* Wire the real create/cancel/status/batch envelope + `DAILY_ORDER_BUDGET>=4000` / `ORDER_TICKER_
  PREFIXES` covering `KXBTC`+`KXBTCD` / `MAX_CONTRACTS_PER_ORDER=2` (Phase-4 S5 `/health` check).

## Files

* Reviewed: `pilot/service/run_v32.py`, `pilot/service/v32/{ledger,report}.py`,
  `pilot/service/v32/core.py` (the additive `book_late_rest_fill`), `pilot/ops/v32_mode.txt`,
  `pilot/ops/{register,unregister}_v32_task.ps1`, `pilot/ops/V32_DRY_RUN.md`,
  `pilot/tests/{test_run_v32,test_register_v32_task}.py`, `.gitignore`; mirrors
  `service/{record_range,run_window,ws_client,record_window,book}.py`.
* Changed: `pilot/service/run_v32.py` (H-1 trade parser, H-2 `_fill_event` + `on_fill` wiring +
  exec-price journaling), `.gitignore` (R2), `pilot/ops/V32_DRY_RUN.md` (R2 step),
  `pilot/tests/test_run_v32.py` (R2 mode-file test), `pilot/tests/test_v32_live_frames.py` (new, 8
  tests), `pilot/tests/fixtures/v32/live_frames/*.json` (4 fixtures); `git rm --cached`
  `pilot/ops/v32_mode.txt`.
