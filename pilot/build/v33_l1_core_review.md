# V3.3 Phase L1 review — the rolling-ladder core (PR #85)

Reviewer: Opus 4.8 (Fable's delegated reviewer). Branch `feat/v33-l1-ladder-core`, head **6de7e5d**,
base `origin/main` **b5059ee**. Reviewed in worktree `dv3_wt_review` (branch
`review/v33-l1-ladder-core`). No code under review modified. Date 2026-09-22.

## VERDICT: BLOCK

Two BLOCKING findings, both in the derived rung/E_rung **labels** and the shipped **`check_invariants`**,
both demonstrated with adversarial replays (scripts run against the PR head, not committed). The core's
actual trading mechanics are sound and well-tested: the roll (1c/2c/up/down/shrink/cap-hold/fallback),
bucket change, wing coalescing, per-rung locks, order-count discipline, no-double-place, and queue
preservation all reach correct end states with no duplicate prices and no > K exposure in every scenario
I constructed. The block is because the core ships a safety invariant it advertises as "safe to run live"
that **raises `AssertionError` on realistic fast-market sequences**, and because the `rung` field that
`RungFill` captures for the §6 falsifier is **wrong after any roll**. Both have small, contained fixes.

Receipts: full suite `1023 passed, 2 skipped, 2 errors` (the 2 errors are pre-existing
`test_quintile.py` corpus gaps — missing `historical-data/15-minute/`, not introduced by L1; matches the
build report). v33-only: `66 passed`. Diff touches only `pilot/service/v33/*`, `pilot/policy/v33_params.json`,
`pilot/tests/test_v33_*`, the fixture, and the report; `git diff origin/main...HEAD -- pilot/service/v32/`
is **0 lines** (V3.2 core frozen, confirmed). No `time.time`/`random`/`open`/`socket`/`import os` in
`service/v33/core.py` (purity confirmed); all V3.2 imports are pure functions/dataclasses.

---

## BLOCKING

### BLOCKING #1 — `rung` is never refreshed while `E_rung` is; after any roll the two disagree for every unmoved order, so the `rung` captured into `RungFill` (the §6 per-rung falsifier key) is wrong.
`service/v33/core.py:447-454` (`_recompute_context`) refreshes **only** `E_rung`
(`replace(o, E_rung=_e_rung(params, n_top, o.price))`) — it does **not** recompute `rung`. `rung` is set
once, at placement (`_desired_rungs` → k, `core.py:875`, `_place_one` `core.py:907`) or at a roll
(`rp.target_rung`, `core.py:503`), and then left. Because `E_rung` tracks the live `n_top` and `rung`
does not, one single normal roll-down desynchronises them:

```
after ONE roll-down (n_top 0.50 -> 0.49):
  price=0.49  rung=1  E_rung=0.05 (implies rung 0)  <-- MISMATCH
  price=0.48  rung=2  E_rung=0.06 (implies rung 1)  <-- MISMATCH
  ... 10 of 11 orders mismatched ...
  price=0.39  rung=10 E_rung=0.15 (implies rung 10)  (the moved order, ok here)
```

`RungFill(rung=order.rung, ...)` (`_apply_fill` `core.py:454-457` → `_book_rung_fill` `core.py:650`)
captures this stale `rung`. The build report advertises `RungFill.rung` as what "the falsifier can compute
per-rung solved-vs-realised lock" (report §"Design decisions" #9; PLAN §6 "per-rung shortfall … at every
rung with ≥ 3 fills"). After a window with even a few rolls, grouping fills by `rung` mixes fills that
actually rested at different margins and splits same-margin fills across indices — the falsifier statistic
is corrupted.

**Failure scenario:** a live window rolls the ladder down 3c over the hour (routine — the roll exists for
exactly this), then a full sweep fills all 11 rungs. Every RungFill's `rung` is off by 3 from its true
ladder position. L3's "per-rung shortfall ≤ 3c at every rung with ≥ 3 fills" gate is computed on
mislabeled buckets and can pass or fail for the wrong reason.

**Fix (minimal, matches how `E_rung` is already handled):** in `_recompute_context`, refresh `rung`
alongside `E_rung`: `rung=int((n_top - o.price) / _CENT)`. Then `rung` is always the live position.
(See BLOCKING #2 for the paired fix in `_apply_amended`, and note the `rung >= 0` invariant must then be
relaxed for the legitimate above-`n_top` transient.) Alternatively, if L3 is willing to group strictly by
resting **price** (unambiguous, roll-independent), `rung` can be dropped from `RungFill` entirely — but
then the report/tests should stop presenting `rung` as the per-rung key. Either way the current
half-maintained field must not ship.

### BLOCKING #2 — the roll applies **emit-time** `E_rung`/`rung`; when `n_top` moves during an in-flight roll (or a cap crash strands rungs above `n_top`), `check_invariants` — run after every event and documented "safe to run live" — raises `AssertionError`.
`_emit_roll` (`core.py:948-950`) computes `target_rung`/`target_E` from `st.n_top` **as of roll issue**,
stores them on `RollPending`, and `_apply_amended` (`core.py:502-504`) writes them onto the moved order at
**ack time** without reconciling to the current `n_top`. `check_invariants` (`core.py:301-305`) asserts
`E_rung == E_min + (n_top - price)` and `rung >= 0`. Two demonstrated sequences break it:

1. **W reverts during an in-flight roll** (scenario (2)(i)). Roll-down issued at `n_top=0.49`
   (`target_E=0.15` for price 0.39). Before the amend acks, W reverts so `n_top=0.50`. On `OrderAmended`,
   the moved order gets `E_rung=0.15`, but the invariant wants `0.05 + (0.50 − 0.39) = 0.16`:
   `AssertionError: E_rung 0.15 != E_min+(n_top-price) for price 0.39, n_top 0.50`. (End state still
   self-heals to the correct contiguous 0.50..0.40 after the queued reverse-roll — the mechanism is fine;
   only the label/invariant break.)
2. **Cap binds after placement** (scenario in task item 5). A bucket `yes_bid` jump drops `cap` from 0.64
   to 0.37, so `n_top` → 0.37 while all 11 rungs still rest at 0.40..0.50 — i.e. above `n_top`. As the
   ladder crawls down, a moved order's `int((n_top − price)/_CENT)` goes negative:
   `AssertionError: bad rung index -2`.

`OrderAmended`, `OrderCancelled`, `Fill`, `OrderAck` and `Trade` never call `_recompute_context`, so a
fill landing between an amend-ack and the next book/clock tick also books stale labels into `RungFill`
(compounds #1). And because the core's own test harness runs `check_invariants` after **every** event
(`_feed`, `test_v33_core.py:70-72`) and the docstring says it is "safe to run live," any L2 wiring that
trusts that claim will crash a live window during precisely the volatile conditions the ladder targets.

**Fix:** (a) in `_apply_amended` and the `_apply_cancelled` fallback place, derive `rung`/`E_rung` from
the **current** `st.n_top` (guard `None` → fall back to the stored target), not the emit-time values;
(b) relax `check_invariants` to permit `rung`/`E_rung` for orders transiently sitting **above** `n_top`
(drop the `rung >= 0` assert, or clamp), since a negative position label is the honest transient during a
cap crash or a fast up-move; (c) with (a)+(b) and BLOCKING #1's refresh, the `E_rung` equality assert
holds after every event. Add a regression test that moves W during an in-flight roll and asserts
`check_invariants` passes.

---

## QUESTIONS FOR BRAD (policy, not code defects)

### QUESTION #3 — a shift-**up** after a partial sweep relocates a deep survivor into an already-filled shallow price.
Verified (scenario (2)(v)): sweep fills the top 3 (0.50/0.49/0.48), then W falls so `n_top` rises 1c →
the **bottom** survivor (0.40) is amended **up** to `top_survivor+1c = 0.48` — a price that was already
filled this window. The lot cap is honored: `live(8) + filled(3) = 11 ≤ K`, so no extra lot is created;
it is one survivor riding the ladder up, its `E_rung` re-labeled from 15c to 8c. This is consistent with
Brad's "the ladder is one cent higher, same K−filled orders" mental model and does **not** violate Q3
(Q3 caps *lots*, not price re-exposure), so I do **not** treat it as a bug. But it does re-expose a price
the market already swept and moves a deep, high-margin resting order up to a shallow margin.
**Recommendation:** intended per the shift-the-whole-ladder design; confirm the risk model is what you
want. If not, the alternative is "roll-up only fills the gap at the top, never relocates a survivor across
the filled region" (more code, L2).

### QUESTION #4 — a cap-bind-after-placement (or any large `n_top` jump) is corrected only one cent per debounce, stranding up to K rungs above the post-only cap for ≈ K × `deb_ms`.
Verified: after the cap dropped to 0.37, **all 11** rungs sat above the cap and the ladder took 13 one-cent
rolls to converge to 0.37..0.27. At the shipped `deb_ms = 5000` that is ~65 s with rungs above the
post-only cap — during exactly the fast move that caused the crash. V3.2 repriced its **single** order to
the capped price in one step. Already-resting bids the market comes down to are normal maker fills (the
strategy *wants* fills), so this is not automatically a loss, but it is a real latency/exposure regression
versus V3.2 and Brad's "one order per 1c" mechanism did not contemplate a multi-cent cap crash.
**Recommendation (Brad's call):** either lower `deb_ms`, or add a fast-path — when the cap binds (or
`|dn|` exceeds a threshold), shift the whole ladder in one step (cancel-all/place-all, as the bucket
change already does) instead of crawling. QUESTION, not blocking, because it follows the frozen spec.

### QUESTION #5 — Q-ROLL-DEB (builder's open question): debounce each cent vs debounce the start of a convergence.
The builder shipped "debounce each cent" (a 2c move paces at `deb_ms` per cent; confirmed by
`test_roll_2c_is_two_rolls_strictly_sequential` using `deb_ms=0`). At `deb_ms=5000` a genuine 2c move
takes ≥ 5 s to fully converge. **My recommendation:** "debounce the start" converges faster once
committed and is a better fit for the roll's queue-preservation goal; the current each-cent is safe
(never thrashes) but interacts badly with QUESTION #4. Your call.

---

## NITS

- **NIT #6 (golden 5..15c vs 5..16c).** Verified independently: at the golden W=1.4737, the study's E14 and
  E15 both solve to n=0.36 (collapse), and the core's distinct 11th rung sits at 0.35 → realised lock
  **+16.03c**. So at this (low-W) regime the ladder's realised locks span 5..16c, and the deepest rung's
  `E_rung` **label** is 15c (`E_min + 10c`) while its true margin is 16c. This does **not** break the §6
  gate direction (shortfall = solved − realised stays ≤ 3c; it is generous here). But: (a) the plan/report
  should note the ladder is "5..(E_min+K)c depending on W," not fixed 5..15c; (b) **L3 must compute the
  per-rung "solved E" as `lock_value(price, W_at_fill)`, not the integer `E_rung` label**, or the deepest
  rung's shortfall reference is understated by 1c. NIT for L1; a definition note for L3.
- **NIT #7 (`refill_in_window` is dead).** Loaded into `V33Params` (`params.py:99,156`) but never read in
  `core.py` — Q3 no-refill is hardcoded (a filled rung is dropped and never re-placed). Harmless while
  `false`, but a future `true` would silently do nothing. Either wire it or comment that it is advisory /
  reserved for L2.
- **NIT #8 (invariant could be stronger).** `check_invariants` does not assert `roll_pending ⇒ exactly one
  in-flight order that is a ladder member`, nor wing-leg/batch consistency (2 legs per taken batch, leg
  counts == `batch.total_count`), nor that prices are whole cents. Task item 4 asked for the roll-pending
  check specifically. Consider adding once BLOCKING #1/#2 are resolved.
- **NIT #9 (fallback re-place skips the cap re-check).** `_apply_cancelled` fallback (`core.py:582-588`)
  re-places at `rp.target_price` without re-checking `st.cap`. For an **up**-roll fallback the cap was
  checked only at emit; if `no_ask` dropped meanwhile, the fresh rung can be placed above the cap. Rare
  and L2-recoverable (venue rejects post-only), but worth a guard.

---

## Checklist dispositions
- **Roll semantics vs Brad's words:** correct. W↑→`n_top`−1c→top order amended to bottom−1c; W↓→bottom
  order to top+1c; K−1 order_ids untouched (verified `untouched == 10` in tests + my replays); 2c = two
  strictly sequential rolls, second only after first ack (verified). Scenarios (i)–(v): (i) queues, no
  second amend while pending, no dup price, correct self-healing end state — but trips the invariant
  (BLOCKING #2); (ii) fallback-then-move converges cleanly; (iii) fill on a non-moving rung is independent
  (books, coalesces, roll untouched); (iv) golden (f) — fill on the moving rung books at the pre-roll
  price, the late amend does not double-place (verified); (v) shift-up-into-filled-region (QUESTION #3).
- **anchor_n_top:** updates correctly per confirmed roll/shrink/fallback (`+ direction*_CENT`), re-set on
  bucket re-placement (`_place_all`), round-trips (`test_roll_round_trip_up_then_down_restores_anchor`).
  Trigger uses the anchor not the top survivor's price → no spurious roll after a partial sweep with W
  constant (verified). The anchor stays consistent with the ladder; the drift I found is in `rung`/`E_rung`
  (BLOCKING #1/#2), not the anchor.
- **Cap / n_min:** cap applied to `n_top` via `solve_n`; roll-down goes deeper (safe), roll-up is
  cap-gated (`target <= st.cap`), `_place_all` uses the already-capped `n_top` — so the core never
  *places/amends* an order above the cap; a cap that binds after placement leaves resting orders exposed
  and is corrected only by the crawl (QUESTION #4). n_min truncates from the bottom at placement; the
  down-roll past n_min shrinks from the top (cancel, counted as a one-order roll); non-regrowth until a
  bucket re-placement is safe (fewer orders, never more) and documented — acceptable for L1.
- **Coalescing:** boundary is `now - first_ts > wing_coalesce_ms/1000` in both `_coalesce_add` and
  `_coalesce_flush` (a fill exactly at the boundary joins; consistent, no double-emit). 100 ms → one batch,
  300 ms → two (verified); wings sized to `total_count`; per-rung locks retained in `WingBatch.fills`
  (`_batch_lock` sums `lock_value(f.price, ...)`); a batch is taken once (`taken` flag). Take-on-fresh-book
  deferral is inherited from V3.2 and is fine — flag for L2 that the driver must tick ClockTicks densely so
  the final coalesce group flushes and its wings are taken before settle.
- **Golden (a):** fixture is internally consistent (`close_epoch 1789876800` = 2026-09-20T04:00:00Z, after
  holdout+seal; `sweep_W 1.4737`; `study_rungs` E14==E15==0.36; `source` cites
  `journals_v32/20260920T040000Z.jsonl.gz` + the `v33_ladder_ideal` filter). I re-ran `solve_n` for
  W=1.4737 independently and reproduced every row of the report's table exactly, including the E14/E15
  collapse and the core's 0.35 → +16.03c 11th rung. I did **not** re-extract the prints from the live
  journal directly: the sweep prints fall at T-824..T-308s (≈ 03:46–03:55 UTC), inside the :38–:59
  streaming-avoid window, so provenance rests on the fixture's `source` field + internal consistency + the
  `test_study_rungs_match_persisted_ideal_json` cross-check.
- **Params:** sha pin present and self-verifying (`load_v33_params` fail-closed on mismatch / missing key /
  shadow-E outside `[E_min, E_min+(K-1)c]` / `rungs < 1`); `max_sets_per_hour 11`, `replace_rate_alarm_per_min
  120`, `wing_coalesce_ms 150`, `refill_in_window false`, `E_min 0.05`, `rungs 11`, `lots_per_rung 1`,
  `n_min 0.05` — all consistent with DECIDED Q1–Q7. `canonical_sha256` agrees with v32/box (tested).
- **Purity / frozen v32:** confirmed (0 v32 lines changed; no I/O/clock/random in v33 core).

## Answers to the builder's other open questions
- **n_min shrink regrowth:** acceptable for L1 (safe direction; documented). Confirm re-grow behavior when
  you spec L2/L3.
- **Golden (a) W-pinning / relaxed freshness:** fine for L1; the realistic W-drift full-tape replay is L2's
  replay-lab job, as stated.

---

# ROUND 2 — head 24421c7 (3 commits atop 6de7e5d)

## ROUND 2 VERDICT: BLOCK

Round 1's two BLOCKING findings are **properly fixed and verified**, and every QUESTION/NIT I raised was
addressed well (fast-shift for the cap crawl, start-debounce pacing, enforced `refill_in_window`, fallback
cap clamp, strengthened invariant, W-dependent ladder-range wording). But the **new fast-shift path
introduces one new BLOCKING regression**: it re-places K rungs regardless of how many rungs already filled
this window, which lets total window exposure exceed the DECIDED Q3 "max K lots per window" cap. (The
pre-existing bucket-change path shares the same defect — I missed it in R1.) One contained fix closes both.

Receipts: v33 `79 passed`; full suite `1036 passed, 2 skipped, 2 errors` (the 2 errors are the same
pre-existing `test_quintile.py` corpus gap, not L1). Params sha self-verifies to
`c0201af78015e24fa7d8984d9f9747215330f5bee29fd2567290c2653c244a20`; the R1 sha is kept as
`PREVIOUS_V33_PARAMS_SHA256_L1_R1`. Diff touches only v33 core/params, the json, the two v33 test files,
the report, and PLAN §5 (doc). All four R1 adversarial scenarios re-run against this head are
**invariant-clean** (no `AssertionError` after any event).

## NEW BLOCKING (R2)

### BLOCKING #R2-1 — fast-shift (and bucket-change) re-place K rungs, not K − filled, breaching the Q3 "max K lots per window" risk cap.
The `_roll` fast-shift path (`if cents >= params.fast_shift_min_cents: _cancel_all(track_outstanding=True);
awaiting_replace=True`) and the bucket-change path both re-place via `_place_all` → `_desired_rungs`, which
always yields the full `range(params.rungs)` = K rungs with **no accounting for `rungs_filled`**. So after
a partial sweep the ladder **regrows past its surviving size** and the window can hold more than K lots.

Demonstrated against this head:
```
partial sweep fills 3 (survivors 8) -> fast-shift (n_top -5c) -> cancel 8, RE-PLACE 11
  MAX total window fills = filled(3) + resting(11) = 14  (K=11)   >>> EXCEEDS K
  burst-sweep the re-placed ladder -> rungs_filled booked = 14    >>> RISK-CAP BREACH
same via bucket-change after a partial sweep: re-placed 11, filled 3 -> 14 vs K=11
```
The `max_sets_per_hour` latch (`rungs_filled >= 11`) is **reactive** — it emits a cancel-all only after a
fill pushes the count to K, and a real sweep prints all rungs in ~90 ms (per PLAN §2), faster than a
cancel round-trips, so the latch does not prevent the over-fill in a burst. This defeats the DECIDED Q3
risk model ("max K lots per window; the risk cap is the ladder size") and undersizes the S4 stop, which
PLAN §5/Q5 sized assuming a worst case of K lots.

Contrast R1's crawl-only design, which never regrew the ladder after a partial sweep (it only *moved*
survivors), so total window fills were bounded by K — the fast-shift is a genuine regression of that
guarantee.

**Failure scenario:** a shallow pump fills 3 rungs at T-9m; BTC then jumps so n_top moves ≥ 4c (or the
bucket rolls) at T-8m → fast-shift/bucket-change re-places 11 fresh rungs; a full sweep at T-7m fills all
11. The window books 14 one-legged-worst-case lots (~$4.90) against an intended cap of 11 (~$3.85) and an
S4 stop sized for 11.

**Fix (one place, covers both paths):** cap the placement count at the remaining hourly allotment — in
`_place_all` (or `_desired_rungs`) place `min(params.rungs, params.max_sets_per_hour - st.rungs_filled)`
rungs (i.e. K − filled) from the top, and if that is ≤ 0 latch `rest_allotment_done` instead of placing.
Add a regression test: partial sweep (f filled) → fast-shift → assert exactly K − f rungs re-placed and
`rungs_filled + len(ladder) ≤ K`; same for a bucket change after a partial sweep.

## Round 1 findings — dispositions

- **BLOCKING #1 (rung never refreshed): CLOSED.** `_recompute_context` now refreshes BOTH `rung` and
  `E_rung` from the current n_top via the new `_rung_of` (`core.py:367`, `485-497`). Verified: after a
  roll-down every unmoved order's `rung` matches `_rung_of(n_top, price)` (was 10/11 mismatched in R1; now
  0 mismatches), and `RungFill` captures the live rung (`test_rungfill_captures_live_rung_after_rolls`).
- **BLOCKING #2 (roll applies emit-time labels; invariant raises): CLOSED.** `_apply_amended` and the
  `_apply_cancelled` fallback now derive `rung`/`E_rung` from the **current** `st.n_top` (fallback to the
  stored target only when n_top is None). The invariant asserts `rung == _rung_of(n_top, price)` and
  permits negative ("stranded above the top") rungs, and drops the old `rung >= 0` assert. Verified: R1
  scenario (i) (W reverts during an in-flight roll) and the cap-crash sequence are both invariant-clean;
  `test_w_reverts_during_in_flight_roll_invariants_green` and `test_invariant_allows_negative_rung_above_n_top`
  codify it.
- **QUESTION #4 (cap-crawl latency): addressed.** `fast_shift_min_cents` (default 4) shifts the whole
  ladder in one step on a ≥4c jump / cap crash instead of crawling K rolls. Verified it fires at 5c, does
  **not** fire at 3c (`test_3c_move_still_crawls_not_fast_shift`), and a fill caught during its cancel-all
  is booked via `cancel_ctx` (verified: `rungs_filled` 0→1 on `OrderCancelled(filled_count=1)`). Good — but
  see BLOCKING #R2-1 for its interaction with prior fills.
- **QUESTION #5 / Q-ROLL-DEB (pacing): addressed.** `deb_ms` now debounces only the START of a
  convergence; same-sign continuations roll on the ack (no re-debounce); a sign flip re-debounces
  (`converging_dir`). Verified the exact Q2 sequence — 1c down, ack, W snaps back +2c up within the
  debounce → re-debounces, then rolls the current **bottom** up, ack-driven second cent, final ladder
  0.51..0.41, **max `roll_pending` observed = 1** (never two in flight). No path issues a roll while
  `roll_pending` is set (the guard precedes all pacing; `_apply_amended` clears it before re-entry).
- **NIT #6 (5..16c wording): addressed.** PLAN §5 now states the realised range is W-dependent, the
  distinct deepest rung realises up to (E_min+K)c at low W (+16.03c at the golden W), and L3 must compute
  solved E from `lock_value(price, W_at_fill)`, not the integer label. Correct.
- **NIT #7 (`refill_in_window` dead): addressed.** Now ENFORCED — `load_v33_params` fails closed on `true`
  (`test_refill_in_window_true_fails_closed`).
- **NIT #8 (invariant strength): addressed.** Now asserts whole-cent prices, `roll_pending` matches ≤ 1
  ladder order, a taken batch has exactly 2 legs each sized to the batch total, and
  `rungs_filled == len(rest_fills)`.
- **NIT #9 (fallback cap re-check): addressed.** The `_apply_cancelled` fallback clamps the re-place to
  `st.cap` and drops the rung if the clamp pushes it below n_min.

## Round 2 checklist dispositions
1. BLOCKING #1/#2 closed — verified above (all four R1 scenarios invariant-clean). OK
2. Pacing (start-debounce, ack-driven, sign-flip re-debounce) — verified, never two in flight, right order
   moves. OK
3. fast_shift: (a) 3c does not fire OK; (b) fill during cancel-all booked via cancel_ctx OK; (c) after
   re-place anchor == n_top and labels consistent (invariants clean) OK; **(d) FAILS — re-places K, not
   K−filled → BLOCKING #R2-1.** FAIL
4. Params: `refill_in_window=true` fails closed OK; `fast_shift_min_cents < 2` fails closed OK; new sha
   self-verifies OK; previous sha kept OK.
5. Strengthened invariant OK; fallback cap clamp OK; PLAN §5 doc-only + correct OK.
6. Suite 1036 passed / 2 skipped / 2 pre-existing corpus errors OK; v33 79 OK.

## QUESTIONS FOR BRAD (unchanged from R1, still open — policy, not defects)
- QUESTION #3: a shift-up after a partial sweep relocates a deep survivor into an already-filled shallow
  price (lot cap honored; re-exposes a swept price). Still present; still a policy confirmation.
- On BLOCKING #R2-1: once the K−filled placement fix is applied, confirm `fast_shift_min_cents=4` matches
  your risk intent (a ≥4c n_top jump triggers a whole-ladder cancel/replace rather than a crawl).

---

# ROUND 3 — head bdf0ed2 (one commit atop 24421c7)

## ROUND 3 VERDICT: APPROVE

BLOCKING #R2-1 is **fixed and verified**. No new findings. Every blocking item raised across R1/R2/R3 is
now closed; only the standing QUESTIONS FOR BRAD remain, and those are policy confirmations, not defects.
The V3.2 core is untouched (0 lines across the whole PR base..head), the core stays pure, the policy sha
still self-verifies, and all goldens are unchanged.

## BLOCKING #R2-1 — CLOSED and verified

`_place_all` now caps the placement at the remaining hourly allotment:
`budget = min(params.rungs, params.max_sets_per_hour - st.rungs_filled)`, places `_desired_rungs(...)[:budget]`
from the top, and latches `rest_allotment_done` (placing nothing) when `budget <= 0`. A new invariant
`rungs_filled + len(ladder) <= K` asserts the exposure at every step. This covers BOTH re-placement paths
(fast-shift and bucket-change), since both route through `_place_all`.

Verified against this head (my `adv_r2.py`, re-run):
```
partial sweep fills 3 -> fast-shift (n_top -5c) -> cancel 8, RE-PLACE 8 (was 11 in R2)
  exposure filled(3) + resting(8) = 11 = K   (was 14 > K in R2)
  burst-sweep the re-placed ladder -> rungs_filled booked = 11 (never 14)
```
Additional checks I ran directly:
- **First placement still full K:** a clean placement (rungs_filled=0) → budget = min(11, 11) = 11 rungs. ✓
- **Bucket-change after a partial sweep:** re-places K − filled = 8 on the new ticker, exposure = 11. ✓
- **Fill arriving DURING the re-place (cancel-phase fill):** a cancel carrying `filled_count=1` books via
  `cancel_ctx` BEFORE `_place_all` runs (the place fires only on the last cancel confirm), so
  `rungs_filled` was 4 by then and the re-place placed exactly 7 (= K − 4), exposure = 4 + 7 = 11. The
  budget correctly reflects the mid-replace fill. ✓
- **K − filled cap ∧ n_min truncation (budget = min of both):** with `n_min=0.44` and n_top=0.50 the
  desired ladder is already truncated to 7 rungs; `_desired_rungs(...)[:budget]` takes `min(n_min_count,
  allotment_budget)`, so whichever is tighter binds. Verified the truncated placement (7 rungs). ✓
- **Budget ≤ 0 latches:** a re-place request with rungs_filled = K places nothing and sets
  `rest_allotment_done` (`test_place_all_budget_zero_latches_allotment`). ✓
- **Exposure invariant catches a regrow:** `rungs_filled + len(ladder) > K` now raises
  (`test_invariant_flags_window_exposure_over_k`). ✓

The builder's five new tests (`test_fast_shift_after_partial_sweep_caps_at_k_minus_filled`,
`test_bucket_change_after_partial_sweep_caps_at_k_minus_filled`,
`test_bucket_change_after_full_sweep_places_nothing`, `test_place_all_budget_zero_latches_allotment`,
`test_invariant_flags_window_exposure_over_k`) are substantive and match these checks.

## Regression confirmation
- R1 BLOCKING #1/#2 still closed: all four R1 adversarial scenarios (`adv_test.py`) re-run against this
  head are invariant-clean. ✓
- Goldens unchanged: `test_v33_golden.py` 9 passed. ✓
- The policy sha is unchanged and still self-verifies (`c0201af7...`); R3 added no param. ✓
- V3.2 core: 0 lines changed across the whole PR (base b5059ee .. head bdf0ed2). ✓

## Round 3 checklist dispositions
1. `_place_all` caps at `min(rungs, max_sets_per_hour − rungs_filled)` from the top, latches when ≤ 0. ✓
2. New invariant `rungs_filled + len(ladder) <= K`. ✓
3. `adv_r2.py` shows the breach closed (8 re-placed, exposure 11); `adv_test.py` R1 scenarios
   invariant-clean. ✓
4. First placement still full K; all goldens unchanged. ✓
5. K − filled cap interacts correctly with n_min (budget = min of both) and with a fill during the
   re-place (cancel_ctx booked → next re-place budget reflects it). ✓
6. Full suite 1041 passed / 2 skipped / 2 pre-existing corpus errors; v33 alone 84. ✓ (both match the
   expected counts)

## QUESTIONS FOR BRAD (still open — policy, not blockers; do not gate the merge)
- QUESTION #3 (from R1): a shift-up after a partial sweep relocates a deep survivor into an already-filled
  shallow price (lot cap honored; re-exposes a swept price). Confirm the risk model.
- `fast_shift_min_cents = 4`: confirm a ≥ 4c n_top jump doing a whole-ladder cancel/replace (rather than a
  crawl) matches your intent.
These are for the L2/arming discussion; the L1 core is correct as it stands.

---

# ROUND 4 — head 87dff91 (3 commits atop bdf0ed2) — FRESH review of the roll rewrite

## ROUND 4 VERDICT: APPROVE

Brad's margin-array convergence (`_converge` replacing the anchor/roll + R2 fast-shift) is a faithful,
correct implementation of his verbatim R4 design. I treated this as a fresh adversarial review of the
convergence — built new probes (the R1/R2 scripts are stale), ran `check_invariants` after every event,
and could not break it: no invariant violations, no duplicate prices at rest, concurrency bounded at
`max_amends_in_flight`, the K-lot cap and Q3 (2-slots never re-opened) hold under adversarial W sequences,
and the convergence always settles (or stands down) — no livelock. Two low NIT observations, neither
gating.

Receipts: v33 `85 passed`; full suite `1042 passed, 2 skipped, 2 errors` (same pre-existing corpus gap);
goldens `9 passed`; the golden fixture is byte-unchanged since bdf0ed2. Params sha self-verifies to
`6dc7cb5b8bcc0790a04a698f130cfe99a2a52326dca3ef46ea784e7f8a11a421`; R1 and R2 shas kept as
`PREVIOUS_V33_PARAMS_SHA256_L1_R1/_R2`. `fast_shift` is gone from `service/` and the json (only a
removal-note comment remains). V3.2 core untouched (0 lines PR-wide). Core stays pure.

## Adversarial verification (my probes, run against this head)

1. **Brad's roll preserved + concurrency bound.**
   - 1c n_top move → exactly 1 amend, 10 coids untouched, ladder → 0.49..0.39.
   - 2c → 2 concurrent amends, 9 untouched, ladder → 0.48..0.38.
   - 5c → 3 concurrent then more (`roll_count=5`), **max in flight = 3**, 6 coids untouched, → 0.45..0.35.
   - **Cap crash** (n_top 0.50→0.42, 8 stranded above a bound cap) → converges N-at-a-time, **max in
     flight = 3, ZERO cancel-all** (`CANCEL_REST` count 0), ends 0.42..0.32 with no order above the cap.
     This is the intended replacement of R2's cancel-all — deep orders keep queue.
2. **Partial sweep + 2-slots never re-opened.**
   - Sweep margins 5,6,7 → `margin_state = (...,2,2,2,1,...)`. W constant after → 0 moves. W down 1c → 1
     amend, and **0 amend/place targets land on a 2-index**.
   - **W oscillating ±3c repeatedly** after the sweep → over the whole oscillation, **0 targets land on a
     2-index**, `margin_state` 2s intact, `live+filled = 11 ≤ K` throughout, no violation, no dup. (In
     PRICE space a swept price can host a new order later at a DIFFERENT profit level — that is Brad's
     explicit profit-space model, and the exposure stays ≤ K; see Observation O-2.)
3. **Concurrency hazards.**
   - Two amends targeting the SAME vacant slot, or an amend targeting a price still occupied by an
     in-flight order: **prevented** — `VACANT` excludes both `occupied` (in-flight orders remain in the
     ladder at their OLD price until ack) and `inflight_targets`. No probe (5c, cap crash, oscillation, all
     with 3 concurrent amends) produced a duplicate price at rest or a step-invariant break.
   - Fill on an in-flight order (golden f): booked at the PRE-roll price (0.49), the late `OrderAmended`
     for the filled order emits 0 places/amends (no phantom), `_drop_roll` clears it, converges to
     `live+filled = 11`.
   - **W ping-pong every tick at `deb_ms=0`**: bounded — the replace-rate alarm stands the ladder down
     after ~120 confirmed amends (`replace_rate_alarm_per_min = 120`); no runaway.
   - **W constant after a partial sweep**: converges and **settles** — `roll_count` steady at 4 across 30
     further constant ticks (no churn). No livelock while W is constant.
4. **Pairing.** Greedy by price, deterministic (5c move emits identical amends on repeat). Unequal sets
   handled: OUT > VACANT (n_min raised, n_top down) → the excess deep OUT are CANCELLED (shrink), rest =
   5 valid rungs all ≥ n_min, distinct. `live + filled ≤ K` held at every step in every probe.
5. **Golden (a)** full-sweep locks unchanged (`test_golden_a...` not modified; passes; fixture unchanged;
   +5.89c..+16.03c per the R2 verification). Goldens (d)/(f) correctly updated for concurrency
   (d = two CONCURRENT amends, 9 untouched; f = `rolls_in_flight`).
6. **Params.** `max_amends_in_flight=0` fails closed (`V33ParamsInvalid`); new sha self-verifies; R2 sha
   kept; `fast_shift_min_cents` fully removed from code + json (grep clean apart from a removal note).
7. **PLAN §1/§5** accurately restate Brad's array model — profit-space index, 0/1/2 semantics, anonymous
   1-slots with price-derived index, convergence pairing OUT↔VACANT N-at-a-time "until prices match E",
   2-slot tied to `filled_at`, cap crash = the loop (not cancel-all), extra OUT → cancel / suppressed slot
   → create. Compared to Brad's verbatim words in the build report: **no drift.**
8. **Suite** 1042 passed / 2 skipped / 2 pre-existing corpus errors; v33 85. Both match the expected
   counts.

## Design review of `_converge` (correctness argument)
- `margin_state` is a FIXED absolute-margin array (length E_min_c + K); an order's index is derived each
  tick from `price` vs `n_top`; only `_placeable_open_slots` (state==1, price ∈ [n_min, cap]) are targets;
  fills set the index to 2 permanently. So no target price ever maps to a 2-index (verified empirically),
  and the number of 1-slots only decreases → placeable ≤ K − filled_in_range, and `_place_all`/create both
  hard-cap at `max_sets_per_hour − rungs_filled` / the K exposure guard. R2-1 exposure guarantee preserved
  and now a step invariant (`rungs_filled + live ≤ K`).
- The convergence terminates while W is constant: constant W ⇒ constant `n_top` ⇒ constant target set ⇒
  each ack moves an OUT order onto a target (removing it from OUT) or a leftover is cancelled/created once;
  it reaches |orders| = |placeable slots| and stops. Under moving W it is bounded by the replace-rate
  alarm. Both verified.
- `check_invariants` correctly relaxed for the convergence: consecutiveness is no longer a step invariant
  (a multi-order convergence is transiently non-contiguous); it is a REST property the tests assert after
  convergence. Distinct-price, ≤ K live, exposure ≤ K, ≤ `max_amends_in_flight` distinct-order rolls,
  rung/E_rung live-consistency, and `rungs_filled == len(rest_fills)` are all still asserted every event.

## NIT / observations (non-blocking)
- **NIT O-1 (premature allotment latch, near-zero reachability).** `_book_rung_fill` latches
  `rest_allotment_done` when a fill empties the ladder (`not st.ladder and …`). If the ladder was only
  PARTIALLY placed (n_min/cap suppressed some open slots) and every placed order then fills, this latches
  with `rungs_filled < K`, giving up the remaining allotment even though `_converge`'s empty-ladder branch
  would otherwise re-place the now-placeable slots. Safe direction (fewer orders, never more) and
  essentially unreachable at K=11 (n_min=0.05 sits ~30+ cents below the ladder; a cap that suppresses the
  shallow top plus a full sweep of the deep remainder is a corner). Worth a one-line note in L2; not a
  defect.
- **Observation O-2 (profit-space price reuse).** Because the array is in PROFIT space, a PRICE that
  already traded this window can host a new resting order later at a DIFFERENT margin as n_top moves. This
  is exactly Brad's design (2-slots are profit levels, not prices) and total exposure stays ≤ K, but L2's
  venue reconciliation should expect to see a fresh order at a price where an earlier fill occurred — it
  is not a double-book (distinct order_id, within the K cap).

## Round 1–3 findings — all closed
R1 BLOCKING #1/#2 (rung/E_rung label maintenance) and R2-1 (K−filled exposure cap) remain fixed under the
rewrite: rung/E_rung are a derived label refreshed every tick; the exposure guard is now a hard step
invariant. The two standing QUESTIONS FOR BRAD (shift-up into the filled region; the large-jump policy)
are now SUBSUMED by the anonymous-slot convergence — an order simply pairs to the nearest vacant 1-slot;
no special-casing, lot cap holds. No open questions remain that gate the merge.
