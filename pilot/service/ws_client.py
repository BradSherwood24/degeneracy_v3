"""Kalshi WebSocket client for the pilot — MULTI-TICKER, market-keyed, proxy-authenticated.

ADAPTED (not lifted — PLAN review F14) from degeneracy_v2 `kalshi/ws.py`. What CHANGED:

  * MULTI-TICKER: the constructor takes a LIST of market tickers; each channel is subscribed with
    ALL tickers in one command, and every dispatch is MARKET-KEYED — callbacks receive
    (market_ticker, payload). The ticker is extracted from the payload's `market_ticker` field;
    a frame missing it is FAIL-CLOSED (journaled by the recorder tap, but NOT dispatched), because
    the pilot runs two co-settling legs at once and an unattributed book update must never be
    applied to the wrong book.
  * AUTH via ProxyAuth: `connect()` fetches (ws_url, headers) FRESH on every dial from the proxy
    (signatures are ephemeral — re-minted on every re-dial, F14). No key material here.
  * CHANNELS: public set = orderbook_delta (its subscribe yields the initial orderbook_snapshot
    then deltas), trade, ticker. `market_positions` and `fill` are subscribed only when
    include_private=True (Phase 3 uses them).

What is KEPT FAITHFULLY from V2 (the load-bearing market-data resilience — do not re-derive):

  * seq-gap detection per sid on the ORDERBOOK channel only, with reconnect-for-fresh-snapshot
    (a missed orderbook_delta silently corrupts the book until the next snapshot, which only
    arrives at subscribe time; ticker/trade/fill are self-healing and are NOT seq-checked —
    reconnect-storm risk for zero protection).
  * the LAG watchdog (PRIMARY): `lag = local_wall - server_ts`. The 07-12 failure signature was a
    chatty stream whose content silently aged to +651s while the socket never went quiet, so the
    primary health gauge is lag, not silence — measured against the server timestamp on each frame
    that carries one (`ts_ms` epoch-ms preferred; `ts` ISO-string or numeric-epoch fallback).
  * the SILENCE watchdog (SECONDARY): stamped on EVERY inbound frame incl. control types; catches a
    truly dead TCP session that websockets' ping/pong keeps nominally "open".
  * `data_age_seconds() = max(lag, silence)` with fail-closed None semantics (unknown age counts as
    stale at the entry gate); `force_close()` resetting both baselines so a reconnect gets a full
    window before the watchdog could fire again.
  * the recorder tap invoked BEFORE dispatch with the parsed {type, msg} envelope (replay parity —
    sid/seq are consulted for gap detection but NOT recorded, so the recording stays byte-identical
    to what a replay reconstructs).
  * the injectable wall clock (tests inject a fake).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import websockets

from service.proxy_auth import ProxyAuth

logger = logging.getLogger(__name__)

# Control-plane message types: neither recorded nor dispatched (V2 semantics).
_CONTROL_TYPES = ("subscribed", "unsubscribed", "error", "ok")

# Seq-gap checking is scoped to the ORDERBOOK subscription only (V2 WP-2 F1).
_ORDERBOOK_TYPES = ("orderbook_snapshot", "orderbook_delta")

# Public channels always subscribed; private channels only when include_private=True.
_PUBLIC_CHANNELS = ("orderbook_delta", "trade", "ticker")
_PRIVATE_CHANNELS = ("market_positions", "fill")

# (market_ticker, payload)
MarketCallback = Callable[[str, dict], None]
# (stream_name, envelope) — envelope is {"type": ..., "msg": payload}
RecordCallback = Callable[[str, dict], None]


@dataclass
class WsCallbacks:
    """Injected per-channel handlers, each invoked as callback(market_ticker, payload). Sync
    callables invoked from the async loop — keep them fast (they feed BookMirror, pure compute)."""

    on_ticker: MarketCallback | None = None
    on_orderbook_snapshot: MarketCallback | None = None
    on_orderbook_delta: MarketCallback | None = None
    on_position: MarketCallback | None = None
    on_fill: MarketCallback | None = None
    on_trade: MarketCallback | None = None


class KalshiWebSocketClient:
    """See module docstring. One instance covers ALL of `tickers` on one connection."""

    def __init__(
        self,
        proxy_auth: ProxyAuth,
        tickers: list[str],
        callbacks: WsCallbacks,
        include_private: bool = False,
        record: RecordCallback | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not tickers:
            raise ValueError("KalshiWebSocketClient requires at least one ticker")
        self.proxy_auth = proxy_auth
        self.tickers = list(tickers)
        self.callbacks = callbacks
        self.include_private = include_private
        self.record = record
        self.ws: websockets.ClientConnection | None = None
        self.message_id = 1
        self._clock = clock
        # --- market-data resilience (V2 WP-2, kept faithfully) ---
        # Lag watchdog (PRIMARY): seconds our last server-timestamped frame trails the server.
        # None = never measured this dial (fail-closed: unknown lag counts as lagged).
        self.last_delta_lag_seconds: float | None = None
        # Silence watchdog (SECONDARY): stamped on EVERY inbound frame + at each dial start.
        self.last_message_ts = 0.0
        # Seq-gap: expected NEXT seq per subscription id. Reset on every dial.
        self._expected_seq: dict[object, int] = {}
        self._reconnect_requested = False
        # Observability (pilot addition): frames dropped for a missing market_ticker (fail-closed).
        self.dropped_no_market = 0

    # === connection ===

    async def connect(self) -> None:
        """Fetch FRESH proxy-minted params, dial, subscribe, and run the message loop until close.

        Every dial re-mints (F14): a re-dial after a seq gap or a dropped socket calls
        `ws_connect_params()` again, so the signature is never reused past its ephemeral life.
        """
        ws_url, headers = self.proxy_auth.ws_connect_params()
        async with websockets.connect(ws_url, additional_headers=headers) as websocket:
            self.ws = websocket
            # Fresh dial: clear reconnect flag + seq counters; seed the silence baseline so the
            # watchdog gives this connection a full window; lag unknown until the first snapshot
            # (the snapshot jumps every book to current).
            self._reconnect_requested = False
            self._expected_seq = {}
            self.last_message_ts = self._clock()
            self.last_delta_lag_seconds = None
            await self.on_open()
            await self.handler()

    def current_lag_seconds(self) -> float | None:
        """Seconds the last server-timestamped frame trailed the server (PRIMARY health gauge).
        None until the first timestamped frame of a dial (the entry gate treats None as lagged)."""
        return self.last_delta_lag_seconds

    def silence_seconds(self) -> float:
        """Seconds since the last inbound frame (0.0 before the first frame of a dial)."""
        if self.last_message_ts == 0.0:
            return 0.0
        return self._clock() - self.last_message_ts

    def data_age_seconds(self) -> float | None:
        """Effective market-view age = max(lag, silence); None while lag is unmeasured (fail closed).

        `current_lag_seconds` freezes during silence, so a fresh reading followed by a long quiet
        would read fresh while the book aged — flooring lag with the inbound silence catches that
        (V2 WP-2 F2)."""
        lag = self.last_delta_lag_seconds
        if lag is None:
            return None
        return max(lag, self.silence_seconds())

    async def force_close(self) -> None:
        """Close the live socket so the caller's loop re-dials (re-mint + fresh snapshots).

        Guarded for ws=None. Resets silence/lag baselines so the reconnect gets a full window before
        the watchdog could fire again (no close churn)."""
        ws = self.ws
        if ws is None:
            return
        self.last_message_ts = self._clock()
        self.last_delta_lag_seconds = None
        await ws.close()

    async def on_open(self) -> None:
        logger.info("Kalshi WS opened (tickers=%s, private=%s).", self.tickers, self.include_private)
        for channel in _PUBLIC_CHANNELS:
            await self._subscribe(channel)
        if self.include_private:
            for channel in _PRIVATE_CHANNELS:
                await self._subscribe(channel)

    def subscription_message(self, channel: str) -> dict:
        """The subscribe command for one channel across ALL of this client's tickers (pure)."""
        return {
            "id": self.message_id,
            "cmd": "subscribe",
            "params": {"channels": [channel], "market_tickers": list(self.tickers)},
        }

    async def _subscribe(self, channel: str) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(self.subscription_message(channel)))
        self.message_id += 1

    async def handler(self) -> None:
        """Consume frames until the connection closes (or a seq gap forces a resubscribe)."""
        assert self.ws is not None
        try:
            async for message in self.ws:
                self.on_message(message)
                if self._reconnect_requested:
                    logger.info("[WS] seq gap — closing WS to force resubscribe (fresh snapshot)")
                    await self.force_close()
                    return
        except websockets.ConnectionClosed as e:
            self.on_close(e.code, e.reason)
        except Exception as e:  # noqa: BLE001 - V2 behavior: log, don't crash the loop owner
            self.on_error(e)

    def on_message(self, message: str | bytes) -> None:
        """Parse the envelope, stamp watchdogs, tap the recorder, then market-keyed dispatch."""
        # Silence watchdog FIRST — every inbound frame is liveness, including dropped control types.
        self.last_message_ts = self._clock()
        data = json.loads(message)
        msg_type = data.get("type")
        payload = data.get("msg", {})

        # Lag (PRIMARY): only frames carrying a server ts move the gauge.
        self._update_lag(payload)

        # Seq-gap — ORDERBOOK frames only; only frames carrying BOTH sid and seq participate.
        if msg_type in _ORDERBOOK_TYPES:
            sid = data.get("sid")
            seq = data.get("seq")
            if sid is not None and seq is not None:
                self._check_seq(sid, seq)

        # Recorder tap: capture the parsed envelope BEFORE dispatch (replay parity). sid/seq are
        # NOT recorded — the recording stays {type, msg}, byte-identical to what replay rebuilds.
        if self.record is not None and msg_type not in _CONTROL_TYPES:
            self.record("kalshi_ws", {"type": msg_type, "msg": payload})

        if msg_type in _CONTROL_TYPES:
            return

        cb = self.callbacks
        handler = {
            "ticker": cb.on_ticker,
            "orderbook_snapshot": cb.on_orderbook_snapshot,
            "orderbook_delta": cb.on_orderbook_delta,
            "market_positions": cb.on_position,
            "fill": cb.on_fill,
            "trade": cb.on_trade,
        }.get(msg_type)
        if handler is None:
            return
        # Market-keyed dispatch: extract the ticker; fail closed (journaled, not dispatched) if
        # absent — an unattributed frame must never be applied to the wrong leg's book.
        market_ticker = payload.get("market_ticker") if isinstance(payload, dict) else None
        if not market_ticker:
            self.dropped_no_market += 1
            logger.warning(
                "[WS] %s frame missing market_ticker — recorded but NOT dispatched (fail closed)",
                msg_type,
            )
            return
        handler(market_ticker, payload)

    def _update_lag(self, payload: dict) -> None:
        """Update `last_delta_lag_seconds` from a frame's server ts, if it carries one.

        Prefers `ts_ms` (epoch ms). Falls back to `ts` (ISO-8601 string on orderbook deltas —
        verified 328,799/328,799 in the healthy 07-12 recording — or numeric epoch on some
        trade/ticker frames). A malformed/absent timestamp leaves the previous reading untouched
        (a single bad frame must not spuriously clear a real lag signal)."""
        if not isinstance(payload, dict):
            return
        server_ts = _parse_server_ts(payload)
        if server_ts is not None:
            self.last_delta_lag_seconds = self._clock() - server_ts

    def _check_seq(self, sid: object, seq: object) -> None:
        """Per-sid seq check on the orderbook channel (V2 WP-2 F1): only a FORWARD skip is a gap
        (log + request reconnect). Rewinds/duplicates are logged and ignored (cannot have skipped
        state); the counter resyncs to the server's value either way. The first frame on a sid
        initializes the counter (never a gap). A malformed seq is inert (log + skip, never raise)."""
        try:
            seq_i = int(seq)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("[WS] malformed seq (sid=%s, seq=%r) — ignoring", sid, seq)
            return
        expected = self._expected_seq.get(sid)
        if expected is not None:
            if seq_i > expected:
                logger.warning("[WS] seq gap (sid=%s, expected=%s, got=%s)", sid, expected, seq_i)
                self._reconnect_requested = True
            elif seq_i < expected:
                logger.warning(
                    "[WS] seq rewind/duplicate (sid=%s, expected=%s, got=%s) — not a gap",
                    sid,
                    expected,
                    seq_i,
                )
        self._expected_seq[sid] = seq_i + 1  # resync to the server's counter in every case

    def on_error(self, error: Exception) -> None:
        logger.error("Kalshi WS error: %s", error)

    def on_close(self, close_status_code: int | None, close_msg: str | None) -> None:
        logger.info("Kalshi WS closed (code=%s, msg=%s).", close_status_code, close_msg)


def _parse_server_ts(payload: dict) -> float | None:
    """Extract a payload's server timestamp as epoch seconds, or None (pure helper).

    `ts_ms` epoch-ms preferred; `ts` numeric epoch or ISO-8601 string ("...Z" -> UTC) fallback.
    """
    ts_ms = payload.get("ts_ms")
    if ts_ms is not None:
        try:
            return float(ts_ms) / 1000.0
        except (TypeError, ValueError):
            return None
    ts = payload.get("ts")
    if ts is None:
        return None
    try:
        return float(ts)  # numeric epoch (some trade/ticker frames)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None
