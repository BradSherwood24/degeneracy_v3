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
from service.v32.actions import ActionKind
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
from service.v33.actions import V33ActionKind
from service.v33.async_writer import (
    CLASS_CANCEL,
    CLASS_POLL,
    CLASS_REST,
    CLASS_ROLL,
    CLASS_WING,
    AsyncOrderWriter,
)
from service.v33.executor import (
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
            self._pending_place_price = action.price
            return await self._place_rest_async(action, now)
        if k == ActionKind.CANCEL_REST:
            return await self._cancel_rest_async(action, now)
        if k == ActionKind.AMEND_REST:
            return await self._amend_rest_async(action, now)
        if k in (ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
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
        await self._pacer.acquire_async(COST_CREATE, "create")
        coid = action.client_order_id or ""
        ticker = action.ticker or ""
        exch = self._exch(ticker)
        if exch is None:
            return self._reject_place(coid, ticker, now, {"reason": "no_exchange_index"})
        violation = await self._pre_place_invariant_async(coid, now, place_price=action.price)
        if violation is not None:
            return violation
        body = self._rest_body(action, coid, exch)
        self.journal.append("place_rest", {**{k: v for k, v in body.items()
                                              if k != "self_trade_prevention_type"},
                                          "n": action.price, "bucket_Sd": self._bucket_sd(ticker)},
                            self.clock())
        resp = await self._apost(REL_SINGLE_CREATE, body, CLASS_REST, slot=coid)
        self.rests_placed += 1
        self._bump("rest_post")
        if not resp.ok:
            unknown = resp.status_code is None or resp.status_code >= 500
            return self._reject_place(coid, ticker, now, {"status": resp.status_code,
                                                          "body": resp.body, "error": resp.error},
                                      unknown=unknown, n=action.price)
        parsed = parse_single_response(resp.body, side=BUY_NO)
        if parsed.error or parsed.order_id is None:
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
            events.append(Fill(order_id=oid, client_order_id=coid, count=parsed.fill_count,
                               price=action.price if action.price is not None else Decimal(0),
                               side="no", server_ts=now))
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
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]

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
        if oid is None:
            self._bump("cancel_noop")
            return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]
        if rec is None:
            rec = self.attribute(coid=coid, order_id=oid)
        exch = rec.exchange_index if rec is not None else None
        if exch is None and rec is not None:
            exch = self._exch(rec.ticker)
        self.cancels_attempted += 1
        self.journal.append("cancel_rest",
                            {"order_id": oid, "client_order_id": coid, "exchange_index": exch},
                            self.clock())
        wr = await self._adelete(cancel_path(oid, exch), CLASS_CANCEL, slot=self._slot(oid))
        self._bump("cancel_delete")
        if wr.status_code == 404:
            self.cancel_404s += 1
        if wr.ok:
            return await self._resolve_cancel_success_async(wr, rec, oid, now)
        return await self._cancel_nonok_async(wr, rec, oid, exch, coid, now)

    async def _resolve_cancel_success_async(self, wr, rec, oid: str, now: float) -> list[Any]:
        """Async twin of LiveExecutor._resolve_cancel_success (reduced_by + status-truth MAX)."""
        filled_delete: int | None = None
        rb = _dec_or_none(wr.body.get("reduced_by")) if isinstance(wr.body, dict) else None
        if rb is not None and rec is not None:
            filled_delete = max(0, int(rec.count) - int(rb))
        filled_status = await self._confirm_cancel_filled_async(oid, now)
        filled = max(filled_delete or 0, filled_status)
        self.cancels_confirmed += 1
        self._last_confirmed_gone_oid = oid
        # V3.3 D3 (F2 port): the EXACT fractional fill = max(placed - reduced_by, status fp) so a 0.44
        # leg surfaced by the cancel confirm is not truncated to 0 (mirrors LiveExecutor._resolve_cancel_success).
        filled_fp = None
        if self._fractional_counts:
            fp_delete = (max(Decimal(0), Decimal(rec.count) - rb)
                         if (rb is not None and rec is not None) else Decimal(0))
            filled_fp = max(fp_delete, self._last_confirm_status_fp)
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
        return [OrderCancelled(order_id=oid, server_ts=now, filled_count_before_cancel=Decimal(0))]

    async def _confirm_cancel_filled_async(self, order_id: str, now: float) -> int:
        """Async twin of LiveExecutor._confirm_cancel_filled (the confirm polls run OFF the loop).
        V3.3 D3 (F2 port): also stashes the EXACT fractional fill in ``self._last_confirm_status_fp`` for
        the fractional cancel resolution (V3.2 ignores it)."""
        filled = 0
        self._last_confirm_status_fp = Decimal(0)
        for i in range(CANCEL_CONFIRM_POLLS):
            st = await self.order_status_async(order_id)
            if st.available:
                filled = st.filled_count
                self._last_confirm_status_fp = st.filled_count_fp
                if st.status not in ("resting", None) or st.remaining_count == 0:
                    return filled
            if i < CANCEL_CONFIRM_POLLS - 1:
                await asyncio.sleep(CANCEL_CONFIRM_INTERVAL_S)
        return filled

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
            return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]
        self.cancels_attempted += 1
        self.journal.append("cancel_rest",
                            {"order_id": oid, "client_order_id": coid_old, "exchange_index": exch,
                             "via": "amend_fallback"}, self.clock())
        wr = await self._adelete(cancel_path(oid, exch), CLASS_CANCEL, slot=self._slot(oid))
        self._bump("cancel_delete")
        if wr.status_code == 404:
            self.cancel_404s += 1
        if wr.ok:
            return await self._resolve_cancel_success_async(wr, rec, oid, now)
        return await self._cancel_nonok_async(wr, rec, oid, exch, coid_old, now)

    # =====================================================================
    # TAKE_WINGS / RETRY_WING — async twin of V33LiveExecutor._take_wings (chunked; WING lane)
    # =====================================================================
    async def _take_wings_async(self, state, now: float) -> list[Any]:
        pending_all = [lg for lg in state.wing_legs if lg.status == "pending"]
        if not pending_all:
            return []
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
        await self._pacer.acquire_async(COST_CREATE * len(entries), "wing_take", priority=True)
        self.wing_batches += 1
        self.wing_chunks += len(entries)
        self._bump("wing_batch")
        if len(entries) == 1:
            self.journal.append("take_wings", {"legs": entries, "chunked": True}, self.clock())
            resp = await self._apost(REL_SINGLE_CREATE, entries[0], CLASS_WING)
            parsed = [parse_single_response(resp.body, side=chunk_owner[entries[0]["client_order_id"]][1])] \
                if resp.ok else []
        else:
            self.journal.append("take_wings", {"legs": entries, "chunked": True}, self.clock())
            resp = await self._apost(REL_BATCH_CREATE, build_batch(entries), CLASS_WING)
            parsed = parse_batch_response(resp.body) if resp.ok else []
        from collections import defaultdict as _dd
        agg_count: dict[tuple[int, str], Decimal] = _dd(lambda: Decimal(0))
        agg_notional: dict[tuple[int, str], Decimal] = _dd(lambda: Decimal(0))
        by_coid = {r.client_order_id: r for r in parsed if r.client_order_id in chunk_owner}
        for chunk_coid, key in chunk_owner.items():
            r = by_coid.get(chunk_coid)
            if r is None or not resp.ok or r.error or r.fill_count <= 0:
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
        events: list[Any] = []
        entries: list[dict[str, Any]] = []
        by_coid: dict[str, Any] = {}
        for a in actions:
            coid = a.client_order_id or ""
            ticker = a.ticker or ""
            exch = self._exch(ticker)
            if exch is None:
                events += self._reject_place(coid, ticker, now, {"reason": "no_exchange_index"})
                continue
            self._pending_place_price = a.price
            violation = await self._pre_place_invariant_async(coid, now, place_price=a.price)
            if violation is not None:
                events += violation
                continue
            entries.append(self._rest_body(a, coid, exch))
            by_coid[coid] = a
            self.journal.append("place_rest", {"client_order_id": coid, "ticker": ticker,
                                               "n": a.price, "batch": True}, self.clock())
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
                events += self._reject_place(coid, a.ticker or "", now,
                                             {"status": resp.status_code, "batch": True})
                continue
            oid = p.order_id
            self.rest_book[coid] = RestRecord(
                client_order_id=coid, order_id=oid,
                price=a.price if a.price is not None else Decimal(0),
                count=int(a.count), ticker=a.ticker or "", bucket_Sd=self._bucket_sd(a.ticker or ""),
                placed_ts=now, status="live", exchange_index=self._exch(a.ticker or ""),
                expiration_epoch=self._expiration_epoch(a),
            )
            self._by_order_id[oid] = coid
            self.rests_placed += 1
            self._consecutive_rejects = 0
            self._bump("rest_acked")
            events.append(OrderAck(client_order_id=coid, order_id=oid, server_ts=now))
            if p.fill_count and p.fill_count > 0:
                events.append(Fill(order_id=oid, client_order_id=coid, count=p.fill_count,
                                   price=a.price if a.price is not None else Decimal(0),
                                   side="no", server_ts=now))
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
