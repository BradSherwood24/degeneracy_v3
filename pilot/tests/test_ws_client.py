"""WS client tests — ported + ADAPTED from degeneracy_v2 tests/kalshi/test_ws.py.

Adaptations: market-keyed dispatch (callbacks receive (market_ticker, payload)); multi-ticker
subscribe payloads; missing-market_ticker fail-closed; public-channel set (orderbook_delta, trade,
ticker) with private only on include_private. The lag/silence/seq-gap/force_close mechanics are
ported faithfully and their tests carry over.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from service.ws_client import KalshiWebSocketClient, WsCallbacks

MK = "KXBTC15M-TEST"
MK2 = "KXBTCD-TEST-T100"


class FakeProxyAuth:
    def ws_connect_params(self):
        return "wss://example.invalid/ws", {}


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def make_client(record=None, clock=None, tickers=None, include_private=False, **cbs):
    seen: dict[str, list] = {
        k: [] for k in ("ticker", "snapshot", "delta", "position", "fill", "trade")
    }

    def mk(name):
        return lambda m, p: seen[name].append((m, p))

    callbacks = WsCallbacks(
        on_ticker=cbs.get("on_ticker", mk("ticker")),
        on_orderbook_snapshot=mk("snapshot"),
        on_orderbook_delta=mk("delta"),
        on_position=mk("position"),
        on_fill=mk("fill"),
        on_trade=mk("trade"),
    )
    kw = {} if clock is None else {"clock": clock}
    client = KalshiWebSocketClient(
        FakeProxyAuth(),  # type: ignore[arg-type]
        tickers=tickers or [MK],
        callbacks=callbacks,
        include_private=include_private,
        record=record,
        **kw,  # type: ignore[arg-type]
    )
    return client, seen


def wire(msg_type: str, payload: dict) -> str:
    return json.dumps({"type": msg_type, "msg": payload})


def wire_seq(msg_type: str, payload: dict, *, sid: object, seq: int) -> str:
    return json.dumps({"type": msg_type, "sid": sid, "seq": seq, "msg": payload})


def wire_delta_ts(*, ts_ms=None, ts=None, market=MK) -> str:
    msg: dict = {"side": "yes", "market_ticker": market}
    if ts_ms is not None:
        msg["ts_ms"] = ts_ms
    if ts is not None:
        msg["ts"] = ts
    return json.dumps({"type": "orderbook_delta", "msg": msg})


# === dispatch table (market-keyed) ===


def test_dispatch_table_routes_every_channel_with_market_key() -> None:
    client, seen = make_client()
    client.on_message(wire("ticker", {"price_dollars": 0.45, "market_ticker": MK}))
    client.on_message(wire("orderbook_snapshot", {"yes_dollars_fp": [], "market_ticker": MK}))
    client.on_message(wire("orderbook_delta", {"side": "yes", "market_ticker": MK}))
    client.on_message(wire("market_positions", {"position": 1, "market_ticker": MK}))
    client.on_message(wire("fill", {"count": 1, "market_ticker": MK}))
    client.on_message(wire("trade", {"taker_side": "yes", "market_ticker": MK}))
    assert seen["ticker"] == [(MK, {"price_dollars": 0.45, "market_ticker": MK})]
    assert seen["snapshot"] == [(MK, {"yes_dollars_fp": [], "market_ticker": MK})]
    assert seen["delta"] == [(MK, {"side": "yes", "market_ticker": MK})]
    assert seen["position"] == [(MK, {"position": 1, "market_ticker": MK})]
    assert seen["fill"] == [(MK, {"count": 1, "market_ticker": MK})]
    assert seen["trade"] == [(MK, {"taker_side": "yes", "market_ticker": MK})]


def test_multi_ticker_dispatch_keys_by_market() -> None:
    client, seen = make_client(tickers=[MK, MK2])
    client.on_message(wire("orderbook_delta", {"side": "yes", "market_ticker": MK}))
    client.on_message(wire("orderbook_delta", {"side": "no", "market_ticker": MK2}))
    markets = [m for m, _ in seen["delta"]]
    assert markets == [MK, MK2]


def test_missing_market_ticker_fail_closed_recorded_not_dispatched() -> None:
    recorded: list = []
    client, seen = make_client(record=lambda s, e: recorded.append((s, e)))
    client.on_message(wire("orderbook_delta", {"side": "yes"}))  # no market_ticker
    assert seen["delta"] == []  # NOT dispatched (fail closed)
    assert client.dropped_no_market == 1
    assert recorded == [("kalshi_ws", {"type": "orderbook_delta", "msg": {"side": "yes"}})]  # journaled


def test_control_messages_ignored_and_not_recorded() -> None:
    recorded: list = []
    client, seen = make_client(record=lambda s, e: recorded.append((s, e)))
    for t in ("subscribed", "unsubscribed", "error", "ok"):
        client.on_message(wire(t, {"noise": True}))
    assert recorded == []
    assert all(v == [] for v in seen.values())


def test_recorder_tap_fires_before_dispatch() -> None:
    order: list[str] = []
    recorded: list = []

    def record(stream, env):
        order.append("record")
        recorded.append((stream, env))

    client, _ = make_client(record=record, on_ticker=lambda m, p: order.append("dispatch"))
    client.on_message(wire("ticker", {"price_dollars": 0.5, "market_ticker": MK}))
    assert order == ["record", "dispatch"]
    assert recorded == [("kalshi_ws", {"type": "ticker", "msg": {"price_dollars": 0.5, "market_ticker": MK}})]


def test_unknown_channel_is_dropped() -> None:
    client, seen = make_client()
    client.on_message(wire("brand_new_channel", {"price_dollars": 0.61, "market_ticker": MK}))
    assert all(v == [] for v in seen.values())  # adapted: no ticker fall-through in the pilot


def test_missing_callback_is_dropped_not_crashed() -> None:
    client = KalshiWebSocketClient(
        FakeProxyAuth(),  # type: ignore[arg-type]
        tickers=["T"],
        callbacks=WsCallbacks(),  # all None
    )
    client.on_message(wire("ticker", {"price_dollars": 0.5, "market_ticker": "T"}))  # no raise


def test_empty_tickers_rejected() -> None:
    with pytest.raises(ValueError):
        KalshiWebSocketClient(FakeProxyAuth(), tickers=[], callbacks=WsCallbacks())  # type: ignore[arg-type]


# === subscription payloads (multi-ticker) ===


def test_subscription_message_multi_ticker() -> None:
    client, _ = make_client(tickers=[MK, MK2])
    msg = client.subscription_message("orderbook_delta")
    assert msg == {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": [MK, MK2]},
    }


def test_on_open_subscribes_public_only_by_default() -> None:
    sent: list[dict] = []

    class RecSock:
        async def send(self, s):
            sent.append(json.loads(s))

    client, _ = make_client(tickers=[MK])
    client.ws = RecSock()  # type: ignore[assignment]
    asyncio.run(client.on_open())
    channels = [m["params"]["channels"][0] for m in sent]
    assert channels == ["orderbook_delta", "trade", "ticker"]


def test_on_open_includes_private_when_flagged() -> None:
    sent: list[dict] = []

    class RecSock:
        async def send(self, s):
            sent.append(json.loads(s))

    client, _ = make_client(tickers=[MK], include_private=True)
    client.ws = RecSock()  # type: ignore[assignment]
    asyncio.run(client.on_open())
    channels = [m["params"]["channels"][0] for m in sent]
    assert channels == ["orderbook_delta", "trade", "ticker", "market_positions", "fill"]


# === lag gauge (ported) ===


def test_lag_computed_from_ts_ms() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    assert client.current_lag_seconds() is None
    client.on_message(wire_delta_ts(ts_ms=995_000))
    assert client.current_lag_seconds() == pytest.approx(5.0)


def test_lag_from_ts_seconds_fallback() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts=990))
    assert client.current_lag_seconds() == pytest.approx(10.0)


def test_lag_from_iso_string_fallback() -> None:
    from datetime import UTC, datetime

    iso = "2026-07-12T02:03:45.415132Z"
    server_epoch = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    clock = FakeClock(server_epoch + 7.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts=iso))
    assert client.current_lag_seconds() == pytest.approx(7.0)


def test_lag_grows_the_07_12_signature() -> None:
    clock = FakeClock(1_000.0)
    client, seen = make_client(clock=clock)
    for i in range(5):
        clock.t = 1_000.0 + i
        client.on_message(wire_delta_ts(ts_ms=1_000_000))
    assert client.current_lag_seconds() == pytest.approx(4.0)
    assert len(seen["delta"]) == 5


def test_lag_unchanged_by_frame_without_server_ts() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts_ms=995_000))
    clock.t = 2_000.0
    client.on_message(wire("ticker", {"price_dollars": 0.5, "market_ticker": MK}))
    assert client.current_lag_seconds() == pytest.approx(5.0)


def test_malformed_ts_is_inert() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts_ms=995_000))
    client.on_message(wire_delta_ts(ts="not-a-timestamp"))
    assert client.current_lag_seconds() == pytest.approx(5.0)


# === data_age (silence-aware) ===


def test_data_age_none_until_first_lag() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire("ticker", {"price_dollars": 0.5, "market_ticker": MK}))
    assert client.data_age_seconds() is None


def test_data_age_floors_frozen_lag_with_silence() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts_ms=995_100))  # lag 4.9
    assert client.data_age_seconds() == pytest.approx(4.9)
    clock.t = 1_025.0
    assert client.current_lag_seconds() == pytest.approx(4.9)  # frozen
    assert client.data_age_seconds() == pytest.approx(25.0)  # silence floors it


def test_data_age_uses_lag_when_chatty_but_stale() -> None:
    clock = FakeClock(1_000.0)
    client, _ = make_client(clock=clock)
    client.on_message(wire_delta_ts(ts_ms=400_000))
    assert client.data_age_seconds() == pytest.approx(600.0)


# === silence + force_close ===


def test_silence_stamped_on_every_message_including_control() -> None:
    clock = FakeClock()
    client, _ = make_client(clock=clock)
    assert client.silence_seconds() == 0.0
    clock.t = 100.0
    client.on_message(wire("ticker", {"price_dollars": 0.5, "market_ticker": MK}))
    assert client.last_message_ts == 100.0
    clock.t = 105.0
    client.on_message(wire("subscribed", {"noise": True}))
    assert client.last_message_ts == 105.0
    clock.t = 108.0
    assert client.silence_seconds() == pytest.approx(3.0)


def test_force_close_safe_when_ws_none() -> None:
    client, _ = make_client()
    asyncio.run(client.force_close())


def test_force_close_closes_and_resets_baselines() -> None:
    clock = FakeClock(50.0)
    client, _ = make_client(clock=clock)
    sock = FakeSocket()
    client.ws = sock  # type: ignore[assignment]
    client.last_message_ts = 10.0
    client.last_delta_lag_seconds = 99.0
    asyncio.run(client.force_close())
    assert sock.closed is True
    assert client.last_message_ts == 50.0
    assert client.last_delta_lag_seconds is None


# === seq-gap detection (ported) ===


def _delta(sid, seq, market=MK):
    return wire_seq("orderbook_delta", {"side": "yes", "market_ticker": market}, sid=sid, seq=seq)


def test_seq_in_order_no_reconnect() -> None:
    client, seen = make_client()
    for s in (5, 6, 7, 8):
        client.on_message(_delta(1, s))
    assert client._reconnect_requested is False
    assert len(seen["delta"]) == 4


def test_seq_gap_requests_reconnect() -> None:
    client, _ = make_client()
    client.on_message(_delta(1, 5))
    assert client._reconnect_requested is False
    client.on_message(_delta(1, 8))
    assert client._reconnect_requested is True


def test_seq_per_subscription_independent_sids() -> None:
    client, _ = make_client()
    client.on_message(_delta(1, 1))
    client.on_message(wire_seq("orderbook_snapshot", {"yes_dollars_fp": [], "market_ticker": MK}, sid=2, seq=1))
    client.on_message(_delta(1, 2))
    client.on_message(_delta(2, 2))
    assert client._reconnect_requested is False


def test_seq_scoped_to_orderbook_only() -> None:
    client, _ = make_client()
    client.on_message(wire_seq("trade", {"taker_side": "yes", "market_ticker": MK}, sid=9, seq=1))
    client.on_message(wire_seq("trade", {"taker_side": "yes", "market_ticker": MK}, sid=9, seq=500))
    client.on_message(wire_seq("ticker", {"price_dollars": 0.5, "market_ticker": MK}, sid=8, seq=3))
    client.on_message(wire_seq("ticker", {"price_dollars": 0.5, "market_ticker": MK}, sid=8, seq=999))
    assert client._reconnect_requested is False
    assert client._expected_seq == {}


def test_seq_rewind_not_a_gap_then_resync() -> None:
    client, _ = make_client()
    client.on_message(_delta(1, 5))
    client.on_message(_delta(1, 5))  # duplicate
    assert client._reconnect_requested is False
    client.on_message(_delta(1, 4))  # rewind
    assert client._reconnect_requested is False
    client.on_message(_delta(1, 5))  # in-order after resync
    assert client._reconnect_requested is False
    client.on_message(_delta(1, 9))  # genuine forward skip
    assert client._reconnect_requested is True


def test_malformed_seq_is_inert() -> None:
    client, seen = make_client()
    client.on_message(_delta(1, 5))
    client.on_message(
        json.dumps({"type": "orderbook_delta", "sid": 1, "seq": "garbage", "msg": {"side": "yes", "market_ticker": MK}})
    )
    assert client._reconnect_requested is False
    assert client._expected_seq[1] == 6
    client.on_message(_delta(1, 6))
    assert client._reconnect_requested is False
    assert len(seen["delta"]) == 3


def test_seq_reinit_after_resubscribe() -> None:
    clock = FakeClock()
    client, _ = make_client(clock=clock)
    client.on_message(_delta(1, 5))
    client.on_message(_delta(1, 9))
    assert client._reconnect_requested is True
    client._reconnect_requested = False
    client._expected_seq = {}
    client.on_message(_delta(7, 100))
    assert client._reconnect_requested is False


def test_sid_seq_not_recorded_replay_byte_identical() -> None:
    recorded: list = []
    client, _ = make_client(record=lambda s, e: recorded.append((s, e)))
    client.on_message(_delta(1, 1))
    assert recorded == [("kalshi_ws", {"type": "orderbook_delta", "msg": {"side": "yes", "market_ticker": MK}})]


class _ScriptedSocket:
    def __init__(self, messages) -> None:
        self._messages = list(messages)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self):
        self.closed = True


def test_handler_force_closes_and_stops_on_seq_gap() -> None:
    clock = FakeClock()
    client, seen = make_client(clock=clock)
    sock = _ScriptedSocket([_delta(1, 1), _delta(1, 5), _delta(1, 6)])
    client.ws = sock  # type: ignore[assignment]
    asyncio.run(client.handler())
    assert sock.closed is True
    assert len(seen["delta"]) == 2  # stopped after the gap frame; the 3rd never processed
