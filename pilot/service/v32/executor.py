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
    ``expiration_time`` set EXPIRATION_GRACE_S AFTER the quote end (a crash backstop; the quote-end
    cancel is the primary path, un-raced by the grace — 2026-09-15 fix), routed by ``exchange_index``.
    A successful create -> OrderAck (the rest is now live); an immediate
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
from service.proxy_auth import REST_PREFIX
from service.proxy_writer import ProxyWriter
from service.v32.actions import ActionKind
from service.v32.core import BUY_NO
from service.v32.events import Fill, OrderAck, OrderCancelled

logger = logging.getLogger(__name__)

_ONE = Decimal(1)
_PRICE_Q = Decimal("0.0001")

# --- create paths, RELATIVE to REST_PREFIX (2026-09-14 doubled-prefix fix) ---
# ``ProxyWriter.rest_post`` composes {base}/trade-api/v2{path}; the envelope's
# SINGLE_CREATE_PATH / BATCH_CREATE_PATH are FULL /trade-api/v2/... paths, so passing
# them to rest_post doubled the prefix -> venue 404 -> stand-down (first armed window,
# 2026-09-14 19:44:59Z). We pass the RELATIVE tail explicitly and pin it to the envelope
# constants below, so a future envelope change cannot silently re-diverge.
REL_SINGLE_CREATE = "/portfolio/events/orders"
REL_BATCH_CREATE = "/portfolio/events/orders/batched"
assert REL_SINGLE_CREATE == SINGLE_CREATE_PATH[len(REST_PREFIX):], (
    f"REL_SINGLE_CREATE {REL_SINGLE_CREATE!r} != SINGLE_CREATE_PATH minus prefix "
    f"{SINGLE_CREATE_PATH[len(REST_PREFIX):]!r}")
assert REL_BATCH_CREATE == BATCH_CREATE_PATH[len(REST_PREFIX):], (
    f"REL_BATCH_CREATE {REL_BATCH_CREATE!r} != BATCH_CREATE_PATH minus prefix "
    f"{BATCH_CREATE_PATH[len(REST_PREFIX):]!r}")

# --- wire constants (see module docstring / build report) ---
REST_TIF = "good_till_canceled"          # the resting maker bid is GTC (auto-expires EXPIRATION_GRACE_S past quote end)
REST_STP = "taker_at_cross"              # self-trade prevention (as the pilot uses)
WING_TIF = "immediate_or_cancel"         # taker completion legs are IOC (build_entry default)
DEFAULT_STP = "taker_at_cross"

# V2 cancel path (prod-proven in degeneracy_v2/kalshi/rest.py:cancel_order): the LEGACY
# /portfolio/orders/{id} began returning HTTP 410 deprecated_v1_order_endpoint on 2026-07-12, so
# cancels MUST use the /events/orders/{id} namespace (same host as create). The proxy routes any
# DELETE under /portfolio/events/orders to the orders host, uncapped/unbudgeted.
#
# SHARD FIX (2026-09-14 21:44Z incident): crypto lives on ``exchange_index: 2`` (Kalshi exchange
# sharding, in force since 2026-08-24). A cancel WITHOUT the shard query param returns
# HTTP 404 {"error":{"code":"not_found"}} even though the order is live and resting; WITH
# ``?exchange_index=2`` it returns 200 {"order_id":..,"reduced_by":..}. The docs page for
# cancel-order-v2 does NOT mention exchange_index (why the review missed it), so the path template
# carries it explicitly and every cancel routes the order's own shard. A DELETE 404 is NEVER treated
# as "already gone" (that misread stacked 21 live rests in the first armed window) — see _cancel_rest.
CANCEL_PATH_TMPL = "/portfolio/events/orders/{order_id}?exchange_index={exchange_index}"
_CANCEL_PATH_NOSHARD_TMPL = "/portfolio/events/orders/{order_id}"  # fallback only when shard unknown
# Order-status + open-orders READS are GETs (routed to the market-data host by the proxy). The
# order-status GET works WITHOUT the shard param and returns the full order (incl. exchange_index).
ORDER_STATUS_PATH_TMPL = "/portfolio/orders/{order_id}"   # VERIFIED docs.kalshi.com/api-reference/orders/get-order
OPEN_ORDERS_PATH = "/portfolio/orders"                    # ?status=resting (VERIFIED get-orders; ticker/order_id/client_order_id)

# a resolved-terminal order status: the order is off the book, the fill count is final.
_TERMINAL_STATUSES = ("canceled", "cancelled", "executed", "expired")

CANCEL_CONFIRM_POLLS = 3
CANCEL_CONFIRM_INTERVAL_S = 0.2
CANCEL_RETRY_ATTEMPTS = 3           # a DELETE that still-rests is retried this many times (shard-aware)
CONSECUTIVE_REJECT_STANDDOWN = 3

# Backoff between the DELETE-retry / status re-read attempts on a non-2xx cancel (2026-09-15
# quote-end-race fix). The 01:00:00Z incident retried the DELETE 3x at the SAME wall-clock instant
# (no delay), so the venue's eventually-consistent order-status read still said "resting" every time
# and a clean (already-expired) order was misdiagnosed as ``cancel_failed``. Sleeping between the
# re-reads lets the terminal status settle so status-truth resolves the cancel. Injectable (tests
# pass a no-op sleep and assert the exact sequence). One entry per CANCEL_RETRY_ATTEMPTS.
CANCEL_BACKOFF_S = (0.25, 0.75, 2.0)

# The GTC rest's ``expiration_time`` is set EXPIRATION_GRACE_S past the quote end (crash backstop)
# rather than AT the quote end, so the executor's own quote-end DELETE (issued at T-quote_end_s) is
# not racing the venue's auto-expiry for the same instant (2026-09-15 01:00Z race). At quote_end_s=300
# the rest expires at T-4 (close-240), well before the wings' T-1 order cutoff and the close; the
# expiry remains the backstop for a CRASHED process, but the quote-end cancel is the primary path.
EXPIRATION_GRACE_S = 60


def cancel_path(order_id: str, exchange_index: int | None) -> str:
    """The DELETE path for a cancel, carrying the order's shard. Falls back to the no-shard path only
    when the exchange_index is unknown (the venue then 404s a sharded order — handled by the caller as
    still-resting, never as gone)."""
    if exchange_index is None:
        return _CANCEL_PATH_NOSHARD_TMPL.format(order_id=order_id)
    return CANCEL_PATH_TMPL.format(order_id=order_id, exchange_index=int(exchange_index))

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
    exchange_index: int | None = None  # the order's shard; REQUIRED to cancel (2026-09-14 shard fix)
    expiration_epoch: int | None = None  # the venue auto-expiry we sent (2026-09-15 quote-end-race fix);
    # lets the cancel path tell an ``expired_at_quote_end`` terminal status from a plain cancel.


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
        # cancel/venue-truth bookkeeping (2026-09-14 shard fix) — surfaced on the ledger row.
        self.cancels_attempted = 0        # CANCEL_REST actions that reached a DELETE
        self.cancels_confirmed = 0        # cancels resolved terminal (2xx reduced_by or terminal status)
        self.cancel_404s = 0              # DELETEs that came back 404 (shard-missing / already terminal)
        self.cancels_via_status = 0       # 404 DELETE but a terminal status GET confirmed it gone
        self.cancels_expired = 0          # 404 DELETE at/after expiration_time -> expired_at_quote_end
        self.cancel_failed_count = 0      # orders still resting after the shard-aware retries
        self.rest_invariant_violations = 0  # pre-PLACE venue check found one of ours already resting
        self._last_confirmed_gone_oid: str | None = None  # excluded from the pre-PLACE invariant

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
        # Belt over the braces (2026-09-14 shard fix): NEVER place while the VENUE already shows one of
        # our orders resting. Internal state can never again disagree with the venue by more than one
        # order — the misread-cancel incident stacked 21 live rests because internal state said "gone"
        # while the venue said "resting". This check consults venue truth, not our RestBook.
        violation = self._pre_place_invariant(coid, now)
        if violation is not None:
            return violation
        body = self._rest_body(action, coid, exch)
        self.journal.append("place_rest", {**{k: v for k, v in body.items()
                                              if k != "self_trade_prevention_type"},
                                          "n": action.price, "bucket_Sd": self._bucket_sd(ticker)},
                            self.clock())
        resp = self.writer.rest_post(REL_SINGLE_CREATE, body)
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
            placed_ts=now, status="live", exchange_index=exch,
            expiration_epoch=self._expiration_epoch(action),
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
                exchange_index=self._exch(ticker), expiration_epoch=self._expiration_epoch(None),
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
        # expiration EXPIRATION_GRACE_S past the quote end (crash backstop; the quote-end cancel is the
        # primary path) so a crashed process leaves nothing resting past the window while a live process
        # cancels first, un-raced (2026-09-15 01:00Z fix).
        # VERIFIED (docs.kalshi.com/api-reference/orders/create-order-v2, 2026-09-13): the field is
        # `expiration_time` (int Unix SECONDS), valid only with time_in_force=good_till_canceled. The
        # earlier build set `expiration_ts` (a non-existent field the venue would ignore), which would
        # have defeated crash-safety by leaving the rest with no auto-expiry.
        body["expiration_time"] = self._expiration_epoch(action)  # int Unix seconds (CreateOrderV2Request)
        return body

    def _expiration_epoch(self, action) -> int:
        """The GTC rest's ``expiration_time`` (Unix seconds): the quote end PLUS EXPIRATION_GRACE_S, so
        the venue's auto-expiry backstop lands AFTER the executor's own quote-end cancel rather than at
        the same instant (2026-09-15 quote-end-race fix). ``action`` may be None (unknown-outcome path):
        the quote-end reference then falls back to the window's own ``close_epoch - quote_end_s``."""
        exp = getattr(action, "expiration_epoch", None) if action is not None else None
        if exp is None:
            exp = self.close_epoch - self.quote_end_s
        return int(exp) + EXPIRATION_GRACE_S

    # ---- pre-PLACE venue-truth invariant (belt over the braces) ----
    def _venue_resting_ours(self, exclude_oid: str | None) -> list[dict[str, Any]] | None:
        """GET the venue's resting orders, filtered to OUR coid prefix (``v32-``). Returns a list of
        {order_id, client_order_id, exchange_index} (excluding ``exclude_oid``), or None if the venue
        list is unreadable (the caller then proceeds — the working cancel path is the primary guard;
        we never self-DoS the strategy on a transient read failure)."""
        try:
            body = self.writer.rest_get(OPEN_ORDERS_PATH, {"status": "resting"})
        except Exception as e:  # noqa: BLE001
            logger.warning("[V32-EXEC] pre-place open-orders GET failed: %s", e)
            return None
        orders = (body or {}).get("orders") if isinstance(body, dict) else None
        if not isinstance(orders, list):
            return None
        out: list[dict[str, Any]] = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            coid = str(o.get("client_order_id") or "")
            if not coid.startswith(_V32_COID_PREFIX):
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

    def _pre_place_invariant(self, coid: str, now: float) -> list[Any] | None:
        """Before every PLACE_REST, confirm the venue holds NONE of our orders resting (other than the
        one we just confirmed gone this replace cycle). If any remain, our internal state disagreed with
        the venue by more than one order — cancel them shard-aware, journal ``rest_invariant_violation``,
        alarm, and stand down the hour. Returns an event list to short-circuit the place, or None to
        proceed (invariant held / venue unreadable)."""
        resting = self._venue_resting_ours(self._last_confirmed_gone_oid)
        if not resting:  # None (unreadable) or [] (clean) -> proceed to place
            return None
        self.rest_invariant_violations += 1
        self._bump("rest_invariant_violation")
        self.journal.append("rest_invariant_violation",
                            {"count": len(resting), "coid_attempted": coid,
                             "resting": [{"order_id": r["order_id"],
                                          "client_order_id": r["client_order_id"]} for r in resting]},
                            self.clock())
        self._record_alarm("rest_invariant_violation",
                           {"count": len(resting), "coid_attempted": coid})
        # cancel the stragglers, shard-aware (the same fix that makes any cancel land).
        for r in resting:
            self.cancels_attempted += 1
            wr = self.writer.rest_delete(cancel_path(r["order_id"], r["exchange_index"]))
            self._bump("cancel_delete")
            if wr.status_code == 404:
                self.cancel_404s += 1
            if wr.ok:
                self.cancels_confirmed += 1
            self.journal.append("rest_invariant_cancel",
                                {"order_id": r["order_id"], "status": wr.status_code}, self.clock())
        if self.stand_down_reason is None:
            self.stand_down_reason = "rest_invariant_violation"
        # Feed the core a filled-0 confirm so it clears the pending slot; the stand-down (applied this
        # event, before the queued OrderCancelled is decided) guarantees no replacement rest is placed.
        return [OrderCancelled(order_id=None, server_ts=now, filled_count_before_cancel=Decimal(0))]

    # ---- CANCEL_REST ----
    def _cancel_rest(self, action, now: float) -> list[Any]:
        coid = action.client_order_id
        oid = action.order_id
        rec = self.rest_book.get(coid) if coid is not None else None
        if oid is None and rec is not None:
            oid = rec.order_id
        if oid is None:
            # nothing to cancel on the exchange (a still-pending create with no order_id yet). Clear
            # the core's slot with a filled-0 confirm.
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
        wr = self.writer.rest_delete(cancel_path(oid, exch))  # shard-aware (2026-09-14 fix)
        self._bump("cancel_delete")
        if wr.status_code == 404:
            self.cancel_404s += 1
        if wr.ok:
            return self._resolve_cancel_success(wr, rec, oid, now)
        # NON-2xx DELETE (incl 404): a 404 is NOT "already gone". Ask the venue for the truth (the
        # order-status GET works WITHOUT the shard param); resolve from a terminal status, else retry
        # the DELETE shard-aware, else declare cancel_failed + stand down (never free a place).
        return self._cancel_nonok(wr, rec, oid, exch, coid, now)

    def _resolve_cancel_success(self, wr, rec, oid: str, now: float) -> list[Any]:
        """A 2xx DELETE: the response's ``reduced_by`` (remaining pulled off the book) is authoritative
        for the race, cross-checked with the order-status GET (take the MAX so a fill is never
        under-counted). The order is off the book synchronously — record it as confirmed-gone."""
        filled_delete: int | None = None
        rb = _dec_or_none(wr.body.get("reduced_by")) if isinstance(wr.body, dict) else None
        if rb is not None and rec is not None:
            filled_delete = max(0, int(rec.count) - int(rb))
        filled_status = self._confirm_cancel_filled(oid, now)
        filled = max(filled_delete or 0, filled_status)
        self.cancels_confirmed += 1
        self._last_confirmed_gone_oid = oid
        return self._finish_cancel(rec, oid, filled, wr.status_code, rb, now)

    def _cancel_nonok(self, wr, rec, oid: str, exch: int | None, coid, now: float) -> list[Any]:
        """A non-2xx DELETE (incl 404) is NOT proof the order is gone AND is not proof it still rests —
        the venue is the truth. GET the order status (no shard param needed): a TERMINAL status resolves
        the cancel by status-truth (2026-09-15 fix), distinguishing an ``expired_at_quote_end`` (we sent
        that expiry and ``now`` is at/after it) from a plain terminal-status confirm. A still-``resting``
        read means the cancel did NOT land yet — sleep the CANCEL_BACKOFF_S step (the 01:00Z incident
        hammered the DELETE 3x at the SAME instant so the eventually-consistent read never settled),
        retry the DELETE shard-aware up to CANCEL_RETRY_ATTEMPTS times, re-reading after each. Only if
        the venue STILL reports resting after the whole backoff sequence do we journal ``cancel_failed``
        + stand down (and NEVER place another rest while it rests)."""
        # expiration-awareness: the executor sent this order's auto-expiry; if ``now`` is at/after it, a
        # subsequent terminal status is an expiry landing (crash backstop), classified distinctly.
        exp = rec.expiration_epoch if rec is not None else None
        expired = exp is not None and now >= exp
        st = self.order_status(oid)
        resolved = self._resolve_cancel_from_status(st, rec, oid, wr.status_code, now, expired)
        if resolved is not None:
            return resolved
        last_status = wr.status_code
        for i in range(CANCEL_RETRY_ATTEMPTS):
            # backoff BEFORE the retry so the venue's terminal status has time to settle (status-truth).
            self.sleep(CANCEL_BACKOFF_S[min(i, len(CANCEL_BACKOFF_S) - 1)])
            self.cancels_attempted += 1
            rwr = self.writer.rest_delete(cancel_path(oid, exch))
            self._bump("cancel_delete")
            last_status = rwr.status_code
            if rwr.status_code == 404:
                self.cancel_404s += 1
            if rwr.ok:
                return self._resolve_cancel_success(rwr, rec, oid, now)
            st = self.order_status(oid)
            resolved = self._resolve_cancel_from_status(st, rec, oid, rwr.status_code, now, expired)
            if resolved is not None:
                return resolved
        # STILL resting after the backoff-spaced shard-aware retries: the order is live on the venue and
        # we could NOT pull it. Stand down the hour; the pre-PLACE invariant blocks any place too.
        self.cancel_failed_count += 1
        if rec is not None:
            rec.status = "cancel_failed"   # still resting on the venue (NOT "cancelled")
        if self.stand_down_reason is None:
            self.stand_down_reason = "cancel_failed"
        self.journal.append("cancel_failed",
                            {"order_id": oid, "client_order_id": coid, "exchange_index": exch,
                             "delete_status": last_status, "last_status": st.status}, self.clock())
        self._record_alarm("cancel_failed", {"order_id": oid, "exchange_index": exch,
                                             "delete_status": last_status})
        # Return a filled-0 confirm so the core's slot resolves; stand-down + the pre-PLACE invariant
        # guarantee no replacement rest is placed while this one rests on the venue.
        return [OrderCancelled(order_id=oid, server_ts=now, filled_count_before_cancel=Decimal(0))]

    def _resolve_cancel_from_status(self, st: OrderStatus, rec, oid: str, delete_status, now: float,
                                    expired: bool) -> list[Any] | None:
        """If an order-status GET shows a TERMINAL status, resolve the cancel by status-truth: the order
        is off the book and ``filled_count`` is final (a race fill is booked/routed by ``_finish_cancel``
        exactly as a fill-before-cancel). Classify ``expired`` (we sent the expiry and ``now`` >= it) vs a
        plain status confirm for the ledger. Returns the event list, or None if the status is not
        terminal (the caller then retries / declares cancel_failed)."""
        if not (st.available and st.status in _TERMINAL_STATUSES):
            return None
        self.cancels_confirmed += 1
        self._last_confirmed_gone_oid = oid
        if expired:
            self.cancels_expired += 1
            via = "expired"
        else:
            self.cancels_via_status += 1
            via = "status"
        return self._finish_cancel(rec, oid, int(st.filled_count), delete_status, None, now, via=via)

    def _finish_cancel(self, rec, oid: str, filled: int, delete_status, rb, now: float,
                       *, via: str = "delete") -> list[Any]:
        """Common cancel resolution: mark the RestRecord (RETAINED for late-fill attr, F-1), book a
        race fill into money-math if one slipped in (maker fee 0, de-duped by order_id), journal
        ``cancel_confirmed``, and hand the core an OrderCancelled carrying the filled count."""
        if rec is not None:
            rec.status = "filled" if filled > 0 else "cancelled"
            if filled > 0 and rec.order_id is not None and rec.order_id not in self.booked_rest_oids:
                self.booked_rest_oids.add(rec.order_id)
                self.fills.append({"leg": "rest", "side": "no", "ticker": rec.ticker,
                                   "price": rec.price, "exec_price": None, "fee": Decimal(0),
                                   "count": int(filled), "bucket_Sd": rec.bucket_Sd,
                                   "path": "cancel_race", "client_order_id": rec.client_order_id})
        self.journal.append("cancel_confirmed",
                            {"order_id": oid, "delete_status": delete_status,
                             "reduced_by": (str(rb) if rb is not None else None),
                             "filled_before_cancel": filled, "via": via}, self.clock())
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
            resp = self.writer.rest_post(REL_SINGLE_CREATE, entries[0])
            parsed = [parse_single_response(resp.body, side=legs_by_coid[
                entries[0]["client_order_id"]].side)] if resp.ok else []
        else:
            body = build_batch(entries)
            self.journal.append("take_wings", {"legs": entries}, self.clock())
            resp = self.writer.rest_post(REL_BATCH_CREATE, body)
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
        # Route the cancel by the order's OWN shard (2026-09-14 fix): a crypto order lives on
        # exchange_index 2 and a cancel without the shard param 404s while the order stays resting.
        exch = o.get("exchange_index")
        try:
            exch = int(exch) if exch is not None else None
        except (TypeError, ValueError):
            exch = None
        wr = writer.rest_delete(cancel_path(oid, exch))
        # A 404 is only "gone" if the shard param was present AND the venue still 404s (already
        # terminal). With no shard we cannot be sure, so a 404 there counts as an error, not cancelled.
        if wr.ok or (wr.status_code == 404 and exch is not None):
            result["cancelled"] += 1
        else:
            result["errors"] += 1
        journal.append("startup_cancel", {"ticker": ticker, "order_id": oid,
                                          "exchange_index": exch, "status": wr.status_code}, clock())
    journal.append("startup_cancel_sweep", result, clock())
    return result
