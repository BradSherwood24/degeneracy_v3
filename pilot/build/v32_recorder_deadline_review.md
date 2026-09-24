# Adversarial review: recorder deadline hard-stop hotfix

- Reviewer: Opus 4.8 (adversarial), degeneracy_v3
- Worktree: C:\Users\Brads\Python_stuff\dv3_wt_review (detached, HEAD 0309503, branch fix/recorder-deadline)
- Base: origin/main ba1694f. Diff under review: `git diff ba1694f 0309503`
- Live tree NOT touched. Worktree left clean at 0309503. No commits, no PR.

## Verdict: APPROVE WITH NITS

The fix closes the actual 2026-09-23 escape. The two behavioral tests genuinely fail on the
pre-fix logic (verified by reverting `run_recording` in-place and re-running them; both hung and
tripped the 5s guard -> TimeoutError). The hard stop is armed correctly, is a daemon, cancels on
every normal exit path, and its close+130s deadline has ~100s of measured headroom over the worst
real finalize. Scope is clean (only 5 files; no params / falsifier / ledger schema / executor /
ws_client / order-path changes; ASCII-only added lines). Findings below are observability nits and
one out-of-scope latent twin -- none block arming.

## Suite counts

`python -m pytest -q` from C:\Users\Brads\Python_stuff\dv3_wt_review\pilot:
`1265 passed, 5 skipped in ~13s` (total 1270).

Reconciles with the build report's "1269 passed, 1 skipped" (also 1270): the 4-test delta is
`test_box_golden.py` x2 and `test_quintile.py` x2, which SKIP here because the gitignored
historical-data corpus is absent in the worktree (present in the builder's tree). The one baseline
skip is the POSIX-only SIGTERM test in `test_supervisor.py`. No failures, no missing tests.

## Old-code test-failure check (honesty of the new tests)

Method: in THIS worktree, reverted only `run_recording`'s supervise body + the `wait_for`/`except
asyncio.TimeoutError` block to the pre-fix single-`force_close()`-then-return behavior (kept
`arm_hard_stop` and the new constants so imports still resolve), ran the two behavioral tests, then
`git checkout -- pilot/service/record_window.py` to restore (confirmed clean).

Result: both FAILED on the reverted code --
`test_run_recording_deadline_mid_dial_does_not_hang` and
`test_run_recording_wait_for_backstop_cancels_wedged_dial` -- each via the outer
`asyncio.wait_for(..., timeout=5.0)` hang-detector raising TimeoutError (2 failed in ~11s = 2x the
5s real-time guard). This confirms the tests exercise the real escape and are not passing on a fake
short-circuit. The fakes are honest: `_EscapeFakeWs` requires the supervisor to RETRY through the
no-op close (first close sets `ws` non-None + releases the dial; only a second, live close ends
`connect`), and `_WedgedFakeWs` never releases `connect` except via the `wait_for` cancellation.

The four hard-stop unit tests trivially fail on ba1694f (symbol/`arm_hard_stop` did not exist).

## Findings (ranked)

### F1 (MEDIUM, observability): a mid-window websockets open-handshake timeout is mislabeled `deadline_forced_close`
`record_window.py:335-339`. The new `except asyncio.TimeoutError` is reached by ANY `TimeoutError`
out of `connect()`, not only the `wait_for` deadline cancellation. websockets 16.0 (installed)
dials with `open_timeout=10` and raises bare `TimeoutError("timed out during opening handshake")`
on a stalled handshake; in Python 3.12 `asyncio.TimeoutError is TimeoutError` (verified True). So an
early-window handshake timeout (clock well below deadline; internal 10s fires before the large
`wait_for` budget) is journaled as `deadline_forced_close` and the `[RECORD] connection error`
warning is skipped -- yet it is really a connectivity fault. The loop still retries (clock <
deadline), so no hang; impact is triage only, and no code consumes the alarm label
(grep: `deadline_forced_close`/`ws_error` are journal-only, no falsifier/ledger/branch). Still, it
can hide a degrading link and inflate `deadline_forced_close` counts that a human would read as "the
deadline fix is firing often." Suggested fix: in the handler branch on `recorder.clock() >=
deadline` -- deadline close if past deadline, else log+journal `ws_error` as before.
Failure scenario: proxy/venue handshake flakiness at :45 produces a journal full of
`deadline_forced_close` with no warning logs; a reviewer concludes the window is repeatedly hitting
its deadline when it is actually losing dials mid-window.

### F2 (LOW-MEDIUM, out of scope, latent twin): `service/run_window.py:2246-2269` still has the pre-fix supervise pattern
The legacy V3.1 single-window path (`run_window.py`) carries the identical one-`force_close()`-then-
return supervisor and an unbounded `await ws.connect()` -- the exact bug that was fixed here. It is
NOT on the live hot path (`supervisor._ROSTER_MODULE` spawns `service.run_v32`/`service.run_v33`
only; `run_window` is imported elsewhere for helpers, not spawned per-window), so this does not
block. Follow-up: confirm no scheduled task still runs `python -m service.run_window`; if any does,
port the same fix. Failure scenario: if a stray task or a manual run uses run_window, it can still
hang mid-dial at the deadline exactly as the 2026-09-23 incident did.

### F3 (LOW, harmless race): a deadline that lands between `self.ws=` and `on_open()` completing journals `ws_error` instead of `deadline_forced_close`
`ws_client.py:129-138` sets `self.ws` inside the `async with` before `on_open()` sends subscribes.
If the supervisor's retried `force_close()` lands in that sub-tick window, `connect()`'s subsequent
`_subscribe` send raises `ConnectionClosed`, caught by the `except Exception` -> `ws_error` alarm at
teardown rather than `deadline_forced_close`. Cosmetic (an extra `ws_error` at end-of-window); the
window still ends cleanly. No action needed beyond awareness.

### F4 (LOW, deliberate but worth a note): `hard_stop.cancel()` is the last statement after `_finalize` in the `finally`
`run_v32.py:2018-2029` / `run_v33.py:~873-879`. If `_finalize` RAISES (not wedges), `cancel()` is
skipped and the armed daemon `os._exit(3)` timer lingers. In practice the raised exception
propagates out of `main()` and the process exits (non-zero) far before close+130s, so the timer
never fires -- harmless. And if `_finalize` WEDGES, the still-armed timer firing is the intended
safety, so this ordering is correct by design. Optional hardening: cancel in a nested finally so a
fast `_finalize` failure cannot leave an armed hard-exit behind.

### F5 (LOW, residual gap in the stated guarantee): the hard stop only covers the recording phase, not setup
`run_v32.py`/`run_v33.py` arm the hard stop immediately before `asyncio.run(...)`, i.e. AFTER wake +
market discovery (which make synchronous proxy calls). A hang during discovery (before arm) is not
covered and would starve the next hour identically to the incident (task is
MultipleInstances=IgnoreNew). The incident was specifically the recording dial, and discovery runs
~:40 with ~20 min of slack, so this is lower-likelihood -- but the fix's own rationale ("cannot
outlive its window and starve the next hour") is only partially delivered. Follow-up: consider
arming the hard stop earlier (it fires at an absolute close+130s regardless of arm time).

## Verification performed (receipts)

- Line-by-line walk of `run_recording` + `ws_client.connect/handler/force_close/on_open`:
  - Mid-dial-at-deadline (ws is None at first close): supervisor now latches `forcing` and RETRIES
    `force_close()` each tick until `stop`; once the dial sets `self.ws`, the next tick closes it,
    `handler()` returns, `connect()` returns. Fixed. No new dial starts at/after the deadline
    (`while recorder.clock() < deadline` is the only place a dial begins).
  - Wedged sync mint (`proxy_auth.ws_connect_params()` is a synchronous `_http_get`, WS_AUTH_TIMEOUT
    = 5.0): while it blocks the loop, neither `wait_for` nor the supervisor tick can fire; the ONLY
    cover is the daemon `threading.Timer` hard stop. This is correctly documented in the code and the
    build report. The 5s auth timeout normally bounds the block; the hard stop covers a pathological
    over-run.
  - Live-socket-at-deadline / stale-lag force-close / seq-gap reconnect: supervisor force-closes,
    `handler`'s `async for` unwinds via `ConnectionClosed`, loop re-dials only if clock < deadline.
    Unchanged semantics; the trailing `await asyncio.sleep(0)` only adds a yield.
  - `wait_for` cancelling `connect()` mid-handshake: `CancelledError` is BaseException, so
    `handler`'s `except Exception` does not swallow it; the `async with websockets.connect()`
    __aexit__ closes the socket; `self.ws` is left pointing at a closed socket but nothing reads it
    again after the loop exits. No trip observed.
  - Double `force_close` on an already-closed socket: `ws.close()` is idempotent in websockets;
    `force_close` is `ws is None`-guarded. Safe.
- `asyncio.sleep(0)` timing regression: full suite (incl. all existing run_recording tests) passes
  1265/5; no hidden regression.
- Hard stop: daemon flag set (test asserts `daemon is True`); armed before `asyncio.run`; `cancel()`
  reachable on normal return, KeyboardInterrupt (caught then finally), and other exceptions (finally
  still runs). `os._exit(3)` skips journal flush/ledger -- acceptable for a hung run (the point is
  something is wedged); exit code 3 does NOT collide with supervisor semantics: `supervisor._wait_child`
  records `exit_code: rc` and branches only on STATUS (exited/watchdog_killed/signaled), never on the
  numeric code, so a 3 is logged and the supervisor continues to the next :40.
- Finalize duration headroom: measured "window done" (logged after `_finalize`) across 20 live
  windows on 2026-09-23/09-24 lands at close+11..+32s (largest journal 2,453,917 records finished at
  close+22s). `_finalize` does journal close + gzip + pure money math + ledger append -- no
  settlement/network wait. close+130s leaves ~100s headroom; safe.
- Supervisor interaction: external watchdog kills at close+120s; in-process hard stop at close+130s.
  Supervisor wins by 10s when present (no race); boot sweep and next-window logic are independent of
  child exit code and unaffected. V3.3 runs under the supervisor, so its hard stop is the bare-run
  backstop only.
- Scope: only pilot/{service/record_window.py, service/run_v32.py, service/run_v33.py,
  tests/test_record_window.py, build/v32_recorder_deadline_build_report.md} changed. No non-ASCII in
  added lines. No params / ceremony falsifier / ledger schema / executor / ws_client.py / order-path
  edits.

## Follow-ups queued (non-blocking)

1. F1: branch the `except asyncio.TimeoutError` on `clock >= deadline` so a mid-window handshake
   timeout is journaled/logged as `ws_error`, not `deadline_forced_close`.
2. F2: port the deadline fix to `service/run_window.py` (or confirm it is dead and delete), so the
   legacy twin cannot reproduce the incident.
3. F5: consider arming the hard stop earlier to also cover the setup/discovery phase.

---

# Round 2 (commit 5841a6f, `git diff 0309503 5841a6f`)

## Verdict: APPROVE

All three round-1 nits (F1, F4, F5) are addressed correctly, and the new past-deadline guard is
sound. No new correctness issue found. Scope still clean (same 5 files; no params / falsifier /
ledger schema / executor / ws_client edits). One trivial ASCII nit and one non-blocking hardening
follow-up.

## Suite counts

`python -m pytest -q` in the review worktree: `1267 passed, 5 skipped` (total 1272). That is +2 over
round 1 (the two new tests). The 5 skips are unchanged: 4 corpus-absent (box_golden x2, quintile x2)
+ 1 POSIX-only SIGTERM. No failures.

## Checks requested

### (1) Can the past-deadline guard disarm protection on a REAL launch? No.
`delay = fire_at - clock() = (close_epoch(close_iso) + GRACE_SECONDS + HARD_STOP_GRACE_S) - now =
close + 130 - now`. On every real launch `close_iso = args.close or next_top_of_hour_iso(now)`; with
no `--close`, `next_top_of_hour_iso` returns the next :00 STRICTLY after now, so `close > now` and
`delay > 130 > 0` -> ALWAYS armed. The clock at arm time is `time.time` (real UTC epoch), matching
`close_epoch` and the recorder's clock (run_v32.py:1800 / run_v33.py:693 both `clock = time.time`) --
no clock-source mismatch. Enumerated launch modes:
- :40 wake for a close ~20 min out: delay ~1330s, armed.
- supervisor `--now`/`--once`: spawns `python -m service.run_v32` with default `--close` = next :00
  (future) -> armed.
- reboot / StartWhenAvailable catch-up: run_v32 with no `--close` still targets the next FUTURE :00,
  never an already-closed window, so `close > now` -> armed. And a run whose deadline is already past
  cannot hang anyway: `run_recording`'s `while recorder.clock() < deadline` is immediately false, so
  it never dials.
`delay <= 0` arises ONLY for an explicit past `--close` (manual replay / tests), where there is no
live window to guard -- disarming is correct. Verified: the one in-process test caller
(test_v33_run.py:96, `--close 2026-09-20T04:00:00Z`) yields delay = -415333s -> not started.

### (2) F5: is `deadline` the same as before (no off-by-one)? Yes.
run_v32.py:1814 `deadline = close_epoch(close_iso) + GRACE_SECONDS`; the removed later line was
`deadline = cts + GRACE_SECONDS` with `cts = close_epoch(close_iso)` (run_v32.py:1964) -- byte-
identical (`close_epoch` is a pure function of the same `close_iso`, which is assigned once at
run_v32.py:1804 and never reassigned). Same in run_v33 (706 vs 822). `gate = connect_gate_epoch(cts,
...)` still uses `cts`. No drift between the armed deadline and the recorder's deadline.

### (3) Any in-process main() caller left with an armed firing timer? No.
Grep of the whole repo for `run_v32.main`/`run_v33.main`/`R.main`/`RUN.main`: the ONLY in-process
run-module caller is test_v33_run.py:96 (past close -> guard disarms). `br.main` (test_box_report)
is box_report's main, no hard stop. Production entry is `if __name__ == "__main__": raise
SystemExit(main())` in both run files, so a stand-down early return (which does NOT reach the outer
try/finally cancel) returns an int, SystemExit exits the process, and the daemon timer dies with it
-- os._exit never fires because the process is already gone. The supervisor spawns a SUBPROCESS
(`subprocess.Popen([python, -m, service.run_v32, ...])`), never calls main() in its own process. So
no path leaves an armed firing timer that could os._exit an unintended caller.

### (4) F1 branch: is the early-timeout test honest? Yes -- verified it fails on 0309503.
Reverted only the F1 except-branch to the round-1 unconditional `deadline_forced_close` in this
worktree and ran the test: FAILED with `assert 'ws_error' in ['deadline_forced_close']`, then
restored via `git checkout` (worktree clean). The fake advances the clock only on dial 2, so dial 1's
bare `TimeoutError` is genuinely pre-deadline and must exercise the `else` (ws_error + retry) branch;
the assertions invert on round-1 code. Honest.

### (5) F4 reindent: any logic change? No.
`git diff -w 0309503 5841a6f` on both run files shows only: the hard-stop arm moved up, the old
`deadline = cts + GRACE_SECONDS` + inline arm removed, and the existing inner
try/except-KeyboardInterrupt/finally(_finalize) wrapped in an outer `try ... finally:
hard_stop.cancel()`. The S1_LEGGED block and `_finalize` call are byte-identical (only reindented).
The cancel now runs even if `_finalize` RAISES (fast failure) while a `_finalize` that HANGS still
lets the timer fire -- both intended.

### (6) Suite counts: 1267 passed, 5 skipped (see above).

## Findings (round 2)

### R2-1 (LOW, non-blocking hardening -- the builder's own flag, confirmed): stand-down early returns rely on process exit
The hard stop is armed at the top of main() but the stand-down early-return paths (params-sha,
no-bucket, no-strike, width) return from main() BEFORE the outer try/finally, so they do not cancel
the timer. This is safe for every caller that exists today: production exits via `SystemExit(main())`
(timer dies with the process), and the only in-process caller uses a past close (guard). Residual
fragility: a FUTURE in-process caller -- e.g. a new test that calls `run_v32.main()`/`run_v33.main()`
with a NEAR or FUTURE `--close` and hits a stand-down early return -- would arm a real daemon timer
that outlives main() and `os._exit(3)` the test process ~130s later (the past-deadline guard only
covers historical closes). Suggested belt: cancel the hard stop on the stand-down early-return paths
too (or span the whole main body with the try/finally). No such caller exists now, so this does not
block.

### R2-2 (TRIVIAL, ASCII nit): F4 reindent carried a pre-existing em-dash onto a `+` line
run_v32.py:2003-area and run_v33.py: the reindented `logger.warning("[V32] Ctrl+C - flushing ...")`
line uses an em-dash (U+2014), so it appears as a non-ASCII ADDED (`+`) line in this diff. The byte
is pre-existing (not newly authored by this change), but if the ASCII-on-touched-lines rule is strict
it could be ASCII-ized ("Ctrl+C -- flushing") while the line is being touched. Cosmetic only.

## Round-1 findings status
- F1 -> FIXED (R2 check 4).
- F5 -> FIXED: arming now precedes the settlement-backfill sweep + discovery proxy calls, covering a
  synchronous wake/discovery hang (round-1 F5 residual gap closed).
- F4 -> FIXED: cancel survives a raising `_finalize` (R2 check 5).
- F2 (legacy `service/run_window.py` twin) -> still open, still out of scope for this hotfix.
- F3 (on_open-race `ws_error` label) -> unchanged; cosmetic.
