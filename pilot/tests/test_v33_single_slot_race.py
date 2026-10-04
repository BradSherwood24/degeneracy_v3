"""V3.3 SINGLE-SLOT CANCEL/RE-PLACE RACE fix (2026-10-04; live trips 02:00Z / 08:00Z / 19:00Z).

Defect: V3.3 rests one maker NO order per rung. When the pin moves, the core may CANCEL a rung at price P
and, a few ms later, want to PLACE a replacement at the SAME P (per-rung convergence / shrink->return),
NOT a cancel-all. The old order's DELETE confirm lags ~100-800 ms, so:
  * (core) the place fired at P while the cancel was unconfirmed, and
  * (executor) the pre-flight invariant GET saw the old order still resting at P -> ``dup_price`` ->
    ``rest_invariant_violation`` -> stand-down -> no quoting until close.

Two independent guards, both pinned here (belt and braces):
  * part (b) CORE: ``cancel_pending_px`` HOLDS a price whose previous order's cancel is unconfirmed; no
    PLACE_REST (nor roll-to) at that price until the matching OrderCancelled / fill clears it.
  * part (a) EXECUTOR: the pre-flight invariant EXCLUDES any venue order with a DELETE of ours in flight
    (``_cancel_oids_inflight``) from count_resting / the dup test / strays. A real stray still trips.

FAKES ONLY; no network, no proxy, no key material, no holdout / sealed dates touched.
"""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, V32Action
from service.v32.events import OrderCancelled as _ExecOrderCancelled
from service.v32.executor import OPEN_ORDERS_PATH
from service.v33 import BookUpdate, ClockTick, OrderCancelled, V33State
from service.v33.core import (RestOrder, _arm_cancel_pending, _clear_cancel_pending,
                              _held_cancel_prices)

# --- part (b) harness (reuse the pure-core ladder helpers) ---
from tests.test_v33_core import (STK_SD, STK_SU, T, _bring_up_ladder, _feed, _params, _prices,
                                 _sd, _state, _top)
# --- part (a) harness (reuse the async executor fakes) ---
from tests.test_v33_async_fractional import B, FracAsyncProxy, _aexec


# ===========================================================================
# PART (b) — CORE: the price-held gate
# ===========================================================================
def _held(st: V33State) -> set[Decimal]:
    return set(st.cancel_pending_px.values())


def test_single_slot_race_holds_replace_until_cancel_confirms():
    """The 02:00Z shape: shrink-CANCEL the top rung at P, n_top RETURNS so P is a target again -> the core
    must NOT emit PLACE_REST at P until OrderCancelled(P's order) arrives; then it re-places."""
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.48"))
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)                     # ladder 0.50 / 0.49 / 0.48
    assert _prices(st) == [Decimal("0.50"), Decimal("0.49"), Decimal("0.48")]
    top_id = max(st.ladder, key=lambda o: o.price).order_id  # the 0.50 rung

    # n_top -> 0.49: 0.50 is OUT with no vacant slot (bottom pinned at n_min) -> SHRINK CANCEL at 0.50.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 1 and cancels[0].order_id == top_id
    assert Decimal("0.50") in _held(st), "the cancelled price must be HELD until its confirm"

    # n_top RETURNS to 0.50 before the cancel confirms -> 0.50 is a target again, but HELD -> no re-place.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 2))
    st, a2 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 2))      # n_top -> 0.50
    st, a3 = _feed(p, st, ClockTick(now + 2.001))
    placed_held = [a for a in (a2 + a3)
                   if a.kind == ActionKind.PLACE_REST and a.price == Decimal("0.50")]
    assert not placed_held, "must NOT re-place at a price whose cancel is still unconfirmed"
    assert Decimal("0.50") in _held(st)

    # the cancel CONFIRMS -> the price clears -> the tail convergence re-places the slot at 0.50.
    st, a4 = _feed(p, st, OrderCancelled(top_id, now + 2.1))
    assert Decimal("0.50") not in _held(st), "the confirm must release the held price"
    placed_after = [a for a in a4 if a.kind == ActionKind.PLACE_REST and a.price == Decimal("0.50")]
    assert placed_after, "after the confirm, the slot re-places at 0.50"


def test_single_slot_race_fill_before_confirm_releases_the_price():
    """If the cancelled rung FILLS before the cancel confirms, the fill (order leaving the book) must also
    release the held price (so the slot is not wedged by a cancel that will never confirm)."""
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.48"))
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    top = max(st.ladder, key=lambda o: o.price)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    assert Decimal("0.50") in _held(st)
    # a fill on the cancelling order's id/coid arrives before the cancel confirm (orphan path; the rung
    # was already dropped from the ladder by the shrink). The order leaving the book must release the price.
    from service.v32.events import Fill
    st, _ = _feed(p, st, Fill(top.order_id, top.client_order_id, Decimal(1), Decimal("0.50"),
                              "no", now + 1.05))
    assert Decimal("0.50") not in _held(st), "a fill on the order must release its held price"


def test_cancel_pending_prices_are_independent():
    """Two DIFFERENT prices do not block each other: clearing one leaves the other held, and only the
    matching order_id clears a price."""
    p = _params()
    st = _state(p)
    o1 = RestOrder(client_order_id="v33-a", order_id="OID-a", price=Decimal("0.50"),
                   count=Decimal(1), placed_ts=0.0, live=True, pending=False, bucket_Sd=0,
                   rung=0, E_rung=Decimal("0.05"))
    o2 = RestOrder(client_order_id="v33-b", order_id="OID-b", price=Decimal("0.48"),
                   count=Decimal(1), placed_ts=0.0, live=True, pending=False, bucket_Sd=0,
                   rung=2, E_rung=Decimal("0.07"))
    st = _arm_cancel_pending(st, o1)
    st = _arm_cancel_pending(st, o2)
    assert _held_cancel_prices(st) == {Decimal("0.50"), Decimal("0.48")}
    # clearing OID-a frees 0.50 only; 0.48 stays held.
    st = _clear_cancel_pending(st, "OID-a")
    assert _held_cancel_prices(st) == {Decimal("0.48")}
    # a non-matching id is a no-op; the matching id clears the last price.
    st = _clear_cancel_pending(st, "OID-zzz")
    assert _held_cancel_prices(st) == {Decimal("0.48")}
    st = _clear_cancel_pending(st, "OID-b")
    assert _held_cancel_prices(st) == set()


def test_pending_order_cancel_arms_nothing():
    """A cancel of a PENDING order (no venue id yet) arms no held price -- nothing rests at the venue."""
    p = _params()
    st = _state(p)
    o = RestOrder(client_order_id="v33-p", order_id=None, price=Decimal("0.50"),
                  count=Decimal(1), placed_ts=0.0, live=False, pending=True, bucket_Sd=0,
                  rung=0, E_rung=Decimal("0.05"))
    assert _arm_cancel_pending(st, o).cancel_pending_px == {}


# ===========================================================================
# PART (a) — EXECUTOR: exclude an in-flight cancel from the pre-flight invariant
# ===========================================================================
PRICE = Decimal("0.61")


class SlowDeleteProxy(FracAsyncProxy):
    """create POSTs normally; the DELETE BLOCKS on a worker thread until the test releases it (so the oid
    sits in ``_cancel_oids_inflight`` while a concurrent place runs). ``resting`` drives the pre-flight GET."""

    def __init__(self):
        super().__init__(cap=100)
        self.delete_started = threading.Event()
        self.release = threading.Event()
        self.resting: list[dict] = []
        self.get_map = {OPEN_ORDERS_PATH: lambda: {"orders": list(self.resting)}}

    def rest_delete(self, path, headers=None):
        self.delete_started.set()
        assert self.release.wait(5.0), "test did not release the DELETE"
        return WriteResponse(200, {"reduced_by": "0.00"}, True)


def _place(coid: str) -> V32Action:
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=PRICE, client_order_id=coid, expiration_epoch=10**12)


def _cancel(coid: str) -> V32Action:
    return V32Action(kind=ActionKind.CANCEL_REST, client_order_id=coid, order_id=None)


def _resting_entry(ex) -> dict:
    rec = ex.rest_book["v33-r1"]
    return {"client_order_id": "v33-r1", "order_id": rec.order_id, "ticker": B,
            "exchange_index": 2, "status": "resting"}


async def _await_event(ev: threading.Event):
    for _ in range(500):
        if ev.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("event never fired")


def test_inflight_cancel_excluded_place_at_same_price_posts():
    fake = SlowDeleteProxy()
    ex, aw = _aexec(fake, k=11, wing_cap=2)

    async def go():
        await ex.on_action_async(_place("v33-r1"), None, 0.0)  # venue empty -> proceeds, rests oid-v33-r1
        fake.resting = [_resting_entry(ex)]                    # the venue now LISTS it resting
        ctask = asyncio.create_task(ex.on_action_async(_cancel("v33-r1"), None, 0.0))
        await _await_event(fake.delete_started)                # DELETE now in flight: oid in-flight set
        ev2 = await ex.on_action_async(_place("v33-r2"), None, 0.0)  # SAME price, concurrent
        fake.release.set()
        await ctask
        return ev2

    try:
        ev2 = asyncio.run(go())
    finally:
        aw.close()

    # the place at the same price PROCEEDED (not a violation): an OrderAck, no stand-down.
    kinds = [type(e).__name__ for e in ev2]
    assert "OrderAck" in kinds, kinds
    assert ex.stand_down_reason is None
    assert ex.rest_invariant_cancelling_excluded >= 1
    assert ex.rest_invariant_violations == 0
    assert "v33-r2" in ex.rest_book and ex.rest_book["v33-r2"].order_id is not None
    kinds_j = [k for k, _ in ex.journal.records]
    assert "rest_invariant_cancelling_excluded" in kinds_j
    assert "rest_invariant_violation" not in kinds_j


def test_real_stray_at_same_price_still_trips_when_no_cancel_in_flight():
    """Regression guard: with NO DELETE of ours in flight, an order still resting at the place price is a
    genuine dup -> the invariant must still trip (the fix must not blind the belt)."""
    fake = SlowDeleteProxy()
    ex, aw = _aexec(fake, k=11, wing_cap=2)

    async def go():
        await ex.on_action_async(_place("v33-r1"), None, 0.0)
        fake.resting = [_resting_entry(ex)]                    # listed resting, but NO cancel in flight
        return await ex.on_action_async(_place("v33-r2"), None, 0.0)  # same price -> must trip

    try:
        ev2 = asyncio.run(go())
    finally:
        aw.close()

    assert any(isinstance(e, _ExecOrderCancelled) for e in ev2), ev2
    assert ex.stand_down_reason == "rest_invariant_violation"
    assert ex.rest_invariant_violations == 1
    assert ex.rest_invariant_dup_price == 1
    assert ex.rest_invariant_cancelling_excluded == 0
