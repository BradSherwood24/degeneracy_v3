"""async_writer.py — the off-loop order writer for V3.3 (Brad 2026-09-30: "I'd vote everything should
be async, that we reprices of multiple levels can be sent without delay").

THE MEASURED CAUSE (2026-09-30, first armed V3.3 day, 13:00Z journal). ``service.run_v33.V33Driver._pump``
runs on the ONE asyncio loop; every order create / amend / cancel / wing-IOC / status-poll went out as a
SYNCHRONOUS ``requests.post/delete`` (and the pacer's ``time.sleep``) INSIDE the loop. While the executor
wrote, the websocket reader could not drain — the books froze while the eval clock (``_last_server_ts`` +
wall elapsed) kept advancing — so after ``freshness_max_age_s`` the wings read stale, tripped a
``stale_or_missing_wing`` hold, the hold expired during the same burst, and the whole ladder was cancelled
and re-placed. 75 of 77 feed gaps >= 1 s had one of our own writes inside them; a cancel-all of 11 rungs
took ~3.4 s of dead loop; the ladder was out of the book 51-109 s of ~610 s.

THE FIX (this module). Every proxy round trip runs on a worker thread via ``loop.run_in_executor`` and is
``await``-ed, so the loop stays free to drain the feed; every executor pause (cancel-confirm poll, backoff)
becomes ``await asyncio.sleep`` (which yields the loop rather than blocking it). Crucially ALL orchestration
and ALL executor / core state stay ON THE LOOP THREAD — only the raw blocking ``requests`` call is handed to
a worker — so there are NO locks and NO cross-thread state races (the codebase's deliberate single-threaded
discipline is preserved; see ``service.run_v32`` L49 "both clients run on the ONE asyncio loop ... the shared
state needs no lock"). This is the ``run_in_executor`` option of the two the build brief offered, chosen over
a dedicated writer thread + ``call_soon_threadsafe`` precisely because it keeps state loop-confined.

PRIORITY LANES (build brief §2c / Brad's wing whitelist). Each class of write draws a worker from its OWN
bounded pool, so a WING IOC take (the safety-critical hedge) NEVER waits behind a queued roll / cancel /
create, and a CANCEL (the T-5 cancel-all / flatten) never waits behind a queued roll / create:

  * ``wing``   -> the WING pool (dedicated). A wing take is dispatched the instant it is submitted.
  * ``cancel`` -> the CANCEL pool (dedicated). A cancel-all fans out concurrently (``asyncio.gather``) and
    never queues behind creates.
  * ``roll`` / ``rest`` / ``poll`` -> the NORMAL pool.

CONCURRENCY ACROSS DISTINCT ORDERS (build brief §2b). Rolls / creates of several levels are dispatched
together (the driver gathers them), so "reprices of multiple levels can be sent without delay". PER-SLOT
FIFO (build brief §2a) is enforced by a per-slot ``asyncio.Lock``: a cancel and a create for the SAME slot
never overlap, and a create for a slot only goes out after that slot's cancel has released the lock.

REQUEST CLASS HEADER (build brief §4). Every write carries ``X-DV3-Class: wing|rest|roll|cancel|poll`` on
the LOCALHOST hop (for Brad's future proxy-side throttle with a wing whitelist; the proxy ignores it today).
See the note on ``_ClassHeaders`` below for the forwarding caveat.

House law inherited whole: every HTTP edge is the injected ``ProxyWriter`` (tests use a fake — this is NEVER
dialed against the live proxy from an automated context); a POST is never retried on an UNKNOWN outcome; a
429 is a definitive non-execution retried on its OWN lane; nothing reads a key / .env / PEM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from service.proxy_writer import ProxyWriter, WriteResponse

logger = logging.getLogger(__name__)

# X-DV3-Class values (build brief §4). A future proxy-side leaky-bucket throttle (pilot/ops/proxy_throttle.md)
# will read this header to whitelist ``wing`` and queue-not-reject the rest; the proxy IGNORES it today.
CLASS_WING = "wing"
CLASS_REST = "rest"
CLASS_ROLL = "roll"
CLASS_CANCEL = "cancel"
CLASS_POLL = "poll"

# The three lanes -> bounded worker pools. A wing never shares a pool with a cancel-all, and a cancel never
# shares a pool with a create, so priority is by construction (not merely a queue ordering that a saturated
# shared pool could defeat). Pool sizes are generous relative to K (the 8..18c ladder rungs) so a full
# cancel-all + a concurrent wing + the next placement never starve each other; total threads stay bounded.
DEFAULT_WING_WORKERS = 6
DEFAULT_CANCEL_WORKERS = 12
DEFAULT_NORMAL_WORKERS = 12

_CLASS_TO_LANE = {
    CLASS_WING: "wing",
    CLASS_CANCEL: "cancel",
    CLASS_ROLL: "normal",
    CLASS_REST: "normal",
    CLASS_POLL: "normal",
}


class _ClassHeaders:
    """Adds ``X-DV3-Class`` (and the existing ``X-DV3-Token`` when set) to the LOCALHOST request headers.

    FORWARDING CAVEAT (build brief §4): the proxy signs and builds its OWN upstream Kalshi request, so a
    client header on the localhost hop is not forwarded to Kalshi by construction — but the proxy source is
    Brad's (proposals only; not read here), so this is stated as UNVERIFIED in the build report and the
    proxy proposal, not asserted. The header is harmless today (the proxy ignores unknown headers)."""


@dataclass
class WriterStats:
    """Per-window writer telemetry surfaced on the ledger row (build brief §6) so a reviewer can SEE the
    loop-blocking is gone: queue depth (writes awaiting a worker), per-write latency, and the batch/lane
    mix. ``feed_gap_max_s`` (the direct proof) is tracked by the driver, not here."""

    submitted: int = 0
    completed: int = 0
    max_inflight: int = 0
    _latency_ms: list[float] = field(default_factory=list)
    by_class: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def latency_p50(self) -> float | None:
        return _pct(self._latency_ms, 0.50)

    def latency_p99(self) -> float | None:
        return _pct(self._latency_ms, 0.99)

    def latency_max(self) -> float | None:
        return max(self._latency_ms) if self._latency_ms else None

    def summary(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "max_inflight": self.max_inflight,
            "latency_ms_p50": _round(self.latency_p50()),
            "latency_ms_p99": _round(self.latency_p99()),
            "latency_ms_max": _round(self.latency_max()),
            "by_class": dict(self.by_class),
        }


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    srt = sorted(xs)
    n = len(srt)
    import math
    rank = min(n - 1, max(0, math.ceil(q * n) - 1))
    return srt[rank]


def _round(v: float | None) -> float | None:
    return round(v, 3) if v is not None else None


class AsyncOrderWriter:
    """Runs proxy round trips OFF the event loop and returns results TO the loop, with per-class priority
    lanes and per-slot FIFO. Constructed with an injected ``ProxyWriter`` (so tests inject a fake and this
    is never dialed against the live proxy). The blocking edge is bounded to ``loop.run_in_executor`` on a
    per-lane ``ThreadPoolExecutor``; everything else runs on the loop.

    ``post(rel, body, klass, slot)`` / ``delete(path, klass, slot)`` / ``get(path, params, klass)`` are the
    three awaitable verbs. They mirror ``ProxyWriter`` exactly (POST/DELETE -> ``WriteResponse``; GET ->
    the parsed dict), so the executor's protocol logic is async-ified with minimal divergence. ``slot``
    (optional) serializes same-slot writes (a cancel then a create for one rung) via a per-slot lock."""

    def __init__(
        self,
        writer: ProxyWriter,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        wing_workers: int = DEFAULT_WING_WORKERS,
        cancel_workers: int = DEFAULT_CANCEL_WORKERS,
        normal_workers: int = DEFAULT_NORMAL_WORKERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._writer = writer
        self._loop = loop
        self._clock = clock
        self._pools: dict[str, ThreadPoolExecutor] = {
            "wing": ThreadPoolExecutor(max_workers=max(1, wing_workers),
                                       thread_name_prefix="dv3w-wing"),
            "cancel": ThreadPoolExecutor(max_workers=max(1, cancel_workers),
                                         thread_name_prefix="dv3w-cancel"),
            "normal": ThreadPoolExecutor(max_workers=max(1, normal_workers),
                                         thread_name_prefix="dv3w-normal"),
        }
        self._slot_locks: dict[str, asyncio.Lock] = {}
        self._inflight = 0
        self.stats = WriterStats()
        self._closed = False

    # --- loop / lane plumbing -------------------------------------------------
    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _slot_lock(self, slot: str) -> asyncio.Lock:
        lk = self._slot_locks.get(slot)
        if lk is None:
            lk = asyncio.Lock()
            self._slot_locks[slot] = lk
        return lk

    def _headers(self, klass: str) -> dict[str, str]:
        """Localhost-hop headers: the request class plus the existing DV3 proxy token when present. Read at
        call time so a token rotation needs no restart; the token value is never logged."""
        headers = {"X-DV3-Class": klass}
        token = os.environ.get("DV3_PROXY_TOKEN", "").strip()
        if token:
            headers["X-DV3-Token"] = token
        return headers

    async def _run_off_loop(self, fn: Callable[[], Any], klass: str) -> Any:
        """Run one blocking proxy call on its lane's pool, tracking inflight depth + latency. The lane is
        derived from the request class so a wing never shares a pool with a cancel-all."""
        lane = _CLASS_TO_LANE.get(klass, "normal")
        pool = self._pools[lane]
        loop = self._get_loop()
        self._inflight += 1
        self.stats.submitted += 1
        self.stats.by_class[klass] += 1
        self.stats.max_inflight = max(self.stats.max_inflight, self._inflight)
        t0 = self._clock()
        try:
            return await loop.run_in_executor(pool, fn)
        finally:
            self._inflight -= 1
            self.stats.completed += 1
            self.stats._latency_ms.append((self._clock() - t0) * 1000.0)

    @property
    def inflight(self) -> int:
        return self._inflight

    # --- the three verbs (mirror ProxyWriter) --------------------------------
    async def post(self, rel: str, body: dict[str, Any], *, klass: str,
                   slot: str | None = None) -> WriteResponse:
        """POST create/amend (never retried; idempotency is the coid). Same ``WriteResponse`` the sync
        writer returns. ``slot`` serializes a same-slot cancel->create so a create never overlaps its
        cancel (build brief §2a). The ``X-DV3-Class`` header rides the localhost hop."""
        headers = self._headers(klass)

        async def _do() -> WriteResponse:
            return await self._run_off_loop(lambda: self._writer.rest_post(rel, body, headers=headers),
                                            klass)

        return await self._with_slot(slot, _do)

    async def delete(self, path: str, *, klass: str, slot: str | None = None) -> WriteResponse:
        """DELETE cancel (idempotent; the sync writer already bounded-retries a 429/5xx internally)."""
        headers = self._headers(klass)

        async def _do() -> WriteResponse:
            return await self._run_off_loop(lambda: self._writer.rest_delete(path, headers=headers),
                                            klass)

        return await self._with_slot(slot, _do)

    async def get(self, path: str, params: dict[str, Any] | None = None, *,
                  klass: str = CLASS_POLL) -> Any:
        """GET status / open-orders (reads only; returns the parsed dict, like ``ProxyWriter.rest_get``)."""
        return await self._run_off_loop(lambda: self._writer.rest_get(path, params), klass)

    async def _with_slot(self, slot: str | None, do: Callable[[], Awaitable[Any]]) -> Any:
        if slot is None:
            return await do()
        async with self._slot_lock(slot):
            return await do()

    # --- shutdown ------------------------------------------------------------
    def close(self) -> None:
        """Shut the pools down at window end. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        for pool in self._pools.values():
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover — py<3.9 has no cancel_futures
                pool.shutdown(wait=False)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ASYNC-WRITER] pool shutdown error: %s", e)
