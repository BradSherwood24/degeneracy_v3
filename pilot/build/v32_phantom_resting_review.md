# Review — PR #56 `fix/phantom-resting` (venue-truth invariant vs read-path phantoms)

Reviewer: Opus 4.8 (Fable-delegated). Worktree `dv3_wt_review` (NOT live tree).
Base: `git diff origin/main...HEAD` @ tip `448c1b4`, off `origin/main` @ `9c048b3`.
Scope reviewed: `pilot/service/v32/executor.py` (invariant path + `_finish_cancel` + counters),
`ledger.py`, `report.py`, `run_v32.py`, `pilot/ops/V32_ARMING.md`, tests + fixture.

## Verdict: APPROVE with one non-blocking money-reconciliation recommendation (fast-follow).

The incident fix is correct, minimal, and the money-safety-critical property — never PLACE a new rest
while one of ours is *genuinely* resting — is preserved. The one gap found is a narrow ledger
under-count (a fill on an *unknown* stray silently dropped), not a double-exposure or a loss, and is no
worse than PR #46 except that PR #46 would have alarmed. I recommend it as a fast-follow, not a blocker.

Receipts: full suite `860 passed, 2 skipped, 2 errors`; the 9 new phantom tests all pass
(`test_v32_phantom_resting.py::… 9 passed`). Both errors are `test_quintile.py` FileNotFoundError on
`historical-data/15-minute/markets/2026-06-11.jsonl`; both skips are `test_box_golden.py` "historical-data
absent". All 4 non-passes are data-absence in this detached worktree, unrelated to the diff.

---

## 1. Money safety (the 09-14 21-stray-rests class)

**Core property preserved.** A survivor stray whose DELETE returns `2xx reduced_by > 0`, or whose
order-status is still `resting`, is a REAL violation → cancel + alarm + stand-down + NO place
(`test_unknown_stray_rechecked_then_genuine_stands_down`, `test_stray_cancel_404_but_status_still_resting_is_a_violation`).
`rest_invariant_violations` counts only these. So the invariant still refuses to double-place against a
genuinely resting order. Good.

**(a) 2xx reduced_by == 0 (nothing pulled — the order had already left the book):** SPLIT VERDICT.
- **Step-1 phantom filter (`_filter_phantoms`, the incident fix) is money-safe.** It only drops a listed
  order whose *own RestRecord* shows `cancel_confirmed_ts` set AND `status in ("cancelled","filled")`
  within `CANCEL_SETTLE_S`. Those orders already ran through `_finish_cancel`, which for `filled > 0`
  sets `status="filled"` and appends the fill to `self.fills` (`path:"cancel_race"`, maker fee 0,
  de-duped by order_id). So if such an order left the book by FILLING, its fill was already booked before
  it could be filtered. Verified in `_finish_cancel` and `_resolve_cancel_success`
  (`filled = max(count - reduced_by, filled_status)` → `reduced_by 0` on our own order yields
  `filled = count`, booked).
- **Step-3 cancel-time confirm of an UNKNOWN survivor is the gap.** For a survivor NOT in our RestBook
  (`attribute(order_id=…)` → None), the code does its own DELETE and, on `2xx` with
  `rb is not None and rb <= 0`, declares `phantom = True`, journals `rest_invariant_phantom`, and
  PROCEEDS — **without cross-checking order-status for a `filled_count` and without booking any fill.**
  A `2xx reduced_by == 0` is ambiguous from the DELETE alone: the order may have left the book by
  EXPIRING/CANCELLING (truly nothing to book) OR by FILLING (an unbooked position). The same applies to
  the `404 + status terminal` branch: `_TERMINAL_STATUSES` includes `"executed"` (a filled order), so a
  filled unknown stray → `_status_confirms_gone` True → phantom → proceed, fill dropped.
  Contrast the normal cancel path (`_resolve_cancel_success`), which cross-checks
  `_confirm_cancel_filled` (status GET) and takes `max`, so a race fill is never under-counted.
  **Impact:** ledger under-counts a fill that actually happened on an order our internal state had lost
  track of. It does NOT create a duplicate order and is not a loss (a fill is +value); it is a
  reconciliation/ledger-honesty gap. It is narrow: it requires an order matching our `v32-` coid prefix
  that is absent from RestBook AND fills exactly during the invariant window. Relative to PR #46, PR #46
  would have stood down + alarmed on any such stray (prompting human reconciliation); the new path
  proceeds silently for the `rb<=0` / `404+terminal` sub-cases. There is no test for this sub-case
  (grep of the test file: no `reduced_by 0`, no `executed`, no fill-booking assertion in the invariant
  path).
  **Recommendation (fast-follow, non-blocking):** in the step-3 phantom branches, before declaring a
  phantom, cross-check `order_status(order_id).filled_count` (as `_resolve_cancel_success` does) and, if
  `filled > 0`, route the fill through the booking path (or route the whole survivor through
  `_finish_cancel`) so a stray that left the book by FILLING is booked, not dropped; at minimum raise a
  reconciliation alarm on a `2xx reduced_by == 0` / `status executed` survivor so a real fill is never
  swallowed without a human seeing it.

**(b) PR #50 status-truth confirms (`via:"status"/"expired"`):** SAFE. Both route through
`_finish_cancel`, which sets `cancel_confirmed_ts` and books any `filled_count` race fill. Terminal is
terminal; correctly treated as engine truth.

**(c) Partial fill:** SAFE, no `count == 1` assumption in the money path. `_finish_cancel` sets
`status="filled"` for any `filled > 0` and books the partial fill; `_resolve_cancel_success` uses
`max(0, count - reduced_by)`. A partially-filled-then-cancelled order is correctly not-resting and its
fill is booked before any filtering.

**(d) The 5 s window — could a confirmed-cancel order be re-listed AND genuinely resting?** SAFE, and
5 s is a sound bound. `_filter_phantoms` matches by exact `order_id`; Kalshi terminal states
(`executed`/`canceled`/`cancelled`/`expired`) are permanent and never reinstated, and a genuinely new
rest is a NEW order_id. So the only order the filter can match is the exact order we already confirmed
gone — which cannot also be genuinely resting. 5.0 s comfortably exceeds the measured 0.890 s phantom
age and the ~1 s PR #50 read-lag class, with headroom, while staying short enough that a stale-beyond-5 s
confirmed order is (correctly) re-examined rather than blindly trusted
(`test_confirmed_cancel_older_than_settle_is_not_a_phantom`). No lower-bound (`age >= 0`) guard exists,
but a negative age implies a place-ts before the confirm-ts (clock skew / out-of-order), which cannot
describe a real still-open order; benign.

**(e) Re-read returns None (venue unreadable) → PROCEED. RECOMMENDATION: ACCEPTABLE as-is; do NOT fail
closed.** Rationale:
1. Failing closed converts every transient resting-LIST read blip into a missed hour — the exact
   false-positive-stand-down class this PR exists to kill. A read-path that lags ~1 s and occasionally
   errors is expected; a strategy that self-DoSes on it is the more expensive failure mode.
2. The residual risk (proceeding while a real unknown stray rests, unread) is BOUNDED by the per-order
   auto-expiry: every rest carries `expiration_epoch` (`EXPIRATION_GRACE_S` past quote-end), so a truly
   orphaned order self-clears at/near quote-end even if never cancelled.
3. It is NOT a new posture — PR #46 already proceeded on an unreadable *initial* read. The new code
   preserves that exact tradeoff one step deeper (the re-read), so it does not widen the accepted risk
   envelope beyond a bounded, documented one.
   *Optional middle ground (not required):* on an unreadable re-read where the first read showed an
   unknown survivor, hold THIS place and retry on the next quoting tick (fail-closed for exactly one
   tick). A single skipped tick is cheap and would avoid the rare "place while a real unread stray
   rests" double-exposure. I would take proceed-as-is for now given (2)+(3); flag the one-tick hold as
   a possible later hardening.

## 2. Real violation path preserved — CONFIRMED

`2xx reduced_by > 0` or `status "resting"` on the stray → `real_strays` → `rest_invariant_violations++`,
`rest_invariant_violation` journal, `A` alarm, `stand_down_reason = "rest_invariant_violation"`, and the
`OrderCancelled(filled_0)` short-circuit (no place). Byte-for-byte PR #46 semantics on the genuine case;
only the counter's meaning tightened to "REAL violations only". Tests cover both the `2xx reduced_by>0`
and `404 + still resting` genuine paths, asserting the shard-aware cancel (`cancel_path("ghost-1", 2)`),
`len(posts) == 0`, and the alarm.

## 3. Interaction with PR #50 backoff / WS reader — ACCEPTABLE

The invariant's own stray-cancel does a SINGLE `writer.rest_delete` plus, on 404, a SINGLE
`_status_confirms_gone` status GET. It does NOT enter the `_cancel_nonok` `CANCEL_BACKOFF_S`
(0.25/0.75/2.0 s) retry ladder, so no multi-second stall is added inside the invariant. Worst-case added
latency before a PLACE ≈ `INVARIANT_RECHECK_S` (0.5 s, once) + one DELETE RTT + one status-GET RTT per
survivor. The common incident path (step-1 phantom filter) sleeps `[]` — zero added latency, asserted by
`test_invariant_sleep_sequence`. Because the executor blocks the WS reader (per project execution
physics), the 0.5 s recheck is a real WS stall, but it fires ONLY on an unknown-survivor pass (rare), not
on the common just-confirmed-cancel phantom. Acceptable; noted.

## 4. Tests — 9 tests, real fixture replayed, coverage good with two omissions

`python -m pytest pilot/tests/test_v32_phantom_resting.py -v` → 9 passed. Coverage:
- phantom-filter proceed + counters + no sleep (a): `test_incident_phantom_confirmed_cancel_filtered_place_proceeds`.
- unknown survivor recheck → genuine violation, shard-aware cancel, no place (b):
  `test_unknown_stray_rechecked_then_genuine_stands_down`; plus clear-on-reread proceed.
- survivor 404 + status terminal/not-found → phantom proceed (c):
  `test_unknown_stray_cancel_404_status_gone_is_phantom_place_proceeds` and `…status_not_found…`; plus
  404 + still-resting → violation.
- sleep sequence asserted (d): `test_invariant_sleep_sequence` (`[]` phantom pass, `[INVARIANT_RECHECK_S]`
  survivor pass).
- fixture (e): `test_incident_fixture_slice_present_and_shows_phantom_signature` replays the real slice
  (3,499 bytes < 200 KB, 46 records), asserting a `cancel_confirmed` order re-appearing in the
  `rest_invariant_violation` resting list, `rest_invariant_cancel` status 404, and confirm→re-listing
  `0 < age < CANCEL_SETTLE_S` (the measured 0.890 s).
- settle-window boundary: `test_confirmed_cancel_older_than_settle_is_not_a_phantom`.

**Omissions (nit-level):**
- **No report-render / old-rows-parse test in the new file.** The build report claims "report render"
  coverage, but the diff's new tests do not call `build_report`/`_render`/`build_v32_ledger_row`. Old
  rows parse via `.get(...,0)` defaults and the existing report suite stays green, so backward-compat
  holds in practice, but the new render line
  (`rest_invariant: violations=… phantoms=… (read-path lag, no stand-down)`) is not asserted. Suggest a
  one-line report-render assertion.
- **No test for the item-1(a) money gap** (unknown survivor whose DELETE `reduced_by == 0` / status
  `executed` because it FILLED → fill should be booked/alarmed). This is untested because the code drops
  it; see recommendation in §1(a).

## 5. Minimality vs `feat/amend-first-replace` — localized, rebase should be straightforward

The diff is confined to: the constants block (`CANCEL_SETTLE_S`, `INVARIANT_RECHECK_S`), one
`RestRecord` field (`cancel_confirmed_ts`), three `__init__` counters, two new helpers
(`_filter_phantoms`, `_status_confirms_gone`), the `_pre_place_invariant` body, and ONE line in
`_finish_cancel`. It does not touch the place/replace path. Likely textual conflict hot spots with an
amend-first branch that also edits `executor.py`: (1) `_finish_cancel` (an amend path would also route
cancel-confirmation bookkeeping through it — most probable conflict, but both edits are additive), and
(2) the `__init__` counter block. Both are additive and mechanically resolvable. Per the build report,
amend-first reduces the phantom *rate* but not the *class*; this invariant is the durable guard and the
two are independent. No structural entanglement found.

## 6. Nits
- `rest_invariant_rechecks` is threaded into the ledger row but NOT surfaced in `report.py` totals/render
  (only `violations` + `phantoms` are). Minor: rechecks is a useful diagnostic; consider adding to the
  render line or dropping the ledger field if not surfaced.
- `_filter_phantoms` has no `age >= 0` lower-bound guard (see §1(d)); benign given a monotonic event
  clock, worth a one-word comment.
- Naming is consistent with prior counters (`cancels_via_status`, etc.); no issues.

## Blocking items
None.

## Recommended fast-follow (non-blocking)
Cross-check `order_status().filled_count` (or route through `_finish_cancel`) in the step-3 phantom
branches so an unknown stray that left the book by FILLING has its fill booked, or at minimum alarms —
so no real fill is swallowed silently (§1(a)). Consider the one-tick fail-closed on an unreadable
re-read (§1(e)) and the two test omissions (§4) at the same time.

---

# Delta re-review — commit `0d996b0` (addresses §1a fast-follow)

Re-reviewed `git diff 448c1b4..0d996b0` (executor invariant path, report, tests). The builder took the
§1a fast-follow. Scope of change: `_status_confirms_gone` → `_status_says_gone` (now static, returns via
an already-fetched `OrderStatus` so the caller reads `filled_count`); step-3 now cross-checks
`filled_count` before ANY phantom classification (both the `2xx reduced_by==0` and the `404 + terminal`
sub-cases); a TRACKED filled stray is booked via `_finish_cancel(..., via="invariant_fill")` and routed
to the core as the hour's entry with the PLACE short-circuited and NO stand-down; an UNTRACKED/unpriceable
filled stray raises `rest_invariant_unbooked_fill` alarm + stand-down; report renders `rechecks`; 4 new
tests (13 total in `test_v32_phantom_resting.py`).

## Verdict: APPROVE. The §1a gap is closed; no new blocking items. Ship.

Receipts: `test_v32_phantom_resting.py` → **13 passed**; full suite **864 passed, 2 skipped, 2 errors**
(the +4 are the new tests; the 2 errors remain `test_quintile.py` FileNotFoundError on `historical-data`,
unrelated to the diff). The two prior nits are also resolved: `rest_invariant_rechecks` is now surfaced
in the report render, and there is now a report-render test.

## Check 1 — booked-fill routing cannot double-book / cannot take wings twice: PASS

The routed fill uses the exact established cancel-race mechanism, and dedupe holds on all three fill
paths by `order_id`:
- **Money ledger (`executor.fills`).** `_finish_cancel` books only if
  `rec.order_id not in self.booked_rest_oids`, adding the id on book. The driver's `_record_fill`
  (the WS / poll paths) guards on the SAME `booked_rest_oids` set (`if rec.order_id in booked: return`)
  and appends to the SAME `executor.fills` list. So the invariant books once via `_finish_cancel`; any
  later WS echo or status-poll of that same fill is dropped by the shared id set. No double economic
  booking. `test_known_stray_that_filled_is_booked_and_routed_not_dropped` asserts exactly one rest fill
  (`len(rest_fills) == 1`), booked at the order's price (0.48), path `cancel_race`.
- **Core state / wings.** The routed `OrderCancelled(filled>0)` flows through `_pump` → `decide_v32` →
  `_apply_cancelled`, which books the set and calls `_wing_step` ONLY under
  `event.filled_count_before_cancel > 0 AND st.rest_fill is None`. The `st.rest_fill is None` gate is the
  single-set latch; a second fill event (WS echo, another cancel) finds `rest_fill` already set and
  neither re-books nor re-wings. The F-1 late-fill hook `book_late_rest_fill` is explicitly idempotent
  (`if st.rest_fill is not None: return st, []`), closing the WS-echo-after-routed-fill vector. Wings
  therefore fire exactly once for the booked stray.
- Not a new vector: `via="invariant_fill"` reuses the identical routing and `path:"cancel_race"` money
  tag as the pre-existing, integration-tested cancel-race fill; the delta adds no new booking channel.

## Check 2 — booked stray fill latches one-set-per-hour: PASS

The one-set rule is latched by `st.rest_fill` in the pure core. Once the routed `OrderCancelled(filled>0)`
sets `rest_fill` (via `_apply_cancelled`), every downstream path that could open a second set is gated:
`_apply_fill` (`is_ours and st.rest_fill is None`), `_apply_cancelled` (`... and st.rest_fill is None`),
and `book_late_rest_fill` (idempotent no-op). The invariant additionally short-circuits the in-flight
PLACE (returns the fill events INSTEAD of an `OrderAck`), so the candidate re-place is never sent to the
exchange (`test_known_stray_… asserts "v32-new" not in rest_book`, `len(w.posts) == 1`). Result: the
surprise fill becomes the hour's single set, hedged by one wing take; no second rest, no second set.
Minor coverage note (non-blocking): the executor unit tests prove the executor returns the right events,
but do not drive the full `_pump`/`decide_v32` loop to assert the routed fill yields exactly one
`TAKE_WINGS` and flips the latch at the core level — that behavior is inherited from the shared
cancel-race path, which is integration-tested elsewhere (`test_v32_phase3_ledger`, `…quote_end_race`).

## Check 3 — builder's judgment (continue vs stand down after a bookable surprise fill): money-safe; PREFER CONTINUE

Both branches are money-safe, and the builder's split is the correct one:
- **Tracked / priceable fill → CONTINUE (book + wing, no stand-down): preferred and safer.** A surprise
  fill on an order whose price we know is economically identical to a cancel-race fill — the strategy's
  designed steady state (rest fills → take wings → hour done). Booking at the retained `rec.price` and
  hedging via the standard wing path leaves a properly hedged one-set. Standing down INSTEAD would strand
  an unhedged bucket-NO (the exact F-1 failure the late-fill hook exists to prevent) or force a flatten —
  and flattens were a documented source of unbooked losses in the first armed campaign. So continuing is
  the more conservative choice here, not the riskier one.
- **Untracked / unpriceable fill → STAND DOWN (after alarm): correct fail-closed.** You cannot honestly
  book or hedge a fill you cannot attribute or price; standing down + alarming hands it to a human rather
  than guessing a price or leaving a silent unhedged leg. Aligns with house law (no silent unhedged leg;
  honest fill conventions).

Preference: keep the builder's design as-is. One belt-and-suspenders suggestion (non-blocking): the
`rest_invariant_unbooked_fill` alarm is the money-critical signal — confirm it is on the ops page's
watched-alarm list so an untracked surprise fill is reconciled promptly, not just stood down.

## Check 4 — tests exercise both branches: PASS

- Tracked booked/routed: `test_known_stray_that_filled_is_booked_and_routed_not_dropped`
  (2xx reduced_by 0 + status executed on a RestBook order → booked at 0.48, routed, no stand-down,
  single fill).
- Untracked unbooked, 2xx path: `test_unknown_stray_2xx_reduced_by_0_but_executed_fill_alarms_stands_down`.
- Untracked unbooked, 404 path: `test_unknown_stray_404_status_executed_fill_alarms_stands_down`.
- Report render: `test_report_render_surfaces_phantoms_and_rechecks`.
Both the book-and-continue and the alarm-and-stand-down branches are exercised, across both the 2xx-rb0
and 404 gone-ness signals. The original 9 (genuine violation, clean phantom, recheck, fixture, settle
boundary) remain green.

## Residual / carried-over (all non-blocking)
- Item 1(e) (unreadable re-read → PROCEED) is unchanged and still ACCEPTABLE per the original review
  (per-order auto-expiry bound + PR #46 posture).
- Integration-level assertion of the routed fill's single TAKE_WINGS at the core is inferred from the
  shared cancel-race path (Check 2 note).
- Suggest verifying `rest_invariant_unbooked_fill` is on the watched-alarm ops list (Check 3).

## Blocking items (delta): none.
