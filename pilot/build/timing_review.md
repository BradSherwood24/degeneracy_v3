# Timing / sequencing review — two-phase wake (opus48 REVIEWER)

Reviewed 2026-08-21. FINDINGS-ONLY: no source file modified (a live scheduled run loads this code at
:40). Read `service/{run_window,wake,quintile,sigma_feed,signal,record_window,ws_client,shakedown}.py`,
`ops/register_task.ps1`, `sim/census.py`, and both phase-4 reports. Suite reproduced **365 passed**
(read-only). No `.env`/`*.pem`, no sealed tape, no live network.

Scope = the timing/sequencing of the two-phase wake ONLY (leg-open respect, pairing-after-strike,
clock discipline, deadline arithmetic, late/early/skewed fire, the 21:00 + dual-gen hours, σ̂ prefetch
vs poll, task-fire edges). Economics/executor/stops internals were reviewed only where they touch
timing.

## VERDICT: SOUND-WITH-FLAGS

The core two-phase invariants hold as built: nothing anchor-dependent runs before the 15M leg opens,
and the hourly leg is paired strictly after the 15M strike materializes. The flags are one MAJOR
correctness gap that is the already-known **F2 dual-generation** issue (not fully fail-closed for the
pairing subset), one MAJOR ops hardening item (unpinned MultipleInstances), and several MINOR
clock-reference-frame issues that all fail in the SAFE direction (spurious stand-down, never a bad
fire). No timing path was found that pairs early, trades a settled window, or fires on a pre-open 15M.

---

## FINDINGS

### F1 — MAJOR — RESOLVED 2026-08-21 — dual-generation hourly: pairing uses ONE selected ladder, not census's pooled strikes; not fully fail-closed
**Path:** `wake.discover_legs` → `_select_smallest_window` → `_make_leg` sets `hourly_leg.markets` to a
SINGLE generation's markets; `run_window._resolve_anchor` (line 796-800) passes only
`wake.hourly_leg.markets` to `sigma_feed.assign` → `nearest_hourly_strike` pairs within that subset.
Census (`sim/census.py` `h1_by_ct[ct]`, lines 268-271, 336-341) pools ALL 1H markets sharing the
close (both generations) and, crucially, EXCLUDES the hour as `EXCL_NEAREST_TIE` when the two nearest
pooled strikes are equidistant (e.g. an identical strike present in both generations).
**Scenario:** on a co-settling hour with >1 hourly generation whose SELECTED (smallest-window)
generation's step equals `expected_step`, `ladder_check` returns `ok=True` (strangle NOT disabled),
and the pilot pairs within that one generation. This (a) can pick a different K than census's pooled
nearest, and (b) never sees census's cross-generation tie-exclusion — so a Q1-strangle can FIRE on a
pairing the sim never validated. The observed Friday 2026-07-31 21:00 case ($500 week-early vs $250
day-before) IS caught (smallest-window selects $250; expected on Friday = $500; step mismatch →
`strangle_disabled` + alarm). The general (non-Friday, matching-step) dual-gen case is NOT — "something
worse" than a clean stand-down is reachable. This is the documented-open F2; bounded because dual-gen
is rare and the sole observed instance is a Friday (caught). Sub-$1 also routes off this same
possibly-divergent leg.
**Fix (one line):** in `wake.discover_legs`, when >1 hourly ladder co-settles at the target, fail
closed (set `strangle_disabled`+alarm, or stand down) until the F2 pooling rule is ruled.
**RESOLVED 2026-08-21 (opus48 builder):** rather than fail-closed-until-ruled, the pilot now
REPRODUCES census's pooling. `wake.sweep` retains ALL live co-settling hourly generations
(`live_hourly_ladders` -> `WakeResult.hourly_ladders`; `hourly_pool_markets` flattens them);
`run_window._resolve_anchor` pairs the anchor against that POOLED strike set (`hourly_pool_markets`)
via the existing `nearest_hourly_strike`, reproducing census `hole_G` / nearest-below-and-above and
the equidistant-tie exclusion (an identical strike in two generations is now a cross-generation TIE ->
stand down). The ladder-map check + the WS subscription follow the CHOSEN market's generation
(`_chosen_hourly_leg` / `_chosen_ladder_check`; commission "validate the ladder of the specific market
actually paired"). Single-generation behavior is byte-unchanged (pool falls back to the one selected
generation; `_chosen_ladder_check` reuses `wake.ladder`). Tests: `test_f1_pooled_pairing_finds_
nearest_in_nonselected_generation`, `test_f1_cross_generation_equidistant_tie_stands_down`,
`test_f1_ladder_check_applies_to_chosen_generation`, `test_f1_ladder_check_disables_when_chosen_
generation_bad`, `test_f1_single_generation_pool_falls_back_unchanged` (test_run_window.py) +
`test_live_hourly_ladders_pools_all_generations`, `test_live_hourly_ladders_drops_all_dead_generation`
(test_wake.py). This also closes the long-open Phase-1-review F2 pairing-model item.

### F2 — MAJOR — scheduled task sets no explicit `-MultipleInstances`; overlap guard relies on the platform default + a TOCTOU-prone reconcile-first
**Path:** `ops/register_task.ps1` line 65/92 — `New-ScheduledTaskSettingsSet -StartWhenAvailable
-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries` with NO `-MultipleInstances`. The overlap
protection is only `run_window` reconcile-first (steps c), which the phase-4 review already showed has
a poll-side TOCTOU (finding 1 there, fixed for S3 but the entry-time flat-read race between two
processes is not closed).
**Scenario:** normal cadence has no overlap (a process lives ~:40→:00:10, ~20 min; next fires :40, a
~20 min gap). Overlap becomes possible if a process outlives 40 min — e.g. an early misfire (see F3
frame) leaving a process asleep in `_resolve_anchor` across the next :40 fire, or a hung
poll/window. If two ARMED instances overlap and both read flat before either trades, reconcile-first
does not stop a double entry (up to 2× the per-window pair across processes). If the platform default
is `IgnoreNew`, a hung instance instead silently SKIPS the next window (a different failure). Either
way the behavior is unpinned.
**Fix (one line):** add `-MultipleInstances IgnoreNew` to the settings set (pins single-instance;
documents intent) — and/or tighten reconcile-first to a proxy-side single-flight before arming.

### F3 — MINOR — unparseable/absent 15M `open_time` makes the anchor-poll time out ~4 min BEFORE the real open (spurious EXCL_NO_ANCHOR)
**Path:** `run_window._resolve_anchor` lines 761-766 — on a parse failure, `open_epoch = self.clock()`
(the :40 wake time), so `poll_deadline = min(clock()+45, window_deadline) ≈ :40:45`. The loop then
polls immediately (now ≥ open_epoch), the still-strikeless market yields no anchor, and the deadline
check trips at ~:40:45 — ~4 minutes before the market actually opens at :45. The comment calls this
"fail-open to poll," but it is really a premature timeout in the wrong reference frame.
**Scenario:** any window where the 15M `open_time` is empty/malformed → immediate spurious stand-down.
Safe direction (no trade); live obs says initialized markets DO carry `open_time`, so this is latent.
**Fix (one line):** fall back to `close_epoch(close_time) - 900` (or the window deadline), not
`self.clock()`, so the poll budget stays anchored to the real open.

### F4 — MINOR — machine-clock skew fast by >~30s at the poll → poll_deadline (an absolute epoch) already passed when the real strike appears → spurious EXCL_NO_ANCHOR
**Path:** `run_window._resolve_anchor` compares `self.clock()` (machine wall) against `poll_deadline =
min(open_epoch+45, window_deadline)` (absolute server epochs). The strike materializes ~13s after the
real open.
**Scenario:** if the machine clock leads real time by more than ~32s, at real open the loop does its
one post-open fetch (strikeless), then `now >= poll_deadline` trips → stand-down before the strike is
ever visible. Safe direction. The DECISION itself is immune — `signal.decide` uses `event.server_ts`,
not the machine clock — so this only costs a spurious stand-down, never a mistimed fire.
**Fix (one line):** widen the poll bound to `window_deadline` (keep the +45s as a soft/log threshold),
or add a clock-sanity guard comparing machine time to a server timestamp.

### F5 — MINOR — missing WS server-ts falls back to the machine clock (not fail-closed as the checklist claims)
**Path:** `run_window.LiveWindowRecorder._drive` lines 242-245 — `server_ts = _parse_server_ts(payload);
if server_ts is None: server_ts = self.clock()`, then drives `decide()`. The review checklist item 2
asserts "else freshness fails closed and nothing fires," but the code fabricates `now = machine clock`,
so the leg reads FRESH (age ≈ 0) and `decide` runs with a machine-clock `t_minus` mixed against the
server-epoch `T`.
**Scenario:** a book frame lacking both `ts_ms` and `ts` would drive a firing decision on machine time
under clock skew. Normal frames carry `ts_ms`, so latent. Safe only if the clock is accurate.
**Fix (one line):** on `_parse_server_ts is None`, skip `driver.on_book_update` (fold the book but do
not drive `decide`) — fail closed as documented.

### F6 — NOTE — window-driver termination uses the machine clock while the decision cutoff uses server ts
**Path:** `_dial_loop` (`while recorder.clock() < deadline`), `_s3_poll_loop`, watchdog — all machine
clock; `deadline = close_epoch(close)+GRACE` is an absolute epoch. A machine clock LEADING server by
>10s (the grace) would stop the window before the real close (could clip the last seconds of the entry
window); LAGGING runs harmlessly past. Bounded by the 10s grace for small skews; `no_orders_after` (1s)
still guards the true end via server ts. No change needed at pilot size; documented for awareness.

### F7 — NOTE — the connect-gate is effectively dead after the two-phase refactor
**Path:** `execute` line 942 sets `_connect_not_before = _connect_gate(wake)`; `_connect_gate` (1151-
1163) returns `gate` only if `gate = open_epoch-5s > self.clock()`. But `_resolve_anchor` returns ONLY
after `now >= open_epoch`, so by the time the gate is computed `clock() >= open_epoch > open-5s` →
`_connect_gate` always returns None → `_await_connect_gate` returns immediately. The "hold the WS dial
until ~open" protection is actually provided by phase B's wait, not the gate. Harmless redundancy —
flagged so no future change relies on the gate as an independent interlock.

### F8 — OK — σ̂ trailing prefetch at :40 excludes the current unopened market and agrees with the sim
`fetch_trailing_15m` (min=T-7200, max=T) returns the current strike-less market too, but
`anchor_tape_from_markets` skips it (`floor_strike is None`), so phase A supplies only T-900..T-7200
(all strike-bearing at :40). Phase B merges the freshly-polled current (carrying A(T)); `assign` dedups
by `id()` (distinct REST objects, no false merge) and `anchor_tape_from_markets`'s strikeful-preference
makes the polled T-anchor win over any strike-less T record. 15M strikes are fixed at open, so
prefetch-at-:40 and poll-at-:45 carry the same values the sim (`census.sigma_hat`) uses → same σ̂.
Missing/settled trailing tolerated per A3.2 (`InsufficientTape` → EXCL_SIGMA → sub-only fallback).

### F9 — OK — late fire (13:47/:52/:59) still joins the window and gets the strike
`_resolve_anchor` does at least one REST fetch once `now >= open_epoch` and checks the anchor BEFORE
the deadline test, so a late wake finds the already-materialized strike and proceeds. Window joined via
the absolute `close+grace` deadline; decision via server ts. A post-close fire (`next_top_of_hour`
rolls to the next :00) targets the NEXT window — no settled-window trading. Sub-note: a very late wake
trades on thin book warmup, but the `_both_fresh` gate still binds — acceptable per the sim mapping.

### F10 — OK — failure taxonomy lands in the intended buckets, distinguishably journalled/ledgered
- strike never appears → `_resolve_anchor` None → `phase_b_timeout` + `EXCL_NO_ANCHOR`, exit 0
  (ledger `stand_down_reason` = "EXCL_NO_ANCHOR…", `quintile_reproduced=None`).
- strike ok, no hourly / nearest-tie / degenerate G → `fallback_pair` stand_down → `quintile` record
  `stand_down=True` with a distinct reason ("no hourly leg" / "equidistant nearest-strike tie" /
  "degenerate corridor"), exit 0.
- anchor ok, σ̂ fail → `fallback_pair` resolves the pair, `strangle_disabled=True`, `_sub_only_quintile`
  bucket → sub-$1 STILL runs (`quintile_reproduced=False`).
- ladder-map deviation → `wake.ladder.strangle_disabled` OR-ed in `_apply_outcome` → strangle off,
  sub-$1 runs. All four are separable in the ledger row.

### F11 — OK — the 21:00 $250/$500 hour (single-generation) is step-agnostic
`expected_step` returns 250 (or 500 on Friday); `observed_step` is the min consecutive gap;
`nearest_hourly_strike` selects by `min |strike-A|` with equidistant→TIE — no $100 assumption anywhere.
(The multi-generation variant of this hour is F1.)

### F12 — OK — DST is UTC-driven end to end
`next_top_of_hour_iso` computes the close from UTC (`time.time`), so a fall-back doubled local hour
yields two DIFFERENT UTC closes (two distinct windows, no conflict); spring-forward skips one local
fire (one missed window); the transition hour stands down cleanly if no leg is discoverable. Minute
arithmetic verified in the phase-4 review.

### F13 — OK — the 06–09 UTC missing-hourly period stands down cleanly
`discover_legs` finds no hourly ladder → `StandDown` → `prepare` returns stand_down → exit 0, one
ledger row. No pairing attempted.

---

## The captain's two named questions

**(a) "the 15-minute market won't open until 5 minutes after wake" — fully respected? → YES.**
Phase A (`prepare`, :40) is REST `/markets` only: leg discovery, hourly ladder-map check, balance,
and the σ̂ trailing-anchor prefetch. It never touches the 15M book, price, freshness, or WS, and never
computes an anchor (the 15M leg is "initialized" with no `floor_strike`, and its `floor_strikes` tuple
is empty). Phase B (`_resolve_anchor`) issues its first REST poll only after `now >= open_epoch`, and
an absent strike is treated as ABSENCE (loop/timeout), never as data. The WS dial and the 15M
subscription happen only in phase B, after open. No pre-open 15M dependency exists.

**(b) "the 1-hour market is decided only after the 15-min strike is set" — airtight? → YES (code-level),
with the F1 caveat on WHICH strikes are in the pool.**
The hourly leg is chosen exclusively inside `_resolve_anchor`/`_apply_outcome` (phase B), which run
only after the strike materializes; `_anchor_at`+`nearest_hourly_strike` are never called in
`prepare()`/phase A (grep-confirmed: the only `_anchor_at` call site in `run_window` is line 790).
`WindowState` (carrying `high_ticker`/`low_ticker`) is built ONLY in `_apply_outcome`, reached only on
a non-None, non-stand-down phase-B outcome — no default/zero anchor, no cross-window cache
(process-per-window). The one caveat is F1: the pairing is airtight in TIMING but pools only the
selected hourly generation, which can diverge from census on a dual-generation hour.

---

## One line per finding
- F1 MAJOR — dual-gen hourly: pilot pairs one selected ladder vs census's pooled strikes; not fully fail-closed (strangle can fire on an unvalidated pairing).
- F2 MAJOR — task registration sets no `-MultipleInstances`; overlap guard is only TOCTOU-prone reconcile-first.
- F3 MINOR — unparseable 15M `open_time` → poll times out ~4 min before the real open (spurious EXCL_NO_ANCHOR).
- F4 MINOR — machine clock fast by >~30s → poll_deadline passed before the strike appears (spurious EXCL_NO_ANCHOR).
- F5 MINOR — missing WS server-ts falls back to the machine clock, not fail-closed as the checklist claims.
- F6 NOTE — window-driver deadline is machine-clock while the decision cutoff is server-ts (bounded by the 10s grace).
- F7 NOTE — the connect-gate is dead code after two-phase (phase B already waits past open); harmless redundancy.
- F8 OK — σ̂ prefetch excludes the unopened current market and agrees with the sim.
- F9 OK — late fire still joins the window and gets the materialized strike; no settled-window trading.
- F10 OK — the failure taxonomy lands in the intended buckets, distinguishably journalled/ledgered.
- F11 OK — the single-generation 21:00 $250/$500 hour is step-agnostic in both the ladder check and the pairing.
- F12 OK — DST is UTC-driven; no double/one-conflict window.
- F13 OK — the 06–09 UTC missing-hourly period stands down cleanly.

Answers: (a) YES. (b) YES (code-level; F1 qualifies which strikes are pooled, not the timing).

---

## DISPOSITION (opus48 builder, 2026-08-21)

Baseline suite reproduced at **365 passed**; after the fixes + 9 new regression tests: **374 passed**.
No `.env`/`*.pem` read, no live network, no sealed tape. Decimal preserved on every money path;
executor/reconciler/stops/signal decide semantics untouched.

- **F1 MAJOR — FIXED.** Phase-B pairing now pools ALL live co-settling hourly generations
  (`wake.hourly_ladders`/`hourly_pool_markets`) and mirrors census `nearest_hourly_strike` +
  cross-generation equidistant-tie exclusion; ladder-map check + WS subscription follow the CHOSEN
  market's generation. Single-generation behavior unchanged. (Files: `wake.py`, `run_window.py`.)
- **F2-ops MAJOR — FIXED.** `register_task.ps1` now sets `-MultipleInstances IgnoreNew` (pins
  single-instance; a hung/overrunning window is skipped, not overlapped). Dry-run parse test asserts it.
- **F3 MINOR — FIXED.** An unparseable/absent 15M `open_time` falls back to `close_epoch(close)-900`
  (the real open), not `self.clock()`, so the poll budget stays anchored to the real open instead of
  timing out ~4 min early. (Regression: `test_f3_unparseable_open_time_anchors_poll_to_close_minus_900`.)
- **F4 MINOR — ACCEPTED (accepted-risk).** A machine clock fast by >~30s can trip a spurious
  EXCL_NO_ANCHOR before the strike is visible; it fails in the SAFE direction (a stand-down, never a
  mistimed fire — `signal.decide` reads `event.server_ts`, not the machine clock) and Brad trusts the
  host clock. No code change.
- **F5 MINOR — FIXED.** A WS frame with no server ts no longer drives `decide()` on the machine clock:
  `LiveWindowRecorder._drive` folds the book, journals `ws_frame_no_server_ts`, and skips the decision
  core (fail-closed as the checklist claims). (Regression: `test_f5_tsless_frame_never_reaches_decide`.)
- **F6 NOTE — ACCEPTED.** Window-driver termination on the machine clock vs the server-ts decision
  cutoff; same safe direction as F4, bounded by the 10s grace and the 1s `no_orders_after`. No change.
- **F7 NOTE — ACCEPTED.** The connect-gate is dead after the two-phase refactor (phase B already waits
  past open); left as harmless defense-in-depth. Flagged so no future change relies on it as an
  independent interlock. No change.
- **F8–F13 — OK as reviewed.** No action.
