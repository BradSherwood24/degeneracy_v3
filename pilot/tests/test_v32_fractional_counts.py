"""V3.2 fractional contract counts (2026-10-01 MECHANICS CLARIFICATION).

Kalshi crypto contracts are fractional (``count_fp``, 0.01 granularity; the 2026-09-30 incident filled
0.44 of a lot). Before this, V3.2 int-truncated on EVERY fill path: a WS fill fell back to the full
placed lot (over-hedge), and a cancel-confirm / poll fill truncated to 0 (fill LOST -> naked rung). These
tests drive each path with a fractional size and prove:

  * a 0.44 WS fill of a 2-lot rest -> wings sized 0.44, 1.56 keeps resting;
  * a 0.44 cancel-confirm (via status fp AND via reduced_by) is booked 0.44, not 0;
  * a 0.44 poll delta is booked exactly;
  * the wing SEND writes a fractional wire count ("1.44"), whole lots stay "2.00" (byte-identical);
  * the money-math / ledger counts are count-weighted (fractional-safe), while a WHOLE-lot window's
    journal / ledger counts serialise as bare ints (byte-identical to the pre-fractional build).

FAKES ONLY: no network, no proxy, no key/holdout. The SEAL (2026-08-02..18) and the 2026-08-20..29
holdout are never touched.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.proxy_writer import WriteResponse
from service.v32 import (
    ActionKind,
    BookUpdate,
    Fill,
    OrderAck,
    OrderCancelled,
    V32State,
    decide_v32,
    load_v32_params,
)
from service.v32.actions import ActionKind as V32ActionKind, V32Action
from service.v32.core import WingLeg, _rest_place_count, _rest_size
from service.v32.executor import (
    LiveExecutor,
    ORDER_STATUS_PATH_TMPL,
    parse_order_status,
)
import service.run_v32 as R

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000
BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"    # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"    # -> 79700
B_SD = "KXBTC-RANGE-B79600"
EXCH = {B_SD: 2, "KXBTC-RANGE-B79700": 2, STK_SD: 2, STK_SU: 2}


def _top(bid: str, ask: str, *, suspect: bool = False) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
                     no_ask_size=Decimal(100), suspect=suspect)


def _params(**over):
    base = {"contracts": 2, "tol": Decimal("0.01"), "deb_ms": 0}
    base.update(over)
    return replace(load_v32_params(), **base)


def _feed(params, st, event):
    return decide_v32(params, st, event)


def _fresh_books(now: float):
    return [BookUpdate(B_SD, _top("0.35", "0.36"), now),
            BookUpdate(STK_SU, _top("0.36", "0.37"), now),
            BookUpdate(STK_SD, _top("0.75", "0.76"), now)]


def _bring_up_live_rest(params, st, now):
    for e in _fresh_books(now):
        st, acts = _feed(params, st, e)
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert place and place[-1].count == params.contracts
    coid = place[-1].client_order_id
    st, _ = _feed(params, st, OrderAck(coid, "OID1", now))
    assert st.rest_live is not None and st.rest_live.count == Decimal(params.contracts)
    return st, coid, "OID1"


# ===========================================================================
# CORE: a fractional WS fill sizes the wings to the fraction and leaves a fractional remainder resting
# ===========================================================================
def test_core_fractional_partial_leaves_fractional_remainder():
    p = _params()
    st = V32State.new(CLOSE, T, BK, p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    # 0.44 of 2 fills -> wings for exactly 0.44; 1.56 keeps resting on the SAME order.
    st, acts = _feed(p, st, Fill(oid, coid, Decimal("0.44"), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1 and tw[0].count == Decimal("0.44")
    assert len(tw[0].legs) == 2 and all(l.count == Decimal("0.44") for l in tw[0].legs)
    assert st.rest_live is not None and st.rest_live.order_id == oid
    assert st.rest_live.count == Decimal("1.56")
    assert st.rest_remaining == Decimal("1.56") and not st.rest_allotment_done
    assert st.rest_fills[-1].count == Decimal("0.44") and st.partial_fills == 1
    assert st.rest_booked_by_coid.get(coid) == Decimal("0.44")


def test_core_fractional_fills_sum_to_allotment_done():
    p = _params()
    st = V32State.new(CLOSE, T, BK, p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal("0.44"), Decimal("0.45"), "no", now + 0.1))
    # the remaining 1.56 fills -> second batch sized 1.56, allotment done, quoting stops.
    st, acts = _feed(p, st, Fill(oid, coid, Decimal("1.56"), Decimal("0.45"), "no", now + 0.2))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1 and tw[0].count == Decimal("1.56")
    assert len(st.wing_batches) == 2 and len(st.rest_fills) == 2
    assert st.rest_allotment_done and st.rest_remaining == 0
    assert st.rest_live is None and st.rest_booked_by_coid.get(coid) == Decimal(2)


# ===========================================================================
# CORE: a cumulative cancel-confirm books only the fractional DELTA
# ===========================================================================
def test_core_fractional_cancel_confirm_books_delta():
    p = _params()
    st = V32State.new(CLOSE, T, BK, p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    # a 0.44 WS fill books 0.44; the eager cancel's confirm reports CUMULATIVE 1.44 -> book exactly 1.00 more.
    st, _ = _feed(p, st, Fill(oid, coid, Decimal("0.44"), Decimal("0.45"), "no", now + 0.1))
    st, acts = _feed(p, st, OrderCancelled(order_id=oid, server_ts=now + 0.2,
                                           filled_count_before_cancel=Decimal("1.44")))
    assert st.rest_booked_by_coid.get(coid) == Decimal("1.44")
    # two batches total 1.44 of hedge (0.44 + the 1.00 delta).
    assert sum((b.fill_count for b in st.wing_batches), Decimal(0)) == Decimal("1.44")
    assert st.rest_fills[-1].count == Decimal("1.00")


# ===========================================================================
# CORE: a sub-lot fractional remainder is NEVER re-placed/amended as a count-0 wire order (fail-closed)
# ===========================================================================
def test_core_sublot_remainder_not_requoted_fail_closed():
    p = _params()
    st = V32State.new(CLOSE, T, BK, p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    # fill 1.56 of 2 -> 0.44 remains (a sub-lot). _rest_place_count floors to 0; _rest_size keeps 0.44.
    st, _ = _feed(p, st, Fill(oid, coid, Decimal("1.56"), Decimal("0.45"), "no", now + 0.1))
    assert st.rest_remaining == Decimal("0.44")
    assert _rest_size(p, st) == Decimal("0.44") and _rest_place_count(p, st) == 0
    # a drift that WOULD amend must NOT emit a (count-0) amend/place while only a sub-lot remains.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    assert not [a for a in acts if a.kind in (ActionKind.AMEND_REST, ActionKind.PLACE_REST)]


# ===========================================================================
# DRIVER pipeline (shakedown + FrozenExecutor synth-fills wings): WS fill + poll
# ===========================================================================
class _CaptureJournal:
    """A FakeJournal with a ``records`` list of {kind, obj} so tests can inspect journalled counts."""

    def __init__(self):
        self.records: list[dict] = []

    def append(self, kind, obj, ts):
        self.records.append({"kind": kind, "obj": obj})
        return len(self.records)

    def __len__(self):
        return len(self.records)

    def close(self):
        pass


class _CaptureExec(R.FrozenExecutor):
    """The dry FrozenExecutor (synth-fills wings) + the money-math capture attributes the driver writes
    into (so ``_compute_money_math`` sees the rest fills)."""

    def __init__(self, bk):
        super().__init__(bk)
        self.fills: list = []
        self.booked_rest_oids: set = set()


def _shakedown_driver(params):
    j = _CaptureJournal()
    state = V32State.new(CLOSE, T, BK, params, shakedown=True)
    drv = R.V32Driver(params, state, j, _CaptureExec(BK), clock=lambda: 0.0)
    return drv, j


def _place_rest_via_driver(drv, now):
    drv.on_book_update(B_SD, _top("0.35", "0.36"), now)
    drv.on_book_update(STK_SU, _top("0.36", "0.37"), now)
    drv.on_book_update(STK_SD, _top("0.75", "0.76"), now)
    assert drv.state.rest_live is not None
    return drv.state.rest_live.client_order_id, drv.state.rest_live.order_id


def test_driver_ws_fractional_fill_sizes_wings_and_journals_fraction():
    p = _params(tol=Decimal("0.50"), deb_ms=100000)   # no drift-requote churn
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    n = drv.state.rest_live.price
    # a WS fill carrying count_fp "0.44".
    drv.on_fill(B_SD, {"client_order_id": coid, "order_id": oid, "trade_id": "t1", "count_fp": "0.44",
                       "purchased_side": "no", "yes_price_dollars": str(Decimal(1) - n)}, now + 0.1)
    assert drv.state.rest_remaining == Decimal("1.56")
    assert drv.state.rest_fills[-1].count == Decimal("0.44")
    assert drv.state.wing_batches[-1].fill_count == Decimal("0.44")
    # the rest_fill journal carries the fraction (a Decimal string "0.44", NOT 0 or 1).
    recs = [r for r in j.records if r["kind"] == "rest_fill"]
    assert recs and recs[-1]["obj"]["count"] == Decimal("0.44")
    j.close()


def test_driver_poll_fractional_delta_books_exactly():
    p = _params(tol=Decimal("0.50"), deb_ms=100000)
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    # poll reports a CUMULATIVE fractional fill 0.44 -> booked 0.44, 1.56 keeps resting.
    drv.on_poll_fill(oid, Decimal("0.44"), now + 0.2)
    assert drv.counts["rest_fill_poll"] == 1
    assert drv.state.rest_fills[-1].count == Decimal("0.44")
    assert drv.state.rest_remaining == Decimal("1.56") and not drv.state.rest_allotment_done
    # a further poll reporting the same cumulative 0.44 -> delta 0, nothing booked.
    drv.on_poll_fill(oid, Decimal("0.44"), now + 0.3)
    assert drv.counts["rest_fill_poll"] == 1 and len(drv.state.rest_fills) == 1
    j.close()


def test_driver_whole_lot_journal_counts_are_bare_ints():
    """Byte-identity: a WHOLE-lot window's rest_fill / place_rest / take_wings journal counts are bare
    ints (not '2.00' Decimal strings), so the dry/armed journal is identical to the pre-fractional build."""
    p = _params(tol=Decimal("0.50"), deb_ms=100000)
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    n = drv.state.rest_live.price
    drv.on_fill(B_SD, {"client_order_id": coid, "order_id": oid, "trade_id": "t1", "count": 1,
                       "purchased_side": "no", "yes_price_dollars": str(Decimal(1) - n)}, now + 0.1)
    # shakedown emits the WOULD_* twins; the place count serialises as a bare int 2.
    place = [r for r in j.records if r["kind"] == "would_place_rest"]
    assert place and place[-1]["obj"]["count"] == 2 and isinstance(place[-1]["obj"]["count"], int)
    rf = [r for r in j.records if r["kind"] == "rest_fill"]
    assert rf and rf[-1]["obj"]["count"] == 1 and isinstance(rf[-1]["obj"]["count"], int)
    j.close()


# ===========================================================================
# EXECUTOR fake-proxy: cancel-confirm surfaces the fraction; the wing wire body is fractional
# ===========================================================================
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
        return WriteResponse(200, {}, True)

    def rest_get(self, path, params=None):
        self.gets.append((path, params))
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


def _v32_exec(w):
    return LiveExecutor(w, BK, EXCH, FakeJournal(), T, 300, clock=lambda: 0.0, sleep=lambda _s: None)


def _place_two_lot(ex, w, coid="c1"):
    ex.on_action(V32Action(kind=V32ActionKind.PLACE_REST, ticker=B_SD, side="no", action="buy",
                           count=Decimal(2), price=Decimal("0.45"), expiration_epoch=T - 300,
                           client_order_id=coid),
                 V32State.new(CLOSE, T, BK, load_v32_params()), T - 600)
    return ex.rest_book[coid].order_id


def test_executor_cancel_confirm_surfaces_fractional_via_status():
    w = FakeWriter()
    ex = _v32_exec(w)
    assert ex._fractional_counts is True
    oid = _place_two_lot(ex, w)
    # 0.44 slipped in before the cancel; the status reports fill_count_fp 0.44.
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count_fp": "0.44",
                  "remaining_count_fp": "0.00"}}
    events = ex.on_action(V32Action(kind=V32ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1"),
                          V32State.new(CLOSE, T, BK, load_v32_params()), T - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)]
    assert len(oc) == 1 and oc[0].filled_count_before_cancel == Decimal("0.44")
    cc = ex.journal.find("cancel_confirmed")
    assert cc and cc[-1]["filled_before_cancel"] == Decimal("0.44")


def test_executor_cancel_confirm_fractional_via_reduced_by():
    w = FakeWriter()
    ex = _v32_exec(w)
    oid = _place_two_lot(ex, w)
    # DELETE 2xx with reduced_by 1.56 on a placed 2-lot rest -> filled = 2 - 1.56 = 0.44.
    w.delete_queue.append(WriteResponse(200, {"order_id": oid, "reduced_by": "1.56"}, True))
    events = ex.on_action(V32Action(kind=V32ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1"),
                          V32State.new(CLOSE, T, BK, load_v32_params()), T - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)][0]
    assert oc.filled_count_before_cancel == Decimal("0.44")


def test_executor_cancel_confirm_whole_count_byte_identical():
    """A WHOLE cancel-confirm fill still journals a bare int (not '2.00')."""
    w = FakeWriter()
    ex = _v32_exec(w)
    oid = _place_two_lot(ex, w)
    w.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
        "order": {"order_id": oid, "status": "executed", "fill_count_fp": "2.00",
                  "remaining_count_fp": "0.00"}}
    events = ex.on_action(V32Action(kind=V32ActionKind.CANCEL_REST, order_id=oid, client_order_id="c1"),
                          V32State.new(CLOSE, T, BK, load_v32_params()), T - 590)
    oc = [e for e in events if isinstance(e, OrderCancelled)][0]
    assert oc.filled_count_before_cancel == Decimal(2)
    cc = ex.journal.find("cancel_confirmed")[-1]
    assert cc["filled_before_cancel"] == 2 and isinstance(cc["filled_before_cancel"], int)


def _take_wing_body(count: Decimal) -> dict:
    """Drive the executor's _take_wings for a single YES wing of ``count`` and return the posted body."""
    w = FakeWriter()
    ex = _v32_exec(w)
    leg = WingLeg(ticker=STK_SD, side="yes", count=count, limit=Decimal("0.76"),
                  client_order_id="w-y", status="pending", batch=0)
    st = replace(V32State.new(CLOSE, T, BK, load_v32_params()),
                 strike_tickers={79600: STK_SD, 79700: STK_SU}, wing_legs=(leg,))
    ex._take_wings(st, T - 500)
    assert w.posts, "expected a wing create POST"
    return w.posts[-1][1]


def test_executor_wing_wire_count_fractional():
    body = _take_wing_body(Decimal("1.44"))
    assert body["count"] == "1.44"       # fractional-safe wire body (not to_v2_order's int '1.00')


def test_executor_wing_wire_count_whole_byte_identical():
    body = _take_wing_body(Decimal(2))
    assert body["count"] == "2.00"       # whole lot byte-identical


# ===========================================================================
# parse_order_status: the int field is retained (unchanged) alongside the exact fp
# ===========================================================================
def test_parse_order_status_keeps_int_and_fp():
    s = parse_order_status({"order": {"order_id": "o", "status": "executed", "fill_count_fp": "0.44",
                                      "remaining_count_fp": "1.56", "initial_count_fp": "2.00"}}, "o")
    assert s.filled_count == 0 and s.filled_count_fp == Decimal("0.44")
    w = parse_order_status({"order": {"fill_count_fp": "2.00", "remaining_count_fp": "0.00"}}, "o")
    assert w.filled_count == 2 and w.filled_count_fp == Decimal("2.00")


# ===========================================================================
# MONEY-MATH / LEDGER: count-weighted for fractional, bare-int for whole (byte-identity)
# ===========================================================================
def _run_money_math(fill_count: Decimal):
    p = _params(tol=Decimal("0.50"), deb_ms=100000)
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    n = drv.state.rest_live.price
    drv.on_fill(B_SD, {"client_order_id": coid, "order_id": oid, "trade_id": "t1",
                       "count_fp": str(fill_count), "purchased_side": "no",
                       "yes_price_dollars": str(Decimal(1) - n)}, now + 0.1)
    money = R._compute_money_math(drv.state, drv.executor, contracts=p.contracts)
    j.close()
    return money


def test_money_math_fractional_count_weighted():
    money = _run_money_math(Decimal("0.44"))
    # the held bucket-NO leg carries the fractional fill count; lots_filled is the fraction.
    held = money["held_legs"]
    assert held and any(h["count"] == Decimal("0.44") and h["side"] == "no" for h in held)
    assert money["lots_filled"] == Decimal("0.44")
    assert money["rest_fills"][-1]["count"] == Decimal("0.44")
    # floor is count-weighted: the shakedown FrozenExecutor synth-fills both wings -> a 3-leg pin,
    # floor = (3-1) * 0.44 = 0.88 per the fixed geometry.
    assert Decimal(str(money["floor_booked"])) == Decimal("0.88")


def test_money_math_whole_count_bare_ints():
    money = _run_money_math(Decimal(1))
    assert money["lots_filled"] == 1 and isinstance(money["lots_filled"], int)
    held = money["held_legs"]
    assert all(isinstance(h["count"], int) for h in held)
    assert money["rest_fills"][-1]["count"] == 1 and isinstance(money["rest_fills"][-1]["count"], int)
    assert isinstance(money["lots_unfilled_at_quote_end"], int)


# ===========================================================================
# REPORT reconciliation: the ``size`` field is a bare int for a WHOLE set (so the
# ``--json`` report stays byte-identical to the pre-fractional build) and a Decimal
# for a fractional set. (Review nit N1, 2026-10-01.)
# ===========================================================================
def test_report_recon_size_whole_is_bare_int_fractional_is_decimal():
    import json as _json
    from service.v32.report import build_ledger_reconciliation

    def _row(close, fill_count, held=3):
        return {
            "mode": "armed", "close_time": close, "Sd": 79600, "Su": 79700,
            "wing_batch_sets": [{"index": 0, "fill_count": fill_count,
                                 "held_legs": held, "completed": True}],
            "realized_lock": "0.10", "one_legged": False,
            "floor_booked": "0.88", "realized_delta": "0.80",
            "held_legs": [{"ticker": "KXBTC-RANGE-B79600", "side": "no", "count": fill_count}],
        }

    # whole set (int row 2, and Decimal-string row "2") -> size a BARE int
    for fc in (2, "2", "2.00"):
        rc = build_ledger_reconciliation([_row("2026-10-01T00:00:00Z", fc)])
        assert rc[0]["size"] == 2 and isinstance(rc[0]["size"], int), (fc, rc[0]["size"])
        # and it serialises as a JSON NUMBER (not a quoted string) under the report's dump
        dumped = _json.dumps(rc[0], default=lambda o: str(o))
        assert '"size": 2' in dumped and '"size": "2"' not in dumped

    # fractional set -> size the exact Decimal (str-encoded by the json dump like the other fields)
    rc = build_ledger_reconciliation([_row("2026-10-01T01:00:00Z", "0.44")])
    assert rc[0]["size"] == Decimal("0.44") and isinstance(rc[0]["size"], Decimal)
