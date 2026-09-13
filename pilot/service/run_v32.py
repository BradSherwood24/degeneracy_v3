"""run_v32.py — the process spine for pilot V3.2 (continuous-requote spot-bucket pump-fader).

One process == one window, launched at UTC :40 by Task Scheduler (``ops/register_v32_task.ps1``).
Mirrors ``record_range`` (memory-light StreamJournal, injected edges, stand-down, CLI) and
``run_window`` (mode lever, discovery, connect gate, watchdog re-dial, ledger row) but wires the pure
Phase-1 core ``decide_v32`` instead of the box. House law throughout: money is Decimal, time comes
only from server timestamps, every network edge is injected (tests use fakes — this is NEVER dialed
against the live proxy from an automated context), fail closed.

WHAT PHASE 2 DOES (no orders are ever sent):
  * Read the mode (``ops/v32_mode.txt`` / ``--mode``; unknown/absent -> shakedown, fail closed).
    ``armed`` DEGRADES to dry with a journaled ``degrade_to_dry`` record (reason ``phase2_no_executor``)
    — the maker executor is Phase 3, so there is no order path yet.
  * Load + sha-verify the frozen policy (``load_v32_params``); any mismatch/invalid -> stand down.
  * Discover the hourly STRIKE ladder (KXBTCD, all live generations; ticker -> exchange_index; strike
    floor via ``parse_strike_ticker``) AND the range BUCKETS (KXBTC via ``discover_range_markets``;
    ticker -> (floor, cap)). Stand down if either universe is empty, or if the observed bucket width
    does not match ``params.bucket_width`` (the 21Z $250/$500 hours stand down).
  * Connect the WS at (close - quote_start_s - 5 s) over TWO connections (see WS TOPOLOGY below).
  * Fold frames into per-market ``BookMirror``s; on each snapshot/delta/trade carrying a server ts,
    build the Phase-1 event and drive ``decide_v32``; a 0.5 s ClockTick pump advances the cutoffs.
  * Route order-bearing actions to a ``FrozenExecutor`` that journals ``would_place_rest`` /
    ``would_cancel_rest`` / ``would_take_wings`` / ``would_retry_wing`` AND synthesizes
    OrderAck/OrderCancelled (and dry wing Fills) back into the core, so the requote state machine
    ACTUALLY CYCLES in dry mode (place -> ack -> replace -> cancelled -> place). This is a SIMULATION
    of the exchange's acknowledgements, clearly labeled ``synth`` in the executor's counters; it sends
    nothing and books no money.
  * Stream every raw WS frame AND every decision record to ``journals_v32/<close>.jsonl`` (one
    StreamJournal, memory-light, same record shape as the pilot journals). Deadline = close + 10 s;
    on close flush, gzip crash-safe, one summary line, one ledger row.

ORDER-TRACKING LAYER (Phase-1 review F-1 — retained cancel context): the ``FrozenExecutor`` keeps a
``RestBook`` mapping client_order_id -> the order's (order_id, price, count, bucket ticker, bucket_Sd,
placed_ts, status) that RETAINS cancelled/replaced orders for the whole window. A ``Fill`` arriving on
a coid the core no longer tracks (a just-replaced order) is attributed here, journaled as
``late_fill``, and booked into the core through the additive ``core.book_late_rest_fill`` hook — so a
late fill is never a silent untracked unhedged leg. Phase 2 sends no orders and subscribes the private
``fill`` channel only when armed (which degrades to dry), so no real fill arrives; the path exists,
tested with fakes, ready for Phase 3.

WS TOPOLOGY DECISION — TWO connections (justified; measured per-connection lag):
  * strikes (KXBTCD ~188 markets): ``orderbook_delta`` + ``trade``.
  * buckets (KXBTC ~180 markets): ``orderbook_delta`` + ``trade`` (+ ``fill`` + ``market_positions``
    only when armed).
  Rationale: (a) a lagging STRIKE feed poisons W (the taker-wing price) while a lagging BUCKET feed
  poisons spot selection — two connections give an INDEPENDENT ``current_lag_seconds()`` gauge for
  each, so the summary/ledger records which stream aged; (b) the private channels belong only on the
  bucket connection (our resting order is a bucket-NO), so a strike-book seq-gap re-dial never churns
  a private re-subscribe; (c) both clients run on the ONE asyncio loop, so their synchronous callbacks
  are serialized — the single shared driver state needs no lock. The cost is two dial loops; the
  proven ``run_recording`` supervisor drives each (a thin ``_ConnRecorder`` adapter delegates
  book-folding/journaling to the one shared recorder).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from service.book import BookMirror, TopOfBook
from service.journal_io import _gzip_one_crash_safe
from service.proxy_auth import ProxyAuth
from service.record_range import (
    RANGE_SERIES,
    StreamJournal,
    discover_range_markets,
    journal_filename,
)
from service.record_window import (
    GRACE_SECONDS,
    _append_summary,
    next_top_of_hour_iso,
    run_recording,
    write_standdown_summary,
)
from service.v32 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    V32Params,
    V32ParamsInvalid,
    V32ParamsShaMismatch,
    V32State,
    book_late_rest_fill,
    decide_v32,
    load_v32_params,
    parse_strike_ticker,
)
from service.v32.events import STRIKE_SERIES_PREFIX
from service.v32.ledger import DEFAULT_V32_LEDGER_PATH, append_v32_ledger_row, build_v32_ledger_row
from service.wake import (
    DEAD_STATUSES,
    MARKETS_PATH,
    StandDown,
    _group_ladders,
    _leg_is_live,
    close_epoch,
    coerce_exchange_index,
)
from service.ws_client import KalshiWebSocketClient, WsCallbacks, _parse_server_ts

logger = logging.getLogger(__name__)

VALID_MODES_V32 = ("shakedown", "dry", "armed")
STRIKE_SERIES = STRIKE_SERIES_PREFIX  # "KXBTCD"

# Public channel sets (private fill/market_positions are added by the WS client when armed).
STRIKE_CHANNELS: tuple[str, ...] = ("orderbook_delta", "trade")
BUCKET_CHANNELS: tuple[str, ...] = ("orderbook_delta", "trade")

# Dial the WS this many seconds before the quote window opens (T - quote_start_s - CONNECT_MARGIN_S).
CONNECT_MARGIN_S = 5.0
# The supervisor ClockTick pump interval (drives cutoffs/staleness when no book frame is arriving).
PUMP_INTERVAL_S = 0.5
_MAX_PAGES = 50  # pagination safety cap (mirrors wake._MAX_PAGES)
_PRESTART_POLL_SECONDS = 5.0
_PUMP_GUARD = 100000  # hard cap on synthetic-event fan-out per pump (loop protection)
_HEARTBEAT_S = 10.0

_PILOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_JOURNAL_DIR = os.path.join(_PILOT_DIR, "journals_v32")
DEFAULT_LOG_DIR = os.path.join(_PILOT_DIR, "logs_v32")
DEFAULT_MODE_PATH = os.path.join(_PILOT_DIR, "ops", "v32_mode.txt")


# ===========================================================================
# Mode lever (config-file driven; Brad flips it WITHOUT re-registering the task)
# ===========================================================================
def read_v32_mode_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve_v32_mode(cli_mode: str | None, mode_txt_path: str) -> str:
    """Effective mode. CLI --mode wins; else read v32_mode.txt. Unknown/absent -> 'shakedown'
    (fail-closed: the no-orders rung)."""
    raw = cli_mode if cli_mode else read_v32_mode_file(mode_txt_path)
    m = (raw or "").strip().lower()
    return m if m in VALID_MODES_V32 else "shakedown"


def effective_mode_and_degrade(resolved_mode: str) -> tuple[str, str | None]:
    """Phase-2 mode mapping (pure, testable). ``armed`` has NO order path in Phase 2 (the maker
    executor is Phase 3), so it DEGRADES to dry with reason ``phase2_no_executor``. shakedown/dry pass
    through unchanged. Returns ``(effective_mode, degrade_reason | None)``."""
    if resolved_mode == "armed":
        return "dry", "phase2_no_executor"
    return resolved_mode, None


def connect_gate_epoch(close_epoch_val: int, params: V32Params) -> float:
    """The earliest clock time to dial the WS: close - quote_start_s - CONNECT_MARGIN_S (5 s before
    the quote window opens). Pure so the gate math is unit-tested."""
    return float(close_epoch_val) - float(params.quote_start_s) - CONNECT_MARGIN_S


# ===========================================================================
# Discovery — strike ladder (KXBTCD) + range buckets (KXBTC)
# ===========================================================================
@dataclass(frozen=True)
class StrikeDiscovery:
    """The co-settling hourly-strike universe for one close (all live generations)."""

    close_time: str
    tickers: tuple[str, ...] = ()
    floor_by_ticker: dict[str, int] = field(default_factory=dict)
    exchange_index_by_ticker: dict[str, int | None] = field(default_factory=dict)
    generations: int = 0


def _fetch_series_markets(proxy: Any, series: str, close_iso: str) -> list[dict]:
    """Paged, close-ts-narrowed, status-agnostic /markets fetch (same shape as
    ``wake._fetch_series_markets`` / ``record_range._fetch_range_markets``)."""
    target = close_epoch(close_iso)
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {
            "series_ticker": series,
            "min_close_ts": target,
            "max_close_ts": target,
            "limit": 1000,
        }
        if cursor:
            params["cursor"] = cursor
        resp = proxy.rest_get(MARKETS_PATH, params)
        out.extend(resp.get("markets", []) or [])
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def discover_strike_ladder(
    proxy: Any, close_iso: str, now_epoch: float, dead_statuses=DEAD_STATUSES
) -> StrikeDiscovery:
    """Every LIVE hourly strike (KXBTCD) co-settling at ``close_iso``, all generations.

    Keeps every market of every non-dead generation (like ``discover_range_markets``, not
    ``wake``'s single-ladder selection): the pump-fader prices its wings off any Sd/Su strike, so the
    whole co-settling ladder is subscribed. Captures ``exchange_index`` per ticker (fail-closed to
    None) for the Phase-3 dispatch, and the strike floor via ``parse_strike_ticker`` (the same key
    convention the pure core uses). Returns an empty discovery (stand-down) when nothing live
    co-settles."""
    markets = _fetch_series_markets(proxy, STRIKE_SERIES, close_iso)
    ladders = _group_ladders(markets, close_iso)
    tickers: list[str] = []
    floor_by: dict[str, int] = {}
    exch_by: dict[str, int | None] = {}
    generations = 0
    for lad in ladders:
        if not _leg_is_live(lad, now_epoch, dead_statuses):
            continue
        generations += 1
        for m in lad:
            tk = m.get("ticker")
            if not tk:
                continue
            tk = str(tk)
            floor = parse_strike_ticker(tk)
            if floor is None:
                # fall back to the record's floor_strike if the ticker suffix is unparseable
                fs = m.get("floor_strike")
                floor = int(round(float(fs))) if fs is not None else None
            if floor is not None:
                floor_by[tk] = floor
            exch_by[tk] = coerce_exchange_index(m.get("exchange_index"))
            tickers.append(tk)
    tickers = sorted(set(tickers))
    return StrikeDiscovery(close_iso, tuple(tickers), floor_by, exch_by, generations)


def build_bucket_map(discovery) -> dict[str, tuple[float, float]]:
    """ticker -> (floor, cap) for every discovered bucket (the static map the pure core keys on).

    A bucket whose record lacks floor OR cap is DROPPED (the core cannot classify it), so a
    half-populated record never becomes an untyped ticker in the map."""
    out: dict[str, tuple[float, float]] = {}
    for b in discovery.buckets:
        if b.floor is None or b.cap is None:
            continue
        out[b.ticker] = (float(b.floor), float(b.cap))
    return out


def observed_bucket_width(bucket_map: dict[str, tuple[float, float]]) -> int | None:
    """The modal per-bucket width = round(cap - floor + 0.01), or None if unmeasurable.

    Used to stand the 21Z $250/$500 hours down unless ``params.bucket_width`` matches: a $100 bucket
    (floor 68200, cap 68299.99) measures 100; a $250 bucket measures 250."""
    widths: dict[int, int] = defaultdict(int)
    for floor, cap in bucket_map.values():
        try:
            w = int(round(cap - floor + 0.01))
        except (TypeError, ValueError):
            continue
        if w > 0:
            widths[w] += 1
    if not widths:
        return None
    return max(widths.items(), key=lambda kv: kv[1])[0]


# ===========================================================================
# Order-tracking layer + FrozenExecutor (F-1 retained cancel context)
# ===========================================================================
@dataclass
class RestRecord:
    """One order the executor has (would have) placed this window. RETAINED after cancel/replace so a
    late fill on its coid is attributable (Phase-1 review F-1). ``status`` in {live,cancelled,filled}."""

    client_order_id: str
    order_id: str | None
    price: Decimal
    count: int
    ticker: str
    bucket_Sd: int | None
    placed_ts: float
    status: str


class FrozenExecutor:
    """Dry/shakedown executor: journals the WOULD_* order intents and SIMULATES the exchange's
    acknowledgements so the requote state machine cycles without sending anything.

    Simulation semantics (all counters tagged ``synth`` — no order, no money):
      * WOULD_PLACE_REST -> record the order in the RestBook as ``live`` and return an ``OrderAck``
        (synthetic order_id ``dry-<coid>``): a placed rest becomes live next event, so ``|dn|``
        replaces and cutoffs exercise exactly as they would live.
      * WOULD_CANCEL_REST -> mark the RestBook entry ``cancelled`` (RETAINED, not deleted — F-1) and
        return an ``OrderCancelled`` (filled_count_before_cancel=0): a dry cancel never fills.
      * WOULD_TAKE_WINGS / WOULD_RETRY_WING -> return a ``Fill`` for each still-pending wing leg at
        its own limit, so a (synthetic or late) rest fill completes the $2 pin and ``sets_done``
        advances. In ordinary dry the rest never fills (a maker fill cannot be honestly synthesized —
        the SHADOW answers "would we have filled" via the public print rule), so this path is reached
        only by ``book_late_rest_fill`` (the F-1 test) or a Phase-3 real fill.
    Real order dispatch (PLACE_REST/CANCEL_REST/TAKE_WINGS with orders sent) is Phase 3; those kinds
    return no synthetic events here (armed degrades to dry in Phase 2, so they never arrive)."""

    def __init__(self, bucket_map: dict[str, tuple[float, float]]) -> None:
        self.bucket_map = bucket_map
        self.rest_book: dict[str, RestRecord] = {}
        self._by_order_id: dict[str, str] = {}
        self.counts: dict[str, int] = defaultdict(int)

    def _bucket_sd(self, ticker: str) -> int | None:
        fc = self.bucket_map.get(ticker)
        return int(round(float(fc[0]))) if fc is not None else None

    def on_action(self, action, state: V32State, now: float) -> list[Any]:
        """Handle one emitted action; return the synthetic exchange events to feed back into the core."""
        k = action.kind
        if k in (ActionKind.WOULD_PLACE_REST, ActionKind.PLACE_REST):
            coid = action.client_order_id or ""
            oid = f"dry-{coid}"
            self.rest_book[coid] = RestRecord(
                client_order_id=coid,
                order_id=oid,
                price=action.price if action.price is not None else Decimal(0),
                count=int(action.count),
                ticker=action.ticker or "",
                bucket_Sd=self._bucket_sd(action.ticker or ""),
                placed_ts=now,
                status="live",
            )
            self._by_order_id[oid] = coid
            self.counts["synth_ack"] += 1
            return [OrderAck(client_order_id=coid, order_id=oid, server_ts=now)]

        if k in (ActionKind.WOULD_CANCEL_REST, ActionKind.CANCEL_REST):
            coid = action.client_order_id
            oid = action.order_id
            if coid is None and oid is not None:
                coid = self._by_order_id.get(oid)
            rec = self.rest_book.get(coid) if coid else None
            if rec is not None:
                rec.status = "cancelled"  # RETAINED for late-fill attribution (F-1)
                oid = rec.order_id
            self.counts["synth_cancelled"] += 1
            return [OrderCancelled(order_id=oid, server_ts=now, filled_count_before_cancel=Decimal(0))]

        if k in (ActionKind.WOULD_TAKE_WINGS, ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
            out: list[Any] = []
            for leg in state.wing_legs:
                if leg.status == "pending":
                    out.append(
                        Fill(
                            order_id=f"dry-w-{leg.client_order_id}",
                            client_order_id=leg.client_order_id,
                            count=Decimal(leg.count),
                            price=leg.limit,
                            side=leg.side,
                            server_ts=now,
                        )
                    )
            self.counts["synth_wing_fill"] += len(out)
            return out

        return []  # STAND_DOWN and any other order-free kind

    def attribute(self, coid: str | None = None, order_id: str | None = None) -> RestRecord | None:
        """Resolve a fill's coid/order_id to a RETAINED RestBook entry (any order we ever placed this
        window), or None if the fill is not ours (foreign — dropped by the caller, fail-closed)."""
        if coid and coid in self.rest_book:
            return self.rest_book[coid]
        if order_id and order_id in self._by_order_id:
            return self.rest_book[self._by_order_id[order_id]]
        return None

    def mark_filled(self, coid: str) -> None:
        rec = self.rest_book.get(coid)
        if rec is not None:
            rec.status = "filled"


# ===========================================================================
# The driver — wires decide_v32 onto the recorder pipeline
# ===========================================================================
def _trade_event(market: str, payload: dict, server_ts: float) -> Trade | None:
    """Build a Phase-1 ``Trade`` (YES-space Decimal DOLLARS) from a Kalshi trade frame, or None if the
    frame lacks the fields the core needs (then the caller journals it, fail-closed).

    Pinned to a real live WS ``trade`` frame (``tests/fixtures/v32/live_frames/trade_frame.json``):
    the exchange sends DOLLAR strings ``yes_price_dollars`` / ``no_price_dollars`` (e.g. "0.5700"),
    the taker side in ``taker_side``, and the size in ``count_fp`` ("10.00"). The earlier parser read
    cents fields (``yes_price`` / ``count``) that the live frame does NOT carry, so every real trade
    was dropped as ``v32_trade_unparsed`` and the shadow never filled. Cents fields are kept only as a
    last-resort fallback for any legacy/synthetic frame that lacks the ``_dollars`` fields."""
    side = payload.get("taker_side")
    if side not in ("yes", "no"):
        return None
    yes_price: Decimal | None = None
    ypd = payload.get("yes_price_dollars")
    npd = payload.get("no_price_dollars")
    yp = payload.get("yes_price")  # legacy/synthetic cents fallback
    npr = payload.get("no_price")
    pr = payload.get("price")
    try:
        if ypd is not None:
            yes_price = Decimal(str(ypd))
        elif npd is not None:
            yes_price = Decimal(1) - Decimal(str(npd))
        elif yp is not None:
            yes_price = Decimal(str(yp)) / Decimal(100)
        elif npr is not None:
            yes_price = (Decimal(100) - Decimal(str(npr))) / Decimal(100)
        elif pr is not None:
            yes_price = Decimal(str(pr)) / Decimal(100)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if yes_price is None:
        return None
    try:
        raw_count = payload.get("count_fp", payload.get("count", 0))
        count = Decimal(str(raw_count))
    except (TypeError, ValueError, ArithmeticError):
        count = Decimal(0)
    return Trade(market_ticker=market, yes_price=yes_price, taker_side=side, count=count,
                 server_ts=server_ts)


def _fill_event(payload: dict) -> dict | None:
    """Parse a Kalshi private ``fill`` WS frame into the fields V3.2 needs, or None if it carries no
    ``client_order_id`` (unattributable). Pinned to a real live frame
    (``tests/fixtures/v32/live_frames/fill_frame.json``).

    THE YES-SPACE UNITS TRAP: a NO purchase reports ``side: "yes"`` with a ``yes_price_dollars`` that
    is the YES leg's price; the NO-space price we actually PAID is ``1 - yes_price_dollars``. Key the
    conversion off ``purchased_side`` / ``outcome_side`` (both "no" on a NO fill), NEVER off ``side``.
    Carries ``count_fp``, ``order_id``, ``client_order_id``, ``is_taker`` and ``fee_cost`` so Phase 3
    can reconcile the executed price + fee against the resting order's price."""
    coid = payload.get("client_order_id")
    if not coid:
        return None
    purchased = payload.get("purchased_side") or payload.get("outcome_side")
    ypd = payload.get("yes_price_dollars")
    yes_price: Decimal | None = None
    price: Decimal | None = None  # NO-space price actually paid (1 - yes) on a NO fill
    try:
        if ypd is not None:
            yes_price = Decimal(str(ypd))
            price = (Decimal(1) - yes_price) if purchased == "no" else yes_price
    except (TypeError, ValueError, ArithmeticError):
        yes_price = None
        price = None
    try:
        count = int(Decimal(str(payload.get("count_fp", payload.get("count", 0)))))
    except (TypeError, ValueError, ArithmeticError):
        count = 0
    fee: Decimal | None = None
    try:
        if payload.get("fee_cost") is not None:
            fee = Decimal(str(payload.get("fee_cost")))
    except (TypeError, ValueError, ArithmeticError):
        fee = None
    return {
        "client_order_id": coid,
        "order_id": payload.get("order_id"),
        "purchased_side": purchased,
        "yes_price": yes_price,
        "price": price,
        "count": count,
        "is_taker": bool(payload.get("is_taker", False)),
        "fee_cost": fee,
    }


class V32Driver:
    """Holds the live V32State and drives ``decide_v32`` on each event, journaling every decision
    record and routing every order-bearing action to the executor. Pure decision logic stays in the
    core, so the driver runs live and in replay identically. Time for the ClockTick pump is derived
    from the last observed server ts plus local elapsed (``server_now``) — never the machine clock as
    a truth source (fail-closed: no server ts seen yet -> no tick driven)."""

    def __init__(
        self,
        params: V32Params,
        state: V32State,
        journal: StreamJournal,
        executor: FrozenExecutor,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.params = params
        self.state = state
        self.journal = journal
        self.executor = executor
        self.clock = clock
        self.counts: dict[str, int] = defaultdict(int)
        self._last_server_ts: float | None = None
        self._last_wall: float | None = None
        self._last_eval_key: tuple | None = None
        self._last_eval_ts: float | None = None

    # --- clock source for the ClockTick pump ---
    def _stamp(self, server_ts: float) -> None:
        self._last_server_ts = server_ts
        self._last_wall = self.clock()

    def server_now(self) -> float | None:
        """last observed server ts + local elapsed since (None until the first timestamped frame)."""
        if self._last_server_ts is None or self._last_wall is None:
            return None
        return self._last_server_ts + (self.clock() - self._last_wall)

    # --- event entry points ---
    def on_book_update(self, market: str, top: TopOfBook, server_ts: float) -> None:
        self._stamp(server_ts)
        self._pump([BookUpdate(market_ticker=market, top=top, server_ts=server_ts)])

    def on_trade(self, market: str, payload: dict, server_ts: float) -> None:
        ev = _trade_event(market, payload, server_ts)
        if ev is None:
            self.journal.append("v32_trade_unparsed", {"market": market}, self.clock())
            return
        self._stamp(server_ts)
        self._pump([ev])

    def on_clock_tick(self, server_ts: float) -> None:
        self._pump([ClockTick(server_ts=server_ts)])

    def on_fill(self, market: str, payload: dict, server_ts: float) -> None:
        """Route a private fill through the RestBook (F-1). A coid/order_id we never placed is a
        FOREIGN fill (journaled + dropped, fail-closed). A currently-tracked coid feeds a normal
        ``Fill``; a RETAINED (replaced/cancelled) coid is a LATE fill -> journaled ``late_fill`` and
        booked via the additive ``book_late_rest_fill`` hook so it is never a silent unhedged leg."""
        pf = _fill_event(payload) or {}
        coid = pf.get("client_order_id") or payload.get("client_order_id")
        oid = pf.get("order_id") or payload.get("order_id")
        rec = self.executor.attribute(coid=coid, order_id=oid)
        if rec is None:
            # coid not in the RestBook -> a foreign fill (R1). Drop + journal, fail-closed.
            self.counts["foreign_fill_ignored"] += 1
            self.journal.append(
                "foreign_fill_ignored",
                {"market": market, "client_order_id": coid, "order_id": oid},
                self.clock(),
            )
            return
        self._stamp(server_ts)
        count = int(pf.get("count") or 0) or rec.count  # count_fp; fall back to the placed count
        exec_price = pf.get("price")  # NO-space executed price (parsed from the frame; 1 - yes on NO)
        exec_fee = pf.get("fee_cost")
        live = self.state.rest_live
        pend = self.state.rest_pending
        tracked = (live is not None and live.client_order_id == rec.client_order_id) or (
            pend is not None and pend.client_order_id == rec.client_order_id
        )
        self.executor.mark_filled(rec.client_order_id)
        if tracked:
            self.counts["rest_fill"] += 1
            self.journal.append(
                "rest_fill",
                {"market": market, "client_order_id": rec.client_order_id, "order_id": rec.order_id,
                 "rest_price": rec.price, "exec_price": exec_price, "exec_fee": exec_fee,
                 "count": count},
                self.clock(),
            )
            # The rest is a post_only maker bid -> it fills AT its resting price; book the lock off
            # ``rec.price`` (the Phase-1 convention). ``exec_price``/``exec_fee`` are journaled above
            # for Phase-3 reconciliation against the frame.
            self._pump([Fill(order_id=rec.order_id, client_order_id=rec.client_order_id,
                             count=Decimal(count), price=rec.price, side="no", server_ts=server_ts)])
            return
        # F-1 late fill on a no-longer-tracked (replaced / eagerly-cancelled) own order
        self.counts["late_fill"] += 1
        self.journal.append(
            "late_fill",
            {
                "market": market,
                "client_order_id": rec.client_order_id,
                "order_id": rec.order_id,
                "price": rec.price,
                "exec_price": exec_price,
                "exec_fee": exec_fee,
                "count": count,
                "bucket_Sd": rec.bucket_Sd,
                "status_before": rec.status,
            },
            self.clock(),
        )
        self.state, actions = book_late_rest_fill(
            self.params, self.state, price=rec.price, count=count, server_ts=server_ts,
            bucket_Sd=rec.bucket_Sd,
        )
        synth: list[Any] = []
        for a in actions:
            self._journal_action(a, server_ts)
            synth += self.executor.on_action(a, self.state, server_ts)
        if synth:
            self._pump(synth)

    # --- the decide + route loop (drains synthetic exchange events to quiescence) ---
    def _pump(self, events: list[Any]) -> None:
        q: deque = deque(events)
        guard = 0
        while q:
            guard += 1
            if guard > _PUMP_GUARD:
                self.journal.append("alarm", {"alarm": "pump_runaway", "guard": guard}, self.clock())
                break
            ev = q.popleft()
            self.state, actions = decide_v32(self.params, self.state, ev)
            for a in actions:
                self._journal_action(a, getattr(ev, "server_ts", self.clock()))
                q.extend(self.executor.on_action(a, self.state, getattr(ev, "server_ts", self.clock())))
            self._maybe_eval(getattr(ev, "server_ts", None))

    # --- journaling ---
    def _journal_action(self, a, server_ts: float) -> None:
        k = a.kind
        if k in (ActionKind.WOULD_PLACE_REST, ActionKind.PLACE_REST):
            rk = "would_place_rest" if k == ActionKind.WOULD_PLACE_REST else "place_rest"
            payload = {
                "ticker": a.ticker, "side": a.side, "action": a.action, "count": a.count,
                "price": a.price, "expiration_epoch": a.expiration_epoch,
                "client_order_id": a.client_order_id,
            }
        elif k in (ActionKind.WOULD_CANCEL_REST, ActionKind.CANCEL_REST):
            rk = "would_cancel_rest" if k == ActionKind.WOULD_CANCEL_REST else "cancel_rest"
            payload = {"order_id": a.order_id, "client_order_id": a.client_order_id}
        elif k in (ActionKind.WOULD_TAKE_WINGS, ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
            legs = [
                {"ticker": lg.ticker, "side": lg.side, "action": lg.action, "count": lg.count,
                 "limit": lg.limit}
                for lg in a.legs
            ]
            # A retry is a single-leg take (RETRY_WING, or its shakedown twin WOULD_TAKE_WINGS with
            # one leg); the initial take carries both wings. Distinguish by leg count.
            is_retry = k == ActionKind.RETRY_WING or len(legs) == 1
            if k in (ActionKind.WOULD_TAKE_WINGS,) or (k == ActionKind.RETRY_WING and self.state.shakedown):
                rk = "would_retry_wing" if is_retry else "would_take_wings"
            else:
                rk = "retry_wing" if is_retry else "take_wings"
            payload = {"legs": legs, "count": a.count, "lock": a.lock}
        elif k == ActionKind.STAND_DOWN:
            rk = "stand_down"
            payload = {"reason": a.reason}
        else:
            rk = "v32_action"
            payload = {"kind": str(k)}
        self.counts[rk] += 1
        self.journal.append(rk, payload, self.clock())

    def _maybe_eval(self, server_ts: float | None) -> None:
        """Throttled observability heartbeat: emit ``v32_eval`` on a spot/quote change or every
        _HEARTBEAT_S. Never load-bearing (all decision math is in the core)."""
        if server_ts is None:
            return
        st = self.state
        key = (st.spot_Sd, str(st.desired_n), st.stand_down_reason)
        heartbeat = self._last_eval_ts is None or (server_ts - self._last_eval_ts) >= _HEARTBEAT_S
        if key == self._last_eval_key and not heartbeat:
            return
        self._last_eval_key = key
        self._last_eval_ts = server_ts
        rest = st.rest_live
        self.journal.append(
            "v32_eval",
            {
                "t_minus_s": st.close_epoch - server_ts,
                "shakedown": st.shakedown,
                "spot_Sd": st.spot_Sd,
                "spot_Su": st.spot_Su,
                "W": st.W,
                "cap": st.cap,
                "desired_n": st.desired_n,
                "rest_live_price": rest.price if rest is not None else None,
                "replace_count": st.replace_count,
                "sets_done": st.sets_done,
                "stand_down_reason": st.stand_down_reason,
            },
            self.clock(),
        )


# ===========================================================================
# Shared recorder (books + journal + drive) and per-connection supervisor adapter
# ===========================================================================
class V32Recorder:
    """Folds both connections' frames into per-market ``BookMirror``s, journals every raw frame
    (house law: journal before dispatch), and drives the ONE shared ``V32Driver``. Both WS clients
    point their callbacks + record tap here; running on the single asyncio loop, dispatch is
    serialized so the shared state needs no lock."""

    def __init__(self, journal: StreamJournal, driver: V32Driver,
                 clock: Callable[[], float] = time.time) -> None:
        self.journal = journal
        self.driver = driver
        self.clock = clock
        self.books: dict[str, BookMirror] = {}
        self.counts: dict[str, int] = defaultdict(int)

    def tap(self, stream: str, envelope: dict) -> None:
        self.journal.append(stream, envelope, self.clock())
        self.counts["ws_" + str(envelope.get("type"))] += 1

    def _book(self, market: str) -> BookMirror:
        b = self.books.get(market)
        if b is None:
            b = BookMirror()
            self.books[market] = b
        return b

    def _drive_book(self, market: str, payload: dict) -> None:
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            # F5 (box_runner discipline): fold the book but NEVER drive a decision off the machine
            # clock — the freshness law reads only server timestamps.
            self.journal.append("ws_frame_no_server_ts", {"market": market}, self.clock())
            return
        self.driver.on_book_update(market, self.books[market].top_of_book(), server_ts)

    def on_snapshot(self, market: str, payload: dict) -> None:
        self._book(market).apply_snapshot(payload)
        self._drive_book(market, payload)

    def on_delta(self, market: str, payload: dict) -> None:
        self._book(market).apply_delta(payload)
        self._drive_book(market, payload)

    def on_trade(self, market: str, payload: dict) -> None:
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            self.journal.append("ws_frame_no_server_ts", {"market": market, "type": "trade"},
                                self.clock())
            return
        self.driver.on_trade(market, payload, server_ts)

    def on_fill(self, market: str, payload: dict) -> None:
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            server_ts = self.clock()  # a private fill without a ts is still ours to attribute
        self.driver.on_fill(market, payload, server_ts)

    def record_alarm(self, kind: str, obj: dict) -> None:
        self.journal.append("alarm", {"alarm": kind, **obj}, self.clock())
        self.counts["alarm"] += 1

    def mark_suspect(self, tickers: list[str]) -> None:
        for tk in tickers:
            b = self.books.get(tk)
            if b is not None:
                b.mark_suspect()

    def callbacks(self, include_private: bool) -> WsCallbacks:
        return WsCallbacks(
            on_orderbook_snapshot=self.on_snapshot,
            on_orderbook_delta=self.on_delta,
            on_trade=self.on_trade,
            on_fill=self.on_fill if include_private else None,
        )


class _ConnRecorder:
    """Thin per-connection handle giving ``run_recording`` the surface it drives (ws_client, clock,
    record_alarm, mark_all_suspect) while delegating all book/journal work to the shared recorder."""

    def __init__(self, shared: V32Recorder, ws_client: KalshiWebSocketClient, tag: str,
                 tickers: list[str]) -> None:
        self._shared = shared
        self.ws_client = ws_client
        self._tag = tag
        self._tickers = list(tickers)

    def clock(self) -> float:
        return self._shared.clock()

    def record_alarm(self, kind: str, obj: dict) -> None:
        self._shared.record_alarm(kind, {"conn": self._tag, **obj})

    def mark_all_suspect(self) -> None:
        self._shared.mark_suspect(self._tickers)


# ===========================================================================
# Run loop (connect gate -> two dial loops + ClockTick pump)
# ===========================================================================
async def _await_gate(
    gate_epoch: float, deadline: float, clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Hold before the first dial until ``gate_epoch`` (bounded by the deadline)."""
    while True:
        remaining = min(gate_epoch, deadline) - clock()
        if remaining <= 0:
            return
        await sleep(remaining)


async def _clock_pump(
    driver: V32Driver, clock: Callable[[], float], deadline: float,
    sleep: Callable[[float], Awaitable[None]], interval: float = PUMP_INTERVAL_S,
) -> None:
    """Drive a ClockTick every ``interval`` seconds from the supervisor loop, using the driver's
    server-derived clock (last server ts + local elapsed). No tick before the first timestamped frame
    (fail-closed). Advances the window cutoffs / staleness even when book frames stop arriving."""
    while clock() < deadline:
        await sleep(interval)
        if clock() >= deadline:
            break
        sn = driver.server_now()
        if sn is not None:
            driver.on_clock_tick(sn)


async def run_v32_window(
    shared: V32Recorder,
    strike_conn: _ConnRecorder,
    bucket_conn: _ConnRecorder,
    driver: V32Driver,
    clock: Callable[[], float],
    deadline: float,
    gate_epoch: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pump_interval: float = PUMP_INTERVAL_S,
) -> None:
    """Await the connect gate, then run both dial loops + the ClockTick pump concurrently on one loop
    until the deadline. ``sleep`` is injected so tests drive the whole thing with a fake clock."""
    await _await_gate(gate_epoch, deadline, clock, sleep)
    await asyncio.gather(
        run_recording(strike_conn, deadline=deadline, sleep=sleep),
        run_recording(bucket_conn, deadline=deadline, sleep=sleep),
        _clock_pump(driver, clock, deadline, sleep, pump_interval),
    )


# ===========================================================================
# Finalize
# ===========================================================================
def _gzip_journal(journal_path: str) -> dict:
    try:
        raw_bytes, gz_bytes = _gzip_one_crash_safe(journal_path)
        return {"final_path": journal_path + ".gz", "gzipped": True,
                "raw_bytes": raw_bytes, "gz_bytes": gz_bytes, "error": None}
    except Exception as e:  # noqa: BLE001 - never lose the recording to a gzip failure
        logger.warning("[V32] gzip failed (%s) — leaving raw journal %s", e, journal_path)
        return {"final_path": journal_path, "gzipped": False,
                "raw_bytes": None, "gz_bytes": None, "error": str(e)}


def _finalize(
    *, journal: StreamJournal, shared: V32Recorder, driver: V32Driver, close_iso: str,
    resolved_mode: str, effective_mode: str, degrade: str | None, params: V32Params,
    strike_disc: StrikeDiscovery, bucket_map: dict[str, tuple[float, float]],
    bucket_generations: int, journal_path: str, summary_path: str, ledger_path: str,
    strike_lag: float | None, bucket_lag: float | None, clock: Callable[[], float],
) -> dict:
    journal.close()
    gz = _gzip_journal(journal_path)
    final_path = os.path.abspath(gz.get("final_path") or journal_path)
    row = build_v32_ledger_row(
        close_time=close_iso,
        resolved_mode=resolved_mode,
        effective_mode=effective_mode,
        degrade=degrade,
        params=params,
        state=driver.state,
        driver_counts=dict(driver.counts),
        executor_counts=dict(driver.executor.counts),
        ws_counts=dict(shared.counts),
        strike_count=len(strike_disc.tickers),
        strike_generations=strike_disc.generations,
        bucket_count=len(bucket_map),
        bucket_generations=bucket_generations,
        strike_lag_seconds=strike_lag,
        bucket_lag_seconds=bucket_lag,
        journal_path=final_path,
        record_count=len(journal),
        stand_down_reason=None,
        now=clock(),
    )
    append_v32_ledger_row(row, ledger_path)
    summary = {
        "close_time": close_iso,
        "stand_down": False,
        "resolved_mode": resolved_mode,
        "effective_mode": effective_mode,
        "degrade": degrade,
        "params_sha": params.sha256,
        "journal_path": final_path,
        "records": len(journal),
        "strike_count": len(strike_disc.tickers),
        "bucket_count": len(bucket_map),
        "would_places": driver.counts.get("would_place_rest", 0),
        "replaces": driver.state.replace_count,
        "sets_done": driver.state.sets_done,
        "ws_counts": dict(shared.counts),
        "gzipped": bool(gz.get("gzipped")),
        "strike_lag_seconds": strike_lag,
        "bucket_lag_seconds": bucket_lag,
        "flushed_at": clock(),
    }
    _append_summary(summary_path, summary)
    return summary


def _stand_down(summary_path: str, ledger_path: str, close_iso: str, reason: str,
                *, resolved_mode: str, effective_mode: str, degrade: str | None,
                params_sha: str | None, clock: Callable[[], float]) -> int:
    sd = StandDown(close_iso, reason)
    write_standdown_summary(summary_path, sd, clock)
    row = build_v32_ledger_row(
        close_time=close_iso, resolved_mode=resolved_mode, effective_mode=effective_mode,
        degrade=degrade, params=None, state=None, driver_counts={}, executor_counts={},
        ws_counts={}, strike_count=0, strike_generations=0, bucket_count=0, bucket_generations=0,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path=None, record_count=0,
        stand_down_reason=reason, params_sha=params_sha, now=clock(),
    )
    append_v32_ledger_row(row, ledger_path)
    logger.info("[V32] stand down %s: %s", close_iso, reason)
    return 0


# ===========================================================================
# main
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V3.2 pump-fader window process (shakedown/dry; armed degrades to dry in Phase 2)."
    )
    parser.add_argument("--close", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--mode", default=None, choices=list(VALID_MODES_V32),
                        help="Override the mode (else read ops/v32_mode.txt; unknown -> shakedown).")
    parser.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--ledger", default=DEFAULT_V32_LEDGER_PATH)
    parser.add_argument("--mode-file", default=DEFAULT_MODE_PATH)
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    parser.add_argument("--flush-every", type=int, default=200,
                        help="Flush the write-through journal to the OS every N frames (default 200).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    proxy = ProxyAuth(base_url=args.proxy_base) if args.proxy_base else ProxyAuth()
    close_iso = args.close or next_top_of_hour_iso(clock())
    summary_path = os.path.join(args.journal_dir, "summary.jsonl")

    resolved_mode = resolve_v32_mode(args.mode, args.mode_file)

    # Load + sha-verify the frozen policy; any drift -> clean stand-down (fail-closed, S5 discipline).
    try:
        params = load_v32_params()
    except (V32ParamsShaMismatch, V32ParamsInvalid, KeyError, OSError, ValueError) as e:
        return _stand_down(summary_path, args.ledger, close_iso, f"params_load_failed: {e}",
                           resolved_mode=resolved_mode, effective_mode="shakedown", degrade=None,
                           params_sha=None, clock=clock)

    # Phase 2: armed has no order path -> degrade to dry with a journaled reason. shakedown/dry both
    # run the FrozenExecutor (WOULD_* twins + synthesized acks), so state.shakedown is True for both.
    effective_mode, degrade = effective_mode_and_degrade(resolved_mode)
    shakedown = True  # Phase 2 never sends orders; armed is Phase 3
    include_private = False  # private fill/positions only when actually armed (Phase 3)

    # Discovery.
    strike_disc = discover_strike_ladder(proxy, close_iso, clock())
    range_disc = discover_range_markets(proxy, close_iso, clock())
    bucket_map = build_bucket_map(range_disc)

    if not bucket_map:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"no KXBTC range buckets co-settling at {close_iso}",
                           resolved_mode=resolved_mode, effective_mode=effective_mode,
                           degrade=degrade, params_sha=params.sha256, clock=clock)
    if not strike_disc.tickers:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"no KXBTCD strike ladder co-settling at {close_iso}",
                           resolved_mode=resolved_mode, effective_mode=effective_mode,
                           degrade=degrade, params_sha=params.sha256, clock=clock)
    obs_width = observed_bucket_width(bucket_map)
    if obs_width is not None and obs_width != params.bucket_width:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"bucket width {obs_width} != params.bucket_width {params.bucket_width} "
                           f"(21Z $250/$500 hour)",
                           resolved_mode=resolved_mode, effective_mode=effective_mode,
                           degrade=degrade, params_sha=params.sha256, clock=clock)

    logger.info("[V32] %s: %d strikes (%d gen), %d buckets (%d gen), mode=%s effective=%s",
                close_iso, len(strike_disc.tickers), strike_disc.generations, len(bucket_map),
                range_disc.generations, resolved_mode, effective_mode)

    # Journal + driver + executor + recorder + WS clients.
    journal_path = os.path.join(args.journal_dir, journal_filename(close_iso))
    journal = StreamJournal(journal_path, flush_every=args.flush_every)
    journal.open()
    journal.append(
        "window_meta",
        {
            "close_time": close_iso,
            "resolved_mode": resolved_mode,
            "effective_mode": effective_mode,
            "degrade": degrade,
            "params_sha": params.sha256,
            "strike_series": STRIKE_SERIES,
            "range_series": RANGE_SERIES,
            "strike_count": len(strike_disc.tickers),
            "strike_generations": strike_disc.generations,
            "bucket_count": len(bucket_map),
            "bucket_generations": range_disc.generations,
            "bucket_width": params.bucket_width,
            "buckets": range_disc.window_meta().get("buckets", []),
        },
        clock(),
    )
    if degrade is not None:
        journal.append("degrade_to_dry", {"reason": degrade, "from_mode": resolved_mode}, clock())

    cts = close_epoch(close_iso)
    state = V32State.new(close_iso, cts, bucket_map, params, shakedown=shakedown)
    executor = FrozenExecutor(bucket_map)
    driver = V32Driver(params, state, journal, executor, clock=clock)
    shared = V32Recorder(journal, driver, clock=clock)

    strike_ws = KalshiWebSocketClient(
        proxy_auth=proxy, tickers=list(strike_disc.tickers), callbacks=shared.callbacks(False),
        include_private=False, record=shared.tap, clock=clock, channels=STRIKE_CHANNELS,
    )
    bucket_ws = KalshiWebSocketClient(
        proxy_auth=proxy, tickers=sorted(bucket_map), callbacks=shared.callbacks(include_private),
        include_private=include_private, record=shared.tap, clock=clock, channels=BUCKET_CHANNELS,
    )
    strike_conn = _ConnRecorder(shared, strike_ws, "strikes", list(strike_disc.tickers))
    bucket_conn = _ConnRecorder(shared, bucket_ws, "buckets", sorted(bucket_map))

    deadline = cts + GRACE_SECONDS
    gate = connect_gate_epoch(cts, params)
    try:
        asyncio.run(run_v32_window(shared, strike_conn, bucket_conn, driver, clock, deadline, gate))
    except KeyboardInterrupt:
        logger.warning("[V32] Ctrl+C — flushing streamed journal.")
    finally:
        summary = _finalize(
            journal=journal, shared=shared, driver=driver, close_iso=close_iso,
            resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
            params=params, strike_disc=strike_disc, bucket_map=bucket_map,
            bucket_generations=range_disc.generations, journal_path=journal_path,
            summary_path=summary_path, ledger_path=args.ledger,
            strike_lag=strike_ws.current_lag_seconds(), bucket_lag=bucket_ws.current_lag_seconds(),
            clock=clock,
        )
        logger.info("[V32] window done: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
