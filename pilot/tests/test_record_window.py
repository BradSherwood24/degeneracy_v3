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
    WindowRecorder,
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
