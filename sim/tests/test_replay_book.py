"""Replayer book tests: integer folding (incl. dust), derived asks, depth, and agreement
with the authoritative Decimal ``pilot/service/book.py`` BookMirror on synthetic + real frames.
"""

import os
import sys

_SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SIM_DIR)
for p in (_SIM_DIR, _REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from replay.book import IntBook, WindowBook, dollars_to_mils   # noqa: E402


def _snap(yes, no):
    return {"yes_dollars_fp": yes, "no_dollars_fp": no}


def test_fold_and_derived_asks():
    b = IntBook()
    b.apply_snapshot(_snap([["0.54", "3.00"], ["0.53", "10.00"]],
                           [["0.45", "2.00"], ["0.44", "8.00"]]))
    assert b.best_yes_bid() == (540, 300)
    assert b.best_no_bid() == (450, 200)
    # NO ask = 1 - best yes bid = 1 - 0.54 = 0.46 ; size = that yes bid's size (3.00)
    assert b.no_ask() == (460, 300)
    # YES ask = 1 - best no bid = 1 - 0.45 = 0.55 ; size = 2.00
    assert b.yes_ask() == (550, 200)
    assert b.depth_at("yes", 540) == 300
    assert b.depth_at("no", 450) == 200
    assert b.depth_at("no", 999) == 0


def test_delta_add_remove_and_new_best():
    b = IntBook()
    b.apply_snapshot(_snap([["0.54", "3.00"]], [["0.45", "2.00"]]))
    # sweep the best yes bid to zero -> yes book empty -> no_ask undefined
    b.apply_delta({"side": "yes", "price_dollars": "0.54", "delta_fp": "-3.00"})
    assert b.best_yes_bid() is None
    assert b.no_ask() is None
    # add a new yes level
    b.apply_delta({"side": "yes", "price_dollars": "0.50", "delta_fp": "5.00"})
    assert b.best_yes_bid() == (500, 500)
    assert b.no_ask() == (500, 500)   # 1 - 0.50


def test_dust_is_zero():
    b = IntBook()
    b.apply_snapshot(_snap([["0.54", "3.00"]], [["0.45", "2.00"]]))
    # bring the level to 0.004 contracts (< 0.005 dust) -> treated as swept (removed)
    b.apply_delta({"side": "yes", "price_dollars": "0.54", "delta_fp": "-2.996"})
    assert b.best_yes_bid() is None            # 0.004c is dust -> gone, not a resurrected level
    # a 0.01c level survives (above the 0.005 dust floor)
    b.apply_delta({"side": "yes", "price_dollars": "0.60", "delta_fp": "0.01"})
    assert b.best_yes_bid() == (600, 1)


def test_snapshot_drops_zero_and_negative():
    b = IntBook()
    b.apply_snapshot(_snap([["0.54", "3.00"], ["0.53", "0.00"]],
                           [["0.45", "2.00"], ["0.40", "0.001"]]))
    assert 530 not in b.yes_bids           # zero-size placeholder dropped
    assert 400 not in b.no_bids            # 0.001c is dust -> dropped


def test_windowbook_routing_and_ready():
    wb = WindowBook(("H", "L"))
    assert wb.ready() is False
    wb.feed({"type": "orderbook_snapshot",
             "msg": {"market_ticker": "H", "yes_dollars_fp": [["0.54", "3.00"]],
                     "no_dollars_fp": [["0.45", "2.00"]]}})
    assert wb.ready() is False             # only H snapshotted
    wb.feed({"type": "orderbook_snapshot",
             "msg": {"market_ticker": "L", "yes_dollars_fp": [["0.48", "1.00"]],
                     "no_dollars_fp": [["0.50", "1.00"]]}})
    assert wb.ready() is True
    assert wb.top_of_book("H").no_ask == 460
    assert wb.size_at("H", "no", "0.45") == 200
    # unknown ticker ignored
    assert wb.feed({"type": "orderbook_delta",
                    "msg": {"market_ticker": "ZZZ", "side": "yes",
                            "price_dollars": "0.5", "delta_fp": "1"}}) is None


def test_agreement_with_bookmirror_synthetic():
    """IntBook top-of-book must equal the Decimal BookMirror on the same event stream."""
    pilot = os.path.join(_REPO, "pilot")
    if pilot not in sys.path:
        sys.path.insert(0, pilot)
    from service.book import BookMirror   # pilot's authoritative Decimal book

    snap = _snap([["0.5400", "3.00"], ["0.5300", "10.00"], ["0.5000", "0.01"]],
                 [["0.4500", "2.00"], ["0.4400", "8.00"]])
    deltas = [
        {"side": "yes", "price_dollars": "0.5400", "delta_fp": "-3.00"},   # sweep best
        {"side": "no", "price_dollars": "0.4600", "delta_fp": "4.00"},     # new best no
        {"side": "yes", "price_dollars": "0.5200", "delta_fp": "7.00"},
        {"side": "no", "price_dollars": "0.4500", "delta_fp": "-2.00"},    # remove
    ]
    ib = IntBook(); ib.apply_snapshot(snap)
    bm = BookMirror(); bm.apply_snapshot(snap)
    for d in deltas:
        ib.apply_delta(d)
        bm.apply_delta(d)
        top = bm.top_of_book()
        iyb = ib.best_yes_bid(); inb = ib.best_no_bid()
        # compare best bids
        assert (top.yes_bid is None) == (iyb is None)
        if iyb is not None:
            assert dollars_to_mils(top.yes_bid) == iyb[0]
            assert int(round(float(top.yes_bid_size) * 100)) == iyb[1]
        assert (top.no_bid is None) == (inb is None)
        if inb is not None:
            assert dollars_to_mils(top.no_bid) == inb[0]
        # compare derived asks
        iya = ib.yes_ask(); ina = ib.no_ask()
        assert (top.yes_ask is None) == (iya is None)
        if iya is not None:
            assert dollars_to_mils(top.yes_ask) == iya[0]
        assert (top.no_ask is None) == (ina is None)
        if ina is not None:
            assert dollars_to_mils(top.no_ask) == ina[0]
