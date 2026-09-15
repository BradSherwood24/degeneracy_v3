# Review — PR #50 `fix/quote-end-expiry-race` (quote-end cancel vs venue expiry)

Reviewer: Opus 4.8 (delegated). Worktree: `C:\Users\Brads\Python_stuff\dv3_wt_review` (NOT the live
tree). Base: `origin/main` @ 0077425. Head: `origin/fix/quote-end-expiry-race` (93e7078). Diff read in
full; executor / ledger / run_v32 / docs / tests read in surrounding context. No live tree touched, no
`.env`/`*.pem`/sealed read, no proxy write.

## Verdict: APPROVE WITH NITS

No blocking (money-safety) items. The fix is correct, well-scoped, and its tests exercise the real
branches with realistic fake venue shapes. One doc-vs-code drift in the FROZEN falsifier must go to
Brad (below) — it is not a code defect and I did not edit the frozen file. Ship-worthy as is; the nits
are for the builder / Brad to weigh, none gate the merge.

## Tests observed

`python -m pytest pilot/tests -q` in this review worktree: **849 passed, 2 skipped, 2 errors**. All 4
non-passing tests are `historical-data absent` (this worktree has no tape checked out):
`test_box_golden.py:300/351` skip on it; `test_quintile.py` errors are `FileNotFoundError` on
`historical-data/15-minute/markets/2026-06-11.jsonl`. `test_quintile.py` last changed at the initial
commit — unrelated to this PR. In the main tree (tape present) all four run, i.e. **853 passed, 0
errors** — this exactly reconciles the build report's "853 passed" claim (849 + 2 box-golden + 2
quintile). The 27 PR-relevant tests
(`test_v32_quote_end_race.py` + `test_v32_executor.py`) pass here: **27 passed in 0.85s**.

## 1. Money safety — PASS

- **Can a PLACE be freed while an order actually rests (the 09-14 21-stray class)?** No. On the non-2xx
  path a place is freed (`_last_confirmed_gone_oid = oid`, `OrderCancelled` returned) ONLY when the
  order-status GET reports a value in `_TERMINAL_STATUSES`. A still-`resting` read never frees it — it
  retries the shard-aware DELETE and, if still resting after the whole backoff, declares `cancel_failed`
  + latches stand-down and marks `RestRecord.status = "cancel_failed"` (not `cancelled`). Test (c)
  pins this. `_pre_place_invariant` still runs before every `_place_rest` (executor.py:348) and consults
  venue truth via the open-orders GET; the status-confirmed oid is excluded from exactly that one check,
  identical to the pre-existing 2xx path — not a new hole.
- **Fill-before-cancel discovered on the 404 status path.** Traced exactly into the wings path. Both
  the 2xx `reduced_by` path (`_resolve_cancel_success`) and the new 404 status path
  (`_resolve_cancel_from_status`) funnel `filled` into the unchanged `_finish_cancel`, which books the
  race fill once (`leg: "rest"`, `path: "cancel_race"`, `fee: Decimal(0)`, de-duped by
  `booked_rest_oids`) and returns `OrderCancelled(filled_count_before_cancel=filled)` so the core owes
  wings. A duplicate arriving on the WS `fill` channel / 1 s poll is de-duped by `order_id` at the
  driver and by `booked_rest_oids` in money-math — no loss, no double-book. Test (b) pins the
  single-booking and `path cancel_race`.
- **Can `via: "expired"` be reached while the order is NOT terminal?** No. `_resolve_cancel_from_status`
  returns `None` (does not classify anything) unless `st.available and st.status in
  _TERMINAL_STATUSES`. `expired` is only a counter/label branch AFTER that gate. `expired` is computed
  once from `now >= rec.expiration_epoch` and is constant across retries (correct — `now` is fixed for
  the `on_action`).

## 2. Timing — acceptable, one note

- Worst-case added latency on the anomalous path: `sum(CANCEL_BACKOFF_S) = 3.0 s` of injected sleep +
  4 GETs + 4 DELETEs. At the quote-end cancel (T-5 = close-300) this lands at ~close-297, far ahead of
  the wings' T-1 cutoff, the close-out, and the journal flush — nothing time-sensitive is starved.
- The executor is synchronous and blocks the WS reader during the sleeps (per memory
  "executor blocks WS reader"). This is only reached on a **non-2xx DELETE whose first status GET is
  still non-terminal**. With the T-4 grace, the normal quote-end DELETE now finds the order live and
  returns 2xx (no backoff at all). The 2xx path is completely untouched — no sleep — and the first
  status GET after a 404 is immediate (no sleep); only the retries are spaced. So "backoff only after
  the first 404" is effectively already the behaviour.
- **NOTE (non-blocking):** the backoff is gated on "non-2xx cancel", not on "at/after quote end". The
  pump-fader requotes continuously mid-window, so a mid-window cancel that ever 404s with a stale
  `resting` read would also block the WS reader up to 3 s. After the shard fix (PR #46) a healthy
  mid-window cancel returns 2xx, so this should be rare — but if Brad wants belt-and-braces, the backoff
  could be shortened for the mid-window case or the first retry could carry a smaller step. Not required;
  the current bounded 3 s on an anomaly is defensible and strictly better than the old
  hammer-3x-then-standdown.

## 3. T-4 expiry — covered, but FROZEN-DOC DRIFT for Brad

- With `EXPIRATION_GRACE_S = 60` a rest can legitimately live to T-4 if the process crashes between T-5
  and T-4. The startup sweep `cancel_stale_open_orders` (executor.py:801, called from run_v32.py:1687
  in the armed block) GETs resting orders and DELETEs any `KXBTC*` carrying our `v32-` coid (or no
  coid), shard-aware — so any leaked T-4 rest is swept at the next wake, and it auto-expires at T-4
  regardless (well before the next `:40` wake). Coverage intact.
- **DRIFT (report-only — do NOT edit the frozen file):** `pilot/ceremony/v32_falsifier.md` is STATUS
  FROZEN and (correctly) untouched by this PR. Its "FIRST ARMED WINDOW MUST CONFIRM" item #1 (line 167)
  still reads verbatim:

  > 1. A GTC rest carries `expiration_time` (Unix seconds) and the venue ACCEPTS it (auto-expires at T-5).

  The code now auto-expires at **T-4**. The editable `pilot/ops/V32_ARMING.md` copy of the same
  confirm list was updated to T-4 ("if a rest lingers past T-4 … STOP"), but the frozen ceremony copy
  now contradicts the code. An operator applying the frozen falsifier verbatim on the first re-armed
  window could read the (correct, by-design) T-4 expiry as a FAIL against "auto-expires at T-5". The
  build report states the falsifier was not edited but does not call out that its confirm item now
  disagrees with the code. **Action for Brad:** knowingly accept the drift, or re-issue confirm item #1
  under Registration to say T-4. This is Brad's line to touch, not the reviewer's or builder's.

## 4. Tests — PASS

The 6 new tests exercise the real branches with documented venue shapes (404 body
`{"error":{"code":"not_found"}}`; GET order with `status` + `fill_count_fp`/`remaining_count_fp` matching
`parse_order_status`, which prefers the `_fp` names). The incident fixture is used (real order id,
coid, `expiration_epoch`, 6 records, 508 bytes, no holdout data). Coverage:
- (a) incident replay: 404 → stale `resting` → backoff → `canceled` ⇒ `via status`, no alarm/stand-down,
  first sleep asserted `== CANCEL_BACKOFF_S[0]`.
- (b) 404 → `executed` with `fill_count_fp 1.00` ⇒ filled-before-cancel booked once, `path cancel_race`.
- (c) 404 → `resting` throughout ⇒ `cancel_failed` + stand-down preserved; `1 + CANCEL_RETRY_ATTEMPTS`
  deletes; **`rec_sleeps == list(CANCEL_BACKOFF_S)`** — the injected sleep sequence asserted exactly.
- (point 2) cancel at `NEW_EXP + 1` with `expired` status ⇒ `via expired`, `cancels_expired == 1`.
- (d) create body `expiration_time == close - 300 + 60` and `RestRecord.expiration_epoch == NEW_EXP`.
- (e) `len(CANCEL_BACKOFF_S) == CANCEL_RETRY_ATTEMPTS` and values `[0.25, 0.75, 2.0]`.

The one gap worth naming (non-blocking): no test asserts the **immediate first status GET carries no
sleep** (i.e. that the 2xx-adjacent / first-check path is un-slept) beyond it being implied by (c)'s
exact 3-element sequence. (c)'s equality assertion does in fact rule out a pre-loop sleep, so this is
covered transitively.

## 5. Ledger / report — PASS

`build_v32_ledger_row` adds `cancels_via_status` / `cancels_expired` as keyword params defaulting to 0
and always writes them (`int(...)`). `run_v32._compute_money_math` reads them off the executor with
`getattr(executor, ..., 0)`. No code path INDEXES these keys out of a ledger row (`grep` for
`["cancels_…"]` / `.get("cancels_…")` across `pilot/**.py` returns only the writers) — so the
pre-existing 09-14/15 JSONL rows that lack the fields break nothing. Same additive pattern PR #46 used.

## 6. Nits

- `via` string rename `status_terminal`/`status_terminal_retry` → `status`/`expired`: fully removed, no
  test or ledger field pinned the old strings (confirmed by grep). If any external log parser keys on
  the old strings it would silently miss — the build report already flags this as an open question;
  agree it is low risk.
- `CANCEL_BACKOFF_S[min(i, len(CANCEL_BACKOFF_S) - 1)]`: the `min` is a no-op today (test (e) pins the
  lengths equal) but a sound guard if `CANCEL_RETRY_ATTEMPTS` ever exceeds the tuple length. Keep.
- `RestRecord.expiration_epoch` docstring/comment says it "lets the cancel path tell an
  `expired_at_quote_end` terminal status from a plain cancel" — accurate.
- Comment accuracy across executor.py docstrings, the ARMING doc, and PLAN_V32 all now consistently say
  "T-4 / EXPIRATION_GRACE_S past the quote end, crash backstop, quote-end cancel primary". Consistent.
- Build report honesty: accurate. The "853 passed" reconciles (see Tests). The secondary S4-baseline
  finding is a clean report-only observation with no code change — good discipline; it is out of scope
  for this PR and correctly left for Brad.

## Residual (pre-existing, not a regression)

A FALSE-terminal status read (venue reports terminal while the order in fact still rests) would free a
place and exclude that oid from the next `_pre_place_invariant`. This is the inverse of the incident
(which was stale-`resting`) and is identical exposure to the pre-existing 2xx path — terminal is a
sticky end-state, so a false-positive terminal is far less likely than the stale-resting we saw, and
the T-4 auto-expiry + startup sweep bound any leak. Not introduced by this PR; noting for completeness.

## Bottom line

APPROVE WITH NITS. Merge is safe. Before / at the first re-armed window, Brad should reconcile the
FROZEN falsifier confirm item #1 (T-5 → T-4) so the ceremony check does not contradict the code.
