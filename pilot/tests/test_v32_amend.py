"""V3.2 amend-first replace (Brad 2026-09-15, verbatim: "Use the cancel and recreate flow as a backup
if our post to ammend the order fails. Go ahead and build that"). FAKES ONLY — no network, no proxy
dialed, no key/holdout/sealed read.

A same-bucket requote AMENDS the resting order in place (Kalshi Amend Order V2: same order_id, a price
change forfeits queue position exactly as cancel+create did) instead of cancel -> confirm -> create. On
ANY non-2xx/timeout the executor FALLS BACK to the sharded cancel -> confirm -> create path. These tests
pin: the exact amend wire body + sharded path; a 2xx updates the RestBook (one order, old coid retained
for late fills); a 2xx that crosses books a TAKER rest fill (fee = average_fee_paid) and routes it to the
wings; a 404/500/timeout journals amend_failed and runs the sharded-cancel fallback (never two rests, the
create consults the pre-PLACE invariant); the FrozenExecutor refuses a real AMEND_REST and synth-amends
the WOULD_ twin; and the ledger row + report surface the amend counters."""

from __future__ import annotations

from collections import deque
from decimal import Decimal

import pytest

from service.proxy_writer import WriteResponse
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderAmended, OrderCancelled
from service.v32.executor import (
    LiveExecutor,
    OPEN_ORDERS_PATH,
    ORDER_STATUS_PATH_TMPL,
    amend_path,
    cancel_path,
)

CLOSE = "2026-09-15T14:00:00Z"
CTS = 1789394400
B = "KXBTC-26SEP1418-B78850"
BUCKET_MAP = {B: (78850.0, 78949.99)}
EXCH = {B: 2}


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]

    def of(self, kind):
        return [obj for k, obj in self.records if k == kind]


class _RecordingWriter:
    """Scriptable POST/DELETE/GET recorder (mirrors test_v32_cancel_shard)."""

    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.post_queue: deque = deque()
        self.post_fn = None
        self.delete_fn = None
        self.get_map: dict[str, object] = {}
        self._oid = 0

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if self.post_fn is not None:
            return self.post_fn(path, body)
        if self.post_queue:
            return self.post_queue.popleft()
        self._oid += 1
        oid = f"oid-{self._oid}"
        return WriteResponse(201, {"order": {"order_id": oid, "client_order_id":
                                             body.get("client_order_id"), "fill_count": "0.00",
                                             "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_fn is not None:
            return self.delete_fn(path)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


def _st():
    return V32State.new(CLOSE, CTS, BUCKET_MAP, load_v32_params())


def _exec(writer, journal=None):
    return LiveExecutor(writer, BUCKET_MAP, EXCH, journal or FakeJournal(), CTS, 300,
                        clock=lambda: 0.0, sleep=lambda _s: None)


def _place(coid="c1", n="0.54"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


def _amend(coid_old, coid_new, oid, n="0.50"):
    return V32Action(kind=ActionKind.AMEND_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), client_order_id=coid_old, updated_client_order_id=coid_new,
                     order_id=oid, expiration_epoch=CTS - 300)


# ===========================================================================
# 1. The exact amend wire body + sharded path
# ===========================================================================
def test_amend_wire_body_and_path_exact():
    w = _RecordingWriter()
    ex = _exec(w)
    ex.on_action(_place("c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.post_queue.append(WriteResponse(200, {"order_id": oid, "client_order_id": "c2",
                                            "fill_count": "0.00", "remaining_count": "1.00"}, True))
    ex.on_action(_amend("c1", "c2", oid, "0.50"), _st(), CTS - 590)
    path, body = w.posts[-1]
    assert path == amend_path(oid, 2) == f"/portfolio/events/orders/{oid}/amend?exchange_index=2"
    # side "ask" (a bucket-NO buy rests as a YES ask); price YES-space 1 - n = 0.5000; count "1.00";
    # both coids; exchange_index in the body too (the proxy signs the query-stripped path).
    assert body == {
        "ticker": B, "side": "ask", "price": "0.5000", "count": "1.00",
        "client_order_id": "c1", "updated_client_order_id": "c2", "exchange_index": 2,
    }


# ===========================================================================
# 2. amend 2xx (no cross) -> RestBook updated, exactly one order, old coid retained
# ===========================================================================
def test_amend_2xx_updates_rest_book_one_order():
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    ex.on_action(_place("c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.post_queue.append(WriteResponse(200, {"order_id": oid, "client_order_id": "c2",
                                            "fill_count": "0.00", "remaining_count": "1.00"}, True))
    events = ex.on_action(_amend("c1", "c2", oid, "0.50"), _st(), CTS - 590)
    assert len(events) == 1 and isinstance(events[0], OrderAmended)
    assert events[0].order_id == oid and events[0].client_order_id == "c2"
    assert events[0].price == Decimal("0.50") and events[0].fill_count == Decimal(0)
    assert ex.amends_attempted == 1 and ex.amends_confirmed == 1 and ex.amends_failed == 0
    # RestBook: new coid live at the new price, SAME order_id; old coid RETAINED as amended (F-1).
    assert ex.rest_book["c2"].order_id == oid and ex.rest_book["c2"].status == "live"
    assert ex.rest_book["c2"].price == Decimal("0.50")
    assert ex.rest_book["c1"].status == "amended"
    assert ex._by_order_id[oid] == "c2"
    assert "amend_rest" in j.kinds() and "amend_confirmed" in j.kinds()
    assert w.deletes == []   # an amend never cancels


# ===========================================================================
# 3. amend 2xx that CROSSES (fill_count 1) -> taker rest fill booked, routed to wings
# ===========================================================================
def test_amend_2xx_cross_books_taker_fill_and_routes():
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    ex.on_action(_place("c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    # the amend crossed: fill_count 1, venue avg_fill_price 0.4400 YES-space (-> NO-space 0.5600), fee 0.02
    w.post_queue.append(WriteResponse(200, {
        "order_id": oid, "client_order_id": "c2", "fill_count": "1.00", "remaining_count": "0.00",
        "average_fill_price": "0.4400", "average_fee_paid": "0.0200"}, True))
    events = ex.on_action(_amend("c1", "c2", oid, "0.50"), _st(), CTS - 590)
    assert ex.amends_confirmed == 1 and ex.fills_on_amend == 1
    am = events[0]
    assert isinstance(am, OrderAmended) and am.fill_count == Decimal(1)
    assert am.average_fill_price == Decimal("0.5600")   # normalized to NO-space (1 - 0.44)
    # money-math booked ONCE, path "amend", NO-space price, REAL taker fee, de-duped by order_id.
    rest_fills = [f for f in ex.fills if f.get("leg") == "rest"]
    assert len(rest_fills) == 1
    assert rest_fills[0]["path"] == "amend" and rest_fills[0]["price"] == Decimal("0.5600")
    assert rest_fills[0]["fee"] == Decimal("0.0200")
    assert oid in ex.booked_rest_oids
    assert "amend_fill" in j.kinds()


def test_core_routes_amend_cross_fill_into_wings():
    # the core-side of the cross: OrderAmended.fill_count>0 -> rest fill at avg price + TAKE_WINGS once.
    # SIZE-1 regression (explicit contracts=1 after AMENDMENT 1 2026-09-20 raised the real file to 2); the
    # size-2 amend-cross delta booking is covered in test_v32_partial_fill.py.
    from service.v32.core import decide_v32, RestOrder
    from dataclasses import replace as dreplace
    p = dreplace(load_v32_params(), contracts=1)
    st = _st()
    # a live rest + a fresh spot context (both strikes) so the wings can price.
    live = RestOrder("c1", "oid-1", Decimal("0.50"), 1, CTS - 600, True, False, 78850)
    st = dreplace(st, rest_live=live, amend_in_flight=True, spot_Sd=78850, spot_Su=78950)
    # give both strikes fresh books
    from service.book import TopOfBook
    def top(bid, ask):
        yb, ya = Decimal(bid), Decimal(ask)
        return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                         no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
                         no_ask_size=Decimal(100), suspect=False)
    st = dreplace(st,
                  strike_tops={78850: top("0.30", "0.31"), 78950: top("0.20", "0.21")},
                  strike_ts={78850: CTS - 590, 78950: CTS - 590},
                  strike_tickers={78850: "KXBTCD-X-T78849.99", 78950: "KXBTCD-X-T78949.99"})
    st2, acts = decide_v32(p, st, OrderAmended("oid-1", "c2", Decimal("0.50"), CTS - 590,
                                               remaining_count=Decimal(0), fill_count=Decimal(1),
                                               average_fill_price=Decimal("0.56")))
    assert st2.rest_fill is not None and st2.rest_fill.price == Decimal("0.56")
    assert len([a for a in acts if a.kind == ActionKind.TAKE_WINGS]) == 1
    assert not st2.amend_in_flight and st2.rest_live is None


# ===========================================================================
# 4. amend failure -> journal amend_failed + FALL BACK to sharded cancel -> confirm -> create
# ===========================================================================
@pytest.mark.parametrize("resp", [
    WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404"),
    WriteResponse(500, {}, False, "http_500"),
    WriteResponse(None, {}, False, "post_exception:Timeout"),   # transport timeout
])
def test_amend_failure_falls_back_to_cancel_create(resp):
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    ex.on_action(_place("c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.post_queue.append(resp)                                   # the amend POST fails
    # the fallback DELETE (sharded) succeeds cleanly (reduced_by 1 -> filled 0).
    w.delete_fn = lambda p: WriteResponse(200, {"order_id": oid, "reduced_by": "1.00"}, True)
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "canceled", "fill_count_fp": "0.00",
                  "remaining_count_fp": "0.00"}}
    events = ex.on_action(_amend("c1", "c2", oid, "0.50"), _st(), CTS - 590)
    assert ex.amends_attempted == 1 and ex.amends_failed == 1 and ex.amends_confirmed == 0
    assert ex.amend_fallbacks == 1
    # the fallback ran the SHARDED DELETE (PR #50 path); no new coid was ever booked.
    assert w.deletes and w.deletes[-1] == cancel_path(oid, 2) and "exchange_index=2" in w.deletes[-1]
    assert "c2" not in ex.rest_book
    # the returned event is an OrderCancelled(filled 0) -> the core clears + re-places next tick.
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert events[0].filled_count_before_cancel == Decimal(0)
    # amend_failed journaled AND recorded that the cancel+create fallback was taken.
    assert "amend_failed" in j.kinds()
    assert j.of("amend_failed")[-1].get("fallback") == "cancel_create"


def test_amend_fallback_create_consults_pre_place_invariant_never_two_rests():
    w = _RecordingWriter()
    j = FakeJournal()
    ex = _exec(w, j)
    ex.on_action(_place("c1"), _st(), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.post_queue.append(WriteResponse(404, {"error": {"code": "not_found"}}, False, "http_404"))
    w.delete_fn = lambda p: WriteResponse(200, {"order_id": oid, "reduced_by": "1.00"}, True)
    ex.on_action(_amend("c1", "c2", oid, "0.50"), _st(), CTS - 590)   # -> fallback cancel
    assert ex.rest_book["c1"].status == "cancelled"
    # the create (core's next PLACE after the fallback confirm) goes through the pre-PLACE venue-truth
    # invariant: the open-orders GET is consulted (venue clean here) and exactly one rest ends up live.
    gets_before = len(w.gets)
    ex.on_action(_place("c3", "0.50"), _st(), CTS - 585)
    assert any(g[0] == OPEN_ORDERS_PATH for g in w.gets[gets_before:])   # invariant consulted
    live = sorted(c for c, r in ex.rest_book.items() if r.status == "live")
    assert live == ["c3"]                                                # never two live rests


# ===========================================================================
# 5. FrozenExecutor: refuses a REAL amend; synth-amends the WOULD_ twin (dry cycle)
# ===========================================================================
def test_frozen_executor_refuses_real_amend():
    from service.run_v32 import FrozenExecutor
    fe = FrozenExecutor(BUCKET_MAP)
    with pytest.raises(AssertionError):
        fe.on_action(_amend("c1", "c2", "oid-1", "0.50"), _st(), CTS - 590)


def test_frozen_executor_synth_amends_would_twin():
    from service.run_v32 import FrozenExecutor
    fe = FrozenExecutor(BUCKET_MAP)
    fe.on_action(V32Action(kind=ActionKind.WOULD_PLACE_REST, ticker=B, side="no", action="buy",
                           count=1, price=Decimal("0.50"), client_order_id="c1"), _st(), CTS - 600)
    oid = fe.rest_book["c1"].order_id
    ev = fe.on_action(V32Action(kind=ActionKind.WOULD_AMEND_REST, ticker=B, side="no", action="buy",
                                count=1, price=Decimal("0.48"), client_order_id="c1",
                                updated_client_order_id="c2", order_id=oid), _st(), CTS - 590)
    assert len(ev) == 1 and isinstance(ev[0], OrderAmended) and ev[0].fill_count == Decimal(0)
    assert fe.rest_book["c2"].status == "live" and fe.rest_book["c2"].price == Decimal("0.48")
    assert fe.rest_book["c2"].order_id == oid
    assert fe.rest_book["c1"].status == "amended"     # RETAINED (F-1)
    assert fe.counts["synth_amended"] == 1


# ===========================================================================
# 6. Ledger row carries the amend counters; report surfaces them next to replaces
# ===========================================================================
def test_ledger_row_carries_amend_counters():
    from service.v32.ledger import build_v32_ledger_row
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None,
        params=None, state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=0, strike_generations=0, bucket_count=0, bucket_generations=0,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path=None, record_count=0,
        stand_down_reason=None, now=0.0, params_sha="x",
        amends_attempted=8, amends_confirmed=6, amends_failed=2, amend_fallbacks=2, fills_on_amend=1,
    )
    assert row["amends_attempted"] == 8
    assert row["amends_confirmed"] == 6
    assert row["amends_failed"] == 2
    assert row["amend_fallbacks"] == 2
    assert row["fills_on_amend"] == 1
    # additive default: an older-shape call leaves them 0.
    row0 = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None,
        params=None, state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=0, strike_generations=0, bucket_count=0, bucket_generations=0,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path=None, record_count=0,
        stand_down_reason=None, now=0.0, params_sha="x",
    )
    assert row0["amends_attempted"] == 0 and row0["fills_on_amend"] == 0


def test_report_surfaces_amend_totals():
    from service.v32.report import build_report, _render
    rows = [{
        "close_time": CLOSE, "mode": "armed", "effective_mode": "armed", "armed": True,
        "replaces": 12, "would_places": 1, "amends_attempted": 8, "amends_confirmed": 7,
        "amends_failed": 1, "amend_fallbacks": 1, "fills_on_amend": 1, "shadow": {},
    }]
    rep = build_report(rows)
    t = rep["totals"]
    assert t["amends"] == 8 and t["amends_confirmed"] == 7 and t["amends_failed"] == 1
    assert t["amend_fallbacks"] == 1 and t["fills_on_amend"] == 1
    out = _render(rep)
    assert "amends=8" in out and "fallbacks(cancel+create)=1" in out and "fills_on_amend=1" in out
