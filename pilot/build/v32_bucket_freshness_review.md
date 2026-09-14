# V3.2 review — spot-bucket FRESHNESS gate (PR #36, branch `v32/bucket-freshness`)

Reviewer: Opus 4.8 (Fable-delegated). Worktree `C:\Users\Brads\Python_stuff\dv3_wt_v11`.
Base `4684bad`; PR commits `d58d886` (exclusion draft) + `b4bc47b` (R-STALE-SPOT final).
No proxy, no socket, no sealed/holdout date read. `python -m pytest -q` = **808 passed** (baseline 801 + 7).

## Verdict: APPROVE (one doc defect found and fixed; no code-behavior defects)

The law is implemented correctly and defensibly. R-STALE-SPOT gates the SELECTED spot, never falls
through to a lower-mid bucket, and the gate is belt-and-suspenders: when the selected spot is stale
`_recompute_context` also nulls W/cap/desired_n, so a stale spot cannot reach a PLACE by ANY path even
if the dedicated `stale_bucket` branch were removed. One documentation defect (params.py docstring
described the superseded exclusion design) was fixed in this review with the code left unchanged.

## Adversarial probes

**1. Gate ordering — no stale-spot PLACE anywhere.** In `_requote` the `stale_bucket` reason
(core.py:781-786) sits in the no-quote chain BEFORE the wing/bucket-change/place logic and returns
early via `_cancel_live_if_any` + `_standdown`. Every ingress that could re-place is downstream of it:
  - wing-fresh strike tick while bucket ts old: `decide_v32` runs `_recompute_context` (now = strike ts)
    → `spot_bucket_stale=True`, W=None → `stale_bucket` fires. No place.
  - bucket-change tick: the bucket-change branch (core.py:812) is AFTER the no-quote block; a stale new
    spot stands down first.
  - first tick after re-dial snapshot: `bucket_ts` is set to the snapshot server_ts; fresh → normal,
    old → `stale_bucket`.
  - OrderCancelled during a stale period: `decide_v32` routes OrderCancelled to `_apply_cancelled`
    ONLY — it never calls `_requote`, so OrderCancelled emits no PLACE (only wing-take on a partial
    fill). The awaiting_replace re-place happens on the NEXT BookUpdate/ClockTick, which recomputes
    freshness first. The `awaiting_replace and cancel_in_flight` hold (core.py:829) is downstream of
    the `stale_bucket` return, so it is never reached while stale. No re-PLACE without a re-check.
  - Belt-and-suspenders: with a stale spot, `_recompute_context` skips the W/cap/desired_n block
    (core.py:451 `and not spot_bucket_stale`), so W is None (→ `stale_or_missing_wing`) and desired_n
    is None (→ `n_below_min`) even if `stale_bucket` were bypassed. A stale spot is unquoteable.

**2. Shadow gate (core.py:861-864).** `_shadow_on_trade` now requires the selected spot bucket book
fresh at `event.server_ts`. A Trade frame does NOT refresh `bucket_ts` — `decide_v32`'s Trade branch
calls only `_shadow_on_trade`/`_shadow_complete`, never `_fold_book`, so trades are correctly NOT
counted as book observations. Decision to NOT refresh `bucket_ts` on a trade is right: the venue emits
a book delta alongside a trade, so a genuine informative print arrives with a fresh book anyway;
refreshing `bucket_ts` off a trade alone would let a lone print off a stalled book synthesize a fill
against a stale cap — exactly the failure this gate closes. Documented in the build report. No change
needed. Edge safe: if `spot_Sd is None` the earlier `cls[1] != st.spot_Sd` returns before the
`bucket_ts.get(None)` lookup.

**3. Cross-connection server-ts monotonicity (Phase-2 P3-3).** `age = now - bucket_ts` uses
venue-server-stamped `server_ts` on both sides (strike-derived `now`, bucket-book `bucket_ts`), so a
single server clock — no per-connection skew term. Even granting delivery-jitter, the 30 s bound gives
~10x headroom over the few-second bucket cadence in the quoting window, so a spurious `stale_bucket`
from clock noise is not a realistic risk. Verdict: 30 s does not produce false cancels.

**4. Golden harness relaxation confined + no production default touched.** The only change in
`_run_core` is `bucket_freshness_max_age_s=3600.0` passed to `dreplace(load_v32_params(), ...)` —
scoped to the test-run params object, not the shipped json (default stays 30.0, verified below). The
1.0 s STRIKE gate still binds. Golden assertions reproduce unchanged: `n == Decimal("0.45")`,
`lock == Decimal("0.1036")` (+10.36c), core completion `take.lock == Decimal("0.1036")`, and the
E=0.10 shadow `(0.45, 0.55, 0.1036)`. Rationale (minute-candle fixture cadence vs a 30 s gate) is
sound and documented inline.

**5. Params / sha / doc.** Canonical sha of `policy/v32_params.json`
(`json.dumps(sort_keys=True, separators=(",",":"))`) computed =
`0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc` = `FROZEN_V32_PARAMS_SHA256` =
loaded `p.sha256` = both occurrences in `ceremony/v32_falsifier.md` (old sha `c6715fc7…` = 0
remaining). `bucket_freshness_max_age_s 30.0 [pin]` present on the Policy Values line (doc:55) and the
A_STALE alarm (doc:87). `test_bucket_freshness_pin_matches_params_and_doc` parses the doc and asserts
value + doc text + sha agreement. `falsifier_pins.py` unchanged (holds verdict/promotion gates, not
policy params) — correct.

**6. Tests pin the law.** `test_stale_spot_bucket_cancels_with_stale_bucket_reason` (stale spot →
CANCEL + `stale_bucket` + no PLACE, floor unchanged); `test_stale_bucket_then_fresh_resumes_place`;
`test_stale_non_spot_bucket_has_no_effect` (a); `test_stale_spot_with_fresh_lower_bucket_stands_down`
(b) — explicitly asserts no PLACE on the fresh lower bucket; `test_clocktick_only_stale_bucket_cancels`
(law 3); `test_shadow_does_not_fill_on_stale_bucket` with a fresh-bucket positive control isolating the
gate as cause. `test_params_load_and_sha_pin` asserts the new field = 30.0. Coverage matches the spec.

## Defect found and fixed

- **params.py docstring described the SUPERSEDED exclusion design** (`bucket_freshness_max_age_s` …
  "excluded from spot selection; if none fresh remain, cancel + stand down"). That is precisely the
  fall-through-to-a-lower-mid-bucket behavior R-STALE-SPOT forbids and that `_select_spot` /
  `_recompute_context` reject. Code was correct; the docstring contradicted the implemented law on a
  live-order-governing param (a future reader could "fix" the code toward the wrong doc). Rewrote the
  docstring to describe the SELECTED-spot gate accurately. Not part of the json → sha unaffected;
  808 tests still pass.

## Recommended default: keep 30.0

30.0 s is the right ship value. It targets the failure mode this PR closes — a bucket-connection stall
(or a partial blackout masked by the liquid co-listed 15M on the same connection) feeding spot
selection + the cap while the strike connection looks alive — without false stand-downs against the
few-second bucket cadence. The cap is a SECONDARY bound; the primary economic protection is W off the
1.0 s strike gate, which is untouched. Follow-up (not blocking): measure per-spot-bucket
inter-book-update gaps in the T-15..T-5 window from the recorder TOB journals (8/30+, non-holdout);
if the median gap is well under 30 s, tightening toward ~10-15 s would catch a stall sooner while
keeping margin. No data in-hand to justify changing it now.
