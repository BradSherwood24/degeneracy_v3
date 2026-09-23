"""events.py — the event vocabulary the pure V3.3 core consumes.

The V3.3 ladder core consumes EXACTLY the V3.2 event vocabulary (a rung fill is a ``Fill``, a roll
confirm is an ``OrderAmended``, a fallback is an ``OrderCancelled``, etc.), so this module re-exports
``service.v32.events`` unchanged (Phase L1, 2026-09-22). Kept as its own module so a future V3.3-only
event can be added here without touching the frozen V3.2 core.
"""

from __future__ import annotations

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

__all__ = [
    "STRIKE_SERIES_PREFIX",
    "BookUpdate",
    "ClockTick",
    "Fill",
    "OrderAck",
    "OrderAmended",
    "OrderCancelled",
    "Trade",
    "classify_ticker",
    "parse_bucket_ticker",
    "parse_strike_ticker",
]
