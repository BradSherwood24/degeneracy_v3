# Phase 4 build report — service harness + ops (opus48 BUILDER)

Built 2026-08-21. Location: NEW files only under `pilot/service/`, `pilot/ops/`, `pilot/tests/`.
No existing module was modified (ADD-only ownership honored). No `sim/` file touched; no `.env`/`*.pem`
read; no sealed-day file read by any code path; no live network in any test. All REST/WS access is
via the proxy at `http://127.0.0.1:8642` (or the proxy's own `/health`). This process never sets
`ALLOW_ORDERS` (the proxy owns it) — it only sets the independent client-side arm, and only when the
S5 gate passes.

## Component inventory

| # | File | Component | ~Lines |
|---|------|-----------|--------|
| 1 | `service/run_window.py` | the armed/dry/shakedown window lifecycle (startup-order law + WS drive + finalize) | ~640 |
| 2 | `service/sigma_feed.py` | trailing-15M fetch for sigma-hat + quintile assignment with fail-closed fallback | ~200 |
| 3 | `service/pilot_ledger.py` | append-only per-window ledger + promotion-gate counters + query CLI | ~250 |
| 4 | `ops/register_task.ps1` | Task Scheduler registration (UTC:40 hourly, mode.txt-driven, `-DryRun`) | ~110 |
| 5 | `ops/unregister_task.ps1` | task removal (`-DryRun`) | ~35 |
| 6 | `ops/mode.txt` | the mode switch (`shakedown` initial) | 1 |
| 7 | `ops/runbook.md` | operator doc (start/stop/kill, modes, alarms/stops, gates, arming, 24h dry cycle, PENDING-BRAD) | — |
| 8 | `tests/test_run_window.py`, `test_sigma_feed.py`, `test_pilot_ledger.py` | 42 new tests | — |

## Tests

`cd pilot && python -m pytest tests/` -> **338 passed, 0 failed** (~3.9s). Of these, **51 are new**
(run_window 27, pilot_ledger 13, sigma_feed 11). The other 287 (Phases 1–3 + review probes) are
untouched by me and still green. NOTE: the pre-existing suite grew from 268 -> 287 during this build —
the concurrent Phase-3 reviewer added tests to existing files; I modified none of them, and my
additions compose against their PUBLIC interfaces (see "Coupling"). The +9 run_window tests over the
first cut lock in the Phase-3-review handoff wiring (F3/F5/F7/F8/F9 — see the handoff section).

New-test coverage:
- **startup-order law:** armed refused on a DRAFT falsifier -> degrade to dry (temp falsifier file);
  armed degraded on missing `/health` caps (fake health); armed ARMS on FROZEN + caps + flat;
  reconcile-first REFUSAL on a nonzero position in our series; unrelated-series position ignored.
- **stand-down clean exit:** wake StandDown and NoQuintile-no-pair both -> exit 0 with a ledger entry.
- **crash-mid-window:** an injected driver that buffers a ws record then raises -> journal flushed
  (buffered record + traceback survive) + exit 1 + ledger `exit_code:1`.
- **zero-orders invariant:** clean shakedown and dry runs -> `orders_attempted == 0` (the dry-cycle
  criterion).
- **NoQuintile fallback:** sub-only routing bucket chosen, strangle disabled.
- **mode.txt parsing:** cli wins; file read; unknown/empty/missing -> `shakedown` (fail closed).
- **sigma_feed:** happy full quintile; missing tape -> sub-only fallback; no-anchor / no-hourly /
  nearest-tie / degenerate-G -> whole-window stand down; fallback orientation (high = higher strike);
  shell fetch sends NO status filter, fail-closed `[]` on error, merges the current-close market.
- **pilot_ledger:** append/load; S4 total; day filter; counter folding; **P1 boundary cases**
  (fill-rate 6/10 pass vs 5/10 fail; 10 vs 9 fired; slippage 0.010 pass vs 0.011 fail; imbalance/S1
  fail); P2 requires 2-pair evidence + book-walk + P1 still holding.
- **register/unregister DRY parse:** runs the real `powershell -File <script> -DryRun` (PowerShell IS
  present here, so the whole script is genuinely PARSED) and asserts the emitted command is
  well-formed (`Register-ScheduledTask`, `New-ScheduledTaskTrigger`, `run_window`, `DRY RUN`) — NO
  registration happens; falls back to a text-structure assertion where PowerShell is absent.

## The startup-order law (run_window, hard sequence)

`prepare()` runs steps (a)–(e) and returns a `Plan`; `execute(plan)` runs (f)–(g). The order NEVER
varies:

- **(a) policy + sha check** — `load_policy()` enforces the frozen pin `1b01fd98…3656`. A mismatch or
  unreadable file -> stand down (fail closed), no window runs.
- **(b) arming (S5)** — ARMED and DRY both call `stops.arming_check(falsifier, /health, policy_verified)`
  (dry for observability only). ARMED that fails S5 -> **degrade to dry** with a loud `degrade_to_dry`
  journal record; it NEVER arms. SHAKEDOWN skips arming.
- **(c) reconcile-first** — GET `/portfolio/positions` via the proxy; any position in `KXBTC15M*` /
  `KXBTCD*` with net != 0 -> journal + `inherited_position_refusal` + stand down (flatten is NOT
  automatic — PENDING-BRAD F4/F8). A positions-READ failure is fatal for ARMED (cannot confirm flat ->
  stand down); dry/shakedown log an alarm and continue (no orders anyway).
- **(d) wake discovery** — `WakeContext.sweep`; StandDown (missing/closed/inactive leg, incl. 06–09Z
  missing-hourly) -> exit 0.
- **(e) sigma_feed -> quintile** — trailing tape fetch + `compute_window_stats`; NoQuintile with a
  resolvable pair -> strangle disabled, sub-$1 continues; NoQuintile with no pair -> stand down.
- **(f) connect + run** — SHAKEDOWN composes `ShakedownRecorder`; DRY/ARMED use the new
  `LiveWindowRecorder` (**armed is NEVER routed through ShakedownRecorder**). ARMED routes FIRE ->
  Executor with the Reconciler + StopController wired; the seeded state has `shakedown=False` only when
  truly armed, so dry always emits WouldFire only.
- **(g) close + grace** — flush the journal to `journals/<close>.jsonl`, append the pilot-ledger
  summary, exit 0. Any unhandled exception -> freeze the executor, journal the traceback, flush what is
  buffered, exit nonzero (tested).

Robustness on the dial: `_run_ws_window` (a new driver reusing `record_window`'s pure `watchdog_action`)
adds **bounded exponential-backoff re-dial** (1s→30s cap), a **consecutive-failure cap** (8), and clean
give-up at the window deadline — Phase-1 review F6, achieved without modifying `record_window`.

## The DST approach chosen (and confessed)

**Fixed-local-minute hourly repeat, recomputed at registration; re-run after a timezone/DST-policy
change (belt-and-suspenders).** `register_task.ps1` computes, once at registration, the local minute
that corresponds to UTC :40 from the machine's current UTC offset
(`((40 + offsetMinutes) % 60 + 60) % 60`) and registers a trigger that fires at that minute and
**repeats every 1 hour**. Because an hourly repeat at a fixed minute keeps firing at that minute of
every local hour, and every real DST shift is a whole hour, the UTC:40 alignment is preserved across a
DST change for every whole-hour-offset timezone with NO re-registration. The only case that actually
needs a re-run is a fractional-hour offset change (essentially only Lord Howe Island's 30-min DST); the
single DST-transition hour can fire once early/late, which is harmless (the window just stands down if
no leg is discoverable). The runbook documents this loudly and tells the operator to re-run the script
after any timezone/DST-policy change. Verified on this machine: UTC offset −240 -> local minute 40,
first fire `…T02:40:00`, repeats 1h.

The MODE is deliberately NOT baked into the registration — the scheduled action runs
`python -m service.run_window` with no `--mode`, so `run_window` reads `ops/mode.txt` at run time and
Brad flips shakedown/dry/armed WITHOUT re-registering.

## CONFESSIONS (judgment calls / interpretations)

1. **NoQuintile fallback + sub-only quintile label.** The task requires "NoQuintile -> strangle stands
   down, sub-$1 unaffected", but `signal.decide` routes sub-$1 by the quintile (an out-of-range
   quintile routes to NO sources, fail closed). To keep sub-$1 alive when sigma-hat can't be reproduced
   (e.g. EXCL_SIGMA — trailing tape missing) but a clean pair still resolves, `sigma_feed.fallback_pair`
   resolves the high/low pair without sigma-hat, and `run_window` labels the window with a **sub-only
   routing bucket** (the lowest quintile whose sources are `sub$1-flip` WITHOUT the strangle — q1 in the
   frozen roster). The strangle is independently disabled, so the label affects only which routing the
   sub-$1 flip takes. This CAN diverge from the offline sim's true quintile label (the sim's replay tape
   usually has the full trailing tape), so it is a named live/sim delta the paired report will surface.
   When even the pair cannot resolve (no anchor / no hourly leg / nearest-tie / degenerate G) the whole
   window stands down.

2. **Trailing-tape query sends NO status filter.** Unlike `WakeContext._fetch_series_markets`
   (`status=open`), `sigma_feed.fetch_trailing_15m` omits status because the 8 trailing 15M anchors have
   already SETTLED at wake (Phase-2 confession #1). If the live `/markets` will not return settled
   markets by close-ts, sigma-hat cannot be built live and EVERY window degrades to sub-$1-only via the
   fallback — the safe direction, surfaced on the first dry runs. Live-verification item.

3. **The current-close 15M market is merged into the anchor tape.** `SigmaFeed.assign` merges
   WakeContext's fifteen-leg markets with the trailing fetch, so the anchor A at T is available even if
   the trailing fetch returns nothing — keeping sub-$1 tradeable when only sigma-hat is missing.

4. **Subscription defaults to the paired decision legs only** (`--max-hourly-strikes 0`), plus the 15M
   market(s); a positive N widens the hourly ladder and a negative value subscribes the full ladder.
   The decision pair (high/low tickers) is ALWAYS included even under a cap (a bare first-N slice could
   drop the paired hourly strike). Phase-1 defaulted to the full ~188-strike ladder for spine
   completeness; the harness only needs the two decision legs' books, so it defaults light. Confessed as
   a departure from the Phase-1 default.

5. **Second-pair book-walk is a proxy.** At IOC-batch dual size the response gives one blended
   `average_fill_price` per leg, not 1st-vs-2nd-contract prints, so `_book_walk` reports the avg-fill-
   vs-entry-ask spread per leg as the available proxy and the P2 gate checks the field's PRESENCE on a
   2-pair window. A precise per-contract walk needs per-fill records — live-verification item.

6. **ThreadSafeJournal wrapper.** Phase-1 confession #11: the Journal is single-thread. In ARMED the
   reconciler polls on a second thread while the WS loop appends, so `run_window` wraps the Journal in a
   lock-guarded drop-in (append/flush/len/records). ARMED also sets `capture_tops=False` (the append+len
   race in `WindowRecorder._capture` could mis-key a top; the journal is the source of truth in armed,
   and book_tops is a Phase-1 replay-parity nicety). dry/shakedown keep `capture_tops=True`
   (single-thread).

7. **Reconcile-first stands down in ALL modes on an inherited position** (not just armed). An inherited
   position means something is wrong; the safe, consistent behavior is to not run the window at all. In
   shakedown/dry there are no orders anyway, but standing down keeps the process-per-window "never
   resume on an inherited position" law uniform.

8. **`_build_ledger_entry` computes the S4 running total by reading the ledger + this window's delta**
   before appending — so the summary line carries the day-cumulative realized figure, while the query
   CLI recomputes it independently.

## Phase-3-review handoff wiring (the 8 items, folded in)

The Phase-3 adversarial review (`phase3_execution_review.md`, APPROVE-WITH-FIXES) flagged items the
harness must bind. Per-item status in the delivered code:

1. **F3 — S4 daily-loss + A4 guard-trips persisted per UTC day — WIRED.** `_ledger_day_totals()` sums
   `realized_delta` and `guard_trips` over the ledger entries sharing this window's UTC day; the armed
   `StopController.state` is SEEDED with those totals (`daily_realized`, `guard_trips`) so the caps are
   enforced per UTC day, not per window. The arming step additionally applies an **S4 day-lock** (if the
   day's realized loss already ≤ −$5.00) and an **A4 day-lock** (if the day's guard trips ≥ 5) that
   REFUSE armed and degrade to dry. The ledger entry now carries `guard_trips` + `day_realized_seed` +
   `day_guard_trips_seed` so the next process reads a truthful running total. Tests:
   `test_F3_armed_refused_by_s4_day_lock_from_ledger`, `…_a4_day_lock_from_ledger`,
   `…_seeds_stopstate_from_ledger_day_totals`.
2. **F5 — S3 poll gated on inflight_cids() — WIRED.** `_s3_poll_once()` skips the diff entirely while
   any order is in flight (checked BEFORE and AFTER the poll, closing the accept-fill/record race), and
   only trips S3 when idle and a real mismatch stands. It runs after each entry+rebalance settles and on
   a periodic `_s3_poll_loop` (armed only, blocking `rest_get` off-loaded via `run_in_executor`). Tests:
   `test_F5_s3_poll_skips_while_order_in_flight`, `…_trips_when_idle_and_mismatch`.
3. **F7 — arm interlock — WIRED (ADD-only `HarnessExecutor`).** Once any `set_armed(False)` fires (a
   stop trip, or the crash disarm), `HarnessExecutor` latches `_arm_locked` and REFUSES every later
   `set_armed(True)` within the process — the interlock is structural, not conventional. `run_window`
   never calls `set_armed(True)` after construction anyway. Test:
   `test_F7_harness_executor_never_rearms_after_stop`.
4. **F8 — stop-flatten exempt from the client rate budget — WIRED (`HarnessExecutor`).** A
   `stop_authorized` FLATTEN dispatches under a temporarily-lifted token budget (held under the
   executor's own reentrant lock so no concurrent dispatch sees the swap), delegating the actual POST to
   `Executor` — risk reduction is never starved; the proxy's persisted daily budget stays the real cap.
   A strategy order under the same exhausted budget is still refused. Test:
   `test_F8_flatten_exempt_from_rate_budget_but_strategy_is_not`.
5. **F9 — arming caps VALUE-agreement — WIRED.** `_caps_agree(health)` requires the proxy
   `max_contracts_per_order` ≥ requested pairs AND ≤ the pilot dual-size ceiling (2) AND ≥ the executor's
   own per-order cap, and the proxy prefix whitelist to cover both our series; any disagreement refuses
   armed (degrade to dry) and journals the mismatch. Tests:
   `test_F9_armed_refused_when_proxy_cap_exceeds_pilot_ceiling`, `…_prefixes_miss_our_series`.
6. **F6 — reduce_only on rebalance sells + the executor flatten path — WIRED.** `_maybe_rebalance`
   sets `reduce_only=True` on every `SellDown` intent (a sell-down can never open the opposite side); a
   `RetryBuy` carries no reduce_only (it is a buy). The stop-flatten routes through `StopController.trip`
   → `build_flatten_intent` (which already sets `reduce_only=True`). No sell can increase a position.
7. **Envelope path constants, no literals — CONFORMS.** `run_window` references no order-create path
   literal (the Executor owns the path; the harness only uses READ paths like `/portfolio/positions`).
   The endpoint fix (envelope now targets `/trade-api/v2/portfolio/events/orders[/batched]`) is picked
   up transparently. The F8 test asserts the POST path via the imported `SINGLE_CREATE_PATH` constant,
   never a literal.
8. **F14 full-suite re-run — DONE.** `python -m pytest tests/` -> 338 passed, 0 failed, no interleave
   damage.

Two review items are NOT harness-owned and are left as-is (correctly): F4 (in-window crash supervisor —
DEFERRED/PENDING-BRAD; the live guard is reconcile-first, which stands down on any inherited position)
and F1/F2 (ledger `FEE_IS_TOTAL=False` and the parity bin-5 guard — fixed in Phase-3-owned files by the
reviewer; the harness consumes them unchanged).

## Coupling to concurrent Phase-3 work (confessed, loose)

The task warned a concurrent reviewer may adjust `orders/envelope` endpoint paths and reconciler
internals. `run_window`'s ARMED path depends only on PUBLIC interfaces: `Executor.execute(intent,
t_minus_s, *, stop_authorized)`, `ExecutorConfig`, `StopController.trip/raise_alarm/config`,
`arming_check`, `check_s1`, `check_slippage_alarms`, the ledger transitions (`new_ledger`,
`record_intent`, `record_response`, `fills_record`), and the reconciler's `propose_rebalance` /
`detect_imbalance` / `parse_positions_response` / `PositionsReconciler`. It does NOT depend on any
envelope PATH constant or reconciler INTERNAL — the executor owns the path, and the reconciler decision
is consumed as returned proposal objects. A rename of those internals would not touch this file; a
change to the listed public signatures would (all covered by the executor/reconciler/stops tests, which
stayed green).

## What only the live 24h dry cycle (and later, armed) can verify

- Every hourly window actually WAKES from Task Scheduler at UTC:40, runs, journals, and exits 0 with
  `orders_attempted == 0` — the Phase-4 exit criterion, verifiable only by leaving `mode.txt=dry` and
  the task registered for 24h and reading the ledger (runbook §8).
- The live WS payload field names/shapes and market status strings (Phase-1 confessions 3/4).
- Whether `/markets` returns SETTLED trailing 15M markets by close-ts (confession 2) — the single
  biggest determinant of whether the strangle ever runs live vs everything degrading to sub-$1.
- The `/health` payload really carrying the caps block + `orders_enabled` at arming, and the WS
  handshake surviving a slow dial with a fresh mint.
- The positions payload shape/sign (reconcile-first + S3), `average_fee_paid` semantics (S1), and the
  live create-order path vs the proxy's capped path — all inherited live-verification items, none of
  which the test suite can settle without real traffic.

## Blockers

None. All 329 tests green. Open items are Brad-ruling flags (F4/F8 stop semantics, F2 dual-generation
pairing rule, R5 budget fail-open) and live-verification items above — not build blockers. The armed
order-routing path is built and composes the Phase-3 primitives, but is exercised only live (the S5
gate refuses to arm on the current DRAFT falsifier by design, and no test opens a socket).

---

## Post-review fixes (2026-08-21 — findings 4 & 5 from `phase4_harness_review.md`)

Two fixes landed after the adversarial review, both in Phase-4-owned files, with regression tests.
Suite now **344 passed** (341 review baseline + 3 new).

### Fix 1 — finding 4 (MEDIUM): strangle `realized_delta` no longer books the optimistic sub-$1 floor

`_build_ledger_entry` booked `realized_delta = fills_record(ls)["realized_payoff"]`
(`= state.realized_min() = matched*$1 − net cash out`) for ANY filled pair. That $1/pair is a
GUARANTEED settlement floor for a sub-$1 flip (conservative, correct) but the WINNING-outcome BEST
case for a Q1-strangle (which pays $1 only if price lands OUTSIDE the corridor, $0 inside). Since
`_ledger_day_totals` sums `realized_delta` for the S4/A4 day-lock, a strangle that will settle at a
loss was booked at a small POSITIVE number → an unsafe UNDER-count of losses feeding the daily cap,
and a violation of the honest-fills law.

**Fix (harness booking site, not `ledger.py`):** in `_build_ledger_entry` (run_window.py), the
`realized_payoff` is booked as the floor ONLY when `ls.source == SUB_DOLLAR_FLIP`; any other source
(a strangle, or any unknown source — fail-conservative) books `realized_delta = 0` and sets a new
`realized_unsettled: True` field on the ledger row. Rationale: at window close a strangle is
UNSETTLED — cost is committed, payout unknown until settlement is confirmed. The true realized is to
be filled in later by the next-day reconcile / paired report; S4 never sees an assumed strangle win.
`ledger.realized_min` / `fills_record` (Phase-3-owned) are left untouched — the defect was the
harness applying the floor to the wrong source, not the floor arithmetic itself.

Regression (`tests/test_run_window.py`):
- `test_strangle_realized_books_zero_unsettled_and_excluded_from_s4` — a filled `Q1-strangle` pair
  whose `realized_min() > 0` books `realized_delta == "0"`, `realized_unsettled is True`, `filled is
  True`, and the S4 day total (`s4_running_loss`) over the appended row is `0` (the optimistic win is
  excluded).
- `test_sub1_pair_still_books_its_floor_arithmetic_unchanged` — a filled `sub$1-flip` pair still
  books `realized_delta == ls.realized_min()` with `realized_unsettled is False`.

### Fix 2 — finding 5 (LOW): `prepare()` startup crash now leaves a flagged trace

`run()` was `plan = self.prepare(); return self.execute(plan)`; `_finalize` (flush + ledger append)
runs only inside `execute()`'s `finally`. A crash in a startup step (e.g. `sigma.assign`, which is
not otherwise wrapped) propagated out of `run()` with NO journal and NO ledger row, so the
24h-dry-cycle quick-scan (`exit_code or orders_attempted`) saw an ABSENT row rather than a flagged
failure.

**Fix:** `run()` wraps `prepare()`; on any exception it calls the new `_finalize_startup_failure`,
which journals the traceback (`startup_exception`), flushes whatever is buffered to the window
journal, and appends a MINIMAL ledger row (`status: "startup-failed"`, `exit_code: 1`,
`stand_down: True`, `error:` last traceback line, `orders_attempted: 0`), then returns nonzero.
Mirrors the execute-path crash behavior (`exit_code:1` row). The method is guarded by `_finalized`
and never raises.

Regression (`tests/test_run_window.py`):
- `test_prepare_crash_writes_startupfailed_row_and_flushes_journal` — an injected `sigma.assign`
  crash → `run()` returns 1, a `status="startup-failed"` / `exit_code=1` ledger row is appended, and
  the flushed journal contains both `window_start` (survived from prepare) and the traceback.

### Files touched
- `pilot/service/run_window.py` — `_build_ledger_entry` (source-gated realized booking +
  `realized_unsettled` field); `run()` (prepare wrap) + new `_finalize_startup_failure`.
- `pilot/tests/test_run_window.py` — 3 new regression tests + `_filled_pair_ledger` helper.

No `.env`/`*.pem` read, no network, no sealed-day read. Money stays Decimal.
