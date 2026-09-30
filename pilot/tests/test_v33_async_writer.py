"""test_v33_async_writer.py — the off-loop V3.3 order writer + async executor.

Proves the build brief's guarantees WITHOUT a network (a fake in-process ProxyWriter with deterministic,
controllable latency):
  * the event loop is NEVER blocked while a slow write is in flight (the measured cause — a blocked loop
    freezing the feed — is gone);
  * per-class PRIORITY lanes: a wing IOC take dispatched during an in-flight cancel-all runs concurrently,
    never waiting behind the queued cancels;
  * per-slot FIFO: a create for a slot never overlaps that slot's cancel;
  * the X-DV3-Class header rides every write;
  * writer stats (queue depth / latency / class mix) are recorded;
  * (executor-level tests added below) never more than K rests at the venue under random latencies; an
    unknown outcome is never re-placed over; a 429 on a wing is retried on its own lane; dry mode unchanged.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from service.proxy_writer import WriteResponse
from service.v33.async_writer import (
    CLASS_CANCEL,
    CLASS_POLL,
    CLASS_REST,
    CLASS_WING,
    AsyncOrderWriter,
)


# ---------------------------------------------------------------------------
# A fake in-process ProxyWriter with controllable, deterministic latency.
# ---------------------------------------------------------------------------
@dataclass
class _Call:
    verb: str
    path: str
    body: dict | None
    headers: dict | None
    klass: str | None
    t_start: float
    t_end: float


class FakeProxyWriter:
    """Mirrors ``ProxyWriter``: rest_post/rest_delete -> WriteResponse, rest_get -> dict. Each call runs on
    a worker thread (via the writer's run_in_executor) and can block on a per-verb gate so a test controls
    concurrency deterministically. Records every call with wall timestamps + the headers it received."""

    def __init__(self, *, post_latency: float = 0.0, delete_latency: float = 0.0,
                 get_latency: float = 0.0) -> None:
        self.calls: list[_Call] = []
        self._lock = threading.Lock()
        self.post_latency = post_latency
        self.delete_latency = delete_latency
        self.get_latency = get_latency
        self._gates: dict[str, threading.Event] = {}
        self._oid = 0
        self.post_queue: list[WriteResponse] = []
        self.delete_queue: list[WriteResponse] = []
        self.get_map: dict[str, Any] = {}

    def gate(self, key: str) -> threading.Event:
        ev = self._gates.get(key)
        if ev is None:
            ev = threading.Event()
            self._gates[key] = ev
        return ev

    def _wait_gate(self, key: str) -> None:
        ev = self._gates.get(key)
        if ev is not None:
            ev.wait(timeout=5.0)

    def _record(self, verb, path, body, headers, klass, t0) -> None:
        with self._lock:
            self.calls.append(_Call(verb, path, body, headers, klass, t0, time.monotonic()))

    def rest_post(self, path, body, headers=None):
        t0 = time.monotonic()
        klass = (headers or {}).get("X-DV3-Class")
        self._wait_gate(f"post:{klass}")
        self._wait_gate(f"post:{body.get('client_order_id')}")
        if self.post_latency:
            time.sleep(self.post_latency)
        self._record("post", path, body, headers, klass, t0)
        if self.post_queue:
            return self.post_queue.pop(0)
        self._oid += 1
        return WriteResponse(200, {"order": {"order_id": f"oid-{self._oid}",
                                             "client_order_id": body.get("client_order_id"),
                                             "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path, headers=None):
        t0 = time.monotonic()
        klass = (headers or {}).get("X-DV3-Class")
        self._wait_gate(f"delete:{klass}")
        if self.delete_latency:
            time.sleep(self.delete_latency)
        self._record("delete", path, None, headers, klass, t0)
        if self.delete_queue:
            return self.delete_queue.pop(0)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        t0 = time.monotonic()
        if self.get_latency:
            time.sleep(self.get_latency)
        self._record("get", path, None, None, CLASS_POLL, t0)
        v = self.get_map.get(path)
        return v() if callable(v) else (v if v is not None else {})


def _writer(fake: FakeProxyWriter) -> AsyncOrderWriter:
    return AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)


# ---------------------------------------------------------------------------
# 1. verbs mirror ProxyWriter + class header rides every write
# ---------------------------------------------------------------------------
def test_verbs_mirror_proxywriter_and_carry_class_header():
    fake = FakeProxyWriter()

    async def go():
        w = _writer(fake)
        try:
            r = await w.post("/portfolio/events/orders", {"client_order_id": "v33-1"}, klass=CLASS_REST)
            assert isinstance(r, WriteResponse) and r.ok and r.status_code == 200
            d = await w.delete("/portfolio/events/orders/oid-1?exchange_index=2", klass=CLASS_CANCEL)
            assert isinstance(d, WriteResponse) and d.ok
            g = await w.get("/portfolio/orders", {"status": "resting"}, klass=CLASS_POLL)
            assert isinstance(g, dict)
        finally:
            w.close()

    asyncio.run(go())
    posts = [c for c in fake.calls if c.verb == "post"]
    deletes = [c for c in fake.calls if c.verb == "delete"]
    assert posts[0].headers.get("X-DV3-Class") == CLASS_REST
    assert deletes[0].headers.get("X-DV3-Class") == CLASS_CANCEL


# ---------------------------------------------------------------------------
# 2. the loop is NEVER blocked while a slow write is in flight (the measured cause)
# ---------------------------------------------------------------------------
def test_loop_not_blocked_during_slow_write():
    fake = FakeProxyWriter()
    fake.gate("post:v33-slow")  # the POST worker will block until the test opens the gate

    async def go():
        w = _writer(fake)
        try:
            ticks = 0
            slow = asyncio.ensure_future(
                w.post("/portfolio/events/orders", {"client_order_id": "v33-slow"}, klass=CLASS_REST))
            # while the POST worker is parked on the gate, the loop must keep running: a plain loop
            # coroutine advances many times (a blocked loop would advance 0 times).
            for _ in range(50):
                await asyncio.sleep(0)
                ticks += 1
            assert not slow.done()          # the write is genuinely still in flight
            assert ticks == 50              # and the loop made progress the whole time (never blocked)
            assert w.inflight == 1
            fake.gate("post:v33-slow").set()
            r = await slow
            assert r.ok
        finally:
            w.close()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 3. wing priority lane: a wing runs concurrently with an in-flight cancel-all
# ---------------------------------------------------------------------------
def test_wing_never_waits_behind_a_cancel_all():
    # all 11 cancels of a cancel-all block on the cancel gate; a wing submitted after them must still
    # complete (it draws from the dedicated WING pool, never the CANCEL pool).
    fake = FakeProxyWriter()
    fake.gate("delete:cancel")   # every cancel parks here until the test releases it

    async def go():
        w = _writer(fake)
        try:
            cancels = [asyncio.ensure_future(
                w.delete(f"/portfolio/events/orders/oid-{i}?exchange_index=2", klass=CLASS_CANCEL))
                for i in range(11)]
            await asyncio.sleep(0)
            # the wing is submitted AFTER the 11 cancels are already queued/in-flight and blocked.
            wing = asyncio.ensure_future(
                w.post("/portfolio/events/orders", {"client_order_id": "v33-wc-1"}, klass=CLASS_WING))
            wr = await asyncio.wait_for(wing, timeout=3.0)   # completes despite the blocked cancel-all
            assert wr.ok
            assert not any(c.done() for c in cancels)        # the cancels are STILL blocked
            fake.gate("delete:cancel").set()
            await asyncio.gather(*cancels)
        finally:
            w.close()

    asyncio.run(go())
    wing_posts = [c for c in fake.calls if c.verb == "post"]
    assert wing_posts and wing_posts[0].klass == CLASS_WING


# ---------------------------------------------------------------------------
# 4. per-slot FIFO: a create for a slot never overlaps that slot's cancel
# ---------------------------------------------------------------------------
def test_per_slot_cancel_then_create_serialized():
    fake = FakeProxyWriter()
    fake.gate("delete:cancel")  # hold the slot's cancel open

    async def go():
        w = _writer(fake)
        try:
            cancel = asyncio.ensure_future(
                w.delete("/portfolio/events/orders/oid-1?exchange_index=2",
                         klass=CLASS_CANCEL, slot="rung-3"))
            await asyncio.sleep(0)
            create = asyncio.ensure_future(
                w.post("/portfolio/events/orders", {"client_order_id": "v33-3b"},
                       klass=CLASS_REST, slot="rung-3"))
            await asyncio.sleep(0.02)
            # the same-slot create must NOT have started while the cancel holds the slot lock.
            assert not create.done()
            assert not any(c.verb == "post" for c in fake.calls)
            fake.gate("delete:cancel").set()
            await asyncio.gather(cancel, create)
        finally:
            w.close()

    asyncio.run(go())
    # the cancel's DELETE was recorded strictly before the create's POST (per-slot FIFO held).
    verbs = [c.verb for c in fake.calls]
    assert verbs.index("delete") < verbs.index("post")


# ---------------------------------------------------------------------------
# 5. distinct slots run concurrently (no false serialization)
# ---------------------------------------------------------------------------
def test_distinct_slots_run_concurrently():
    fake = FakeProxyWriter(post_latency=0.05)

    async def go():
        w = _writer(fake)
        try:
            t0 = time.monotonic()
            await asyncio.gather(*[
                w.post("/portfolio/events/orders", {"client_order_id": f"v33-{i}"},
                       klass=CLASS_REST, slot=f"rung-{i}")
                for i in range(6)])
            elapsed = time.monotonic() - t0
            # 6 x 50 ms concurrent ~ 50-150 ms, NOT 300 ms (which would mean they serialized).
            assert elapsed < 0.28, elapsed
        finally:
            w.close()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# 6. stats: submitted / completed / latency / class mix
# ---------------------------------------------------------------------------
def test_writer_stats_recorded():
    fake = FakeProxyWriter(post_latency=0.01)

    async def go():
        w = _writer(fake)
        try:
            await asyncio.gather(
                w.post("/portfolio/events/orders", {"client_order_id": "v33-1"}, klass=CLASS_REST),
                w.post("/portfolio/events/orders", {"client_order_id": "v33-wc-1"}, klass=CLASS_WING),
                w.delete("/portfolio/events/orders/oid-1?exchange_index=2", klass=CLASS_CANCEL),
            )
            return w.stats.summary()
        finally:
            w.close()

    summ = asyncio.run(go())
    assert summ["submitted"] == 3 and summ["completed"] == 3
    assert summ["by_class"][CLASS_REST] == 1 and summ["by_class"][CLASS_WING] == 1
    assert summ["by_class"][CLASS_CANCEL] == 1
    assert summ["latency_ms_max"] is not None and summ["latency_ms_max"] >= 0.0
    assert summ["max_inflight"] >= 1


# ===========================================================================
# V33AsyncExecutor — the off-loop armed executor (async twins of the sync paths)
# ===========================================================================
import random  # noqa: E402

from service.v32.actions import ActionKind, V32Action  # noqa: E402
from service.v32.events import OrderAck, OrderAmended, OrderCancelled  # noqa: E402
from service.v32.executor import OPEN_ORDERS_PATH, RestRecord  # noqa: E402
from service.v33 import V33State, WingLeg, load_v33_params  # noqa: E402
from service.v33.async_executor import V33AsyncExecutor, cancel_stale_open_orders_async  # noqa: E402
from tests.test_v33_executor import B, BUCKET_MAP, CLOSE, CTS, EXCH, FakeJournal  # noqa: E402


def _aexec(fake, journal=None, *, k=11, wing_cap=2, batch_create=False):
    aw = AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)
    ex = V33AsyncExecutor(aw, fake, BUCKET_MAP, EXCH, journal or FakeJournal(), CTS, 300,
                          k_rungs=k, clock=lambda: 0.0, sleep=lambda _s: None, wing_cap=wing_cap,
                          batch_create=batch_create)
    return ex, aw


def _place(coid="v33-c1", n="0.45"):
    return V32Action(kind=ActionKind.PLACE_REST, ticker=B, side="no", action="buy", count=1,
                     price=Decimal(n), expiration_epoch=CTS - 300, client_order_id=coid)


def _state():
    return V33State.new(CLOSE, CTS, BUCKET_MAP, load_v33_params())


def test_async_place_records_rung_and_acks():
    fake = FakeProxyWriter()

    async def go():
        ex, aw = _aexec(fake)
        try:
            events = await ex.on_action_async(_place("v33-r0", "0.45"), _state(), CTS - 600)
            return events, ex
        finally:
            aw.close()

    events, ex = asyncio.run(go())
    assert any(isinstance(e, OrderAck) for e in events)
    rec = ex.rest_book["v33-r0"]
    assert rec.status == "live" and rec.price == Decimal("0.45")
    posts = [c for c in fake.calls if c.verb == "post"]
    assert posts[0].klass == CLASS_REST and posts[0].body["post_only"] is True


def test_async_roll_amend_persists_order_id():
    fake = FakeProxyWriter()

    async def go():
        ex, aw = _aexec(fake)
        try:
            await ex.on_action_async(_place("v33-r0", "0.45"), _state(), CTS - 600)
            oid = ex.rest_book["v33-r0"].order_id
            fake.post_queue.append(WriteResponse(200, {"order": {"order_id": oid,
                                                                "client_order_id": "v33-r0b",
                                                                "fill_count": "0.00",
                                                                "remaining_count": "1.00"}}, True))
            amend = V32Action(kind=ActionKind.AMEND_REST, order_id=oid, ticker=B, side="no",
                              action="buy", count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                              updated_client_order_id="v33-r0b")
            events = await ex.on_action_async(amend, _state(), CTS - 590)
            return events, ex, oid
        finally:
            aw.close()

    events, ex, oid = asyncio.run(go())
    assert any(isinstance(e, OrderAmended) and e.order_id == oid for e in events)
    assert ex.rest_book["v33-r0b"].order_id == oid and ex.amends_confirmed == 1
    amends = [c for c in fake.calls if c.verb == "post" and "/amend" in c.path]
    assert amends and amends[0].klass == "roll"


def test_async_amend_failure_falls_back_to_cancel_create():
    fake = FakeProxyWriter()

    async def go():
        ex, aw = _aexec(fake)
        try:
            await ex.on_action_async(_place("v33-r0", "0.45"), _state(), CTS - 600)
            oid = ex.rest_book["v33-r0"].order_id
            fake.post_queue.append(WriteResponse(403, {"error": "amend blocked"}, False, "http_403"))
            amend = V32Action(kind=ActionKind.AMEND_REST, order_id=oid, ticker=B, side="no",
                              action="buy", count=1, price=Decimal("0.44"), client_order_id="v33-r0",
                              updated_client_order_id="v33-r0b")
            events = await ex.on_action_async(amend, _state(), CTS - 590)
            return events, ex
        finally:
            aw.close()

    events, ex = asyncio.run(go())
    assert ex.amends_failed == 1 and ex.amend_fallbacks == 1
    assert any(isinstance(e, OrderCancelled) for e in events)
    assert any(c.verb == "delete" for c in fake.calls)


def test_async_pre_place_invariant_overflow_stands_down():
    fake = FakeProxyWriter()
    k = 3
    fake.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"order_id": f"oid-{i}", "client_order_id": f"v33-old{i}", "exchange_index": 2, "ticker": B}
        for i in range(k)]}

    async def go():
        ex, aw = _aexec(fake, k=k)
        try:
            for i in range(k):
                ex.rest_book[f"v33-old{i}"] = RestRecord(
                    client_order_id=f"v33-old{i}", order_id=f"oid-{i}", price=Decimal("0.40"),
                    count=1, ticker=B, bucket_Sd=80400, placed_ts=0.0, status="live", exchange_index=2)
                ex._by_order_id[f"oid-{i}"] = f"v33-old{i}"
            events = await ex.on_action_async(_place("v33-new", "0.45"), _state(), CTS - 600)
            return events, ex
        finally:
            aw.close()

    events, ex = asyncio.run(go())
    assert ex.stand_down_reason == "rest_invariant_violation"
    assert ex.rest_invariant_overflow == 1
    assert not any(c.verb == "post" and c.body.get("client_order_id") == "v33-new" for c in fake.calls)
    assert any(isinstance(e, OrderCancelled) for e in events)


def test_async_unknown_outcome_never_replaced_over():
    fake = FakeProxyWriter()
    fake.post_queue.append(WriteResponse(503, {}, False, "http_503"))

    async def go():
        ex, aw = _aexec(fake)
        try:
            e1 = await ex.on_action_async(_place("v33-u1", "0.45"), _state(), CTS - 600)
            posts_before = len([c for c in fake.calls if c.verb == "post"])
            return e1, ex, posts_before
        finally:
            aw.close()

    e1, ex, posts_before = asyncio.run(go())
    assert ex.stand_down_reason == "post_unknown_outcome"
    assert "v33-u1" in ex.rest_book and ex.rest_book["v33-u1"].status == "unknown"
    assert posts_before == 1


def test_async_429_on_wing_retried_on_wing_lane():
    fake = FakeProxyWriter()
    fake.post_queue.append(WriteResponse(429, {}, False, "http_429"))

    async def go():
        from dataclasses import replace as dc_replace
        st = _state()
        leg = WingLeg(ticker=B, side="yes", count=1, limit=Decimal("0.03"),
                      client_order_id="v33-w0", batch=0)
        st = dc_replace(st, wing_legs=(leg,))
        ex, aw = _aexec(fake, wing_cap=2)
        try:
            await ex.on_action_async(
                V32Action(kind=ActionKind.TAKE_WINGS, legs=[], count=1), st, CTS - 500)
            return ex
        finally:
            aw.close()

    ex = asyncio.run(go())
    wing_posts = [c for c in fake.calls if c.verb == "post" and c.klass == CLASS_WING]
    assert len(wing_posts) == 2
    assert ex.async_rate_limited == 1


class FakeVenue:
    """Stateful in-process venue: a create adds a resting order (our coid), a cancel removes it, a GET
    returns the resting list / one order's status. Each call sleeps a random latency on its worker thread.
    Tracks the max simultaneous count of OUR rests (the K-invariant witness)."""

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self.resting = {}
        self._oid = 0
        self.max_ours = 0

    def _sleep(self):
        time.sleep(self.rng.uniform(0.0, 0.006))

    def rest_post(self, path, body, headers=None):
        self._sleep()
        coid = body.get("client_order_id") or ""
        with self._lock:
            self._oid += 1
            oid = f"oid-{self._oid}"
            if body.get("post_only"):
                self.resting[oid] = {"coid": coid, "ticker": body.get("ticker")}
                ours = sum(1 for r in self.resting.values() if str(r["coid"]).startswith("v33-"))
                self.max_ours = max(self.max_ours, ours)
            return WriteResponse(200, {"order": {"order_id": oid, "client_order_id": coid,
                                                 "fill_count": "0.00", "remaining_count": "1.00"}}, True)

    def rest_delete(self, path, headers=None):
        self._sleep()
        oid = path.split("/")[-1].split("?")[0]
        with self._lock:
            self.resting.pop(oid, None)
        return WriteResponse(200, {"reduced_by": "1.00"}, True)

    def rest_get(self, path, params=None):
        self._sleep()
        with self._lock:
            if path == OPEN_ORDERS_PATH:
                orders = [{"order_id": oid, "client_order_id": r["coid"], "exchange_index": 2,
                           "ticker": r["ticker"]} for oid, r in self.resting.items()]
                return {"orders": orders}
            oid = path.split("/")[-1]
            if oid in self.resting:
                return {"order": {"order_id": oid, "status": "resting", "fill_count_fp": "0.00",
                                  "remaining_count_fp": "1.00"}}
            return {"order": {"order_id": oid, "status": "canceled", "fill_count_fp": "0.00",
                              "remaining_count_fp": "0.00"}}


def test_property_never_more_than_k_rests_under_random_latencies():
    K = 4
    for seed in range(12):
        venue = FakeVenue(seed)

        async def go():
            ex, aw = _aexec(venue, k=K)
            try:
                for i in range(K):
                    await ex.on_action_async(_place(f"v33-r{i}", f"0.4{i}"), _state(), CTS - 600)
                await asyncio.gather(*[
                    ex.on_action_async(_place(f"v33-x{j}", "0.30"), _state(), CTS - 590)
                    for j in range(4)])
                return ex
            finally:
                aw.close()

        asyncio.run(go())
        assert venue.max_ours <= K, f"seed {seed}: venue held {venue.max_ours} > K={K}"


def test_async_startup_sweep_scoped_to_v33():
    fake = FakeProxyWriter()
    fake.get_map[OPEN_ORDERS_PATH] = {"orders": [
        {"order_id": "oid-a", "client_order_id": "v33-old", "ticker": B, "exchange_index": 2},
        {"order_id": "oid-b", "client_order_id": "v32-live", "ticker": B, "exchange_index": 2},
    ]}
    journal = FakeJournal()

    async def go():
        aw = AsyncOrderWriter(fake, wing_workers=2, cancel_workers=2, normal_workers=2)
        try:
            return await cancel_stale_open_orders_async(aw, journal, lambda: 0.0)
        finally:
            aw.close()

    result = asyncio.run(go())
    assert result["found"] == 1 and result["cancelled"] == 1 and result["skipped_foreign"] == 1
    assert any(c.verb == "delete" and "oid-a" in c.path for c in fake.calls)
    assert not any(c.verb == "delete" and "oid-b" in c.path for c in fake.calls)


# ===========================================================================
# V33Driver async dispatch — end-to-end through the real core (armed)
# ===========================================================================
from dataclasses import replace as _dr  # noqa: E402

import service.run_v33 as RUN  # noqa: E402
from tests.test_v33_run import BK, B_SD, STK_SD, STK_SU, J as DryJ, _bring_up  # noqa: E402

_EXCH2 = {B_SD: 2, STK_SD: 2, STK_SU: 2, "KXBTC-RANGE-B80500": 2}


def _armed_params():
    from service.v33 import load_v33_params
    return _dr(load_v33_params(), E_min=Decimal("0.05"), tol=Decimal("0.01"), deb_ms=0,
              freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)


def _armed_driver(fake, *, post_latency=0.008):
    p = _armed_params()
    cts = 1789876800
    close = "2026-09-20T04:00:00Z"
    st = V33State.new(close, cts, BK, p, shakedown=False)  # ARMED -> REAL place/amend/cancel
    fake.post_latency = post_latency
    aw = AsyncOrderWriter(fake, wing_workers=6, cancel_workers=12, normal_workers=12)
    ex = V33AsyncExecutor(aw, fake, BK, _EXCH2, DryJ(), cts, 300, k_rungs=p.rungs,
                          clock=lambda: 0.0, sleep=lambda _s: None, wing_cap=2)
    drv = RUN.V33Driver(p, st, DryJ(), ex, dry_sim=False, clock=lambda: 0.0, async_writer=aw)
    return drv, aw, cts


async def _drain(pred, *, timeout=4.0):
    t0 = time.monotonic()
    while not pred():
        await asyncio.sleep(0.001)
        if time.monotonic() - t0 > timeout:
            break


def test_async_driver_places_full_ladder_end_to_end():
    fake = FakeProxyWriter()

    async def go():
        drv, aw, cts = _armed_driver(fake, post_latency=0.006)
        try:
            _bring_up(drv, cts)   # schedules the initial ladder placement off the loop
            await _drain(lambda: len(drv.state.ladder) == 11
                         and all(o.live and o.order_id is not None for o in drv.state.ladder))
            return drv
        finally:
            aw.close()

    drv = asyncio.run(go())
    assert len(drv.state.ladder) == 11
    assert all(o.live and o.order_id is not None for o in drv.state.ladder)
    # every rung's place went out on the REST lane as a real create.
    rests = [c for c in fake.calls if c.verb == "post" and c.body.get("post_only")]
    assert len(rests) == 11 and all(c.klass == CLASS_REST for c in rests)


def test_async_driver_loop_not_blocked_and_placements_concurrent():
    # 11 creates at 12 ms each: if they ran SERIALLY on the loop (the 2026-09-30 bug) the ladder would
    # take >= ~132 ms and the loop would be frozen the whole time; off-loop + concurrent it completes in a
    # small multiple of one latency AND the loop keeps iterating (loops counter high).
    fake = FakeProxyWriter()

    async def go():
        drv, aw, cts = _armed_driver(fake, post_latency=0.012)
        try:
            _bring_up(drv, cts)
            loops = 0
            t0 = time.monotonic()
            while not (len(drv.state.ladder) == 11 and all(o.live for o in drv.state.ladder)):
                await asyncio.sleep(0.001)
                loops += 1
                if time.monotonic() - t0 > 4.0:
                    break
            return drv, loops, time.monotonic() - t0
        finally:
            aw.close()

    drv, loops, elapsed = asyncio.run(go())
    assert len(drv.state.ladder) == 11 and all(o.live for o in drv.state.ladder)
    assert elapsed < 0.11, f"ladder took {elapsed:.3f}s -> writes were serialized, not concurrent off-loop"
    assert loops > 3, "the loop did not iterate while writes were in flight (it was blocked)"
    # the feed never froze while quoting: the max feed gap stayed tiny (the direct proof).
    assert drv._feed_gap_max_s < 0.05
    stats = RUN._v33_writer_stats(drv)
    assert stats["async_writer"] is True
    assert stats["writer"]["submitted"] >= 11 and stats["writer"]["by_class"][CLASS_REST] >= 11


def test_async_driver_loop_free_while_creates_gated():
    # gate every create: the ladder can't ack, but the loop MUST keep processing (a blocked loop would
    # not advance the tick counter). Proves the dispatch does not block the loop even when the venue hangs.
    fake = FakeProxyWriter()
    fake.gate("post:rest")

    async def go():
        drv, aw, cts = _armed_driver(fake, post_latency=0.0)
        try:
            _bring_up(drv, cts)   # 11 creates dispatched, all parked on the gate
            ticks = 0
            for _ in range(60):
                await asyncio.sleep(0)
                ticks += 1
            gated = (aw.inflight > 0) and not any(o.live for o in drv.state.ladder)
            fake.gate("post:rest").set()
            await _drain(lambda: len(drv.state.ladder) == 11 and all(o.live for o in drv.state.ladder))
            return drv, ticks, gated
        finally:
            aw.close()

    drv, ticks, gated = asyncio.run(go())
    assert gated, "expected creates in flight (gated) with no rung live yet"
    assert ticks == 60, "the loop was blocked while creates were in flight"
    assert len(drv.state.ladder) == 11 and all(o.live for o in drv.state.ladder)


def test_dry_driver_is_not_async_and_unchanged():
    # DRY (async_writer=None) -> the synchronous path; _async is False; writer_stats carry no writer block.
    from service.run_v32 import FrozenExecutor
    p = _armed_params()
    st = V33State.new("2026-09-20T04:00:00Z", 1789876800, BK, p, shakedown=True)
    drv = RUN.V33Driver(p, st, DryJ(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    assert drv._async is False and drv._aw is None
    _bring_up(drv, 1789876800)
    assert len(drv.state.ladder) == 11              # dry FrozenExecutor acks synchronously (unchanged)
    stats = RUN._v33_writer_stats(drv)
    assert stats["async_writer"] is False and "writer" not in stats


def test_async_wing_retry_storm_belt_one_inflight_per_leg():
    # 2026-09-30 22:00Z finding: a missing wing leg was retried EVERY TICK (967 IOCs in ~96 s). The
    # transport belt allows only ONE in-flight IOC per missing leg: a duplicate retry for a leg already in
    # flight is DROPPED (counted, no journal record each) and never sent.
    from dataclasses import replace as dc_replace

    fake = FakeProxyWriter()
    fake.gate("post:wing")   # hold the first wing take in flight

    async def go():
        st = _state()
        leg = WingLeg(ticker=B, side="yes", count=1, limit=Decimal("0.03"),
                      client_order_id="v33-w0", batch=0)
        st = dc_replace(st, wing_legs=(leg,))
        ex, aw = _aexec(fake, wing_cap=2)
        try:
            take = V32Action(kind=ActionKind.TAKE_WINGS, legs=[], count=1)
            retry = V32Action(kind=ActionKind.RETRY_WING, legs=[], count=1)
            t1 = asyncio.ensure_future(ex.on_action_async(take, st, CTS - 500))
            await asyncio.sleep(0.02)   # let t1 CLAIM the leg and park on the wing gate
            # three duplicate retries for the SAME leg while the first is in flight -> all DROPPED.
            for _ in range(3):
                r = await ex.on_action_async(retry, st, CTS - 499)
                assert r == []
            fake.gate("post:wing").set()
            await t1
            return ex
        finally:
            aw.close()

    ex = asyncio.run(go())
    wing_posts = [c for c in fake.calls if c.verb == "post" and c.klass == CLASS_WING]
    assert len(wing_posts) == 1                 # ONE IOC sent despite four take/retry actions
    assert ex.wing_retries_dropped == 3         # the three duplicates were counted, not sent
    # the belt counted (never a journal record each): no per-drop journal record kind.
    kinds = [k for k, _ in ex.journal.records]
    assert kinds.count("take_wings") == 1


def test_async_wing_retry_belt_allows_next_after_completion():
    # once the in-flight take COMPLETES, the belt releases the leg so the core's NEXT retry can send again.
    from dataclasses import replace as dc_replace

    fake = FakeProxyWriter()
    fake.post_queue.append(WriteResponse(200, {"order": {"order_id": "wo-1", "client_order_id": "v33-wc-1",
                                                        "fill_count": "0.00", "remaining_count": "1.00"}}, True))

    async def go():
        st = _state()
        leg = WingLeg(ticker=B, side="yes", count=1, limit=Decimal("0.03"),
                      client_order_id="v33-w0", batch=0)
        st = dc_replace(st, wing_legs=(leg,))
        ex, aw = _aexec(fake, wing_cap=2)
        try:
            take = V32Action(kind=ActionKind.TAKE_WINGS, legs=[], count=1)
            await ex.on_action_async(take, st, CTS - 500)      # completes (IOC unfilled)
            await ex.on_action_async(take, st, CTS - 490)      # leg released -> sends again
            return ex
        finally:
            aw.close()

    ex = asyncio.run(go())
    wing_posts = [c for c in fake.calls if c.verb == "post" and c.klass == CLASS_WING]
    assert len(wing_posts) == 2 and ex.wing_retries_dropped == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
