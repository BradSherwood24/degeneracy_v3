"""Legacy -> V2 order translation — the direction-critical pure function.

PORTED VERBATIM from ``degeneracy_v2/kalshi/order_translate.py`` (itself a port of v1
``KalshiHttpClient._to_v2_order``, 2026-06-04 V2 migration, action-aware fix 2026-06-20). The body
of ``to_v2_order`` is UNCHANGED from V2 — only this docstring's location note is new. Its unit tests
are carried over verbatim in ``pilot/tests/test_orders_translate.py``.

This is the ONLY automated guard on the buy/sell direction mapping: sim mode short-circuits before
any order POST, so a sign-inverted exit (a sell submitted as a position-DOUBLING buy) is invisible to
every replay/sim test — the unit tests carried from v1 lock the mapping in place.

The V2 'side' is the BOOK side (bid = buy YES, ask = sell YES), so it depends on BOTH the
contract side AND the action. The YES-cents level is invariant to direction; only bid/ask flips
with buy vs sell:
  - buy  yes -> 'bid' @ yes_price          (buy YES)
  - buy  no  -> 'ask' @ (100 - no_price)   (sell YES == buy NO)
  - sell yes -> 'ask' @ yes_price          (sell YES — TP/close on a YES hold)
  - sell no  -> 'bid' @ (100 - no_price)   (buy YES == sell NO — TP/close on a NO hold)
`action` defaults to 'buy' when absent (preserves the entry-shotgun mapping).
"""

from __future__ import annotations

from typing import Any

_PASSTHROUGH_KEYS = (
    "reduce_only",
    "cancel_order_on_pause",
    "expiration_time",
    "subaccount",
    "order_group_id",
    "exchange_index",
)


def to_v2_order(legacy: dict[str, Any]) -> dict[str, Any]:
    """Translate a legacy-shaped order dict to V2 wire format (see module docstring).

    Field mapping: count -> '{:.2f}' fixed-point string; yes_price/no_price (int cents) ->
    price '{:.4f}' dollar string (a NO bid at N cents == a YES ask at 100-N cents);
    type='limit' dropped (V2 is limit-only); self_trade_prevention_type injected
    ('taker_at_cross' default); post_only / client_order_id / time_in_force passed through.
    """
    side_legacy = legacy.get("side")
    action = legacy.get("action", "buy")
    if action not in ("buy", "sell"):
        raise ValueError(f"to_v2_order: unexpected action={action!r}")
    # YES-cents level (direction-invariant) + the BUY-side book side.
    if side_legacy == "yes":
        buy_side = "bid"
        cents = int(legacy["yes_price"])
    elif side_legacy == "no":
        buy_side = "ask"
        # NO at no_price (cents) == YES at (100 - no_price) cents.
        cents = 100 - int(legacy["no_price"])
    else:
        raise ValueError(f"to_v2_order: unexpected side={side_legacy!r}")
    # Selling flips the book side; buying keeps it. (bid<->ask)
    side_v2 = ("ask" if buy_side == "bid" else "bid") if action == "sell" else buy_side

    v2: dict[str, Any] = {
        "ticker": legacy["ticker"],
        "side": side_v2,
        "count": f"{int(legacy['count']):.2f}",
        "price": f"{cents / 100:.4f}",
        "time_in_force": legacy.get("time_in_force", "good_till_canceled"),
        "self_trade_prevention_type": legacy.get("self_trade_prevention_type", "taker_at_cross"),
    }
    if "post_only" in legacy:
        v2["post_only"] = bool(legacy["post_only"])
    if legacy.get("client_order_id"):
        v2["client_order_id"] = legacy["client_order_id"]
    for k in _PASSTHROUGH_KEYS:
        if k in legacy:
            v2[k] = legacy[k]
    return v2
