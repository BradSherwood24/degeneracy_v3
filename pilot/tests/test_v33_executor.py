"""V3.3 armed maker executor (L2). FAKES ONLY -- no network, no proxy dialed, no key/holdout read.

Covers the ladder-specific surface of the fork: the v33 coid prefix, the K-aware pre-place venue-truth
invariant (overflow >= K, dup-price, and O-2 = a fresh order at a previously-FILLED price is NOT a
double-book), the startup sweep scoped to v33-* (never v32-*), the roll (amend) + its cancel->create
fallback, coalesced wing takes, per-order fill dedup, and the optional batch create. Every inherited
per-order path (place/amend/cancel wire bodies) is proven in the V3.2 suite -- here we prove the ladder
generalisation only.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, LegOrder, V32Action
from service.v32.events import Fill, OrderAck, OrderAmended, OrderCancelled
from service.v32.executor import RestRecord, REL_SINGLE_CREATE, REL_BATCH_CREATE
from service.v33 import V33State, WingLeg, load_v33_params
from service.v33.executor import (
    DEFAULT_BATCH_CREATE_MAX,
    V33LiveExecutor,
    cancel_stale_open_orders,
)

CLOSE = "2026-09-20T04:00:00Z"
CTS = 1789876800
B = "KXBTC-26SEP2000-B80450"
S_SD = "KXBTCD-26SEP2000-T80399.99"
S_SU = "KXBTCD-26SEP2000-T80499.99"
BUCKET_MAP = {B: (80400.0, 80499.99)}
EXCH = {B: 2, S_SD: 2, S_SU: 2}


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]


class FakeWriter:
    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.post_queue: deque = deque()
        self.delete_queue: deque = deque()
        self.get_map: dict[str, object] = {}

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if self.post_queue:
            return self.post_queue.popleft()
        return WriteResponse(200, {"order": {"order_id": "oid-1",
                                             "client_order_id": body.get("client_order_id"),
                                             "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_queue:
            return self.delete_queue.popleft()
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        v = self.get_map.get(path)
        if callable(v):
            return v()
        return v if v is not None else {}


def _exec(writer=None, journal=None, k=11, batch_create=False):
    return V33LiveExecutor(writer or FakeWriter(), BUCKET_MAP, EXCH, journal or FakeJournal(),
                           CTS, 300, k_rungs=k, clock=lambda: 0.0, sleep=lambda _s: None,
                           batch_create=batch_create)


def _place(coid="v33-c1", n="0.45"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


def _state():
    return V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())


# ---------------------------------------------------------------------------
# coid prefix + single place
# ---------------------------------------------------------------------------
def test_coid_prefix_is_v33():
    assert V33LiveExecutor.COID_PREFIX == "v33-"


def test_single_place_records_the_rung_and_price():
    w = FakeWriter()
    ex = _exec(w)
    events = ex.on_action(_place("v33-r0", "0.45"), _state(), CTS - 600)
    assert any(isinstance(e, OrderAck) for e in events)
    rec = ex.rest_book["v33-r0"]
    assert rec.status == "live" and rec.order_id == "oid-1" and rec.price == Decimal("0.45")
    # on_action recorded the price for the K-aware invariant.
    assert ex._pending_place_price == Decimal("0.45")
    path, body = w.posts[0]
    assert path == REL_SINGLE_CREATE and body["post_only"] is True


# ---------------------------------------------------------------------------
# the roll (amend) + fallback
# ---------------------------------------------------------------------------
def test_roll_amend_persists_order_id():
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place("v33-r0", "0.45"), _state(), CTS - 600)
    w.post_queue.append(WriteResponse(200, {"order": {"order_id": "oid-1",
                                                      "client_order_id": "v33-r0b",
                                                      "fill_count": "0.00",
                                                      "remaining_count": "1.00"}}, True))
    amend = V32Action(kind=ActionKind.AMEND_REST, order_id="oid-1", ticker=B, side="no", action="buy",
                      count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                      updated_client_order_id="v33-r0b")
    events = ex.on_action(amend, _state(), CTS - 590)
    assert any(isinstance(e, OrderAmended) and e.order_id == "oid-1" for e in events)
    assert ex.rest_book["v33-r0b"].order_id == "oid-1" and ex.amends_confirmed == 1


def test_roll_amend_failure_falls_back_to_cancel_create():
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place("v33-r0", "0.45"), _state(), CTS - 600)
    w.post_queue.append(WriteResponse(403, {"error": "amend blocked"}, False, "http_403"))  # amend fails
    # the fallback DELETE succeeds (reduced_by 1 -> gone)
    amend = V32Action(kind=ActionKind.AMEND_REST, order_id="oid-1", ticker=B, side="no", action="buy",
                      count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                      updated_client_order_id="v33-r0b")
    events = ex.on_action(amend, _state(), CTS - 590)
    assert ex.amends_failed == 1 and ex.amend_fallbacks == 1
    assert any(isinstance(e, OrderCancelled) for e in events)
    assert len(w.deletes) == 1


# ---------------------------------------------------------------------------
# coalesced wing take (sized to the batch total)
# ---------------------------------------------------------------------------
def test_coalesced_wing_take_two_legs_sized_to_total():
    w = FakeWriter()
    ex = _exec(w)
    st = _state()
    # a coalesced batch of 3 rungs -> both wings sized 3.
    legs = (
        WingLeg(S_SD, "yes", 3, Decimal("0.55"), "v33-wy", batch=0),
        WingLeg(S_SU, "no", 3, Decimal("0.90"), "v33-wn", batch=0),
    )
    st = st.__class__.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    from dataclasses import replace as dr
    st = dr(st, wing_legs=legs)
    w.post_queue.append(WriteResponse(200, {"orders": [
        {"client_order_id": "v33-wy", "order_id": "wy1", "fill_count": "3.00",
         "yes_price_dollars": "0.5700"},
        {"client_order_id": "v33-wn", "order_id": "wn1", "fill_count": "3.00",
         "yes_price_dollars": "0.0900"}]}, True))
    events = ex.on_action(V32Action(kind=ActionKind.TAKE_WINGS, count=3), st, CTS - 400)
    fills = [e for e in events if isinstance(e, Fill)]
    assert len(fills) == 2 and all(f.count == Decimal(3) for f in fills)
    assert ex.wing_batches == 1


# ---------------------------------------------------------------------------
# startup sweep scoped to v33-* (never v32-*)
# ---------------------------------------------------------------------------
def test_startup_sweep_cancels_only_v33_and_skips_v32():
    w = FakeWriter()
    j = FakeJournal()
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": B, "order_id": "o-v33", "client_order_id": "v33-x", "exchange_index": 2},
        {"ticker": B, "order_id": "o-v32", "client_order_id": "v32-y", "exchange_index": 2},
        {"ticker": B, "order_id": "o-none", "client_order_id": None, "exchange_index": 2},
        {"ticker": "OTHER-1", "order_id": "o-oth", "client_order_id": "v33-z"},
    ]}
    res = cancel_stale_open_orders(w, j, clock=lambda: 0.0)
    # only the v33-* KXBTC order is cancelled; v32-* and missing-coid are skipped_foreign; OTHER-* ignored.
    assert res["found"] == 1 and res["cancelled"] == 1
    assert res["skipped_foreign"] == 2
    assert w.deletes and "o-v33" in w.deletes[0]
    assert not any("o-v32" in d for d in w.deletes)


# ---------------------------------------------------------------------------
# K-aware pre-place venue invariant
# ---------------------------------------------------------------------------
def _seed_resting(ex, entries):
    """Pre-populate the RestBook + venue-list for a set of (coid, oid, price) resting v33 orders."""
    orders = []
    for coid, oid, price in entries:
        ex.rest_book[coid] = RestRecord(client_order_id=coid, order_id=oid, price=Decimal(price),
                                        count=1, ticker=B, bucket_Sd=80400, placed_ts=CTS - 600,
                                        status="live", exchange_index=2)
        ex._by_order_id[oid] = coid
        orders.append({"ticker": B, "order_id": oid, "client_order_id": coid, "exchange_index": 2})
    return orders


def test_invariant_partial_ladder_proceeds():
    w = FakeWriter()
    ex = _exec(w, k=11)
    w.get_map["/portfolio/orders"] = {"orders": _seed_resting(
        ex, [("v33-a", "oa", "0.45"), ("v33-b", "ob", "0.44")])}
    ex._pending_place_price = Decimal("0.43")   # a NEW price, ladder not full -> proceed
    assert ex._pre_place_invariant("v33-c", CTS - 500) is None
    assert ex.rest_invariant_violations == 0


def test_invariant_dup_price_is_violation():
    w = FakeWriter()
    ex = _exec(w, k=11)
    w.get_map["/portfolio/orders"] = {"orders": _seed_resting(ex, [("v33-a", "oa", "0.45")])}
    ex._pending_place_price = Decimal("0.45")   # SAME price as a resting order -> two on one price
    out = ex._pre_place_invariant("v33-c", CTS - 500)
    assert out is not None and isinstance(out[0], OrderCancelled)
    assert ex.rest_invariant_dup_price == 1 and ex.rest_invariant_violations == 1
    assert ex.stand_down_reason == "rest_invariant_violation"


def test_invariant_overflow_at_k_is_violation():
    w = FakeWriter()
    ex = _exec(w, k=2)   # tiny K so 2 resting == full
    w.get_map["/portfolio/orders"] = {"orders": _seed_resting(
        ex, [("v33-a", "oa", "0.45"), ("v33-b", "ob", "0.44")])}
    ex._pending_place_price = Decimal("0.43")   # a new price, but the ladder already holds K=2 -> overflow
    out = ex._pre_place_invariant("v33-c", CTS - 500)
    assert out is not None and ex.rest_invariant_overflow == 1 and ex.rest_invariant_violations == 1


def test_invariant_o2_fresh_order_at_previously_filled_price_ok():
    """O-2 (reviewer R4): a rung that FILLED is off the book, so the venue-resting list never shows it;
    placing a fresh order at that same price is NOT a double-book. The RestBook may still RETAIN the
    filled record, but the venue-truth read (resting only) is authoritative -> proceed."""
    w = FakeWriter()
    ex = _exec(w, k=11)
    # a RETAINED filled record at 0.45 (off the book), plus one live order elsewhere.
    ex.rest_book["v33-filled"] = RestRecord("v33-filled", "of", Decimal("0.45"), 1, B, 80400,
                                            CTS - 600, "filled", exchange_index=2)
    ex._by_order_id["of"] = "v33-filled"
    w.get_map["/portfolio/orders"] = {"orders": _seed_resting(ex, [("v33-a", "oa", "0.44")])}
    ex._pending_place_price = Decimal("0.45")   # the previously-filled price; NOT in the resting list
    assert ex._pre_place_invariant("v33-new", CTS - 500) is None
    assert ex.rest_invariant_violations == 0


def test_invariant_unattributable_stray_is_violation():
    w = FakeWriter()
    ex = _exec(w, k=11)
    # a v33 order on the venue that is NOT in our RestBook (a stray we cannot attribute a price to).
    w.get_map["/portfolio/orders"] = {"orders": [
        {"ticker": B, "order_id": "o-stray", "client_order_id": "v33-stray", "exchange_index": 2}]}
    ex._pending_place_price = Decimal("0.43")
    out = ex._pre_place_invariant("v33-c", CTS - 500)
    assert out is not None and ex.rest_invariant_violations == 1


# ---------------------------------------------------------------------------
# optional batch create
# ---------------------------------------------------------------------------
def test_place_batch_chunks_and_acks():
    w = FakeWriter()
    ex = _exec(w, k=11, batch_create=True)
    actions = [_place(f"v33-r{i}", str(Decimal("0.45") - i * Decimal("0.01"))) for i in range(3)]
    # empty venue list so the pre-place invariant proceeds for each.
    w.get_map["/portfolio/orders"] = {"orders": []}
    w.post_queue.append(WriteResponse(200, {"orders": [
        {"client_order_id": a.client_order_id, "order_id": f"oid-{i}",
         "fill_count": "0.00", "remaining_count": "1.00"} for i, a in enumerate(actions)]}, True))
    events = ex.place_batch(actions, CTS - 600)
    acks = [e for e in events if isinstance(e, OrderAck)]
    assert len(acks) == 3 and ex.batch_creates == 1
    assert w.posts[0][0] == REL_BATCH_CREATE


def test_place_batch_respects_chunk_max():
    w = FakeWriter()
    ex = _exec(w, k=20, batch_create=True)
    ex.batch_create_max = 8
    actions = [_place(f"v33-r{i}", str(Decimal("0.45") - i * Decimal("0.01"))) for i in range(11)]
    w.get_map["/portfolio/orders"] = {"orders": []}
    # each chunk posts once; the responses echo the coids.
    def _resp(_p, body):
        w.posts.append((_p, body))
        from service.orders.envelope import BATCH_CREATE_PATH  # noqa: F401
        orders = body.get("orders", [body])
        return WriteResponse(200, {"orders": [
            {"client_order_id": o.get("client_order_id"), "order_id": f"oid-{o.get('client_order_id')}",
             "fill_count": "0.00", "remaining_count": "1.00"} for o in orders]}, True)
    w.rest_post = _resp
    ex.place_batch(actions, CTS - 600)
    assert ex.batch_creates == 2 and DEFAULT_BATCH_CREATE_MAX == 8


def test_plain_cancel_confirms_and_retains_record():
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place("v33-r0", "0.45"), _state(), CTS - 600)
    cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id="oid-1", client_order_id="v33-r0")
    events = ex.on_action(cancel, _state(), CTS - 500)
    assert any(isinstance(e, OrderCancelled) for e in events)
    assert ex.cancels_attempted == 1 and ex.rest_book["v33-r0"].status == "cancelled"  # RETAINED (F-1)


def test_cancel_with_no_order_id_is_noop_confirm():
    ex = _exec()
    cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id=None, client_order_id="v33-unknown")
    events = ex.on_action(cancel, _state(), CTS - 500)
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert events[0].order_id is None


def test_cancel_race_fill_booked_once_into_money_math():
    w = FakeWriter()
    ex = _exec(w)
    ex.on_action(_place("v33-r0", "0.45"), _state(), CTS - 600)
    # DELETE returns reduced_by 0 -> the order pulled nothing off the book; the status GET shows 1 filled.
    w.delete_queue.append(WriteResponse(200, {"reduced_by": "0.00"}, True))
    w.get_map["/portfolio/orders/oid-1"] = {"order": {"order_id": "oid-1", "status": "executed",
                                                      "fill_count_fp": "1.00", "remaining_count_fp": "0.00"}}
    ex.on_action(V32Action(kind=ActionKind.CANCEL_REST, order_id="oid-1", client_order_id="v33-r0"),
                 _state(), CTS - 500)
    rest_fills = [f for f in ex.fills if f["leg"] == "rest"]
    assert len(rest_fills) == 1 and rest_fills[0]["count"] == 1
    assert "oid-1" in ex.booked_rest_oids   # de-dup key set so a WS echo won't double-book


def test_amend_with_no_order_id_falls_back_noop():
    ex = _exec()
    amend = V32Action(kind=ActionKind.AMEND_REST, order_id=None, ticker=B, side="no", action="buy",
                      count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                      updated_client_order_id="v33-r0b")
    events = ex.on_action(amend, _state(), CTS - 500)
    assert ex.amends_failed == 1 and ex.amend_fallbacks == 1
    assert len(events) == 1 and isinstance(events[0], OrderCancelled) and events[0].order_id is None


def test_would_twin_raises_on_armed_executor():
    ex = _exec()
    try:
        ex.on_action(V32Action(kind=ActionKind.WOULD_PLACE_REST, ticker=B, price=Decimal("0.45"),
                               client_order_id="v33-x", count=1), _state(), CTS - 600)
        assert False, "expected AssertionError"
    except AssertionError as e:
        assert "shakedown twin" in str(e)
