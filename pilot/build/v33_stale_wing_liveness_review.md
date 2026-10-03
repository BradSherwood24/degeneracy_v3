# V3.3 gate D (stale-wing liveness) + gate F measurement -- adversarial review

*Opus 4.8 reviewer, 2026-10-03. Branch `fix/v33-stale-wing-liveness` (PR #120), commits 7c917cd / d1bb92c /
9f513fc, reviewed at merge-base with `main` 2ed9ff5. I did not write this PR. Worktree
`C:\Users\Brads\Python_stuff\dv3_wt_v11`. Failure mode this project pins on: a rest resting unhedged at the
venue.*

## Verdict: APPROVE WITH NITS

The re-place race that fed the 02:00Z naked fill is closed on this branch: every cancel-all that can be
followed by a re-place now tracks its cancels (`_cancel_all_then_replace`) and the place path waits for
`outstanding_cancels == 0`, which I confirmed by mutation (reverting the tracking re-raises *"PLACE_REST while
the venue still holds 11 of our rests"* -- the incident). The liveness predicate is fail-closed, v32 is
byte-identical, the loader fails closed on bad bounds, and the gate F measurement reproduces on the real
journals. No BLOCKER and no self-contained code DEFECT found; the one real residual (the `outstanding_cancels`
count can be released one confirm early) is correctly owned by sibling PR #119 and I verified the two PRs merge
clean and together close it. Everything below is a NIT or a cross-PR / ceremony recommendation. I changed no
code; the review branch equals the PR branch plus this document.

---

## 1. The race is closed (verified)

`_cancel_all(` call sites in `pilot/service/v33/core.py`, classified:

| site | path | tracked? | correct |
|---|---|---|---|
| core.py:1248 | `_book_rung_fill` fill-bucket-unknown fail-closed | untracked | ✓ terminal (`stood_down=True, rest_allotment_done=True` set first) |
| core.py:2283 | `_converge` `rest_allotment_done` | untracked | ✓ terminal (stop quoting) |
| core.py:2289 | `_converge` `replace_rate` alarm | untracked | ✓ terminal (`stood_down=True`) |
| core.py:2331 | stale-wing hold-expiry | `_cancel_all_then_replace` | ✓ resumable -> tracked |
| core.py:2340 | generic no-quote (`no_spot_bucket` / `stale_bucket` / `n_below_min` / hold-disabled / `past_quote_end` / `stood_down`) | `_cancel_all_then_replace` | ✓ resumable -> tracked |
| core.py:2359 | bucket change | `_cancel_all_then_replace` | ✓ (was already tracked pre-PR) |

The guard `if st.awaiting_replace and st.outstanding_cancels > 0: return` (core.py:2365) now covers all of them.
`_cancel_all_then_replace` sets `awaiting_replace` only when `had_orders` (never clears it), so a no-op cancel
over an empty ladder cannot drop an in-progress wait. The `assert not (rolls_in_flight and awaiting_replace)`
invariant (core.py:558) holds because `_cancel_all` clears `rolls_in_flight` and `awaiting_replace` only
coincides with an empty ladder until `_place_all`/place clears it; the full suite runs `check_invariants` on
every replay event and passes.

**Mutation check (I ran it).** Reverting `_cancel_all_then_replace` to the pre-fix untracked `_cancel_all(st)`
on a scratch copy and running the gate-D tests:
`test_hold_expiry_cancel_all_waits_for_every_confirm_before_replace`,
`test_stale_bucket_cancel_is_tracked_and_resume_waits_for_confirms`, `test_n_below_min_cancel_is_tracked`, and
`test_golden_02z_old_1s_per_strike_predicate_flaps_on_the_same_sequence` all FAIL with
`AssertionError: PLACE_REST while the venue still holds 11 of our rests  assert 11 < 11`. Restored to clean.

**The `#115` deferred-cancel / pending-rung path (traced).** A rung cancelled while its create is still
in flight is **not** counted (`_cancel_all` only increments `n_live` for `order_id is not None`) and leaves no
`cancel_ctx` entry. When its create acks and the deferred DELETE confirms, the resulting `OrderCancelled` carries
a venue id that was never counted. It does **not** rest unowned: the executor's deferred cancel DELETEs it; a
rejected create leaves nothing at the venue. So gate D introduces no new naked-rest path here. It does expose
the premature-decrement residual below.

## 2. Residual: `outstanding_cancels` is a count, closed by #119 (cross-PR) -- DEFECT if armed on #120 alone

On this branch `_apply_cancelled` is **unchanged** (`git diff origin/main...HEAD` touches neither
`_apply_cancelled` nor `_apply_fill`). Its decrement (core.py:1071) fires on *any* `OrderCancelled` reaching
that line. So an untracked cancel event -- a pre-flight-rejection `OrderCancelled(order_id=None)`, a no-op
cancel, the late DELETE of a never-counted pending rung, or a duplicate confirm -- can drop
`outstanding_cancels` one early while `awaiting_replace`, releasing `_place_all` one confirm before the venue
is clear. That is the exact hole gate D is meant to close, so **#120 alone does not fully close it.**

#119 (`origin/fix/v33-hedge-owned-fills`) rewrites the decrement to
`if (ev_oid is not None and (live_order is not None or ev_oid in st.cancel_ctx) and st.outstanding_cancels > 0)`.
Since `_cancel_all(track_outstanding=True)` counts only `order_id`-bearing orders and remembers each in
`cancel_ctx`, every counted cancel's confirm (and only those) decrements. A pending rung's late DELETE, a
rejection, or a no-op (none in `cancel_ctx`) no longer decrements. **That closes the residual.**

- **Merge compatibility: CLEAN.** `git merge-tree --write-tree origin/fix/v33-stale-wing-liveness
  origin/fix/v33-hedge-owned-fills` exits 0 with no conflict markers. #120 edits `_converge` / `_cancel_all` /
  the wing gates / V33State (`strike_feed_ts`); #119 edits `_apply_cancelled` / `_apply_fill` / `_book_rung_fill`
  / V33State (`fill_seen_by_coid`). Disjoint regions; both add distinct V33State fields.
- **Order-independent:** the merged `_apply_cancelled` is #119's version regardless of merge order, so the
  end-state closes the hole either way.
- **Neither alone is sufficient:** #120 alone has the premature decrement above; #119 alone has no
  `_cancel_all_then_replace` and no wait gate, so no race protection at all. **Re-arm (gates A-D) must land
  both.** This is DRY-safe to ship #120 first: in DRY the `FrozenExecutor` emits no untracked `OrderCancelled`
  (it always acks; no pre-flight rejections), so the hole cannot fire DRY, and re-arm is gated on #119 anyway.
- I did **not** re-implement #119's fix here: doing so would make the two PRs conflict textually. The right
  action is the merge-order requirement above.

## 3. Liveness predicate (verified fail-closed)

- `_stamp_strike_feed` (core.py:770) stamps `strike_feed_ts` from `event.book_ts` (the frame's OWN server ts)
  on any `("strike", _)` fold -- so a lagging connection (frames arriving, book_ts old) ages out. The
  `else event.server_ts` fallback is dead on the live path (see snapshots below) and harmless.
- **Snapshots do not drive.** `V32Recorder._drive_book` (run_v32.py:1231) calls `on_book_update` only when
  `_parse_server_ts` is non-None, and KXBTCD `orderbook_snapshot` frames carry no ts: I counted the 15:00Z
  journal -> **strike snapshots with ts: 0 / without: 3008; strike deltas with ts: 720858**. So snapshots fold
  the book but never reach `decide_v33`/`_stamp_strike_feed`. The measurement replay's "deltas only" assumption
  is correct, which is why the OLD replay reproduces the journaled hold counts. A strike that only snapshots
  (reconnect, no deltas yet) reads dead = fail-closed, which is right; at window open the strike feed is already
  delta-active (p99.9 inter-frame 0.13 s) so no spurious open-time stand-down.
- **Negative-age can't pin alive forever.** `now` is the monotone eval clock = max server ts over all driven
  frames (strikes included) plus 0.5 s wall ticks, so `now >= strike_feed_ts` always and age >= 0; if strikes
  stop, the wall ticks keep advancing `now`, so age crosses `strike_feed_dead_s` -> dead. A never-seen feed
  (`strike_feed_ts is None`) -> `_fresh` returns False -> dead. Confirmed.
- `strike_feed_dead_s = 4.0` sits above the max live-feed eval-clock age (2.96 s) by 35% and below every
  stall/lag window's max (>= 8.45 s). Sound against the measurement.

## 4. Wing-take gate (verified)

- v32 byte-identical: `git diff origin/main...HEAD -- pilot/service/v32/` is empty; no v32 test changed.
- `_WingLawView(freshness_max_age_s=wing_book_max_age_s)` is a duck-typed stand-in for `params`; `_compute_W`
  and `_wing_prices` read only `params.freshness_max_age_s`, so the reuse is exact.
- The imported shadow still uses 1.0 s: `_shadow_complete` is called with the real `params`
  (core.py:712/720/747), not the view -- the falsifier capture-ratio comparator is unchanged.
- The 30 s take bound: deltas are the only book updates, so a quiet non-suspect book carries its true current
  prices; a missed delta sets `suspect` (book.py:164 malformed; a seq gap closes the WS -> reconnect -> the
  feed-dead gate catches the stall, and the fresh snapshot clears suspect). 30 s is 2.2x the longest quiet seen
  on a live feed (13.8 s). Refusing the take instead would leave the fill naked -- the 02:00Z failure -- so
  pricing off a quiet-but-live book is the correct, safer branch.

## 5. Measurement honesty (reproduced)

Ran `pilot/build/v33_stale_wing_measure.py` over `journals_v33` myself (`--feed-dead-s 4.0 --wing-max-age-s 30.0`):

- **02:00Z:** new-replay full **0/0** (old-replay full 47/5). Matches.
- **10-02 15:00Z (real stall):** new-replay full **20/11** -- still stands down. Matches.
- **All windows:** new-replay **holds 395 / cancels 299** (report 395 / 299). My run counted 86 windows, not
  the report's 85, because another window has closed on 10-03 since; it is a live window and added **0**
  new-replay holds (old-replay went 3140 -> 3213 from that one window), which strengthens the finding.
- **Spot-check (journaled column by hand):** raw record counts 20261002T220000Z = 39 hold / 39 resume / 0
  cancel and 20261003T020000Z = 5 / 4 / 1, exactly the report's table.
- **Thinning is conservative:** `extract_fixture` only drops frames (keeps the first after `thin_ms`), so the
  gap between kept frames is >= the real gap -- the thinned stream can only hold *more* than the real one, so a
  thinned "zero holds" proves the real sequence holds zero. Correct direction for the no-hold claim.

## 6. Tests (reviewed)

- Full suite in the worktree: **1487 passed, 1 skipped in 56.8 s** (the builder's 1482/5 differs only in
  Rung-1 module-level skips; the census CSV is present here so 4 of those run -- 1488 collected either way).
- The 14 adjusted existing tests are legitimate: the `*=3600.0` tests disable the new gate to isolate unrelated
  behaviour; the 4 hold tests pin `strike_feed_dead_s=1.0` so their 2 s silence still reads "feed dead" and
  still exercises the *unchanged* hold state machine (hold -> resume / hold -> cancel / fill-during-hold).
- The 3 take-timing tests (`test_v33_fill_attribution` x2, `test_v33_fill_discovery_fractional`) collect both
  ticks because the take now fires one tick earlier off the quiet-but-live book -- a strict improvement (hedge
  sooner), not a hidden regression: `test_wings_solved_on_fill_bucket_strikes_not_spot_strikes` still asserts
  exactly one `TAKE_WINGS` with the right legs, so a missing/duplicate take would fail.

## 7. Findings

- **NIT-1 (diagnosability).** A dead-feed stand-down is not distinguishable in the journal from a
  missing/suspect/too-old-wing one: all four sub-causes collapse to `W is None` -> reason
  `"stale_or_missing_wing"` (run_v33.py:620-640). The information is *recoverable* from the raw journaled frames
  (the measurement tool computes exactly this distinction post-hoc), so it is not lossy and not a blocker. For
  the next incident it would help to annotate the hold/cancel payload with the sub-cause
  (`feed_dead` / `missing` / `suspect` / `wing_too_old`). I did not add it: it touches the action schema and the
  journal, which is higher risk than the recoverable-info benefit warrants late in review. Recommend as a small
  follow-up.
- **NIT-2 (fragility).** `_WingLawView` + `# type: ignore[arg-type]` works only because the reused v32 law
  reads exactly one param field. A future edit to `_compute_W`/`_wing_prices` that reads another field would
  `AttributeError` at runtime with mypy silenced. Acceptable while v32 is frozen; worth a comment that the
  view's field set is load-bearing. (The docstring already says so.)
- **NIT-3 (test assertion).** The fractional and missing-leg take-timing tests assert the take list is truthy /
  take[0], not `len == 1`; a duplicate-take regression would slip past *those two* (it is caught by
  `test_wings_solved_...`). Minor.

## 8. Params pin (recommendation; I did not edit ceremony/policy)

`strike_feed_dead_s = 4.0` and `wing_book_max_age_s = 30.0` are OPTIONAL keys with code defaults, absent from
the shipped JSON, so the frozen params sha `295590ce...` (asserted by `test_v33_falsifier_pins.py`) is
unchanged. The loader fails closed on `<= 0` / non-finite / `wing_book_max_age_s < freshness_max_age_s`
(range refusals), which is good -- but it does **not** pin the specific measured values: a future policy JSON
could set `strike_feed_dead_s = 3.9` (passes the guard) and silently change whether rests can go naked, with no
sha change. Under the house "registered specs rule," a safety value governing naked exposure should be pinned
and enforced, not left purely as an overridable code default.

**Recommendation to the orchestrator:** add a Registration line recording the two measured defaults
(4.0 / 30.0) and their provenance (`v33_stale_wing_measurement_2026_10_03.md`) so the agreed values are pinned
in ceremony even though the JSON/sha is untouched. Keeping the sha frozen is fine; the Registration line is what
enforces the spec. This is a ceremony action, not a code change -- flagged, not made.

## 9. Scope / house law

No `ops/`, `ceremony/`, `policy/`, `.env`, key, PEM, proxy or process touched. Both rosters DRY. The
measurement tool reads `journals_v33/` only (never `historical-data/`), so the sealed holdout
(2026-08-02..08-18) is not in any path; fixtures are from 10-02/10-03. No sealed data read.
