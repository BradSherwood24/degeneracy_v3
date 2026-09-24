# V3.2/V3.3 recorder deadline hang -- build report

Branch: `fix/recorder-deadline` (off origin/main `ba1694f`). Worktree: `dv3_wt_v11`.
Scope: the passive window recorder loop shared by V3.2 (`service/run_v32.py`) and V3.3
(`service/run_v33.py`). No params/falsifier/ledger/order/executor changes. ASCII only.

## The incident (2026-09-23 16:00Z window)

The V3.2 run for the 16:00Z close (started 15:40Z) hung after its window closed and never exited.
From `pilot/logs_v32/scheduler.out` + the journal: ~20 `order_status` GETs returned 404 at 15:46Z,
a replace_rate stand-down at 15:46:34Z, then a WebSocket reconnect storm (code 1006 closes, re-dials
15:46..15:56:42Z). Deadline = close + `GRACE_SECONDS`(10) = 16:00:10Z. The journal kept recording
past the deadline -- the last records are fresh `orderbook_snapshot` frames at 16:01:36Z (a dial that
COMPLETED after the deadline) -- then total silence. No `[V32] window done` was ever logged, the
process never exited, and because the scheduled task uses `MultipleInstances=IgnoreNew`, every hourly
fire afterwards was skipped (11 windows lost). V3.3 shares the same loop but is protected externally
by `service/supervisor.py`'s watchdog (kills a child at close + `WATCHDOG_GRACE_S`=120 s); the plain
V3.2 task has no such protection.

## Root-cause hypothesis -- VERDICT: HOLDS (confirmed against the code)

The hypothesis holds exactly as stated. Trace, pre-fix:

1. `record_window.py::run_recording` (pre-fix lines ~217-257): each loop iteration creates a
   `supervise()` task that polls `watchdog_action(...)`; on `DEADLINE` or `FORCE_CLOSE` it did
   `await ws.force_close()` and then **RETURNED** (pre-fix line 244 `return`).
2. `ws_client.py::KalshiWebSocketClient.force_close()` (lines 164-174) is a **no-op when
   `self.ws is None`** (`if ws is None: return`). `self.ws` is None during the whole minting/dialing
   phase: `connect()` (lines 123-140) first calls the SYNC `self.proxy_auth.ws_connect_params()`
   (blocks the event loop), then `async with websockets.connect(...) as websocket:` and only THEN
   sets `self.ws = websocket` (line 131).
3. So a deadline landing mid-dial -> supervisor performs a no-op close and returns -> the dial then
   completes (`self.ws` set, `handler()` entered) -> `handler()`'s `async for message in self.ws`
   (line 201) sits on an idle socket (markets closed, no frames) with **no supervisor left to close
   it** -> `await ws.connect()` never returns -> `run_recording` never returns -> the
   `asyncio.gather` in `run_v32_window` never completes -> `_finalize` never runs -> the process never
   exits. This matches the journal's post-deadline `orderbook_snapshot` at 16:01:36Z followed by
   silence, and the missing `window done`.

Also confirmed: `connect()` had no overall timeout, and `ws_connect_params()` is a synchronous
`requests` call with retry/backoff that blocks the event loop (so while it is minting, NOTHING in
asyncio -- supervisor tick or timeout -- can fire; only an out-of-loop stop can save that case).

## The change (minimal; happy path unchanged)

### `pilot/service/record_window.py`

1. **Supervisor keeps supervising until `stop` (the deadline/force-close is now latched-and-retried).**
   `run_recording`'s `supervise()` now latches a `forcing` flag once it decides to end the dial
   (`DEADLINE`, or `FORCE_CLOSE` after recording the `watchdog_stale` alarm) and, instead of returning
   after one `force_close()`, retries `await ws.force_close()` on every tick until `stop` is set (i.e.
   until the connect task actually ends). A no-op close (dial still minting, `ws is None`) therefore
   no longer ends supervision -- the close lands the instant the socket exists. A trailing
   `await asyncio.sleep(0)` after the retry yields so `connect()` can unwind and set `stop` even under
   a non-yielding injected test sleep (production `asyncio.sleep` already yields).

2. **Each dial is bounded by the deadline.** `await ws.connect()` is now
   `await asyncio.wait_for(ws.connect(), timeout=max(0.0, deadline - clock()) + connect_timeout_slack)`
   (`CONNECT_TIMEOUT_SLACK_S = 5.0`, injectable). On `asyncio.TimeoutError` it journals a
   `deadline_forced_close` alarm (NOT an error / not logged as a warning) and treats it as a normal
   end-of-window close. This is the in-loop backstop for a dial that outran the deadline and that the
   supervisor's close could not reach. `connect_timeout_slack` was added as an optional kwarg (same
   default) purely so the backstop is testable in real time without a 5 s test.
   No new dial can begin at/after the deadline: the `while recorder.clock() < deadline` guard is the
   only place a dial starts, and the post-connect path only `mark_all_suspect()`s (never dials).

3. **In-process HARD STOP (belt-and-braces), out of asyncio.** New `arm_hard_stop(deadline,
   close_label, ...)` arms a **daemon** `threading.Timer` at `deadline + HARD_STOP_GRACE_S`
   (`HARD_STOP_GRACE_S = 120.0`). On firing it logs
   `[HARD-STOP] window <close> still running at close+130s; exiting 3`, flushes logging handlers, and
   calls `os._exit(HARD_STOP_EXIT_CODE=3)`. `clock`, `exit_fn`, and `timer_factory` are injected so
   tests never touch a real thread or real `os._exit`. This is the ONLY guard for the case the
   hypothesis flags (the event loop wedged inside the sync mint), which no asyncio timeout can break.
   The stop deliberately lands at close + GRACE_SECONDS(10) + 120 = **close + 130 s**, a hair AFTER
   the supervisor's external close + 120 s watchdog, so under V3.3 the supervisor always wins and the
   two never race; under the bare V3.2 task the in-process stop is the guarantee.

### `pilot/service/run_v32.py` and `pilot/service/run_v33.py`

Import `arm_hard_stop`; in `main()`, arm it (`hard_stop = arm_hard_stop(deadline, close_iso)`) right
before `asyncio.run(run_v32_window(...))` / `run_v33_window(...)`, and `hard_stop.cancel()` at the end
of the `finally` block, AFTER `_finalize` and the `[V32]/[V33] window done` log (so the timer still
protects finalize itself). Nothing else in either `main()` changed.

`pilot/service/ws_client.py` was intentionally left untouched: the retry mechanism does not depend on
`force_close()` signalling whether it closed, and adding a return value would have forced edits to
every fake ws client in the suite for no functional gain.

## Tests added (`pilot/tests/test_record_window.py`)

- `test_run_recording_deadline_mid_dial_does_not_hang` -- reproduces the escape with `_EscapeFakeWs`
  (deadline lands while `ws is None`; the dial then completes; the handler would block forever). The
  test wraps `run_recording` in a real-time `asyncio.wait_for(..., 5.0)`: the OLD code hangs (would
  raise `TimeoutError` -> fail), the fixed code returns. Asserts `calls == 1` (no re-dial past the
  deadline), `noop_closes >= 1` (supervisor kept supervising through the no-op close), and
  `real_closes == 1` (closed the socket once it existed). Covers required cases (a) and (b).
- `test_run_recording_wait_for_backstop_cancels_wedged_dial` -- `_WedgedFakeWs` whose dial never
  returns and whose `force_close` never lands; only the `wait_for` backstop can end it. Asserts a
  `deadline_forced_close` alarm is journaled and `calls == 1`.
- `test_arm_hard_stop_arms_daemon_timer_at_deadline_plus_grace` -- fake timer factory + injected
  exit_fn; asserts the timer is started, `daemon is True`, delay == `(deadline + grace) - now`, no
  exit before firing, and `.cancel()` propagates. Covers required case (d).
- `test_arm_hard_stop_fires_exit_code_when_not_cancelled` -- firing the fake timer calls
  `exit_fn(HARD_STOP_EXIT_CODE)` (never a real `os._exit`).
- `test_hard_stop_grace_lands_just_after_supervisor_watchdog` -- pins `HARD_STOP_GRACE_S == 120.0` and
  `GRACE_SECONDS + HARD_STOP_GRACE_S == 130.0` (the close+130 s figure).

Required case (c), happy path unchanged, is covered by the pre-existing (unmodified) tests
`test_run_recording_deadline_exits_and_flushes` (live socket force-closed at the deadline) and
`test_run_recording_force_closes_on_stale_lag` (a socket that trips the watchdog re-dials before the
deadline), plus the analogous fakes in `test_record_range.py`, `test_run_v32.py`, `test_run_window.py`
-- all still green with no fake modifications.

## Suite result

`python -m pytest -q` from `dv3_wt_v11/pilot`: **1269 passed, 1 skipped** in ~31 s (main baseline
was 1264 passed, 1 skipped; +5 new tests). The recorder/ws/run subset (`test_record_window`,
`test_record_range`, `test_run_v32`, `test_run_window`, `test_ws_client`) is 143 passed.

## Deliberately left alone

- `ws_client.py` (`connect()`, `force_close()`) -- no signature/behaviour change; the fix lives in the
  loop that drives it.
- Params, falsifier, ledger schemas, order paths, executor -- untouched. No sha changes.
- `self.ws` is still not reset to None after a dial ends (pre-existing); the retry mechanism does not
  rely on the no-op distinction, so this was left as-is to keep the change surgical.
- The supervisor's external watchdog (`service/supervisor.py`, close+120 s) is unchanged; the
  in-process stop is intentionally 10 s later so it never fights the supervisor.
