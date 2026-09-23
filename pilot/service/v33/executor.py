"""executor.py — the ARMED maker executor for V3.3 (the rolling K-rung ladder).

A FORK of ``service.v32.executor.LiveExecutor`` generalised from ONE resting order to K one-lot rungs.
The V3.2 executor is already per-ORDER (every method keys on ``client_order_id`` / ``order_id`` and a
RETAINED ``RestBook``), so the ladder needs almost none of its per-order bookkeeping changed: place,
amend (the roll — ONE order per call, no batch amend on Kalshi), cancel, the amend->cancel->create
fallback, coalesced wing takes (sized to the batch total), per-order fill dedup (WS / poll / cancel_ctx)
and the venue-truth cancel-race machinery are INHERITED unchanged. This subclass changes only what the
ladder actually needs:

  * ``_V33_COID_PREFIX`` = ``"v33-"`` — the client_order_id prefix the core mints (``v33-<window>-<seq>``)
    so the two rosters are distinguishable on the venue. The startup cancel sweep and the pre-place
    venue-truth read are scoped to it and NEVER touch ``v32-*`` (the concurrently-armed V3.2 roster).
  * The PRE-PLACE venue-truth invariant is K-AWARE: V3.2 refused to place while ANY of ours rested
    (it rests exactly one), but V3.3 rests up to K concurrently. The invariant instead refuses when the
    venue already holds >= K of ours (a stacked ladder — the 21-rest incident generalised) OR when one
    of ours ALREADY rests at the price we are about to place (two on one price). O-2 (reviewer R4):
    reconciliation reads LIVE venue truth, so a fresh order at a previously-FILLED price (a distinct
    order_id, the old one off the book) is NOT a double-book — it simply does not appear in the resting
    list, so it is placed cleanly.
  * OPTIONAL batch create (``batch_create``, default OFF): when the core emits several PLACE_REST in one
    decide tick (a first placement / bucket-change re-placement lays K), the driver MAY hand them to
    ``place_batch`` which chunks them into Kalshi ``/portfolio/orders/batched`` calls of <=
    ``batch_create_max`` (default 8). Default OFF -> single creates (the amend cap is NOT yet applied at
    the proxy, so the cancel->create fallback per order is the default roll path; flipping to amend-first
    or batch create is a params/env value, not code).

House law is inherited whole: every network edge is the injected ``ProxyWriter``, a POST is never
retried (idempotency is the coid), a cancel is confirmed by status-truth, nothing reads a key/.env/PEM,
and a WOULD_* twin reaching this armed executor raises (P3-1). Client order ids ``v33-*`` so the sweep
and the venue read never confuse the two rosters.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from service.orders.envelope import build_batch, parse_batch_response, parse_single_response
from service.proxy_writer import ProxyWriter
from service.v32.actions import ActionKind
from service.v32.core import BUY_NO
from service.v32.events import Fill, OrderAck, OrderCancelled
from service.v32.executor import (  # inherited, UNCHANGED wire law
    INVARIANT_RECHECK_S,
    OPEN_ORDERS_PATH,
    REL_BATCH_CREATE,
    REL_SINGLE_CREATE,
    LiveExecutor,
    RestRecord,
    cancel_path,
)

logger = logging.getLogger(__name__)

_KXBTC_PREFIX = "KXBTC"          # covers range (KXBTC-) and strikes (KXBTCD-) via startswith
_V33_COID_PREFIX = "v33-"        # OUR roster's coid prefix (core._mint_coid); NEVER v32-*
_V32_COID_PREFIX = "v32-"        # the concurrently-armed V3.2 roster — the sweep must skip it

DEFAULT_BATCH_CREATE_MAX = 8     # Kalshi batch chunk size (Basic-tier token bucket: 8*10 = 80 < 100)


class V33LiveExecutor(LiveExecutor):
    """The armed maker executor for the K-rung ladder. Subclasses ``LiveExecutor`` and overrides only
    the ladder-specific surface (the v33 coid prefix, the K-aware pre-place invariant, optional batch
    create). Every other order path is inherited unchanged."""

    COID_PREFIX = _V33_COID_PREFIX

    def __init__(
        self,
        writer: ProxyWriter,
        bucket_map: dict[str, tuple[float, float]],
        exchange_index_by_ticker: dict[str, int | None],
        journal: Any,
        close_epoch: int,
        quote_end_s: int,
        *,
        k_rungs: int,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        batch_create: bool = False,
        batch_create_max: int = DEFAULT_BATCH_CREATE_MAX,
    ) -> None:
        super().__init__(
            writer, bucket_map, exchange_index_by_ticker, journal, close_epoch, quote_end_s,
            clock=clock, sleep=sleep,
        )
        self.k_rungs = int(k_rungs)
        self.batch_create = bool(batch_create)
        self.batch_create_max = max(1, int(batch_create_max))
        self._pending_place_price: Decimal | None = None
        # ladder-specific venue-truth counters (surfaced on the ledger row alongside the inherited ones)
        self.rest_invariant_overflow = 0     # venue already held >= K of ours at a place
        self.rest_invariant_dup_price = 0    # venue already held one of ours at the place price
        self.batch_creates = 0               # PLACE_REST batches sent via place_batch

    # =====================================================================
    # on_action — intercept PLACE_REST to record the target price for the K-aware invariant
    # =====================================================================
    def on_action(self, action, state, now: float) -> list[Any]:
        if action.kind == ActionKind.PLACE_REST:
            # record the price being placed so the pre-place invariant can check "no two on one price".
            self._pending_place_price = action.price
        return super().on_action(action, state, now)

    # =====================================================================
    # v33 coid-scoped venue read (the ONLY change: prefix + captured NO-space price via our RestBook)
    # =====================================================================
    def _venue_resting_ours(self, exclude_oid: str | None) -> list[dict[str, Any]] | None:
        """GET the venue's resting orders, filtered to OUR roster (``v33-`` coids). Same shape as the
        V3.2 read but scoped to the v33 prefix so it NEVER sees a resting ``v32-*`` order (the two
        rosters run against one account). Returns None on an unreadable list (caller proceeds)."""
        try:
            body = self.writer.rest_get(OPEN_ORDERS_PATH, {"status": "resting"})
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33-EXEC] pre-place open-orders GET failed: %s", e)
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
                continue  # NOT ours (v32-* or anything else) -> the sweep/invariant leaves it alone
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

    # =====================================================================
    # K-aware pre-place invariant (O-1 / O-2)
    # =====================================================================
    def _resting_price(self, r: dict[str, Any]) -> Decimal | None:
        """The NO-space resting price of one venue-listed order of ours, looked up in our RETAINED
        RestBook (we placed it, so we know its price). None if we cannot attribute it (a true stray)."""
        rec = self.attribute(order_id=r.get("order_id"), coid=r.get("client_order_id"))
        return rec.price if rec is not None else None

    def _pre_place_invariant(self, coid: str, now: float) -> list[Any] | None:
        """Before every PLACE_REST, confirm the venue does not already hold a FULL ladder of ours or a
        rest at the SAME price we are about to place. Unlike V3.2 (which rests one order, so ANY of ours
        resting was a violation), V3.3 rests up to K concurrently, so:

          * OVERFLOW: the venue already lists >= K of ours -> a place would stack past K (the 21-rest
            incident generalised) -> real violation.
          * DUP PRICE: the venue already holds one of ours at ``self._pending_place_price`` -> two on one
            price -> real violation. A previously-FILLED price is off the book (not listed), so a fresh
            order there is NOT flagged (O-2, reviewer R4).
          * A resting order we cannot attribute to our RestBook is a genuine stray -> conservative
            violation (cancel + alarm + stand down; the inherited stand-down + this refusal block any
            replacement place).

        The read-path-lag phantom filter + one recheck (V3.2's PR #50 machinery) run first, so a
        just-cancelled order still showing in a lagging list is not misread as resting. Returns an event
        list to short-circuit the place, or None to proceed."""
        resting = self._venue_resting_ours(self._last_confirmed_gone_oid)
        if not resting:  # None (unreadable) or [] (clean) -> proceed
            return None
        resting = self._filter_phantoms(resting, now)
        if not resting:
            return None
        # a survivor could be read-path lag on an order not in our recently-cancelled book: recheck once.
        self.rest_invariant_rechecks += 1
        self._bump("rest_invariant_recheck")
        self.sleep(INVARIANT_RECHECK_S)
        reread = self._venue_resting_ours(self._last_confirmed_gone_oid)
        if not reread:
            return None
        resting = self._filter_phantoms(reread, now)
        if not resting:
            return None
        # Decide: attribute each survivor's price; flag overflow, dup-price, or an unattributable stray.
        place_price = self._pending_place_price
        resting_prices: list[Decimal] = []
        strays: list[dict[str, Any]] = []
        for r in resting:
            p = self._resting_price(r)
            if p is None:
                strays.append(r)
            else:
                resting_prices.append(p)
        overflow = len(resting) >= self.k_rungs
        dup = place_price is not None and place_price in resting_prices
        if not overflow and not dup and not strays:
            return None  # a partial ladder of ours, none at the place price -> healthy, proceed
        detail = {
            "count_resting": len(resting), "k": self.k_rungs, "coid_attempted": coid,
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
        # Feed a filled-0 confirm so the core clears the pending slot; the stand-down blocks any replace.
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]

    # =====================================================================
    # OPTIONAL batch create (default OFF) — chunked /portfolio/orders/batched
    # =====================================================================
    def place_batch(self, actions: list, now: float) -> list[Any]:
        """Place several PLACE_REST rungs in chunks of <= ``batch_create_max`` via the batch-create
        endpoint (Basic-tier token bucket: a batch of N creates costs 10*N and must fit the 100/s bucket,
        so 8*10 = 80 is safe; K=11 as one batch would be 110 and REJECTED). Off by default; the driver
        only calls this when ``batch_create`` is set. Each accepted create -> OrderAck (+ Fill if the
        venue somehow reports one). A rejected create routes the SAME single-order rejection path (so the
        3-consecutive stand-down still latches). Every order still goes through the K-aware pre-place
        invariant one at a time BEFORE the batch (a batch cannot double-book a price its own members set).
        """
        events: list[Any] = []
        chunk: list = []
        for a in actions:
            chunk.append(a)
            if len(chunk) >= self.batch_create_max:
                events += self._place_one_chunk(chunk, now)
                chunk = []
        if chunk:
            events += self._place_one_chunk(chunk, now)
        return events

    def _place_one_chunk(self, actions: list, now: float) -> list[Any]:
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
            # K-aware pre-place check per order (belt): a stacked-ladder / dup-price at batch time.
            self._pending_place_price = a.price
            violation = self._pre_place_invariant(coid, now)
            if violation is not None:
                events += violation
                continue
            entries.append(self._rest_body(a, coid, exch))
            by_coid[coid] = a
            self.journal.append("place_rest", {"client_order_id": coid, "ticker": ticker,
                                               "n": a.price, "batch": True}, self.clock())
        if not entries:
            return events
        self.batch_creates += 1
        self._bump("rest_batch_post")
        if len(entries) == 1:
            resp = self.writer.rest_post(REL_SINGLE_CREATE, entries[0])
            parsed = [parse_single_response(resp.body, side=BUY_NO)] if resp.ok else []
        else:
            resp = self.writer.rest_post(REL_BATCH_CREATE, build_batch(entries))
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


# ===========================================================================
# Startup safety: cancel any of OUR (v33-*) open orders in KXBTC* before arming
# ===========================================================================
def cancel_stale_open_orders(writer: ProxyWriter, journal: Any,
                             clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """On an armed prepare(): GET our resting orders and DELETE any KXBTC* order whose coid is ``v33-*``
    (a prior crashed V3.3 process). It NEVER cancels a ``v32-*`` order (the concurrently-armed V3.2
    roster) nor a missing-coid / foreign order — the two rosters share one account, so V3.3 clears ONLY
    its own rests (V3.2 has its own sweep for v32-*). Fail-closed / never raises out of startup."""
    result = {"found": 0, "cancelled": 0, "errors": 0, "skipped_foreign": 0}
    try:
        body = writer.rest_get(OPEN_ORDERS_PATH, {"status": "resting"})
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
        # ONLY our v33 rests. A v32-* order (or anything else, incl. a missing coid) is left untouched.
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
        wr = writer.rest_delete(cancel_path(oid, exch))
        if wr.ok or (wr.status_code == 404 and exch is not None):
            result["cancelled"] += 1
        else:
            result["errors"] += 1
        journal.append("startup_cancel", {"ticker": ticker, "order_id": oid,
                                          "exchange_index": exch, "status": wr.status_code}, clock())
    journal.append("startup_cancel_sweep", result, clock())
    return result
