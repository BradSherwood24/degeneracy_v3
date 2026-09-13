"""V3.2 trade/fill/delta parsers pinned to REAL live WS frames (R1).

The fixtures in ``fixtures/v32/live_frames/`` were captured from a real (non-holdout) 2026-09-01
pilot journal. They exist because the Phase-2 spine's trade/fill parsers were first written against a
cents-shaped payload the live exchange does NOT send: the live frames carry DOLLAR strings
(``yes_price_dollars`` / ``no_price_dollars``), ``count_fp``, and the YES-space-units trap on a NO
fill (``side: "yes"`` with a NO purchase). These tests pin the parsers to the real bytes so a
regression cannot silently drop every trade (which would kill the shadow) or misread a NO fill's
price. FAKES ONLY — no network, no proxy, no holdout date read (the frame is 2026-09-01)."""

from __future__ import annotations

import json
import os
from decimal import Decimal

from service.book import BookMirror
from service.record_range import StreamJournal
from service.v32 import V32State, load_v32_params
from service.ws_client import _parse_server_ts
import service.run_v32 as R

_FRAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v32", "live_frames")


def _frame(name: str) -> dict:
    with open(os.path.join(_FRAMES, name), "r", encoding="utf-8") as f:
        return json.load(f)["obj"]["msg"]


# ---------------------------------------------------------------------------
# Trade parser (R1: taker_side from taker_side, price from yes_price_dollars as Decimal, count from
# count_fp, server ts from ts_ms)
# ---------------------------------------------------------------------------
def test_trade_parser_pinned_to_live_frame():
    msg = _frame("trade_frame.json")
    server_ts = _parse_server_ts(msg)
    assert server_ts == 1788255917202 / 1000.0  # from ts_ms, not the coarse `ts` seconds
    ev = R._trade_event(msg["market_ticker"], msg, server_ts)
    assert ev is not None, "the real trade frame must parse (cents-only parser dropped every trade)"
    assert ev.taker_side == "no"
    assert ev.yes_price == Decimal("0.57")   # yes_price_dollars, DOLLARS (never /100)
    assert ev.count == Decimal("10.00")
    assert ev.server_ts == server_ts


def test_trade_parser_prefers_no_price_dollars_when_yes_absent():
    msg = dict(_frame("trade_frame.json"))
    msg.pop("yes_price_dollars")
    ev = R._trade_event(msg["market_ticker"], msg, 1.0)
    assert ev is not None and ev.yes_price == Decimal("1") - Decimal("0.43")  # 1 - no_price_dollars


# ---------------------------------------------------------------------------
# Fill parser (R1: NO fill's yes_price_dollars -> NO-space price 1-yes when purchased/outcome no;
# carry count_fp, order_id, client_order_id, is_taker, fee_cost)
# ---------------------------------------------------------------------------
def test_fill_parser_pinned_to_live_frame():
    msg = _frame("fill_frame.json")
    pf = R._fill_event(msg)
    assert pf is not None
    # the YES-space trap: side=="yes" but purchased_side=="no" -> NO-space price = 1 - 0.04
    assert pf["purchased_side"] == "no"
    assert pf["yes_price"] == Decimal("0.0400")
    assert pf["price"] == Decimal("1") - Decimal("0.0400")   # == 0.96, the NO-space price paid
    assert pf["count"] == 1                                   # from count_fp "1.00"
    assert pf["order_id"] == "01a05a71-c2c0-7610-98a2-0562d07501e0"
    assert pf["client_order_id"] == "dcf3f16a-32fd-4622-8a84-9683dafb6f2c"
    assert pf["is_taker"] is True
    assert pf["fee_cost"] == Decimal("0.002700")
    assert _parse_server_ts(msg) == 1788223800645 / 1000.0    # ts_ms


def test_fill_parser_yes_purchase_keeps_yes_price():
    msg = dict(_frame("fill_frame.json"))
    msg["purchased_side"] = "yes"
    msg["outcome_side"] = "yes"
    pf = R._fill_event(msg)
    assert pf["price"] == Decimal("0.0400")   # a YES purchase pays the yes price directly


def test_fill_parser_no_coid_is_unparseable():
    msg = dict(_frame("fill_frame.json"))
    msg.pop("client_order_id")
    assert R._fill_event(msg) is None


# ---------------------------------------------------------------------------
# Delta parser (R1: the real orderbook_delta shape folds into the BookMirror)
# ---------------------------------------------------------------------------
def test_delta_folds_into_bookmirror():
    msg = _frame("delta_frame.json")
    assert _parse_server_ts(msg) == 1788255917151 / 1000.0   # ts_ms preferred over the ISO `ts`
    b = BookMirror()
    b.apply_snapshot({})            # empty snapshot clears suspect
    b.apply_delta(msg)              # side "no", price_dollars 0.38, delta_fp 50
    assert b.depth_at("no", Decimal("0.38")) == Decimal("50")
    top = b.top_of_book()
    assert top.no_bid == Decimal("0.38") and not top.suspect


# ---------------------------------------------------------------------------
# on_fill wired to the real frame: a tracked coid books the rest fill (count from count_fp, executed
# price journaled); an unplaced coid is a foreign fill (dropped + journaled)
# ---------------------------------------------------------------------------
CLOSE = "2026-09-13T20:00:00Z"
CTS = 1789156800
B_SD = "KXBTC-26SEP1316-B68200"
BUCKET_MAP = {B_SD: (68200.0, 68299.99)}


def _driver(tmp_path):
    p = load_v32_params()
    j = StreamJournal(os.path.join(tmp_path, "w.jsonl"), flush_every=1)
    j.open()
    state = V32State.new(CLOSE, CTS, BUCKET_MAP, p, shakedown=True)
    execu = R.FrozenExecutor(BUCKET_MAP)
    drv = R.V32Driver(p, state, j, execu, clock=lambda: 0.0)
    return drv, j


def test_on_fill_real_frame_foreign_when_unplaced(tmp_path):
    drv, j = _driver(tmp_path)
    msg = _frame("fill_frame.json")   # its coid was never placed in THIS window's RestBook
    drv.on_fill(B_SD, msg, 1788223800.645)
    assert drv.counts["foreign_fill_ignored"] == 1
    assert drv.state.rest_fill is None
    j.close()


def test_on_fill_real_frame_tracked_books_rest_fill(tmp_path):
    drv, j = _driver(tmp_path)
    msg = _frame("fill_frame.json")
    coid = msg["client_order_id"]
    # drive a real PLACE so the RestBook + a live core slot exist, then rebind them to the frame's
    # coid so the captured live fill attributes to us:
    from service.book import TopOfBook

    def top(**kw):
        return TopOfBook(yes_bid=kw.get("yb"), yes_bid_size=None, yes_ask=kw.get("ya"),
                         yes_ask_size=None, no_bid=kw.get("nb"), no_bid_size=None,
                         no_ask=kw.get("na"), no_ask_size=None, suspect=False)

    now = CTS - 600
    drv.on_book_update(B_SD, top(yb=Decimal("0.40"), ya=Decimal("0.40")), now)
    drv.on_book_update("KXBTCD-26SEP1316-T68299.99", top(na=Decimal("0.20")), now)
    drv.on_book_update("KXBTCD-26SEP1316-T68199.99", top(ya=Decimal("0.30")), now)
    assert drv.state.rest_live is not None
    # rebind the RestBook + core slot to the FRAME's coid so the real fill attributes to us
    placed_coid = drv.state.rest_live.client_order_id
    rec = drv.executor.rest_book.pop(placed_coid)
    rec.client_order_id = coid
    drv.executor.rest_book[coid] = rec
    drv.executor._by_order_id[rec.order_id] = coid
    from dataclasses import replace as dc_replace
    drv.state = dc_replace(drv.state, rest_live=dc_replace(drv.state.rest_live, client_order_id=coid))

    drv.on_fill(B_SD, msg, 1788223800.645)
    assert drv.counts["rest_fill"] == 1
    assert drv.state.rest_fill is not None
    assert drv.state.rest_fill.count == 1              # from count_fp
    assert drv.state.rest_fill.price == rec.price      # booked at the resting price (post_only maker)
    j.close()
    with __import__("service.journal_io", fromlist=["open_journal"]).open_journal(
        os.path.join(tmp_path, "w.jsonl")
    ) as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
    rf = [r for r in recs if r["kind"] == "rest_fill"]
    assert rf and rf[0]["obj"]["exec_price"] == "0.9600"  # NO-space executed price journaled
