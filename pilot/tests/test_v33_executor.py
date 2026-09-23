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
# chunked coalesced wing take (MUST-FIX-1) against a CAP-ENFORCING fake
# ---------------------------------------------------------------------------
class CapWriter(FakeWriter):
    """A proxy fake that ENFORCES ``MAX_CONTRACTS_PER_ORDER``: any create whose count > cap is rejected
    (like the real proxy). Records every chunk's count so a test can assert the chunking. Echoes each
    order's coid so the executor can aggregate."""

    def __init__(self, cap, reject_coids=()):
        super().__init__()
        self.cap = cap
        self.reject_coids = set(reject_coids)
        self.chunk_counts: list[int] = []

    def _slot(self, o):
        coid = o.get("client_order_id")
        cnt = int(Decimal(str(o.get("count", 1))))
        self.chunk_counts.append(cnt)
        if cnt > self.cap or coid in self.reject_coids:
            return {"client_order_id": coid, "order_id": None, "fill_count": "0.00",
                    "error": "too_large"}
        return {"client_order_id": coid, "order_id": f"oid-{coid}", "fill_count": f"{cnt}.00",
                "average_fill_price": o.get("price", "0.5000")}

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if "orders" in body:                      # batch create
            return WriteResponse(200, {"orders": [self._slot(o) for o in body["orders"]]}, True)
        return WriteResponse(200, {"order": self._slot(body)}, True)   # single create


def _wing_state(count):
    from dataclasses import replace as dr
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    legs = (WingLeg(S_SD, "yes", count, Decimal("0.55"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", count, Decimal("0.90"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def test_wing_take_chunks_at_cap_two_six_plus_six():
    w = CapWriter(cap=2)
    ex = _exec(w, k=11)
    ex.wing_cap = 2
    st = _wing_state(11)
    events = ex._take_wings(st, CTS - 400)
    # 11 lots per wing at cap 2 -> ceil(11/2)=6 chunks per wing -> 12 chunk orders, none over cap 2.
    assert ex.wing_chunks == 12 and all(c <= 2 for c in w.chunk_counts)
    fills = [e for e in events if isinstance(e, Fill)]
    assert len(fills) == 2 and all(f.count == Decimal(11) for f in fills)   # aggregated to the full 11


def test_wing_take_cap_eleven_one_plus_one():
    w = CapWriter(cap=11)
    ex = _exec(w, k=11)
    ex.wing_cap = 11
    st = _wing_state(11)
    ex._take_wings(st, CTS - 400)
    assert ex.wing_chunks == 2 and w.chunk_counts == [11, 11]   # 1 order per wing


def test_wing_take_rejected_chunk_retried_for_remainder():
    # cap 2, count 4 -> 2 chunks per wing; reject ONE yes chunk -> yes leg partial -> reported unfilled;
    # a second take (the core's RETRY) sends chunks ONLY for the remaining 2 (never over-hedging).
    w = CapWriter(cap=2, reject_coids={"v33-wc-1"})   # the first yes chunk fails
    ex = _exec(w, k=11)
    ex.wing_cap = 2
    st = _wing_state(4)
    events1 = ex._take_wings(st, CTS - 400)
    yes_fill = next(e for e in events1 if isinstance(e, Fill) and e.side == "yes")
    no_fill = next(e for e in events1 if isinstance(e, Fill) and e.side == "no")
    assert no_fill.count == Decimal(4)          # no leg fully filled
    assert yes_fill.count == Decimal(0)         # yes leg partial (1 chunk rejected) -> unfilled -> retry
    assert ex._wing_filled[(0, "yes")] == 2     # 2 of 4 taken; retry must send only the remaining 2
    # simulate the core's RETRY_WING: the leg re-pends with a NEW coid, still count 4.
    from dataclasses import replace as dr
    st2 = dr(st, wing_legs=(dr(st.wing_legs[0], client_order_id="v33-wy2", status="pending"),
                            dr(st.wing_legs[1], status="filled")))
    w.chunk_counts.clear()
    w.reject_coids.clear()
    ex._take_wings(st2, CTS - 390)
    assert sum(w.chunk_counts) == 2             # only the 2 remaining lots re-chunked, not 4


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


def test_invariant_healthy_partial_ladder_one_get_no_sleep():
    """MUST-FIX-2: a HEALTHY resting ladder (all attributed, no dup, < K) proceeds on the FIRST read --
    exactly ONE venue GET, NO 0.5 s recheck sleep. (V3.2 stalled here on every create.)"""
    w = FakeWriter()
    sleeps: list[float] = []
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                         clock=lambda: 0.0, sleep=lambda s: sleeps.append(s))
    seeded = _seed_resting(ex, [(f"v33-r{i}", f"o{i}", str(Decimal("0.45") - i * Decimal("0.01")))
                                for i in range(10)])   # 10 healthy rungs, K=11
    w.get_map["/portfolio/orders"] = {"orders": seeded}
    ex._pending_place_price = Decimal("0.35")          # a fresh price, none resting there
    assert ex._pre_place_invariant("v33-r10", CTS - 500) is None
    open_orders_gets = [g for g in w.gets if g[0] == "/portfolio/orders"]
    assert len(open_orders_gets) == 1 and sleeps == []   # ONE GET, NO sleep
    assert ex.rest_invariant_rechecks == 0


def test_invariant_dup_triggers_recheck():
    """A flagged anomaly (dup price) DOES recheck (sleep + 2nd GET) before declaring a violation."""
    w = FakeWriter()
    sleeps: list[float] = []
    ex = V33LiveExecutor(w, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                         clock=lambda: 0.0, sleep=lambda s: sleeps.append(s))
    seeded = _seed_resting(ex, [("v33-a", "oa", "0.45")])
    w.get_map["/portfolio/orders"] = {"orders": seeded}
    ex._pending_place_price = Decimal("0.45")          # dup with the resting order -> anomaly -> recheck
    out = ex._pre_place_invariant("v33-c", CTS - 500)
    open_orders_gets = [g for g in w.gets if g[0] == "/portfolio/orders"]
    assert len(open_orders_gets) == 2 and len(sleeps) == 1   # recheck fired
    assert out is not None and ex.rest_invariant_dup_price == 1


# ---------------------------------------------------------------------------
# write-token pacer (MUST-FIX-4)
# ---------------------------------------------------------------------------
class _AdvancingClock:
    """A clock whose ``sleep`` advances it, so the pacer's token refill is testable in wall-clock."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_pacer_11_creates_never_go_negative():
    from service.v33.executor import WriteTokenBucket, COST_CREATE
    ck = _AdvancingClock()
    b = WriteTokenBucket(rate=100.0, size=100.0, clock=ck.now, sleep=ck.sleep)
    min_tokens = b.tokens
    for _ in range(11):
        b.acquire(COST_CREATE, "create")
        min_tokens = min(min_tokens, b.tokens)
    assert min_tokens >= 0.0                    # a burst of 11 creates never overdraws the bucket
    assert b.total_wait_s > 0.0 and ck.t > 0.0  # the 11th create paced (110 tokens > 100 in one burst)


def test_pacer_cancels_not_delayed_behind_creates():
    from service.v33.executor import WriteTokenBucket, COST_CREATE, COST_CANCEL
    ck = _AdvancingClock()
    b = WriteTokenBucket(rate=100.0, size=100.0, clock=ck.now, sleep=ck.sleep)
    for _ in range(10):                          # deplete the bucket to 0 with creates (no clock advance)
        b.acquire(COST_CREATE, "create")
    t_before = ck.t
    wait = b.acquire(COST_CANCEL, "cancel", priority=True)   # a priority cancel jumps the queue
    assert wait == 0.0 and ck.t == t_before      # the cancel is NOT delayed behind the creates


# ---------------------------------------------------------------------------
# batched order poll (NIT-d)
# ---------------------------------------------------------------------------
def test_batched_poll_returns_filled_by_order_id():
    w = FakeWriter()
    ex = _exec(w, k=11)
    w.get_map["/portfolio/orders"] = {"orders": [
        {"client_order_id": "v33-a", "order_id": "oa", "fill_count_fp": "1.00"},
        {"client_order_id": "v33-b", "order_id": "ob", "fill_count_fp": "0.00"},
        {"client_order_id": "v32-x", "order_id": "ox", "fill_count_fp": "2.00"}]}  # foreign -> excluded
    res = ex.poll_orders_for_bucket(B)
    assert res == {"oa": 1, "ob": 0} and "ox" not in res


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
