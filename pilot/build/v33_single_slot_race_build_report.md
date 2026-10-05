# V3.3 single-slot cancel/re-place race — build report

Branch: `fix/v33-single-slot-race` (from main c31cdb0). Opus 4.8 builder. Date 2026-10-04.

## The defect (three live trips 2026-10-04: 02:00Z, 08:00Z, 19:00Z; zero exposure, each killed the rest of the window)

V3.3 rests one maker NO order per rung (K=11). When the pin moves, the core's per-rung convergence can
CANCEL a rung resting at price P and, a few ms later, want to PLACE a replacement at the SAME P (a
shrink-then-return / roll-path convergence, **not** a cancel-all). The CANCEL's DELETE confirm lags the
send by ~100–800 ms. In that gap two things went wrong, each sufficient to trip the hour:

- **core** emitted the re-place at P while the old order's cancel was still unconfirmed; and
- **executor** `_pre_place_invariant_async` (`pilot/service/v33/async_executor.py`) GETs our resting
  orders, saw the old order still listed at P, and `_invariant_verdict` flagged `dup_price` →
  `rest_invariant_violation` → executor stand-down → `standdown_sweep` cancels everything → no quoting
  until close.

### Journal evidence (read before the build)
- `pilot/journals_v33/20261004T020000Z.jsonl.gz`: coid `-14` `cancel_rest` at t+377.21, coid `-18`
  `place_rest` at `0.14` at t+377.22 → `rest_invariant_violation {dup_price: true}`; the `-14` DELETE
  `cancel_confirmed` only at t+378.04 (≈0.83 s later).
- `pilot/journals_v33/20261004T080000Z.jsonl.gz`: coid `-187` at `0.10`, `count_resting` 8.
- `pilot/journals_v33/20261004T190000Z.jsonl.gz`: coid `-38` at `0.10`, `count_resting` 6.

Gate C (standdown sweep) already made this **safe** (0 exposure). This build makes it **cheap** (the hour
keeps quoting).

## Part (a) — executor belt (`pilot/service/v33/async_executor.py`)

The pre-flight invariant now classifies any venue order **for which WE have a DELETE in flight** as
*cancelling*, not *resting*: excluded from `count_resting`, from the `dup_price` test, and from `strays`.

- Reuses the existing GATE C set `self._cancel_oids_inflight` (added 2026-10-03), which already holds
  exactly "venue order ids with a DELETE sent and not yet resolved" — armed in `_delete_and_resolve_async`
  **before** `await self._adelete(...)` and discarded in its `finally`, covering the `_cancel_rest_async`,
  `cancel_after_ack`, and standdown-sweep DELETE paths. No new parallel set was needed (see CONFESSIONS).
- New method `_exclude_cancelling(entries, now)`: drops list entries whose `order_id ∈ _cancel_oids_inflight`,
  journals `rest_invariant_cancelling_excluded {order_ids, coids}`, and bumps counter
  `rest_invariant_cancelling_excluded` (new instance attr + `_bump` key). Applied **after**
  `_filter_phantoms` (a cancelling order is not yet cancel-CONFIRMED, so the phantom belt keeps it) and
  before **every** `_invariant_verdict` (first read and the post-recheck reread).
- The `rest_invariant_phantom` / `book_confirm` belt is untouched and still runs first; exclusion is a
  second, independent filter layered after it.
- A **real stray** (no DELETE of ours in flight) is not excluded and trips exactly as today.

Files/functions touched: `V33AsyncExecutor.__init__` (new counter), new
`V33AsyncExecutor._exclude_cancelling`, `V33AsyncExecutor._pre_place_invariant_async` (two call sites).

## Part (b) — core gate (`pilot/service/v33/core.py`)

No `PLACE_REST` (nor roll-to) at a price whose previous order in that slot has an unconfirmed cancel.

- New `V33State` field `cancel_pending_px: Mapping[str, Decimal]` — venue `order_id → NO-space price` of a
  rung we asked the venue to CANCEL whose `OrderCancelled` has not yet arrived. Keyed by `order_id` so the
  confirm (which carries the id) clears **exactly** its own price; two different prices never unblock each
  other.
- Arm (`_arm_cancel_pending`): at every CANCEL_REST emission for an order that **has** a venue id —
  `_cancel_all` (bucket change / stand-down / quote-end / allotment-done), the `_converge` shrink-cancel
  (the single-slot-race site), and `_pt_cancel_unfilled_rungs` (print-through stall). A pending-order cancel
  (no venue id) arms nothing (nothing rests at the venue).
- Hold (`_held_cancel_prices`): the convergence `VACANT` set now excludes held prices (blocks both the
  vacant-CREATE and a roll-TO that price), and `_place_all` skips a held price. A held slot reappears on the
  next tick after the confirm, so it is **deferred, never dropped**.
- Clear (`_clear_cancel_pending`): in `_apply_cancelled` (the cancel confirm / reject-as-OrderCancelled,
  keyed by `ev_oid`) and in `_apply_fill` (the order filled instead of cancelling, keyed by
  `event.order_id`). `_apply_cancelled` then runs its tail `_converge`, which re-places the freed slot.
- `outstanding_cancels` semantics unchanged (it still counts only the cancel-all/shrink tracked cancels);
  the new map is independent and additive, not a replacement.

Files/functions touched: `V33State` (new field), new `_arm_cancel_pending` / `_clear_cancel_pending` /
`_held_cancel_prices`, `_cancel_all`, `_converge` (VACANT filter + shrink-cancel arm),
`_pt_cancel_unfilled_rungs`, `_place_all`, `_apply_cancelled`, `_apply_fill`.

## Fail-safe / bound

A never-confirming cancel blocks **only its own price**, and **only until quote end**: the quote-end
cancel-all + stand-down paths do not depend on this gate and run regardless, so the ladder cannot be wedged
beyond the window. Dry/shakedown paths are byte-identical when nothing is pending (the map is empty, the
filters are no-ops).

## Tests (`pilot/tests/test_v33_single_slot_race.py`, 6 new)

- `test_single_slot_race_holds_replace_until_cancel_confirms` — golden, the 02:00Z shape: shrink-CANCEL the
  top rung at 0.50, n_top returns so 0.50 is a target again → no PLACE_REST at 0.50 across two book updates
  + a ClockTick; after `OrderCancelled(top_id)` the slot re-places at 0.50.
- `test_single_slot_race_fill_before_confirm_releases_the_price` — a fill on the cancelling order releases
  the held price (orphan path).
- `test_cancel_pending_prices_are_independent` — two different prices do not block each other; only the
  matching order_id clears a price.
- `test_pending_order_cancel_arms_nothing` — a cancel of a pending (id-less) order arms nothing.
- `test_inflight_cancel_excluded_place_at_same_price_posts` — part (a): a SlowDeleteProxy (threading.Event
  gate, `SlowCreateProxy` pattern) whose DELETE blocks while its resting-orders GET still lists the order; a
  PLACE at the same price during the in-flight DELETE does NOT trip the invariant, POSTs, and bumps
  `rest_invariant_cancelling_excluded` + journals it; `stand_down_reason` stays `None`.
- `test_real_stray_at_same_price_still_trips_when_no_cancel_in_flight` — regression guard: the same list
  with no cancel in flight still trips `rest_invariant_violation {dup_price}`.

## Suite counts (from `pilot/`, this worktree)

- New file alone: `6 passed`.
- v33 subset (`-k v33`): `508 passed, 4 skipped` (the 4 census_train.csv corpus skips).
- Full suite: `1510 passed, 10 skipped, 0 failed` in ~40 s.
  - 10 skips = 4 × census_train.csv corpus absent (the expected worktree skips) + 2 × box_golden
    historical-data absent + 2 × quintile historical-data absent + 1 × quintile census corpus +
    1 × test_supervisor POSIX-only SIGTERM. All environmental (gitignored data / POSIX-only), none caused by
    this change. (Main's full-checkout baseline is ~1557 passed / 5 skipped; the delta is the corpus/data
    files absent in a worktree, plus the 6 new tests.)

## CONFESSIONS

1. **Spec vs code — part (a) tracking set.** The spec said to "add the symmetric tracking for in-flight
   cancels … added before the DELETE is awaited." That set **already exists** as GATE C's
   `_cancel_oids_inflight` (armed before `await self._adelete`, discarded in `finally`, covering every
   DELETE path). Per the registered-specs / safest-faithful rule I **reused** it rather than add a redundant
   parallel set that could drift. Behaviour matches the spec's intent; I did not add a second set.
2. **Residual window the executor belt does not cover.** `_cancel_oids_inflight` is armed inside
   `_delete_and_resolve_async`, i.e. after the cancel lane's `await self._pacer.acquire_async(...)` in
   `_cancel_rest_async`. During that pacer wait the DELETE is not yet "in flight," so a racing place's
   pre-flight GET in that sliver would **not** be excluded by part (a) alone. This is exactly why part (b)
   (core) is required and is the primary fix: the core does not emit the place at that price at all until the
   confirm. Belt (a) + gate (b) together close it.
3. **Roll-fallback re-place is not price-gated.** The amend→cancel→create fallback in `_apply_cancelled`
   re-places at the roll's target via `_place_one` directly (not through the VACANT filter). Rolls emit
   AMEND_REST (venue-atomic reprice), not CANCEL_REST, so they are not armed in `cancel_pending_px` and are
   not part of the single-slot race; if a fallback target ever coincided with another rung's held price,
   part (a)'s invariant exclusion is the backstop. I did not gate it to avoid widening scope.
4. **Part (a) keyed by order_id only.** The venue resting-list entries always carry an order_id (id-less
   entries are already dropped by `_venue_resting_ours_async`), so order_id keying is sufficient; I journal
   both `order_ids` and `coids` for auditability but exclude on id.
5. I could not exercise the live proxy, WS feed, or real venue (house law); all tests use in-process fakes.
   I did not read the sealed holdout, journals as anything but read-only fixtures, `.env`, or any `*.pem`.
   V3.2 code was not touched.

## How to read the next armed window

- **Success (fix working):** journal kind `rest_invariant_cancelling_excluded` appears (with `order_ids` /
  `coids`) and counter `rest_invariant_cancelling_excluded > 0`, while `rest_invariant_violation` stays
  **absent** and `standdown_sweep` does **not** fire. The core side is silent (no journal kind for a held
  price by design) — confirm via continued `place_rest` / `amend_rest` activity through to close.
- **Regression (fix failed):** any `rest_invariant_violation {dup_price: true}` followed by
  `standdown_sweep` / `standdown_sweep_done` — the same signature as tonight's trips.
- Counters to pull from the executor row: `rest_invariant_cancelling_excluded` (new, should be ≥ the number
  of same-price convergence re-places), `rest_invariant_violations` / `rest_invariant_dup_price` (should
  stay 0 for this cause), `standdown_sweep_cancels` (should stay 0).
