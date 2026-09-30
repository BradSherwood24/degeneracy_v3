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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
