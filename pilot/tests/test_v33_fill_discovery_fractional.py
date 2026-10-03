"""V3.3 F2 (review, 2026-09-30 incident): fill DISCOVERY must not truncate count_fp to int.

The core D3 fix carries a fractional fill through decide_v33, but the incident's OWN surfacing path is the
cancel confirm / order-status poll in the executor, which parsed the venue's ``fill_count_fp`` with
``int(Decimal(...))``. A 0.44 leg then reached the core as ``Decimal(0)`` and was LOST (the WS fill
arrives ~100 s late and is dropped once the order left the ladder). These tests drive a fixture-style fake
proxy returning ``fill_count_fp: "0.44"`` on the cancel response / status poll and prove the whole
pipeline — executor -> OrderCancelled -> core -> RungFill + wings — books Decimal("0.44"), while the
LIVE-SHARED V3.2 executor path stays int-exact (byte-identical).

FAKES ONLY: no network, no proxy, no key/holdout. The SEAL (2026-08-02..18) is untouched.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.proxy_writer import WriteResponse
from service.v32 import V32State, load_v32_params
from service.v32.actions import ActionKind as V32ActionKind, V32Action
from service.v32.executor import (
    LiveExecutor,
    ORDER_STATUS_PATH_TMPL,
    cancel_path,
    parse_order_status,
)
from service.v33 import (
    BookUpdate,
    OrderAck,
    OrderCancelled,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import ActionKind, V33Action
from service.v33.executor import V33LiveExecutor

# --- shared board (the incident's 83600/83700 as 79600/79700) ---------------
CLOSE = "2026-09-04T20:00:00Z"
CTS = 1_000_000
B_SD_T = "KXBTC-RANGE-B79600"
B_SU_T = "KXBTC-RANGE-B79700"
STK_SD = "KXBTCD-26SEP0416-T79599.99"
STK_SU = "KXBTCD-26SEP0416-T79699.99"
STK_SU2 = "KXBTCD-26SEP0416-T79799.99"
BK = {B_SD_T: (79600.0, 79699.99), B_SU_T: (79700.0, 79799.99)}
EXCH = {B_SD_T: 2, B_SU_T: 2, STK_SD: 2, STK_SU: 2, STK_SU2: 2}


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def find(self, kind):
        return [o for k, o in self.records if k == kind]


class FakeWriter:
    def __init__(self):
        self.posts: list[tuple] = []
        self.deletes: list[str] = []
        self.gets: list[tuple] = []
        self.post_queue: deque = deque()
        self.delete_queue: deque = deque()
        self.get_map: dict[str, object] = {}
        self.orders_reply: object = None      # reply for GET /portfolio/orders?ticker=...

    def rest_post(self, path, body):
        self.posts.append((path, body))
        if self.post_queue:
            return self.post_queue.popleft()
        return WriteResponse(200, {"order": {"order_id": "oid-1", "client_order_id":
                                             body.get("client_order_id"), "fill_count": "0.00",
                                             "remaining_count": "1.00"}}, True)

    def rest_delete(self, path):
        self.deletes.append(path)
        if self.delete_queue:
            return self.delete_queue.popleft()
        return WriteResponse(200, {}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        if params and "ticker" in params and self.orders_reply is not None:
            return self.orders_reply
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


def _v33_exec(writer, k=11):
    return V33LiveExecutor(writer, BK, EXCH, FakeJournal(), CTS, 300, k_rungs=k,
                           clock=lambda: 0.0, sleep=lambda _s: None, batch_create=True)


def _v33_place(coid="v33-c313", n="0.46"):
    return V33Action(kind=ActionKind.PLACE_REST, ticker=B_SD_T, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


# ===========================================================================
# parse_order_status carries the exact fractional fill (int stays for V3.2)
# ===========================================================================
def test_parse_order_status_keeps_fraction_in_fp_field():
    s = parse_order_status(
        {"order": {"order_id": "o", "status": "executed", "fill_count_fp": "0.44",
                   "remaining_count_fp": "0.56", "initial_count_fp": "1.00"}}, "o")
    assert s.filled_count == 0                       # the int V3.2 has always seen (truncated)
    assert s.filled_count_fp == Decimal("0.44")      # the EXACT fill the V3.3 path now reads
    # a whole lot is identical in both.
    w = parse_order_status({"order": {"fill_count_fp": "2.00", "remaining_count_fp": "0.00"}}, "o")
    assert w.filled_count == 2 and w.filled_count_fp == Decimal("2.00")


# ===========================================================================
# V3.3 cancel-confirm SURFACES the fraction (status path + reduced_by path)
# ===========================================================================
def _v33_place_then(ex, w, coid="v33-c313"):
    ex.on_action(_v33_place(coid=coid), V33State.new(CLOSE, CTS, BK, load_v33_params()), CTS - 600)
    return ex.rest_book[coid].order_id


def test_v33_cancel_confirm_surfaces_fractional_fill_via_status():
    w = FakeWriter()
    ex = _v33_exec(w)
    oid = _v33_place_then(ex, w)
    # a 0.44 fill slipped in before the cancel; the status reports fill_count_fp 0.44.
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count_fp": "0.44",
                  "remaining_count_fp": "0.00"}}
    cancel = V33Action(kind=ActionKind.CANCEL_REST, order_id=oid, client_order_id="v33-c313")
    events = ex.on_action(cancel, V33State.new(CLOSE, CTS, BK, load_v33_params()), CTS - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)]
    assert len(oc) == 1
    assert oc[0].filled_count_before_cancel == Decimal("0.44")     # NOT int-truncated to 0
    # the cancel_confirmed journal carries the fraction, not 1/0.
    cc = ex.journal.find("cancel_confirmed")
    assert cc and cc[-1]["filled_before_cancel"] == Decimal("0.44")


def test_v33_cancel_confirm_fractional_via_reduced_by():
    w = FakeWriter()
    ex = _v33_exec(w)
    oid = _v33_place_then(ex, w)
    # DELETE 2xx with reduced_by 0.56 on a placed 1-lot rung -> filled = 1 - 0.56 = 0.44; status blank.
    w.delete_queue.append(WriteResponse(200, {"order_id": oid, "reduced_by": "0.56"}, True))
    cancel = V33Action(kind=ActionKind.CANCEL_REST, order_id=oid, client_order_id="v33-c313")
    events = ex.on_action(cancel, V33State.new(CLOSE, CTS, BK, load_v33_params()), CTS - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)][0]
    assert oc.filled_count_before_cancel == Decimal("0.44")


def test_v33_poll_orders_for_bucket_returns_decimal():
    w = FakeWriter()
    ex = _v33_exec(w)
    w.orders_reply = {"orders": [
        {"client_order_id": "v33-c313", "order_id": "oid-x", "fill_count_fp": "0.44"},
        {"client_order_id": "v33-c312", "order_id": "oid-y", "fill_count_fp": "1.00"},
        {"client_order_id": "v99-other", "order_id": "oid-z", "fill_count_fp": "2.00"},  # not ours
    ]}
    out = ex.poll_orders_for_bucket(B_SD_T)
    assert out == {"oid-x": Decimal("0.44"), "oid-y": Decimal("1.00")}   # v99 excluded, no truncation


# ===========================================================================
# V3.2 (LIVE-SHARED) stays int-exact / byte-identical — the gate off
# ===========================================================================
def _v32_exec(w):
    return LiveExecutor(w, {"KXBTC-26SEP1316-B68200": (68200.0, 68299.99)},
                        {"KXBTC-26SEP1316-B68200": 2}, FakeJournal(), CTS, 300,
                        clock=lambda: 0.0, sleep=lambda _s: None)


def test_v32_cancel_confirm_stays_int_byte_identical():
    # 2026-10-01 MECHANICS CLARIFICATION: V3.2 is now fractional too (``_fractional_counts`` True), but a
    # WHOLE cancel-confirm fill must still journal a BARE int (not a "1.00" Decimal string) so the ledger /
    # report / golden rows stay byte-identical. (A fractional V3.2 cancel is proven by
    # ``test_v32_fractional_counts.py``.)
    w = FakeWriter()
    ex = _v32_exec(w)
    assert ex._fractional_counts is True
    ex.on_action(V32Action(kind=V32ActionKind.PLACE_REST, ticker="KXBTC-26SEP1316-B68200", side="no",
                           action="buy", count=1, price=Decimal("0.45"),
                           expiration_epoch=CTS - 300, client_order_id="c1"),
                 V32State.new(CLOSE, CTS, {"KXBTC-26SEP1316-B68200": (68200.0, 68299.99)},
                              load_v32_params()), CTS - 600)
    oid = ex.rest_book["c1"].order_id
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count_fp": "1.00",
                  "remaining_count_fp": "0.00"}}
    cancel = V32Action(kind=V32ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1")
    events = ex.on_action(cancel, V32State.new(CLOSE, CTS,
                                               {"KXBTC-26SEP1316-B68200": (68200.0, 68299.99)},
                                               load_v32_params()), CTS - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)][0]
    assert oc.filled_count_before_cancel == Decimal(1)
    cc = ex.journal.find("cancel_confirmed")[-1]
    # V3.2 journal keeps the bare int (not a "1.00" decimal string) -> byte-identical.
    assert cc["filled_before_cancel"] == 1 and isinstance(cc["filled_before_cancel"], int)


# ===========================================================================
# END-TO-END: the executor-produced fractional OrderCancelled -> core books 0.44 + wings 0.44
# ===========================================================================
def _top(bid, ask):
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
                     no_ask_size=Decimal(100), suspect=False)


def _sd(ask):
    return _top(str(Decimal(ask) - Decimal("0.01")), ask)


def _core_params():
    p = replace(load_v33_params(), E_min=Decimal("0.05"), tol=Decimal("0.01"), deb_ms=0,
                bucket_switch_deb_ms=0, bucket_switch_hysteresis_usd=0)
    return p


def _feed(p, st, ev):
    st, a = decide_v33(p, st, ev)
    st.check_invariants(p)
    return st, a


def test_pipeline_executor_fraction_reaches_core_and_sizes_wings():
    # 1) drive the V3.3 EXECUTOR's cancel confirm to PRODUCE OrderCancelled(0.44) from fill_count_fp.
    w = FakeWriter()
    ex = _v33_exec(w)
    oid_exec = _v33_place_then(ex, w)
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid_exec)] = {
        "order": {"order_id": oid_exec, "status": "executed", "fill_count_fp": "0.44",
                  "remaining_count_fp": "0.00"}}
    ev = ex.on_action(V33Action(kind=ActionKind.CANCEL_REST, order_id=oid_exec,
                                client_order_id="v33-c313"),
                      V33State.new(CLOSE, CTS, BK, load_v33_params()), CTS - 590)
    produced = [e for e in ev if isinstance(e, OrderCancelled)][0]
    assert produced.filled_count_before_cancel == Decimal("0.44")

    # 2) bring up the CORE ladder on 79600, move the bucket so cancel-all retains each rung's bucket in
    #    cancel_ctx, then feed the executor-PRODUCED fractional fill on the 0.46 rung's oid.
    p = _core_params()
    st = V33State.new(CLOSE, CTS, BK, p)
    now = CTS - 600
    for ev0 in (BookUpdate(B_SD_T, _top("0.35", "0.36"), now),
                BookUpdate(STK_SU, _top("0.36", "0.37"), now),
                BookUpdate(STK_SD, _sd("0.76"), now)):
        st, acts = _feed(p, st, ev0)
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    o313 = next(o for o in st.ladder if o.price == Decimal("0.46"))
    # spot moves to 79700 -> cancel-all -> cancel_ctx retains bucket 79600 for every rung.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 1))
    st, _ = _feed(p, st, BookUpdate(B_SU_T, _top("0.55", "0.57"), now + 1))
    assert st.awaiting_replace and st.spot_Sd == 79700

    # feed the executor-produced fractional fill on o313's oid.
    st, _ = _feed(p, st, OrderCancelled(o313.order_id, now + 2,
                                        produced.filled_count_before_cancel))
    rf = st.rest_fills[-1]
    assert rf.count == Decimal("0.44") and rf.bucket_Sd == 79600 and rf.bucket_ticker == B_SD_T

    # close the coalesce window on the 79600 strikes -> wings size 0.44 on STK_SD/STK_SU.
    # gate D (2026-10-03): the take may fire on the first tick off the quiet-but-live STK_SD book.
    st, a0 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 3))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 3))
    take = [a for a in a0 + acts if a.kind == ActionKind.TAKE_WINGS][0]
    assert take.count == Decimal("0.44")
    assert {l.ticker for l in take.legs} == {STK_SD, STK_SU}
