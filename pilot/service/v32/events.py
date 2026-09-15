"""events.py — the event vocabulary the pure V3.2 core consumes, plus ticker classifiers.

Every event carries the ONLY notion of "now": a server-derived ``server_ts`` (epoch seconds). The
core reads time exclusively from these timestamps, so ``decide_v32`` runs live and in replay
bit-identically (no clock reads).

The core subscribes to BOTH the hourly strike ladder (KXBTCD, priced by ``parse_strike_ticker``) and
the range buckets (KXBTC, keyed by a static ``ticker -> (floor, cap)`` map supplied at discovery —
the core NEVER guesses bucket ticker syntax). ``BookUpdate.top`` reuses ``service.book.TopOfBook``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from service.book import TopOfBook

# The hourly-strike series. Strike tickers are ``KXBTCD-<gen>-T<strike-0.01>`` (e.g. T79599.99).
STRIKE_SERIES_PREFIX = "KXBTCD"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BookUpdate:
    """A top-of-book update for ONE market (a strike KXBTCD-... or a bucket KXBTC-...).

    ``server_ts`` is the EVALUATION clock (``now``) — the driver folds books onto a MONOTONE clock
    across the two interleaving WS connections (``run_v32.V32Driver``), so it never runs behind a
    folded book. ``book_ts`` is the FRAME'S OWN server ts, recorded as this market's book age anchor
    (``_fold_book``), so a genuinely stalled feed still ages that book out even while the monotone
    ``server_ts`` keeps advancing off the other connection. ``book_ts is None`` (the default, e.g. a
    unit test or the golden harness that constructs events directly) falls back to ``server_ts`` for
    the recorded book ts — the pre-2026-09-14 single-clock behavior."""

    market_ticker: str
    top: TopOfBook
    server_ts: float
    book_ts: float | None = None


@dataclass(frozen=True)
class Trade:
    """A public print on ONE market. ``taker_side`` in {"yes","no"}; ``yes_price`` is the print's
    YES-space price (Decimal dollars); ``count`` is the traded size."""

    market_ticker: str
    yes_price: Decimal
    taker_side: str
    count: Decimal
    server_ts: float


@dataclass(frozen=True)
class Fill:
    """A private fill on one of OUR orders (from the ``fill`` WS channel or an order-status poll)."""

    order_id: str | None
    client_order_id: str
    count: Decimal
    price: Decimal
    side: str
    server_ts: float


@dataclass(frozen=True)
class OrderAck:
    """The exchange acknowledged one of our creates: the order is now resting (live)."""

    client_order_id: str
    order_id: str
    server_ts: float


@dataclass(frozen=True)
class OrderCancelled:
    """The exchange confirmed one of our orders is cancelled. ``filled_count_before_cancel`` > 0
    means it partially filled before the cancel landed (treated as a fill by the core)."""

    order_id: str
    server_ts: float
    filled_count_before_cancel: Decimal = Decimal(0)


@dataclass(frozen=True)
class OrderAmended:
    """The exchange confirmed an AMEND of one of our resting orders (Kalshi Amend Order V2, the
    amend-first replace mechanic, Brad 2026-09-15). The ``order_id`` PERSISTS across the amend; a price
    change forfeits queue position exactly as cancel+create did. ``client_order_id`` is the UPDATED
    coid, ``price`` the new resting n (NO-space dollars). ``fill_count`` > 0 means the amend CROSSED and
    filled that many contracts at ``average_fill_price`` (a TAKER fill in NO-space; the core routes it
    into the wings like a fill-before-cancel). With count 1 a fill leaves ``remaining_count`` 0, so no
    remainder rests."""

    order_id: str
    client_order_id: str
    price: Decimal | None
    server_ts: float
    remaining_count: Decimal = Decimal(0)
    fill_count: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None


@dataclass(frozen=True)
class ClockTick:
    """A time-only event (no book change): server-derived ``server_ts`` (epoch seconds). Drives the
    window cutoffs (cancel the rest at T-quote_end_s) when no book frame is arriving."""

    server_ts: float


# ---------------------------------------------------------------------------
# Ticker classifiers (pure)
# ---------------------------------------------------------------------------
def parse_strike_ticker(ticker: str) -> int | None:
    """``"KXBTCD-26AUG3006-T77799.99"`` -> ``77800`` (the sim's ``strike_of``: round(strike)+0.01
    pattern). Returns None for a non-strike ticker or an unparseable suffix.

    The floor int returned is the KEY the core uses for strike books; Su = Sd + bucket_width shares
    the same key convention (a $100 bucket [Sd, Sd+100) pairs strike floors Sd and Sd+100).
    """
    if not ticker.startswith(STRIKE_SERIES_PREFIX + "-"):
        return None
    if "-T" not in ticker:
        return None
    try:
        return round(float(ticker.split("-T")[1]) + 0.01)
    except (ValueError, IndexError):
        return None


def parse_bucket_ticker(
    ticker: str, bucket_map: Mapping[str, tuple[float, float]]
) -> int | None:
    """The bucket-floor int for a range ticker, from the static discovery ``bucket_map`` (ticker ->
    (floor, cap)). Returns None if the ticker is not a known bucket. The core NEVER infers bucket
    ticker syntax — the floor/cap comes only from discovery (``record_range.discover_range_markets``).
    """
    fc = bucket_map.get(ticker)
    if fc is None:
        return None
    floor, _cap = fc
    return int(round(float(floor)))


def classify_ticker(
    ticker: str, bucket_map: Mapping[str, tuple[float, float]]
) -> tuple[str, int] | None:
    """Classify a market ticker -> ("strike", floor) | ("bucket", floor) | None. Buckets take
    precedence (a ticker present in the discovery map is a bucket); otherwise try the strike pattern.
    """
    fc = bucket_map.get(ticker)
    if fc is not None:
        return ("bucket", int(round(float(fc[0]))))
    k = parse_strike_ticker(ticker)
    if k is not None:
        return ("strike", k)
    return None
