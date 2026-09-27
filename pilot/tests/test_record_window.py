"""Passive window recorder: watchdog decision, wiring + live-vs-replay parity, run loop with fakes,
flush/summary, stand-down summary. NEVER dials the live proxy."""

from __future__ import annotations

import asyncio
import json
import os

from service.journal import Journal
from service.replay import replay_books
from service.record_window import (
    CONTINUE,
    DEADLINE,
    FORCE_CLOSE,
    GRACE_SECONDS,
    HARD_STOP_EXIT_CODE,
    HARD_STOP_GRACE_S,
    WindowRecorder,
    arm_hard_stop,
    next_top_of_hour_iso,
    run_recording,
    watchdog_action,
    write_standdown_summary,
)
from service.wake import StandDown, WakeResult, discover_legs, ladder_check

MK = "KXBTC15M-26AUG201500-00"
HK = "KXBTCD-26AUG2015-T60000.00"
CLOSE = "2026-08-20T15:00:00Z"
NOW = 1700000000.0


def make_wake_result():
    m15 = [{"event_ticker": "KXBTC15M-26AUG201500", "ticker": MK, "close_time": CLOSE,
            "open_time": "2026-08-20T14:45:00Z", "floor_strike": 60000.0, "status": "active"}]
    mh = [{"event_ticker": "KXBTCD-26AUG2015", "ticker": f"KXBTCD-26AUG2015-T{60000 + i*100:.2f}",
           "close_time": CLOSE, "open_time": "2026-08-20T14:00:00Z",
           "floor_strike": 60000.0 + i * 100, "status": "active"} for i in range(3)]
    f, h = discover_legs(m15, mh, CLOSE, NOW)
    return WakeResult(CLOSE, f, h, ladder_check(h))


# === watchdog_action (pure) — the "watchdogs demonstrated firing" evidence ===


def test_watchdog_deadline() -> None:
    assert watchdog_action(100.0, 100.0, 0.0, 0.0, 30.0, 45.0) == DEADLINE
    assert watchdog_action(101.0, 100.0, 0.0, 0.0, 30.0, 45.0) == DEADLINE


def test_watchdog_force_close_on_lag() -> None:
    assert watchdog_action(0.0, 100.0, 31.0, 0.0, 30.0, 45.0) == FORCE_CLOSE


def test_watchdog_force_close_on_silence() -> None:
    assert watchdog_action(0.0, 100.0, 1.0, 46.0, 30.0, 45.0) == FORCE_CLOSE


def test_watchdog_unknown_age_is_startup_grace_not_forced() -> None:
    # data_age None (no timestamped frame yet); silence under threshold -> continue.
    assert watchdog_action(0.0, 100.0, None, 5.0, 30.0, 45.0) == CONTINUE
    # ...but a dead stream (silence over threshold) is still force-closed even with unknown lag.
    assert watchdog_action(0.0, 100.0, None, 46.0, 30.0, 45.0) == FORCE_CLOSE


def test_watchdog_continue_when_healthy() -> None:
    assert watchdog_action(0.0, 100.0, 2.0, 3.0, 30.0, 45.0) == CONTINUE


# === next_top_of_hour ===


def test_next_top_of_hour() -> None:
    # 2026-08-20T14:45:00Z -> next :00 is 15:00
    import datetime as dt
    now = dt.datetime(2026, 8, 20, 14, 45, tzinfo=dt.timezone.utc).timestamp()
    assert next_top_of_hour_iso(now) == "2026-08-20T15:00:00Z"


# === wiring + live-vs-replay parity ===


def _feed(recorder, msg_type, payload):
    """Simulate the WS client's on_message order: tap (journal) BEFORE dispatch."""
    recorder.tap("kalshi_ws", {"type": msg_type, "msg": payload})
    cb = {
        "orderbook_snapshot": recorder.callbacks.on_orderbook_snapshot,
        "orderbook_delta": recorder.callbacks.on_orderbook_delta,
        "trade": recorder.callbacks.on_trade,
        "ticker": recorder.callbacks.on_ticker,
    }[msg_type]
    cb(payload["market_ticker"], payload)


def test_recorder_builds_books_and_captures_tops_matching_replay() -> None:
    rec = WindowRecorder(make_wake_result(), Journal(), clock=lambda: NOW)
    _feed(rec, "orderbook_snapshot", {"market_ticker": MK, "yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": [[0.53, 50]]})
    _feed(rec, "orderbook_delta", {"market_ticker": MK, "side": "yes", "price_dollars": 0.45, "delta_fp": 20})
    _feed(rec, "orderbook_snapshot", {"market_ticker": HK, "yes_dollars_fp": [[0.10, 5]], "no_dollars_fp": []})
    _feed(rec, "trade", {"market_ticker": MK, "taker_side": "yes"})
    # live-captured tops == deterministic replay of the journal (Phase 1 golden parity)
    assert rec.book_tops == list(replay_books(rec.journal))
    assert rec.counts["trade"] == 1
    assert rec.counts["ws_orderbook_snapshot"] == 2


def test_recorder_mark_all_suspect() -> None:
    rec = WindowRecorder(make_wake_result(), Journal(), clock=lambda: NOW)
    _feed(rec, "orderbook_snapshot", {"market_ticker": MK, "yes_dollars_fp": [[0.44, 1]], "no_dollars_fp": []})
    assert rec.books[MK].suspect is False
    rec.mark_all_suspect()
    assert rec.books[MK].suspect is True


# === run_recording with a fake ws client ===


class FakeWsClient:
    def __init__(self, recorder, frames_per_dial, clock, lag=1.0, silence=0.5):
        self.recorder = recorder
        self.frames_per_dial = frames_per_dial
        self._clock = clock
        self._lag = lag
        self._silence = silence
        self.calls = 0
        self.force_closed = 0
        self.dropped_no_market = 0
        self._closed = asyncio.Event()

    async def connect(self):
        idx = self.calls
        self.calls += 1
        frames = self.frames_per_dial[idx] if idx < len(self.frames_per_dial) else []
        for msg_type, payload in frames:
            _feed(self.recorder, msg_type, payload)
        self._closed.clear()
        await self._closed.wait()

    async def force_close(self):
        self.force_closed += 1
        self._closed.set()

    def data_age_seconds(self):
        return self._lag

    def silence_seconds(self):
        return self._silence

    def current_lag_seconds(self):
        return self._lag


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_run_recording_deadline_exits_and_flushes(tmp_path) -> None:
    clock = FakeClock(0.0)
    rec = WindowRecorder(make_wake_result(), Journal(), clock=clock)
    frames = [[
        ("orderbook_snapshot", {"market_ticker": MK, "yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": [[0.53, 50]]}),
        ("orderbook_delta", {"market_ticker": MK, "side": "yes", "price_dollars": 0.45, "delta_fp": 20}),
    ]]
    ws = FakeWsClient(rec, frames, clock, lag=1.0, silence=0.5)
    rec.ws_client = ws

    async def fast_sleep(_):
        clock.t += 60.0  # each poll advances the clock a minute

    asyncio.run(run_recording(rec, deadline=100.0, sleep=fast_sleep))
    assert ws.calls >= 1
    # frames were journaled + booked, and parity holds after the run
    assert rec.book_tops == list(replay_books(rec.journal))
    jpath = os.path.join(tmp_path, "w.jsonl")
    spath = os.path.join(tmp_path, "summary.jsonl")
    summary = rec.flush(jpath, spath)
    assert summary["stand_down"] is False
    assert summary["records"] == len(rec.journal)
    assert os.path.exists(jpath)
    with open(spath) as f:
        line = json.loads(f.readline())
    assert line["close_time"] == CLOSE and line["ladder_ok"] is True


def test_run_recording_force_closes_on_stale_lag(tmp_path) -> None:
    clock = FakeClock(0.0)
    rec = WindowRecorder(make_wake_result(), Journal(), clock=clock)
    # lag permanently stale -> supervisor must FORCE_CLOSE (and record an alarm) before deadline
    ws = FakeWsClient(rec, [[], []], clock, lag=999.0, silence=0.0)
    rec.ws_client = ws

    async def fast_sleep(_):
        clock.t += 60.0

    asyncio.run(run_recording(rec, deadline=100.0, lag_threshold=30.0, sleep=fast_sleep))
    assert ws.force_closed >= 1
    alarms = [r for r in rec.journal.iter_records() if r["kind"] == "alarm"]
    assert any(a["obj"]["alarm"] == "watchdog_stale" for a in alarms)


# === stand-down summary ===


def test_standdown_summary_written(tmp_path) -> None:
    spath = os.path.join(tmp_path, "summary.jsonl")
    sd = StandDown(CLOSE, "no hourly leg co-settling")
    s = write_standdown_summary(spath, sd, clock=lambda: 123.0)
    assert s["stand_down"] is True and s["reason"] == "no hourly leg co-settling"
    with open(spath) as f:
        line = json.loads(f.readline())
    assert line["close_time"] == CLOSE and line["stand_down"] is True


# === 2026-09-23 hang: deadline-mid-dial escape + wait_for backstop ===


class _EscapeFakeWs:
    """Reproduces the 2026-09-23 hang. The deadline lands while ``connect()`` is still minting (``ws``
    is None), so the FIRST force_close is a no-op; THEN the in-flight dial completes and ``handler()``
    would idle forever on a dead market. The fixed supervisor must keep supervising through the no-op
    close and close the socket once it exists, and no new dial must start after the deadline."""

    def __init__(self) -> None:
        self.ws = None            # phase A: minting -- no live socket the close can reach
        self.calls = 0
        self.noop_closes = 0
        self.real_closes = 0
        self._dialed = asyncio.Event()   # released when the (post-deadline) dial completes
        self._closed = asyncio.Event()   # released only by a real force_close (live socket)

    async def connect(self):
        self.calls += 1
        self.ws = None
        await self._dialed.wait()        # the dial completes only after the deadline no-op close
        await self._closed.wait()        # phase B: socket live; handler idles until force_close

    async def force_close(self):
        if self.ws is None:
            self.noop_closes += 1
            # Model the incident: the mint/dial in flight completes right after the no-op close.
            self.ws = object()
            self._dialed.set()
            return
        self.real_closes += 1
        self._closed.set()

    def data_age_seconds(self):
        return None                       # unmeasured -- still in per-dial startup grace

    def silence_seconds(self):
        return 0.0

    def current_lag_seconds(self):
        return None


def test_run_recording_deadline_mid_dial_does_not_hang() -> None:
    clock = FakeClock(0.0)
    rec = WindowRecorder(make_wake_result(), Journal(), clock=clock)
    ws = _EscapeFakeWs()
    rec.ws_client = ws

    async def yielding_sleep(_):
        await asyncio.sleep(0)  # let connect() make progress between supervisor ticks
        clock.t += 60.0

    async def _run():
        # A real-time guard: the OLD code hangs here (connect never returns after the no-op close);
        # the fixed code returns promptly. asyncio.TimeoutError would fail the test.
        await asyncio.wait_for(
            run_recording(rec, deadline=100.0, sleep=yielding_sleep), timeout=5.0
        )

    asyncio.run(_run())
    assert ws.calls == 1          # no new dial started at/after the deadline
    assert ws.noop_closes >= 1    # supervisor kept supervising through the no-op close...
    assert ws.real_closes == 1    # ...and closed the socket once it existed


class _WedgedFakeWs:
    """A dial that never returns and whose force_close never lands (models an idle handler the close
    cannot reach). Only the ``asyncio.wait_for`` backstop can end it. connect() jumps the clock past
    the deadline so the loop does not re-dial once the wedged dial is cancelled."""

    def __init__(self, clock, deadline) -> None:
        self._clock = clock
        self._deadline = deadline
        self.ws = object()
        self.calls = 0
        self.force_closed = 0
        self._never = asyncio.Event()

    async def connect(self):
        self.calls += 1
        self._clock.t = self._deadline + 100.0
        await self._never.wait()

    async def force_close(self):
        self.force_closed += 1  # a close that cannot reach the wedged dial

    def data_age_seconds(self):
        return None

    def silence_seconds(self):
        return 0.0

    def current_lag_seconds(self):
        return None


def test_run_recording_wait_for_backstop_cancels_wedged_dial() -> None:
    clock = FakeClock(99.99)  # a hair before the deadline so the real wait_for timeout is short
    rec = WindowRecorder(make_wake_result(), Journal(), clock=clock)
    ws = _WedgedFakeWs(clock, deadline=100.0)
    rec.ws_client = ws

    async def _run():
        await asyncio.wait_for(
            run_recording(rec, deadline=100.0, poll_seconds=0.02, connect_timeout_slack=0.05),
            timeout=5.0,
        )

    asyncio.run(_run())
    alarms = [r for r in rec.journal.iter_records()
              if r["kind"] == "alarm" and r["obj"].get("alarm") == "deadline_forced_close"]
    assert alarms                 # the wedged dial was cancelled and journaled as a deadline close
    assert ws.calls == 1          # exactly one dial, no re-dial after the deadline


class _EarlyTimeoutFakeWs:
    """connect() raises a BARE TimeoutError on the first dial -- exactly what websockets raises on its
    open-handshake timeout, and on py3.12 `asyncio.TimeoutError is TimeoutError`, so this is
    indistinguishable by type from the deadline backstop. It happens BEFORE the deadline (the clock is
    NOT advanced during the failing dial), so it must be treated as an ordinary dial failure
    (ws_error + retry), NOT a quiet deadline_forced_close. The retry dial connects and, the deadline
    now reached, closes cleanly. The fake drives the clock so task ordering cannot change the verdict."""

    def __init__(self, clock, deadline) -> None:
        self._clock = clock
        self._deadline = deadline
        self.calls = 0
        self.force_closed = 0

    async def connect(self):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("simulated open-handshake timeout")  # clock stays < deadline
        self._clock.t = self._deadline + 100.0  # dial 2 connects; the window is now past its deadline
        # returns immediately -> a clean connected close; the loop sees clock >= deadline and stops

    async def force_close(self):
        self.force_closed += 1

    def data_age_seconds(self):
        return 1.0

    def silence_seconds(self):
        return 0.5

    def current_lag_seconds(self):
        return 1.0


def test_run_recording_early_timeout_is_ws_error_not_deadline_close() -> None:
    clock = FakeClock(0.0)
    rec = WindowRecorder(make_wake_result(), Journal(), clock=clock)
    ws = _EarlyTimeoutFakeWs(clock, deadline=100.0)
    rec.ws_client = ws

    async def yielding_sleep(_):
        await asyncio.sleep(0)  # yield only; the fake drives the clock, so dial 1 stays pre-deadline

    asyncio.run(run_recording(rec, deadline=100.0, sleep=yielding_sleep))
    kinds = [r["obj"].get("alarm") for r in rec.journal.iter_records() if r["kind"] == "alarm"]
    # A pre-deadline TimeoutError is a real dial failure: journaled as ws_error, and the loop re-dials.
    assert "ws_error" in kinds
    assert "deadline_forced_close" not in kinds
    assert ws.calls == 2          # the failed dial was retried


# === in-process HARD STOP (belt-and-braces) ===


class _FakeTimer:
    """A stand-in for threading.Timer: records arming/cancel and fires on demand (never a real thread,
    never a real os._exit)."""

    def __init__(self, delay, fn) -> None:
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


def test_arm_hard_stop_arms_daemon_timer_at_deadline_plus_grace() -> None:
    made: dict[str, _FakeTimer] = {}

    def factory(delay, fn):
        t = _FakeTimer(delay, fn)
        made["t"] = t
        return t

    exits: list[int] = []
    clock = FakeClock(1000.0)
    timer = arm_hard_stop(2000.0, CLOSE, grace_s=120.0,
                          clock=clock, exit_fn=exits.append, timer_factory=factory)
    assert timer is made["t"]
    assert made["t"].started is True
    assert made["t"].daemon is True                 # can never keep the process alive on its own
    assert abs(made["t"].delay - 1120.0) < 1e-9     # (deadline 2000 + grace 120) - now 1000
    assert exits == []                              # not fired
    timer.cancel()
    assert made["t"].cancelled is True


def test_arm_hard_stop_fires_exit_code_when_not_cancelled() -> None:
    exits: list[int] = []
    clock = FakeClock(0.0)
    t = arm_hard_stop(10.0, CLOSE, grace_s=5.0, clock=clock, exit_fn=exits.append,
                      timer_factory=lambda d, f: _FakeTimer(d, f))
    t.fire()                                        # simulate the timer elapsing (no real os._exit)
    assert exits == [HARD_STOP_EXIT_CODE]


def test_arm_hard_stop_does_not_arm_when_already_past_deadline() -> None:
    # A process armed more than GRACE_SECONDS + grace after close has no live window to guard: the
    # timer must NOT start/fire (this is also what keeps a real main() called with a historical close,
    # e.g. the v33 stand-down test, from os._exit-ing the whole test process).
    made: dict[str, _FakeTimer] = {}

    def factory(delay, fn):
        t = _FakeTimer(delay, fn)
        made["t"] = t
        return t

    exits: list[int] = []
    clock = FakeClock(10_000.0)  # well past deadline(100) + grace(120)
    timer = arm_hard_stop(100.0, CLOSE, grace_s=120.0,
                          clock=clock, exit_fn=exits.append, timer_factory=factory)
    assert made["t"].started is False   # no firing timer armed for an already-overdue window
    assert exits == []
    timer.cancel()                      # still safe to cancel
    assert made["t"].cancelled is True


def test_hard_stop_grace_lands_just_after_supervisor_watchdog() -> None:
    # The in-process stop fires at close + GRACE_SECONDS + HARD_STOP_GRACE_S = close + 130 s, a hair
    # after service.supervisor's external close + 120 s watchdog, so the two never race.
    assert HARD_STOP_GRACE_S == 120.0
    assert GRACE_SECONDS + HARD_STOP_GRACE_S == 130.0
