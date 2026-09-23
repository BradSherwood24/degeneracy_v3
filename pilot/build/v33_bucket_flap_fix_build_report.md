# V3.3 bucket-flap hotfix — build report (2026-09-23)

Branch `fix/v33-bucket-flap` off `origin/main` 07a2f80 (Phase H + L1 + L2 + L3 merged). Pure-core hotfix
in `service/v33/core.py` + `params.py` + `v33_params.json` (re-pin) + the driver's action-journaling; no
`service/v32/` change, no live tree, no network.

## The finding (first live DRY window, 07:00Z close 2026-09-23)
BTC spot straddled the 86,500 bucket boundary. `v33_eval.spot_Sd` flipped 86400<->86500 in tight bursts.
Every flip drove the core's bucket-change path (cancel-all 11 + place-all 11): the DRY window logged
**would_place 418 / would_cancel 418** (vs 11 + 11 legitimately) — armed, ~10,000 creates/day against the
8,000 budget, queue lost on every flap, pacer stalls. Separately, 15 sub-second `stale_or_missing_wing`
stand-downs each did cancel-all + re-place (~330 more orders). V3.2 paid 2 orders/flap; the ladder pays 22.

## Fix 1 — spot-bucket switch debounce + hysteresis (item 1)
The resolved spot bucket (`_select_spot`, highest yes-mid) is now mapped to an EFFECTIVE bucket via
`_resolve_effective_bucket` in `_recompute_context`. A switch away from the ladder's bucket
(`rest_bucket_Sd`) commits only when BOTH hold:
- **debounce** `bucket_switch_deb_ms` (default 3000): the new bucket has been the resolved spot
  CONTINUOUSLY for >= that long; a flip back (or to a different candidate) resets the timer;
- **hysteresis** `bucket_switch_hysteresis_usd` (default 15): the implied spot sits >= that many $ inside
  the new bucket at least once while pending.

While pending, the effective bucket stays the ladder's bucket, so `n_top`/cap/W and every roll and fill are
computed on the OLD bucket ("the ladder stays on the old bucket, rolls continue there, fills book there").
Only at commit does `spot_Sd` flip to the new bucket, and the existing (already-tested) cancel-all/place-all
bucket-change path runs ONCE. First placement (no ladder yet) follows the spot immediately — nothing to
protect.

**Implied spot / hysteresis — data-model note.** The core carries no raw BTC spot (the spot bucket is
resolved by yes-mid), so the hysteresis uses `_implied_spot`: the yes-mid-weighted centroid of the range
buckets' CENTRES (E[settlement] ~ current spot over the short horizon), which the core already has. The
candidate is accepted only if `floor + hyst <= implied_spot < floor + width - hyst`. If the ladder is too
thin to estimate (no weighted bucket), or `hysteresis_usd == 0`, it falls back to debounce-only. This is
the simplest correct combination given no spot feed; `bucket_switch_hysteresis_usd` is Brad's lever and 0
disables it. (An alternative — implied spot from the strike CDF — was rejected as heavier and the strike
ladder is sparse.)

## Fix 2 — stale/missing-wing stand-down HOLD (item 2)
On a `stale_or_missing_wing` stand-down WITH a live ladder, the core no longer cancels immediately: it HOLDS
the rests (no new places/rolls, no cancel) for up to `stand_down_hold_ms` (default 1500). If freshness
returns within the window it RESUMES; if the hold elapses it cancels-all as before. The hold is for the
RESTS only — a rung fill during the hold still books and spawns its wing batch (that path is independent of
`_converge`; wings are taker orders priced from the live book at fill time, exactly as the executor does),
and the wings take once the strike book is fresh again. With no live ladder to protect (e.g. first
placement), a stale wing stands down immediately as before. `stand_down_hold_ms == 0` restores the pre-fix
immediate cancel. The lifecycle journals as `stand_down_hold` / `stand_down_resume` / `stand_down_cancel`
(new reasons `stale_or_missing_wing_hold/_resume/_cancel` mapped in `run_v33._journal_action`); hold/resume
do NOT count as real stand-downs, only the terminal cancel does.

## Before / after (the real 07:00Z flap, fixture `tests/fixtures/v33/bucket_flap_20260923T070000Z.json`)
The fixture is the real `v33_eval` spot_Sd/W/cap sequence for t-730..t-715 (86 rows, **11 flips** between
86400/86500). Replayed through the core:

| core | bucket-change CANCEL_REST actions on the flap |
|---|---|
| debounced (deb 3000 / hyst 15) — the fix | **0** |
| control (deb 0 / hyst 0) — pre-fix behaviour | **55** (5 cancel-alls x 11 rungs) |

`test_fixture_flap_debounced_zero_cancel_all_vs_old_many` asserts new == 0 and old >= 6.

## Params (Brad's levers) — re-pinned
`v33_params.json` adds `bucket_switch_deb_ms: 3000`, `bucket_switch_hysteresis_usd: 15`,
`stand_down_hold_ms: 1500`. New sha
`3fe3919c5b31bbe9bd258fb7a82f5757a50ee0188b94c6bf0fe1f96f1fcb6aac`; the L3 sha is kept as
`PREVIOUS_V33_PARAMS_SHA256_L3`. Loader fails closed: debounce/hold >= 0, `0 <= hysteresis` and
`2*hysteresis < bucket_width`. The ceremony falsifier doc and the arming runbook sha references were
updated to the new pin (the falsifier-pins test requires it).

All three are levers: set any to 0 to restore the pre-fix behaviour for that mechanism.

## Tests
- New file `tests/test_v33_bucket_flap.py` (8): (a) 5 flips in 0.3 s -> 0 cancel-all; (b) a clean move that
  persists 3 s and is 15$ inside -> exactly one cancel-all/place-all; (c) a flip back at ~2.9 s resets the
  timer; a boundary oscillation (>3 s but <15$ inside) is blocked by hysteresis; (d) stale wing 800 ms then
  fresh -> hold+resume, no cancel; (e) stale 1600 ms -> cancel-all once; (f) a rung fill during a hold ->
  wing batch spawned, taken on the next fresh book; and the FIXTURE test above. All use an injected clock.
- Updated (asserted immediate bucket switches / immediate stale-cancel, now pass the 0-levers or reflect
  the hold): `test_v33_core.py::test_bucket_change_cancels_all_then_places_all_after_confirm`,
  `::test_bucket_change_after_partial_sweep_caps_at_k_minus_filled`,
  `::test_bucket_change_after_full_sweep_places_nothing`, and `test_stale_wing_cancels_ladder` ->
  split into `test_stale_wing_holds_then_cancels_ladder` + `test_stale_wing_cancels_immediately_when_hold_disabled`;
  `test_v33_golden.py::_sweep_params` (golden e) uses the 0-levers; sha-repin tests
  `test_v33_hardening.py::test_params_sha_repinned_and_previous_defined` and
  `test_v33_falsifier_pins.py::test_stop_pins_and_sha_match_doc` updated to the new pin.
- Suite: v33 250 passed; full pilot **1258 passed, 5 skipped, 0 failures/errors** (was 1253).

## L2/executor note
The core now emits far fewer cancel/place actions on a flap (one commit, or none) and adds the
hold/resume/cancel STAND_DOWN reasons. The executor is unchanged (it never acted on STAND_DOWN; the driver
journals + counts it). No proxy / budget change here; the budget pressure the finding flagged is removed at
the source (the flap no longer generates orders).

---

## Round 2 (2026-09-23) — PR #89 review (APPROVE WITH NITS)

Reviewer replay of the real hour: pre-fix 264 creates / 23 switches -> shipped 22 creates / 1 switch.

### NIT-1 (strand) — FIXED: `bucket_switch_max_pending_ms` (default 15000 = 5x deb)
The switch CANDIDATE is `_select_spot` (highest yes-mid) but the hysteresis GATE is the yes-mid-weighted
centroid; on ~10k boundary ticks they disagree, and a spot parked 5-14$ inside the new bucket satisfies the
3 s debounce yet is blocked by the 15$ hysteresis for the WHOLE window — stranding the ladder on the old
(now-wrong) bucket. Added an ANTI-STRAND cap: once the new bucket has been the resolved spot continuously
for >= `bucket_switch_max_pending_ms`, commit the switch even if the hysteresis never held (the debounce
still applies; a flip back still resets both timers). Loader fails closed if
`bucket_switch_max_pending_ms < bucket_switch_deb_ms`. Re-pinned sha
`20188bbe76b592198f2f3aa2f1b8ff8857b12cc6b5ad9f5d3f5d75f76030cc78` (FLAP-R1 sha kept as
`PREVIOUS_V33_PARAMS_SHA256_FLAP_R1`); ceremony + arming docs updated. Tests: spot 8$ inside for 20 s ->
exactly one switch at ~15 s (committed with `pending_switch_hyst_met` False); flip back at ~14 s -> no
switch. Existing tests unchanged.

### NIT-2 (fill-during-hold naked tail when W never returns) — doc only, no code change
A rung can FILL during a stale-wing HOLD. Its wings are taker orders priced from the live book at fill
time, so they hedge normally once the strike book returns. But if W NEVER returns before the settle cutoff,
that filled lot reaches the cutoff unhedged — a `one_legged` set, identical to any fill whose wings never
priced (already latched by the existing one-legged rule + S1). This is a monitoring item, not a new stop:
**whenever a window logs `stand_down_cancel` (a hold that expired to a real cancel), check that window's
report `one_legged` count** — that is the case where a fill-during-hold could be a bounded naked tail.
`stand_down_hold_ms` = 0 disables the hold (immediate cancel, pre-fix behaviour) if the tail risk is judged
worse than the flap-avoidance benefit. Added to `ops/V33_RUNBOOK.md` §9 alarms.

### Levers (updated)
- `bucket_switch_deb_ms` (3000), `bucket_switch_hysteresis_usd` (15),
  **`bucket_switch_max_pending_ms` (15000 = anti-strand cap)**, `stand_down_hold_ms` (1500). Each is a
  lever; 0 (or, for the cap, == deb) restores the narrower behaviour.

### Tests / suite (R2)
+2 flap tests (anti-strand commit; flip-back reset); sha-pin test updated to the new pin + FLAP_R1 chain.
Full pilot suite green (see the handback for the final count).
