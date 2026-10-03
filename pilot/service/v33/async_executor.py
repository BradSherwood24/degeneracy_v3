"""async_executor.py — the OFF-LOOP armed maker executor for V3.3 (2026-09-30).

A subclass of ``V33LiveExecutor`` that keeps every DECISION identical but moves every proxy round trip off
the event loop via ``AsyncOrderWriter`` (``loop.run_in_executor``) and turns every executor pause
(cancel-confirm poll, cancel backoff, invariant recheck, write pacing) into ``await asyncio.sleep`` — so the
websocket reader never stalls and the books never freeze (the 2026-09-30 measured cause).

DIVERGENCE IS MINIMISED BY CONSTRUCTION. Each ``*_async`` method below is the LINE-FOR-LINE async twin of a
synchronous method on ``LiveExecutor`` / ``V33LiveExecutor`` (named in its docstring), with exactly three
mechanical substitutions and nothing else changed:

    self.writer.rest_post(path, body)  ->  await self._apost(path, body, klass, slot)
    self.writer.rest_delete(path)      ->  await self._adelete(path, klass, slot)
    self.writer.rest_get(path, params) ->  await self._aget(path, params)
    self.sleep(x) / self._pacer.acquire(...)  ->  await asyncio.sleep(x) / await self._pacer.acquire_async(...)

Every PURE helper (body builders, response parsers, ``_finish_cancel``, ``_resolve_cancel_from_status``,
``_reject_place``, ``_filter_phantoms``, ``_invariant_verdict``, ``_record_alarm``, ``attribute`` …) is
INHERITED UNCHANGED and reused, so the incident-fix logic (shard-aware cancel, status-truth, read-path
phantom filter, the K-aware invariant, unknown-outcome latch, weighted-average wing aggregation, the 3-reject
stand-down) is the SAME code, not a re-implementation.

STATE IS LOOP-CONFINED. Because only the raw blocking ``requests`` call runs on a worker thread (inside the
writer), and every mutation of ``rest_book`` / counters / the RestBook happens on the loop thread between
awaits, there are NO locks and NO cross-thread races. ``self.writer`` is replaced with a guard that RAISES on
any synchronous ``rest_*`` call, so an un-async-ified path fails LOUD (it would block the loop) rather than
silently regressing.

REQUEST CLASSES (build brief §4): a create -> ``rest``; an amend/roll -> ``roll``; a cancel/stray-cancel ->
``cancel``; a wing IOC take (and print-through complete/unwind) -> ``wing`` (the priority lane); a status /
open-orders GET -> ``poll``. A 429 on any POST is a definitive non-execution: this executor waits and retries
the SAME client_order_id ONCE on the SAME lane (so a 429 on a wing is retried on the wing lane, ahead of any
queued roll/cancel/create — build brief §5), then defers to the inherited 429-exempt ``_reject_place``.

House law inherited whole: a POST is never retried on an UNKNOWN outcome (timeout/5xx); nothing reads a key /
.env / PEM; a WOULD_* twin reaching this armed executor raises (P3-1).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from math import ceil
from typing import Any

from service.orders.envelope import (
    build_batch,
    normalize_fill_to_side,
    parse_batch_response,
    parse_single_response,
)
from service.proxy_writer import ProxyWriter
from service.v32.actions import ActionKind, V32Action
from service.v32.core import BUY_NO
from service.v32.events import Fill, OrderAck, OrderAmended, OrderCancelled
from service.v32.executor import (
    CANCEL_BACKOFF_S,
    CANCEL_CONFIRM_INTERVAL_S,
    CANCEL_CONFIRM_POLLS,
    CANCEL_RETRY_ATTEMPTS,
    INVARIANT_RECHECK_S,
    OPEN_ORDERS_PATH,
    ORDER_STATUS_PATH_TMPL,
    REL_BATCH_CREATE,
    REL_SINGLE_CREATE,
    OrderStatus,
    RestRecord,
    _dec_or_none,
    amend_path,
    cancel_path,
    parse_order_status,
)
from datetime import datetime, timezone

from service.v33.actions import V33ActionKind
from service.v33.events import V33Fill
from service.v33.async_writer import (
    CLASS_CANCEL,
    CLASS_POLL,
    CLASS_REST,
    CLASS_ROLL,
    CLASS_WING,
    AsyncOrderWriter,
)
from service.v33.executor import (
    WING_429_BACKOFF_BASE_S,
    WING_429_BACKOFF_MAX_S,
    split_wing_batches,
    wing_batch_max_orders,
    COST_AMEND,
    COST_CANCEL,
    COST_CREATE,
    RATE_LIMIT_RETRY_WAIT_S,
    _HTTP_TOO_MANY_REQUESTS,
    V33LiveExecutor,
)

logger = logging.getLogger(__name__)


class _SyncGuardWriter:
    """Replaces ``self.writer`` on the async executor so ANY synchronous ``rest_*`` call (an un-async-ified
    path that would block the loop) fails loud instead of silently regressing the fix."""

    def rest_post(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("V33AsyncExecutor.writer.rest_post called synchronously — an un-async-ified "
                             "path would block the event loop (the 2026-09-30 measured cause)")

    def rest_delete(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("V33AsyncExecutor.writer.rest_delete called synchronously — un-async-ified path")

    def rest_get(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("V33AsyncExecutor.writer.rest_get called synchronously — un-async-ified path")


# 2026-10-02 wing venue-confirm (Brad: "confirm the order attempted last time actually didn't fill").
FILLS_PATH = "/portfolio/fills"
WING_CONFIRM_SKEW_S = 5.0


def _iso_to_epoch(value):
    """'2026-10-01T23:54:06.199Z' -> epoch seconds; None when absent/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


class V33AsyncExecutor(V33LiveExecutor):
    """The off-loop armed executor. Constructed with an ``AsyncOrderWriter`` (the HTTP edge) AND the
    underlying ``ProxyWriter`` (so the inherited ``__init__`` sets up the pacer/counters/RestBook exactly as
    the sync executor does); ``self.writer`` is then swapped for a guard so no sync HTTP can slip through."""

    def __init__(self, async_writer: AsyncOrderWriter, proxy_writer: ProxyWriter, *args: Any,
                 **kwargs: Any) -> None:
        super().__init__(proxy_writer, *args, **kwargs)
        self._aw = async_writer
        self.writer = _SyncGuardWriter()   # trap synchronous HTTP (would block the loop)
        self.async_rate_limited = 0        # 429s observed across retries (surfaced on the exec counters)
        # WING RETRY-STORM BELT (2026-09-30 22:00Z finding): ONE in-flight IOC per missing leg. The (batch,
        # side) keys of legs with a wing take currently in flight; a RETRY_WING for a leg already in flight
        # is DROPPED (counted, never a journal record each) so a per-tick retry storm cannot starve the
        # cancel/roll lanes or the reader. The CORE-side re-emission floor (fix/v33-fill-attribution branch,
        # core.py) is the source fix; this is the transport belt on the priority lane.
        self._wing_inflight_legs: set[tuple[int, str]] = set()
        self.wing_retries_dropped = 0
        # 2026-10-02 (Brad: "confirm the order attempted last time actually didn't fill"): venue order ids of
        # every wing chunk whose response we DID read, so a venue fills lookup after a LOST response can
        # exclude already-attributed fills and count only the unknown chunk's.
        self._wing_order_ids: set[str] = set()
        # 2026-10-02 (Test Fire #2, 15:00Z window): creates whose POST is IN FLIGHT (no venue id yet) and the
        # coids the core asked to cancel while that was so. A cancel for an un-acked order used to be a
        # no-op that REPORTED cancelled -> the core re-placed the slot -> 17 orphan rests sat unmanaged
        # until expiry. Now the cancel is DEFERRED and executed the instant the ack lands.
        self._inflight_creates: set[str] = set()
        self._cancel_pending: set[str] = set()
        # GATE C (2026-10-03 02:00Z naked fill): venue order ids with a DELETE currently in flight (so the
        # stand-down sweep and a racing core CANCEL_REST never double-DELETE one order; the first resolves
        # it and reports the one OrderCancelled), and the one-shot latch of the stand-down sweep.
        self._cancel_oids_inflight: set[str] = set()
        self._standdown_swept = False
        self.standdown_sweep_cancels = 0
        self.places_refused_stood_down = 0
        self.amends_refused_stood_down = 0
        self.wing_venue_confirms = 0
        self.wing_venue_confirmed_count = Decimal(0)
        # 2026-10-02 (02:00Z 429 storm): a wing 429 is a DEFINITIVE non-execution; the retry cadence backs
        # off (base * 2**(streak-1), capped) -- a retry inside the backoff window sends NOTHING and reports
        # the leg unfilled so the core re-emits after the window (no network, no venue hammering).
        self._wing_429_streak = 0
        self._wing_backoff_until = 0.0
        self.wing_backoff_skips = 0
        self.wing_rate_limited_batches = 0

    # =====================================================================
    # off-loop verb helpers (429 belt on the SAME lane; build brief §5)
    # =====================================================================
    @staticmethod
    def _retry_after_s(resp: Any) -> float | None:
        body = getattr(resp, "body", None)
        if isinstance(body, dict):
            for key in ("retry_after", "retry_after_s", "retry_after_seconds"):
                v = body.get(key)
                if v is not None:
                    try:
                        return max(0.0, float(v))
                    except (TypeError, ValueError):
                        pass
        return None

    def _estimate_pacer_wait(self) -> float:
        p = self._pacer
        return max(0.0, (COST_CREATE - p.tokens) / p.rate) if p.rate > 0 else RATE_LIMIT_RETRY_WAIT_S

    async def _apost(self, path: str, body: dict[str, Any], klass: str,
                     slot: str | None = None) -> Any:
        """POST off-loop. A 429 is a DEFINITIVE non-execution (the venue throttled and created nothing), so
        the SAME client_order_id is re-sent ONCE on the SAME lane after a wait — a wing 429 retries on the
        wing lane, ahead of any queued roll/cancel/create. A still-429 returns as-is (the inherited
        ``_reject_place`` exempts it from the consecutive-reject stand-down)."""
        resp = await self._aw.post(path, body, klass=klass, slot=slot)
        if getattr(resp, "status_code", None) != _HTTP_TOO_MANY_REQUESTS:
            return resp
        self.async_rate_limited += 1
        self._bump("rest_rate_limited")
        wait = self._retry_after_s(resp)
        if wait is None:
            wait = self._estimate_pacer_wait() or RATE_LIMIT_RETRY_WAIT_S
        try:
            self.journal.append("rate_limited",
                                {"path": path, "coid": body.get("client_order_id"),
                                 "wait_s": round(wait, 4), "retry": "once", "klass": klass}, self.clock())
        except Exception:  # noqa: BLE001 — telemetry must never break the send path
            pass
        if wait > 0:
            await asyncio.sleep(wait)
        return await self._aw.post(path, body, klass=klass, slot=slot)

    async def _adelete(self, path: str, klass: str, slot: str | None = None) -> Any:
        return await self._aw.delete(path, klass=klass, slot=slot)

    async def _aget(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._aw.get(path, params, klass=CLASS_POLL)

    @staticmethod
    def _slot(oid: Any) -> str | None:
        return str(oid) if oid is not None else None

    # =====================================================================
    # dispatch — the async twin of V33LiveExecutor.on_action + LiveExecutor.on_action
    # =====================================================================
    async def on_action_async(self, action, state, now: float) -> list[Any]:
        k = action.kind
        if k in (V33ActionKind.WOULD_TAKE_BUCKET_NO, V33ActionKind.WOULD_UNWIND_WINGS):
            raise AssertionError(
                f"V33AsyncExecutor received a shakedown twin {k}; the core must be armed (P3-1)")
        if k == V33ActionKind.TAKE_BUCKET_NO:
            return await self._take_bucket_no_async(action, now)
        if k == V33ActionKind.UNWIND_WINGS:
            return await self._unwind_wings_async(action, now)
        if k == ActionKind.PLACE_REST:
            if self.stand_down_reason is not None:
                return self._refuse_place_stood_down(action.client_order_id or "", now, "dispatch")
            self._pending_place_price = action.price
            return await self._place_rest_async(action, now)
        if k == ActionKind.CANCEL_REST:
            return await self._cancel_rest_async(action, now)
        if k == ActionKind.AMEND_REST:
            if self.stand_down_reason is not None:
                return await self._refuse_amend_stood_down(action, now)
            return await self._amend_rest_async(action, now)
        if k in (ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
            # GATE C(iii): a STOOD-DOWN executor still hedges -- wings / retries / cancels are always answered.
            return await self._take_wings_async(state, now)
        if k in (ActionKind.WOULD_PLACE_REST, ActionKind.WOULD_CANCEL_REST,
                 ActionKind.WOULD_AMEND_REST, ActionKind.WOULD_TAKE_WINGS):
            raise AssertionError(
                f"V33AsyncExecutor received a shakedown twin {k}; the core must be armed (P3-1)")
        return []  # STAND_DOWN and any order-free kind

    # =====================================================================
    # PLACE_REST — async twin of V33LiveExecutor._place_rest (pace) + LiveExecutor._place_rest
    # =====================================================================
    async def _place_rest_async(self, action, now: float) -> list[Any]:
        """GATE C(i) (2026-10-03 02:00Z): the coid is registered IN FLIGHT at the very top -- before the pacer
        and the pre-flight GET -- so a CANCEL_REST arriving at ANY point of the call is deferred (the
        pre-fix registration around the POST only let #27's cancel, landing 3 ms before its POST inside the
        pre-flight, fall through as a no-op: #27 then rested owned by nobody and filled naked). Cleared on
        EVERY exit path (finally); a raise also drops any deferred cancel (#115 review N3)."""
        coid = action.client_order_id or ""
        self._inflight_creates.add(coid)
        try:
            return await self._place_rest_inflight_async(action, coid, now)
        except BaseException:
            self._cancel_pending.discard(coid)   # review N3: no deferred cancel may outlive a create that raised
            raise
        finally:
            self._inflight_creates.discard(coid)

    async def _place_rest_inflight_async(self, action, coid: str, now: float) -> list[Any]:
        await self._pacer.acquire_async(COST_CREATE, "create")
        ticker = action.ticker or ""
        exch = self._exch(ticker)
        if exch is None:
            self._cancel_pending.discard(coid)   # nothing was sent; the rejection event resolves the slot
            return self._reject_place(coid, ticker, now, {"reason": "no_exchange_index"})
        violation = await self._pre_place_invariant_async(coid, now, place_price=action.price)
        if violation is not None:
            self._cancel_pending.discard(coid)   # never POSTed; the rejection (with coid) resolves the slot
            return violation
        skip = self._skip_before_post(coid, now)
        if skip is not None:
            return skip
        body = self._rest_body(action, coid, exch)
        self.journal.append("place_rest", {**{k: v for k, v in body.items()
                                              if k != "self_trade_prevention_type"},
                                          "n": action.price, "bucket_Sd": self._bucket_sd(ticker)},
                            self.clock())
        resp = await self._apost(REL_SINGLE_CREATE, body, CLASS_REST, slot=coid)
        self.rests_placed += 1
        self._bump("rest_post")
        if not resp.ok:
            self._cancel_pending.discard(coid)   # nothing rests at the venue; nothing to cancel
            unknown = resp.status_code is None or resp.status_code >= 500
            return self._reject_place(coid, ticker, now, {"status": resp.status_code,
                                                          "body": resp.body, "error": resp.error},
                                      unknown=unknown, n=action.price)
        parsed = parse_single_response(resp.body, side=BUY_NO)
        if parsed.error or parsed.order_id is None:
            self._cancel_pending.discard(coid)
            return self._reject_place(coid, ticker, now,
                                      {"status": resp.status_code, "parsed_error": parsed.error,
                                       "order_id": parsed.order_id})
        oid = parsed.order_id
        self.rest_book[coid] = RestRecord(
            client_order_id=coid, order_id=oid,
            price=action.price if action.price is not None else Decimal(0),
            count=int(action.count), ticker=ticker, bucket_Sd=self._bucket_sd(ticker),
            placed_ts=now, status="live", exchange_index=exch,
            expiration_epoch=self._expiration_epoch(action),
        )
        self._by_order_id[oid] = coid
        self._consecutive_rejects = 0
        self._bump("rest_acked")
        events: list[Any] = [OrderAck(client_order_id=coid, order_id=oid, server_ts=now)]
        if parsed.fill_count > 0:
            events.append(V33Fill(order_id=oid, client_order_id=coid, count=parsed.fill_count,
                                  price=action.price if action.price is not None else Decimal(0),
                                  side="no", server_ts=now, market_ticker=ticker))
        if coid in self._cancel_pending:
            events += await self._cancel_after_ack_async(coid, oid, exch, now)
        return events

    async def _cancel_after_ack_async(self, coid: str, oid: str, exch: int | None, now: float) -> list[Any]:
        """The core (or the stand-down sweep) cancelled this slot while its create was in flight (stand-down
        / bucket change / quote end). Execute that cancel NOW that the venue id is known, so the order never
        rests unowned. The cancel-confirm path books any fill that landed in the ack->DELETE gap (its
        OrderCancelled carries coid / price / ticker, so the core hedges it even off-ladder -- gate A)."""
        self._cancel_pending.discard(coid)
        self._bump("cancel_after_ack")
        rec = self.rest_book.get(coid)
        return await self._delete_and_resolve_async(rec, oid, exch, coid, now, via="cancel_after_ack")

    def _skip_before_post(self, coid: str, now: float) -> list[Any] | None:
        """GATE C: the last check before a create POST. If the slot was cancelled while this create sat in
        the pacer / pre-flight, or the executor stood down meanwhile (a sibling's pre-flight tripped the
        invariant), the POST is NOT sent -- nothing reaches the venue, so nothing can rest unowned. The
        coid-carrying OrderCancelled resolves the core's pending slot (gate B)."""
        if coid in self._cancel_pending:
            self._cancel_pending.discard(coid)
            reason = "cancelled_in_flight"
        elif self.stand_down_reason is not None:
            reason = "stood_down"
        else:
            return None
        self._bump("place_skipped_before_post")
        self.journal.append("place_skipped_before_post",
                            {"client_order_id": coid, "reason": reason,
                             "stand_down_reason": self.stand_down_reason}, self.clock())
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0),
                               client_order_id=coid)]

    def _refuse_place_stood_down(self, coid: str, now: float, where: str) -> list[Any]:
        """GATE C(iii): a stood-down executor refuses every NEW rest (nothing is sent)."""
        self.places_refused_stood_down += 1
        self._bump("place_refused_stood_down")
        self.journal.append("place_refused_stood_down",
                            {"client_order_id": coid, "stand_down_reason": self.stand_down_reason,
                             "where": where}, self.clock())
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0),
                               client_order_id=coid)]

    async def _refuse_amend_stood_down(self, action, now: float) -> list[Any]:
        """GATE C(iii): a stood-down executor refuses an AMEND (no re-price of an owned rest while stood
        down) and CANCELS the order instead -- the stand-down's intent. The core receives the cancel as the
        roll's fallback and, being stood down, does not re-place."""
        self.amends_refused_stood_down += 1
        self._bump("amend_refused_stood_down")
        self.journal.append("amend_refused_stood_down",
                            {"order_id": action.order_id, "client_order_id": action.client_order_id,
                             "stand_down_reason": self.stand_down_reason}, self.clock())
        return await self._cancel_rest_async(
            V32Action(kind=ActionKind.CANCEL_REST, order_id=action.order_id,
                      client_order_id=action.client_order_id), now)

    async def standdown_sweep_async(self, now: float) -> list[Any]:
        """GATE C(ii) (2026-10-03 02:00Z): on an executor stand-down, cancel EVERY order this executor owns
        that may still rest at the venue -- the stand-down used to cancel nothing it owned while the
        stood-down core issued no actions, so #23 / #27 rested until they filled naked. One-shot. Creates
        still IN FLIGHT get a deferred cancel (executed on ack, or the POST is skipped); every other owned
        record with a venue id and no confirmed cancel is DELETEd on the cancel lane, confirmed, and
        reported as ``OrderCancelled(order_id, client_order_id, filled_count_before_cancel, price,
        market_ticker)`` so any racing fill is booked and hedged by the core (gate A)."""
        if self._standdown_swept or self.stand_down_reason is None:
            return []
        self._standdown_swept = True
        deferred = sorted(c for c in self._inflight_creates if c)
        for c in deferred:
            self._cancel_pending.add(c)
        targets: list[RestRecord] = []
        seen: set[str] = set()
        for rec in list(self.rest_book.values()):
            if (rec.order_id is None or rec.order_id in seen or rec.cancel_confirmed_ts is not None
                    or rec.status not in ("live", "filled", "cancel_failed")):
                continue
            seen.add(rec.order_id)
            targets.append(rec)
        unknown = [r.client_order_id for r in self.rest_book.values()
                   if r.order_id is None and r.status == "unknown"]
        info = {"reason": self.stand_down_reason, "cancel": [r.client_order_id for r in targets],
                "deferred_in_flight": deferred, "unknown_outcome_unowned": unknown}
        self.journal.append("standdown_sweep", info, self.clock())
        self._record_alarm("standdown_sweep", info)
        results = await asyncio.gather(
            *(self._cancel_rest_async(V32Action(kind=ActionKind.CANCEL_REST, order_id=r.order_id,
                                                client_order_id=r.client_order_id), now)
              for r in targets),
            return_exceptions=True)
        events: list[Any] = []
        errors = 0
        for r, res in zip(targets, results):
            if isinstance(res, BaseException):
                errors += 1
                self.journal.append("standdown_sweep_error",
                                    {"order_id": r.order_id, "client_order_id": r.client_order_id,
                                     "error": str(res)}, self.clock())
                continue
            events += res
        self.standdown_sweep_cancels += len(targets) - errors
        self.journal.append("standdown_sweep_done",
                            {"cancelled": len(targets) - errors, "errors": errors,
                             "events": len(events)}, self.clock())
        return events

    # =====================================================================
    # pre-PLACE K-aware invariant — async twin of V33LiveExecutor._pre_place_invariant
    # =====================================================================
    async def _venue_resting_ours_async(self, exclude_oid: str | None) -> list[dict[str, Any]] | None:
        """Async twin of V33LiveExecutor._venue_resting_ours (GET status=resting, v33- scoped)."""
        try:
            body = await self._aget(OPEN_ORDERS_PATH, {"status": "resting"})
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33-ASYNC] pre-place open-orders GET failed: %s", e)
            return None
        orders = (body or {}).get("orders") if isinstance(body, dict) else None
        if not isinstance(orders, list):
            return None
        out: list[dict[str, Any]] = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            if not coid.startswith(self.COID_PREFIX):
                continue
            oid = o.get("order_id")
            if oid is None or (exclude_oid is not None and oid == exclude_oid):
                continue
            exch = o.get("exchange_index")
            try:
                exch = int(exch) if exch is not None else self._exch(str(o.get("ticker") or ""))
            except (TypeError, ValueError):
                exch = self._exch(str(o.get("ticker") or ""))
            out.append({"order_id": oid, "client_order_id": coid, "exchange_index": exch})
        return out

    async def _pre_place_invariant_async(self, coid: str, now: float,
                                         place_price: Any = None) -> list[Any] | None:
        """Async twin of V33LiveExecutor._pre_place_invariant. First read decides on a healthy partial
        ladder with NO sleep; an anomaly triggers one ``await asyncio.sleep`` recheck; strays are cancelled
        and the place proceeds; a real overflow/dup stands the hour down (all logic reused via the inherited
        pure ``_filter_phantoms`` / ``_invariant_verdict``).

        ``place_price`` (THIS place's price) is threaded EXPLICITLY into ``_invariant_verdict`` so two
        concurrently-dispatched places never read each other's price off the shared
        ``_pending_place_price`` field across the open-orders GET await (review finding A1). Falls back to
        the instance field only if a caller omits it.

        REVIEW NOTE (B1): this is a BELT that reads venue truth, not the K GATE. Because the read->place is
        not atomic across the GET await, two concurrently-dispatched FIRST-TIME places can each see venue
        ``< K`` and both proceed; the invariant cannot serialize them. The real K-limiter is the CORE (it
        emits at most K place actions), so total resting stays ``<= K`` in practice — do not rely on this
        belt to CATCH a concurrency overflow, only a core/venue disagreement already resting at the venue."""
        if place_price is None:
            place_price = self._pending_place_price
        first = await self._venue_resting_ours_async(self._last_confirmed_gone_oid)
        if not first:
            return None
        first = self._filter_phantoms(first, now)
        if not first:
            return None
        overflow, dup, strays = self._invariant_verdict(first, place_price)
        if not (overflow or dup or strays):
            return None
        self.rest_invariant_rechecks += 1
        self._bump("rest_invariant_recheck")
        await asyncio.sleep(INVARIANT_RECHECK_S)
        reread = await self._venue_resting_ours_async(self._last_confirmed_gone_oid)
        if not reread:
            return None
        reread = self._filter_phantoms(reread, now)
        if not reread:
            return None
        overflow, dup, strays = self._invariant_verdict(reread, place_price)
        if not (overflow or dup or strays):
            return None
        if strays and not (overflow or dup):
            for r in strays:
                await self._cancel_stray_async(r, now)
            self.rest_stray_cancels += len(strays)
            info = {"strays": [r["client_order_id"] for r in strays], "count": len(strays),
                    "coid_attempted": coid}
            self._bump("rest_stray_cancelled")
            self.journal.append("rest_stray_cancelled", info, self.clock())
            self._record_alarm("rest_stray_cancelled", info)
            return None
        detail = {
            "count_resting": len(reread), "k": self.k_rungs, "coid_attempted": coid,
            "overflow": overflow, "dup_price": dup,
            "place_price": (str(place_price) if place_price is not None else None),
            "strays": [r["client_order_id"] for r in strays],
        }
        if overflow:
            self.rest_invariant_overflow += 1
        if dup:
            self.rest_invariant_dup_price += 1
        self.rest_invariant_violations += 1
        self._bump("rest_invariant_violation")
        self.journal.append("rest_invariant_violation", detail, self.clock())
        self._record_alarm("rest_invariant_violation", detail)
        if self.stand_down_reason is None:
            self.stand_down_reason = "rest_invariant_violation"
        # GATE B (2026-10-03 02:00Z): carry the coid. The pre-fix id-less event was attributed by the core
        # to the FIRST pending order (None == None): nine of these evicted nine innocent pending rungs.
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0),
                               client_order_id=coid)]

    async def _cancel_stray_async(self, r: dict[str, Any], now: float) -> None:
        """Async twin of V33LiveExecutor._cancel_stray (priority-paced DELETE; fail-closed)."""
        oid = r.get("order_id")
        if oid is None:
            return
        exch = r.get("exchange_index")
        exch_i = exch if isinstance(exch, int) else None
        try:
            await self._pacer.acquire_async(COST_CANCEL, "stray_cancel", priority=True)
            wr = await self._adelete(cancel_path(oid, exch_i), CLASS_CANCEL, slot=self._slot(oid))
            self.journal.append("stray_cancel",
                                {"order_id": oid, "client_order_id": r.get("client_order_id"),
                                 "exchange_index": exch_i, "status": wr.status_code}, self.clock())
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33-ASYNC] stray cancel failed for %s: %s", oid, e)
            self.journal.append("stray_cancel_error", {"order_id": oid, "error": str(e)}, self.clock())

    # =====================================================================
    # CANCEL_REST — async twin of V33LiveExecutor._cancel_rest (pace) + LiveExecutor._cancel_rest
    # =====================================================================
    async def _cancel_rest_async(self, action, now: float) -> list[Any]:
        await self._pacer.acquire_async(COST_CANCEL, "cancel", priority=True)
        coid = action.client_order_id
        oid = action.order_id
        rec = self.rest_book.get(coid) if coid is not None else None
        if oid is None and rec is not None:
            oid = rec.order_id
        if oid is None and coid is not None and coid in self._inflight_creates:
            # 2026-10-02: the create for this coid is IN FLIGHT (no venue id yet). Reporting "cancelled"
            # here is a lie the core acts on (re-places the slot). Defer: the ack path cancels it.
            self._cancel_pending.add(coid)
            self._bump("cancel_deferred_unacked")
            self.journal.append("cancel_deferred_unacked", {"client_order_id": coid}, self.clock())
            return []
        if oid is None:
            self._bump("cancel_noop")
            return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0),
                                   client_order_id=coid)]
        if rec is None:
            rec = self.attribute(coid=coid, order_id=oid)
        exch = rec.exchange_index if rec is not None else None
        if exch is None and rec is not None:
            exch = self._exch(rec.ticker)
        return await self._delete_and_resolve_async(rec, oid, exch, coid, now)

    async def _delete_and_resolve_async(self, rec, oid: str, exch: int | None, coid, now: float,
                                        via: str | None = None) -> list[Any]:
        """DELETE one owned order on the cancel lane and resolve it by status truth. GATE C: a DELETE already
        in flight for this order (the stand-down sweep racing a core CANCEL_REST, or the ack-path cancel) is
        NOT repeated -- that DELETE's own OrderCancelled resolves the order for the core."""
        if oid in self._cancel_oids_inflight:
            self._bump("cancel_dup_inflight")
            return []
        self._cancel_oids_inflight.add(oid)
        try:
            self.cancels_attempted += 1
            payload = {"order_id": oid, "client_order_id": coid, "exchange_index": exch}
            if via is not None:
                payload["via"] = via
            self.journal.append("cancel_rest", payload, self.clock())
            wr = await self._adelete(cancel_path(oid, exch), CLASS_CANCEL, slot=self._slot(oid))
            self._bump("cancel_delete")
            if wr.status_code == 404:
                self.cancel_404s += 1
            if wr.ok:
                return await self._resolve_cancel_success_async(wr, rec, oid, now)
            return await self._cancel_nonok_async(wr, rec, oid, exch, coid, now)
        finally:
            self._cancel_oids_inflight.discard(oid)

    async def _resolve_cancel_success_async(self, wr, rec, oid: str, now: float) -> list[Any]:
        """Async twin of LiveExecutor._resolve_cancel_success (reduced_by + status-truth MAX)."""
        filled_delete: int | None = None
        rb = _dec_or_none(wr.body.get("reduced_by")) if isinstance(wr.body, dict) else None
        if rb is not None and rec is not None:
            filled_delete = max(0, int(rec.count) - int(rb))
        filled_status, status_fp = await self._confirm_cancel_filled_async(oid, now)
        filled = max(filled_delete or 0, filled_status)
        self.cancels_confirmed += 1
        self._last_confirmed_gone_oid = oid
        # V3.3 D3 (F2 port): the EXACT fractional fill = max(placed - reduced_by, status fp) so a 0.44
        # leg surfaced by the cancel confirm is not truncated to 0 (mirrors LiveExecutor._resolve_cancel_success).
        # REVIEW (fractional integration): ``status_fp`` is THIS cancel's own confirm return, NOT the shared
        # ``self._last_confirm_status_fp`` field -- a cancel-all dispatches N confirm coroutines concurrently,
        # which interleave across the off-loop status-GET await and would clobber a shared field (a sibling's
        # fp read as our own = a phantom fractional fill when our own status poll was unreadable).
        filled_fp = None
        if self._fractional_counts:
            fp_delete = (max(Decimal(0), Decimal(rec.count) - rb)
                         if (rb is not None and rec is not None) else Decimal(0))
            filled_fp = max(fp_delete, status_fp)
        return self._finish_cancel(rec, oid, filled, wr.status_code, rb, now, filled_fp=filled_fp)

    async def _cancel_nonok_async(self, wr, rec, oid: str, exch: int | None, coid, now: float) -> list[Any]:
        """Async twin of LiveExecutor._cancel_nonok (status-truth, shard-aware backoff retries)."""
        exp = rec.expiration_epoch if rec is not None else None
        expired = exp is not None and now >= exp
        st = await self.order_status_async(oid)
        resolved = self._resolve_cancel_from_status(st, rec, oid, wr.status_code, now, expired)
        if resolved is not None:
            return resolved
        last_status = wr.status_code
        for i in range(CANCEL_RETRY_ATTEMPTS):
            await asyncio.sleep(CANCEL_BACKOFF_S[min(i, len(CANCEL_BACKOFF_S) - 1)])
            self.cancels_attempted += 1
            rwr = await self._adelete(cancel_path(oid, exch), CLASS_CANCEL, slot=self._slot(oid))
            self._bump("cancel_delete")
            last_status = rwr.status_code
            if rwr.status_code == 404:
                self.cancel_404s += 1
            if rwr.ok:
                return await self._resolve_cancel_success_async(rwr, rec, oid, now)
            st = await self.order_status_async(oid)
            resolved = self._resolve_cancel_from_status(st, rec, oid, rwr.status_code, now, expired)
            if resolved is not None:
                return resolved
        self.cancel_failed_count += 1
        if rec is not None:
            rec.status = "cancel_failed"
        if self.stand_down_reason is None:
            self.stand_down_reason = "cancel_failed"
        self.journal.append("cancel_failed",
                            {"order_id": oid, "client_order_id": coid, "exchange_index": exch,
                             "delete_status": last_status, "last_status": st.status}, self.clock())
        self._record_alarm("cancel_failed", {"order_id": oid, "exchange_index": exch,
                                             "delete_status": last_status})
        return [OrderCancelled(order_id=oid, server_ts=now, filled_count_before_cancel=Decimal(0),
                               client_order_id=(rec.client_order_id if rec is not None else coid),
                               price=(rec.price if rec is not None else None),
                               market_ticker=(rec.ticker if rec is not None else None))]

    async def _confirm_cancel_filled_async(self, order_id: str, now: float) -> tuple[int, Decimal]:
        """Async twin of LiveExecutor._confirm_cancel_filled (the confirm polls run OFF the loop).
        V3.3 D3 (F2 port): also returns the EXACT fractional fill for the fractional cancel resolution.

        REVIEW (fractional integration): the sync twin stashes the fp in ``self._last_confirm_status_fp``
        and the sync resolver reads that instance field -- safe because the sync path cancels ONE order at a
        time. On the ASYNC path a cancel-all dispatches N confirm coroutines CONCURRENTLY
        (``_dispatch_async`` ``as_completed``): two confirm loops interleave across the off-loop status-GET
        await and both write that one field, so a resolver could read a SIBLING cancel's fp (a phantom
        fractional fill on an order whose own status poll was unreadable). The fp is therefore RETURNED here
        (a per-call local) and read from the return in ``_resolve_cancel_success_async`` -- never cross-read
        off the shared field. (The field is still written for sync-twin parity; it is not read on the async
        path.)"""
        filled = 0
        status_fp = Decimal(0)
        self._last_confirm_status_fp = Decimal(0)
        for i in range(CANCEL_CONFIRM_POLLS):
            st = await self.order_status_async(order_id)
            if st.available:
                filled = st.filled_count
                status_fp = st.filled_count_fp
                self._last_confirm_status_fp = st.filled_count_fp
                if st.status not in ("resting", None) or st.remaining_count == 0:
                    return filled, status_fp
            if i < CANCEL_CONFIRM_POLLS - 1:
                await asyncio.sleep(CANCEL_CONFIRM_INTERVAL_S)
        return filled, status_fp

    async def order_status_async(self, order_id: str) -> OrderStatus:
        """Async twin of LiveExecutor.order_status (fail-closed to unavailable on any error)."""
        try:
            body = await self._aget(ORDER_STATUS_PATH_TMPL.format(order_id=order_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33-ASYNC] order_status GET failed for %s: %s", order_id, e)
            return OrderStatus(order_id, None, 0, None, available=False)
        return parse_order_status(body if isinstance(body, dict) else {}, order_id)

    # =====================================================================
    # AMEND_REST — async twin of V33LiveExecutor._amend_rest (pace) + LiveExecutor._amend_rest
    # =====================================================================
    async def _amend_rest_async(self, action, now: float) -> list[Any]:
        await self._pacer.acquire_async(COST_AMEND, "amend")
        coid_old = action.client_order_id
        coid_new = action.updated_client_order_id or coid_old
        oid = action.order_id
        rec = self.rest_book.get(coid_old) if coid_old else None
        if oid is None and rec is not None:
            oid = rec.order_id
        ticker = action.ticker or (rec.ticker if rec is not None else "")
        exch = rec.exchange_index if rec is not None else None
        if exch is None:
            exch = self._exch(ticker)
        self.amends_attempted += 1
        if oid is None:
            self.amends_failed += 1
            self.journal.append("amend_failed",
                                {"client_order_id": coid_old, "reason": "no_order_id",
                                 "fallback": "cancel_create"}, self.clock())
            return await self._amend_fallback_async(rec, None, exch, coid_old, now)
        body = self._amend_body(action, oid, exch, coid_old, coid_new)
        self.journal.append("amend_rest",
                            {"order_id": oid, "client_order_id": coid_old,
                             "updated_client_order_id": coid_new, "ticker": ticker,
                             "n": action.price, "price": body.get("price"),
                             "exchange_index": exch, "bucket_Sd": self._bucket_sd(ticker)},
                            self.clock())
        resp = await self._apost(amend_path(oid, exch), body, CLASS_ROLL, slot=self._slot(oid))
        self._bump("amend_post")
        if not resp.ok:
            self.amends_failed += 1
            if resp.status_code == 404:
                # 2026-10-02 (00:00Z window): an amend racing a fill comes back 404 -- the order is GONE
                # (filled/cancelled/expired), so resolve it by STATUS TRUTH straight away instead of a DELETE
                # that can only 404 again. The status GET surfaces any fill before the core may re-place.
                self.journal.append("amend_failed",
                                    {"order_id": oid, "client_order_id": coid_old,
                                     "status": resp.status_code, "body": resp.body, "error": resp.error,
                                     "fallback": "status_resolve"}, self.clock())
                exp = rec.expiration_epoch if rec is not None else None
                st = await self.order_status_async(oid)
                resolved = self._resolve_cancel_from_status(st, rec, oid, resp.status_code, now,
                                                            exp is not None and now >= exp)
                if resolved is not None:
                    self._bump("amend_404_status_resolved")
                    return resolved
                # status unavailable / still live (should not happen on a 404): proven DELETE fallback.
                return await self._amend_fallback_async(rec, oid, exch, coid_old, now)
            self.journal.append("amend_failed",
                                {"order_id": oid, "client_order_id": coid_old,
                                 "status": resp.status_code, "body": resp.body, "error": resp.error,
                                 "fallback": "cancel_create"}, self.clock())
            return await self._amend_fallback_async(rec, oid, exch, coid_old, now)
        parsed = parse_single_response(resp.body, side=BUY_NO)
        self.amends_confirmed += 1
        new_price = action.price if action.price is not None else (
            rec.price if rec is not None else Decimal(0))
        new_count = int(action.count) if action.count else (rec.count if rec is not None else 1)
        new_rec = RestRecord(
            client_order_id=coid_new, order_id=oid, price=new_price, count=new_count,
            ticker=ticker, bucket_Sd=self._bucket_sd(ticker), placed_ts=now, status="live",
            exchange_index=exch,
            expiration_epoch=(rec.expiration_epoch if rec is not None
                              else self._expiration_epoch(action)),
        )
        if rec is not None and coid_old is not None and coid_old != coid_new:
            rec.status = "amended"
        self.rest_book[coid_new] = new_rec
        self._by_order_id[oid] = coid_new
        fill_count = int(parsed.fill_count) if parsed.fill_count is not None else 0
        avg_price = parsed.average_fill_price
        avg_fee = parsed.average_fee_paid
        if fill_count > 0:
            self.fills_on_amend += fill_count
            booked_price = avg_price if avg_price is not None else new_price
            new_rec.status = "filled"
            if oid not in self.booked_rest_oids:
                self.booked_rest_oids.add(oid)
                self.fills.append({"leg": "rest", "side": "no", "ticker": ticker,
                                   "price": booked_price, "exec_price": avg_price,
                                   "fee": avg_fee if avg_fee is not None else Decimal(0),
                                   "count": int(fill_count), "bucket_Sd": self._bucket_sd(ticker),
                                   "path": "amend", "client_order_id": coid_new})
            self.journal.append("amend_fill",
                                {"order_id": oid, "client_order_id": coid_new,
                                 "avg_fill_price": avg_price, "avg_fee": avg_fee,
                                 "fill_count": fill_count,
                                 "remaining": str(parsed.remaining_count)}, self.clock())
        self.journal.append("amend_confirmed",
                            {"order_id": oid, "client_order_id": coid_new, "price": body.get("price"),
                             "n": new_price, "remaining_count": str(parsed.remaining_count),
                             "fill_count": fill_count, "average_fill_price": avg_price,
                             "average_fee_paid": avg_fee}, self.clock())
        return [OrderAmended(order_id=oid, client_order_id=coid_new, price=new_price, server_ts=now,
                             remaining_count=parsed.remaining_count, fill_count=Decimal(fill_count),
                             average_fill_price=(avg_price if fill_count > 0 else None))]

    async def _amend_fallback_async(self, rec, oid: str | None, exch: int | None, coid_old,
                                    now: float) -> list[Any]:
        """Async twin of LiveExecutor._amend_fallback (cancel -> confirm -> create; the create is the
        core's next PLACE_REST through the K-aware invariant)."""
        self.amend_fallbacks += 1
        if oid is None:
            self._bump("amend_fallback_noop")
            return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0),
                                   client_order_id=coid_old)]
        return await self._delete_and_resolve_async(rec, oid, exch, coid_old, now, via="amend_fallback")

    # =====================================================================
    # TAKE_WINGS / RETRY_WING — async twin of V33LiveExecutor._take_wings (chunked; WING lane)
    # =====================================================================
    async def _take_wings_async(self, state, now: float) -> list[Any]:
        pending_all = [lg for lg in state.wing_legs if lg.status == "pending"]
        if not pending_all:
            return []
        if self.clock() < self._wing_backoff_until:
            # 429 BACKOFF (2026-10-02): inside the window -> no send; report unfilled so the core's next
            # RETRY_WING (>= its own floor) re-asks once the window has passed.
            self.wing_backoff_skips += 1
            self._bump("wing_backoff_skip")
            return [Fill(order_id=None, client_order_id=lg.client_order_id, count=Decimal(0),
                         price=lg.limit, side=lg.side, server_ts=now) for lg in pending_all]
        # WING RETRY-STORM BELT: claim one in-flight IOC per missing leg; DROP a retry for a leg that
        # already has an IOC in flight (count it, no journal record each). The claim is check-and-add on the
        # single loop thread (no await between), so it is atomic. Cleared in the finally below when the take
        # returns — the core's next RETRY_WING (subject to its own re-emission floor) then re-claims.
        pending: list[Any] = []
        claimed: list[tuple[int, str]] = []
        for lg in pending_all:
            key = (lg.batch, lg.side)
            if key in self._wing_inflight_legs:
                self.wing_retries_dropped += 1
                self._bump("wing_retry_dropped")
                continue
            self._wing_inflight_legs.add(key)
            claimed.append(key)
            pending.append(lg)
        if not pending:
            return []
        try:
            return await self._take_wings_send(pending, now)
        finally:
            for key in claimed:
                self._wing_inflight_legs.discard(key)

    async def _take_wings_send(self, pending: list[Any], now: float) -> list[Any]:
        events: list[Any] = []
        entries: list[dict[str, Any]] = []
        chunk_owner: dict[str, tuple[int, str]] = {}
        leg_by_key: dict[tuple[int, str], Any] = {}
        for lg in pending:
            key = (lg.batch, lg.side)
            leg_by_key[key] = lg
            exch = self._exch(lg.ticker)
            if exch is None:
                self._record_alarm("wing_no_exchange_index", {"ticker": lg.ticker, "side": lg.side})
                continue
            # D3 (review, F1 port): remaining is Decimal (a 1.44 fill hedges 1.44 lots, not int(1.44)=1).
            # Chunk as ceil(remaining/cap) with the LAST chunk carrying the fractional remainder (Σ == remaining).
            remaining = self._dc(lg.count) - self._wing_filled.get(key, Decimal(0))
            if remaining <= 0:
                continue
            n_chunks = ceil(remaining / self.wing_cap)
            _sum_chunks = Decimal(0)
            for c in range(n_chunks):
                cnt = min(Decimal(self.wing_cap), remaining - c * self.wing_cap)
                _sum_chunks += cnt
                chunk_coid = self._mint_wing_coid()
                entries.append(self._wing_chunk_entry(lg, exch, cnt, chunk_coid))
                chunk_owner[chunk_coid] = key
                self.wing_coids.add(chunk_coid)
            assert _sum_chunks == remaining, f"wing chunk sum {_sum_chunks} != remaining {remaining}"
        for lg in pending:
            if (lg.batch, lg.side) not in {v for v in chunk_owner.values()} and self._exch(lg.ticker) is None:
                events.append(Fill(order_id=None, client_order_id=lg.client_order_id, count=Decimal(0),
                                   price=lg.limit, side=lg.side, server_ts=now))
        if not entries:
            return events
        # 2026-10-02 (02:00Z 429 storm): SUB-BATCHES that each fit the venue's write bucket. The first is
        # served now (priority); the rest wait only until the bucket can FIT their cost. Kalshi rejects a
        # batch WHOLE when its cost exceeds the balance, so one 12-order burst could never pass on Basic.
        send_ts = self.clock()
        max_orders = wing_batch_max_orders(self._pacer.size)
        sub_batches = split_wing_batches(entries, chunk_owner, max_orders)
        parsed = []
        failed: dict[str, tuple[Any, Any]] = {}          # chunk coid -> (status, error) of a non-ok sub-batch
        rate_limited: set[str] = set()                   # chunk coids the venue answered 429 (created nothing)
        for i, sub in enumerate(sub_batches):
            await self._pacer.acquire_async(COST_CREATE * len(sub), "wing_take", priority=True, fit=i > 0)
            self.wing_batches += 1
            self.wing_chunks += len(sub)
            self._bump("wing_batch")
            self.journal.append("take_wings", {"legs": sub, "chunked": True, "sub_batch": i + 1,
                                               "sub_batches": len(sub_batches)}, self.clock())
            if len(sub) == 1:
                resp = await self._apost(REL_SINGLE_CREATE, sub[0], CLASS_WING)
                p_ = [parse_single_response(resp.body, side=chunk_owner[sub[0]["client_order_id"]][1])] \
                    if resp.ok else []
            else:
                resp = await self._apost(REL_BATCH_CREATE, build_batch(sub), CLASS_WING)
                p_ = parse_batch_response(resp.body) if resp.ok else []
            if resp.ok:
                parsed += p_
                self._wing_429_streak = 0
                continue
            for e in sub:
                failed[e["client_order_id"]] = (resp.status_code, resp.error)
            if resp.status_code == _HTTP_TOO_MANY_REQUESTS:
                rate_limited.update(e["client_order_id"] for e in sub)
                self._note_wing_rate_limited()
        from collections import defaultdict as _dd
        agg_count: dict[tuple[int, str], Decimal] = _dd(lambda: Decimal(0))
        agg_notional: dict[tuple[int, str], Decimal] = _dd(lambda: Decimal(0))
        by_coid = {r.client_order_id: r for r in parsed if r.client_order_id in chunk_owner}
        # Chunks whose OUTCOME IS UNKNOWN: a sub-batch with no readable response (transport / 5xx), or a
        # chunk absent from / errored in the body. A chunk the venue answered with fill_count 0 is a
        # DEFINITIVE no-fill (IOC) and a 429 is a DEFINITIVE non-execution -- neither is unknown.
        unknown_keys: set[tuple[int, str]] = set()
        for chunk_coid, key in chunk_owner.items():
            r = by_coid.get(chunk_coid)
            if chunk_coid in rate_limited:
                continue
            if chunk_coid in failed or r is None or r.error:
                unknown_keys.add(key)
            elif getattr(r, "order_id", None):
                self._wing_order_ids.add(r.order_id)
        for chunk_coid, key in chunk_owner.items():
            r = by_coid.get(chunk_coid)
            if r is None or chunk_coid in failed or r.error or r.fill_count <= 0:
                continue
            side = key[1]
            nr = normalize_fill_to_side(r, side)
            price = nr.average_fill_price if nr.average_fill_price is not None else leg_by_key[key].limit
            fc = self._dc(nr.fill_count)          # D3 (review, F1 port): fractional-safe chunk fill
            agg_count[key] += fc
            agg_notional[key] += Decimal(price) * fc
            self.fills.append({"leg": "wing", "side": side, "ticker": leg_by_key[key].ticker,
                               "price": price, "fee": nr.average_fee_paid, "count": fc, "ts": now,
                               "path": "wing_chunk", "client_order_id": chunk_coid})
            self._bump("wing_fill")
        # VENUE CONFIRM (2026-10-02): for a leg with an unknown-outcome chunk, ask the venue what it filled
        # for us on that ticker/side since the send (excluding chunks we already read) BEFORE reporting the
        # leg unfilled. A lost response on an executed IOC must not become a second, duplicate wing buy.
        for key in sorted(unknown_keys):
            lg = leg_by_key[key]
            readable = agg_count.get(key, Decimal(0))
            # ROOM for THIS take = leg count - prior takes' booked (``_wing_filled``) - this take's READABLE
            # chunk fills. The venue GET already EXCLUDES this take's readable chunks (their order_ids are in
            # ``_wing_order_ids``), so ``venue`` is the UNATTRIBUTED fill count -- book it ADDITIVELY on top
            # of the readable chunks, capped by ``room``. The old code compared venue (readable-excluded)
            # against agg_count (readable-included) and booked the difference: on a leg with BOTH readable
            # and unknown chunks (a lost sub-batch beside a served one) the condition failed, the real
            # unknown fills were dropped, the leg reported unfilled, and the core RE-BOUGHT the chunk that
            # had in fact executed -> over-hedge.
            room = self._dc(lg.count) - self._wing_filled.get(key, Decimal(0)) - readable
            if room <= 0:
                continue
            venue = await self._venue_wing_fills_async(lg.ticker, key[1], send_ts)
            extra = min(venue, room)
            self.wing_venue_confirms += 1
            self._bump("wing_venue_confirm")
            fs = sorted({str(failed[c]) for c, k in chunk_owner.items() if k == key and c in failed})
            self.journal.append("wing_venue_confirm",
                                {"ticker": lg.ticker, "side": key[1], "batch": key[0],
                                 "failed_responses": fs,
                                 "parsed_count": str(readable),
                                 "venue_count": str(venue), "confirmed": str(extra)}, self.clock())
            if extra > 0:
                self.wing_venue_confirmed_count += extra
                # price unknown for the lost chunk -> book at the leg's limit (the most we could have paid).
                agg_count[key] = readable + extra
                agg_notional[key] += Decimal(lg.limit) * extra
                self.fills.append({"leg": "wing", "side": key[1], "ticker": lg.ticker,
                                   "price": Decimal(lg.limit), "fee": None, "count": extra, "ts": now,
                                   "path": "wing_venue_confirm", "client_order_id": None})
                self._bump("wing_fill")
        for lg in pending:
            key = (lg.batch, lg.side)
            got = agg_count.get(key, Decimal(0))
            self._wing_filled[key] += got
            self._wing_notional[key] += agg_notional.get(key, Decimal(0))
            total = self._wing_filled[key]
            if total >= self._dc(lg.count) and got > 0:
                avg = (self._wing_notional[key] / total) if total else lg.limit
                events.append(Fill(order_id=None, client_order_id=lg.client_order_id, count=total,
                                   price=avg, side=lg.side, server_ts=now))
            elif key in leg_by_key:
                events.append(Fill(order_id=None, client_order_id=lg.client_order_id, count=Decimal(0),
                                   price=lg.limit, side=lg.side, server_ts=now))
        return events

    def _note_wing_rate_limited(self) -> None:
        """A wing sub-batch came back 429 (after the one-shot same-lane retry): a DEFINITIVE non-execution.
        Grow the backoff window base * 2**(streak-1), capped; journal it once per occurrence."""
        self.wing_rate_limited_batches += 1
        self._wing_429_streak += 1
        delay = min(WING_429_BACKOFF_MAX_S, WING_429_BACKOFF_BASE_S * (2 ** (self._wing_429_streak - 1)))
        self._wing_backoff_until = self.clock() + delay
        self._bump("wing_backoff")
        self.journal.append("wing_backoff", {"streak": self._wing_429_streak, "delay_s": delay},
                            self.clock())

    async def _venue_wing_fills_async(self, ticker: str, side: str, since_ts: float) -> Decimal:
        """2026-10-02 (Brad): venue truth for a wing chunk whose POST response was LOST or non-2xx. Sums
        OUR fills on ``ticker``/``side`` created at/after ``since_ts`` (5 s skew allowance; the laptop clock
        runs ~0.3 s fast) whose order_id is NOT one of the chunks we already read (``_wing_order_ids``).
        Fail-closed to 0 on any error -- the existing 'unfilled -> retry' path then applies.

        Every COUNTED fill's order_id is recorded into ``_wing_order_ids`` so a LATER confirm (a subsequent
        take whose own response is also lost) inside the 5 s skew window can never re-count the SAME venue
        fill -- without this, a lost-response retry would see a prior take's already-booked fill again and
        either over-book the leg or phantom-hedge a chunk that never executed."""
        try:
            body = await self._aget(FILLS_PATH, {"ticker": ticker, "limit": 100,
                                                  "min_ts": int(since_ts - WING_CONFIRM_SKEW_S)})
        except Exception as e:  # noqa: BLE001
            self._record_alarm("wing_confirm_get_failed",
                               {"ticker": ticker, "side": side, "error": str(e)})
            return Decimal(0)
        fills = body.get("fills") if isinstance(body, dict) else None
        if not isinstance(fills, list):
            return Decimal(0)
        total = Decimal(0)
        for f in fills:
            if not isinstance(f, dict) or f.get("ticker") != ticker or f.get("side") != side:
                continue
            if f.get("order_id") in self._wing_order_ids:
                continue
            ts = _iso_to_epoch(f.get("created_time"))
            if ts is not None and ts < since_ts - WING_CONFIRM_SKEW_S:
                continue
            c = _dec_or_none(f.get("count_fp"))
            if c is None and f.get("count") is not None:
                c = _dec_or_none(f.get("count"))
            if c is not None and c > 0:
                total += c
                oid = f.get("order_id")
                if oid:
                    self._wing_order_ids.add(oid)
        return total

    # =====================================================================
    # OPTIONAL batch create — async twin of V33LiveExecutor.place_batch / _place_one_chunk
    # =====================================================================
    async def place_batch_async(self, actions: list, now: float) -> list[Any]:
        events: list[Any] = []
        chunk: list = []
        for a in actions:
            chunk.append(a)
            if len(chunk) >= self.batch_create_max:
                events += await self._place_one_chunk_async(chunk, now)
                chunk = []
        if chunk:
            events += await self._place_one_chunk_async(chunk, now)
        return events

    async def _place_one_chunk_async(self, actions: list, now: float) -> list[Any]:
        """GATE G (#115 residual): the batch create gets the SAME in-flight / deferred-cancel handling as the
        single path -- every coid of the chunk is registered in flight at the top (before the pre-flights),
        a slot cancelled or an executor stood down before the POST is never sent, a slot cancelled during
        the POST is cancelled on its ack, and the registration is cleared on every exit path."""
        coids = [a.client_order_id or "" for a in actions]
        for c in coids:
            self._inflight_creates.add(c)
        try:
            return await self._place_one_chunk_inflight_async(actions, now)
        except BaseException:
            for c in coids:
                self._cancel_pending.discard(c)   # review N3: no deferred cancel outlives a create that raised
            raise
        finally:
            for c in coids:
                self._inflight_creates.discard(c)

    async def _place_one_chunk_inflight_async(self, actions: list, now: float) -> list[Any]:
        events: list[Any] = []
        entries: list[dict[str, Any]] = []
        by_coid: dict[str, Any] = {}
        for a in actions:
            coid = a.client_order_id or ""
            ticker = a.ticker or ""
            if self.stand_down_reason is not None:
                events += self._refuse_place_stood_down(coid, now, "batch")
                self._cancel_pending.discard(coid)
                continue
            exch = self._exch(ticker)
            if exch is None:
                self._cancel_pending.discard(coid)
                events += self._reject_place(coid, ticker, now, {"reason": "no_exchange_index"})
                continue
            self._pending_place_price = a.price
            violation = await self._pre_place_invariant_async(coid, now, place_price=a.price)
            if violation is not None:
                self._cancel_pending.discard(coid)
                events += violation
                continue
            entries.append(self._rest_body(a, coid, exch))
            by_coid[coid] = a
        # last check before the POST (a cancel / stand-down that landed during the pre-flights).
        kept_entries: list[dict[str, Any]] = []
        for e in entries:
            skip = self._skip_before_post(e.get("client_order_id") or "", now)
            if skip is not None:
                events += skip
                by_coid.pop(e.get("client_order_id") or "", None)
                continue
            kept_entries.append(e)
            a = by_coid[e.get("client_order_id") or ""]
            self.journal.append("place_rest", {"client_order_id": a.client_order_id, "ticker": a.ticker,
                                               "n": a.price, "batch": True}, self.clock())
        entries = kept_entries
        if not entries:
            return events
        await self._pacer.acquire_async(COST_CREATE * len(entries), "batch_create")
        self.batch_creates += 1
        self._bump("rest_batch_post")
        if len(entries) == 1:
            resp = await self._apost(REL_SINGLE_CREATE, entries[0], CLASS_REST)
            parsed = [parse_single_response(resp.body, side=BUY_NO)] if resp.ok else []
        else:
            resp = await self._apost(REL_BATCH_CREATE, build_batch(entries), CLASS_REST)
            parsed = parse_batch_response(resp.body) if resp.ok else []
        by_parsed = {p.client_order_id: p for p in parsed if p.client_order_id}
        for coid, a in by_coid.items():
            p = by_parsed.get(coid)
            if not resp.ok or p is None or p.error or p.order_id is None:
                self._cancel_pending.discard(coid)   # nothing rests at the venue; nothing to cancel
                events += self._reject_place(coid, a.ticker or "", now,
                                             {"status": resp.status_code, "batch": True})
                continue
            oid = p.order_id
            exch = self._exch(a.ticker or "")
            self.rest_book[coid] = RestRecord(
                client_order_id=coid, order_id=oid,
                price=a.price if a.price is not None else Decimal(0),
                count=int(a.count), ticker=a.ticker or "", bucket_Sd=self._bucket_sd(a.ticker or ""),
                placed_ts=now, status="live", exchange_index=exch,
                expiration_epoch=self._expiration_epoch(a),
            )
            self._by_order_id[oid] = coid
            self.rests_placed += 1
            self._consecutive_rejects = 0
            self._bump("rest_acked")
            events.append(OrderAck(client_order_id=coid, order_id=oid, server_ts=now))
            if p.fill_count and p.fill_count > 0:
                events.append(V33Fill(order_id=oid, client_order_id=coid, count=p.fill_count,
                                      price=a.price if a.price is not None else Decimal(0),
                                      side="no", server_ts=now, market_ticker=a.ticker or None))
            if coid in self._cancel_pending:
                events += await self._cancel_after_ack_async(coid, oid, exch, now)
        return events

    # =====================================================================
    # batched order-status poll — async twin of V33LiveExecutor.poll_orders_for_bucket
    # =====================================================================
    async def poll_orders_for_bucket_async(self, ticker: str) -> dict[str, Decimal]:
        """D3 (F2 port): the cumulative fill is a DECIMAL (``fill_count_fp``), not int-truncated -- a 0.44
        poll-discovered fill must not vanish (mirrors LiveExecutor.poll_orders_for_bucket)."""
        try:
            body = await self._aget(OPEN_ORDERS_PATH, {"ticker": ticker})
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33-ASYNC] batched order poll failed for %s: %s", ticker, e)
            return {}
        orders = (body or {}).get("orders") if isinstance(body, dict) else None
        if not isinstance(orders, list):
            return {}
        out: dict[str, Decimal] = {}
        for o in orders:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            oid = o.get("order_id")
            if not coid.startswith(self.COID_PREFIX) or oid is None:
                continue
            fc = o.get("fill_count_fp")
            if fc is None:
                fc = o.get("fill_count")
            try:
                out[str(oid)] = Decimal(str(fc)) if fc is not None else Decimal(0)
            except Exception:  # noqa: BLE001
                out[str(oid)] = Decimal(0)
        return out

    # =====================================================================
    # PRINT-THROUGH (dormant unless params.print_through) — async twins (WING lane)
    # =====================================================================
    async def _take_bucket_no_async(self, action, now: float) -> list[Any]:
        coid = action.client_order_id
        ticker = (action.legs[0].ticker if action.legs else action.ticker) or ""
        limit = action.legs[0].limit if action.legs else action.price
        # D3 (F2/F1 port): ``want`` is Decimal (fractional-safe); chunk as ceil(want/cap) with the LAST
        # chunk carrying the fractional remainder (sum of chunk counts == want). Mirrors _take_bucket_no.
        want = self._dc(action.count or (action.legs[0].count if action.legs else 0))
        exch = self._exch(ticker)
        if exch is None or limit is None or want <= 0:
            self._record_alarm("print_through_complete_unrouted", {"ticker": ticker, "count": str(want)})
            return [Fill(order_id=None, client_order_id=coid, count=Decimal(0),
                         price=(limit or Decimal(0)), side=BUY_NO, server_ts=now)] if coid else []
        got = Decimal(0)
        notional = Decimal(0)
        n_chunks = ceil(want / self.wing_cap)
        entries: list[dict[str, Any]] = []
        _sum_chunks = Decimal(0)
        for c in range(n_chunks):
            cnt = min(Decimal(self.wing_cap), want - c * self.wing_cap)
            _sum_chunks += cnt
            entries.append(self._pt_taker_entry(ticker, BUY_NO, "buy", cnt, limit, exch,
                                                self._mint_wing_coid()))
        assert _sum_chunks == want, f"pt-complete chunk sum {_sum_chunks} != want {want}"
        await self._pacer.acquire_async(COST_CREATE * len(entries), "print_through_complete", priority=True)
        self.pt_bucket_no_takes += 1
        self._bump("pt_bucket_no_take")
        self.journal.append("print_through_complete",
                            {"ticker": ticker, "limit": str(limit), "count": str(want),
                             "chunks": len(entries)}, self.clock())
        if len(entries) == 1:
            resp = await self._apost(REL_SINGLE_CREATE, entries[0], CLASS_WING)
            parsed = [parse_single_response(resp.body, side=BUY_NO)] if resp.ok else []
        else:
            resp = await self._apost(REL_BATCH_CREATE, build_batch(entries), CLASS_WING)
            parsed = parse_batch_response(resp.body) if resp.ok else []
        for r in parsed:
            if r is None or r.error or not r.fill_count or r.fill_count <= 0:
                continue
            nr = normalize_fill_to_side(r, BUY_NO)
            fc = self._dc(nr.fill_count)
            price = nr.average_fill_price if nr.average_fill_price is not None else limit
            got += fc
            notional += Decimal(price) * fc
        self.pt_bucket_no_fills += got
        self._bump("pt_bucket_no_fill", got)
        avg = (notional / got) if got else limit
        if got < want:
            self._bump("pt_bucket_no_short")
            self._record_alarm("print_through_complete_short",
                               {"ticker": ticker, "wanted": str(want), "filled": str(got)})
            self.journal.append("print_through_complete_short",
                                {"ticker": ticker, "wanted": str(want), "filled": str(got)}, self.clock())
        return [Fill(order_id=None, client_order_id=coid, count=got, price=avg, side=BUY_NO,
                     server_ts=now)] if coid else []

    async def _unwind_wings_async(self, action, now: float) -> list[Any]:
        entries: list[dict[str, Any]] = []
        want = Decimal(0)
        for lg in action.legs:
            exch = self._exch(lg.ticker)
            lg_count = self._dc(lg.count)                      # D3 (F1 port): fractional-safe
            if exch is None or lg_count <= 0:
                self._record_alarm("print_through_unwind_unrouted",
                                   {"ticker": lg.ticker, "side": lg.side, "count": str(lg_count)})
                continue
            want += lg_count
            n_chunks = ceil(lg_count / self.wing_cap)
            _sum_chunks = Decimal(0)
            for c in range(n_chunks):
                cnt = min(Decimal(self.wing_cap), lg_count - c * self.wing_cap)
                _sum_chunks += cnt
                entries.append(self._pt_taker_entry(lg.ticker, lg.side, "sell", cnt, lg.limit, exch,
                                                    self._mint_wing_coid()))
            assert _sum_chunks == lg_count, f"unwind chunk sum {_sum_chunks} != leg {lg_count}"
        if not entries:
            return []
        await self._pacer.acquire_async(COST_CREATE * len(entries), "print_through_unwind", priority=True)
        self.pt_unwinds += 1
        self._bump("pt_unwind")
        self.journal.append("print_through_unwind",
                            {"legs": [{"ticker": e.get("ticker"), "count": e.get("count")}
                                      for e in entries], "chunks": len(entries)}, self.clock())
        if len(entries) == 1:
            resp = await self._apost(REL_SINGLE_CREATE, entries[0], CLASS_WING)
            parsed = [parse_single_response(resp.body, side=BUY_NO)] if resp.ok else []
        else:
            resp = await self._apost(REL_BATCH_CREATE, build_batch(entries), CLASS_WING)
            parsed = parse_batch_response(resp.body) if resp.ok else []
        sold = Decimal(0)
        for r in parsed:
            if r is not None and not r.error and r.fill_count and r.fill_count > 0:
                sold += self._dc(r.fill_count)
        if sold < want:
            self.pt_unwind_shortfalls += 1
            self._bump("pt_unwind_short")
            self._record_alarm("print_through_unwind_short", {"wanted": str(want), "sold": str(sold)})
            self.journal.append("print_through_unwind_short",
                                {"wanted": str(want), "sold": str(sold)}, self.clock())
            if self.stand_down_reason is None:
                self.stand_down_reason = "print_through_unwind_short"
        return []


# ===========================================================================
# Startup safety (async twin of cancel_stale_open_orders) — off-loop crash-recovery sweep
# ===========================================================================
async def cancel_stale_open_orders_async(async_writer: AsyncOrderWriter, journal: Any,
                                         clock) -> dict[str, Any]:
    """Async twin of V33 cancel_stale_open_orders: GET our resting orders and DELETE any KXBTC* order whose
    coid is ``v33-*`` (a prior crashed V3.3 process); NEVER touches v32-* or foreign orders. Fail-closed."""
    from service.v33.executor import _KXBTC_PREFIX, _V33_COID_PREFIX
    result = {"found": 0, "cancelled": 0, "errors": 0, "skipped_foreign": 0}
    try:
        body = await async_writer.get(OPEN_ORDERS_PATH, {"status": "resting"}, klass=CLASS_POLL)
    except Exception as e:  # noqa: BLE001
        journal.append("startup_open_orders_read_failed", {"error": str(e)}, clock())
        return result
    orders = (body or {}).get("orders") if isinstance(body, dict) else None
    if not isinstance(orders, list):
        return result
    for o in orders:
        if not isinstance(o, dict):
            continue
        ticker = str(o.get("ticker") or o.get("market_ticker") or "")
        oid = o.get("order_id")
        if not ticker.startswith(_KXBTC_PREFIX) or not oid:
            continue
        coid = o.get("client_order_id")
        if not (coid and str(coid).startswith(_V33_COID_PREFIX)):
            result["skipped_foreign"] += 1
            journal.append("startup_skip_foreign_order",
                           {"ticker": ticker, "order_id": oid, "client_order_id": coid}, clock())
            continue
        result["found"] += 1
        exch = o.get("exchange_index")
        try:
            exch = int(exch) if exch is not None else None
        except (TypeError, ValueError):
            exch = None
        wr = await async_writer.delete(cancel_path(oid, exch), klass=CLASS_CANCEL, slot=str(oid))
        if wr.ok or wr.status_code == 404:
            result["cancelled"] += 1
        else:
            result["errors"] += 1
        journal.append("startup_cancel", {"ticker": ticker, "order_id": oid,
                                          "exchange_index": exch, "status": wr.status_code}, clock())
    journal.append("startup_cancel_sweep", result, clock())
    return result
