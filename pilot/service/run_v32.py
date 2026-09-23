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
import math
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from service.book import BookMirror, TopOfBook
from service.journal_io import _gzip_one_crash_safe
from service.proxy_auth import ProxyAuth
from service.proxy_writer import ProxyWriter
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
    OrderAmended,
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
from service.v32.executor import (
    LiveExecutor,
    cancel_stale_open_orders,
)
from service.v32.core import lock_value
from service.v32.ledger import (
    append_v32_ledger_row,
    build_v32_ledger_row,
    load_v32_rows,
    v32_pending_credit,
    v32_set_floor_dollars,
    v32_settlement_backfill_sweep,
)
from service._simlaw import fee as _fee
from service._simlaw import fee_rate as _FEE_RATE
from service.v32.stops import (
    V32_S1_LEGGED_LATCH_THRESHOLD,
    decide_v32_arming,
    record_legged_occurrence,
    v32_day_guard_path,
    v32_s4_decision,
)
from service.paths import (
    checkout_ops_dir,
    data_dir,
    default_proxy_base,
    journal_dir_v32,
    ledger_path_v32,
    log_dir_v32,
    mode_path_v32,
    ops_dir_v32,
)
from service.stops import (
    ensure_balance_start,
    parse_balance,
    read_day_guard,
)
from service.wake import (
    DEAD_STATUSES,
    FIFTEEN_SERIES,
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
# Writable paths route through service.paths so DV3_DATA_DIR (the host-shaped runtime / Render disk)
# can relocate them; with DV3_DATA_DIR unset every value below is byte-identical to the historic
# _PILOT_DIR-relative literal (behaviour-neutral for the live V3.2). The falsifier is a READ-ONLY
# input and always stays in the checkout.
DEFAULT_JOURNAL_DIR = journal_dir_v32()
DEFAULT_LOG_DIR = log_dir_v32()
DEFAULT_MODE_PATH = mode_path_v32()
DEFAULT_FALSIFIER_PATH = os.path.join(_PILOT_DIR, "ceremony", "v32_falsifier.md")


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


def _resolve_v32_guard_path(utc_day: str) -> str:
    """The V3.2 day-guard/stops file for ``utc_day``. Normally under ``ops_dir_v32()``.

    Mid-day-cutover safety (PR #83 review Finding #1): if ``DV3_DATA_DIR`` is set but the data-dir guard
    for today does NOT yet exist while the CHECKOUT ``ops/v32_stops_<day>.json`` DOES, use the CHECKOUT
    guard as authoritative for that day and log loudly -- so a latched S1/S2/S4 stop (or legged count)
    from earlier in the same UTC day is never silently dropped by opting into a data dir mid-day. This
    is a read-only-style fallback for the GUARD ONLY -- NEVER for the mode file (which has no fallback,
    by decision). With ``DV3_DATA_DIR`` unset this is byte-identical to the historic
    ``v32_day_guard_path(os.path.join(_PILOT_DIR, "ops"), utc_day)``.
    """
    primary = v32_day_guard_path(ops_dir_v32(), utc_day)
    if data_dir() is None:
        return primary
    checkout = v32_day_guard_path(checkout_ops_dir(), utc_day)
    if primary != checkout and not os.path.exists(primary) and os.path.exists(checkout):
        logger.warning(
            "[V32] DV3_DATA_DIR set but data-dir day-guard %s is missing while the checkout guard %s "
            "exists -> using the CHECKOUT guard as authoritative for %s. Copy today's "
            "v32_stops_*.json into the data dir at cutover (see V33_RUNBOOK). Latched stops preserved.",
            primary, checkout, utc_day)
        return checkout
    return primary


def effective_mode_and_degrade(resolved_mode: str) -> tuple[str, str | None]:
    """Phase-3 mode passthrough (pure, testable). All three modes pass through unchanged here — the
    armed->dry DEGRADE decision now lives in ``service.v32.stops.decide_v32_arming`` (S5 + reconcile +
    day latch + S4), which runs in ``main`` with the live /health, positions and balance. shakedown/dry
    never arm. Returns ``(effective_mode, degrade_reason | None)`` (degrade is always None here)."""
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


@dataclass(frozen=True)
class M15Discovery:
    """The co-settling 15-minute (KXBTC15M) market(s) recorded ALONGSIDE the window (recording-only —
    V3.2 never trades the 15M leg, it exists so this process is the single tape recorder and the v1.1
    pilot can stay disabled with no data gap)."""

    close_time: str
    tickers: tuple[str, ...] = ()
    exchange_index_by_ticker: dict[str, int | None] = field(default_factory=dict)


def discover_co_settling_15m(
    proxy: Any, close_iso: str, now_epoch: float, dead_statuses=DEAD_STATUSES
) -> M15Discovery:
    """Every LIVE KXBTC15M market co-settling at ``close_iso`` (usually one). Reuses the same
    status-agnostic, close-ts-narrowed paged /markets fetch the wake/strike/bucket discoveries use;
    liveness via ``_leg_is_live`` on each single-market ladder (close in the future AND not a dead
    status). Absence is NOT a stand-down — the caller journals ``m15_missing`` and continues, because
    this leg is RECORDING-ONLY (the decision path never classifies a 15M ticker)."""
    markets = _fetch_series_markets(proxy, FIFTEEN_SERIES, close_iso)
    tickers: list[str] = []
    exch_by: dict[str, int | None] = {}
    for m in markets:
        tk = m.get("ticker")
        if not tk:
            continue
        if not _leg_is_live([m], now_epoch, dead_statuses):
            continue
        tk = str(tk)
        exch_by[tk] = coerce_exchange_index(m.get("exchange_index"))
        tickers.append(tk)
    tickers = sorted(set(tickers))
    return M15Discovery(close_iso, tuple(tickers), exch_by)


def discover_co_settling_15m_safe(
    proxy: Any, close_iso: str, now_epoch: float, dead_statuses=DEAD_STATUSES
) -> tuple[M15Discovery, str | None]:
    """Recording-only 15M discovery that NEVER raises out. The strike/bucket discoveries ARE the trade
    (their failure correctly stands the window down), but the 15M leg is RECORDING-ONLY, so a discovery
    failure (proxy 5xx, malformed body) must never cost a viable trading window. Returns
    ``(M15Discovery, error)`` — an empty discovery + an error string the caller journals as
    ``m15_discovery_error`` before continuing, instead of aborting the window."""
    try:
        return discover_co_settling_15m(proxy, close_iso, now_epoch, dead_statuses), None
    except Exception as e:  # noqa: BLE001 — recording-only leg: its failure must not stand the window down
        return M15Discovery(close_iso), repr(e)


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


def bucket_width_of(floor: float, cap: float) -> int | None:
    """The $ width of one bucket [floor, cap], or None if unmeasurable (round(cap - floor + 0.01))."""
    try:
        w = int(round(float(cap) - float(floor) + 0.01))
    except (TypeError, ValueError):
        return None
    return w if w > 0 else None


def filter_buckets_to_width(
    bucket_map: dict[str, tuple[float, float]], width: int
) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """P3-2: DROP every bucket whose width != ``width`` so a mixed-width hour cannot select a
    wrong-width spot bucket and break the $2 pin (a strike could land INSIDE a 250-wide bucket). Returns
    (kept_map, dropped_tickers). The caller stands down if the kept map is empty."""
    kept: dict[str, tuple[float, float]] = {}
    dropped: list[str] = []
    for tk, (floor, cap) in bucket_map.items():
        if bucket_width_of(floor, cap) == int(width):
            kept[tk] = (floor, cap)
        else:
            dropped.append(tk)
    return kept, dropped


# ===========================================================================
# Proxy read helpers (health / settlement) — all GET through the proxy
# ===========================================================================
def get_health(proxy_base: str, http_get: Callable[..., Any] | None = None) -> dict[str, Any]:
    """GET {base}/health (the proxy's own endpoint; not under /trade-api/v2). Fail-closed to {} on any
    error so the arming check simply refuses. ``http_get`` is injected in tests."""
    try:
        if http_get is not None:
            resp = http_get(proxy_base.rstrip("/") + "/health")
        else:
            import requests
            resp = requests.get(proxy_base.rstrip("/") + "/health", timeout=5.0)
        if getattr(resp, "status_code", None) != 200:
            return {}
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[V32] /health read failed: %s", e)
        return {}


def fetch_market_result_v32(proxy: Any, ticker: str) -> str | None:
    """The settled result ('yes'/'no') for ``ticker`` via /markets/{ticker} (exact-ticker match), or
    None until settled/unavailable. Mirror of run_window._fetch_market_result (fail-closed to None)."""
    try:
        resp = proxy.rest_get(f"/markets/{ticker}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[V32] market-result fetch failed for %s: %s", ticker, e)
        return None
    if not isinstance(resp, dict):
        return None
    market = resp.get("market")
    rec = market if isinstance(market, dict) and market.get("ticker") == ticker else None
    if rec is None:
        markets = resp.get("markets")
        if isinstance(markets, list):
            rec = next((m for m in markets if isinstance(m, dict) and m.get("ticker") == ticker), None)
    if not isinstance(rec, dict):
        return None
    result = rec.get("result")
    return result if result in ("yes", "no") else None


# ===========================================================================
# Executor selection — P3-1: the ONE place the executor kind is chosen
# ===========================================================================
def build_executor(
    effective_mode: str,
    *,
    bucket_map: dict[str, tuple[float, float]],
    exchange_index_by_ticker: dict[str, int | None],
    journal: StreamJournal,
    close_epoch_val: int,
    params: V32Params,
    writer: ProxyWriter | None,
    clock: Callable[[], float] = time.time,
) -> Any:
    """Select the executor by ``effective_mode`` in EXACTLY ONE place (P3-1). ``armed`` -> the real
    ``LiveExecutor`` (requires a ``ProxyWriter``); everything else -> the dry ``FrozenExecutor`` (which
    refuses a real action kind). A LiveExecutor is NEVER constructed unless effective_mode is armed."""
    if effective_mode == "armed":
        if writer is None:
            raise ValueError("armed executor requires a ProxyWriter (P3-1)")
        return LiveExecutor(
            writer, bucket_map, exchange_index_by_ticker, journal, close_epoch_val,
            params.quote_end_s, clock=clock,
        )
    return FrozenExecutor(bucket_map)


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

    # Real (order-emitting) action kinds. A FrozenExecutor must NEVER see one — it would synthesize a
    # phantom ack/cancel/fill and book money that was never sent (Phase-2 review P3-1). The executor is
    # selected by ``effective_mode`` in exactly one place (``build_executor``); a real kind reaching a
    # FrozenExecutor is a mis-wire, so we fail loud rather than silently synth-fill.
    _REAL_KINDS = (
        ActionKind.PLACE_REST, ActionKind.CANCEL_REST, ActionKind.AMEND_REST,
        ActionKind.TAKE_WINGS, ActionKind.RETRY_WING,
    )

    def on_action(self, action, state: V32State, now: float) -> list[Any]:
        """Handle one emitted action; return the synthetic exchange events to feed back into the core.

        P3-1: refuses a REAL (non-WOULD_*) kind — the core must be in shakedown (WOULD_* twins only)
        whenever a FrozenExecutor is selected, so a real kind here means the armed executor was not
        wired. Raise so the mis-wire fails loud and the window stands down, never synth-fills live money.
        """
        k = action.kind
        if k in self._REAL_KINDS:
            raise AssertionError(
                f"FrozenExecutor received a REAL action kind {k}; a FrozenExecutor may only handle "
                f"WOULD_* twins (the core must be shakedown=True). Armed windows use LiveExecutor (P3-1)."
            )
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

        if k == ActionKind.WOULD_AMEND_REST:
            # amend-first replace (dry/shakedown twin): simulate the venue's amend so the requote state
            # machine cycles without sending anything. The order_id persists; the RestBook entry moves to
            # the new coid + price (the old coid RETAINED for late-fill attribution, F-1). No dry fill.
            coid_old = action.client_order_id
            coid_new = action.updated_client_order_id or coid_old
            oid = action.order_id
            rec = self.rest_book.get(coid_old) if coid_old else None
            if oid is None and rec is not None:
                oid = rec.order_id
            new_price = action.price if action.price is not None else (
                rec.price if rec is not None else Decimal(0))
            if rec is not None:
                self.rest_book[coid_new] = RestRecord(
                    client_order_id=coid_new, order_id=oid, price=new_price, count=rec.count,
                    ticker=rec.ticker, bucket_Sd=rec.bucket_Sd, placed_ts=now, status="live",
                )
                if coid_old is not None and coid_old != coid_new:
                    rec.status = "amended"  # RETAINED for late-fill attribution (F-1)
                if oid is not None:
                    self._by_order_id[oid] = coid_new
            self.counts["synth_amended"] += 1
            return [OrderAmended(order_id=oid or f"dry-{coid_old}", client_order_id=coid_new,
                                 price=new_price, server_ts=now,
                                 remaining_count=Decimal(int(action.count or 1)),
                                 fill_count=Decimal(0), average_fill_price=None)]

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
        "trade_id": payload.get("trade_id"),   # for fill de-dup (WS channel + status poll)
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
    a truth source (fail-closed: no server ts seen yet -> no tick driven).

    CLOCK LAW (P3-3 + 2026-09-14 clock-flap fix): the two WS connections carry independent, interleaving
    server clocks. The EVALUATION clock handed to the core as each event's ``server_ts`` (``now``) is the
    MONOTONE ``_last_server_ts`` = max over every frame's ts on BOTH connections, so it is never behind
    any book already folded from the other connection. A book's OWN raw frame ts still travels as
    ``BookUpdate.book_ts`` and is what ``_fold_book`` records for that market, so genuine staleness
    (a stalled feed) is still detected while the monotone clock advances off the live connection. This
    kills the negative-age flap that emptied the first live-dry hour (close 2026-09-14T17:00:00Z)."""

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
        # fill de-dup: a fill can surface on BOTH the private ``fill`` WS channel AND the 1 s
        # order-status poll (belt and braces). Book each once, keyed by trade_id (WS) / order_id (poll).
        self._seen_trade_ids: set[str] = set()
        self._rest_fill_booked_oids: set[str] = set()
        # Last-quoted spot bucket, captured WHILE quoting (each place/would-place), so the finalized
        # ledger row + summary carry the bucket we actually rested on rather than the post-quote-end
        # reset state (which nulls spot_Sd/Su at close). Plus stand-down bookkeeping: the T-5
        # end-of-quoting cancel emits a STAND_DOWN("past_quote_end") that must NOT count as a
        # stand-down (a stand-down means an alarm / staleness / no-spot event).
        self._last_quoted_bucket_ticker: str | None = None
        self._last_quoted_Sd: int | None = None
        self._last_quoted_Su: int | None = None
        self._last_rest_price: Decimal | None = None
        self._last_desired_n: Decimal | None = None
        self._spot_buckets_quoted: list[int] = []        # Sd ints, order of first appearance
        self._last_stand_down_reason: str | None = None   # last REAL stand-down (not quote-end)
        self._real_stand_downs: int = 0                   # alarms / staleness / no-spot only
        self._quote_end_cancel: bool = False              # the T-5 end-of-quoting cancel fired

    # --- clock source for the ClockTick pump ---
    def _stamp(self, server_ts: float) -> None:
        # P3-3: the two WS connections carry independent server clocks that interleave. Keep the
        # freshness/tick clock MONOTONE (never let a later-arriving frame from the slower clock step
        # ``server_now`` backwards) so a clock skew cannot make a fresh book look stale -> spurious
        # CANCEL_REST + re-place churn (budget burn + A_REPLACE). A regressing frame is still FOLDED
        # (its book is applied by the recorder); only the tick clock refuses to go back.
        prev = self._last_server_ts
        self._last_server_ts = server_ts if prev is None else max(prev, server_ts)
        self._last_wall = self.clock()

    def server_now(self) -> float | None:
        """last observed server ts + local elapsed since (None until the first timestamped frame)."""
        if self._last_server_ts is None or self._last_wall is None:
            return None
        return self._last_server_ts + (self.clock() - self._last_wall)

    # --- event entry points ---
    def on_book_update(self, market: str, top: TopOfBook, server_ts: float) -> None:
        # P3-3 / clock-flap fix (2026-09-14): the EVALUATION clock (``now``) is the MONOTONE
        # ``self._last_server_ts`` (= max(this frame's ts, every prior frame's ts across BOTH
        # connections), never behind any folded book), while ``book_ts`` keeps this frame's OWN raw ts
        # so a genuinely stalled feed still ages that market's book out. Before the fix ``now`` was the
        # raw frame ts, so a frame from the slower connection made ``now`` regress below a book stamped
        # by the faster one -> negative age -> "stale" -> W None -> cancel; the next (newer) frame ->
        # place; ~1 ms flap (61 place/cancel pairs -> replace-rate alarm -> dead hour) in the first
        # live-dry window (close 2026-09-14T17:00:00Z).
        self._stamp(server_ts)
        eval_ts = self._last_server_ts  # monotone across connections
        self._pump([BookUpdate(market_ticker=market, top=top, server_ts=eval_ts, book_ts=server_ts)])

    def on_trade(self, market: str, payload: dict, server_ts: float) -> None:
        ev = _trade_event(market, payload, server_ts)
        if ev is None:
            self.journal.append("v32_trade_unparsed", {"market": market}, self.clock())
            return
        self._stamp(server_ts)
        # Same monotone evaluation clock as the book path: the shadow-on-trade freshness gate compares
        # this ``server_ts`` against the spot bucket's book ts, so it must not regress below a book the
        # other connection just stamped (a Trade carries no book to age, so it needs no ``book_ts``).
        ev = replace(ev, server_ts=self._last_server_ts)
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
        trade_id = pf.get("trade_id") or payload.get("trade_id")
        # de-dup: a fill can arrive on the WS channel AND the status poll; book each trade once.
        if trade_id is not None and trade_id in self._seen_trade_ids:
            self.counts["fill_dup_ignored"] += 1
            return
        # a WS echo of a taker wing leg (already booked synchronously from the batch response) -> skip
        # (not "foreign"): the wing coid is known to the executor.
        if coid is not None and coid in getattr(self.executor, "wing_coids", set()):
            if trade_id is not None:
                self._seen_trade_ids.add(trade_id)
            self.counts["wing_fill_ws_dup"] += 1
            return
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
        if trade_id is not None:
            self._seen_trade_ids.add(trade_id)
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
            if rec.order_id is not None:
                self._rest_fill_booked_oids.add(rec.order_id)
            self.counts["rest_fill"] += 1
            self._check_exec_price(rec, exec_price)  # P3-4
            self.journal.append(
                "rest_fill",
                {"market": market, "client_order_id": rec.client_order_id, "order_id": rec.order_id,
                 "rest_price": rec.price, "exec_price": exec_price, "exec_fee": exec_fee,
                 "count": count, "path": "ws"},
                self.clock(),
            )
            self._record_fill(rec, exec_price, exec_fee, count, path="ws")
            # The rest is a post_only maker bid -> it fills AT its resting price; book the lock off
            # ``rec.price`` (the Phase-1 convention). ``exec_price``/``exec_fee`` are journaled above
            # for Phase-3 reconciliation against the frame.
            self._pump([Fill(order_id=rec.order_id, client_order_id=rec.client_order_id,
                             count=Decimal(count), price=rec.price, side="no", server_ts=server_ts)])
            return
        # F-1 late fill on a no-longer-tracked (replaced / eagerly-cancelled) own order
        if rec.order_id is not None:
            self._rest_fill_booked_oids.add(rec.order_id)
        self.counts["late_fill"] += 1
        self._check_exec_price(rec, exec_price)  # P3-4
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
                "path": "ws",
            },
            self.clock(),
        )
        self._record_fill(rec, exec_price, exec_fee, count, path="ws")
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

    # --- P3-4 exec-price reconciliation + money-math capture ---
    def _check_exec_price(self, rec, exec_price) -> None:
        """P3-4: a post_only maker fills at its RESTING limit, so the frame's NO-space executed price
        must equal ``rec.price``. A mismatch means our model of maker fills is wrong — record an alarm
        and continue (the lock is still booked at the resting price, the conservative convention)."""
        if exec_price is None:
            return
        try:
            if abs(Decimal(str(exec_price)) - Decimal(str(rec.price))) > Decimal("0.0001"):
                detail = {"client_order_id": rec.client_order_id, "order_id": rec.order_id,
                          "rest_price": str(rec.price), "exec_price": str(exec_price)}
                self.counts["exec_price_mismatch"] += 1
                self.journal.append("alarm", {"alarm": "exec_price_mismatch", **detail}, self.clock())
                mm = getattr(self.executor, "exec_price_mismatches", None)
                if mm is not None:
                    mm.append(detail)
        except (ArithmeticError, ValueError, TypeError):
            pass

    def _record_fill(self, rec, exec_price, exec_fee, count: int, *, path: str) -> None:
        """Append the REST fill to the executor's money-math capture (the ledger reads it at finalize).
        Booked at the resting price ``rec.price`` (post_only maker); the frame's executed price/fee are
        kept for reconciliation."""
        fills = getattr(self.executor, "fills", None)
        if fills is None:
            return
        # de-dup the REST leg across the cancel-race / WS / poll paths so money-math never
        # double-counts one fill (the cancel path may have already booked it by order_id).
        booked = getattr(self.executor, "booked_rest_oids", None)
        if booked is not None and rec.order_id is not None:
            if rec.order_id in booked:
                return
            booked.add(rec.order_id)
        fills.append({"leg": "rest", "side": "no", "ticker": rec.ticker, "price": rec.price,
                      "exec_price": exec_price, "fee": exec_fee, "count": int(count),
                      "bucket_Sd": rec.bucket_Sd, "path": path,
                      "client_order_id": rec.client_order_id})

    def on_poll_fill(self, order_id: str, filled_count: int, server_ts: float) -> None:
        """Book a REST fill discovered by the 1 s order-status poll (belt and braces).

        ``filled_count`` is the venue-CUMULATIVE fill for this order. The core treats ``Fill.count`` as a
        PER-FILL delta, so we feed only the DELTA over what the core has already booked for this order
        (``rest_booked_by_coid``) — mirroring the cancel path's cumulative->delta arithmetic. This is
        what makes the poll BACKSTOP the extra lots at ``contracts`` > 1 (PARTIAL-FILL WINGS N1): a lot
        whose WS ``fill`` was missed (WS hiccup / reconnect / low-RAM watcher death — the reason the poll
        exists) is topped up here instead of sitting naked until the T-5 quote-end cancel. At
        ``contracts`` = 1 the order is fully booked after one fill, so a poll reporting the same total
        yields delta 0 and does nothing — byte-identical to the pre-partial single-shot poll (and the
        WS-then-poll dedup: the WS lot already advanced ``rest_booked_by_coid``)."""
        if filled_count <= 0:
            return
        rec = self.executor.attribute(order_id=order_id)
        if rec is None:
            return
        already = int(self.state.rest_booked_by_coid.get(rec.client_order_id, 0))
        delta = int(filled_count) - already
        if delta <= 0:
            return
        self._rest_fill_booked_oids.add(order_id)
        self._stamp(server_ts)
        self.executor.mark_filled(rec.client_order_id)
        self.counts["rest_fill_poll"] += 1
        self.journal.append(
            "rest_fill", {"client_order_id": rec.client_order_id, "order_id": order_id,
                          "rest_price": rec.price, "count": int(delta), "path": "poll"},
            self.clock(),
        )
        self._record_fill(rec, None, None, int(delta), path="poll")
        # feed the core the DELTA (per-fill) so it books the newly-filled lot(s) and completes their wings.
        self.state, actions = decide_v32(
            self.params, self.state,
            Fill(order_id=order_id, client_order_id=rec.client_order_id,
                 count=Decimal(int(delta)), price=rec.price, side="no", server_ts=server_ts),
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
            self._apply_executor_standdown(getattr(ev, "server_ts", self.clock()))
            self._maybe_eval(getattr(ev, "server_ts", None))

    def _apply_executor_standdown(self, server_ts: float) -> None:
        """The armed executor latches ``stand_down_reason`` after 3 consecutive rest rejections. Turn
        that into the core's ``stood_down`` so ``_requote`` cancels + stops quoting for the hour (the
        next book/clock tick honors it); idempotent once applied."""
        reason = getattr(self.executor, "stand_down_reason", None)
        if reason and not self.state.stood_down:
            from dataclasses import replace as _replace
            self.state = _replace(self.state, stood_down=True)
            self.counts["executor_standdown"] += 1
            self.journal.append("alarm", {"alarm": "executor_standdown", "reason": reason},
                                self.clock())

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
            self._capture_quote(a)
        elif k in (ActionKind.WOULD_CANCEL_REST, ActionKind.CANCEL_REST):
            rk = "would_cancel_rest" if k == ActionKind.WOULD_CANCEL_REST else "cancel_rest"
            payload = {"order_id": a.order_id, "client_order_id": a.client_order_id}
        elif k in (ActionKind.WOULD_AMEND_REST, ActionKind.AMEND_REST):
            rk = "would_amend_rest" if k == ActionKind.WOULD_AMEND_REST else "amend_rest"
            payload = {
                "order_id": a.order_id, "ticker": a.ticker, "side": a.side, "action": a.action,
                "count": a.count, "price": a.price, "client_order_id": a.client_order_id,
                "updated_client_order_id": a.updated_client_order_id,
            }
            self._capture_quote(a)
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
            # The T-5 end-of-quoting cancel (reason "past_quote_end") is orderly window close, not a
            # stand-down; record it distinctly and keep it out of the real stand-down tally.
            if a.reason == "past_quote_end":
                self._quote_end_cancel = True
            else:
                self._last_stand_down_reason = a.reason
                self._real_stand_downs += 1
        elif k == ActionKind.SHADOW_FILL_OUTSIDE_WINDOW:
            # A would-be shadow fill the live path could NOT have taken (print outside T-15..T-5).
            # Observability only: journal the suppressed fill; the generic tally below counts it under
            # ``shadow_fill_outside_window`` (the ledger surfaces it as ``shadow_fills_outside_window``).
            rk = "shadow_fill_outside_window"
            payload = {
                "E": a.shadow_E, "offer": a.offer, "print": a.print_price,
                "count": a.count, "t_to_close": a.t_to_close,
            }
        elif k == ActionKind.SHADOW_FILL_BELOW_MIN:
            # A would-be shadow fill at a solved n < n_min the live path would never have rested at
            # (it stands down n_below_min). Observability only: journal the suppressed fill; the tally
            # below counts it under ``shadow_fill_below_min`` (ledger: ``shadow_fills_below_min``).
            rk = "shadow_fill_below_min"
            payload = {
                "E": a.shadow_E, "offer": a.offer, "print": a.print_price,
                "count": a.count, "t_to_close": a.t_to_close,
            }
        else:
            rk = "v32_action"
            payload = {"kind": str(k)}
        self.counts[rk] += 1
        self.journal.append(rk, payload, self.clock())

    def _capture_quote(self, a) -> None:
        """Record the LAST bucket we quoted (rested on) WHILE quoting. Called on each place/would-place
        so the finalized row + summary carry the bucket we actually rested on (its ticker, Sd, Su), the
        last rest price and desired n, and every distinct spot bucket quoted in first-appearance order
        — none of which survive on the post-quote-end state (spot_Sd is nulled at close)."""
        st = self.state
        if a.ticker:
            self._last_quoted_bucket_ticker = a.ticker
        if st.spot_Sd is not None:
            self._last_quoted_Sd = st.spot_Sd
            self._last_quoted_Su = st.spot_Su
            if st.spot_Sd not in self._spot_buckets_quoted:
                self._spot_buckets_quoted.append(st.spot_Sd)
        if a.price is not None:
            self._last_rest_price = a.price
        if st.desired_n is not None:
            self._last_desired_n = st.desired_n

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
                 clock: Callable[[], float] = time.time,
                 m15_tickers: frozenset[str] | set[str] | None = None) -> None:
        self.journal = journal
        self.driver = driver
        self.clock = clock
        self.books: dict[str, BookMirror] = {}
        self.counts: dict[str, int] = defaultdict(int)
        # 15M (KXBTC15M) tickers subscribed on the bucket connection for RECORDING ONLY: their frames
        # are tapped + folded into a BookMirror like any other, but they are NEVER driven into the core
        # (a 15M ticker classifies as neither strike nor bucket, so it would only cause no-op recompute
        # churn). Counted here so the ledger/report can show the recorded 15M frame volume per window.
        self.m15_tickers: frozenset[str] = frozenset(m15_tickers or ())
        self.m15_frames = 0

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
        if market in self.m15_tickers:
            self.m15_frames += 1
            return  # recording only — fold the book, never drive the decision core
        self._drive_book(market, payload)

    def on_delta(self, market: str, payload: dict) -> None:
        self._book(market).apply_delta(payload)
        if market in self.m15_tickers:
            self.m15_frames += 1
            return  # recording only — fold the book, never drive the decision core
        self._drive_book(market, payload)

    def on_trade(self, market: str, payload: dict) -> None:
        if market in self.m15_tickers:
            self.m15_frames += 1
            return  # recording only — the 15M trade is tapped by the WS record hook, not decided on
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


class LagSampler:
    """Samples each connection's ``current_lag_seconds()`` on the clock-pump tick and aggregates it.

    WHY (2026-09-14 fix): ``KalshiWebSocketClient.force_close()`` sets ``last_delta_lag_seconds = None``
    at window end, so reading ``current_lag_seconds()`` AT FINALIZE (after the connections have closed)
    always yielded ``None`` — the ledger row and report showed ``strike/bucket_lag_seconds: null`` and
    "mean data-age n/a" even though ~1.35 M frames streamed. Sampling on the live 0.5 s tick captures a
    real distribution while the gauge is populated; the finalized row carries the MEAN (the per-window
    representative the report already reduces to a cross-window p99), and the summary carries mean/p99/
    last/n per connection. ``None`` readings (a dial before its first timestamped frame) are skipped."""

    def __init__(self, conns: Mapping[str, KalshiWebSocketClient]) -> None:
        self._conns = dict(conns)
        self._samples: dict[str, list[float]] = {k: [] for k in self._conns}

    def sample(self) -> None:
        """Read every connection's current lag once; append the non-None readings."""
        for key, ws in self._conns.items():
            v = ws.current_lag_seconds()
            if v is not None:
                self._samples[key].append(float(v))

    def summary(self, key: str) -> dict[str, float | int] | None:
        """{mean, p99 (nearest-rank), last, n} for one connection, or None if never sampled."""
        xs = self._samples.get(key) or []
        if not xs:
            return None
        srt = sorted(xs)
        n = len(srt)
        rank = min(n - 1, max(0, math.ceil(0.99 * n) - 1))
        return {"mean": sum(srt) / n, "p99": srt[rank], "last": xs[-1], "n": n}

    def mean(self, key: str) -> float | None:
        """The window mean lag for one connection (the ledger's per-window ``*_lag_seconds`` field)."""
        summ = self.summary(key)
        return summ["mean"] if summ is not None else None

    def summaries(self) -> dict[str, dict[str, float | int] | None]:
        return {key: self.summary(key) for key in self._conns}


async def _clock_pump(
    driver: V32Driver, clock: Callable[[], float], deadline: float,
    sleep: Callable[[float], Awaitable[None]], interval: float = PUMP_INTERVAL_S,
    lag_sampler: "LagSampler | None" = None,
) -> None:
    """Drive a ClockTick every ``interval`` seconds from the supervisor loop, using the driver's
    server-derived clock (last server ts + local elapsed). No tick before the first timestamped frame
    (fail-closed). Advances the window cutoffs / staleness even when book frames stop arriving. Also
    samples the per-connection data-age gauge each tick (``lag_sampler``) while the connections are
    still live, so the finalized row carries a real lag even though force_close() nulls the gauge."""
    while clock() < deadline:
        await sleep(interval)
        if clock() >= deadline:
            break
        if lag_sampler is not None:
            lag_sampler.sample()
        sn = driver.server_now()
        if sn is not None:
            driver.on_clock_tick(sn)


ORDER_POLL_INTERVAL_S = 1.0


async def _order_status_poll(
    driver: V32Driver, executor: Any, clock: Callable[[], float], deadline: float,
    sleep: Callable[[float], Awaitable[None]], interval: float = ORDER_POLL_INTERVAL_S,
) -> None:
    """Belt-and-braces: every ``interval`` s, GET the status of our live rest and book any fill the WS
    ``fill`` channel missed (de-duped by order_id in ``driver.on_poll_fill``). Armed only. Never raises
    out of the loop."""
    while clock() < deadline:
        await sleep(interval)
        if clock() >= deadline:
            break
        rest = driver.state.rest_live
        if rest is None or rest.order_id is None:
            continue
        try:
            st = executor.order_status(rest.order_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[V32] order-status poll error: %s", e)
            continue
        if st.available and st.filled_count > 0:
            driver.on_poll_fill(rest.order_id, int(st.filled_count),
                                driver.server_now() or clock())


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
    order_poll: bool = False,
    lag_sampler: "LagSampler | None" = None,
) -> None:
    """Await the connect gate, then run both dial loops + the ClockTick pump (and, when armed, the 1 s
    order-status poll) concurrently on one loop until the deadline. ``sleep`` is injected so tests drive
    the whole thing with a fake clock. ``lag_sampler`` (if given) records each connection's data-age on
    every pump tick while the sockets are live."""
    await _await_gate(gate_epoch, deadline, clock, sleep)
    tasks = [
        run_recording(strike_conn, deadline=deadline, sleep=sleep),
        run_recording(bucket_conn, deadline=deadline, sleep=sleep),
        _clock_pump(driver, clock, deadline, sleep, pump_interval, lag_sampler=lag_sampler),
    ]
    if order_poll:
        tasks.append(_order_status_poll(driver, driver.executor, clock, deadline, sleep))
    await asyncio.gather(*tasks)


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


def _batch_set_records(state: V32State) -> list[dict[str, Any]]:
    """One record per wing batch (PARTIAL-FILL WINGS, Brad 2026-09-18): a completed batch is one SET.

    Each record carries the batch's per-contract realized lock (``lock_value`` is already per contract,
    so per-set stats stay comparable to the size-1 history), completion, one_legged, and the legs held
    to settlement for that batch. At ``contracts`` = 1 there is exactly one batch and this mirrors the
    single-set row the pre-partial build produced."""
    out: list[dict[str, Any]] = []
    for b in getattr(state, "wing_batches", ()):  # type: ignore[attr-defined]
        legs = [lg for lg in state.wing_legs if lg.batch == b.index]
        completed = bool(legs) and all(lg.status == "filled" for lg in legs)
        w_paid = Decimal(0)
        held = 1  # the bucket-NO lot is always held once the rest fills
        for lg in legs:
            if lg.status == "filled":
                held += 1
                if lg.fill_price is not None:
                    w_paid += lg.fill_price + _fee(lg.fill_price)
        lock = lock_value(b.fill_price, w_paid) if completed else None
        out.append({
            "index": b.index,
            "fill_price": str(b.fill_price),
            "fill_count": int(b.fill_count),
            "completed": bool(completed),
            "one_legged": bool(b.one_legged),
            "realized_lock": (str(lock) if lock is not None else None),
            "held_legs": held,
        })
    return out


def _fee_total(price: Any, count: int) -> Decimal:
    """Venue per-FILL taker fee in dollars: ``ceil(0.07*p*(1-p)*count, $0.0001)`` -- the $0.0001
    ceiling applied ONCE to the whole fill, which is how Kalshi charges (MEMORY kalshi-fee-exact;
    169 live fills). This EQUALS the frozen per-contract law ``_fee(price)`` at ``count`` == 1, so a
    1-lot fill is unchanged; a size-N taker fill's true fee is THIS, never ``_fee(price) * N`` (the
    per-contract fee already rounded up once, so multiplying by N over-counts the rounding). Built on
    the SAME frozen ``_FEE_RATE`` as ``_fee`` -- no reimplementation of the coefficient."""
    p = price if isinstance(price, Decimal) else Decimal(str(price))
    raw = _FEE_RATE * p * (Decimal(1) - p) * Decimal(int(count)) * Decimal(10000)
    return Decimal(math.ceil(raw)) / Decimal(10000)


def _fill_total_fee(f: Mapping[str, Any]) -> tuple[Decimal, str]:
    """The (total_fee_dollars, fee_source) actually charged for ONE fill record.

    A fill record's ``fee`` key is PER CONTRACT (Kalshi's ``average_fee_paid``); the maker rest leg is
    fee-free on crypto (``fee`` 0). The TOTAL charged is:
      * ``fee`` 0            -> 0                      (maker leg; source "maker_zero")
      * taker, count 1       -> the per-contract ``fee`` itself IS the venue total (source
        "per_contract") -- keeps every pre-existing size-1 row byte-for-byte identical
      * taker, count >= 2    -> ``_fee_total(price, count)`` = the venue per-fill charge (source
        "law_total"); NOT ``fee * count`` (which under/over-counts the once-applied ceiling)."""
    per = f.get("fee")
    per_d = Decimal(str(per)) if per is not None else Decimal(0)
    if per_d == 0:
        return Decimal(0), "maker_zero"
    count = int(f.get("count", 0) or 0)
    if count <= 1:
        return per_d, "per_contract"
    return _fee_total(f.get("price", 0), count), "law_total"


def _annotate_fee(f: Mapping[str, Any]) -> dict[str, Any]:
    """A COPY of a fill record with two explicit, unambiguous fee keys added so a reader never has to
    guess the ``fee`` field's scale: ``fee`` stays PER CONTRACT (Kalshi ``average_fee_paid``; 0 on the
    maker rest leg), ``fee_total`` is the dollars actually charged for ALL lots of the fill (the venue
    per-fill ceiling; see ``_fill_total_fee``), and ``fee_source`` records how ``fee_total`` was
    derived (maker_zero / per_contract / law_total)."""
    total, src = _fill_total_fee(f)
    g = dict(f)
    g["fee_total"] = total
    g["fee_source"] = src
    return g


def _compute_money_math(state: V32State, executor: Any, contracts: int = 1) -> dict[str, Any]:
    """Fold the completed window's fills into the ledger's money-math slots. Returns the kwargs for
    ``build_v32_ledger_row`` (all empty/None when nothing filled — dry/shakedown windows). ``held_legs``
    are the bucket-NO + each FILLED wing leg (side/ticker/count), marked ``realized_unsettled`` so the
    settlement backfill sweep corrects the conservative floor booked here. ``floor_booked`` is the
    count-aware guaranteed floor actually netted into ``realized_delta`` (Σ per batch
    ``v32_set_floor_dollars(held_this, fill_count)``); the backfill nets THIS, never a count-blind $1.

    BUCKET-LEG DROP FIX (Fable 2026-09-20): ``state.spot_Sd`` is nulled at close, so
    ``bucket_tickers.get(spot_Sd)`` was None here and the bucket-NO leg was dropped from ``held``
    (backfill priced wings only, netted $1) — recovered from the rest fill record's own ticker.

    PARTIAL-FILL WINGS (Brad 2026-09-18): the money math is derived PER BATCH (each rest fill event is
    its own set), so a partial-fill window books one held bucket-NO + wings per fill and one per-contract
    realized lock per completed set. At ``contracts`` = 1 there is one batch and every existing key keeps
    its exact value (the hard acceptance test); the new keys (``rest_fills``, ``wing_batch_sets``,
    ``lots_filled``, ``lots_unfilled_at_quote_end``, ``partial_fills``) are additive.

    FEES (2026-09-20): the ``realized_delta`` cost sums the TOTAL fee per fill (venue per-fill
    ceiling ``ceil(0.07*p*(1-p)*count)`` via ``_fill_total_fee``), NOT the per-contract ``fee``
    added once -- the size-2 04:00Z set over-stated realized_delta by $0.0228 the old way. Every
    ``fills``/``wing_fills`` record now also carries ``fee_total`` + ``fee_source`` (``fee`` stays
    per contract). At ``count`` == 1 the total equals the per-contract fee, so size-1 rows are
    byte-identical."""
    # Operational counters ALWAYS travel to the row — even when nothing filled. The 2026-09-14 20:00Z
    # armed row read 0/0 rests despite 3 rejected creates because the no-fill early-return below dropped
    # these; they now ride both exits. Cancel/venue-truth counters added with the shard fix.
    counters = {
        "rests_placed": int(getattr(executor, "rests_placed", 0)),
        "rests_rejected": int(getattr(executor, "rests_rejected", 0)),
        "wing_batches": int(getattr(executor, "wing_batches", 0)),
        "exec_price_mismatches": list(getattr(executor, "exec_price_mismatches", []) or []),
        "cancels_attempted": int(getattr(executor, "cancels_attempted", 0)),
        "cancels_confirmed": int(getattr(executor, "cancels_confirmed", 0)),
        "cancel_404s": int(getattr(executor, "cancel_404s", 0)),
        "cancels_via_status": int(getattr(executor, "cancels_via_status", 0)),
        "cancels_expired": int(getattr(executor, "cancels_expired", 0)),
        "rest_invariant_violations": int(getattr(executor, "rest_invariant_violations", 0)),
        "rest_invariant_phantoms": int(getattr(executor, "rest_invariant_phantoms", 0)),
        "rest_invariant_rechecks": int(getattr(executor, "rest_invariant_rechecks", 0)),
        # amend-first replace counters (Brad 2026-09-15)
        "amends_attempted": int(getattr(executor, "amends_attempted", 0)),
        "amends_confirmed": int(getattr(executor, "amends_confirmed", 0)),
        "amends_failed": int(getattr(executor, "amends_failed", 0)),
        "amend_fallbacks": int(getattr(executor, "amend_fallbacks", 0)),
        "fills_on_amend": int(getattr(executor, "fills_on_amend", 0)),
    }
    fills = list(getattr(executor, "fills", []) or [])
    if not fills or state.rest_fill is None:
        return {"fills": fills, **counters}
    rest_fills_mm = [_annotate_fee(f) for f in fills if f.get("leg") == "rest"]
    wing_fills = [_annotate_fee(f) for f in fills if f.get("leg") == "wing"]
    rest_records = [f for f in fills if f.get("leg") == "rest"]
    # BUCKET-LEG DROP FIX (Fable 2026-09-20): the reset-at-close nulls ``state.spot_Sd``, so
    # ``bucket_tickers.get(spot_Sd)`` returns None at _finalize time and the guaranteed bucket-NO leg
    # was silently dropped from ``held``/``unsettled_legs`` (the backfill then priced only the wings
    # and netted a count-blind $1 floor -> every complete set's backfill correction was wrong by
    # $1 x contracts). Recover the bucket-NO ticker from the REST FILL RECORD itself, which captured
    # ``ticker`` at fill time (the 04:00Z row carries KXBTC-26SEP2000-B80450); a single fallback for a
    # batch we can't otherwise place.
    fallback_bt = state.bucket_tickers.get(state.spot_Sd) if state.spot_Sd is not None else None
    if fallback_bt is None:
        for rec in rest_records:
            if rec.get("ticker"):
                fallback_bt = rec["ticker"]
                break
        if fallback_bt is None and getattr(state, "rest_bucket_Sd", None) is not None:
            fallback_bt = state.bucket_tickers.get(state.rest_bucket_Sd)

    # PER-BATCH bucket ticker (Round-2 fix, reviewer PR #74 FINDING #1): a mid-hour bucket change
    # rests+fills on TWO different buckets in one hour (partial fill on A, spot crosses, core cancels A
    # and places a NEW order on B, B fills -> two batches on two buckets). The old single recovered
    # ticker mislabeled batch 1's bucket-NO leg with bucket A's ticker -> an over-credit if A settled
    # ``no`` (mislabeled leg "wins" $1 the true B leg loses). The bucket change places a DISTINCT order
    # (distinct order_id -> distinct rest-fill record), so we walk the rest-fill records in order and
    # consume each record's lot count as we assign batches (rest fills spawn batches 1:1 in order). If
    # the records are exhausted (the executor's order-id dedup collapsed SAME-ORDER refills into one
    # record -- necessarily the SAME bucket), the remaining batches fall back to ``fallback_bt``.
    # Single-bucket windows and contracts=1 are byte-identical (every record carries the one ticker).
    batch_bt: dict[int, str | None] = {}
    _ri = 0
    _rem = int(rest_records[0].get("count", 0) or 0) if rest_records else 0
    for b in getattr(state, "wing_batches", ()):  # type: ignore[attr-defined]
        while _ri < len(rest_records) and _rem <= 0:
            _ri += 1
            _rem = int(rest_records[_ri].get("count", 0) or 0) if _ri < len(rest_records) else 0
        if _ri < len(rest_records):
            rec = rest_records[_ri]
            tk = rec.get("ticker")
            if not tk and rec.get("bucket_Sd") is not None:
                tk = state.bucket_tickers.get(rec["bucket_Sd"])
            batch_bt[b.index] = tk or fallback_bt
            _rem -= int(b.fill_count)
        else:
            batch_bt[b.index] = fallback_bt

    # Per-batch held legs, floor and first-completed-set lock (PARTIAL-FILL WINGS). At contracts=1 the
    # single batch reproduces the pre-partial held list, floor and realized_lock exactly.
    held: list[dict[str, Any]] = []
    floor = Decimal(0)
    realized_lock: Decimal | None = None
    for b in getattr(state, "wing_batches", ()):  # type: ignore[attr-defined]
        legs = [lg for lg in state.wing_legs if lg.batch == b.index]
        # The bucket-NO lot is held the moment the rest fills; list it (with the batch's fill_count and
        # THIS batch's own bucket ticker) and count it into this batch's floor. ``held_this`` counts
        # ONLY legs actually listed, so an (essentially impossible) unrecoverable bucket ticker fails
        # closed to the wings alone rather than claiming a floor for a leg it cannot name.
        bt = batch_bt.get(b.index)
        held_this = 0
        if bt:
            held_this += 1
            held.append({"ticker": bt, "side": "no", "count": int(b.fill_count)})
        w_paid = Decimal(0)
        completed = bool(legs) and all(lg.status == "filled" for lg in legs)
        for lg in legs:
            if lg.status == "filled":
                held_this += 1
                held.append({"ticker": lg.ticker, "side": lg.side, "count": int(lg.count)})
                if lg.fill_price is not None:
                    w_paid += lg.fill_price + _fee(lg.fill_price)
        floor += v32_set_floor_dollars(held_this, int(b.fill_count))
        if completed and realized_lock is None:
            realized_lock = lock_value(b.fill_price, w_paid)
    # conservative realized at close = floor guaranteed by the held legs − cash actually paid.
    cost = Decimal(0)
    for f in fills:
        try:
            cost += Decimal(str(f.get("price", 0))) * Decimal(int(f.get("count", 0)))
            # TOTAL fee for ALL lots of this fill. The venue charges ceil(0.07*p*(1-p)*count)
            # ONCE per fill, so the per-contract ``fee`` must be scaled by count (not added
            # once). Adding it once dropped every lot past the first -- the 2026-09-20 04:00Z
            # size-2 set over-stated realized_delta by $0.0228 (the 2nd lot's wing fees).
            cost += _fill_total_fee(f)[0]
        except (ArithmeticError, ValueError, TypeError):
            continue
    realized_delta = floor - cost
    rest_fills_events = [
        {"price": str(rf.price), "count": int(rf.count), "server_ts": rf.server_ts}
        for rf in getattr(state, "rest_fills", ())
    ]
    lots_filled = sum(int(rf.count) for rf in getattr(state, "rest_fills", ()))
    lots_unfilled = max(0, int(contracts) - lots_filled)
    return {
        "fills": rest_fills_mm,
        "wing_fills": wing_fills,
        "held_legs": held,
        "realized_lock": realized_lock,
        "one_legged": bool(state.one_legged),
        "realized_unsettled": bool(held),
        "realized_delta": realized_delta,
        # count-aware floor actually netted into realized_delta (Σ per batch
        # v32_set_floor_dollars(held_this, fill_count)); the backfill nets THIS exact floor.
        "floor_booked": floor,
        # PARTIAL-FILL WINGS additive slots
        "rest_fills": rest_fills_events,
        "wing_batch_sets": _batch_set_records(state),
        "lots_filled": int(lots_filled),
        "lots_unfilled_at_quote_end": int(lots_unfilled),
        "partial_fills": int(getattr(state, "partial_fills", 0)),
        **counters,
    }


def _finalize(
    *, journal: StreamJournal, shared: V32Recorder, driver: V32Driver, close_iso: str,
    resolved_mode: str, effective_mode: str, degrade: str | None, params: V32Params,
    strike_disc: StrikeDiscovery, bucket_map: dict[str, tuple[float, float]],
    bucket_generations: int, journal_path: str, summary_path: str, ledger_path: str,
    strike_lag: float | None, bucket_lag: float | None, clock: Callable[[], float],
    m15_tickers: list[str] | None = None, lag_stats: dict | None = None,
    armed: bool = False, degrade_reason: str | None = None,
) -> dict:
    journal.close()
    gz = _gzip_journal(journal_path)
    final_path = os.path.abspath(gz.get("final_path") or journal_path)
    money = _compute_money_math(driver.state, driver.executor, contracts=params.contracts)
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
        m15_tickers=list(m15_tickers or []),
        m15_frames=shared.m15_frames,
        armed=armed,
        degrade_reason=degrade_reason,
        # last-quoted spot bucket + stand-down bookkeeping (captured while quoting)
        last_quoted_bucket_ticker=driver._last_quoted_bucket_ticker,
        last_quoted_Sd=driver._last_quoted_Sd,
        last_quoted_Su=driver._last_quoted_Su,
        last_rest_price=driver._last_rest_price,
        last_desired_n=driver._last_desired_n,
        spot_buckets_quoted=list(driver._spot_buckets_quoted),
        quote_end_cancel=driver._quote_end_cancel,
        real_stand_downs=driver._real_stand_downs,
        last_stand_down_reason=driver._last_stand_down_reason,
        lag_stats=lag_stats,
        **money,
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
        # last-quoted spot bucket + rest, captured while quoting (mirror the ledger row)
        "spot_bucket_ticker": driver._last_quoted_bucket_ticker,
        "Sd": driver._last_quoted_Sd,
        "Su": driver._last_quoted_Su,
        "last_rest_price": (str(driver._last_rest_price)
                            if driver._last_rest_price is not None else None),
        "last_desired_n": (str(driver._last_desired_n)
                           if driver._last_desired_n is not None else None),
        "spot_buckets_quoted": list(driver._spot_buckets_quoted),
        "quote_end_cancel": driver._quote_end_cancel,
        "stand_downs": driver._real_stand_downs,
        "stand_down_reason": driver._last_stand_down_reason,
        "m15_tickers": list(m15_tickers or []),
        "m15_frames": shared.m15_frames,
        "ws_counts": dict(shared.counts),
        "gzipped": bool(gz.get("gzipped")),
        "strike_lag_seconds": strike_lag,
        "bucket_lag_seconds": bucket_lag,
        "lag_stats": lag_stats or {},
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
        description="V3.2 pump-fader window process (shakedown/dry/armed; armed degrades to dry unless "
                    "S5 + reconcile-first + day-latch + S4 all pass)."
    )
    parser.add_argument("--close", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--mode", default=None, choices=list(VALID_MODES_V32),
                        help="Override the mode (else read ops/v32_mode.txt; unknown -> shakedown).")
    # Resolve writable-path defaults at parse time so DV3_DATA_DIR set by the supervisor/host is
    # honoured; unset -> the historic _PILOT_DIR-relative default (behaviour-neutral).
    parser.add_argument("--journal-dir", default=journal_dir_v32())
    parser.add_argument("--log-dir", default=log_dir_v32())
    parser.add_argument("--ledger", default=ledger_path_v32())
    parser.add_argument("--mode-file", default=mode_path_v32())
    parser.add_argument("--falsifier", default=DEFAULT_FALSIFIER_PATH,
                        help="V3.2 falsifier (S5: must carry STATUS: FROZEN to arm).")
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    parser.add_argument("--flush-every", type=int, default=200,
                        help="Flush the write-through journal to the OS every N frames (default 200).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    # --proxy-base wins; else DV3_PROXY_BASE; else http://127.0.0.1:8642 (unchanged default).
    proxy_base_url = args.proxy_base or default_proxy_base()
    proxy = ProxyAuth(base_url=proxy_base_url)
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

    # prepare(): settlement backfill for prior windows still realized_unsettled whose held tickers have
    # settled (GET /markets/{ticker} exact match). Fail-closed / idempotent (never raises out of start).
    try:
        prior_rows = load_v32_rows(args.ledger)
        backfills = v32_settlement_backfill_sweep(
            prior_rows, lambda tk: fetch_market_result_v32(proxy, tk), clock()
        )
        for bf in backfills:
            append_v32_ledger_row(bf, args.ledger)
        if backfills:
            logger.info("[V32] settlement backfill appended %d row(s)", len(backfills))
    except Exception as e:  # noqa: BLE001
        logger.warning("[V32] settlement backfill sweep failed: %s", e)

    effective_mode = resolved_mode  # armed may DEGRADE below once /health + positions are read
    degrade: str | None = None
    degrade_reason: str | None = None

    # Discovery.
    strike_disc = discover_strike_ladder(proxy, close_iso, clock())
    range_disc = discover_range_markets(proxy, close_iso, clock())
    # Co-settling KXBTC15M market(s), RECORDING ONLY (this process is the single tape recorder so the
    # disabled v1.1 pilot leaves no 15M data gap). Absence is NOT a stand-down (journaled below), and a
    # discovery FAILURE (proxy 5xx) is likewise non-fatal: recording-only data must never cost a viable
    # trading window, so it degrades to an empty discovery + a journaled m15_discovery_error.
    m15_disc, m15_error = discover_co_settling_15m_safe(proxy, close_iso, clock())
    if m15_error is not None:
        logger.warning("[V32] 15M discovery failed (%s) — recording-only, window continues", m15_error)
    bucket_map = build_bucket_map(range_disc)
    # P3-2: drop any bucket whose width != params.bucket_width so a mixed-width hour cannot select a
    # wrong-width spot bucket and break the $2 pin.
    bucket_map, dropped_width = filter_buckets_to_width(bucket_map, params.bucket_width)

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
            "m15_series": FIFTEEN_SERIES,
            "m15_tickers": list(m15_disc.tickers),
        },
        clock(),
    )
    # Record the co-settling 15M leg (recording only). Absence is not a stand-down.
    if m15_disc.tickers:
        journal.append("m15_recording", {"tickers": list(m15_disc.tickers)}, clock())
        logger.info("[V32] recording co-settling 15M: %s", ", ".join(m15_disc.tickers))
    elif m15_error is not None:
        journal.append("m15_discovery_error",
                       {"series": FIFTEEN_SERIES, "close_time": close_iso, "error": m15_error},
                       clock())
        logger.info("[V32] 15M discovery error journaled (recording continues): %s", m15_error)
    else:
        journal.append("m15_missing", {"series": FIFTEEN_SERIES, "close_time": close_iso}, clock())
        logger.info("[V32] no co-settling %s market at %s (recording continues)",
                    FIFTEEN_SERIES, close_iso)
    # --- arming resolution (S5 + reconcile-first + day latch + S4), the ONE arm-or-degrade gate ---
    writer: ProxyWriter | None = None
    if resolved_mode == "armed":
        writer = ProxyWriter(proxy_auth=proxy, base_url=proxy_base_url)
        health = get_health(proxy_base_url)
        try:
            positions = proxy.rest_get("/portfolio/positions")
        except Exception as e:  # noqa: BLE001
            positions = None
            logger.warning("[V32] positions read failed: %s", e)
        utc_day = close_iso[:10]
        guard_path = _resolve_v32_guard_path(utc_day)
        day_guard = read_day_guard(guard_path, utc_day)
        s4 = None
        try:
            bal = parse_balance(proxy.rest_get("/portfolio/balance"))
            if bal.ok and not day_guard.corrupt:
                start, _first = ensure_balance_start(guard_path, utc_day, bal.dollars, clock())
                if start is not None:
                    pending = v32_pending_credit(load_v32_rows(args.ledger), utc_day)
                    s4 = v32_s4_decision(start, bal.dollars, pending)
        except Exception as e:  # noqa: BLE001
            logger.warning("[V32] balance/S4 read failed: %s", e)
        outcome = decide_v32_arming(
            resolved_mode=resolved_mode, falsifier_path=args.falsifier, health=health,
            positions=positions, params_verified=True, contracts=params.contracts,
            day_guard=day_guard, s4=s4,
        )
        effective_mode = outcome.effective_mode
        if not outcome.armed:
            degrade = "degrade_to_dry"
            degrade_reason = "; ".join(outcome.reasons)
            journal.append("degrade_to_dry",
                           {"reason": degrade_reason, "from_mode": resolved_mode,
                            "reasons": list(outcome.reasons)}, clock())
            logger.warning("[V32] ARMED refused -> dry: %s", degrade_reason)

    armed = effective_mode == "armed"
    shakedown = not armed              # dry/shakedown -> WOULD_* twins; armed -> real orders
    include_private = armed            # private fill/market_positions channel only when armed
    if dropped_width:
        journal.append("buckets_dropped_off_width",
                       {"count": len(dropped_width), "width": params.bucket_width}, clock())

    cts = close_epoch(close_iso)
    state = V32State.new(close_iso, cts, bucket_map, params, shakedown=shakedown)

    # exchange_index map (strikes + buckets) for the LiveExecutor's per-leg routing.
    exch_map: dict[str, int | None] = dict(strike_disc.exchange_index_by_ticker)
    for b in range_disc.buckets:
        if getattr(b, "ticker", None):
            exch_map[b.ticker] = coerce_exchange_index(getattr(b, "exchange_index", None))

    # P3-1: the executor is selected by effective_mode in EXACTLY ONE place.
    executor = build_executor(
        effective_mode, bucket_map=bucket_map, exchange_index_by_ticker=exch_map,
        journal=journal, close_epoch_val=cts, params=params, writer=writer, clock=clock,
    )
    driver = V32Driver(params, state, journal, executor, clock=clock)
    shared = V32Recorder(journal, driver, clock=clock, m15_tickers=frozenset(m15_disc.tickers))

    # Startup safety (armed): cancel any of OUR resting KXBTC* orders a prior crash left behind.
    if armed and writer is not None:
        try:
            cancel_stale_open_orders(writer, journal, clock)
        except Exception as e:  # noqa: BLE001
            logger.warning("[V32] startup open-order cancel failed: %s", e)

    strike_ws = KalshiWebSocketClient(
        proxy_auth=proxy, tickers=list(strike_disc.tickers), callbacks=shared.callbacks(False),
        include_private=False, record=shared.tap, clock=clock, channels=STRIKE_CHANNELS,
    )
    # The bucket connection also carries the recording-only 15M ticker(s) (orderbook_delta + trade,
    # the same public channels — no private/ticker channel added for them).
    bucket_sub_tickers = sorted(set(bucket_map) | set(m15_disc.tickers))
    bucket_ws = KalshiWebSocketClient(
        proxy_auth=proxy, tickers=bucket_sub_tickers, callbacks=shared.callbacks(include_private),
        include_private=include_private, record=shared.tap, clock=clock, channels=BUCKET_CHANNELS,
    )
    strike_conn = _ConnRecorder(shared, strike_ws, "strikes", list(strike_disc.tickers))
    bucket_conn = _ConnRecorder(shared, bucket_ws, "buckets", bucket_sub_tickers)

    deadline = cts + GRACE_SECONDS
    gate = connect_gate_epoch(cts, params)
    lag_sampler = LagSampler({"strikes": strike_ws, "buckets": bucket_ws})
    try:
        asyncio.run(run_v32_window(shared, strike_conn, bucket_conn, driver, clock, deadline, gate,
                                   order_poll=armed, lag_sampler=lag_sampler))
    except KeyboardInterrupt:
        logger.warning("[V32] Ctrl+C — flushing streamed journal.")
    finally:
        # S1_LEGGED: a completed set left one-legged below the floor at T-1 s. One occurrence stood the
        # hour down (core); the DAY latches at the threshold (recorded in the SEPARATE v32 day guard).
        if armed and driver.state.one_legged:
            try:
                utc_day = close_iso[:10]
                n = record_legged_occurrence(_resolve_v32_guard_path(utc_day), utc_day,
                                             close_iso, "set left one-legged below lock floor",
                                             clock())
                journal.append("s1_legged_occurrence",
                               {"count": n, "latch_threshold": V32_S1_LEGGED_LATCH_THRESHOLD},
                               clock())
            except Exception as e:  # noqa: BLE001
                logger.warning("[V32] S1_LEGGED record failed: %s", e)
        summary = _finalize(
            journal=journal, shared=shared, driver=driver, close_iso=close_iso,
            resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
            params=params, strike_disc=strike_disc, bucket_map=bucket_map,
            bucket_generations=range_disc.generations, journal_path=journal_path,
            summary_path=summary_path, ledger_path=args.ledger,
            strike_lag=lag_sampler.mean("strikes"), bucket_lag=lag_sampler.mean("buckets"),
            lag_stats=lag_sampler.summaries(),
            clock=clock, m15_tickers=list(m15_disc.tickers), armed=armed,
            degrade_reason=degrade_reason,
        )
        logger.info("[V32] window done: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
