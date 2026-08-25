"""Tests for service/orders/envelope.py — the wire-envelope builder + response parser."""

from __future__ import annotations

from decimal import Decimal

from service.ledger import IntentLeg
from service.orders.envelope import (
    BATCH_CREATE_PATH,
    SINGLE_CREATE_PATH,
    build_batch,
    build_entry,
    new_client_order_id,
    parse_batch_response,
    parse_single_response,
    wire_price,
)


def leg(ticker, side, action, count, price, cid="c1", reduce_only=None):
    return IntentLeg(
        ticker=ticker, side=side, action=action, count=count,
        limit_price=Decimal(str(price)), client_order_id=cid, reduce_only=reduce_only,
    )


def test_paths_are_the_live_events_create_paths():
    # LIVE V2 endpoints (docs.kalshi.com 2026-08-21; prod-proven in V2 rest.py) — the proxy caps
    # + routes these after the Phase-3 review widened _ORDER_CREATE_PATHS to cover them.
    assert SINGLE_CREATE_PATH == "/trade-api/v2/portfolio/events/orders"
    assert BATCH_CREATE_PATH == "/trade-api/v2/portfolio/events/orders/batched"


def test_buy_yes_entry_shape():
    e = build_entry(leg("KXBTCD-X", "yes", "buy", 1, "0.46", cid="cid-yes"))
    assert e["ticker"] == "KXBTCD-X"
    assert e["side"] == "bid"                      # buy YES -> bid (via translate)
    assert e["price"] == "0.4600"
    assert e["count"] == "1.00"                    # fixed-point STRING
    assert e["time_in_force"] == "immediate_or_cancel"
    assert e["self_trade_prevention_type"] == "taker_at_cross"
    assert e["client_order_id"] == "cid-yes"
    assert "reduce_only" not in e


def test_buy_no_entry_price_is_one_minus_no_ask():
    # buy NO at no_ask 0.55 -> YES ask (sell YES) at 1-0.55 = 0.45
    e = build_entry(leg("KXBTCD-Y", "no", "buy", 1, "0.55"))
    assert e["side"] == "ask"
    assert e["price"] == "0.4500"


def test_sell_down_directions():
    # sell YES (held YES) at bid 0.40 -> ask @ 0.40
    e = build_entry(leg("T", "yes", "sell", 1, "0.40", reduce_only=True))
    assert e["side"] == "ask" and e["price"] == "0.4000"
    assert e["reduce_only"] is True
    # sell NO (held NO) at no_bid 0.30 -> bid @ 1-0.30 = 0.70
    e2 = build_entry(leg("T", "no", "sell", 1, "0.30"))
    assert e2["side"] == "bid" and e2["price"] == "0.7000"


def test_wire_price_preserves_deci_cent_precision():
    # 15-minute ladder deci-cent (0.001) survives to 4dp — translate's int-cent would round to 0.
    assert wire_price("yes", Decimal("0.001")) == "0.0010"
    assert wire_price("no", Decimal("0.001")) == "0.9990"
    e = build_entry(leg("KXBTC15M-Z", "yes", "buy", 1, "0.0010"))
    assert e["price"] == "0.0010"


def test_wire_price_matches_translate_on_whole_cents():
    # for whole-cent inputs the override equals translate's price (byte-identical)
    for side, p, want in [("yes", "0.46", "0.4600"), ("no", "0.55", "0.4500"),
                          ("yes", "0.83", "0.8300"), ("no", "0.82", "0.1800")]:
        assert wire_price(side, Decimal(p)) == want


def test_build_batch_shape():
    e1 = build_entry(leg("A", "no", "buy", 1, "0.57", cid="a"))
    e2 = build_entry(leg("B", "yes", "buy", 1, "0.24", cid="b"))
    body = build_batch([e1, e2])
    assert list(body.keys()) == ["orders"]
    assert body["orders"] == [e1, e2]


def test_new_client_order_id_is_unique_uuid():
    a, b = new_client_order_id(), new_client_order_id()
    assert a != b and len(a) == 36


def test_parse_single_response_wrapped_and_bare():
    wrapped = {"order": {"order_id": "o1", "client_order_id": "c1", "fill_count": "1",
                          "remaining_count": "0", "average_fill_price": "0.46",
                          "average_fee_paid": "0.01", "ts_ms": 123}}
    r = parse_single_response(wrapped)
    assert r.order_id == "o1" and r.fill_count == Decimal(1) and r.filled
    assert r.average_fill_price == Decimal("0.46") and r.average_fee_paid == Decimal("0.01")
    bare = parse_single_response({"order_id": "o2", "client_order_id": "c2", "fill_count": "0",
                                  "remaining_count": "1"})
    assert bare.order_id == "o2" and not bare.filled


def test_parse_batch_response_per_entry_error_is_no_fill():
    body = {"orders": [
        {"order_id": "o1", "client_order_id": "a", "fill_count": "1", "remaining_count": "0",
         "average_fill_price": "0.57", "average_fee_paid": "0.01", "ts_ms": 1},
        {"client_order_id": "b", "error": {"code": "insufficient_balance"}},
    ]}
    rs = parse_batch_response(body)
    assert len(rs) == 2
    assert rs[0].filled and rs[0].client_order_id == "a"
    assert rs[1].no_fill and rs[1].error and "insufficient_balance" in rs[1].error


def test_parse_batch_response_malformed_body_fails_closed():
    assert parse_batch_response({"nope": 1})[0].no_fill
    assert parse_batch_response("garbage")[0].no_fill
