# V3.2 build report — quote-end cancel vs venue-expiry race

Branch: `fix/quote-end-expiry-race` (off `origin/main` @ 0077425)
Scope: `pilot/service/v32/executor.py`, `ledger.py`, `run_v32.py`, docs, tests. No policy JSON, no
falsifier edit, no live tree touched. Full suite green: **853 passed** (`python -m pytest pilot/tests -q`).

## The incident (2026-09-15T01:00:00Z window, armed, no money at risk)

The last unfilled rest of the window — coid `v32-2026-09-15T01:00:00Z-100`, order
`01a0a28f-4f00-7285-89e7-a04bb83d1b1d`, created 00:54:56.08Z — carried
`expiration_time = close_epoch - quote_end_s = 1789433700 = 00:55:00Z`, i.e. **the exact instant of the
executor's own quote-end (T-5) cancel**. Journal evidence (live tree
`pilot/journals_v32/20260915T010000Z.jsonl.gz`, sliced into the fixture below):

- `place_rest` driver form: `{"expiration_epoch": 1789433700, "price": "0.21", "ticker": "KXBTC-26SEP1421-B77950", ...}`
- `place_rest` wire form: `{"expiration_time": 1789433700, "time_in_force": "good_till_canceled", ...}`
- `cancel_rest`: `{"order_id": "01a0a28f-...", "exchange_index": 2}` (journaled 1789433699.846Z)
- `cancel_failed`: `{"delete_status": 404, "last_status": "resting", "exchange_index": 2, ...}` (1789433700.368Z)
- `alarm` `cancel_failed`, then `alarm` `executor_standdown` reason `cancel_failed`, then `stand_down` `stood_down`.

Sequence: the venue expired the order at 00:55:00.000659Z. Our correctly-sharded DELETE at
00:55:00.367Z returned **404** (order already terminal). The non-2xx path did an order-status GET; the
venue — an eventually-consistent read — still reported **"resting"**. The executor then retried the
DELETE 3× **all logged at the same timestamp `00:55:00.367` (no delay between retries)**, read
"resting" every time, and declared `cancel_failed` + alarm + executor stand-down. Ledger row for that
window: `cancels_attempted 103, cancels_confirmed 99, cancel_404s 4, alarms 2`. The venue was clean
(nothing resting, no positions) — **the outcome was correct, the diagnosis was wrong and noisy**, and
because the cancel and the expiry were scheduled for the same instant it would recur on **every**
unfilled window.

## Changes

### 1. Grace so the quote-end cancel wins (executor.py)
New code constant `EXPIRATION_GRACE_S = 60`. The GTC rest's `expiration_time` is now
`close_epoch - quote_end_s + EXPIRATION_GRACE_S` (T-4 instead of T-5), computed in
`_expiration_epoch(action)` and used by `_rest_body`. The quote-end DELETE at T-5 now lands a full
minute before the venue's auto-expiry backstop at T-4, so the two no longer race; the expiry still
bounds a **crashed** process and still lands well before the wings' T-1 cutoff and the close. The value
is **not** in the sha-pinned `v32_params.json` (untouched; `FROZEN_V32_PARAMS_SHA256` unchanged).

### 2. Backoff + status-truth on a non-2xx DELETE (executor.py `_cancel_nonok`)
New constant `CANCEL_BACKOFF_S = (0.25, 0.75, 2.0)` (one entry per `CANCEL_RETRY_ATTEMPTS`), slept via
the **injected** `sleep` **before** each DELETE retry so the venue's eventually-consistent order status
settles. After the initial 404 and after each retry, the order status is re-GET; a **terminal** status
(`canceled`/`cancelled`/`executed`/`expired`) confirms the cancel by status-truth via the new helper
`_resolve_cancel_from_status`, journaling `cancel_confirmed` with `delete_status: 404` and
`filled_before_cancel = fill_count_fp`. A filled-before-cancel (`fill_count_fp > 0`) flows through the
unchanged `_finish_cancel` path (booked once as `path: "cancel_race"`, wings owed). Only if the venue
**still** reports "resting" after the whole backoff sequence does the executor declare `cancel_failed`
+ alarm + stand-down — the existing safety behaviour, preserved.

### 3. Expiry-aware classification + counters (executor.py, ledger.py, run_v32.py)
`RestRecord` now stores `expiration_epoch` (the auto-expiry we sent). In `_cancel_nonok`, if
`now >= expiration_epoch` when a 404 resolves terminal, it is classified as an
`expired_at_quote_end` confirmation (`cancel_confirmed` `via: "expired"`); otherwise `via: "status"`.
Two new counters `cancels_via_status` / `cancels_expired` sit next to `cancel_404s` on the executor,
are folded into the ledger row by `run_v32._compute_money_math`, and are new params + row fields in
`ledger.build_v32_ledger_row` (same pattern PR #46 used for `cancels_attempted/confirmed/cancel_404s`).
(The prior `via` strings `status_terminal` / `status_terminal_retry` are replaced by `status` /
`expired`; no test pinned the old strings.)

### 4. Docs (wording only)
`pilot/ops/V32_ARMING.md` (the mid-window stand-down note, the by-hand-cancel note, and the
FIRST-ARMED-WINDOW confirm item) and `pilot/PLAN_V32.md` now say the rest "expires EXPIRATION_GRACE_S
after the quote end (crash backstop; the quote-end cancel is the primary path)". `V32_DRY_RUN.md` has
no expiry statement (nothing to change). **The frozen falsifier `pilot/ceremony/v32_falsifier.md` was
NOT edited** — `test_v32_falsifier_pins.py` stays green.

## Tests
New file `pilot/tests/test_v32_quote_end_race.py` (6 tests, fixture-driven, fakes only):
- **(a)** incident replay — DELETE 404 → GET "resting" → (after backoff) GET "canceled" ⇒
  `cancel_confirmed via status`, no `cancel_failed`, no alarm, no stand-down, `cancels_via_status == 1`,
  at least the first backoff sleep fired.
- **(b)** DELETE 404 → terminal status `fill_count_fp "1.00"` ⇒ filled-before-cancel (booked once,
  `path cancel_race`, `OrderCancelled.filled == 1`).
- **(c)** DELETE 404 → "resting" through the whole backoff ⇒ `cancel_failed` + stand-down (existing
  behaviour preserved); `1 + CANCEL_RETRY_ATTEMPTS` deletes; sleeps exactly `(0.25, 0.75, 2.0)`.
- **(point 2)** cancel at/after the order's own expiration ⇒ `cancel_confirmed via expired`,
  `cancels_expired == 1`.
- **(d)** create body `expiration_time == close - 300 + 60` and `RestRecord.expiration_epoch == NEW_EXP`.
- **(e)** backoff sequence is exactly the backoff and is injectable (asserted in (c) + a constant check).

Updated `pilot/tests/test_v32_executor.py::test_place_rest_wire_body_post_only_gtc_expiration` to assert
`expiration_time == CTS - 300 + EXPIRATION_GRACE_S` (the intentional grace change).

Fixture `pilot/tests/fixtures/v32/incident_20260915T010000Z_quote_end_race.jsonl.gz` — the 6 real
incident-order records (place ×2, cancel ×2, cancel_failed, alarm), 508 bytes, ws/eval stripped.

## Secondary finding (report-only, no code change)

`pilot/ops/v32_stops_2026-09-15.json` shows `balance_start_dollars "51.9970"` while the venue balance
at the 00:40Z wake was ~52.11 (the 00:00Z set settled at 00:02:48Z).

**Mechanism (`stops.py` `ensure_balance_start`, called from `run_v32.py:1640` inside the
`resolved_mode == "armed"` block):** the day's S4 baseline is a **fresh, live** `/portfolio/balance`
read taken at the **first armed wake of the UTC day**, persisted per-day and never overwritten after.
It is **not** a cached previous-day value by mechanism (each `v32_stops_<utc_day>.json` gets its own
first-wake read). But the first window attributed to a UTC day is the `:00`-close window, whose process
wakes at `:40` of the **prior** hour (T-20) — for 2026-09-15 that is 2026-09-14T23:40Z. So the baseline
is snapshotted ~20 min before the day boundary and, crucially, **before the day's first set (the 00:00Z
set) trades and settles**. Relative to that set it is a **pre-settlement read** — which is why 51.9970
predates the +0.11 the 00:00Z settlement posted by 00:40Z. (The identical 51.9970 in the 09-14 guard
file is consistent with a flat balance across the two first-wakes, not with a copy.)

**Is it a bug?** In the harmful direction, no. For a day-loss cap the baseline *should* be the balance
before the day's trading, so the 00:00Z set's stake (debit) and settlement (credit) both fall inside
the day and are measured correctly; `v32_s4_decision`'s pending-settlement band already prevents an
unsettled set from spuriously latching. The only wrinkle: a baseline 0.11 **below** the post-first-set
balance makes S4 marginally **more lenient** (it measures a slightly smaller `start - now`), negligible
against the $3.00 cap.

**Proposed fix:** none required; optionally document that the S4 baseline is the pre-trading balance of
the day's first window (settlement gains/losses of that window are in-day by design). I did **not**
implement a change: making the baseline a post-settlement / day-boundary read is a semantics decision
for Brad, not a one-liner, and doing it naively would risk excluding the first set's P&L from the day.

## Open questions / unsure
- Chose `via` values `status` / `expired` (replacing `status_terminal` / `status_terminal_retry`); no
  test or report field pinned the old strings, but if any external log parser keys on them, flag it.
- With the T-4 grace, a real 404 at the T-5 cancel should now be rare (the order isn't expired yet, so
  the DELETE should 2xx); test (a) still simulates the 404 to prove the backoff/status-truth path is
  robust if the venue ever 404s a live order again.
