"""Tests for service/orders/translate.py — carried VERBATIM from V2's test_order_translate.py.

This is the ONLY automated guard on the buy/sell direction mapping: sim mode short-circuits before
any order POST, so a sign-inverted exit (sell submitted as a position-DOUBLING buy) is invisible to
every replay/sim test. The 2026-06-20 BD live trial surfaced exactly that bug.
"""

from __future__ import annotations

import pytest

from service.orders.translate import to_v2_order


def legacy(side: str, price: int, action: str | None = None, count: int = 1) -> dict:
    """A legacy-shaped order dict: price goes to yes_price/no_price per `side`."""
    d: dict = {"ticker": "T", "side": side, "count": count}
    if action is not None:
        d["action"] = action
    d["yes_price" if side == "yes" else "no_price"] = price
    return d


def check(order: dict, want_side: str, want_price: str) -> None:
    v2 = to_v2_order(order)
    assert v2["side"] == want_side, f"{order} -> side {v2['side']!r}, want {want_side!r}"
    assert v2["price"] == want_price, f"{order} -> price {v2['price']!r}, want {want_price!r}"


def test_buy_entry_shotgun_mapping() -> None:
    """The pre-existing, must-not-regress BUY mapping."""
    check(legacy("yes", 46, "buy"), "bid", "0.4600")
    check(legacy("no", 45, "buy"), "ask", "0.5500")
    # action omitted defaults to buy (older callers may omit it)
    check(legacy("yes", 46), "bid", "0.4600")


def test_sell_exit_mapping() -> None:
    """The exit / take-profit / close path — the one that was broken/inverted in v1."""
    check(legacy("yes", 83, "sell"), "ask", "0.8300")
    check(legacy("no", 82, "sell"), "bid", "0.1800")
    check(legacy("yes", 1, "sell"), "ask", "0.0100")
    check(legacy("no", 1, "sell"), "bid", "0.9900")


def test_inversion_guard_explicit() -> None:
    """A sell must NEVER map to the buy-side book side — that doubles the position."""
    assert to_v2_order(legacy("yes", 83, "sell"))["side"] != "bid", (
        "REGRESSION: YES take-profit submitted as a BUY"
    )
    assert to_v2_order(legacy("no", 82, "sell"))["side"] != "ask", (
        "REGRESSION: NO take-profit submitted as a BUY"
    )


def test_passthrough_fields() -> None:
    v2 = to_v2_order(
        {
            "ticker": "T",
            "side": "yes",
            "action": "sell",
            "yes_price": 83,
            "count": 2,
            "time_in_force": "good_till_canceled",
            "post_only": True,
            "reduce_only": True,
            "client_order_id": "abc",
        }
    )
    assert v2["count"] == "2.00"
    assert v2["time_in_force"] == "good_till_canceled"
    assert v2["post_only"] is True
    assert v2["reduce_only"] is True
    assert v2["client_order_id"] == "abc"
    assert v2["self_trade_prevention_type"] == "taker_at_cross"


@pytest.mark.parametrize(
    "bad",
    [
        {"ticker": "T", "side": "maybe", "yes_price": 1, "count": 1},
        {"ticker": "T", "side": "yes", "action": "hold", "yes_price": 1, "count": 1},
    ],
)
def test_bad_input_rejects_loudly(bad: dict) -> None:
    with pytest.raises(ValueError):
        to_v2_order(bad)
