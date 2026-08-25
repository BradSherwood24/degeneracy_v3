# Phase 4 service harness + ops — adversarial review (SEPARATE opus48 REVIEWER)

Reviewed 2026-08-21. Scope: `pilot/service/{run_window,sigma_feed,pilot_ledger}.py`,
`pilot/ops/{register_task,unregister_task}.ps1` + `mode.txt` + `runbook.md`,
`pilot/tests/test_{run_window,sigma_feed,pilot_ledger}.py`, and the composed interfaces
(executor/reconciler/stops/ledger — Phase 3 post-review; signal/policy/parity/parity_report — Phase 2;
wake/ws_client/journal/book/record_window/shakedown — Phase 1). Against `pilot/PLAN.md`,
`ceremony/commission.md`, `ceremony/falsifier.md` (DRAFT), and the Phase-4 build report (8 confessions
+ the handoff-wiring section) and `phase3_execution_review.md` (F3–F10).

Constraints honored: no `.env`/`*.pem` read; nothing under `sim/out/sealed_eval/` touched; no live
network dialed by any code I wrote or ran; no sealed-day tape read. All probes use fakes/injected
readers. Baseline suite reproduced at **338 passed** before changes.

## VERDICT: APPROVE FOR SHAKEDOWN

The startup-order law is real and correctly ordered; mode parsing, reconcile-first (incl. short and
unrelated-series positions), S5/degrade, sigma fail-closed, and the journal→parity→ledger seam all
hold. I found **three defects and fixed them in-tree with regression tests** (S3 poll TOCTOU false-halt;
`load_entries` breaking on a crash-truncated trailing line; the scheduler log-dir that makes the FIRST
scheduled window fail to launch). Suite now **341 passed**.

Shakedown and the 24h dry cycle place **no orders** and are sound as delivered (with the three fixes).
Before Brad ARMS, resolve **F4-strangle-realized** (finding 4, an optimistic number feeding the S4
daily cap), wrap `prepare()` (finding 5), and the standing **PENDING-BRAD F4/F8** stop semantics; then
walk the LIVE-VERIFICATION CHECKLIST on the first passive runs. This is not APPROVE-FOR-ARMING — the
S5 gate correctly refuses to arm on the DRAFT falsifier, and one armed-only economic flag remains open.

---

## FINDINGS (severity · disposition)

### FIXED (Phase-4-owned files; minimal + regression test)

**1 — MEDIUM — FIXED — S3 poll TOCTOU: an entry that lands+records DURING the poll trips a spurious
S3 (false halt).** `_s3_poll_once` captures `ls = self.ledger_state` before `reconciler.tick(ls)`, then
guards only `after.inflight_cids()`. The F5 gate closes the "still-in-flight during the poll" sub-case
but NOT the "started AND completed during the poll" sub-case: the mismatch is computed against the
stale `ls` snapshot, and by the time `tick` returns the response is already recorded (no inflight), so
the after-check does not defer → S3 trips against a diff that raced the order path. Reproduced live
(probe: a reconciler whose `tick` advances `svc.ledger_state` to a consistent, no-inflight state and
returns `stop="S3"` from the stale snapshot → `stopped=True has_S3=True`). S3 halts the whole pilot and
requires a manual reconcile per the runbook, so a false S3 is costly (safe direction — never a wrong
fire — hence MEDIUM, not HIGH). **Fix:** defer whenever the ledger changed at all during the poll —
`if after is not ls: return` (identity check subsumes the inflight-after check; a real phantom-fill S3
still trips because nothing mutates during a synchronous post-entry poll). Regression:
`test_F5_s3_poll_defers_when_ledger_advances_during_poll`.

**2 — MEDIUM — FIXED — `pilot_ledger.load_entries` breaks on a crash-truncated trailing line.** A
crash mid-append (single writer, `open(...,"a")` + two writes) can leave a truncated final JSON line.
`load_entries` did `json.loads(line)` per line → the partial line raises `JSONDecodeError`, breaking
(a) the operator query CLI (`day`/`loss`/`gate`/`summary`) and (b) — worse — `_ledger_day_totals`,
which catches the exception and returns `(0,0)`, so the **S4 daily-loss and A4 guard-trip day-locks
silently FAIL OPEN** (a corrupt ledger → no day-lock → arms even after the day already hit the cap),
the opposite of house-law fail-closed. Reproduced (probe 4: truncated trailing line → `JSONDecodeError`).
**Fix:** tolerate ONLY a truncated trailing line (log + skip); a corrupt NON-final line is real damage
and still raises loudly. Regressions: `test_load_entries_tolerates_truncated_trailing_line`,
`test_load_entries_raises_on_midfile_corruption`.

**3 — MEDIUM — FIXED — the scheduler redirect targets a `logs\` dir that does not exist → the FIRST
(every) scheduled window fails to launch.** The registered action is
`cmd.exe /c "python -m service.run_window >> "…\logs\scheduler.out" 2>&1"`. `cmd.exe` does NOT create
the redirect's parent directory, and `logs\` is absent from the repo (confirmed: no `logs/`,
`journals/`, `ledger/`). `journals\`/`ledger\` are auto-created by Python (`Journal.flush` /
`append_entry` `os.makedirs`), but the `logs\` redirect happens in `cmd.exe` BEFORE Python starts —
so on a fresh machine every scheduled window dies at the redirect with no journal, no ledger entry, no
trace, silently, at 6am. `register_task.ps1` computed `$LogDir` but never created it. **Fix:**
`register_task.ps1` now creates `$LogDir` at registration (after the `-DryRun` return, so dry-run stays
side-effect-free — verified). Covered by the existing `test_register_script_dryrun_is_well_formed`
(still green) + the manual dry-run.

### FLAGGED (not fixed — armed-only economics / needs a ruling)

**4 — MEDIUM — FLAGGED — ledger `realized_delta` books the sub-$1 $1/pair FLOOR for a Q1-strangle,
an OPTIMISTIC number that feeds the S4 daily-loss cap.** `_build_ledger_entry` sets
`realized_delta = fills_record(ls)["realized_payoff"] = state.realized_min()` for ANY filled pair, and
`realized_min = matched_pairs * $1.00 − pair_net_cash_out`. For a sub-$1 flip that $1 is a *guaranteed
floor* (correct, conservative). For a Q1-strangle it is the *winning-outcome BEST case* — a strangle
pays $1 only if price lands outside the corridor, $0 if inside — so a filled strangle that will settle
at a loss can be booked at a small POSITIVE `realized_delta`. `_ledger_day_totals` sums `realized_delta`
for the S4/A4 day-lock, so a strangle loss is under-counted toward the $5 daily cap (unsafe direction),
and it violates the honest-fills law ("optimistic fills are V1/V2's mirage"). Bounded by pilot size
(per-window strangle exposure « $5) and by the fact that true P&L is measured by the paired report at
settlement, so it is MEDIUM not HIGH. **Recommend before arming:** at window close a strangle is
UNSETTLED — book `realized_delta = 0` (or a strangle-appropriate value) for non-sub-$1 sources rather
than the $1 floor; the S4 enforcement then rests on the next-day reconcile + paired report. Touches
`ledger.realized_min` semantics (Phase-3-owned) and wants a one-line ceremony note, so I flag rather
than fix.

**5 — LOW — FLAGGED — `prepare()` is not exception-wrapped; a startup-step crash exits without a
journal flush or a ledger entry.** `run()` is `plan = self.prepare(); return self.execute(plan)`;
`_finalize` runs only in `execute()`'s `finally`. An exception in steps (a)–(e) (e.g. `sigma.assign`
on a malformed market dict, `ev_curve` load, `close_epoch`) propagates out of `run()`→`main()` and the
process dies nonzero with **no journal and no ledger row**. Fail-closed on the critical axis (no order
is placed — good), but the 24h-dry-cycle quick-scan (`exit_code or orders_attempted`) only scans
EXISTING rows, so a `prepare()`-crashed window shows as an *absent* row, not a flagged one — only the
runbook's manual "an entry exists per window" check catches it. **Recommend:** wrap `prepare()` so a
startup crash still journals the traceback, flushes, and writes a `exit_code:1` ledger row (matching the
execute-path crash behavior already tested).

### INFORMATIONAL

**6 — LOW/info — `HarnessExecutor._arm_locked` is read/written without the executor lock.** The
transition is monotonic (False→True on any disarm) and `run_window` never calls `set_armed(True)` after
construction (the armed executor is built with `ExecutorConfig(armed=True)`), so the F7 latch is not
racy in practice; the F8 budget swap correctly holds `self._lock`. Note for future multi-caller use.

**7 — LOW/info — S4 is enforced at the per-window boundary, never intra-window.** `on_realized` is not
wired into the live loop (realized P&L lands at settlement, after the window's process exits), so S4
trips only via the arming-time day-lock + the seeded `StopState`. Correct for the process-per-window
cadence and pilot dual size (per-window exposure « $5), but a within-window drawdown cannot be stopped
mid-window. Document it; no change needed at this size.

**8 — LOW/info — confession #8 mislabels `s4_running_total`.** The field is
`s4_running_loss(all entries) + realized_delta` = ALL-TIME cumulative, not "day-cumulative" as the
confession states. Day-cumulative is `day_realized_seed + realized_delta` (both present as separate
fields) and the CLI `loss --day` computes it correctly. The field is informational; the DAILY S4
enforcement (`_ledger_day_totals`, day-scoped) is correct.

---

## Startup-order law — probe results (dimension 1)

- **No path reaches WS connect / decide / executor before the gates.** `prepare()` runs (a) policy+sha
  → (b) arming/S5 → (c) reconcile-first → (d) wake → (e) sigma, and builds the armed stack only at the
  END of `prepare()`. `execute()` builds the recorder and dials ONLY when `not plan.stand_down`. Order
  never varies.
- **Arming cannot fail-open.** `armed=True` requires `arm_decision.armed AND not extra` (extra = caps
  disagreement / S4 day-lock / A4 day-lock). `arming_check` handles non-dict/None health without
  raising; a hypothetical raise propagates out of `prepare()` → process dies, no order (fail-closed).
- **Reconcile-first refuses on ANY nonzero position in our series, incl. a SHORT.** Probe: `position:-2`
  on `KXBTC15M-ANCHOR` → `stand_down=True, inherited={…:-2}`. Probe: `position:-5` on `KXETH-XYZ`
  (unrelated) → `stand_down=False, armed=True` (correctly ignored). A positions-READ failure is fatal
  for armed (stand down); dry/shakedown log + continue.
- **Mode parsing fail-closed.** Garbage/empty/missing `mode.txt` → `shakedown` (tested).

## Interlock chain — probe results (dimension 2)

- **Stop latch mid-batch:** the executor holds one RLock across the whole POST+parse+journal, and
  `set_armed(False)` acquires the same lock, so a trip cannot land between the batch POST and its
  response handling; a mid-sequence rebalance after a trip is refused `not_armed` (executor is the
  single authority). Batch entries are one POST (per-entry success), so there is no inter-leg trip
  window.
- **S3 poll TOCTOU:** the exact race the gate claims to close was OPEN — see finding 1 (fixed).
- **S4 day-seed:** no double-count on crash-restart — each window appends its `realized_delta` once;
  the seed reads the sum of PRIOR entries (this window's row is not yet appended); the StopState
  accumulates on top. Correct per-UTC-day.
- **`HarnessExecutor.set_armed(True)` latch:** works (test_F7); thread-safety note in finding 6.

## Scheduler + scripts — hostile read (dimension 3)

- `-DryRun` on both scripts is well-formed and registers NOTHING (verified live; PS 5.1 compatible — no
  `&&`, no ternary, ASCII only). `unregister` removes exactly the `TaskName` `register` creates.
- **DST minute arithmetic verified** for whole-hour AND fractional offsets:
  `((40+off)%60+60)%60` → offset +5:30 (330) = local :10 (UTC 22:40 = local 04:10 ✓); all whole-hour
  offsets → :40 ✓; the formula is sign-safe in PowerShell (probed offsets 330/−300/−240/0/60/−210/570/630).
  The claim "whole-hour DST shifts preserve the UTC:40 alignment with no re-registration" holds — a
  whole-hour shift changes only the hour, and an hourly repeat fires at the fixed minute every local
  hour. Only fractional-offset changes need a re-run (documented + belt-and-suspenders re-run).
- **Mode is read at RUN time** (action has no `--mode`; `run_window` reads `mode.txt`) — confirmed.
- **Output redirection dir** was the finding-3 gap (fixed).

## sigma_feed honesty (dimension 4)

- Fail-closed contract holds: missing/short/malformed trailing tape → `NoQuintile` → strangle stands
  down; a resolvable sub-$1 pair keeps sub-$1 alive; no pair at all → whole-window stand down (tests +
  read confirm every branch).
- **Sub-only routing cannot leak a strangle fire.** Double-guarded: `fallback_pair` always sets
  `strangle_disabled=True`, AND `_sub_only_quintile` returns the lowest quintile whose sources contain
  sub-$1 but NOT the strangle (q1 in the frozen roster, never q0), so `signal.decide`'s strangle branch
  (`quintile == 0 AND not strangle_disabled`) can never be reached.
- **No-status-filter confession** is the single biggest live unknown — made precise in the checklist
  (item 4): exact call + expected shape to check on the first passive run.

## pilot_ledger vs the falsifier text (dimension 5)

The computed gates match the falsifier's letter:
- P1 fill rate = filled-windows / **fired-windows** (correct denominators); `filled` requires BOTH legs
  net>0. Slippage is **per side** (`slippage_abs_per_side` = one entry per filled leg; mean is over
  per-side entries). "Unresolved imbalance" = end-state not 1:1/0:0 → a **resolved-by-selldown** (0:0)
  correctly does NOT count. Boundary tests pin 6/10 pass vs 5/10 fail, 10 vs 9 fired, 0.010 pass vs
  0.011 fail.
- P2 computes over the 2-pair rung, requires ≥10 further fired incl. ≥5 sub-$1, re-checks the P1
  conditions on the 2-pair rung AND `p1_gate` on the 1-pair rung, and requires a book-walk field present.
- S4 sign convention: negative `realized_delta` = loss; `daily_loss_cap $5` enforced as
  `loss_today <= -5.00` (finding 4 is the strangle input to this, not the arithmetic).
- Crash-mid-append: was intolerant — finding 2 (fixed).

## Runbook accuracy (dimension 6)

Commands verified against the code: `pilot_ledger day/loss/gate/summary` (parent `--ledger`, `loss
--day`), `parity_report --manifest --out-json --out-text`, the kill/`Get-CimInstance` incantation, the
`register/unregister` invocations. The A1–A4/S1–S5 plain-English mapping matches the falsifier. The
arming checklist is complete (falsifier FROZEN + policy sha + proxy restart w/ ALLOW_ORDERS + /health
caps + flat + `mode.txt=armed`) and §9 lists the PENDING-BRAD F4/F8/F2/R5 items. The 24h-dry-cycle pass
criteria are verifiable from the ledger as written (note finding 5: a `prepare()`-crashed window shows
as an absent row, which the quick-scan does not flag — the manual "entry exists" check does). Nothing in
the runbook contradicts the code. **One doc add recommended:** the runbook should tell the operator the
`logs\` dir is auto-created by `register_task.ps1` (post-fix) — no manual step, but worth a line so a
hand-run `python -m service.run_window` (which creates `logs\` itself via `_setup_logging`) and the
scheduled run agree.

## Cross-phase seams (dimension 7)

- **Journal record kinds match.** `run_window`'s recorder tap journals WS frames under kind
  `"kalshi_ws"` with envelope `{"type","msg"}`; `parity.live_book_events` reads exactly that. Action
  records use `would_fire`/`fire`/`stand_down`.
- **End-to-end pipeline runs (synthetic).** I drove a real `WindowService` shakedown recorder with two
  synthetic snapshot frames (high NO-ask 0.40 / low YES-ask 0.35, both fresh, t−300s), flushed, then
  fed the flushed journal to `parity.live_entry_for_window`: it reconstructed the WouldFire exactly
  (`fired=True source=sub$1-flip C=0.7828 t_minus=300.0`). Journal kinds present:
  `window_start, policy_loaded, reconcile_first, window_meta, quintile, kalshi_ws×2, would_fire`. The
  ledger row carried `would_fires=1, high_ticker, quintile` — the window meta `parity_report`'s manifest
  needs.
- **Promotion-gate fields populated in armed mode.** `_build_ledger_entry` sets
  `fires/filled/imbalance_unresolved/s1_violation/sub1_entry/slippage_abs_per_side/second_pair_book_walk/pairs`
  from the ledger state — all fields P1/P2 read.

## Confession rulings (all 8)

1. **NoQuintile fallback + sub-only bucket** — ACCEPTABLE. Double-guarded against a strangle fire
   (finding-free); named live/sim delta correctly surfaced to the paired report.
2. **Trailing-tape no status filter** — ACCEPTABLE; made precise (checklist item 4).
3. **Current-close 15M merged into anchor tape** — ACCEPTABLE (dedups by `id()`; keeps sub-$1 tradeable
   when only sigma is missing).
4. **Subscription defaults to the decision legs only (`--max-hourly-strikes 0`)** — ACCEPTABLE;
   `_subscription_tickers` guarantees the decision pair is always included even under a cap.
5. **Second-pair book-walk is an avg-vs-ask proxy** — ACCEPTABLE-with-flag; P2 checks field PRESENCE,
   the precise per-contract walk is a live-verification item.
6. **ThreadSafeJournal wrapper + `capture_tops=False` in armed** — ACCEPTABLE (two-thread append; the
   journal is armed's source of truth; `book_tops` is a replay-parity nicety).
7. **Reconcile-first stands down in ALL modes on an inherited position** — ACCEPTABLE (uniform, safe).
8. **`s4_running_total` computed pre-append** — mechanism ACCEPTABLE; label inaccurate (finding 8).

## Handoff-wiring spot-checks (6 of 8 verified against the diff, not the report)

- **F7 interlock** — VERIFIED: `HarnessExecutor.set_armed` latches `_arm_locked` on any disarm and
  refuses later `set_armed(True)` (code + `test_F7`).
- **F8 flatten budget exemption** — VERIFIED: `execute` swaps `window_token_budget` under `self._lock`
  for a `stop_authorized` flatten only; the probe/test shows the flatten POSTs to `SINGLE_CREATE_PATH`
  (events path) while a strategy order under the same exhausted budget is refused.
- **F6 reduce_only on rebalance sells** — VERIFIED: `_maybe_rebalance` sets `reduce_only=True` for
  `SellDown`, `None` for `RetryBuy` (run_window.py).
- **F9 caps value-agreement** — VERIFIED: `_caps_agree` requires proxy `max_contracts_per_order` in
  `[pairs, PILOT_MAX_CONTRACTS_CEILING=2]` and ≥ executor cap, and prefixes covering both series (tests).
- **F5 S3 gate** — VERIFIED WIRED but was INCOMPLETE → finding 1 (now fixed).
- **F3 S4/A4 day-lock + seed** — VERIFIED: `_ledger_day_totals` sums the UTC day; arming applies the
  day-locks; StopState is seeded (tests). (Finding 2 hardens the read this depends on; finding 4 is the
  strangle input.)
- F14 full-suite re-run and the "not-harness-owned F4/F1/F2" items — consistent with the code.
- **Dual-generation pairing (timing-review F1 / Phase-1 F2) — RESOLVED 2026-08-21 (opus48 builder).**
  Phase-B pairing now pools ALL live co-settling hourly generations (`wake.hourly_ladders`/
  `hourly_pool_markets`) and mirrors census `nearest_hourly_strike` + cross-generation equidistant-tie
  exclusion; the ladder-map check and the WS subscription follow the CHOSEN market's generation.
  Single-generation behavior unchanged. Suite 365 -> 374 (9 new regressions). See
  `build/timing_review.md` F1 for the full fix. This retires the standing dual-generation confound the
  earlier reviews flagged before the strangle trades live.

---

## LIVE-VERIFICATION CHECKLIST (first passive/dry runs — ordered, checkable)

Merged from Phase-1's checklist + sigma_feed items + fee semantics + positions/order payload shapes.
None of these can be settled without real traffic; run them on the first dry windows before arming.

1. **WS handshake via proxy** — a dial fetches fresh `(ws_url, headers)` from the proxy `/ws-auth`;
   a slow dial re-mints on re-dial. Confirm a real socket opens and subscribes both legs.
2. **WS payload shapes** — `orderbook_snapshot` carries `yes_dollars_fp`/`no_dollars_fp` as
   `[[price,size],…]`; every dispatched frame carries `market_ticker`; server ts is `ts_ms` (epoch ms)
   or `ts` (ISO-"Z"/epoch). Confirm `BookMirror` builds a sane top and `_parse_server_ts` returns a
   value (else freshness fails closed and nothing fires).
3. **Live market status strings** — RESOLVED (live observation 2026-08-21, verified against the live
   API via the proxy). OBSERVED FACTS: Kalshi 15M markets are listed DAYS ahead with status
   `"initialized"`, `open_time` = the :45 window start, and flip `initialized`→`active` EXACTLY at
   `open_time` (observed 15:45:07 for a 15:45:00 open). At the :40 wake the co-settling 15M leg ALWAYS
   exists but is still `"initialized"` (the hourly KXBTCD leg is found `active` fine). The old
   `ACTIVE_STATUSES = {"active","open"}` allow-list therefore stood EVERY window down forever
   ("no 15-minute leg co-settling…"), which was the first live shakedown bug. FIX shipped: leg
   SELECTION is now STATUS-AGNOSTIC — `wake.discover_legs` selects by `close_time` regardless of
   status via a DEAD deny-list (`DEAD_STATUSES = {"settled","finalized","closed","determined"}`;
   a leg is refused only when its markets are ALL dead), `_fetch_series_markets` sends NO `status`
   param (a `status=open` filter hid the initialized leg), and `run_window` holds the WS dial until
   `(15M open_time − 5s)` so it never subscribes a not-yet-open market. Regression tests:
   `test_discover_selects_initialized_15m_leg_at_wake`, `test_each_dead_status_refused`,
   `test_sweep_sends_no_status_param_and_selects_initialized_leg`,
   `test_run_ws_window_holds_dial_until_connect_gate` (+5 more). Ticker naming observed:
   `KXBTC15M-26AUG211200-00` has `close_time` 16:00Z (names use ET local time) — selection keys on
   the `close_time` epoch, not the name.

   **3a. Strike-at-open lifecycle — RESOLVED (live observation 2026-08-21, shakedown run 2, verified
   against the live API via the proxy).** OBSERVED FACT: an `"initialized"` 15M market has NO
   `floor_strike`/`cap_strike`. The strike materializes at the instant the market flips `"active"` at
   `open_time` — observed 12:45:13 local: `status:"active"` and `floor_strike:77315.17` appearing
   SIMULTANEOUSLY. The anchor A(T) is BTC SPOT at window open — a NON-round number (e.g. 77315.17),
   never a round strike. CONSEQUENCE: at the :40 wake the anchor does not exist yet, so EVERYTHING
   anchor-dependent is impossible at :40 — the hourly-leg pairing (census `hole_G`/nearest-threshold),
   G, g/σ̂, and the quintile. Run 2 (leg-discovery fix from item 3 working, `ladder_ok=true`)
   nonetheless stood down at 12:40:03 with `EXCL_NO_ANCHOR` because `prepare()` computed the quintile
   at :40 → the anchor was absent → permanent stand-down every window (the SECOND live shakedown bug).
   FIX shipped: the wake is now TWO-PHASE. **PHASE A** at :40 (unchanged parts: policy sha, S5/arming,
   reconcile-first, balance, 15M leg identity, hourly ladder + ladder-map check) additionally
   PREFETCHES the σ̂ trailing anchors (T-900..T-7200, all present with strikes at :40) and journals a
   `phase_a` record; NO quintile/pairing is computed. **PHASE B** at leg open (`run_window.execute`
   → `_resolve_anchor`): after the connect gate, POLL the co-settling 15M market via REST every ~2s
   from `open_time` until `floor_strike` is present, timing out at `open_time+45s`; on the strike,
   compute A(T) → pair the hourly leg (census law: nearest threshold, both sides) → G → g/σ̂ → quintile
   → `strangle_disabled`, then subscribe/dial the WS for BOTH legs together and run the window; on
   timeout, `EXCL_NO_ANCHOR` is now a LEGITIMATE stand-down (journal `phase_b_timeout` + a clean
   ledger row, exit 0). WS-dial ordering: BOTH legs are dialed together at phase B (the anchor is
   needed to know WHICH hourly strike is the paired leg, and the connect gate already held the dial to
   ~open anyway); the freshness gates in `signal.decide` remain the last defense. Fail-closed
   distinction preserved: no anchor → no pairing → no C for EITHER source (both leg prices needed) →
   whole-window stand down (sub-$1 also needs phase B); sub-only routing applies ONLY when the anchor
   /pairing succeeded but σ̂/quintile failed. Regression tests: `test_phase_a_defers_quintile_no_
   quintile_record`, `test_phase_b_polls_until_strike_appears_at_open`,
   `test_phase_b_poll_timeout_stands_down_no_anchor`, `test_phase_b_reuses_prefetched_trailing_and_
   polls_current`, `test_phase_b_sub_only_routing_reachable_when_sigma_fails`,
   `test_phase_b_crash_writes_exit1_row_via_execute_path`, and the census-pairing checks
   `test_live_pairing_nearest_below_on_100_step_ladder` / `…_nearest_above_case` /
   `…_equidistant_is_tie_excluded` (anchor 77315.17 → 77299.99 on a $100 ladder; nearest-ABOVE case;
   tie → excluded). Surgical scope: `wake.py` (public `fetch_co_settling_15m`), `run_window.py`
   (two-phase `prepare`/`execute` + `_resolve_anchor`/`_apply_outcome` + poll constants), and one
   backward-compatible kwarg `trailing_markets` on `sigma_feed.assign` (data-prep only — executor/
   reconciler/stops/signal untouched); `quintile.py` needed NO change (its `nearest_hourly_strike`
   already reproduces census both-sides selection). LIVE-VERIFY on run 3: confirm the strike appears
   within the 45s poll budget after `open_time` (observed ~13s), and that the ~13s poll consumed at
   the head of the 15-min window still leaves ample book time before the t-5min decision.
4. **`/markets` returns SETTLED trailing 15M anchors** (THE determinant of whether the strangle ever
   runs). Call: `GET /markets?series_ticker=KXBTC15M&min_close_ts=<T−7200>&max_close_ts=<T>&limit=1000`
   (NO `status` param). Expect ≥ 8 trailing anchors, each with a numeric `floor_strike` and its
   `close_time` (status likely `finalized`/`settled`). If settled markets are omitted by close-ts →
   `sigma_hat` cannot be built → EVERY window degrades to sub-$1-only via the fallback (the safe
   direction, but the strangle never fires live).
5. **`/health` caps at arming** — `{orders_enabled:true, caps:{max_contracts_per_order, ticker_prefixes:
   [...], daily_order_budget}}`. Confirm present AND value-agreeing: `max_contracts_per_order ∈ [pairs,2]`,
   prefixes cover `KXBTC15M` + `KXBTCD` (else S5/F9 refuses — correct).
6. **`/portfolio/positions` shape + sign** — `GET /portfolio/positions` → `market_positions:[{ticker,
   position},…]`. Confirm the signed convention (long-YES `+`, long-NO `−`) matches
   `parse_positions_response`/`expected_positions`, or reconcile-first false-refuses / S3 storms.
7. **`average_fee_paid` semantics (total vs per-contract)** — on the FIRST live fill, confirm. The
   ledger currently multiplies by `fill_count` (`FEE_IS_TOTAL=False`, fail-closed over-count); if TOTAL
   is confirmed, flip it back. Feeds S1 (arithmetic floor) and the paired-report actual-fee bins.
8. **Create-order path** — the live POST is `/trade-api/v2/portfolio/events/orders[/batched]`, routes
   through the proxy, and is capped (proxy events-path tests prove this offline; confirm the first live
   order 2xx's and consumes the proxy budget per entry).
9. **Batch fill-truth on a partial fill at 1–2 contracts** — `fill_count`, `remaining_count`,
   `average_fill_price`, `average_fee_paid`, `ts_ms` per entry; confirm the orphan/partial paths see them.
10. **Watchdogs fire on injected faults** — demonstrate the lag/silence force-close on a dry run
    (Phase-1 exit criterion) and confirm re-dial backoff + clean give-up at the deadline.

## Test count

`cd pilot && python -m pytest tests/` → **341 passed, 0 failed** (338 baseline + 3 new regressions:
`test_F5_s3_poll_defers_when_ledger_advances_during_poll`,
`test_load_entries_tolerates_truncated_trailing_line`,
`test_load_entries_raises_on_midfile_corruption`).

## Files

- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\run_window.py` (S3 poll identity gate)
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\pilot_ledger.py` (trailing-line tolerance)
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\ops\register_task.ps1` (create `$LogDir`)
- Tests added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests\test_run_window.py`,
  `…\tests\test_pilot_ledger.py`
- Flagged (not modified): `service/run_window.py` `_build_ledger_entry` realized_delta for strangle
  (finding 4) + `prepare()` wrap (finding 5); `service/ledger.py` `realized_min` semantics (Phase-3,
  finding 4).
