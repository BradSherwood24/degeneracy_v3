# V3.2 Phase 3 review — maker execution + money math + stops (`v32/phase3-exec`, PR #33)

Reviewer: Opus 4.8. Base `ae81a4c` (Phases 1+2). Reviewed commit `1103112`; fixes committed on top.
Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`. No `.env`/`*.pem`/`sim/out/sealed_eval`/`pilot/journals`/
`pilot/ledger` or 2026-08-20..29 date read by any code path or test I ran. `python` only; the live proxy
(127.0.0.1:8642) and Kalshi were NEVER dialed. Only public docs.kalshi.com pages were fetched (read-only).

## VERDICT

**APPROVE WITH FIXES APPLIED** (do not merge — Brad merges). The architecture is sound: one executor-
selection point, unconditional wing take, retained cancel context, fill de-dup across three channels, the
banded S4, the separate day guard. But the doc verification turned up **three wire bugs that would have lost
money or left naked legs on the first armed window** — the auto-expire field name, the order-status field
names, and the cancel-race truth — plus a naked-leg accounting gap and a POST-timeout double-entry / dropped-
fill hazard. All are fixed with tests. Suite: **765 passed** (was 759; +6 net new tests). The residual items
below are for the FIRST ARMED WINDOW to confirm and for the Phase-4 `[pin]` list.

## DOC VERIFICATION (docs.kalshi.com, fetched 2026-09-13)

* **(a) create-order expiration + post_only** — `create-order-v2`: the field is **`expiration_time`**
  (integer Unix **seconds**), valid only with `time_in_force=good_till_canceled`; `post_only` IS a top-level
  boolean; `side` is `bid`/`ask` only (NO `yes_price`/`no_price`/`action` in the V2 body — `translate`
  already maps them away); `self_trade_prevention_type` ∈ {`taker_at_cross`,`maker`}. The code sent
  **`expiration_ts`** — a NON-EXISTENT field the venue ignores. **BUG (fixed).**
  URL: https://docs.kalshi.com/api-reference/orders/create-order-v2
* **(b) single-order GET** — `get-order`: path `GET /portfolio/orders/{order_id}` (code correct). Fields are
  **`fill_count_fp` / `remaining_count_fp` / `initial_count_fp`** (fixed-point) + `status`,
  `yes_price_dollars`/`no_price_dollars` — there is **NO** `fill_count`, `remaining_count`, `place_count`,
  `maker_fill_count`, or `taker_fill_count`. `parse_order_status` read ONLY those non-existent names, so a
  live status ALWAYS parsed `filled=0`. **BUG (fixed).**
  URL: https://docs.kalshi.com/api-reference/orders/get-order
* **(c) cancel** — `cancel-order-v2`: `DELETE /portfolio/events/orders/{order_id}` (code correct; the build
  report's deviation from the task note is VALIDATED by the docs). Response is `{order_id, client_order_id,
  reduced_by, ts_ms}` — **NOT** the order with a fill count. `reduced_by` = "the remaining count at time of
  cancellation" (contracts pulled off the book), so the race IS decidable from the DELETE reply:
  `filled_before_cancel = placed − reduced_by`. The code ignored `reduced_by` and relied solely on the
  (broken, see b) status GET → the cancel race silently read 0. A 404 is returned for a non-existent order.
  **BUG (fixed).** URL: https://docs.kalshi.com/api-reference/orders/cancel-order-v2
* **(d) open-orders list** — `get-orders`: `GET /portfolio/orders?status=resting` (values
  `resting`/`canceled`/`executed`); order carries `ticker` (not `market_ticker`), `order_id`,
  `client_order_id`. Code correct. URL: https://docs.kalshi.com/api-reference/orders/get-orders

## FINDINGS (ranked; file:line at review commit) — all HIGH/MED fixed with tests

1. **HIGH — auto-expire field name wrong (crash-safety defeated).** `executor.py:328` set
   `body["expiration_ts"]`. Per (a) the field is `expiration_time`; the venue ignores the unknown field, so a
   crashed process would leave the GTC rest on the book with NO auto-expiry (rests to settlement). FIX:
   `body["expiration_time"] = int(exp)`. Test: `test_place_rest_wire_body_post_only_gtc_expiration`
   (+ asserts `expiration_ts` absent).
2. **HIGH — order-status parses filled=0 always (cancel race + poll blind).** `executor.py:parse_order_status`
   read `fill_count`/`remaining_count`/`place_count`/`maker_fill_count`/`taker_fill_count` — none exist per
   (b). Every live cancel-confirm and every belt-and-braces poll would report 0 filled → a real fill on a
   cancelled order goes unhedged unless the WS channel also carries it. FIX: read `fill_count_fp` /
   `initial_count_fp` − `remaining_count_fp`, legacy names as fallback. Test:
   `test_parse_order_status_derivations` (fp cases added).
3. **HIGH — cancel race ignored the authoritative `reduced_by`.** `executor.py:_cancel_rest` relied only on
   the status GET (broken by #2) and never assumed the fill. FIX: compute `filled = placed − reduced_by` from
   the DELETE reply, cross-check the status GET, take the **max** (never under-count a fill), AND book the
   race fill into money-math once (de-duped by order_id vs a later WS echo). Tests:
   `test_cancel_race_reduced_by_from_delete_response_is_authoritative`,
   `test_cancel_confirms_filled_count_from_status_not_assumed_zero`.
4. **HIGH — POST timeout/5xx = double-entry + dropped phantom fill.** `executor.py:_place_rest` treated ANY
   non-2xx as a clean rejection and let the core re-place. On a transport timeout (status None) or 5xx the
   order may be LIVE: re-placing double-enters, and a fill on it arrives on a coid never recorded → dropped
   as `foreign_fill_ignored` → a naked, unhedged bucket-NO. FIX: on an unknown outcome (status None or ≥500)
   record the coid in the RestBook (so a phantom fill is attributed and hedged via the late-fill path) AND
   latch a `post_unknown_outcome` stand-down (no second rest this hour; `expiration_time` bounds the leak). A
   definite 4xx (post_only cross / cap / `daily_order_budget` 403) stays a clean rejection → the 3-strike
   stand-down. Tests: `test_post_unknown_outcome_records_coid_and_stands_down`,
   `test_post_5xx_is_treated_as_unknown_not_clean_reject`.
5. **MED — a never-hedged lone bucket-NO was not flagged `one_legged` (missed S1_LEGGED latch).**
   `core.py:_wing_step` cutoff required `wing_taken=True`; if the strike feed died from the fill to the settle
   cutoff, the wings were never taken → a lone bucket-NO held naked, and the day-latch counter never
   incremented and the ledger said "not one-legged". FIX: at the cutoff flag `one_legged` whenever a
   `rest_fill` exists and `wings_needed` is still set (covers 0-wings-taken and a-leg-missed). Test:
   `test_lone_bucket_no_never_hedged_latches_one_legged_at_cutoff`.
6. **MED — S4 pending-credit overstated a lone-leg pin (could miss a real day-loss latch).**
   `ledger.py:v32_pending_credit` used `2 − floor`; a lone bucket-NO pays AT MOST $1, so crediting $2 (2−0)
   credits money that can never arrive → the banded S4's optimistic bound could fail to latch a real loss.
   FIX: `credit = min(#legs, 2) − floor`. Test: `test_pending_credit_lone_leg_bounded_at_one_not_two`.
7. **LOW — startup sweep could cancel another pilot's order.** `cancel_stale_open_orders` filtered by ticker
   prefix `KXBTC` only; the account is shared, so a re-armed v1.1 box order on a KXBTCD strike would be
   cancelled. FIX: skip any KXBTC* order whose `client_order_id` is present and does NOT start with `v32-`;
   still cancel coid-less crash leftovers. Test: `test_startup_cancel_skips_foreign_coid_but_clears_...`.
8. **LOW — dead tautology in the caps prefix check.** `stops.py:v32_caps_agree` OR'd
   `probe.startswith(p)` with an identical clause; replaced with the single exact (fail-closed) condition.

## NOT FIXED — flagged for a ruling / the first armed window

* **S4 floor-netting (conservative, arguable).** For a COMPLETE or 2-leg unsettled pin, `v32_pending_credit`
  still nets the guaranteed floor (`min(#legs,2) − floor`), so an unsettled but profitable pin's cash dip is
  NOT credited back — biasing S4 toward a (spurious) stand-down if two pins are unsettled at once at :40. This
  errs toward HALTING (safe), and the "minus floor" is a deliberate, documented choice, so I left it — but the
  real-balance S4 arguably wants the FULL owed payoff (`min(#legs,2)`). **Phase-4 decision** (kept #6's
  unambiguous unsafe overstatement fixed either way).
* **Reconcile-first keys on non-zero size only.** `reconcile_positions_clean` refuses if any KXBTC* position
  size ≠ 0. Confirm the venue zeroes a SETTLED position by the next :40 (else a slow settlement blocks arming
  — fail-closed, safe, but could stand the pilot down). Added to the first-armed-window list.

## RESOLVED BY RULING (R-OVERLAP, coordinator 2026-09-13) — applied on this branch

* **Requote-overlap double-fill.** The replace is now STRICTLY SEQUENTIAL: `core._requote` emits CANCEL_REST,
  waits for OrderCancelled (a fill there → TAKE_WINGS, never PLACE), then PLACE_REST at the freshly re-solved
  n on a later tick — exactly like the bucket-change path. `rest_live` is kept populated (not eagerly cleared)
  so a fill during the cancel books at the RESTING price, not the drifted `desired_n`; `awaiting_replace` +
  the cancel-in-flight hold suppress any PLACE while the cancel is outstanding. Never two live rests; never a
  fillable old rest beside a new one in flight. The ~200-400 ms of no quote per replace (~30 s/hour) is
  accepted. Core law docstring + `PLAN_V32.md` "Requote policy" updated. Tests: rewrote
  `test_requote_above_tol_and_debounce_replaces` and `test_requote_debounce_blocks_until_elapsed`
  (sequential); added `test_replace_no_live_fill_on_trade_while_cancel_in_flight` and
  `test_fill_during_replace_cancel_takes_wings_not_place`.

## FIRST ARMED WINDOW MUST CONFIRM

1. A GTC rest carries `expiration_time` (Unix seconds) and the venue ACCEPTS it (auto-expires at T-5).
2. A live `GET /portfolio/orders/{id}` returns `fill_count_fp`/`remaining_count_fp`/`initial_count_fp` and a
   `status` the confirm treats as terminal (`status not in ("resting", None)`), so the poll/cancel see fills.
3. The `DELETE` reply carries `reduced_by`; `placed − reduced_by` agrees with the WS fill on a real cancel race.
4. A NO (bucket) rest fill reports `purchased_side="no"` with `yes_price_dollars` (validated against
   `fill_frame.json`; the NO-space paid price = 1 − yes) — the `exec_price_mismatch` alarm stays quiet.
5. `/health` exposes `orders_enabled`, `caps.max_contracts_per_order`, `caps.ticker_prefixes`,
   `orders_remaining_today` in the shapes `v32_caps_agree` reads; positions/balance shapes match reconcile/S4.
6. No requote-overlap double-fill (now sequential by R-OVERLAP); no unknown-POST stand-down under normal latency.
7. Reconcile-first sees ZERO size for the previous hour's SETTLED positions by :40 (a settled KXBTC* position
   reports position 0 / is absent, so a fresh window is not blocked from arming by a stale settled row).

## PHASE 4 `[pin]` LIST (every threshold hard-coded)

`service.v32.stops`: `V32_S4_DAY_LOSS_CAP_DOLLARS=3.00`, `V32_MIN_ORDER_BUDGET_AT_ARM=200`,
`V32_MAX_CONTRACTS_PER_ORDER=2`, `V32_S1_LEGGED_LATCH_THRESHOLD=2`, `V32_RANGE_TICKER_PROBE="KXBTC-"`,
`V32_STRIKE_TICKER_PROBE="KXBTCD-"`.
`service.v32.executor`: `CANCEL_CONFIRM_POLLS=3`, `CANCEL_CONFIRM_INTERVAL_S=0.2`,
`CONSECUTIVE_REJECT_STANDDOWN=3`, POST-unknown classifier (`status is None or >=500`), `_V32_COID_PREFIX="v32-"`.
`service.run_v32`: `ORDER_POLL_INTERVAL_S=1.0`, `CONNECT_MARGIN_S=5.0`, `PUMP_INTERVAL_S=0.5`.
`policy/v32_params.json` (sha-pinned): `E`, `tol`, `deb_ms`, `wing_margin`, `lock_floor`,
`no_orders_after_s_to_settle`, `freshness_max_age_s`, `n_min`, `replace_rate_alarm_per_min`, `bucket_width`,
`contracts`, `quote_start_s`, `quote_end_s`, `max_sets_per_hour`.

## SUITE

`cd pilot && python -m pytest -q` → **767 passed in ~26s** (759 baseline + 6 review fixes + 2 R-OVERLAP tests).
