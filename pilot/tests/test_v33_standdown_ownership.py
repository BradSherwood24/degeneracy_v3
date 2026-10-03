"""2026-10-03 02:00Z naked fill -- executor gates C (ownership) and G (#115 residuals), with the core (gate A)
closing the loop where a racing fill must be hedged.

C(i)  the in-flight registration starts at the TOP of a create (before the pacer and the pre-flight GET),
      single AND batch path, so a cancel landing during the pre-flight is deferred (the pre-fix #27 case: its
      cancel arrived 3 ms before its POST, inside the pre-flight, and was a no-op). A slot cancelled before
      its POST is never sent; one cancelled during its POST is cancelled on the ack.
C(ii) an executor stand-down sweeps every owned order: DELETE on the cancel lane, confirm, and report
      ``OrderCancelled(order_id, client_order_id, filled, price, ticker)`` so a racing fill is hedged.
C(iii) a stood-down executor still answers TAKE_WINGS / RETRY_WING / CANCEL_REST; it refuses PLACE_REST and
      AMEND_REST (an amend is converted to a cancel of the order).
G     the batch create gets the same in-flight / deferred-cancel handling; a fill in the ack->DELETE gap of
      a deferred-cancelled slot is booked and hedged.
FAKES ONLY: no network, no proxy, no key, no holdout / seal read."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace as dr
from decimal import Decimal

from service.proxy_writer import WriteResponse
from service.v32.actions import ActionKind, V32Action
from service.v32.events import Fill, OrderAck, OrderCancelled
from service.v33 import ClockTick, WingLeg
from service.v33.async_executor import V33AsyncExecutor
from service.v33.async_writer import AsyncOrderWriter
from tests.test_v33_naked_fill_core import (B, BK, COID, FX, S_SD, S_SU, T0, _books, _healthy_empty, _kinds,
                                            _params, _run, _alarms)

EXCH = {B: 2, S_SD: 2, S_SU: 2}
CTS = FX["close_epoch"]


class FakeJournal:
    def __init__(self):
        self.records: list[tuple] = []

    def append(self, kind, obj, ts):
        self.records.append((kind, obj))

    def kinds(self):
        return [k for k, _ in self.records]


class VenueFake:
    """A ProxyWriter fake with a tiny venue: rests (GTC) rest unfilled; IOC wings fill in full; a DELETE
    cancels the order and reports ``reduced_by`` = remaining; the status GET reports ``fill_count_fp``.
    ``fill(oid, n)`` simulates a maker fill. ``preflight_gate`` / ``post_gate`` block the open-orders GET /
    the create POST on their worker thread until the test releases them."""

    def __init__(self):
        self.posts: list[dict] = []
        self.deletes: list[str] = []
        self.orders: dict[str, dict] = {}          # oid -> {"count", "filled", "status", "coid"}
        self.preflight_gate: threading.Event | None = None
        self.preflight_started = threading.Event()
        self.post_gate: threading.Event | None = None
        self.post_started = threading.Event()

    def fill(self, oid: str, n: str) -> None:
        o = self.orders[oid]
        o["filled"] += Decimal(n)

    def _one(self, o: dict) -> dict:
        coid = o.get("client_order_id")
        cnt = Decimal(str(o.get("count", "1")))
        oid = f"oid-{coid}"
        ioc = o.get("time_in_force") == "immediate_or_cancel"
        self.orders[oid] = {"count": cnt, "filled": cnt if ioc else Decimal(0),
                            "status": "executed" if ioc else "resting", "coid": coid}
        return {"client_order_id": coid, "order_id": oid,
                "fill_count": f"{cnt if ioc else Decimal('0.00')}",
                "average_fill_price": o.get("price", "0.5000")}

    def rest_post(self, path, body, headers=None):
        self.post_started.set()
        if self.post_gate is not None and "orders" not in body and body.get("time_in_force") != \
                "immediate_or_cancel":
            assert self.post_gate.wait(5.0), "test never released the create POST"
        if "orders" in body:
            if self.post_gate is not None:
                assert self.post_gate.wait(5.0), "test never released the batch POST"
            self.posts.append(body)
            return WriteResponse(200, {"orders": [self._one(o) for o in body["orders"]]}, True)
        self.posts.append(body)
        return WriteResponse(200, {"order": self._one(body)}, True)

    def rest_delete(self, path, headers=None):
        self.deletes.append(path)
        oid = path.split("/portfolio/events/orders/")[1].split("?")[0]
        o = self.orders.get(oid)
        if o is None:
            return WriteResponse(404, {"error": "not_found"}, False, "nf")
        remaining = o["count"] - o["filled"]
        o["status"] = "canceled"
        return WriteResponse(200, {"reduced_by": f"{remaining:.2f}"}, True)

    def rest_get(self, path, params=None):
        if path == "/portfolio/orders" and (params or {}).get("status") == "resting":
            self.preflight_started.set()
            if self.preflight_gate is not None:
                assert self.preflight_gate.wait(5.0), "test never released the pre-flight GET"
            return {"orders": []}
        if path.startswith("/portfolio/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            if o is None:
                return {}
            return {"order": {"order_id": oid, "status": o["status"],
                              "fill_count_fp": f"{o['filled']:.2f}",
                              "remaining_count_fp": f"{o['count'] - o['filled']:.2f}",
                              "initial_count_fp": f"{o['count']:.2f}"}}
        if path == "/portfolio/fills":
            return {"fills": []}
        return {}


def _aexec(fake):
    aw = AsyncOrderWriter(fake, wing_workers=4, cancel_workers=4, normal_workers=4)
    ex = V33AsyncExecutor(aw, fake, dict(BK), EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                          clock=lambda: 1000.0, sleep=lambda _s: None, wing_cap=2, batch_create=True)
    return ex, aw


def _place(coid, price="0.22"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(price), client_order_id=coid, expiration_epoch=CTS - 240)


def _cancel(coid, oid=None):
    return V32Action(kind=ActionKind.CANCEL_REST, client_order_id=coid, order_id=oid)


async def _wait(ev: threading.Event):
    for _ in range(500):
        if ev.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("event never set")


def _creates(fake):
    return [b for b in fake.posts if b.get("time_in_force") != "immediate_or_cancel" and "orders" not in b]


# ===========================================================================
# 3. C(ii): executor stand-down with two live rests -> two DELETEs, nothing left resting
# ===========================================================================
def test_standdown_sweep_cancels_every_live_rest_and_a_racing_fill_is_hedged():
    fake = VenueFake()
    ex, aw = _aexec(fake)
    c23, c27 = COID[23], COID[27]

    async def go():
        await ex.on_action_async(_place(c23, "0.22"), None, T0)
        await ex.on_action_async(_place(c27, "0.18"), None, T0)
        assert {r.status for r in ex.rest_book.values()} == {"live"}
        # the stand-down trips (the pre-flight invariant in the incident); before the sweep's DELETE lands,
        # the venue fills #27 (1 lot @0.18) -- the race the sweep must surface.
        ex.stand_down_reason = "rest_invariant_violation"
        fake.fill(f"oid-{c27}", "1")
        events = await ex.standdown_sweep_async(T0 + 1)
        again = await ex.standdown_sweep_async(T0 + 2)        # one-shot
        return events, again

    events, again = asyncio.run(go())
    aw.close()
    assert again == []
    assert sorted(fake.deletes) == sorted(f"/portfolio/events/orders/oid-{c}?exchange_index=2"
                                          for c in (c23, c27))
    assert all(o["status"] == "canceled" for o in fake.orders.values())       # zero live rests left
    assert all(r.cancel_confirmed_ts is not None for r in ex.rest_book.values())
    by = {e.client_order_id: e for e in events if isinstance(e, OrderCancelled)}
    assert set(by) == {c23, c27}
    assert by[c23].filled_count_before_cancel == 0 and by[c23].order_id == f"oid-{c23}"
    assert by[c27].filled_count_before_cancel == Decimal(1)
    assert by[c27].price == Decimal("0.18") and by[c27].market_ticker == B
    assert "standdown_sweep" in ex.journal.kinds() and "standdown_sweep_done" in ex.journal.kinds()

    # the core (stood down, ladder already cleared -- it lost these orders) books + hedges the racing fill.
    p = _params()
    st = _healthy_empty(p, T0 + 1)
    st, acts = _run(p, st, _books(T0 + 1) + list(events))
    assert _alarms(acts, "orphan_rung_fill_hedged")
    st, acts = _run(p, st, _books(T0 + 1.3) + [ClockTick(T0 + 1.3)])
    takes = _kinds(acts, ActionKind.TAKE_WINGS)
    assert [t.count for t in takes] == [Decimal(1)]
    assert [(lg.ticker, lg.side) for lg in takes[0].legs] == [(S_SD, "yes"), (S_SU, "no")]


def test_standdown_sweep_defers_creates_still_in_flight():
    """A create whose POST is in flight when the sweep runs is cancelled on its ack (never left unowned)."""
    fake = VenueFake()
    fake.post_gate = threading.Event()
    ex, aw = _aexec(fake)

    async def go():
        place = asyncio.create_task(ex.on_action_async(_place(COID[23]), None, T0))
        await _wait(fake.post_started)
        ex.stand_down_reason = "rest_invariant_violation"
        sweep = await ex.standdown_sweep_async(T0 + 1)
        assert sweep == [] and COID[23] in ex._cancel_pending
        fake.post_gate.set()
        return await place

    events = asyncio.run(go())
    aw.close()
    assert [type(e).__name__ for e in events] == ["OrderAck", "OrderCancelled"]
    assert fake.deletes == [f"/portfolio/events/orders/oid-{COID[23]}?exchange_index=2"]
    assert not ex._cancel_pending and not ex._inflight_creates


def test_core_cancel_racing_the_sweep_is_not_a_second_delete():
    fake = VenueFake()
    ex, aw = _aexec(fake)

    async def go():
        await ex.on_action_async(_place(COID[23]), None, T0)
        ex.stand_down_reason = "rest_invariant_violation"
        return await asyncio.gather(ex.standdown_sweep_async(T0 + 1),
                                    ex.on_action_async(_cancel(COID[23], f"oid-{COID[23]}"), None, T0 + 1))

    sweep_events, core_events = asyncio.run(go())
    aw.close()
    assert len(fake.deletes) == 1
    assert len([e for e in sweep_events + core_events if isinstance(e, OrderCancelled)]) == 1


# ===========================================================================
# 4. C(i) / G: the in-flight guard covers the pre-flight (single and batch)
# ===========================================================================
def test_single_cancel_during_preflight_is_deferred_and_the_post_never_sent():
    fake = VenueFake()
    fake.preflight_gate = threading.Event()
    ex, aw = _aexec(fake)

    async def go():
        place = asyncio.create_task(ex.on_action_async(_place(COID[27], "0.18"), None, T0))
        await _wait(fake.preflight_started)
        assert COID[27] in ex._inflight_creates                # registered BEFORE the pre-flight
        cancel_events = await ex.on_action_async(_cancel(COID[27]), None, T0)
        assert cancel_events == [] and COID[27] in ex._cancel_pending
        fake.preflight_gate.set()
        return await place

    events = asyncio.run(go())
    aw.close()
    assert _creates(fake) == [] and fake.deletes == []      # nothing reached the venue: nothing to own
    assert len(events) == 1 and isinstance(events[0], OrderCancelled)
    assert events[0].order_id is None and events[0].client_order_id == COID[27]
    assert "place_skipped_before_post" in ex.journal.kinds()
    assert not ex._cancel_pending and not ex._inflight_creates


def test_batch_cancel_during_preflight_is_deferred_and_that_slot_never_sent():
    fake = VenueFake()
    fake.preflight_gate = threading.Event()
    ex, aw = _aexec(fake)

    async def go():
        batch = asyncio.create_task(ex.place_batch_async([_place(COID[23], "0.22"),
                                                          _place(COID[24], "0.21")], T0))
        await _wait(fake.preflight_started)
        assert {COID[23], COID[24]} <= ex._inflight_creates     # whole chunk registered at the top
        assert await ex.on_action_async(_cancel(COID[23]), None, T0) == []
        fake.preflight_gate.set()
        return await batch

    events = asyncio.run(go())
    aw.close()
    sent = [b.get("client_order_id") for b in fake.posts]
    assert sent == [COID[24]]                                  # #23 never sent; #24 sent and acked
    oc = [e for e in events if isinstance(e, OrderCancelled)]
    assert [(e.order_id, e.client_order_id) for e in oc] == [(None, COID[23])]
    assert any(isinstance(e, OrderAck) and e.client_order_id == COID[24] for e in events)
    assert fake.deletes == [] and not ex._cancel_pending and not ex._inflight_creates


def test_batch_cancel_during_post_is_executed_on_ack():
    fake = VenueFake()
    fake.post_gate = threading.Event()
    ex, aw = _aexec(fake)

    async def go():
        batch = asyncio.create_task(ex.place_batch_async([_place(COID[23], "0.22"),
                                                          _place(COID[24], "0.21")], T0))
        await _wait(fake.post_started)
        assert await ex.on_action_async(_cancel(COID[23]), None, T0) == []
        fake.post_gate.set()
        return await batch

    events = asyncio.run(go())
    aw.close()
    assert fake.deletes == [f"/portfolio/events/orders/oid-{COID[23]}?exchange_index=2"]
    oc = [e for e in events if isinstance(e, OrderCancelled)]
    assert [(e.order_id, e.client_order_id) for e in oc] == [(f"oid-{COID[23]}", COID[23])]
    assert ex.rest_book[COID[23]].status == "cancelled" and ex.rest_book[COID[24]].status == "live"
    assert not ex._cancel_pending and not ex._inflight_creates


def test_g_fill_in_the_ack_to_delete_gap_of_a_deferred_cancel_is_hedged():
    """G: the slot was cancelled while its create was in flight; the venue fills it between the ack and the
    DELETE. The cancel-confirm reports the fill with coid / price / ticker; the core -- which already
    dropped that pending slot -- books it as an owned orphan and takes the wings."""
    fake = VenueFake()
    fake.post_gate = threading.Event()
    ex, aw = _aexec(fake)
    orig_delete = fake.rest_delete

    def fill_then_delete(path, headers=None):        # the fill lands in the ack -> DELETE gap
        oid = path.split("/portfolio/events/orders/")[1].split("?")[0]
        fake.fill(oid, "0.60")
        return orig_delete(path, headers)

    fake.rest_delete = fill_then_delete

    async def go():
        place = asyncio.create_task(ex.on_action_async(_place(COID[23], "0.22"), None, T0))
        await _wait(fake.post_started)
        assert await ex.on_action_async(_cancel(COID[23]), None, T0) == []
        fake.post_gate.set()
        return await place

    events = asyncio.run(go())
    aw.close()
    oc = next(e for e in events if isinstance(e, OrderCancelled))
    assert oc.filled_count_before_cancel == Decimal("0.60") and oc.client_order_id == COID[23]

    p = _params()
    st = _healthy_empty(p, T0 + 1)                 # the core cleared the pending slot when it cancelled
    st, acts = _run(p, st, _books(T0 + 1) + [e for e in events if not isinstance(e, OrderAck)])
    assert _alarms(acts, "orphan_rung_fill_hedged")
    st, acts = _run(p, st, _books(T0 + 1.3) + [ClockTick(T0 + 1.3)])
    assert [t.count for t in _kinds(acts, ActionKind.TAKE_WINGS)] == [Decimal("0.60")]


# ===========================================================================
# 5. C(iii): a stood-down executor hedges and cancels; it refuses new rests and amends
# ===========================================================================
def _wing_state(count="1"):
    p = _params()
    st = _healthy_empty(p, T0)
    legs = (WingLeg(S_SD, "yes", Decimal(count), Decimal("0.82"), "v33-wy", batch=0),
            WingLeg(S_SU, "no", Decimal(count), Decimal("0.88"), "v33-wn", batch=0))
    return dr(st, wing_legs=legs)


def test_stood_down_executor_takes_and_retries_wings():
    fake = VenueFake()
    ex, aw = _aexec(fake)
    ex.stand_down_reason = "rest_invariant_violation"
    st = _wing_state()

    async def go():
        take = await ex.on_action_async(V32Action(kind=ActionKind.TAKE_WINGS, count=1), st, T0)
        # a RETRY of a leg the venue has not filled yet (a different batch, so nothing is pre-filled).
        retry_state = dr(st, wing_legs=(dr(st.wing_legs[0], client_order_id="v33-wy2", batch=1),))
        retry = await ex.on_action_async(V32Action(kind=ActionKind.RETRY_WING, count=1), retry_state, T0)
        return take, retry

    take, retry = asyncio.run(go())
    aw.close()
    assert sorted((f.client_order_id, f.count) for f in take if isinstance(f, Fill)) == [
        ("v33-wn", Decimal(1)), ("v33-wy", Decimal(1))]
    assert [(f.client_order_id, f.count) for f in retry if isinstance(f, Fill)] == [("v33-wy2", Decimal(1))]


def test_stood_down_executor_cancels_but_refuses_place_and_amend():
    fake = VenueFake()
    ex, aw = _aexec(fake)

    async def go():
        await ex.on_action_async(_place(COID[23]), None, T0)
        await ex.on_action_async(_place(COID[24], "0.21"), None, T0)
        n_posts = len(fake.posts)
        ex.stand_down_reason = "rest_invariant_violation"
        cancel = await ex.on_action_async(_cancel(COID[23], f"oid-{COID[23]}"), None, T0 + 1)
        place = await ex.on_action_async(_place(COID[25], "0.20"), None, T0 + 1)
        batch = await ex.place_batch_async([_place(COID[26], "0.19"), _place(COID[28], "0.17")], T0 + 1)
        amend = await ex.on_action_async(
            V32Action(kind=ActionKind.AMEND_REST, order_id=f"oid-{COID[24]}", ticker=B, side="no",
                      action="buy", count=1, price=Decimal("0.10"), client_order_id=COID[24],
                      updated_client_order_id="v33-amended"), None, T0 + 1)
        return n_posts, cancel, place, batch, amend

    n_posts, cancel, place, batch, amend = asyncio.run(go())
    aw.close()
    assert len(fake.posts) == n_posts                          # no create, no amend POST after stand-down
    assert [(e.order_id, e.client_order_id) for e in cancel] == [(f"oid-{COID[23]}", COID[23])]
    assert [(e.order_id, e.client_order_id) for e in place] == [(None, COID[25])]
    assert [(e.order_id, e.client_order_id) for e in batch] == [(None, COID[26]), (None, COID[28])]
    # the refused amend became a CANCEL of the order (the stand-down's intent), resolved for the core.
    assert [(e.order_id, e.client_order_id) for e in amend] == [(f"oid-{COID[24]}", COID[24])]
    assert sorted(fake.deletes) == sorted(f"/portfolio/events/orders/oid-{c}?exchange_index=2"
                                          for c in (COID[23], COID[24]))
    k = ex.journal.kinds()
    assert k.count("place_refused_stood_down") == 3 and "amend_refused_stood_down" in k
    assert ex.places_refused_stood_down == 3 and ex.amends_refused_stood_down == 1


# ===========================================================================
# DRIVER: the stand-down sweep is scheduled once and its events re-enter decide; a ws fill on an
# executor-owned order reaches the stood-down core and the stood-down executor takes the wings.
# ===========================================================================
import time  # noqa: E402

import service.run_v33 as RUN  # noqa: E402
from tests.test_v33_run import J as DriverJ  # noqa: E402


async def _drain(pred, timeout=4.0):
    t0 = time.monotonic()
    while not pred():
        await asyncio.sleep(0.002)
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition never met")


def _wing_posts(fake):
    out = []
    for b in fake.posts:
        for o in (b["orders"] if "orders" in b else [b]):
            if o.get("time_in_force") == "immediate_or_cancel":
                out.append(o)
    return out


def test_driver_executor_standdown_sweeps_and_a_ws_fill_while_stood_down_is_hedged():
    fake = VenueFake()
    p = _params()
    c23, c27 = COID[23], COID[27]

    async def go():
        aw = AsyncOrderWriter(fake, wing_workers=4, cancel_workers=4, normal_workers=4)
        ex = V33AsyncExecutor(aw, fake, dict(BK), EXCH, FakeJournal(), CTS, 300, k_rungs=11,
                              clock=lambda: 1000.0, sleep=lambda _s: None, wing_cap=2)
        try:
            # the executor owns #23 / #27 at the venue; the (stood-down) core has an empty ladder.
            await ex.on_action_async(_place(c23, "0.22"), None, T0)
            await ex.on_action_async(_place(c27, "0.18"), None, T0)
            st = dr(_healthy_empty(p, T0 + 20), shakedown=False)
            drv = RUN.V33Driver(p, st, DriverJ(), ex, dry_sim=False, clock=lambda: 0.0, async_writer=aw)
            # 1) a ws fill on #23 (0.40) lands BEFORE the stand-down sweep: it reaches the stood-down core.
            for bu in _books(T0 + 21.4):
                drv.on_book_update(bu.market_ticker, bu.top, T0 + 21.4)
            drv.on_fill(B, {"client_order_id": c23, "order_id": f"oid-{c23}", "trade_id": "t-1",
                            "count_fp": "0.40", "yes_price_dollars": "0.78", "purchased_side": "no"},
                        T0 + 21.468)
            assert sum(f.count for f in drv.state.rest_fills) == Decimal("0.40")
            # 2) the executor stands down; the driver schedules the ONE sweep; #27 fills before its DELETE.
            fake.fill(f"oid-{c27}", "1")
            ex.stand_down_reason = "rest_invariant_violation"
            drv._apply_executor_standdown(T0 + 22)
            drv._apply_executor_standdown(T0 + 22)                 # idempotent: still one sweep
            await _drain(lambda: len(fake.deletes) == 2
                         and sum(f.count for f in drv.state.rest_fills) == Decimal("1.40"))
            # 3) clock past the coalesce window -> the stood-down core emits TAKE_WINGS, the stood-down
            #    executor SENDS them.
            for bu in _books(T0 + 22.5):
                drv.on_book_update(bu.market_ticker, bu.top, T0 + 22.5)
            drv.on_clock_tick(T0 + 22.6)
            await _drain(lambda: sum(Decimal(o["count"]) for o in _wing_posts(fake)) >= Decimal("2.80"))
            return drv, ex
        finally:
            aw.close()

    drv, ex = asyncio.run(go())
    assert drv.state.stood_down and drv._standdown_sweep_scheduled
    assert ex.journal.kinds().count("standdown_sweep") == 1
    assert all(o["status"] == "canceled" for oid, o in fake.orders.items() if oid.startswith("oid-v33-2026"))
    alarms = [o for k, o in drv.journal.recs if k == "alarm"]
    hedged = [a for a in alarms if a.get("alarm") == "orphan_rung_fill_hedged"]
    assert [(a["client_order_id"], str(a["count"])) for a in hedged] == [(c23, "0.40"), (c27, "1")]
    # the core was ALREADY stood down (no executor_standdown flip to journal) -- the sweep still ran: the
    # executor's ownership does not depend on which side stood down first.
    assert not any(a.get("alarm") == "executor_standdown" for a in alarms)
    # wings on the fill bucket's strikes, 1.40 lots per side (0.40 + 1.00), no new rest after stand-down.
    by_ticker: dict[str, Decimal] = {}
    for o in _wing_posts(fake):
        by_ticker[o["ticker"]] = by_ticker.get(o["ticker"], Decimal(0)) + Decimal(o["count"])
    assert by_ticker == {S_SD: Decimal("1.40"), S_SU: Decimal("1.40")}
    assert len(_creates(fake)) == 2                                   # only the two pre-stand-down rests
