"""Complement identity (NO ask = 1 - yes_bid) and cost assembly (commission section 5)."""
from decimal import Decimal

from census import cost_C, fee, leg_prices


def _candle(ya_close, ya_high, yb_close, yb_low):
    return {
        "yes_ask": {"close_dollars": ya_close, "high_dollars": ya_high,
                    "low_dollars": ya_close},
        "yes_bid": {"close_dollars": yb_close, "high_dollars": yb_close,
                    "low_dollars": yb_low},
    }


def test_complement_identity_base_and_worst():
    h = _candle("0.30", "0.40", "0.99", "0.99")   # H leg (YES ask)
    l = _candle("0.99", "0.99", "0.55", "0.40")   # L leg (NO ask = 1 - yes_bid)
    ya_b, ya_w, na_b, na_w = leg_prices(h, l)
    assert ya_b == Decimal("0.30")
    assert ya_w == Decimal("0.40")
    # NO ask (base) = 1 - yes_bid.close ; NO ask (worst) = 1 - yes_bid.low
    assert na_b == Decimal("1") - Decimal("0.55")   # 0.45
    assert na_w == Decimal("1") - Decimal("0.40")   # 0.60


def test_no_ask_worst_can_reach_one_when_bid_zero():
    l = _candle("0.99", "0.99", "0.10", "0.00")
    h = _candle("0.30", "0.40", "0.99", "0.99")
    _, _, na_b, na_w = leg_prices(h, l)
    assert na_b == Decimal("0.90")
    assert na_w == Decimal("1.00")   # fidelity-bounded worst; A2.5 keeps inclusion on BASE


def test_cost_C_uses_own_price_fee_each_leg():
    ya = Decimal("0.30"); na = Decimal("0.45")
    expected = ya + fee(ya) + na + fee(na)
    assert cost_C(ya, na) == expected


def test_leg_prices_none_when_candle_absent():
    ya_b, ya_w, na_b, na_w = leg_prices(None, None)
    assert (ya_b, ya_w, na_b, na_w) == (None, None, None, None)
