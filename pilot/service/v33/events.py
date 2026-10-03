"""events.py — the event vocabulary the pure V3.3 core consumes.

The V3.3 ladder core consumes EXACTLY the V3.2 event vocabulary (a rung fill is a ``Fill``, a roll
confirm is an ``OrderAmended``, a fallback is an ``OrderCancelled``, etc.), so this module re-exports
``service.v32.events`` unchanged (Phase L1, 2026-09-22). Kept as its own module so a future V3.3-only
event can be added here without touching the frozen V3.2 core.
"""

from __future__ import annotations

from dataclasses import dataclass

from service.v32.events import (  # noqa: F401
    STRIKE_SERIES_PREFIX,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderAmended,
    OrderCancelled,
    Trade,
    classify_ticker,
    parse_bucket_ticker,
    parse_strike_ticker,
)


@dataclass(frozen=True)
class V33Fill(Fill):
    """A V3.3-only private fill that ALSO carries the fill's own ``market_ticker`` (D1, 2026-09-30
    incident). ``isinstance(v33_fill, Fill)`` is True, so ``decide_v33``'s Fill dispatch is unchanged; the
    core reads the ticker with ``getattr(event, "market_ticker", None)`` as the LAST-RESORT bucket
    attribution (invert ``st.bucket_tickers``) when neither the live order nor the retained ``cancel_ctx``
    record names the bucket. A plain V3.2 ``Fill`` (no ticker) simply yields None there."""

    market_ticker: str | None = None
    # GATE A (2026-10-03): which channel reported the fill. ``"poll"`` marks a DELTA the driver derived from
    # the venue's CUMULATIVE count over what the core had booked (``on_poll_fill``); every other source
    # (``"ws"``, the dry sim, None) is a per-trade INCREMENT. The core's off-ladder (orphan) dedupe sums only
    # increments, so a poll catch-up and a late ws echo of the same lots are never hedged twice.
    source: str | None = None


__all__ = [
    "STRIKE_SERIES_PREFIX",
    "BookUpdate",
    "ClockTick",
    "Fill",
    "V33Fill",
    "OrderAck",
    "OrderAmended",
    "OrderCancelled",
    "Trade",
    "classify_ticker",
    "parse_bucket_ticker",
    "parse_strike_ticker",
]
