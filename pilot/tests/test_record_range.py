"""Passive RANGE-bucket recorder: discovery filtering by close_time, thin-channel subscription,
memory-light journal record shape (round-trips through journal_io.open_journal in the pilot shape),
stand-down path, and reuse of the shared watchdog/run-loop. FAKES ONLY — never dials the proxy."""

from __future__ import annotations

import asyncio
import json
import os

from service.journal import load_journal
from service.journal_io import open_journal
from service.record_range import (
    RANGE_CHANNELS,
    Bucket,
    RangeDiscovery,
    RangeRecorder,
    StreamJournal,
    discover_range_markets,
    journal_filename,
    watchdog_action,
)
from service.record_window import (
    DEADLINE,
    FORCE_CLOSE,
    run_recording,
    watchdog_action as ww_watchdog,
    write_standdown_summary,
)
from service.wake import StandDown
from service.ws_client import KalshiWebSocketClient, WsCallbacks

CLOSE = "2026-08-20T21:00:00Z"
OTHER = "2026-08-20T20:00:00Z"
NOW = 1755000000.0  # well before the 2026-08-20T21:00Z close (leg is live)
MK1 = "KXBTC-26AUG2021-B114000"
MK2 = "KXBTC-26AUG2021-B114250"


def _mkt(ticker, floor, cap, close=CLOSE, open_t="2026-08-20T20:00:00Z",
         event="KXBTC-26AUG2021", status="active", exch=2):
    return {"ticker": ticker, "floor_strike": floor, "cap_strike": cap, "close_time": close,
            "open_time": open_t, "event_ticker": event, "status": status, "exchange_index": exch}


class FakeProxy:
    """Serves one page of /markets from a fixed list; records the params it was asked for."""

    def __init__(self, markets):
        self._markets = markets
        self.calls = []

    def rest_get(self, path, params=None):
        self.calls.append((path, params))
        return {"markets": list(self._markets), "cursor": None}


# === discovery: filters by close_time, keeps all widths, captures floor/cap, drops dead gens ===


def test_discovery_filters_by_close_time_and_captures_floor_cap() -> None:
    markets = [
        _mkt(MK1, 114000.0, 114250.0),
        _mkt(MK2, 114250.0, 114500.0),
        _mkt("KXBTC-26AUG2020-B1", 113000.0, 113250.0, close=OTHER),  # different close -> excluded
    ]
    disc = discover_range_markets(FakeProxy(markets), CLOSE, NOW)
    assert disc.tickers == [MK1, MK2]  # only the co-settling pair, ticker-sorted
    b0 = disc.buckets[0]
    assert (b0.floor, b0.cap, b0.exchange_index) == (114000.0, 114250.0, 2)
    assert disc.generations == 1


def test_discovery_keeps_all_widths_across_generations() -> None:
    # A second generation (distinct open_time/event) co-settling at the same close -> ALL kept.
    markets = [
        _mkt(MK1, 114000.0, 114250.0),
        _mkt("KXBTC-26AUG2021W-B1", 114000.0, 114500.0, open_t="2026-08-14T21:00:00Z",
             event="KXBTC-26AUG2021W"),
    ]
    disc = discover_range_markets(FakeProxy(markets), CLOSE, NOW)
    assert set(disc.tickers) == {MK1, "KXBTC-26AUG2021W-B1"}
    assert disc.generations == 2


def test_discovery_drops_all_dead_generation() -> None:
    dead = [_mkt(MK1, 114000.0, 114250.0, status="settled"),
            _mkt(MK2, 114250.0, 114500.0, status="finalized")]
    disc = discover_range_markets(FakeProxy(dead), CLOSE, NOW)
    assert disc.buckets == ()  # stand-down universe


def test_discovery_empty_when_no_co_settling() -> None:
    disc = discover_range_markets(FakeProxy([_mkt("X", 1.0, 2.0, close=OTHER)]), CLOSE, NOW)
    assert disc.tickers == []


def test_discovery_narrows_fetch_to_target_close() -> None:
    proxy = FakeProxy([_mkt(MK1, 114000.0, 114250.0)])
    discover_range_markets(proxy, CLOSE, NOW)
    _, params = proxy.calls[0]
    assert params["series_ticker"] == "KXBTC"
    assert params["min_close_ts"] == params["max_close_ts"]  # pinned to the one close epoch


# === subscription: two channels only, all tickers; default caller unchanged (back-compat) ===


class FakeProxyAuth:
    def ws_connect_params(self):
        return "wss://example.invalid/ws", {}


def test_range_client_subscribes_two_channels_all_tickers() -> None:
    sent: list[dict] = []

    class RecSock:
        async def send(self, s):
            sent.append(json.loads(s))

    client = KalshiWebSocketClient(FakeProxyAuth(), tickers=[MK1, MK2], callbacks=WsCallbacks(),
                                   channels=RANGE_CHANNELS)
    client.ws = RecSock()  # type: ignore[assignment]
    asyncio.run(client.on_open())
    channels = [m["params"]["channels"][0] for m in sent]
    assert channels == ["orderbook_delta", "trade"]  # NO ticker
    for m in sent:
        assert m["params"]["market_tickers"] == [MK1, MK2]  # every ticker on each channel


def test_default_client_still_subscribes_full_public_set() -> None:
    # Back-compat: existing callers that omit `channels` get the unchanged public set.
    client = KalshiWebSocketClient(FakeProxyAuth(), tickers=[MK1], callbacks=WsCallbacks())
    assert client.channels == ("orderbook_delta", "trade", "ticker")


# === journal shape: write-through record round-trips in the pilot shape ===


def _discovery():
    return RangeDiscovery(CLOSE, (Bucket(MK1, 114000.0, 114250.0, "KXBTC-26AUG2021",
                                         "2026-08-20T20:00:00Z", "active", 2),), 1)


def test_streamed_record_roundtrips_in_pilot_shape(tmp_path) -> None:
    path = os.path.join(tmp_path, journal_filename(CLOSE))
    j = StreamJournal(path, flush_every=1)
    j.open()
    rec = RangeRecorder(_discovery(), j, clock=lambda: NOW)
    j.append("window_meta", rec.discovery.window_meta(), NOW)
    # Feed a frame exactly as ws_client's tap would: (stream, {type, msg}).
    rec.tap("kalshi_ws", {"type": "orderbook_snapshot",
                          "msg": {"market_ticker": MK1, "yes_dollars_fp": [[0.44, 100]]}})
    rec.tap("kalshi_ws", {"type": "trade", "msg": {"market_ticker": MK1, "taker_side": "yes"}})
    j.close()

    # Raw lines carry the exact pilot record shape, in gap-free idx order.
    with open_journal(path) as f:
        recs = [json.loads(line) for line in f if line.strip()]
    assert [r["idx"] for r in recs] == [0, 1, 2]
    assert all(set(r) == {"idx", "kind", "local_ts", "obj"} for r in recs)
    assert recs[0]["kind"] == "window_meta" and recs[0]["obj"]["ticker_count"] == 1
    assert recs[0]["obj"]["buckets"][0]["floor"] == 114000.0
    frame = recs[1]
    assert frame["kind"] == "kalshi_ws"
    assert frame["obj"]["type"] == "orderbook_snapshot"
    assert frame["obj"]["msg"]["market_ticker"] == MK1
    # ...and the pilot's own loader reads it back (gz-transparent open_journal path).
    reloaded = load_journal(path)
    assert len(reloaded) == 3
    assert rec.counts["ws_orderbook_snapshot"] == 1 and rec.counts["ws_trade"] == 1


def test_journal_filename_is_fixed_width_sortable() -> None:
    assert journal_filename(CLOSE) == "20260820T210000Z.jsonl"


# === stand-down path ===


def test_standdown_summary_written_for_empty_universe(tmp_path) -> None:
    spath = os.path.join(tmp_path, "summary.jsonl")
    disc = discover_range_markets(FakeProxy([]), CLOSE, NOW)
    assert disc.buckets == ()
    sd = StandDown(CLOSE, f"no KXBTC range markets co-settling at {CLOSE}")
    s = write_standdown_summary(spath, sd, clock=lambda: 123.0)
    assert s["stand_down"] is True
    with open(spath) as f:
        line = json.loads(f.readline())
    assert line["close_time"] == CLOSE and line["stand_down"] is True


# === watchdog / run-loop reuse (the SAME decision + supervisor as record_window) ===


def test_watchdog_is_the_shared_record_window_decision() -> None:
    assert watchdog_action is ww_watchdog  # re-exported, not re-implemented
    assert watchdog_action(100.0, 100.0, 0.0, 0.0, 30.0, 45.0) == DEADLINE
    assert watchdog_action(0.0, 100.0, 999.0, 0.0, 30.0, 45.0) == FORCE_CLOSE


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWsClient:
    """Feeds `frames_per_dial[i]` into the recorder tap on dial i, then blocks until force_close."""

    def __init__(self, recorder, frames_per_dial, lag=1.0, silence=0.5):
        self.recorder = recorder
        self.frames_per_dial = frames_per_dial
        self._lag = lag
        self._silence = silence
        self.calls = 0
        self.force_closed = 0
        self.dropped_no_market = 0
        self._closed = asyncio.Event()

    async def connect(self):
        idx = self.calls
        self.calls += 1
        for stream, env in (self.frames_per_dial[idx] if idx < len(self.frames_per_dial) else []):
            self.recorder.tap(stream, env)
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


def test_run_recording_streams_frames_and_hits_deadline(tmp_path) -> None:
    clock = FakeClock(0.0)
    path = os.path.join(tmp_path, journal_filename(CLOSE))
    j = StreamJournal(path, flush_every=1)
    j.open()
    j.append("window_meta", _discovery().window_meta(), 0.0)
    rec = RangeRecorder(_discovery(), j, clock=clock)
    frames = [[("kalshi_ws", {"type": "orderbook_snapshot",
                              "msg": {"market_ticker": MK1, "yes_dollars_fp": [[0.44, 1]]}})]]
    rec.ws_client = FakeWsClient(rec, frames, lag=1.0, silence=0.5)

    async def fast_sleep(_):
        clock.t += 60.0

    asyncio.run(run_recording(rec, deadline=100.0, sleep=fast_sleep))
    j.close()
    assert rec.ws_client.calls >= 1
    reloaded = load_journal(path)
    assert len(reloaded) == 2  # window_meta + the one snapshot
    assert rec.counts["ws_orderbook_snapshot"] == 1


def test_run_recording_force_closes_and_alarms_on_stale_lag(tmp_path) -> None:
    clock = FakeClock(0.0)
    path = os.path.join(tmp_path, journal_filename(CLOSE))
    j = StreamJournal(path, flush_every=1)
    j.open()
    rec = RangeRecorder(_discovery(), j, clock=clock)
    rec.ws_client = FakeWsClient(rec, [[], []], lag=999.0, silence=0.0)  # permanently stale

    async def fast_sleep(_):
        clock.t += 60.0

    asyncio.run(run_recording(rec, deadline=100.0, lag_threshold=30.0, sleep=fast_sleep))
    j.close()
    assert rec.ws_client.force_closed >= 1
    alarms = [r for r in load_journal(path).iter_records()
              if r["kind"] == "alarm" and r["obj"].get("alarm") == "watchdog_stale"]
    assert alarms  # the shared supervisor recorded the watchdog trip to the streamed journal
