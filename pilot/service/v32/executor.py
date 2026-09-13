"""executor.py — the ARMED maker executor for V3.2 (the only V3.2 code path that sends orders).

House law: every network edge is injected (the ``ProxyWriter``); nothing here reads .env / *.pem or
dials the live proxy from a test. It fails closed by construction — an unrouted leg (no
``exchange_index``) is refused, a POST is never retried (idempotency is the ``client_order_id``), and a
cancel is confirmed by an order-status GET (the status is the truth in the cancel/fill race), never
assumed. The pure decision core (``decide_v32``) decides WHAT to do; this object only turns each
emitted action into the exact wire call and feeds the exchange's reply back as a Phase-1 event.

``LiveExecutor.on_action(action, state, now) -> list[event]`` is the SAME interface the dry
``FrozenExecutor`` implements, so the driver is executor-agnostic. The executor is selected by
``effective_mode`` in exactly ONE place (``run_v32.build_executor``); a ``WOULD_*`` twin reaching a
LiveExecutor (a mis-wire) raises, and a real kind reaching a FrozenExecutor raises (P3-1) — neither can
silently book phantom or live money.

Actions:
  * PLACE_REST  — single create: bucket-NO buy at n, ``post_only`` true, ``good_till_canceled`` with an
    ``expiration_time`` at the quote end (a crashed process leaves nothing resting past the window),
    routed by ``exchange_index``. A successful create -> OrderAck (the rest is now live); an immediate
    fill on a post_only order is impossible but ``fill_count > 0`` is routed as a Fill anyway. A
    rejection (post_only cross, cap, budget, transport) -> journal ``rest_rejected`` + OrderCancelled
    (filled 0) so the core re-solves; three CONSECUTIVE rejections latch a stand-down for the hour.
  * CANCEL_REST — DELETE the order; the cancel response's ``reduced_by`` (contracts pulled off the
    book) makes the race decidable (filled = placed - reduced_by), cross-checked with an order-status
    GET (take the MAX so a fill is never under-counted). ``filled_count_before_cancel`` is NEVER
    assumed 0. The RestRecord is RETAINED (F-1) so a late fill on its coid is attributable.
  * TAKE_WINGS / RETRY_WING — batch create (2 legs) or single (1-leg retry), IOC taker at the leg's
    limit (ask + margin), count = the filled rest count, routed per leg. The batch response is
    synchronous fill truth: a filled leg -> Fill(count>0, exec price); an unfilled leg -> Fill(count 0)
    so the core emits a RETRY_WING on the next tick.

The private ``fill`` channel AND a 1 s order-status poll both surface fills; both funnel through the
driver, which de-duplicates by ``trade_id`` / ``order_id`` so a fill seen twice is booked once.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from service.orders.envelope import (
    BATCH_CREATE_PATH,
    SINGLE_CREATE_PATH,
    build_batch,
    normalize_fill_to_side,
    parse_batch_response,
    parse_single_response,
)
from service.orders.translate import to_v2_order
from service.proxy_writer import ProxyWriter
from service.v32.actions import ActionKind
from service.v32.core import BUY_NO
from service.v32.events import Fill, OrderAck, OrderCancelled

logger = logging.getLogger(__name__)

_ONE = Decimal(1)
_PRICE_Q = Decimal("0.0001")

# --- wire constants (see module docstring / build report) ---
REST_TIF = "good_till_canceled"          # the resting maker bid is GTC (auto-expires at quote end)
REST_STP = "taker_at_cross"              # self-trade prevention (as the pilot uses)
WING_TIF = "immediate_or_cancel"         # taker completion legs are IOC (build_entry default)
DEFAULT_STP = "taker_at_cross"

# V2 cancel path (prod-proven in degeneracy_v2/kalshi/rest.py:cancel_order): the LEGACY
# /portfolio/orders/{id} began returning HTTP 410 deprecated_v1_order_endpoint on 2026-07-12, so
# cancels MUST use the /events/orders/{id} namespace (same host as create). The proxy routes any
# DELETE under /portfolio/events/orders to the orders host, uncapped/unbudgeted.
CANCEL_PATH_TMPL = "/portfolio/events/orders/{order_id}"
# Order-status + open-orders READS are GETs (routed to the market-data host by the proxy).
ORDER_STATUS_PATH_TMPL = "/portfolio/orders/{order_id}"   # VERIFIED docs.kalshi.com/api-reference/orders/get-order
OPEN_ORDERS_PATH = "/portfolio/orders"                    # ?status=resting (VERIFIED get-orders; ticker/order_id/client_order_id)

CANCEL_CONFIRM_POLLS = 3
CANCEL_CONFIRM_INTERVAL_S = 0.2
CONSECUTIVE_REJECT_STANDDOWN = 3

_KXBTC_PREFIX = "KXBTC"  # covers both range (KXBTC-) and strikes (KXBTCD-) via startswith
_V32_COID_PREFIX = "v32-"  # our client_order_id prefix (core._mint_coid); scopes the startup sweep


def _cents(price: Decimal) -> int:
    """Whole-cent integer of a dollar price (translate's direction mapping keys on cents)."""
    return int((Decimal(price) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _dec_or_none(v: Any) -> Decimal | None:
    """Parse a fixed-point/dollar value to Decimal, or None (unparseable/absent)."""
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def _wire_price_no(n: Decimal) -> str:
    """4-dp YES-space wire price for a NO order at ``n`` dollars: 1 - n (a NO bid at n == YES ask at
    1-n). Matches ``envelope.wire_price('no', n)`` but kept local so the rest body is self-contained."""
    return str((_ONE - Decimal(n)).quantize(_PRICE_Q))


@dataclass
class RestRecord:
    """One resting bucket-NO order the executor has placed this window. RETAINED after cancel/replace
    so a late fill on its coid is attributable (Phase-1 review F-1). ``status`` in
    {live, cancelled, filled, rejected}."""

    client_order_id: str
    order_id: str | None
    price: Decimal
    count: int
    ticker: str
    bucket_Sd: int | None
    placed_ts: float
    status: str


@dataclass(frozen=True)
class OrderStatus:
    """Parsed order-status GET. ``filled_count`` is the contracts filled so far (the truth for the
    cancel race); ``resting`` True while the order can still fill. Fail-closed: an unreadable payload
    yields ``available=False`` and ``filled_count=0`` (the caller journals and treats as no-fill)."""

    order_id: str | None
    status: str | None
    filled_count: int
    remaining_count: int | None
    available: bool


def parse_order_status(body: dict[str, Any], order_id: str) -> OrderStatus:
    """Parse a GetOrder body ({"order": {...}} or a bare order) into an OrderStatus.

    VERIFIED (docs.kalshi.com/api-reference/orders/get-order, 2026-09-13): the order object carries
    ``fill_count_fp`` / ``remaining_count_fp`` / ``initial_count_fp`` (fixed-point strings) and
    ``status`` — NOT ``fill_count`` / ``remaining_count`` / ``place_count`` / ``maker_fill_count`` /
    ``taker_fill_count``. ``filled_count`` prefers ``fill_count_fp``; else ``initial - remaining``.
    The legacy names are kept only as a defensive fallback (older venue builds / test doubles). The
    earlier build read ONLY the legacy names, so a live status always parsed filled=0 — silently
    defeating both the cancel-race truth and the belt-and-braces poll."""
    if not isinstance(body, dict):
        return OrderStatus(order_id, None, 0, None, available=False)
    order = body.get("order") if isinstance(body.get("order"), dict) else body
    if not isinstance(order, dict):
        return OrderStatus(order_id, None, 0, None, available=False)

    def _i(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(Decimal(str(v)))
        except Exception:  # noqa: BLE001
            return None

    status = order.get("status")
    # documented (fixed-point) fields first, legacy names as a fallback.
    remaining = _i(order.get("remaining_count_fp"))
    if remaining is None:
        remaining = _i(order.get("remaining_count"))
    initial = _i(order.get("initial_count_fp"))
    if initial is None:
        initial = _i(order.get("place_count"))
    filled = _i(order.get("fill_count_fp"))
    if filled is None:
        filled = _i(order.get("fill_count"))
    if filled is None:
        mk = _i(order.get("maker_fill_count")) or 0
        tk = _i(order.get("taker_fill_count")) or 0
        if mk or tk:
            filled = mk + tk
    if filled is None and initial is not None and remaining is not None:
        filled = max(0, initial - remaining)
    if filled is None:
        filled = 0
    return OrderStatus(order_id=order.get("order_id") or order_id, status=status,
                       filled_count=int(filled), remaining_count=remaining, available=True)


class LiveExecutor:
    """The armed maker executor. See module docstring for the law."""

    def __init__(
        self,
        writer: ProxyWriter,
        bucket_map: dict[str, tuple[float, float]],
        exchange_index_by_ticker: dict[str, int | None],
        journal: Any,
        close_epoch: int,
        quote_end_s: int,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.writer = writer
        self.bucket_map = bucket_map
        self.exch_by_ticker = dict(exchange_index_by_ticker)
        self.journal = journal
        self.close_epoch = int(close_epoch)
        self.quote_end_s = int(quote_end_s)
        self.clock = clock
        self.sleep = sleep
        # RestBook (F-1): retained across cancel/replace for the whole window.
        self.rest_book: dict[str, RestRecord] = {}
        self._by_order_id: dict[str, str] = {}
        self.wing_coids: set[str] = set()   # taker-leg coids (so a WS echo is de-duped, not "foreign")
        self.counts: dict[str, int] = {}
        self._consecutive_rejects = 0
        self.stand_down_reason: str | None = None
        # money-math capture for the ledger (appended, read by run_v32._finalize).
        self.fills: list[dict[str, Any]] = []          # rest + wing fills (price, fee, ts, path)
        self.booked_rest_oids: set[str] = set()        # de-dup rest-fill money-math across cancel/ws/poll
        self.rests_placed = 0
        self.rests_rejected = 0
        self.wing_batches = 0
        self.exec_price_mismatches: list[dict[str, Any]] = []
        self.alarms: list[dict[str, Any]] = []

    # ---- RestBook API (shared with FrozenExecutor; the driver is executor-agnostic) ----
    def attribute(self, coid: str | None = None, order_id: str | None = None) -> RestRecord | None:
        if coid and coid in self.rest_book:
            return self.rest_book[coid]
        if order_id and order_id in self._by_order_id:
            return self.rest_book[self._by_order_id[order_id]]
        return None

    def mark_filled(self, coid: str) -> None:
        rec = self.rest_book.get(coid)
        if rec is not None:
            rec.status = "filled"

    def _bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def _exch(self, ticker: str) -> int | None:
        return self.exch_by_ticker.get(ticker)

    def _bucket_sd(self, ticker: str) -> int | None:
        fc = self.bucket_map.get(ticker)
        return int(round(float(fc[0]))) if fc is not None else None

    # =====================================================================
    # on_action
    # =====================================================================
    def on_action(self, action, state, now: float) -> list[Any]:
        k = action.kind
        if k == ActionKind.PLACE_REST:
            return self._place_rest(action, now)
        if k == ActionKind.CANCEL_REST:
            return self._cancel_rest(action, now)
        if k in (ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
            return self._take_wings(state, now)
        if k in (ActionKind.WOULD_PLACE_REST, ActionKind.WOULD_CANCEL_REST,
                 ActionKind.WOULD_TAKE_WINGS):
            # A WOULD_* twin means shakedown/dry state reached the ARMED executor — a mis-wire. Fail
            # loud rather than send nothing silently or, worse, book a phantom.
            raise AssertionError(
                f"LiveExecutor received a shakedown twin {k}; the core must be armed (shakedown=False) "
                f"when a LiveExecutor is selected (P3-1)"
            )
        return []  # STAND_DOWN and any order-free kind

    # ---- PLACE_REST ----
    def _place_rest(self, action, now: float) -> list[Any]:
        coid = action.client_order_id or ""
        ticker = action.ticker or ""
        exch = self._exch(ticker)
        if exch is None:
            # never send an unrouted order (2026-08-27 market_not_found): treat as a rejection.
            return self._reject_place(coid, ticker, now, {"reason": "no_exchange_index"})
        body = self._rest_body(action, coid, exch)
        self.journal.append("place_rest", {**{k: v for k, v in body.items()
                                              if k != "self_trade_prevention_type"},
                                          "n": action.price, "bucket_Sd": self._bucket_sd(ticker)},
                            self.clock())
        resp = self.writer.rest_post(SINGLE_CREATE_PATH, body)
        self.rests_placed += 1
        self._bump("rest_post")
        if not resp.ok:
            # A POST is NEVER retried (a lost response on a server-side success would duplicate). A
            # transport timeout (status None) or a 5xx leaves the outcome UNKNOWN: the order may be
            # live on the book. Treating that as a clean rejection and re-placing would DOUBLE-ENTER,
            # and a fill on the phantom would arrive on a coid we never recorded (dropped as foreign =
            # an unhedged naked bucket-NO). So on an unknown outcome we (a) RECORD the coid so a later
            # fill is attributable and gets hedged via the late-fill path, and (b) latch a stand-down
            # so no second rest is placed this hour (the per-order expiration_time bounds the leak).
            unknown = resp.status_code is None or resp.status_code >= 500
            return self._reject_place(coid, ticker, now, {"status": resp.status_code,
                                                          "body": resp.body, "error": resp.error},
                                      unknown=unknown, n=action.price)
        parsed = parse_single_response(resp.body, side=BUY_NO)
        if parsed.error or parsed.order_id is None:
            return self._reject_place(coid, ticker, now,
                                      {"status": resp.status_code, "parsed_error": parsed.error,
                                       "order_id": parsed.order_id})
        # success -> the rest is live. Record + ack.
        oid = parsed.order_id
        self.rest_book[coid] = RestRecord(
            client_order_id=coid, order_id=oid,
            price=action.price if action.price is not None else Decimal(0),
            count=int(action.count), ticker=ticker, bucket_Sd=self._bucket_sd(ticker),
            placed_ts=now, status="live",
        )
        self._by_order_id[oid] = coid
        self._consecutive_rejects = 0
        self._bump("rest_acked")
        events: list[Any] = [OrderAck(client_order_id=coid, order_id=oid, server_ts=now)]
        if parsed.fill_count > 0:
            # impossible for a true post_only maker, but book it if the venue reports it.
            events.append(Fill(order_id=oid, client_order_id=coid, count=parsed.fill_count,
                               price=action.price if action.price is not None else Decimal(0),
                               side="no", server_ts=now))
        return events

    def _reject_place(self, coid: str, ticker: str, now: float, detail: dict, *,
                      unknown: bool = False, n: Decimal | None = None) -> list[Any]:
        self.rests_rejected += 1
        self._consecutive_rejects += 1
        self._bump("rest_rejected")
        self.journal.append("rest_rejected", {"client_order_id": coid, "ticker": ticker,
                                              "consecutive": self._consecutive_rejects,
                                              "unknown_outcome": bool(unknown), **detail},
                            self.clock())
        if unknown:
            # The order MAY be live (timeout / 5xx). Record the coid (order_id unknown) so a fill on it
            # is attributable and hedged via the late-fill path, not dropped as foreign; and latch a
            # stand-down so no second rest stacks on top of a possibly-live one.
            self.rest_book[coid] = RestRecord(
                client_order_id=coid, order_id=None,
                price=n if n is not None else Decimal(0), count=1, ticker=ticker,
                bucket_Sd=self._bucket_sd(ticker), placed_ts=now, status="unknown",
            )
            if self.stand_down_reason is None:
                self.stand_down_reason = "post_unknown_outcome"
                self._record_alarm("post_unknown_outcome",
                                   {"client_order_id": coid, "ticker": ticker, **detail})
        else:
            rec = self.rest_book.get(coid)
            if rec is not None:
                rec.status = "rejected"
            if (self._consecutive_rejects >= CONSECUTIVE_REJECT_STANDDOWN
                    and self.stand_down_reason is None):
                self.stand_down_reason = "rest_rejected_x%d" % self._consecutive_rejects
                self._record_alarm("rest_rejected_standdown",
                                   {"consecutive": self._consecutive_rejects})
        # Feed the core an OrderCancelled(filled 0) so it clears the pending slot and re-solves. The
        # placed order never became live (order_id None) -> match the pending's None order_id.
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]

    def _rest_body(self, action, coid: str, exch: int) -> dict[str, Any]:
        n = action.price if action.price is not None else Decimal(0)
        legacy = {
            "ticker": action.ticker,
            "side": BUY_NO,            # bucket-NO
            "action": "buy",
            "no_price": _cents(n),     # NO at n cents; translate -> ask @ (1 - n) YES-space
            "count": int(action.count),
            "time_in_force": REST_TIF,
            "post_only": True,
            "self_trade_prevention_type": REST_STP,
            "client_order_id": coid,
            "exchange_index": int(exch),
        }
        body = to_v2_order(legacy)
        body["price"] = _wire_price_no(n)  # full 4-dp precision (translate rounds to whole cents)
        # expiration at the quote end so a crashed process leaves nothing resting past the window.
        # VERIFIED (docs.kalshi.com/api-reference/orders/create-order-v2, 2026-09-13): the field is
        # `expiration_time` (int Unix SECONDS), valid only with time_in_force=good_till_canceled. The
        # earlier build set `expiration_ts` (a non-existent field the venue would ignore), which would
        # have defeated crash-safety by leaving the rest with no auto-expiry.
        exp = action.expiration_epoch
        if exp is None:
            exp = self.close_epoch - self.quote_end_s
        body["expiration_time"] = int(exp)  # docs.kalshi.com CreateOrderV2Request (int Unix seconds)
        return body

    # ---- CANCEL_REST ----
    def _cancel_rest(self, action, now: float) -> list[Any]:
        coid = action.client_order_id
        oid = action.order_id
        if oid is None and coid is not None:
            rec = self.rest_book.get(coid)
            oid = rec.order_id if rec is not None else None
        if oid is None:
            # nothing to cancel on the exchange (a still-pending create with no order_id yet). Clear
            # the core's slot with a filled-0 confirm.
            self._bump("cancel_noop")
            return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]
        self.journal.append("cancel_rest", {"order_id": oid, "client_order_id": coid}, self.clock())
        wr = self.writer.rest_delete(CANCEL_PATH_TMPL.format(order_id=oid))
        self._bump("cancel_delete")
        rec = self.attribute(coid=coid, order_id=oid)
        # The DELETE response is AUTHORITATIVE for the race: docs.kalshi.com cancel-order-v2 returns
        # ``reduced_by`` = "the remaining count at time of cancellation" (contracts pulled off the book),
        # so filled_before_cancel = placed_count - reduced_by. Cross-check with the order-status GET and
        # take the MAX so a fill is NEVER under-counted (an under-count would leave a filled bucket-NO
        # unhedged). A 404 (order already terminal) leaves reduced_by absent -> rely on the status GET.
        filled_delete: int | None = None
        rb = _dec_or_none(wr.body.get("reduced_by")) if isinstance(wr.body, dict) else None
        if rb is not None and rec is not None:
            filled_delete = max(0, int(rec.count) - int(rb))
        filled_status = self._confirm_cancel_filled(oid, now)
        filled = max(filled_delete or 0, filled_status)
        if rec is not None:
            rec.status = "filled" if filled > 0 else "cancelled"  # RETAINED for late-fill attr (F-1)
            if filled > 0 and rec.order_id is not None and rec.order_id not in self.booked_rest_oids:
                # book the race fill into money-math (maker fee 0). De-duped by order_id so a later WS
                # echo of the same fill (driver._record_fill) does NOT double-count the rest leg.
                self.booked_rest_oids.add(rec.order_id)
                self.fills.append({"leg": "rest", "side": "no", "ticker": rec.ticker,
                                   "price": rec.price, "exec_price": None, "fee": Decimal(0),
                                   "count": int(filled), "bucket_Sd": rec.bucket_Sd,
                                   "path": "cancel_race", "client_order_id": rec.client_order_id})
        self.journal.append("cancel_confirmed",
                            {"order_id": oid, "delete_status": wr.status_code, "reduced_by": str(rb),
                             "filled_before_cancel": filled}, self.clock())
        return [OrderCancelled(order_id=oid, server_ts=now,
                               filled_count_before_cancel=Decimal(filled))]

    def _confirm_cancel_filled(self, order_id: str, now: float) -> int:
        """Poll order-status up to CANCEL_CONFIRM_POLLS times; return filled_count_before_cancel.
        The status is authoritative for the race; an unreadable status yields 0 (fail toward re-solve,
        journaled by the caller)."""
        filled = 0
        for i in range(CANCEL_CONFIRM_POLLS):
            st = self.order_status(order_id)
            if st.available:
                filled = st.filled_count
                # once the order is off the book (not resting) the count is final.
                if st.status not in ("resting", None) or st.remaining_count == 0:
                    return filled
            if i < CANCEL_CONFIRM_POLLS - 1:
                self.sleep(CANCEL_CONFIRM_INTERVAL_S)
        return filled

    def order_status(self, order_id: str) -> OrderStatus:
        """GET the order status (bounded-retry GET). Fail-closed to unavailable on any error."""
        try:
            body = self.writer.rest_get(ORDER_STATUS_PATH_TMPL.format(order_id=order_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("[V32-EXEC] order_status GET failed for %s: %s", order_id, e)
            return OrderStatus(order_id, None, 0, None, available=False)
        return parse_order_status(body if isinstance(body, dict) else {}, order_id)

    # ---- TAKE_WINGS / RETRY_WING ----
    def _take_wings(self, state, now: float) -> list[Any]:
        pending = [lg for lg in state.wing_legs if lg.status == "pending"]
        if not pending:
            return []
        entries: list[dict[str, Any]] = []
        legs_by_coid: dict[str, Any] = {}
        for lg in pending:
            exch = self._exch(lg.ticker)
            if exch is None:
                # unrouted taker leg: report an unfilled (count 0) fill so the core retries next tick.
                self._record_alarm("wing_no_exchange_index", {"ticker": lg.ticker, "side": lg.side})
                continue
            entries.append(self._wing_entry(lg, exch))
            legs_by_coid[lg.client_order_id] = lg
            self.wing_coids.add(lg.client_order_id)
        events: list[Any] = []
        # any pending leg we could not route -> emit an unfilled Fill so the core marks it unfilled.
        for lg in pending:
            if lg.client_order_id not in legs_by_coid:
                events.append(Fill(order_id=None, client_order_id=lg.client_order_id,
                                   count=Decimal(0), price=lg.limit, side=lg.side, server_ts=now))
        if not entries:
            return events
        self.wing_batches += 1
        self._bump("wing_batch")
        if len(entries) == 1:
            self.journal.append("take_wings", {"legs": entries}, self.clock())
            resp = self.writer.rest_post(SINGLE_CREATE_PATH, entries[0])
            parsed = [parse_single_response(resp.body, side=legs_by_coid[
                entries[0]["client_order_id"]].side)] if resp.ok else []
        else:
            body = build_batch(entries)
            self.journal.append("take_wings", {"legs": entries}, self.clock())
            resp = self.writer.rest_post(BATCH_CREATE_PATH, body)
            parsed = parse_batch_response(resp.body) if resp.ok else []
        events += self._wing_events(parsed, legs_by_coid, now, ok=resp.ok, status=resp.status_code)
        return events

    def _wing_entry(self, leg, exch: int) -> dict[str, Any]:
        legacy = {
            "ticker": leg.ticker,
            "side": leg.side,             # "yes" (low @ Sd) or "no" (high @ Su)
            "action": "buy",
            "count": int(leg.count),
            "time_in_force": WING_TIF,    # IOC taker
            "self_trade_prevention_type": DEFAULT_STP,
            "client_order_id": leg.client_order_id,
            "exchange_index": int(exch),
        }
        if leg.side == "yes":
            legacy["yes_price"] = _cents(leg.limit)
        else:
            legacy["no_price"] = _cents(leg.limit)
        body = to_v2_order(legacy)
        # full-precision 4-dp price in YES-space (yes -> limit; no -> 1 - limit)
        p = Decimal(leg.limit) if leg.side == "yes" else (_ONE - Decimal(leg.limit))
        body["price"] = str(p.quantize(_PRICE_Q))
        return body

    def _wing_events(self, parsed, legs_by_coid, now: float, *, ok: bool,
                     status: int | None) -> list[Any]:
        # units choke point: normalize each slot's reported price into THIS leg's side-space (Kalshi
        # reports a NO order's price in YES-space) so the booked exec price is comparable to the limit.
        by_coid = {}
        for r in parsed:
            if r.client_order_id and r.client_order_id in legs_by_coid:
                by_coid[r.client_order_id] = normalize_fill_to_side(
                    r, legs_by_coid[r.client_order_id].side)
        events: list[Any] = []
        for coid, lg in legs_by_coid.items():
            r = by_coid.get(coid)
            if r is None or not ok or r.error or r.fill_count <= 0:
                # no fill (IOC did not complete, or the batch slot errored) -> count 0 so the core
                # emits a RETRY_WING on the next tick.
                if r is None or not ok:
                    self.journal.append("wing_no_fill", {"client_order_id": coid, "ticker": lg.ticker,
                                                        "status": status,
                                                        "error": (r.error if r else "no_slot")},
                                        self.clock())
                events.append(Fill(order_id=(r.order_id if r else None), client_order_id=coid,
                                   count=Decimal(0), price=lg.limit, side=lg.side, server_ts=now))
                continue
            price = r.average_fill_price if r.average_fill_price is not None else lg.limit
            self.fills.append({"leg": "wing", "side": lg.side, "ticker": lg.ticker,
                               "price": price, "fee": r.average_fee_paid,
                               "count": int(r.fill_count), "ts": now, "path": "batch",
                               "client_order_id": coid})
            self._bump("wing_fill")
            events.append(Fill(order_id=r.order_id, client_order_id=coid, count=r.fill_count,
                               price=price, side=lg.side, server_ts=now))
        return events

    # ---- alarms ----
    def _record_alarm(self, kind: str, detail: dict) -> None:
        self.alarms.append({"alarm": kind, **detail})
        self._bump("alarm")
        try:
            self.journal.append("alarm", {"alarm": kind, **detail}, self.clock())
        except Exception:  # noqa: BLE001 — an alarm must never crash the send path
            pass


# ===========================================================================
# Startup safety: cancel any of OUR open orders in KXBTC* before arming (crash recovery)
# ===========================================================================
def cancel_stale_open_orders(writer: ProxyWriter, journal: Any,
                             clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """On an armed ``prepare()``: GET our resting orders and DELETE any in KXBTC* so a prior crashed
    process leaves nothing live. Fail-closed and never raises out of startup: an unreadable list just
    means we cancel nothing (and the per-order ``expiration_time`` still bounds a leaked rest)."""
    result = {"found": 0, "cancelled": 0, "errors": 0}
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
        # Cross-pilot safety (the account is shared): NEVER cancel an order that carries a foreign
        # client_order_id (e.g. a re-armed v1.1 box order). A missing coid is still cancelled (a
        # crashed-process leftover with no attribution is ours to clear). Our coids are "v32-...".
        coid = o.get("client_order_id")
        if coid and not str(coid).startswith(_V32_COID_PREFIX):
            journal.append("startup_skip_foreign_order",
                           {"ticker": ticker, "order_id": oid, "client_order_id": coid}, clock())
            continue
        result["found"] += 1
        wr = writer.rest_delete(CANCEL_PATH_TMPL.format(order_id=oid))
        if wr.ok or (wr.status_code == 404):
            result["cancelled"] += 1
        else:
            result["errors"] += 1
        journal.append("startup_cancel", {"ticker": ticker, "order_id": oid,
                                          "status": wr.status_code}, clock())
    journal.append("startup_cancel_sweep", result, clock())
    return result
