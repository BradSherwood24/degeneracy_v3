"""envelope.py — the NEW V2 order-wire envelope builder + response parser (synchronous fill truth).

The direction mapping (bid/ask, and the YES-cents price level) is delegated to the VERBATIM
``translate.to_v2_order`` port; this module wraps it into the exact create-order wire shape the
proxy forwards and the exchange accepts, and parses the per-entry response.

Wire shape (verified against PLAN "API facts" + the proxy parser):
  single create -> POST {SINGLE_CREATE_PATH}   body = one entry object
  batch  create -> POST {BATCH_CREATE_PATH}    body = {"orders": [entry, entry]}

  entry = {
    ticker, side (bid/ask via translate), count (fixed-point STRING e.g. "1.00"),
    price (fixed-point DOLLAR STRING e.g. "0.4500"), time_in_force="immediate_or_cancel",
    self_trade_prevention_type="taker_at_cross", client_order_id=uuid4, [reduce_only]
  }

PATH DETERMINATION (FIXED in Phase-3 review 2026-08-21): the LIVE V2 order-create endpoints are
``/trade-api/v2/portfolio/events/orders`` (single) and ``/trade-api/v2/portfolio/events/orders/batched``
(batch) — verified against docs.kalshi.com (2026-08-21) and prod-proven in
``degeneracy_v2/kalshi/rest.py`` (the /events/orders path has been correct since the 2026-06-04
migration; the 2026-07-11 incident was the HOST, not the path). The proxy's ``_ORDER_CREATE_PATHS``
was widened in the same review to cap+route BOTH the live events paths and the legacy
``/portfolio/orders`` paths, so the envelope now targets the live events paths and the proxy routes
them to the orders host and applies the contract/ticker/budget caps. The prior build targeted the
legacy paths (a mirage: through the real API they would 404 on the market-data host and bypass every
proxy cap); that divergence is resolved on both sides.

Price precision: ``translate`` rounds the price to whole cents (``int()`` on the cents field). The
pilot's 15-minute ladder can quote in deci-cents (Decimal("0.001")), so we OVERRIDE the wire price
with a full-precision 4-dp Decimal computed from the observed limit — using the SAME YES-cents
convention translate encodes (side 'yes' -> price as-is; side 'no' -> 1 - price), which is invariant
to buy/sell. For a whole-cent input this is byte-identical to translate's price (tested).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from service.orders.translate import to_v2_order

# --- live V2 CREATE paths (see module docstring: events/orders, prod-proven + proxy-capped) ---
SINGLE_CREATE_PATH = "/trade-api/v2/portfolio/events/orders"
BATCH_CREATE_PATH = "/trade-api/v2/portfolio/events/orders/batched"

# --- pilot wire constants (commission: IOC at max price; STP required) ---
DEFAULT_TIF = "immediate_or_cancel"
DEFAULT_STP = "taker_at_cross"

_PRICE_Q = Decimal("0.0001")  # Kalshi dollar fixed-point is 4 dp (matches V2's "{:.4f}")
_ONE = Decimal(1)


def new_client_order_id() -> str:
    """A fresh uuid4 idempotency key (one per leg intent)."""
    return str(uuid.uuid4())


def _cents(price: Decimal) -> int:
    """Whole-cent integer for the translate direction mapping (price is overridden afterwards)."""
    return int((Decimal(price) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def wire_price(side: str, limit_price: Decimal) -> str:
    """Full-precision 4-dp YES-cents-level wire price string.

    YES-perspective wire price is invariant to buy/sell (only bid/ask flips): side 'yes' -> the
    price itself; side 'no' -> 1 - price (a NO order at p == a YES order at 1-p). Matches translate's
    price for whole-cent inputs but preserves deci-cent precision.
    """
    p = Decimal(limit_price)
    if side == "no":
        p = _ONE - p
    elif side != "yes":
        raise ValueError(f"wire_price: unexpected side={side!r}")
    return str(p.quantize(_PRICE_Q))


def build_entry(leg: Any, *, tif: str = DEFAULT_TIF, stp: str = DEFAULT_STP) -> dict[str, Any]:
    """Build one create-order wire entry from a leg-like object.

    ``leg`` must expose: ``ticker`` (str), ``side`` ('yes'/'no' outcome bought/sold), ``action``
    ('buy'/'sell'), ``count`` (int), ``limit_price`` (Decimal dollars), ``client_order_id`` (str),
    and optionally ``reduce_only`` (bool). Direction/side come from the verbatim translate; the price
    is overridden with the full-precision Decimal (see module docstring).
    """
    side = leg.side
    legacy: dict[str, Any] = {
        "ticker": leg.ticker,
        "side": side,
        "action": leg.action,
        "count": int(leg.count),
        "time_in_force": tif,
        "self_trade_prevention_type": stp,
        "client_order_id": leg.client_order_id,
    }
    cents = _cents(leg.limit_price)
    legacy["yes_price" if side == "yes" else "no_price"] = cents
    reduce_only = getattr(leg, "reduce_only", None)
    if reduce_only is not None:
        legacy["reduce_only"] = bool(reduce_only)
    entry = to_v2_order(legacy)
    # Preserve full 4-dp precision (translate rounds price to whole cents via int()).
    entry["price"] = wire_price(side, leg.limit_price)
    return entry


def build_batch(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The batch-create body: {"orders": [entry, ...]} (the proxy parser reads this exact shape)."""
    return {"orders": list(entries)}


# ---------------------------------------------------------------------------
# Response parsing — the batch/single response is SYNCHRONOUS FILL TRUTH.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderResponse:
    """Parsed per-entry order result. ``no_fill`` distinguishes a zero-fill / error / transport
    failure from a real (possibly partial) fill; ``error`` carries the classifier string.

    UNITS (Phase-3a units repair, 2026-08-26): ``average_fill_price`` is in the ORDER'S side-space
    — directly comparable to the leg's ``limit_price`` — for EVERY consumer (ledger, stops, parity,
    reports). Kalshi reports a NO-side order's price in YES-space (a NO fill at 0.97 comes back as
    0.0300 == 1 - 0.97), so the parse layer converts it exactly once via ``normalize_fill_to_side``
    (price_no = 1 - price_yes). ``raw_reported_price`` preserves the untouched venue value (the
    YES-space number for a NO order) so the journal keeps the raw truth and normalization stays
    idempotent (it always re-derives from ``raw_reported_price``). ``average_fee_paid`` is a dollar
    amount and is side-INDEPENDENT (venue-verified against Kalshi's realized on the 2026-08-23 fills)
    — it is never flipped."""

    client_order_id: str | None
    order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None
    ts_ms: int | None
    raw_reported_price: Decimal | None = None
    error: str | None = None
    no_fill: bool = False

    @property
    def filled(self) -> bool:
        return self.fill_count > 0


def _dec(v: object) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001 - fail closed on an unparseable numeric
        return None
    return d if d.is_finite() else None


def _dec0(v: object) -> Decimal:
    d = _dec(v)
    return d if d is not None else Decimal(0)


def normalize_fill_to_side(resp: OrderResponse, side: str) -> OrderResponse:
    """THE units choke point: return ``resp`` with ``average_fill_price`` in ``side``'s space.

    Kalshi reports a NO-side order's price in YES-space (a NO fill at 0.97 -> 0.0300); this converts
    it to NO-space (price_no = 1 - price_yes) so every downstream consumer sees a price directly
    comparable to the leg's ``limit_price``. ``side`` is the order's outcome side ('yes'/'no').

    Idempotent: the normalized price is ALWAYS derived from ``raw_reported_price`` (the untouched
    venue value), so calling this twice — or on an already-normalized response — yields the same
    result. When ``raw_reported_price`` is unset (a freshly parsed slot), the current
    ``average_fill_price`` is treated as the raw venue value and captured as ``raw_reported_price``.
    A None price (no-fill / error) passes through untouched. Fees are side-independent — never
    flipped here."""
    raw = resp.raw_reported_price if resp.raw_reported_price is not None else resp.average_fill_price
    if raw is None:
        return resp
    if side == "no":
        norm = _ONE - raw
    elif side == "yes":
        norm = raw
    else:
        raise ValueError(f"normalize_fill_to_side: unexpected side={side!r}")
    return replace(resp, average_fill_price=norm, raw_reported_price=raw)


def no_fill_response(client_order_id: str | None, error: str) -> OrderResponse:
    """A synthetic zero-fill response (transport 429/5xx, refusal, or exception on the order path).

    The imbalance protocol treats this as a deficient/orphaned leg — never as a fill.
    """
    return OrderResponse(
        client_order_id=client_order_id,
        order_id=None,
        fill_count=Decimal(0),
        remaining_count=Decimal(0),
        average_fill_price=None,
        average_fee_paid=None,
        ts_ms=None,
        error=error,
        no_fill=True,
    )


def parse_entry_response(slot: dict[str, Any]) -> OrderResponse:
    """Parse ONE per-entry response slot into an OrderResponse.

    A slot carrying an ``error`` object/string is a no-fill (per-entry error in a non-atomic batch).
    """
    if not isinstance(slot, dict):
        return no_fill_response(None, f"malformed_response_slot:{type(slot).__name__}")
    # The venue price is captured RAW here (YES-space for a NO order). Side-normalization is applied
    # by the caller via ``normalize_fill_to_side`` once the leg's side is known (single: below;
    # batch: the executor's _align_batch, keyed by client_order_id). ``raw_reported_price`` mirrors
    # the parsed value so normalization is idempotent no matter how many times it runs.
    raw_price = _dec(slot.get("average_fill_price"))
    err = slot.get("error")
    if err:
        return OrderResponse(
            client_order_id=slot.get("client_order_id"),
            order_id=slot.get("order_id"),
            fill_count=_dec0(slot.get("fill_count")),
            remaining_count=_dec0(slot.get("remaining_count")),
            average_fill_price=raw_price,
            average_fee_paid=_dec(slot.get("average_fee_paid")),
            ts_ms=_int(slot.get("ts_ms")),
            raw_reported_price=raw_price,
            error=str(err),
            no_fill=True,
        )
    return OrderResponse(
        client_order_id=slot.get("client_order_id"),
        order_id=slot.get("order_id"),
        fill_count=_dec0(slot.get("fill_count")),
        remaining_count=_dec0(slot.get("remaining_count")),
        average_fill_price=raw_price,
        average_fee_paid=_dec(slot.get("average_fee_paid")),
        ts_ms=_int(slot.get("ts_ms")),
        raw_reported_price=raw_price,
        error=None,
        no_fill=False,
    )


def _int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_single_response(body: dict[str, Any], side: str | None = None) -> OrderResponse:
    """Parse a single-order create response. Kalshi wraps the order in {"order": {...}}; a bare
    object is also accepted (defensive).

    ``side`` is the submitted order's outcome side ('yes'/'no'); when given, the reported price is
    normalized into that side's space (the units choke point). Omitting it leaves the raw venue
    value (back-compat for callers that normalize later)."""
    if isinstance(body, dict) and isinstance(body.get("order"), dict):
        resp = parse_entry_response(body["order"])
    else:
        resp = parse_entry_response(body)
    return normalize_fill_to_side(resp, side) if side is not None else resp


def parse_batch_response(body: dict[str, Any]) -> list[OrderResponse]:
    """Parse a batch-create response body {"orders": [slot, ...]} into per-entry OrderResponses."""
    if not isinstance(body, dict):
        return [no_fill_response(None, f"malformed_batch_body:{type(body).__name__}")]
    orders = body.get("orders")
    if not isinstance(orders, list):
        return [no_fill_response(None, "batch_response_missing_orders_list")]
    return [parse_entry_response(slot) for slot in orders]
