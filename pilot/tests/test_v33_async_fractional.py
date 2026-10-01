"""V3.3 ASYNC executor D3 (F1/F2 port, 2026-09-30): the OFF-LOOP twin must hedge a FRACTIONAL fill and
surface a fractional cancel-confirm fill EXACTLY as the synchronous ``V33LiveExecutor`` now does.

These are the async twins of ``test_v33_wing_fractional_exec.py`` (wing SEND) and
``test_v33_fill_discovery_fractional.py`` (fill DISCOVERY). The async executor inherits the fractional
helpers (``_dc`` / ``_count_body_str`` / ``_finish_cancel``) from the sync class, so this file pins the
sites the async OVERRIDES touch: ``_take_wings_send`` (wire count + chunk sum + aggregate), the
cancel-confirm path (``_resolve_cancel_success_async`` + ``_confirm_cancel_filled_async`` -> the exact
``filled_count_before_cancel``), ``poll_orders_for_bucket_async`` (Decimal, not int-truncated), and the
print-through ``_take_bucket_no_async`` / ``_unwind_wings_async`` chunking. FAKES ONLY; no network, no
proxy, no key, no holdout.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace as dr
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, V32Action
from service.v32.events import Fill, OrderCancelled
from service.v32.executor import (OPEN_ORDERS_PATH, ORDER_STATUS_PATH_TMPL, OrderStatus,
                                  RestRecord)
from service.v33 import V33State, WingLeg, load_v33_params
from service.v33.actions import LegOrder, V33Action, V33ActionKind
from service.v33.async_writer import AsyncOrderWriter
from service.v33.async_executor import V33AsyncExecutor

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


class FracAsyncProxy:
    """A ProxyWriter fake (post/delete take ``headers=`` as the AsyncOrderWriter passes) that keeps the
    FRACTIONAL wire ``count`` (no int() truncation), rejects a create whose count > cap numerically
    (as the real proxy's ``max_contracts_per_order`` does), and echoes the requested count as the
    fill_count. ``delete_queue`` / ``get_map`` let a test drive the cancel-confirm path."""

    def __init__(self, cap=2):
        self.cap = Decimal(str(cap))
        self.posts: list[tuple] = []
        self.chunk_counts: list[Decimal] = []
        self.delete_queue: list[WriteResponse] = []
        self.get_map: dict = {}
        self._oid = 0

    def _one(self, o):
        coid = o.get("client_order_id")
        cnt = Decimal(str(o.get("count", "1")))     # NO int() truncation — keep the fraction
        self.chunk_counts.append(cnt)
        if cnt > self.cap:                          # the real proxy's numeric cap comparison
            return {"client_order_id": coid, "order_id": None, "fill_count": "0.00",
                    "error": "too_large"}
        self._oid += 1
        return {"client_order_id": coid, "order_id": f"oid-{coid or self._oid}",
                "fill_count": f"{cnt}", "average_fill_price": o.get("price", "0.5000")}

    def rest_post(self, path, body, headers=None):
        self.posts.append((path, body))
        if "orders" in body:
            return WriteResponse(200, {"orders": [self._one(o) for o in body["orders"]]}, True)
        return WriteResponse(200, {"order": self._one(body)}, True)

    def rest_delete(self, path, headers=None):
        if self.delete_queue:
            return self.delete_queue.pop(0)
        return WriteResponse(200, {"reduced_by": "0.00"}, True)

    def rest_get(self, path, params=None):
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


def _aexec(fake, *, k=11, wing_cap=2, batch_create=True):
    aw = AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)
    ex = V33AsyncExecutor(aw, fake, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=k,
                          clock=lambda: 0.0, sleep=lambda _s: None, wing_cap=wing_cap,
                          batch_create=batch_create)
    ex.wing_cap = wing_cap
    return ex, aw


def _wing_state(count):
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())
    legs = (WingLeg(S_SD, "yes", count, Decimal("0.55"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", count, Decimal("0.90"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def _take_wings(count):
    """Run the ASYNC wing take for a leg count through the real on_action_async dispatch."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    st = _wing_state(count)
    act = V32Action(kind=ActionKind.TAKE_WINGS, ticker=B, side="no", action="buy", count=1,
                    price=Decimal("0.55"), client_order_id="v33-w")

    async def go():
        try:
            return await ex.on_action_async(act, st, CTS - 400)
        finally:
            aw.close()

    return fake, asyncio.run(go())


# ---------------------------------------------------------------------------
# F1 — async wing SEND is fractional end to end
# ---------------------------------------------------------------------------
def test_async_wing_wire_count_is_fractional_not_int_truncated():
    """The async wing chunk wire body sends ``count`` as the 2dp fractional string ('1.44'), not
    ``int(1.44)`` = '1.00' (the pre-port async bug)."""
    fake, _events = _take_wings(Decimal("1.44"))
    counts = sorted(str(c) for c in fake.chunk_counts)
    assert counts == ["1.44", "1.44"], counts
    for _path, body in fake.posts:
        bodies = body["orders"] if "orders" in body else [body]
        assert all(o["count"] == "1.44" for o in bodies), bodies


def test_async_fractional_fill_is_fully_hedged_not_under_hedged():
    """A 1.44 fill hedges 1.44 lots on each wing through the async path, never the int-truncated 1."""
    _fake, events = _take_wings(Decimal("1.44"))
    fills = [e for e in events if isinstance(e, Fill)]
    assert len(fills) == 2
    assert all(f.count == Decimal("1.44") for f in fills), [str(f.count) for f in fills]
    assert Decimal("1") not in {f.count for f in fills}


def test_async_multi_chunk_last_chunk_is_the_fractional_remainder():
    """A 3.44 fill at cap 2 chunks to [2.00, 1.44] per wing (sum = 3.44), the LAST chunk fractional, and
    aggregates back to the full 3.44 hedge — the async twin of the sync multi-chunk test."""
    fake, events = _take_wings(Decimal("3.44"))
    assert len(fake.chunk_counts) == 4                       # 2 wings x ceil(3.44/2)=2 chunks
    assert sum(fake.chunk_counts) == Decimal("2") * Decimal("3.44")
    per_wing = sorted(str(c) for c in fake.chunk_counts)
    assert per_wing == ["1.44", "1.44", "2.00", "2.00"], per_wing
    assert all(c <= Decimal("2") for c in fake.chunk_counts)
    fills = [e for e in events if isinstance(e, Fill)]
    assert all(f.count == Decimal("3.44") for f in fills), [str(f.count) for f in fills]


# ---------------------------------------------------------------------------
# F2 — async fill DISCOVERY is fractional (cancel-confirm + poll)
# ---------------------------------------------------------------------------
def test_async_cancel_confirm_delivers_fractional_fill():
    """The cancel-confirm path (2xx DELETE + status-truth GET) surfaces a 0.44 fill as
    ``OrderCancelled(filled_count_before_cancel=Decimal('0.44'))`` through the ASYNC executor — the
    incident's own surfacing path, now fractional off the loop."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    st = V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())

    async def go():
        try:
            place = V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                              price=Decimal("0.46"), expiration_epoch=CTS - 300,
                              client_order_id="v33-r0")
            await ex.on_action_async(place, st, CTS - 600)
            oid = ex.rest_book["v33-r0"].order_id
            # the DELETE pulled 0.56 off the book (1 - 0.44 filled) and the status GET corroborates 0.44.
            fake.delete_queue.append(WriteResponse(200, {"reduced_by": "0.56"}, True))
            fake.get_map[ORDER_STATUS_PATH_TMPL.format(order_id=oid)] = {
                "order": {"order_id": oid, "status": "canceled",
                          "fill_count_fp": "0.44", "remaining_count_fp": "0.00"}}
            cancel = V32Action(kind=ActionKind.CANCEL_REST, order_id=oid, ticker=B, side="no",
                               action="buy", count=1, client_order_id="v33-r0")
            return await ex.on_action_async(cancel, st, CTS - 590), oid
        finally:
            aw.close()

    events, oid = asyncio.run(go())
    cancels = [e for e in events if isinstance(e, OrderCancelled)]
    assert len(cancels) == 1
    assert cancels[0].filled_count_before_cancel == Decimal("0.44"), cancels[0].filled_count_before_cancel
    # and it is a Decimal fraction, never truncated to 0 or rounded to 1.
    assert cancels[0].filled_count_before_cancel not in (Decimal(0), Decimal(1))


def test_async_poll_returns_decimal_fractional_fill():
    """``poll_orders_for_bucket_async`` returns the cumulative fill as a DECIMAL (0.44), not the pre-port
    ``int(Decimal('0.44'))`` = 0 that would make a poll-discovered fractional fill vanish."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    fake.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"client_order_id": "v33-r0", "order_id": "oid-9", "fill_count_fp": "0.44"}]}

    async def go():
        try:
            return await ex.poll_orders_for_bucket_async(B)
        finally:
            aw.close()

    out = asyncio.run(go())
    assert out == {"oid-9": Decimal("0.44")}, out
    assert isinstance(out["oid-9"], Decimal)


# ---------------------------------------------------------------------------
# F1 — async print-through taker/unwind chunking is fractional (dormant path, pinned for parity)
# ---------------------------------------------------------------------------
def _cancel_rec(coid, oid):
    return RestRecord(client_order_id=coid, order_id=oid, price=Decimal("0.46"), count=1,
                      ticker=B, bucket_Sd=80400, placed_ts=0.0, status="live", exchange_index=2)


def test_async_concurrent_cancels_do_not_cross_contaminate_status_fp():
    """REVIEW (fractional integration): a cancel-all dispatches N cancel coroutines CONCURRENTLY, whose
    ``_confirm_cancel_filled_async`` loops interleave across the off-loop status-GET await. The fractional
    cancel resolution must read ITS OWN confirm's status fp, never a sibling's off the shared
    ``_last_confirm_status_fp`` field.

    Deterministic interleave: order A (TRUE fill 0, reduced_by = full count, status poll UNREADABLE) and
    order B (0.44 fill, reduced_by 0.56) resolve concurrently; B writes the shared field 0.44 while A's own
    poll is unreadable. A must still report filled_before_cancel = 0 (its truth), not B's 0.44 (which would
    book a PHANTOM fractional fill on A and hedge a position it never held). PRE-FIX A reads the clobbered
    0.44; POST-FIX A reads its own confirm return (0)."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    recA, recB = _cancel_rec("v33-A", "oidA"), _cancel_rec("v33-B", "oidB")

    async def go():
        a_entered = asyncio.Event()
        b_wrote = asyncio.Event()

        async def status(oid):
            if oid == "oidA":
                a_entered.set()
                await b_wrote.wait()               # A's poll stays unreadable until B wrote the field
                return OrderStatus("oidA", None, 0, None, available=False)
            await a_entered.wait()                  # ensure A entered confirm (reset field) before B writes
            return OrderStatus("oidB", "canceled", 0, 0, True, filled_count_fp=Decimal("0.44"))
        ex.order_status_async = status

        orig_finish = ex._finish_cancel
        def finish(rec, oid, filled, ds, rb, now, **kw):
            r = orig_finish(rec, oid, filled, ds, rb, now, **kw)
            if oid == "oidB":
                b_wrote.set()                       # B has computed filled_fp (field written) -> release A
            return r
        ex._finish_cancel = finish

        wrA = WriteResponse(200, {"reduced_by": "1.00"}, True)   # A: 0 filled
        wrB = WriteResponse(200, {"reduced_by": "0.56"}, True)   # B: 0.44 filled
        try:
            return await asyncio.gather(
                ex._resolve_cancel_success_async(wrA, recA, "oidA", 0.0),
                ex._resolve_cancel_success_async(wrB, recB, "oidB", 0.0))
        finally:
            aw.close()

    resA, resB = asyncio.run(go())
    assert resA[0].filled_count_before_cancel == Decimal(0), resA[0].filled_count_before_cancel
    assert resB[0].filled_count_before_cancel == Decimal("0.44"), resB[0].filled_count_before_cancel


def test_async_take_bucket_no_fractional_chunking():
    """``_take_bucket_no_async`` chunks a 3.44 want to [2.00, 1.44] and books the fractional got."""
    fake = FracAsyncProxy(cap=2)
    ex, aw = _aexec(fake)
    act = V33Action(kind=V33ActionKind.TAKE_BUCKET_NO, ticker=B, side="no", action="buy",
                    count=Decimal("3.44"), price=Decimal("0.95"), client_order_id="v33-ptc",
                    legs=(LegOrder(B, "no", "buy", Decimal("3.44"), Decimal("0.95")),))

    async def go():
        try:
            return await ex.on_action_async(act, V33State.new(CLOSE, CTS, BUCKET_MAP,
                                                              load_v33_params()), CTS - 300)
        finally:
            aw.close()

    events = asyncio.run(go())
    per = sorted(str(c) for c in fake.chunk_counts)
    assert per == ["1.44", "2.00"], per
    fills = [e for e in events if isinstance(e, Fill)]
    assert fills and fills[0].count == Decimal("3.44"), [str(f.count) for f in fills]
