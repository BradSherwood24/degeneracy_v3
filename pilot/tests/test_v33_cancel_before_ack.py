"""2026-10-02 Test Fire #2, 15:00Z window: a CANCEL_REST for an order whose create is still IN FLIGHT.

Incident: Kalshi was slow (creates 2+ s), the strike feed stalled, the core called a stale-wing hold and
cancelled the ladder before any create had a venue id. The async executor treated each such cancel as a
no-op and REPORTED it cancelled, the core re-placed the slots, and 17 orphan rests sat unmanaged at the
venue until expiry (rest-count invariant -> executor stand-down; 0 fills, by luck).

Fix under test: the cancel is DEFERRED (no event; the slot stays owned) and executed the instant the ack
lands, so the order never rests unowned. A cancel for a coid with NO create in flight is unchanged."""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderAck, OrderCancelled
from service.v33.async_executor import V33AsyncExecutor
from service.v33.async_writer import AsyncOrderWriter
from tests.test_v33_async_fractional import B, BUCKET_MAP, CTS, EXCH, FakeJournal, FracAsyncProxy


class SlowCreateProxy(FracAsyncProxy):
    """The create POST blocks (on its worker thread) until the test releases it; cancels are recorded."""

    def __init__(self, *, reject=False):
        super().__init__(cap=2)
        self.post_started = threading.Event()
        self.release = threading.Event()
        self.deletes: list[str] = []
        self.reject = reject

    def rest_post(self, path, body, headers=None):
        self.post_started.set()
        assert self.release.wait(5.0), "test did not release the create"
        if self.reject:
            return WriteResponse(400, {"error": "rejected"}, False, "bad")
        return super().rest_post(path, body, headers)

    def rest_delete(self, path, headers=None):
        self.deletes.append(path)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)


def _aexec(fake):
    aw = AsyncOrderWriter(fake, wing_workers=2, cancel_workers=2, normal_workers=2)
    ex = V33AsyncExecutor(aw, fake, BUCKET_MAP, EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                          clock=lambda: 1000.0, sleep=lambda _s: None, wing_cap=2, batch_create=True)
    return ex, aw


def _place(coid="v33-r1"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal("0.61"), client_order_id=coid, expiration_epoch=CTS + 60)


def _cancel(coid="v33-r1"):
    return V32Action(kind=ActionKind.CANCEL_REST, client_order_id=coid, order_id=None)


async def _wait_started(fake):
    for _ in range(500):
        if fake.post_started.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("create never started")


def test_cancel_during_inflight_create_is_deferred_then_executed_on_ack():
    fake = SlowCreateProxy()
    ex, aw = _aexec(fake)

    async def go():
        place = asyncio.create_task(ex.on_action_async(_place(), None, 0.0))
        await _wait_started(fake)
        cancel_events = await ex.on_action_async(_cancel(), None, 0.0)
        assert cancel_events == []                       # NOT a reported cancel; the slot stays owned
        assert "v33-r1" in ex._cancel_pending
        fake.release.set()
        return cancel_events, await place

    cancel_events, place_events = asyncio.run(go())
    aw.close()
    kinds = [type(e).__name__ for e in place_events]
    assert "OrderAck" in kinds and "OrderCancelled" in kinds, kinds
    oc = next(e for e in place_events if isinstance(e, OrderCancelled))
    assert oc.order_id == "oid-v33-r1"                   # the cancel carries the REAL venue id
    assert len(fake.deletes) == 1 and "oid-v33-r1" in fake.deletes[0]
    assert ex.rest_book["v33-r1"].status == "cancelled"   # the record is CANCELLED: nothing rests unowned
    assert not ex._cancel_pending and not ex._inflight_creates
    assert ex.stand_down_reason is None


def test_cancel_with_no_inflight_create_is_still_a_noop_cancel():
    fake = SlowCreateProxy()
    ex, aw = _aexec(fake)

    async def go():
        return await ex.on_action_async(_cancel("v33-never-placed"), None, 0.0)

    events = asyncio.run(go())
    aw.close()
    assert len(events) == 1 and isinstance(events[0], OrderCancelled) and events[0].order_id is None
    assert fake.deletes == [] and not ex._cancel_pending


def test_rejected_create_clears_the_deferred_cancel_without_a_delete():
    fake = SlowCreateProxy(reject=True)
    ex, aw = _aexec(fake)

    async def go():
        place = asyncio.create_task(ex.on_action_async(_place(), None, 0.0))
        await _wait_started(fake)
        assert await ex.on_action_async(_cancel(), None, 0.0) == []
        fake.release.set()
        return await place

    events = asyncio.run(go())
    aw.close()
    assert fake.deletes == []                            # nothing rested, nothing to cancel
    assert not ex._cancel_pending and not ex._inflight_creates
    assert not any(isinstance(e, OrderAck) for e in events)
