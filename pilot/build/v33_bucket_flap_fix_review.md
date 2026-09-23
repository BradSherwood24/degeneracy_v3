# V3.3 bucket-flap hotfix review — PR #89 (2026-09-23)

Reviewer: Opus 4.8 (Fable's delegated reviewer). Branch `fix/v33-bucket-flap`, head **c182f18**, base
`origin/main` **07a2f80** (full V3.3 merged). Worktree `dv3_wt_review`, branch `review/v33-bucket-flap`.

## VERDICT: APPROVE WITH NITS

The two fixes — spot-bucket-switch debounce+hysteresis, and the stale/missing-wing stand-down hold — are
correct, pure-core, fail-closed, well-tested, and dramatically cut the pathological flap churn the first
DRY window exposed. V3.2 is untouched (0 lines), the sha re-pins cleanly (prior shas kept, ceremony/arming
updated), the loader fails closed, and 243k×(book+echo) replayed events through the new core are
invariant-clean. Three NITs / questions for Brad, none blocking: the hysteresis estimator (centroid) is a
different, sometimes-disagreeing signal from the switch selector (`_select_spot`) and can strand the ladder
on the old bucket for a whole window; the stand-down hold opens a bounded naked-exposure window on a fill
during a persistent strike outage; and a small replay-fidelity note.

Receipts: full suite `1258 passed, 5 skipped, 0 failures/errors`; v33 collection `250`. Params sha
`3fe3919c5b31bbe9bd258fb7a82f5757a50ee0188b94c6bf0fe1f96f1fcb6aac` self-verifies; `PREVIOUS_..._L3` =
the old L3 sha; `bucket_switch_deb_ms=0`/`stand_down_hold_ms<0`/`2*hyst>=bucket_width` all fail closed.
`git diff 07a2f80..c182f18 -- pilot/service/v32/` = 0 lines.

---

## Replay of the full DRY journal (item 6) — what Brad will see next window

I replayed the real 07:00Z dry journal (`v33_dry_0700/...20260923T070000Z.jsonl.gz`, 741k records) through
the ACTUAL `decide_v33` with the new params, reconstructing books with `service.book.BookMirror` (the same
fold `service.replay` pins) and simulating a synchronous dry executor (ack every place, confirm every
amend/cancel) so the roll/bucket-change lifecycle completes. There were **no rung fills** in the window
(`max rungs_filled == 0`), so a book+lifecycle replay is faithful for the action counts. Feed window
T-950..T-250 (the quote window), 243,002 book updates fed; `check_invariants` after every event and every
echo — **zero assertions fired**.

| core | PLACE | CANCEL | AMEND | committed bucket switches |
|---|---|---|---|---|
| CONTROL (deb 0 / hyst 0 = pre-fix) | 264 | 264 | 212 | 23 |
| **SHIPPED (deb 3000 / hyst 15)** | **22** | **22** | **204** | **1** |
| DEBOUNCE-ONLY (deb 3000 / hyst 0) | 44 | 44 | 201 | 3 |

Reading: the bucket-flap churn is essentially eliminated — the shipped core commits **1** bucket switch
over the whole window (22 places = the initial 11-rung ladder + one legitimate re-placement) vs 23 switches
/ 264 places in the control. Debounce alone gets there (44 places / 3 switches); hysteresis blocks 2 of the
3 debounce-surviving switches (44→22). The AMEND count (~204) is the legitimate `n_top`-drift roll traffic
and is **unchanged** by the fix (control 212, shipped 204, live 202) — as it should be; the fix targets the
flap, not the rolls.

**Caveats on the absolute numbers** (be precise for Brad): my harness (a) clocks on the journal's
`local_ts`, not the live WS `server_ts`; (b) relaxes freshness so it isolates FIX 1 and does **not** re-fire
the 15 stale-wing stand-downs — so the control's 264 is the bucket-flip component only, not the live 418
(the ~154-order gap is the stale-wing re-place churn that FIX 2 removes, plus timing/echo differences); (c)
uses a synchronous echo, not the live async executor with pacing/partials. So the takeaway is the *shape*,
not the exact integer: **bucket-flip creates/cancels drop ~10-12x (264→22, or 44 debounce-only), the ~200
roll-amends persist, and FIX 2 removes the stale-wing re-place churn on top.** The authoritative micro-proof
is the committed fixture test (`0` vs `55` cancel-alls on the real 11-flip slice), which I re-ran green.

---

## Item-by-item verification

### 1. Debounce semantics — CORRECT
`_resolve_effective_bucket` commits a switch only when the candidate has been the resolved spot
CONTINUOUSLY for `>= bucket_switch_deb_ms` (a flip back to the ladder's bucket, or to a third candidate,
resets `pending_switch_since`) AND `_hysteresis_ok` latched at least once while pending. While pending the
effective bucket stays `rest_bucket_Sd`, so `n_top`/cap/W and all rolls/fills compute on the OLD bucket;
commit runs the existing (K-filled-capped, 2-slots preserved) cancel-all/place-all path exactly once. First
placement (`rest_bucket_Sd is None`) follows `raw_Sd` immediately. Verified by the 8 dedicated tests and by
the full replay (1 commit over a window that flapped 23× in the control). Flip-storm → 0 switches;
persist-3s-and-inside → 1; flip-back-at-2.9s → reset (0); parametric all green.

On the coordinator's "$300 move across two buckets in 4 s" question: each intermediate bucket that never
holds the resolved spot for 3 s continuously is never committed, so the ladder skips it and commits only the
final bucket once it satisfies deb+hyst — i.e. **1 commit, not 2**, and it ends on the right bucket. If the
middle bucket *did* hold spot 3 s + 15$ inside, 2 commits is correct (spot genuinely dwelt there). Both are
the intended, safe behaviour (a switch is only ever *delayed*, never spurious).

### 2. `_implied_spot` reliability — the main NIT (see NIT-1)
`_implied_spot` is the yes-mid-weighted centroid of the two-sided range buckets' centres. In the replay,
while a switch was pending the centroid-implied bucket disagreed with `_select_spot` (the switch selector,
highest single yes-mid) on **9,965** ticks — always by one bucket at the boundary (e.g. `_select_spot`
86400 while the centroid sat at ~86509 = bucket 86500). That is the hysteresis doing its job (smoothing a
noisy `_select_spot` flip), and it errs toward *fewer* switches (safe). But it means the switch **candidate**
and the switch **gate** use two different spot estimators, and a genuine spot parked 5-14$ inside a new
bucket (centroid there, `_select_spot` there) satisfies debounce yet is blocked by hysteresis for the whole
window — the ladder then quotes the adjacent (old) bucket for the entire window. Thin-ladder / one-mid case:
the centroid collapses to that bucket's centre (passes), and `_hysteresis_ok` explicitly falls back to
debounce-only when the centroid is unmeasurable, so it never *forces* a wrong switch — it can only *block*
one. See NIT-1.

### 3. Stand-down hold + the money path — CORRECT mechanics, one bounded risk (NIT-2)
On `stale_or_missing_wing` with a live ladder (or rolls in flight), the core holds the rests up to
`stand_down_hold_ms` (no places/rolls/cancels), resumes on fresh (emitting `..._resume`), else cancels-all
once at expiry (`..._cancel`). A rung fill during the hold books and spawns its wing batch independently of
`_converge`. **The money path, verified precisely:** the wing take waits for a fresh strike book; if strikes
refresh at any point before the settle cutoff the wings ARE taken (the `test_hold_rung_fill...` test and my
probe both show `TAKE_WINGS` on the resume tick). But if W stays missing for the rest of the window after a
fill during the hold, the batch is **never taken and is flagged `one_legged` at the settle cutoff**
(`t_to_close < no_orders_after_s_to_settle`) — i.e. the filled NO leg sits NAKED for the window. I confirmed
this directly (10 s of stale ClockTicks after a hold-fill → batch `taken=False`; near settle →
`one_legged=True`). This is bounded (fills can only enter this state during the ≤1.5 s hold), lever-gated
(`stand_down_hold_ms=0` restores immediate cancel), and flagged (`one_legged`, which the falsifier's
one-legged gate counts). See NIT-2.

Stand-down counter mapping in `run_v33._journal_action` verified: `stand_down_hold` and `stand_down_resume`
journal under distinct kinds and do **not** increment `_real_stand_downs`; only `stand_down_cancel` (and
other plain reasons) do — exactly the requested accounting.

### 4. Hold vs the roll — CORRECT
During the hold `W is None → n_top is None`. `_converge`'s hold branch returns before any convergence, so no
AMEND/PLACE with a None-derived price is emitted. `_recompute_context` refreshes the rung/E_rung labels only
`if n_top is not None`, so the labels are frozen (not recomputed against None), and `check_invariants` skips
the label checks when `n_top is None`. No assertion fired across the 10 s-stale probe or the 243k-event
replay. Confirmed.

### 5. Params — CORRECT
Defaults 3000 / 15 / 1500; each is a lever (0 restores the pre-fix behaviour, used by the updated goldens
and the immediate-switch core tests). Fail-closed: `bucket_switch_deb_ms/stand_down_hold_ms >= 0`,
`0 <= hyst` and `2*hyst < bucket_width` (verified: `max_amends... =0` style bad values raise
`V33ParamsInvalid`). New sha self-verifies; `PREVIOUS_V33_PARAMS_SHA256_L3` retained; ceremony
`v33_falsifier.md` and `V33_ARMING.md` sha references updated to the new pin; the falsifier-pins test
(`test_stop_pins_and_sha_match_doc`) and `test_params_sha_repinned_and_previous_defined` are green.

### 6. Fixture + full replay — done above. Fixture test asserts new==0 / old>=6 on the real 11-flip slice;
re-ran green.

### 7. Suite — `1258 passed, 5 skipped, 0 failures/errors`; v33 `250`. Matches the report.

---

## NITs / QUESTIONS FOR BRAD (none blocking)

### NIT-1 (item 2) — hysteresis uses a different spot estimator than the switch selector; can strand the ladder.
The switch candidate is chosen by `_select_spot` (highest single yes-mid) but gated by `_implied_spot` (the
probability-weighted centroid); the replay shows they disagree by one bucket on ~10k boundary ticks. This is
conservative (blocks switches, never forces them) and the centroid is arguably the *better* spot estimator,
so I do not consider it a defect. But two consequences are worth your call: (a) a spot genuinely parked
5-14$ inside a new bucket satisfies the 3 s debounce yet never satisfies the 15$ hysteresis → the ladder
quotes the *adjacent* bucket for the whole window; (b) selector≠gate is a latent inconsistency.
**Recommendation:** either add a time-based override (commit after, say, `5 x bucket_switch_deb_ms` of
continuous resolved-spot regardless of hysteresis, so a persistent parked spot eventually switches), or
ship **debounce-only** (`bucket_switch_hysteresis_usd = 0`) as the default — the replay shows debounce alone
already cuts creates 264→44 (~6x), and it removes the strand risk and the estimator mismatch entirely.
Hysteresis then stays available as a lever. Your call; the shipped default is safe either way.

### NIT-2 (item 3) — the stand-down hold opens a bounded naked-exposure window.
Keeping rests live during a wing-stale period means a taker fill during the ≤`stand_down_hold_ms` hold that
is followed by a persistent strike outage leaves that filled NO leg un-hedged (`one_legged`) for the window.
For the *observed* trigger (15 sub-second stale-wing blips) the strikes refresh within the hold and the wing
is taken on resume, so this is a rare-tail risk, bounded and flagged. **Recommendation:** keep
`stand_down_hold_ms` short (1500 is reasonable), and watch the `one_legged` / `stand_down_cancel` counts in
the next DRY window; if a real multi-second strike outage ever coincides with a sweep, `stand_down_hold_ms=0`
disables the hold. Worth one line in the L2/arming notes that the hold trades a small naked-exposure tail for
queue preservation.

### NIT-3 (item 6, fidelity) — the replay isolates FIX 1 only.
My replay relaxes freshness (to avoid re-firing stale-wing on `local_ts` timing), so it does not reproduce
FIX 2's stale-wing churn removal, and it clocks on `local_ts` with a synchronous echo. The bucket-flip
reduction and the roll-amend invariance are faithful; the exact pre-fix integer (264 here vs 418 live) is
not, because the ~154 stale-wing re-places and executor timing are out of scope of this harness. The
committed fixture test is the authoritative per-flap proof. No action needed — just don't read the 22 as an
exact live prediction; read it as "the flap no longer generates a ladder rebuild per flip."
