"""run_window.py — one process == one window lifecycle (Phase 4 service harness).

Task Scheduler wakes this at :40 each hour; the process discovers the co-settling leg pair for the
NEXT top-of-hour close, runs the window, journals, writes a pilot-ledger summary, and exits. Crash
isolation is by construction: no state survives the process, and reconcile-first at startup makes
the exchange — never our memory — the authority on whether we hold anything.

STARTUP ORDER (HARD LAW — the sequence never varies):
  (a) load policy (sha check). A sha mismatch or unreadable policy -> stand down (fail closed).
  (b) arming checks (S5) — ARMED mode only: falsifier STATUS line == 'STATUS: FROZEN', proxy /health
      caps + orders_enabled, policy sha verified. ANY failure -> DEGRADE to dry with a LOUD journal
      record (never arm). DRY mode runs the same check for observability but never arms; SHAKEDOWN
      skips it.
  (c) RECONCILE-FIRST: GET positions via the proxy. ANY existing position in our two series
      (KXBTC15M / KXBTCD) -> journal + notify + REFUSE to trade this window (stand down). Flatten is
      NOT automatic — pending Brad's F4/F8 ruling. A positions-read FAILURE is fatal for ARMED (we
      cannot confirm we are flat -> stand down); dry/shakedown log an alarm and continue (no orders).
  (d) WakeContext discovery. Missing/closed/inactive leg -> clean StandDown, exit 0.
  (e) PHASE A prefetch: the σ̂ trailing-anchor tape (T-900..T-7200) — everything ANCHOR-INDEPENDENT.
      NO quintile/pairing is computed here (live finding 2026-08-21: the 15M leg is "initialized"
      with NO floor_strike at :40; the anchor A(T) materializes only when it flips "active" at
      open_time, ~:45). Steps (a)-(e) constitute PHASE A and are all done at the :40 wake.
  (f) PHASE B (execute, at leg open): after the connect gate, POLL the 15M market via REST until
      floor_strike is present (bounded; timeout open_time+45s). On the strike: compute anchor A(T) ->
      pair the hourly leg (census law: nearest hourly threshold, both sides) -> G -> g/σ̂ -> quintile
      -> strangle_disabled, then connect the WS via ProxyAuth, run BookMirror + Journal, and drive
      signal.decide on each paired-leg book event. NoQuintile with a resolvable pair -> strangle
      stands down, sub-$1 continues; NoQuintile with no pair -> whole-window stand down; strike never
      arrives before the poll timeout -> EXCL_NO_ANCHOR stand-down. ARMED routes FIRE -> the Executor
      (with Reconciler + Stops wired). dry/shakedown journal WouldFire only (shakedown composes
      ShakedownRecorder; dry uses LiveWindowRecorder with a FROZEN executor — armed mode is NEVER
      routed through ShakedownRecorder).
  (g) at close + grace: flush the journal to pilot/journals/<close>.jsonl and append a pilot-ledger
      summary. exit 0.

ROBUSTNESS: any unhandled exception -> freeze orders, flush what is buffered, journal the traceback,
exit nonzero. Proxy-down/unsigned on the WS dial -> bounded re-dial with exponential backoff, capped
consecutive attempts, clean give-up at window end (Phase-1 review F6).

House law: never reads .env/*.pem, never sets ALLOW_ORDERS (the proxy owns it; this process only
sets the independent client-side arm and only when the S5 gate passes), all REST via the proxy.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from service.book import TopOfBook
from service.box import (
    BOX_RESCAN_WOULD_FIRE,
    BOX_SKIP,
    WIDE_BOX,
    BoxPolicyShaMismatch,
    BoxState,
    DEFAULT_BOX_POLICY_PATH,
    load_box_policy,
)
from service.box_runner import BoxWindowRecorder
from service.executor import Executor, ExecutorConfig
from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_FLATTEN,
    PURPOSE_REBALANCE_BUY,
    PURPOSE_REBALANCE_SELL,
    Intent,
    IntentLeg,
    LedgerState,
    fills_record,
    new_ledger,
    record_intent,
    record_response,
)
from service.orders.envelope import new_client_order_id
from service.pilot_ledger import (
    A5_ONE_LEGGED_RATE_MAX,
    DEFAULT_LEDGER_PATH,
    already_backfilled,
    append_entry,
    box_one_legged_rate,
    build_backfill_entry,
    load_entries,
    s4_running_loss,
)
from service.policy import (
    DEFAULT_POLICY_PATH,
    Q1_STRANGLE,
    SUB_DOLLAR_FLIP,
    PolicyParams,
    PolicyShaMismatch,
    load_policy,
)
from service.proxy_auth import ProxyAuth
from service.record_window import (
    CONTINUE,
    DEADLINE,
    DEFAULT_JOURNAL_DIR,
    DEFAULT_LAG_THRESHOLD,
    DEFAULT_POLL_SECONDS,
    DEFAULT_SILENCE_THRESHOLD,
    FORCE_CLOSE,
    GRACE_SECONDS,
    WindowRecorder,
    next_top_of_hour_iso,
    watchdog_action,
)
from service.reconciler import (
    Balanced,
    GiveUp,
    PositionsReconciler,
    RebalanceQuotes,
    RetryBuy,
    RideToSettlement,
    SellDown,
    detect_imbalance,
    parse_positions_response,
    propose_rebalance,
)
from service.quintile import _anchor_at
from service.shakedown import ShakedownRecorder, SignalDriver
from service.signal import FIRE, WOULD_FIRE, Action, WindowState
from service.wake import StandDown, WakeContext, WakeResult, close_epoch
from service.ws_client import KalshiWebSocketClient, _parse_server_ts

logger = logging.getLogger(__name__)

_PILOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_FALSIFIER_PATH = os.path.join(_PILOT_DIR, "ceremony", "falsifier.md")
# F2 (Phase-4): the box arms against ITS OWN ceremonial falsifier, never the corridor's. S5 checks
# THIS file's STATUS line when strategy == box; the corridor keeps checking falsifier.md.
DEFAULT_BOX_FALSIFIER_PATH = os.path.join(_PILOT_DIR, "ceremony", "box_falsifier.md")
DEFAULT_MODE_TXT = os.path.join(_PILOT_DIR, "ops", "mode.txt")
DEFAULT_LOG_DIR = os.path.join(_PILOT_DIR, "logs")

VALID_MODES = ("shakedown", "dry", "armed")
TICKER_PREFIXES = ("KXBTC15M", "KXBTCD")

# Strategy selection lever (spec item 1). ops/strategy.txt is read fresh each wake, like mode.txt;
# --strategy overrides, --strategy-file points elsewhere. An unknown/missing value fails CLOSED: run
# the corridor decision core, DRY (never armed). The file ships set to "corridor" (Brad flips it to
# "box" at Phase 4 alongside mode.txt).
VALID_STRATEGIES = ("corridor", "box")
DEFAULT_STRATEGY_TXT = os.path.join(_PILOT_DIR, "ops", "strategy.txt")

A5_ONE_LEGGED = "A5"  # box one-legged-entry-rate alarm (notify + journal, keep running)

# F4: the one-legged flatten is bounded at 3 attempts total (box_falsifier.md: "up to 3 attempts at
# fresh bids"). Max orders one box window can emit = 2 entry legs + 3 flatten = 5. Retries are
# EVENT-DRIVEN: the next attempt is issued on the next later book event for the held ticker whose bid
# is present, OR on the next later event once >= _FLATTEN_RETRY_MIN_ELAPSED_S of ENGINE time has
# passed since the last attempt (whichever comes first). Engine time = event server_ts, never wall
# clock, so this stays replay-deterministic and can never price a stale frozen book (the review's F4).
_FLATTEN_MAX_ATTEMPTS = 3
_FLATTEN_RETRY_MIN_ELAPSED_S = 0.250
A_FLATTEN_EXHAUSTED = "A_FLATTEN_EXHAUSTED"  # all flatten attempts missed -> held naked, alarm

# The pilot is capped at DUAL size; sizing to 5 requires a new commission (falsifier/commission).
# The proxy's per-order cap must not exceed this ceiling and must not be looser than the executor's.
PILOT_MAX_CONTRACTS_CEILING = 2
# Effective "no client budget" for a stop-authorized flatten (F8): risk reduction is never starved by
# the client token budget; the proxy's persisted daily budget remains the real cap.
_FLATTEN_EXEMPT_BUDGET = 10 ** 9

# Re-dial backoff (F6): bounded, exponential, capped, gives up cleanly at the deadline.
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_MAX_CONSECUTIVE_REDIALS = 8
# Connect-gate (live finding 2026-08-21): the co-settling 15M leg is listed "initialized" at the :40
# wake and flips to "active" EXACTLY at open_time (:45; observed 15:45:07 for a 15:45:00 open). We
# DISCOVER + arm at :40 (leg selection is status-agnostic in wake.py) but hold the WS dial until just
# before the 15M leg opens so we never subscribe a not-yet-open market — subscription harmlessness on
# an "initialized" market is a live-unknown, so we fail closed to WAITING rather than dialing early.
# The hourly leg is already active at :40, but a few minutes of pre-open hourly book adds nothing to a
# near-settlement decision, so the wait costs nothing the strategy needs. Bounded by the deadline; a
# leg already open at wake yields no gate (dial immediately).
_CONNECT_MARGIN_S = 5.0
# Anchor poll (PHASE B, live finding 2026-08-21): the co-settling 15M market is "initialized" with
# NO floor_strike at the :40 wake; the strike (= BTC spot at window open, a NON-round number, e.g.
# 77315.17) materializes only when the market flips "active" at open_time (observed status active +
# floor_strike appearing together at ~:45:13 for a :45:00 open). So NOTHING anchor-dependent can be
# computed at :40 — the hourly-leg pairing (census law: nearest hourly threshold to the anchor), G,
# g/σ̂, and the quintile all wait for the strike. run_window PREFETCHES everything else at :40
# (phase A) and, at leg open, polls the 15M market via REST every ANCHOR_POLL_INTERVAL_S until the
# strike is present, giving up at open_time + ANCHOR_POLL_TIMEOUT_S (then EXCL_NO_ANCHOR is a
# LEGITIMATE stand-down). Bounded by the window deadline.
ANCHOR_POLL_INTERVAL_S = 2.0
ANCHOR_POLL_TIMEOUT_S = 45.0
# Imbalance-protocol iteration cap per window (each step is bounded by the falsifier's retry budget
# via retries_for_side; this is a belt-and-suspenders loop guard).
_MAX_REBALANCE_STEPS = 20


# ---------------------------------------------------------------------------
# Mode resolution (config-file driven so Brad flips modes WITHOUT re-registering)
# ---------------------------------------------------------------------------
def read_mode_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve_mode(cli_mode: str | None, mode_txt_path: str) -> str:
    """The effective mode. CLI --mode wins; else read mode.txt. Unknown/absent -> 'shakedown'
    (fail-closed: the safest rung is the no-orders one)."""
    raw = cli_mode if cli_mode else read_mode_file(mode_txt_path)
    m = (raw or "").strip().lower()
    return m if m in VALID_MODES else "shakedown"


def resolve_strategy(cli_strategy: str | None, strategy_txt_path: str) -> tuple[str, bool]:
    """The effective strategy + a validity flag. CLI --strategy wins; else read strategy.txt.

    Returns ``(strategy, valid)``. A known value -> ``(value, True)``. An unknown/absent value ->
    ``("corridor", False)``: the caller then runs the corridor decision core DRY (never armed), and
    journals ``strategy_invalid`` (spec item 1 fail-closed)."""
    raw = cli_strategy if cli_strategy else read_mode_file(strategy_txt_path)
    s = (raw or "").strip().lower()
    if s in VALID_STRATEGIES:
        return s, True
    return "corridor", False


# ---------------------------------------------------------------------------
# Thread-safe journal wrapper (armed mode appends from the WS thread AND the reconciler thread;
# Phase-1 Journal is single-thread by construction — confession #11. This serializes appends.)
# ---------------------------------------------------------------------------
class ThreadSafeJournal:
    """Drop-in wrapper serializing append/flush/len/records under one RLock."""

    def __init__(self, journal: Journal | None = None) -> None:
        self._j = journal or Journal()
        self._lock = threading.RLock()

    def append(self, kind: str, obj: Any, local_ts: float) -> int:
        with self._lock:
            return self._j.append(kind, obj, local_ts)

    def flush(self, path: str) -> int:
        with self._lock:
            return self._j.flush(path)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._j.records()

    def iter_records(self):
        return iter(self.records())

    def __len__(self) -> int:
        with self._lock:
            return len(self._j)


# ---------------------------------------------------------------------------
# Live recorder for dry + armed (NOT ShakedownRecorder — armed is never routed through it).
# ---------------------------------------------------------------------------
class LiveWindowRecorder(WindowRecorder):
    """WindowRecorder + a SignalDriver on the two paired legs, with an ``on_action`` hook.

    In DRY the seeded state has ``shakedown=True`` so decide() only ever emits WouldFire and the
    hook is a no-op beyond the driver's own journaling; the executor (if any) stays frozen. In ARMED
    the state has ``shakedown=False`` so decide() emits FIRE and the hook routes it to the Executor.
    """

    def __init__(
        self,
        wake_result: WakeResult,
        journal: Any,
        params: PolicyParams,
        state: WindowState,
        clock: Callable[[], float] = time.time,
        capture_tops: bool = True,
        on_action: Callable[[Action], None] | None = None,
    ) -> None:
        super().__init__(wake_result, journal, clock=clock, capture_tops=capture_tops)
        self.driver = SignalDriver(params, state, journal, clock=clock, on_action=on_action)

    def _drive(self, market: str, payload: dict) -> None:
        if market not in (self.driver.state.high_ticker, self.driver.state.low_ticker):
            return
        book = self.books.get(market)
        if book is None:
            return
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            # F5: a frame carrying NO server timestamp must NOT drive a decision. Falling back to the
            # machine clock would make the leg read FRESH (age ~= 0) and let decide() evaluate a FIRE
            # on machine time under clock skew — the opposite of the fail-closed freshness law. The
            # book fold already happened in the super() _on_snapshot/_on_delta call; here we journal
            # the drop and SKIP the decision core (the ts-less frame never reaches decide()).
            self.journal.append(
                "ws_frame_no_server_ts",
                {"market": market,
                 "note": "no server ts -> book folded, decide() NOT driven (F5 fail-closed)"},
                self.clock(),
            )
            return
        self.driver.on_book_update(market, book.top_of_book(), server_ts)

    def _on_snapshot(self, market: str, payload: dict) -> None:
        super()._on_snapshot(market, payload)
        self._drive(market, payload)

    def _on_delta(self, market: str, payload: dict) -> None:
        super()._on_delta(market, payload)
        self._drive(market, payload)


# ---------------------------------------------------------------------------
# Harness executor — binds two interlocks the Phase-3 review (F7/F8) flagged as harness-owned:
#   F7: once ANY stop latches (StopController calls set_armed(False)), the executor can NEVER be
#       re-armed within this process — the interlock is structural, not conventional.
#   F8: a stop-authorized FLATTEN is exempt from the CLIENT rate-token budget so risk reduction is
#       never starved (the proxy's persisted daily budget stays the real cap).
# It delegates the actual dispatch (mutex, caps, single-flight, POST-once, journaling) to Executor —
# it does NOT reimplement the single order authority.
# ---------------------------------------------------------------------------
class HarnessExecutor(Executor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arm_locked = False

    def set_armed(self, value: bool) -> None:
        if value and self._arm_locked:
            # F7: refuse to re-arm a stopped executor within the process.
            return
        if not value:
            self._arm_locked = True  # any disarm (stop trip or crash) latches the interlock
        super().set_armed(value)

    @property
    def arm_locked(self) -> bool:
        return self._arm_locked

    def execute(self, intent: Any, t_minus_s: float | None = None, *,
                stop_authorized: bool = False) -> Any:
        if stop_authorized and intent.purpose == PURPOSE_FLATTEN:
            # F8: exempt the flatten from the client token budget for the duration of THIS dispatch
            # (held under the executor's own reentrant lock so no concurrent dispatch sees the swap).
            with self._lock:
                saved = self._cfg
                self._cfg = replace(self._cfg, window_token_budget=_FLATTEN_EXEMPT_BUDGET)
                try:
                    return super().execute(intent, t_minus_s, stop_authorized=True)
                finally:
                    self._cfg = saved
        return super().execute(intent, t_minus_s, stop_authorized=stop_authorized)


# ---------------------------------------------------------------------------
# The startup plan (produced by prepare(), consumed by execute())
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    effective_mode: str
    armed: bool = False
    degraded: bool = False
    degrade_reason: str | None = None
    stand_down: bool = False
    stand_down_reason: str | None = None
    wake: WakeResult | None = None
    outcome: Any = None                     # sigma_feed.QuintileOutcome | None
    state: WindowState | None = None
    policy: PolicyParams | None = None
    arm_decision: Any = None                # stops.ArmDecision | None
    inherited: dict[str, Decimal] | None = None
    trailing_tape: list[dict] | None = None  # PHASE A prefetch: σ̂ trailing 15M anchors (T-900..T-7200)
    # --- box strategy (spec item 2) ---
    strategy: str = "corridor"
    strategy_valid: bool = True
    box_policy: Any = None                   # box.BoxParams | None (the sha-pinned box roster)
    box_state: Any = None                    # box.BoxState | None (built at phase B from the anchor)


WindowDriver = Callable[[WindowRecorder, float], None]


class WindowService:
    """One window lifecycle. All I/O collaborators are injectable so prepare()/execute() are testable
    with fakes and never touch a live network in the suite."""

    def __init__(
        self,
        close_time: str,
        cli_mode: str | None,
        *,
        pairs: int = 1,
        proxy: Any = None,
        policy_path: str = DEFAULT_POLICY_PATH,
        falsifier_path: str = DEFAULT_FALSIFIER_PATH,
        box_falsifier_path: str = DEFAULT_BOX_FALSIFIER_PATH,
        mode_txt_path: str | None = None,
        journal_dir: str = DEFAULT_JOURNAL_DIR,
        ledger_path: str = DEFAULT_LEDGER_PATH,
        proxy_base: str = "http://127.0.0.1:8642",
        max_hourly: int | None = 0,
        clock: Callable[[], float] = time.time,
        journal: Any = None,
        wake_context: Any = None,
        sigma_feed: Any = None,
        health_get: Callable[[], Any] | None = None,
        positions_reader: Callable[[], Any] | None = None,
        balance_get: Callable[[], Any] | None = None,
        ev_curve: Any = None,
        window_driver: WindowDriver | None = None,
        post_fn: Any = None,
        anchor_fetcher: Callable[[], list[dict]] | None = None,
        poll_sleep: Callable[[float], None] = time.sleep,
        cli_strategy: str | None = None,
        strategy_txt_path: str | None = None,
        box_policy_path: str = DEFAULT_BOX_POLICY_PATH,
        market_result_getter: Callable[[str], str | None] | None = None,
    ) -> None:
        self.close_time = close_time
        self.cli_mode = cli_mode
        self.pairs = int(pairs)
        self.policy_path = policy_path
        self.falsifier_path = falsifier_path
        self.box_falsifier_path = box_falsifier_path
        # Lever paths resolve to the MODULE-LEVEL defaults at call time (not import time) so a
        # monkeypatch of run_window.DEFAULT_MODE_TXT / DEFAULT_STRATEGY_TXT (the test-isolation
        # fixture) is honored. Callers that pass explicit paths are unaffected. The day-guard/ops
        # dir is derived from the resolved mode path below, so redirecting mode.txt also redirects
        # the stops_YYYY-MM-DD.json guard files.
        self.mode_txt_path = mode_txt_path if mode_txt_path is not None else DEFAULT_MODE_TXT
        self.journal_dir = journal_dir
        self.ledger_path = ledger_path
        self.proxy_base = proxy_base
        self.max_hourly = max_hourly
        self.clock = clock
        self.journal = journal if journal is not None else ThreadSafeJournal()
        self.proxy = proxy
        self._wake_context = wake_context
        self._sigma_feed = sigma_feed
        self._health_get = health_get
        self._positions_reader = positions_reader
        self._balance_get = balance_get
        self._ev_curve_obj = ev_curve
        self._window_driver = window_driver or self._default_window_driver
        self._post_fn = post_fn
        self._anchor_fetcher = anchor_fetcher
        self._poll_sleep = poll_sleep
        # Strategy lever (spec item 1). Resolved in prepare(); stored so the whole lifecycle branches.
        self.cli_strategy = cli_strategy
        self.strategy_txt_path = (
            strategy_txt_path if strategy_txt_path is not None else DEFAULT_STRATEGY_TXT
        )
        self.box_policy_path = box_policy_path
        self._market_result_getter = market_result_getter
        self._strategy = "corridor"
        self._strategy_valid = True
        self._box_policy: Any = None
        self._current_box_state: BoxState | None = None
        # F3: box_one_legged = "exactly one ENTRY leg filled" (set at entry, NEVER reset by a flatten)
        # -> the A5 counter measures entry quality, not whether we later escaped the naked leg.
        self._box_one_legged = False
        # F3: the flatten OUTCOME is recorded separately (None = no flatten attempted; True = the
        # one-legged leg was flattened flat; False = flatten missed/no-bid/cutoff -> held naked).
        self._box_flatten_filled: bool | None = None
        # F4: an in-flight one-legged flatten whose retries are EVENT-DRIVEN (each retry prices a
        # fresh bid on a later book event). None when no flatten is pending.
        self._pending_flatten: dict[str, Any] | None = None

        self.armed = False
        self.executor: Any = None
        self.stops: Any = None
        self.reconciler: Any = None
        self.ledger_state: LedgerState | None = None
        self._current_state: WindowState | None = None
        self._recorder: WindowRecorder | None = None
        # Exchange-sharding route map {ticker: exchange_index} captured from the wake sweep (set in
        # _run_box_window / _run_corridor_window). Every dispatched IntentLeg (entry, rebalance,
        # flatten) is stamped from this so orders route to the right shard (2026-08-27 incident);
        # a ticker absent here -> None -> the Executor refuses rather than send an unrouted order.
        self._exchange_index_by_ticker: dict[str, int | None] = {}
        self._connect_not_before: float | None = None  # WS-dial gate (15M open_time); set in execute()
        self._finalized = False
        self._day_totals: tuple[Decimal, int] = (Decimal(0), 0)  # (realized_today, guard_trips_today)
        # BUG-3 repair: day-scoped guard file (stop latching + S4 balance baseline) at ops/.
        self._utc_day = str(close_time)[:10]
        self._ops_dir = os.path.dirname(os.path.abspath(self.mode_txt_path))
        self._day_guard_path = os.path.join(self._ops_dir, f"stops_{self._utc_day}.json")
        self._day_guard: Any = None

    # --- lazy collaborators ---
    def _wake(self) -> Any:
        if self._wake_context is None:
            self._wake_context = WakeContext(self.proxy, clock=self.clock)
        return self._wake_context

    def _sigma(self) -> Any:
        if self._sigma_feed is None:
            from service.sigma_feed import SigmaFeed

            self._sigma_feed = SigmaFeed(self.proxy)
        return self._sigma_feed

    def _fetch_current_15m(self) -> list[dict]:
        """PHASE B: fetch the co-settling 15M market(s) via the proxy REST path (injectable for
        tests). Returns the markets list; the anchor is present once the leg has flipped active."""
        if self._anchor_fetcher is not None:
            return self._anchor_fetcher()
        return self._wake().fetch_co_settling_15m(self.close_time)

    def _ev_curve(self) -> Any:
        if self._ev_curve_obj is None:
            from service._simlaw import load_ev_curve

            self._ev_curve_obj = load_ev_curve()
        return self._ev_curve_obj

    def _journal(self, kind: str, obj: dict) -> None:
        self.journal.append(kind, obj, self.clock())

    # --- health / positions ---
    def _fetch_health(self) -> Any:
        if self._health_get is not None:
            try:
                return self._health_get()
            except Exception as e:  # noqa: BLE001 - a health-fetch failure fails closed to None
                logger.warning("[RUN] health fetch failed: %s", e)
                return None
        try:
            import requests

            r = requests.get(self.proxy_base.rstrip("/") + "/health", timeout=5)
            if getattr(r, "status_code", None) != 200:
                return None
            return r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[RUN] health fetch failed: %s", e)
            return None

    def _fetch_balance_payload(self) -> Any:
        """Fetch the raw /portfolio/balance payload via the proxy (read-only). Injectable for tests.
        Any transport failure -> None (the S4 check fails closed). Parsing/validation is done by
        stops.parse_balance at the call site so the mismatch/units check can be journaled."""
        try:
            if self._balance_get is not None:
                return self._balance_get()
            return self.proxy.rest_get("/portfolio/balance", {})
        except Exception as e:  # noqa: BLE001 - a balance-fetch failure fails closed to None
            logger.warning("[RUN] balance fetch failed: %s", e)
            return None

    def _read_positions(self) -> dict[str, Decimal]:
        if self._positions_reader is not None:
            body = self._positions_reader()
        else:
            body = self.proxy.rest_get("/portfolio/positions", {})
        return parse_positions_response(body)

    @staticmethod
    def _ours(ticker: str) -> bool:
        return any(ticker.startswith(p) for p in TICKER_PREFIXES)

    def _ledger_day_totals(self) -> tuple[Decimal, int]:
        """(realized_delta sum, guard_trips sum) over the ledger entries on the SAME UTC day as this
        window's close. Persists S4 (daily loss) + A4 (guard trips) across the process-per-window
        boundary — F3. Robust to a missing/short ledger (returns zeros)."""
        day = str(self.close_time)[:10]
        total = Decimal(0)
        trips = 0
        try:
            for e in load_entries(self.ledger_path):
                if str(e.get("close_time", ""))[:10] != day:
                    continue
                try:
                    total += Decimal(str(e.get("realized_delta", "0")))
                except (InvalidOperation, TypeError):
                    pass
                trips += int(e.get("guard_trips", 0) or 0)
        except Exception as ex:  # noqa: BLE001 - ledger read must never break startup
            logger.warning("[RUN] ledger day-total read failed: %s", ex)
        return total, trips

    def _caps_agree(self, health: Any) -> tuple[bool, str]:
        """F9: the proxy /health caps must AGREE in VALUE with the executor/pilot caps, not merely
        exist. Requires: proxy max_contracts_per_order >= requested pairs AND <= the pilot dual-size
        ceiling AND >= the executor's own per-order cap (executor no looser than the proxy); and the
        proxy ticker-prefix whitelist must cover our two series. Refuse armed on any disagreement."""
        if not isinstance(health, dict):
            return False, "no /health payload"
        caps = health.get("caps")
        if not isinstance(caps, dict):
            return False, "no caps block in /health"
        try:
            proxy_max = int(caps.get("max_contracts_per_order"))
        except (TypeError, ValueError):
            return False, "proxy max_contracts_per_order missing/non-numeric"
        if proxy_max < self.pairs:
            return False, f"proxy max_contracts_per_order {proxy_max} < requested pairs {self.pairs}"
        if proxy_max > PILOT_MAX_CONTRACTS_CEILING:
            return False, (f"proxy max_contracts_per_order {proxy_max} > pilot ceiling "
                           f"{PILOT_MAX_CONTRACTS_CEILING} (sizing to 5 needs a new commission)")
        exec_max = max(2, self.pairs)  # the ExecutorConfig.max_contracts the armed stack uses
        if exec_max > proxy_max:
            return False, f"executor max_contracts {exec_max} > proxy cap {proxy_max}"
        prefixes = caps.get("ticker_prefixes")
        if not isinstance(prefixes, (list, tuple)):
            return False, "proxy ticker_prefixes missing"
        missing = [p for p in TICKER_PREFIXES if p not in prefixes]
        if missing:
            return False, f"proxy prefixes do not cover our series: {missing}"
        return True, "ok"

    def _s3_poll_once(self) -> None:
        """F5: one gated S3 positions poll. SKIP entirely while any order is in flight (before AND
        after the poll) so an in-flight fill never reads as a phantom position -> spurious S3. When
        idle and a real mismatch stands, trip S3 (freeze + flatten policy)."""
        if not self.armed or self.reconciler is None or self.stops is None:
            return
        if self.stops.state.is_stopped:
            return
        ls = self.ledger_state
        if ls is None or ls.inflight_cids():
            return
        try:
            result = self.reconciler.tick(ls)
        except Exception as e:  # noqa: BLE001 - a poll failure must not crash the window
            logger.warning("[RUN] S3 poll failed: %s", e)
            return
        after = self.ledger_state
        # F5 (review): the diff ran against `ls`, captured BEFORE the poll's network read. If the
        # ledger changed AT ALL during the poll -- an order went in flight (inflight now non-empty)
        # OR an entry/rebalance landed AND recorded (clearing inflight but making `ls` stale) -- the
        # mismatch was computed against a stale snapshot. Defer to the next poll; never trip S3 on a
        # diff that raced the order path. (Identity check subsumes the inflight-after check.)
        if after is not ls:
            return
        if result.stop == "S3":
            from service.stops import S3_RECON

            self.stops.trip(S3_RECON, f"reconcile mismatch {result.mismatches}",
                            ledger_state=ls, bids=self._bids(),
                            exchange_index=self._exchange_index_by_ticker)

    def _sub_only_quintile(self, policy: PolicyParams) -> int:
        """A routing bucket whose sources are sub-$1-flip WITHOUT the strangle — used when the
        quintile could not be reproduced (NoQuintile) but a sub-$1 pair resolved (CONFESSED: this
        labels the window with a sub-only quintile the offline sim may not agree with; the strangle
        is independently disabled, so the label affects only which routing the sub-$1 flip takes)."""
        routing = policy.quintile_routing
        for q in sorted(routing):
            srcs = routing[q]
            if SUB_DOLLAR_FLIP in srcs and Q1_STRANGLE not in srcs:
                return q
        for q in sorted(routing):
            if SUB_DOLLAR_FLIP in routing[q]:
                return q
        return 0

    # -----------------------------------------------------------------------
    # prepare() — the startup-order law (steps a..e)
    # -----------------------------------------------------------------------
    def prepare(self) -> Plan:
        mode = resolve_mode(self.cli_mode, self.mode_txt_path)
        # (1) strategy lever (spec item 1). Resolved fresh each wake; an unknown/missing value fails
        # closed to the corridor core, DRY (never armed).
        strategy, strategy_valid = resolve_strategy(self.cli_strategy, self.strategy_txt_path)
        self._strategy = strategy
        self._strategy_valid = strategy_valid
        self._journal(
            "window_start",
            {
                "close_time": self.close_time,
                "requested_mode": mode,
                "pairs": self.pairs,
                "cli_mode": self.cli_mode,
                "strategy": strategy,
                "strategy_valid": strategy_valid,
            },
        )
        self._journal("strategy_selected", {"strategy": strategy, "valid": strategy_valid,
                                            "cli_strategy": self.cli_strategy})
        if not strategy_valid:
            self._journal(
                "strategy_invalid",
                {"raw_cli": self.cli_strategy, "strategy_file": self.strategy_txt_path,
                 "note": "unknown/missing strategy -> running the corridor core DRY, never armed "
                         "(fail closed)"},
            )

        # (a) policy + sha check. The box loads ITS sha-pinned roster (box-v1); the corridor loads the
        # pilot roster. Only one policy is loaded per window (the selected strategy's).
        policy: PolicyParams | None = None
        try:
            if strategy == "box":
                self._box_policy = load_box_policy(self.box_policy_path)
                policy_verified = True
                self._journal("policy_loaded",
                              {"sha256": self._box_policy.sha256,
                               "roster": self._box_policy.roster_name})
            else:
                policy = load_policy(self.policy_path)
                policy_verified = True
                self._journal("policy_loaded",
                              {"sha256": policy.sha256, "roster": policy.roster_name})
        except (PolicyShaMismatch, BoxPolicyShaMismatch) as e:
            self._journal("policy_refusal", {"error": str(e)})
            return Plan(effective_mode=mode, strategy=strategy, strategy_valid=strategy_valid,
                        stand_down=True, stand_down_reason=f"policy sha mismatch (S5): {e}")
        except (OSError, KeyError, ValueError) as e:
            self._journal("policy_refusal", {"error": str(e)})
            return Plan(effective_mode=mode, strategy=strategy, strategy_valid=strategy_valid,
                        stand_down=True, stand_down_reason=f"policy unreadable: {e}")

        # (b) arming (S5) — armed exercises it for real; dry exercises it for observability
        armed = False
        arm_decision = None
        effective_mode = mode
        degraded = False
        degrade_reason = None
        if mode in ("armed", "dry"):
            from service.stops import (
                StopConfig,
                arming_check,
                latched_stop_kind,
                read_day_guard,
            )

            stop_cfg = StopConfig()
            health = self._fetch_health()
            # F2: the box arms against box_falsifier.md and requires the resolved strategy to be
            # exactly "box"; the corridor keeps arming against falsifier.md. The box roster sha being
            # verified rides in policy_verified (load_box_policy self-checks the pinned sha above).
            if strategy == "box":
                active_falsifier = self.box_falsifier_path
                arm_decision = arming_check(
                    active_falsifier, health, policy_verified,
                    strategy=strategy, expected_strategy="box",
                )
            else:
                active_falsifier = self.falsifier_path
                arm_decision = arming_check(active_falsifier, health, policy_verified)
            # F9: value-agreement between the proxy /health caps and the executor/pilot caps.
            caps_ok, caps_reason = self._caps_agree(health)
            # A4 day-lock (guard trips) still derives from the ledger's UTC-day totals; the ledger
            # realized total is now the SECONDARY, reported figure (S4 is balance-based, checked below).
            loss_today, trips_today = self._ledger_day_totals()
            self._day_totals = (loss_today, trips_today)
            a4_locked = trips_today >= stop_cfg.guard_trips_standdown
            # BUG-3 repair: a day-halting stop (S1-S4) latched earlier today (persisted to the
            # day-scoped guard file) refuses arming for the rest of the UTC day. A CORRUPT guard file
            # fails closed (cannot confirm no latch -> refuse).
            guard = read_day_guard(self._day_guard_path, self._utc_day)
            self._day_guard = guard
            latched_kind = latched_stop_kind(guard)
            extra: list[str] = []
            if not self._strategy_valid:
                extra.append("strategy invalid -> corridor core, never armed (fail closed)")
            if not caps_ok:
                extra.append(f"caps disagreement (F9): {caps_reason}")
            if guard.corrupt:
                extra.append(f"day-guard file corrupt at {os.path.basename(self._day_guard_path)} (fail closed)")
            if latched_kind is not None:
                extra.append(f"{latched_kind} latched earlier today -> day halted (falsifier: a stop halts the DAY)")
            if a4_locked:
                extra.append(
                    f"A4 day-lock: {trips_today} guard trips today >= {stop_cfg.guard_trips_standdown}"
                )
            self._journal(
                "arming",
                {
                    "mode": mode,
                    "armed": arm_decision.armed,
                    "reasons": list(arm_decision.reasons),
                    "falsifier_path": active_falsifier,
                    "falsifier_basename": os.path.basename(active_falsifier),
                    "strategy": strategy,
                    "caps_ok": caps_ok,
                    "caps_reason": caps_reason,
                    "day_realized_ledger": str(loss_today),
                    "day_guard_trips": trips_today,
                    "stop_latched": latched_kind,
                    "day_guard_corrupt": guard.corrupt,
                    "a4_day_locked": a4_locked,
                    "health_present": health is not None,
                },
            )
            if mode == "armed":
                if arm_decision.armed and not extra:
                    armed = True
                else:
                    effective_mode = "dry"
                    degraded = True
                    all_reasons = list(arm_decision.reasons) + extra
                    degrade_reason = "; ".join(all_reasons) or "arming refused"
                    self._journal(
                        "degrade_to_dry",
                        {"reasons": all_reasons,
                         "note": "ARMED requested but a gate failed -> running DRY, orders frozen"},
                    )

        # (c) reconcile-first
        # B3: the pending-settlement picture the sweep computes, consumed by the S4 wake block below.
        # Initialized empty so a reconcile-first failure (caught) leaves the S4 block a safe {} (no
        # pending legs -> the pessimistic == optimistic loss, i.e. today's balance behaviour).
        settlement_pending: dict[str, dict[str, Any]] = {}
        try:
            observed = self._read_positions()
            inherited = {t: n for t, n in observed.items() if self._ours(t) and n != 0}
            self._journal(
                "reconcile_first",
                {"observed": {t: str(v) for t, v in observed.items()},
                 "inherited": {t: str(v) for t, v in inherited.items()}},
            )
            # Settlement-backfill automation (spec item 5): for any prior ledger row still marked
            # realized_unsettled, fetch each held ticker's settled result via the proxy /markets
            # (read-only) and run the existing backfill. Idempotent, fail-closed (an unsettled/absent
            # result just waits for a later wake); never breaks startup.
            settlement_pending = self._settlement_backfill_sweep()
            # B4 (LOW fix): the sweep may have appended a backfill row for TODAY (crediting the pinned
            # +$1 that was booked at the $1 floor). Recompute _day_totals AFTER the sweep so the S4
            # record's ledger_realized_today / ledger_vs_balance_delta reflect the just-backfilled
            # credit instead of the pre-sweep snapshot (the backfill timing artifact).
            self._day_totals = self._ledger_day_totals()
            if inherited:
                # BUG-3 (S4 balance): positions are open at wake, so the balance is NOT clean cash
                # and the loss compare would be meaningless -> record a skip rather than compare
                # (Brad's ruling note a). The baseline is also not snapshotted from a dirty balance.
                self._journal(
                    "balance_check_skipped_open_positions",
                    {"inherited": {t: str(v) for t, v in inherited.items()},
                     "note": "open positions at wake -> S4 balance compare skipped (dirty balance)"},
                )
                self._journal(
                    "inherited_position_refusal",
                    {"inherited": {t: str(v) for t, v in inherited.items()},
                     "note": "existing position in our series -> refuse to trade this window; "
                             "flatten is NOT automatic (PENDING-BRAD F4/F8)"},
                )
                return Plan(effective_mode=effective_mode, armed=False, degraded=degraded,
                            degrade_reason=degrade_reason, stand_down=True,
                            stand_down_reason="inherited position in our series (F4/F8 pending)",
                            policy=policy, arm_decision=arm_decision,
                            inherited={t: n for t, n in inherited.items()})
        except Exception as e:  # noqa: BLE001 - a positions-read failure
            self._journal("reconcile_first_error", {"error": str(e)})
            if armed:
                return Plan(effective_mode="dry", armed=False, degraded=True,
                            degrade_reason=f"positions read failed: {e}", stand_down=True,
                            stand_down_reason=f"cannot confirm flat (armed): {e}",
                            policy=policy, arm_decision=arm_decision)
            # dry/shakedown: no orders, continue after an alarm

        # (c2) S4 DAILY LOSS from the ACCOUNT BALANCE (Brad's ruling 2026-08-26). The wake is a flat
        # point (all prior windows have settled), so the balance is clean cash and its delta == P&L.
        # First wake of the UTC day: snapshot balance_start into the day-guard file. Every wake:
        # loss = balance_start - balance_now; loss >= cap -> latch S4 (refuse to arm, degrade to dry,
        # loud journal record). A missing/failed balance read FAILS CLOSED (do not arm). The repaired
        # ledger realized stays as the SECONDARY figure; ledger_vs_balance_delta is journaled so a
        # divergence between our books and the venue is visible.
        if mode in ("armed", "dry"):
            from service.stops import (
                StopConfig,
                ensure_balance_start,
                parse_balance,
                record_latched_stop,
                s4_balance_decision,
                s4_pending_value,
            )

            stop_cfg = StopConfig()

            def _degrade(reason: str) -> None:
                nonlocal armed, effective_mode, degraded, degrade_reason
                if mode != "armed":
                    return  # dry mode is already non-arming; nothing to degrade
                armed = False
                effective_mode = "dry"
                degraded = True
                degrade_reason = "; ".join(r for r in [degrade_reason, reason] if r)
                self._journal(
                    "degrade_to_dry",
                    {"reasons": [reason],
                     "note": "S4 gate failed -> running DRY, orders frozen"},
                )

            guard = self._day_guard
            if guard is not None and guard.corrupt:
                # F3: a corrupt guard is NEVER self-healed. Arming was already refused in step (b);
                # here we ALSO skip the balance snapshot/compare entirely (never write over it) so the
                # latches/baseline survive and every wake this day keeps refusing until a human fixes
                # the file.
                self._journal(
                    "stops_guard_corrupt",
                    {"path": os.path.basename(self._day_guard_path),
                     "note": "day-guard corrupt -> refuse to arm, no balance snapshot, no overwrite"},
                )
            else:
                payload = self._fetch_balance_payload()
                if payload is None:
                    self._journal(
                        "s4_balance_read_failed",
                        {"note": "balance read failed/absent at wake -> fail closed (no arm)"},
                    )
                    _degrade("S4 balance read failed (fail closed)")
                else:
                    br = parse_balance(payload)
                    if not br.ok:
                        # A units mismatch (cents vs dollars disagree) gets its own loud record.
                        if br.status == "mismatch":
                            self._journal(
                                "balance_parse_mismatch",
                                {"balance_cents": br.cents, "note": "balance vs balance_dollars "
                                 "disagree by >= 0.01 -> fail closed (no arm)"},
                            )
                        else:
                            self._journal(
                                "s4_balance_read_failed",
                                {"status": br.status,
                                 "note": "balance payload unusable -> fail closed (no arm)"},
                            )
                        _degrade(f"S4 balance unusable ({br.status})")
                    else:
                        start, first_wake = ensure_balance_start(
                            self._day_guard_path, self._utc_day, br.dollars, self.clock()
                        )
                        # B2/B3: the pending-settlement band. pending_value is the OPTIMISTIC credit
                        # still owed by today's unfinalized legs ($1/leg); the decision latches only if
                        # the cap is breached even under that best case, and stands down (no latch)
                        # while the breach depends on an unfinalized leg (the 16:40Z measurement bug).
                        pending_value = s4_pending_value(settlement_pending, self._utc_day)
                        cap = stop_cfg.daily_loss_cap_dollars
                        s4d = s4_balance_decision(start, br.dollars, pending_value, cap)
                        loss_dollars = s4d.loss_pessimistic  # start - now (secondary reporting figure)
                        breached = s4d.kind == "latch"
                        pending_legs = [
                            {"window": w, "ticker": lg[0], "count": lg[2]}
                            for w, row in (settlement_pending or {}).items()
                            for lg in row.get("legs", []) or []
                            if (len(lg) <= 3 or lg[3] is None)
                            and str(row.get("close_time", ""))[:10] == self._utc_day
                        ]
                        # Secondary: the repaired ledger realized today + the venue-vs-ledger delta.
                        ledger_realized_today = self._day_totals[0]
                        balance_pnl = -loss_dollars  # balance_now - balance_start
                        self._journal(
                            "s4_balance_check",
                            {
                                "balance_start_dollars": str(start),
                                "balance_now_dollars": str(br.dollars),
                                "balance_cents": br.cents,
                                "portfolio_value": br.portfolio_value,
                                "first_wake": first_wake,
                                "loss_dollars": str(loss_dollars),
                                "cap_dollars": str(cap),
                                "breached": breached,
                                "pending_value_dollars": str(pending_value),
                                "pending_legs": pending_legs,
                                "loss_pessimistic": str(s4d.loss_pessimistic),
                                "loss_optimistic": str(s4d.loss_optimistic),
                                "decision": s4d.kind,
                                "ledger_realized_today": str(ledger_realized_today),
                                "ledger_vs_balance_delta": str(ledger_realized_today - balance_pnl),
                            },
                        )
                        if s4d.kind == "latch":
                            record_latched_stop(
                                self._day_guard_path, self._utc_day, "S4",
                                f"S4 balance: loss {s4d.loss_pessimistic} (optimistic "
                                f"{s4d.loss_optimistic}) >= cap {cap} even after pending credit "
                                f"${pending_value} (start ${start} -> now ${br.dollars})",
                                self.close_time, self.clock(),
                            )
                            self._journal(
                                "s4_balance_latch",
                                {"loss_pessimistic": str(s4d.loss_pessimistic),
                                 "loss_optimistic": str(s4d.loss_optimistic),
                                 "pending_value_dollars": str(pending_value),
                                 "cap_dollars": str(cap),
                                 "note": "S4 cap breached under EVERY resolution of pending "
                                         "settlements -> latched for the day"},
                            )
                            _degrade(
                                f"S4 balance day-lock: loss {s4d.loss_pessimistic} >= {cap} "
                                f"(optimistic {s4d.loss_optimistic} also >= {cap})"
                            )
                        elif s4d.kind == "pending":
                            # The breach depends on a leg the venue has not finalized. Stand down THIS
                            # window (degrade to dry) but do NOT latch and do NOT write the day guard —
                            # the next wake re-evaluates once the settlement lands (or does not).
                            self._journal(
                                "s4_pending_settlement",
                                {"loss_pessimistic": str(s4d.loss_pessimistic),
                                 "loss_optimistic": str(s4d.loss_optimistic),
                                 "pending_value_dollars": str(pending_value),
                                 "pending_legs": pending_legs,
                                 "cap_dollars": str(cap),
                                 "note": "S4 loss is between the pessimistic and optimistic bounds; a "
                                         "pending settlement can still move it -> stand down this "
                                         "window, no latch, re-evaluate next wake"},
                            )
                            _degrade(
                                f"S4 pending settlement: loss between {s4d.loss_optimistic} and "
                                f"{s4d.loss_pessimistic} vs cap {cap} (pending ${pending_value}) -> "
                                f"stand down this window, no latch"
                            )

        # (d) wake discovery
        try:
            wake = self._wake().sweep(self.close_time)
        except Exception as e:  # noqa: BLE001 - a wake sweep failure = cannot discover -> stand down
            self._journal("wake_error", {"error": str(e)})
            return Plan(effective_mode=effective_mode, armed=armed, degraded=degraded,
                        degrade_reason=degrade_reason, stand_down=True,
                        stand_down_reason=f"wake sweep failed: {e}", policy=policy,
                        arm_decision=arm_decision)
        if isinstance(wake, StandDown):
            self._journal("wake_standdown", {"reason": wake.reason})
            return Plan(effective_mode=effective_mode, armed=armed, degraded=degraded,
                        degrade_reason=degrade_reason, stand_down=True,
                        stand_down_reason=wake.reason, policy=policy, arm_decision=arm_decision)
        self._journal(
            "window_meta",
            {
                "close_time": wake.close_time,
                "fifteen_ticker": wake.fifteen_leg.primary_ticker,
                "hourly_event": wake.hourly_leg.event_ticker,
                "ladder_ok": wake.ladder.ok,
                "ladder_alarm": wake.ladder.alarm,
                "strangle_disabled": wake.ladder.strangle_disabled,
                "expected_step": str(wake.ladder.expected_step),
                "observed_step": None if wake.ladder.observed_step is None else str(wake.ladder.observed_step),
            },
        )

        # (e) PHASE A: σ̂ trailing-anchor prefetch (everything ANCHOR-INDEPENDENT is done here).
        # Live finding 2026-08-21: the co-settling 15M market is "initialized" with NO floor_strike
        # at the :40 wake — the anchor A(T) materializes only when the leg flips "active" at open_time
        # (~:45). So NO quintile/pairing/G/g-σ̂ is computed here: those all need A(T) and are deferred
        # to PHASE B (execute() -> _resolve_anchor, at leg open). What IS available at :40 and
        # prefetched now: the 8 trailing σ̂ anchors T-900..T-7200 (all present with strikes at :40).
        # A prefetch failure is fail-closed to None (phase B re-fetches inside assign()).
        trailing_tape: list[dict] | None = None
        if self._strategy == "box":
            # The box has NO σ̂/quintile — the trailing-anchor tape is corridor-only, so skip the
            # prefetch entirely (spec item 2: quintile assignment is corridor-only).
            logger.info("[RUN] box strategy: skipping σ̂ trailing prefetch (no quintile)")
        else:
            try:
                trailing_tape = self._sigma().fetch_trailing_15m(self.close_time)
            except Exception as e:  # noqa: BLE001 - a prefetch failure degrades to a phase-B re-fetch
                logger.warning("[RUN] σ̂ trailing prefetch failed: %s", e)
                trailing_tape = None
        self._journal(
            "phase_a",
            {
                "close_time": self.close_time,
                "fifteen_ticker": wake.fifteen_leg.primary_ticker,
                "fifteen_open_time": wake.fifteen_leg.open_time,
                "hourly_event": wake.hourly_leg.event_ticker,
                "trailing_anchors_prefetched": (0 if trailing_tape is None else len(trailing_tape)),
                "ladder_ok": wake.ladder.ok,
                "strangle_disabled": wake.ladder.strangle_disabled,
                "note": "anchor A(T) NOT yet available (15M initialized); pairing/σ̂/quintile "
                        "deferred to PHASE B at leg open",
            },
        )

        if armed:
            self._build_armed_stack(self._box_policy if self._strategy == "box" else policy)

        return Plan(effective_mode=effective_mode, armed=armed, degraded=degraded,
                    degrade_reason=degrade_reason, stand_down=False, wake=wake, policy=policy,
                    arm_decision=arm_decision, trailing_tape=trailing_tape,
                    strategy=self._strategy, strategy_valid=self._strategy_valid,
                    box_policy=self._box_policy)

    def _build_armed_stack(self, policy: Any) -> None:
        from service.stops import StopConfig, StopController

        cfg = ExecutorConfig(
            armed=True,
            max_contracts=max(2, self.pairs),
            ticker_prefixes=TICKER_PREFIXES,
            no_orders_after_s_to_settle=policy.no_orders_after_s_to_settle,
            proxy_base=self.proxy_base,
        )
        kwargs: dict[str, Any] = {}
        if self._post_fn is not None:
            kwargs["post_fn"] = self._post_fn
        self.executor = HarnessExecutor(self.journal, cfg, clock=self.clock, **kwargs)
        # BUG-3 repair: the controller persists day-halting trips (S1-S4) to the day-scoped guard
        # file so the NEXT window refuses to arm for the rest of the UTC day.
        self.stops = StopController(
            self.executor, self.journal, StopConfig(), clock=self.clock,
            latch_path=self._day_guard_path, utc_day=self._utc_day, window=self.close_time,
            flatten_sink=self._fold_flatten_response,
        )
        # F3: seed the per-process StopState's S4 daily-loss + A4 guard-trip counters from the
        # ledger's UTC-day totals so the caps are enforced PER UTC DAY, not per window.
        loss_today, trips_today = self._day_totals
        self.stops.state = replace(
            self.stops.state, daily_realized=loss_today, guard_trips=trips_today
        )
        # The corridor's positions reconciler drives the imbalance/S3 protocol; the box has NO
        # rebalance and its own post-fill policy (immediate flatten / hold), so it runs WITHOUT a
        # reconciler (leaving it None also keeps the WS window's S3 poll loop from ever starting).
        if self._strategy == "box":
            self.reconciler = None
        else:
            self.reconciler = PositionsReconciler(
                lambda path, params=None: self.proxy.rest_get(path, params or {})
            )
        self._policy_cache = policy

    # -----------------------------------------------------------------------
    # PHASE B — anchor poll + quintile (at leg open; live finding 2026-08-21)
    # -----------------------------------------------------------------------
    def _resolve_anchor(self, plan: Plan) -> Any:
        """PHASE B: wait for the 15M leg to open, poll REST until the anchor strike materializes,
        then reproduce the quintile from the phase-A-prefetched trailing tape + the polled current
        market. Returns the ``QuintileOutcome`` (ok / sub-only fallback / whole-window stand-down),
        or ``None`` on poll timeout (EXCL_NO_ANCHOR — now a LEGITIMATE stand-down).

        The strike is the ONLY thing that waits for :45: A(T) = BTC spot at window open (a non-round
        number), which does not exist until the market flips "active". Everything else (leg identity,
        hourly ladder, ladder-map check, trailing σ̂ anchors) was resolved in phase A. Bounded by
        both open_time+ANCHOR_POLL_TIMEOUT_S and the window deadline; ``poll_sleep``/``clock`` are
        injectable so tests drive the poll with a fake clock and no real network."""
        wake = plan.wake
        assert wake is not None
        try:
            open_epoch = float(close_epoch(wake.fifteen_leg.open_time))
        except (ValueError, TypeError, KeyError, AttributeError):
            # F3: an unparseable/absent 15M open_time falls back to the REAL open (close - 900s),
            # NOT the machine clock. The 15M leg opens 15 minutes before the top-of-hour close, so
            # anchoring the poll budget here keeps the full open..open+45s window instead of timing
            # out ~4 minutes early at the :40 wake time (spurious EXCL_NO_ANCHOR).
            open_epoch = float(close_epoch(wake.close_time) - 900)
        window_deadline = float(close_epoch(wake.close_time) + GRACE_SECONDS)
        poll_deadline = min(open_epoch + ANCHOR_POLL_TIMEOUT_S, window_deadline)
        self._journal(
            "phase_b_start",
            {
                "fifteen_ticker": wake.fifteen_leg.primary_ticker,
                "open_time": wake.fifteen_leg.open_time,
                "open_epoch": open_epoch,
                "poll_deadline": poll_deadline,
                "poll_interval_s": ANCHOR_POLL_INTERVAL_S,
                "note": "15M strike materializes at open_time; polling REST for floor_strike",
            },
        )
        polls = 0
        while True:
            now = self.clock()
            if now < open_epoch:
                # not open yet: sleep to the open (bounded by the poll deadline).
                self._poll_sleep(max(0.0, min(open_epoch - now, poll_deadline - now)))
                if self.clock() < open_epoch:
                    # a bounded (deadline-clamped) sleep returned before open -> we are out of time.
                    return None
                continue
            markets_15m = self._fetch_current_15m()
            polls += 1
            A, a_ticker = _anchor_at(markets_15m, self.close_time)
            if A is not None and a_ticker:
                self._journal(
                    "phase_b_anchor",
                    {"anchor_A": A, "anchor_ticker": a_ticker, "polls": polls, "clock": now},
                )
                outcome = self._sigma().assign(
                    self.close_time,
                    # F1: pair against the POOLED strikes of ALL co-settling hourly generations
                    # (census h1_by_ct semantics), NOT just the selected smallest-window generation.
                    # nearest_hourly_strike over the pool reproduces census's nearest-below/above and
                    # equidistant-tie exclusion (an identical strike present in two generations is a
                    # cross-generation tie -> stand down). The chosen market's own generation is used
                    # for the ladder-map check and the WS subscription in _apply_outcome.
                    list(wake.hourly_pool_markets),
                    current_15m_markets=markets_15m,
                    trailing_markets=plan.trailing_tape,
                )
                self._journal(
                    "quintile",
                    {
                        "ok": outcome.ok,
                        "stand_down": outcome.stand_down,
                        "quintile": outcome.quintile,
                        "high_ticker": outcome.high_ticker,
                        "low_ticker": outcome.low_ticker,
                        "G": None if outcome.G is None else str(outcome.G),
                        "sigma_hat": outcome.sigma_hat,
                        "strangle_disabled": outcome.strangle_disabled,
                        "reason": outcome.reason,
                    },
                )
                return outcome
            now = self.clock()
            if now >= poll_deadline:
                return None  # timeout -> EXCL_NO_ANCHOR (the strike never materialized)
            self._poll_sleep(max(0.0, min(ANCHOR_POLL_INTERVAL_S, poll_deadline - now)))

    def _chosen_hourly_leg(self, wake: WakeResult, high_ticker: str | None,
                           low_ticker: str | None) -> Any:
        """The hourly GENERATION (Leg) the pooled pairing actually chose (F1). With >1 co-settling
        hourly generation, the pooled nearest-threshold pairing may pick a strike from a generation
        OTHER than wake's selected smallest-window one; both the WS subscription and the ladder-map
        check must then follow the market actually paired. Identifies it by which generation's ladder
        contains the chosen hourly ticker (the non-anchor leg). Falls back to ``wake.hourly_leg`` when
        the ticker maps to no retained generation (single-generation / back-compat construction)."""
        for tk in (high_ticker, low_ticker):
            if not tk:
                continue
            for leg in wake.hourly_ladders:
                if tk in leg.market_tickers:
                    return leg
        return wake.hourly_leg

    def _chosen_ladder_check(self, wake: WakeResult, chosen_leg: Any) -> Any:
        """The ladder-map check for the GENERATION of the chosen hourly market (F1 / commission:
        'validate the ladder of the specific market actually paired'). When the chosen generation IS
        wake's selected one, reuse the already-computed ``wake.ladder`` (identical); otherwise compute
        the step-map check against the chosen generation's own floor_strikes."""
        from service.wake import ladder_check
        sel = wake.hourly_leg
        if (chosen_leg.event_ticker, chosen_leg.open_time) == (sel.event_ticker, sel.open_time):
            return wake.ladder
        return ladder_check(chosen_leg)

    def _apply_outcome(self, plan: Plan, outcome: Any) -> None:
        """Build the seeded WindowState from a resolved phase-B outcome (an anchor+pairing exists).
        Mirrors the former prepare()-tail: sub-only routing bucket when the quintile could not be
        reproduced, strangle_disabled OR-ed with the ladder check, shakedown flag from armed."""
        assert plan.wake is not None and plan.policy is not None
        q = outcome.quintile if outcome.quintile is not None else self._sub_only_quintile(plan.policy)
        # F1: the ladder-map gate applies to the GENERATION of the CHOSEN market, not (necessarily)
        # wake's selected smallest-window generation — the pooled pairing may have picked a strike
        # from a different co-settling generation.
        chosen_leg = self._chosen_hourly_leg(plan.wake, outcome.high_ticker, outcome.low_ticker)
        chosen_ladder = self._chosen_ladder_check(plan.wake, chosen_leg)
        sel = plan.wake.hourly_leg
        cross_generation = (chosen_leg.event_ticker, chosen_leg.open_time) != (
            sel.event_ticker, sel.open_time
        )
        if cross_generation:
            self._journal(
                "chosen_ladder",
                {
                    "chosen_event": chosen_leg.event_ticker,
                    "chosen_open_time": chosen_leg.open_time,
                    "selected_event": sel.event_ticker,
                    "selected_open_time": sel.open_time,
                    "expected_step": str(chosen_ladder.expected_step),
                    "observed_step": (None if chosen_ladder.observed_step is None
                                      else str(chosen_ladder.observed_step)),
                    "ok": chosen_ladder.ok,
                    "strangle_disabled": chosen_ladder.strangle_disabled,
                    "note": "pooled pairing chose a NON-selected hourly generation; ladder-map check "
                            "applies to the chosen generation (F1)",
                },
            )
        strangle_disabled = chosen_ladder.strangle_disabled or outcome.strangle_disabled
        fair_q = self._ev_curve().fair_for("strangle", q)
        state = WindowState.new(
            close_time=self.close_time,
            high_ticker=outcome.high_ticker,
            low_ticker=outcome.low_ticker,
            quintile=q,
            fair_strangle_q=fair_q,
            strangle_disabled=strangle_disabled,
            shakedown=(not plan.armed),
            G=outcome.G,
            sigma_hat=outcome.sigma_hat,
            T=int(close_epoch(self.close_time)),
        )
        self._current_state = state
        plan.outcome = outcome
        plan.state = state

    # -----------------------------------------------------------------------
    # execute() — steps f + g, robust to crash
    # -----------------------------------------------------------------------
    def run(self) -> int:
        try:
            plan = self.prepare()
        except Exception:  # noqa: BLE001 - finding 5: a startup-step crash must still leave a trace
            return self._finalize_startup_failure(traceback.format_exc())
        # Disk repair (Brad, 2026-09-02): at the wake, AFTER prepare() has done all reconcile/inherit
        # reads, gzip closed journals from prior hours to reclaim disk. Best-effort and fully
        # isolated — never allowed to affect the window run.
        self._rotate_closed_journals()
        return self.execute(plan)

    def _rotate_closed_journals(self) -> None:
        """Compress closed raw journals in ``self.journal_dir`` to ``.jsonl.gz`` (disk-full repair).

        Skips ``summary.jsonl``, the CURRENT window's journal (which the write path still emits raw),
        any name listed in ``ops/journal_keep.txt``, and anything with mtime younger than 30 min.
        The current window's journal does not yet exist on disk at the wake, but it is excluded by
        name anyway (defensive). Work is BOUNDED per wake (at most DEFAULT_MAX_FILES files / a wall-
        clock budget) so a large backlog can never push execute() past the anchor poll deadline —
        the remainder drains over subsequent wakes. ANY exception — from the sweep or from journaling
        its result — is caught and logged here so a rotation failure can NEVER affect the window run."""
        try:
            from service.journal_io import (
                DEFAULT_MAX_FILES,
                DEFAULT_MAX_SECONDS,
                rotate_closed_journals,
            )

            current = _safe_close(self.close_time) + ".jsonl"
            keep_path = os.path.join(self._ops_dir, "journal_keep.txt")
            summary = rotate_closed_journals(
                self.journal_dir,
                exclude_basenames={current},
                keep_path=keep_path,
                now=self.clock(),
                max_files=DEFAULT_MAX_FILES,       # bound the pre-execute critical path
                max_seconds=DEFAULT_MAX_SECONDS,
            )
            if summary["rotated"] or summary["errors"] or summary.get("deferred"):
                logger.info(
                    "[RUN] journal rotation: %d compressed, %d bytes saved, %d error(s), %d deferred",
                    summary["count"], summary["bytes_saved"], len(summary["errors"]),
                    summary.get("deferred", 0),
                )
                self._journal("journal_rotation", summary)
        except Exception as e:  # noqa: BLE001 - rotation must never affect the window run
            logger.warning("[RUN] journal rotation failed (ignored): %s", e)
            try:
                self._journal("journal_rotation_error", {"error": repr(e)})
            except Exception:  # noqa: BLE001
                pass

    def _finalize_startup_failure(self, tb: str) -> int:
        """Finding 5: a startup step in prepare() crashed BEFORE a Plan existed. execute()'s finally
        (which flushes + writes the ledger row) never runs, so without this the window leaves NO
        journal and NO ledger row -- the 24h-dry-cycle quick-scan then sees an ABSENT row, not a
        FLAGGED failure. Journal the traceback, flush whatever is buffered, and append a MINIMAL
        ledger row (status=startup-failed, exit_code=1) so the failure is visible; exit nonzero.
        Mirrors the execute-path crash behavior (exit_code:1 row). This method must never raise."""
        if self._finalized:
            return 1
        self._finalized = True
        logger.error("[RUN] startup (prepare) failed:\n%s", tb)
        try:
            self._journal("startup_exception", {"traceback": tb})
        except Exception as e:  # noqa: BLE001 - finalize must never raise
            logger.error("[RUN] startup journal append failed: %s", e)
        journal_path = os.path.join(self.journal_dir, _safe_close(self.close_time) + ".jsonl")
        records = 0
        try:
            records = self.journal.flush(journal_path)
        except Exception as e:  # noqa: BLE001
            logger.error("[RUN] startup journal flush failed: %s", e)
        try:
            # mode.txt/CLI resolution is normally crash-proof but keep it fail-closed to 'shakedown'.
            try:
                mode = resolve_mode(self.cli_mode, self.mode_txt_path)
            except Exception:  # noqa: BLE001
                mode = "shakedown"
            last_line = next((ln for ln in reversed(tb.strip().splitlines()) if ln.strip()),
                             "startup failure")
            entry = {
                "close_time": self.close_time,
                "mode": mode,
                "requested_mode": mode,
                "pairs": self.pairs,
                "armed": False,
                "status": "startup-failed",
                "stand_down": True,
                "stand_down_reason": "startup (prepare) crashed before a plan existed",
                "error": last_line.strip()[:500],
                "exit_code": 1,
                "orders_attempted": 0,
                "realized_delta": "0",
                "realized_unsettled": False,
                "journal_path": os.path.abspath(journal_path),
                "records": records,
                "flushed_at": self.clock(),
            }
            append_entry(entry, self.ledger_path)
        except Exception as e:  # noqa: BLE001 - the row is best-effort; never mask the exit code
            logger.error("[RUN] startup ledger append failed: %s", e)
        return 1

    def execute(self, plan: Plan) -> int:
        self.armed = plan.armed
        exit_code = 0
        recorder: WindowRecorder | None = None
        try:
            if plan.stand_down:
                return exit_code
            if plan.strategy == "box":
                recorder = self._run_box_window(plan)
            else:
                recorder = self._run_corridor_window(plan)
        except KeyboardInterrupt:
            logger.warning("[RUN] Ctrl+C — flushing buffered journal.")
            self._journal("interrupt", {"note": "keyboard interrupt; flushing buffered journal"})
        except Exception:  # noqa: BLE001 - freeze, journal the traceback, flush, exit nonzero
            exit_code = 1
            if self.executor is not None:
                try:
                    self.executor.set_armed(False)
                except Exception:  # noqa: BLE001
                    pass
            tb = traceback.format_exc()
            logger.error("[RUN] unhandled exception:\n%s", tb)
            self._journal("unhandled_exception", {"traceback": tb})
        finally:
            # A crash AFTER the recorder was built loses `recorder` (the assignment above never
            # completed), but self._recorder was set inside the run method — finalize needs it so the
            # buffered journal is flushed via the recorder (crash-flush law).
            self._finalize(plan, recorder if recorder is not None else self._recorder, exit_code)
        return exit_code

    def _run_corridor_window(self, plan: Plan) -> WindowRecorder | None:
        """The corridor PHASE B + window run (unchanged from the single-strategy harness). Returns the
        recorder, or None on a legitimate phase-B stand-down."""
        # PHASE B: resolve the current-window anchor by polling at leg open (live finding
        # 2026-08-21). Only after the strike materializes can we pair/score/quintile the window.
        outcome = self._resolve_anchor(plan)
        if outcome is None:
            # the strike never arrived before the poll timeout -> a LEGITIMATE stand-down.
            plan.stand_down = True
            plan.stand_down_reason = (
                "EXCL_NO_ANCHOR (15M strike did not materialize before the poll timeout)"
            )
            self._journal(
                "phase_b_timeout",
                {"reason": plan.stand_down_reason,
                 "note": "no anchor -> no pairing, no C computation (both legs' prices needed) "
                         "-> whole window stands down (sub-$1 also requires the anchor)"},
            )
            return None
        if outcome.stand_down:
            # anchor materialized but no clean pair (no hourly leg / nearest-tie / degenerate G):
            # no C can be computed for EITHER source -> whole window stands down.
            plan.outcome = outcome
            plan.stand_down = True
            plan.stand_down_reason = outcome.reason
            return None
        self._apply_outcome(plan, outcome)
        # Exchange-sharding route map for every dispatched leg this window (2026-08-27 incident).
        assert plan.wake is not None
        self._exchange_index_by_ticker = plan.wake.exchange_index_by_ticker
        recorder = self._build_recorder(plan)
        self._recorder = recorder
        deadline = close_epoch(plan.wake.close_time) + GRACE_SECONDS
        self._connect_not_before = self._connect_gate(plan.wake)
        if self._connect_not_before is not None:
            self._journal(
                "connect_gate",
                {"fifteen_open_time": plan.wake.fifteen_leg.open_time,
                 "connect_not_before": self._connect_not_before,
                 "margin_seconds": _CONNECT_MARGIN_S,
                 "note": "15M leg is 'initialized' at wake; holding the WS dial until ~open_time"},
            )
        self._window_driver(recorder, deadline)
        return recorder

    # -----------------------------------------------------------------------
    # BOX strategy — PHASE B (anchor poll, no quintile) + window run + post-fill policy
    # -----------------------------------------------------------------------
    def _run_box_window(self, plan: Plan) -> WindowRecorder | None:
        """The box PHASE B + window run. Polls the 15M leg for its strike (anchor A + the 15M ticker),
        builds a BoxState from the CHOSEN hourly generation's ladder, subscribes the FULL ladder + 15M
        (regardless of --max-hourly-strikes), and drives ``decide_box``. Returns the recorder, or None
        on a legitimate stand-down."""
        wake = plan.wake
        assert wake is not None and plan.box_policy is not None
        # Exchange-sharding route map for every dispatched leg this window (2026-08-27 incident).
        self._exchange_index_by_ticker = wake.exchange_index_by_ticker
        anchor = self._resolve_box_anchor(plan)
        if anchor is None:
            plan.stand_down = True
            plan.stand_down_reason = (
                "EXCL_NO_ANCHOR (box: 15M strike did not materialize before the poll timeout)"
            )
            self._journal("box_phase_b_timeout", {"reason": plan.stand_down_reason})
            return None
        anchor_A, a_ticker = anchor
        # Ladder strikes (Decimal) from the SELECTED hourly generation's wake market records — the same
        # generation the ladder-map check validated. F1 pooled-generation logic does NOT apply to the
        # box (spec item 2): use the selected generation.
        strikes: dict[str, Decimal] = {}
        for m in wake.hourly_leg.markets:
            tk = m.get("ticker")
            fs = m.get("floor_strike")
            if tk and fs is not None:
                strikes[str(tk)] = Decimal(str(fs))
        if not strikes:
            plan.stand_down = True
            plan.stand_down_reason = "box: chosen hourly generation has no strikes"
            self._journal("box_phase_b_no_strikes", {"reason": plan.stand_down_reason})
            return None
        box_state = BoxState.new(
            close_time=self.close_time,
            anchor_A=anchor_A,
            m15_ticker=a_ticker,
            strikes=strikes,
            shakedown=(not plan.armed),
            T=int(close_epoch(self.close_time)),
        )
        self._current_box_state = box_state
        plan.box_state = box_state
        self._journal(
            "box_state",
            {
                "anchor_A": str(anchor_A),
                "m15_ticker": a_ticker,
                "hourly_event": wake.hourly_leg.event_ticker,
                "ladder_strikes": len(strikes),
                "shakedown": box_state.shakedown,
                "T": box_state.T,
                "ladder_alarm": wake.ladder.alarm,
                "note": "box ladder-map deviation is an A2 alarm only, never a box stand-down",
            },
        )
        recorder = self._build_box_recorder(plan, box_state, a_ticker)
        self._recorder = recorder
        deadline = close_epoch(wake.close_time) + GRACE_SECONDS
        self._connect_not_before = self._connect_gate(wake)
        if self._connect_not_before is not None:
            self._journal(
                "connect_gate",
                {"fifteen_open_time": wake.fifteen_leg.open_time,
                 "connect_not_before": self._connect_not_before,
                 "margin_seconds": _CONNECT_MARGIN_S,
                 "note": "15M leg is 'initialized' at wake; holding the WS dial until ~open_time"},
            )
        self._window_driver(recorder, deadline)
        return recorder

    def _resolve_box_anchor(self, plan: Plan) -> tuple[Decimal, str] | None:
        """PHASE B for the box: poll the 15M market at leg open until its floor_strike materializes,
        returning (anchor_A Decimal, 15M ticker), or None on the poll timeout (EXCL_NO_ANCHOR). Does
        NOT require σ̂/quintile (the box has none). Bounded by open_time+ANCHOR_POLL_TIMEOUT_S and the
        window deadline; ``poll_sleep``/``clock`` are injectable so tests drive it with a fake clock."""
        wake = plan.wake
        assert wake is not None
        try:
            open_epoch = float(close_epoch(wake.fifteen_leg.open_time))
        except (ValueError, TypeError, KeyError, AttributeError):
            open_epoch = float(close_epoch(wake.close_time) - 900)
        window_deadline = float(close_epoch(wake.close_time) + GRACE_SECONDS)
        poll_deadline = min(open_epoch + ANCHOR_POLL_TIMEOUT_S, window_deadline)
        self._journal(
            "box_phase_b_start",
            {"fifteen_ticker": wake.fifteen_leg.primary_ticker,
             "open_time": wake.fifteen_leg.open_time, "open_epoch": open_epoch,
             "poll_deadline": poll_deadline, "poll_interval_s": ANCHOR_POLL_INTERVAL_S},
        )
        polls = 0
        while True:
            now = self.clock()
            if now < open_epoch:
                self._poll_sleep(max(0.0, min(open_epoch - now, poll_deadline - now)))
                if self.clock() < open_epoch:
                    return None
                continue
            markets_15m = self._fetch_current_15m()
            polls += 1
            A, a_ticker = _anchor_at(markets_15m, self.close_time)
            if A is not None and a_ticker:
                self._journal("box_phase_b_anchor",
                              {"anchor_A": A, "anchor_ticker": a_ticker, "polls": polls})
                return Decimal(str(A)), a_ticker
            now = self.clock()
            if now >= poll_deadline:
                return None
            self._poll_sleep(max(0.0, min(ANCHOR_POLL_INTERVAL_S, poll_deadline - now)))

    def _box_subscription_tickers(self, wake: WakeResult, a_ticker: str) -> list[str]:
        """Box subscription: the FULL selected hourly ladder + the 15M market(s), regardless of
        --max-hourly-strikes (spec item 2). The anchor ticker is always included."""
        hourly = list(wake.hourly_leg.market_tickers)
        fifteen = list(wake.fifteen_leg.market_tickers)
        extra = [a_ticker] if a_ticker else []
        return list(dict.fromkeys(hourly + fifteen + extra))

    def _build_box_recorder(self, plan: Plan, box_state: BoxState, a_ticker: str) -> WindowRecorder:
        wake = plan.wake
        assert wake is not None and plan.box_policy is not None
        recorder = BoxWindowRecorder(
            wake, self.journal, plan.box_policy, box_state, clock=self.clock,
            capture_tops=(not plan.armed), on_action=self._on_box_action,
            on_book_event=self._box_on_book_event,
        )
        tickers = self._box_subscription_tickers(wake, a_ticker)
        recorder.ws_client = KalshiWebSocketClient(
            proxy_auth=self.proxy,
            tickers=tickers,
            callbacks=recorder.callbacks,
            include_private=plan.armed,
            record=recorder.tap,
            clock=self.clock,
        )
        return recorder

    # -----------------------------------------------------------------------
    # BOX order routing + post-fill policy (armed-only; dry/shakedown emit WouldFire)
    # -----------------------------------------------------------------------
    def _on_box_action(self, action: Action) -> None:
        if action.kind != FIRE or not self.armed or self.executor is None:
            return
        sel = self._current_box_state.fired_selection if self._current_box_state else None
        hourly_ticker = sel.hourly_ticker if sel is not None else action.legs[0].ticker
        m15_ticker = sel.m15_ticker if sel is not None else action.legs[1].ticker
        legs = tuple(
            IntentLeg(
                ticker=lg.ticker, side=lg.side, action="buy",
                count=int(lg.count), limit_price=lg.limit_price,
                client_order_id=new_client_order_id(),
                exchange_index=self._xi(lg.ticker),
            )
            for lg in action.legs
        )
        intent = Intent(window=self.close_time, source=WIDE_BOX, purpose=PURPOSE_ENTRY,
                        legs=legs, t_minus_s=action.t_minus_s)
        # Ledger slot mapping (documented): the ledger's high/low are just two storage slots. For the
        # box, high_ticker <- the hourly leg, low_ticker <- the 15M leg (there is no strike ordering
        # between a $-strike hourly market and the 15M anchor market). The held sides are captured
        # from the entry legs by record_intent (high_side=hourly_side, low_side=m15_side).
        self.ledger_state = record_intent(
            new_ledger(self.close_time, WIDE_BOX, high_ticker=hourly_ticker, low_ticker=m15_ticker),
            intent,
        )
        result = self.executor.execute(intent, t_minus_s=action.t_minus_s)
        for r in result.responses:
            self.ledger_state = record_response(self.ledger_state, r)
        self._box_post_entry(action.t_minus_s if action.t_minus_s is not None else 0.0)

    def _box_post_entry(self, t_minus_s: float) -> None:
        """The box post-fill policy (spec item 4 — REPLACES the corridor rebalance protocol entirely:
        no retries, no rebalance, no I1 ceiling). Both legs filled -> hold to settlement (the $1 floor
        is booked at close, +$1 backfilled if pinned). Exactly one leg filled -> flatten it reduce-only
        at the best bid (any price); the first attempt fires now, retries are EVENT-DRIVEN on later
        book frames (F4), 3 attempts max; no bid -> A_FLATTEN_NO_BID + hold. Then S1_box (units +
        booked-cost) and the A5 one-legged alarm.

        F3: ``_box_one_legged`` records the ENTRY quality (exactly one leg filled) and is NEVER reset by
        a later flatten; the flatten OUTCOME is recorded separately in ``_box_flatten_filled``."""
        from service.stops import S1_ARITH, check_s1_box

        assert self.ledger_state is not None and self.stops is not None
        ls = self.ledger_state
        hi_net, lo_net = ls.net("high"), ls.net("low")
        both = hi_net > 0 and lo_net > 0
        one_leg = (hi_net > 0) != (lo_net > 0)
        self._box_one_legged = one_leg  # F3: entry quality, latched (never reset by the flatten)
        if both:
            self._journal(
                "box_post_fill",
                {"outcome": "both_filled_hold",
                 "realized_at_close": str(ls.realized_at_close()),
                 "matched_pairs": str(ls.matched_pairs()),
                 "note": "box pair holds to settlement; $1 floor booked, +$1 backfill if pinned"},
            )
        elif one_leg:
            self._box_begin_flatten(ls, t_minus_s)
        else:
            self._journal("box_post_fill", {"outcome": "no_fill", "note": "neither box leg filled"})
        # S1_box: freeze the DAY on a units/booked-cost violation, but do NOT run the corridor
        # position_policy (ledger_state omitted from trip()): a both-filled box is floor-protected and
        # HELD; a one-legged box's flatten is in flight / already resolved above.
        reason = check_s1_box(self.ledger_state, self._box_policy.pair_cost_max)
        if reason:
            self._journal("box_s1", {"reason": reason})
            self.stops.trip(S1_ARITH, reason)
        self._box_a5_alarm()

    def _box_leg_bid(self, ticker: str, side: str) -> Decimal | None:
        """The current best bid to flatten a held ``side`` on ``ticker`` (sell YES at yes_bid; sell NO
        at no_bid), from the recorder's live book. None if unknown."""
        if self._recorder is None:
            return None
        book = self._recorder.books.get(ticker)
        if book is None:
            return None
        top = book.top_of_book()
        return top.yes_bid if side == "yes" else top.no_bid

    def _box_begin_flatten(self, ls: LedgerState, t_minus_s: float) -> None:
        """Start the one-legged flatten: record the pending state and issue the FIRST attempt now (it
        prices the entry-time bid). If it misses, ``_pending_flatten`` stays set and later book frames
        drive the retries (F4). The flatten is the ONLY order class after the entry."""
        which = "high" if ls.net("high") > 0 else "low"
        pos = ls.position(which)
        if pos is None or pos.net <= 0:
            return
        ticker, side, count = pos.ticker, pos.side, int(pos.net)
        self._journal(
            "box_flatten",
            {"stage": "start", "ticker": ticker, "side": side, "count": count,
             "note": f"one-legged box -> reduce-only flatten at best bid (any price, "
                     f"{_FLATTEN_MAX_ATTEMPTS} attempts max, retries event-driven)"},
        )
        self._pending_flatten = {
            "which": which, "ticker": ticker, "side": side, "count": count,
            "attempts": 0, "last_ts": None,
        }
        # First attempt now, at the fresh entry-time bid. server_ts is event-derived (T - t_minus_s).
        server_ts = (self._current_box_state.T - t_minus_s) if self._current_box_state else None
        self._box_flatten_attempt(t_minus_s, server_ts)

    def _box_flatten_attempt(self, t_minus_s: float, server_ts: float | None) -> None:
        """Execute one reduce-only flatten attempt against the CURRENT best bid. Clears
        ``_pending_flatten`` when the leg goes flat, when there is no bid (A_FLATTEN_NO_BID), when the
        no-orders-to-settle cutoff is reached, or when all attempts are exhausted (A_FLATTEN_EXHAUSTED).
        Otherwise the pending state persists for the next event-driven retry."""
        from service.stops import PositionAction, build_flatten_intent

        pf = self._pending_flatten
        if pf is None:
            return
        which = pf["which"]
        # Already flat (a prior attempt filled, or the leg never existed) -> done.
        if self.ledger_state is None or self.ledger_state.net(which) <= 0:
            self._pending_flatten = None
            return
        ticker, side, count = pf["ticker"], pf["side"], int(pf["count"])
        # The no-orders cutoff (t < no_orders_after_s_to_settle, ~1s) STILL stops flatten attempts:
        # hold the naked leg to settlement rather than pound a refusing executor.
        cutoff = self._box_policy.no_orders_after_s_to_settle if self._box_policy is not None else 1
        if t_minus_s is not None and t_minus_s < cutoff:
            self._journal(
                "box_flatten",
                {"stage": "cutoff_hold", "ticker": ticker, "t_minus_s": t_minus_s,
                 "attempts": pf["attempts"],
                 "note": "within no-orders-to-settle cutoff -> hold naked to settlement"},
            )
            self._box_flatten_filled = False
            self._pending_flatten = None
            return
        bid = self._box_leg_bid(ticker, side)
        if bid is None:
            self.stops.raise_alarm(
                "A_FLATTEN_NO_BID",
                f"box flatten of {ticker} skipped: no bid to price against (held, alerting)",
                {"ticker": ticker, "count": count, "attempt": pf["attempts"] + 1},
            )
            self._journal("box_flatten",
                          {"stage": "no_bid_hold", "ticker": ticker, "attempt": pf["attempts"] + 1})
            self._box_flatten_filled = False
            self._pending_flatten = None
            return
        attempt_no = pf["attempts"] + 1
        action = PositionAction("flatten", ticker, side, count, "box one-legged flatten")
        intent = build_flatten_intent(action, self.close_time, WIDE_BOX, bid, new_client_order_id(),
                                      exchange_index=self._xi(ticker))
        res = self.executor.execute(intent, t_minus_s=t_minus_s, stop_authorized=True)
        # Book the flatten (intent + fills) as a round-trip via the SAME folding path the corridor's
        # stop-authorized flattens use (repairs/instruments F1): a filled flatten reduces net -> drops
        # out of unsettled_legs, never a phantom naked leg.
        self._fold_flatten_response(intent, res)
        pf["attempts"] = attempt_no
        pf["last_ts"] = server_ts
        if self.ledger_state.net(which) <= 0:
            self._box_flatten_filled = True  # F3: the flatten OUTCOME (entry one-legged flag stays)
            self._pending_flatten = None
            self._journal("box_flatten",
                          {"stage": "flat", "ticker": ticker, "attempt": attempt_no, "bid": str(bid)})
            return
        if attempt_no >= _FLATTEN_MAX_ATTEMPTS:
            self.stops.raise_alarm(
                A_FLATTEN_EXHAUSTED,
                f"box flatten of {ticker} missed all {_FLATTEN_MAX_ATTEMPTS} attempts (held naked)",
                {"ticker": ticker, "count": count, "attempts": attempt_no},
            )
            self._journal(
                "box_flatten",
                {"stage": "giveup_hold", "ticker": ticker, "attempts": attempt_no,
                 "remaining": str(self.ledger_state.net(which)),
                 "note": "flatten missed after all attempts -> hold to settlement (naked)"},
            )
            self._box_flatten_filled = False
            self._pending_flatten = None
            return
        # Missed but attempts remain: stay pending; a later book frame drives the next attempt.
        self._journal(
            "box_flatten",
            {"stage": "miss_retry", "ticker": ticker, "attempt": attempt_no, "bid": str(bid),
             "remaining": str(self.ledger_state.net(which))},
        )

    def _box_on_book_event(self, market: str, server_ts: float) -> None:
        """F4 retry trigger: called by the recorder after each subscribed book frame. Issues the next
        pending flatten attempt when the frame is a LATER event and either (a) it is the held ticker
        and its best bid is present, or (b) >= _FLATTEN_RETRY_MIN_ELAPSED_S of engine time has passed
        since the last attempt (whichever first). The 'later event' guard stops a retry from re-pricing
        the SAME frozen frame the previous attempt already saw (the review's F4 bug)."""
        pf = self._pending_flatten
        if pf is None:
            return
        last_ts = pf["last_ts"]
        if last_ts is not None and server_ts <= last_ts:
            return  # same or earlier frame -> not a fresh bid; wait for a later one
        held_has_bid = (
            market == pf["ticker"] and self._box_leg_bid(pf["ticker"], pf["side"]) is not None
        )
        elapsed = None if last_ts is None else (server_ts - last_ts)
        time_fallback = elapsed is not None and elapsed >= _FLATTEN_RETRY_MIN_ELAPSED_S
        if not (held_has_bid or time_fallback):
            return
        t_minus_s = (self._current_box_state.T - server_ts) if self._current_box_state else None
        self._box_flatten_attempt(t_minus_s, server_ts)

    def _box_a5_alarm(self) -> None:
        """A5: over the rolling last 20 box fires (this window included), if one-legged/fires > 0.10,
        raise an alarm (notify + journal, keep running)."""
        if self.stops is None:
            return
        try:
            past = load_entries(self.ledger_path)
        except Exception as e:  # noqa: BLE001 - a ledger read must never break the post-fill path
            logger.warning("[RUN] A5 ledger read failed: %s", e)
            past = []
        synthetic = {"fires": 1, "fired_source": WIDE_BOX, "box_one_legged": self._box_one_legged}
        rate, one_legged, total = box_one_legged_rate(past + [synthetic], n=20)
        if rate is not None and rate > A5_ONE_LEGGED_RATE_MAX:
            self.stops.raise_alarm(
                A5_ONE_LEGGED,
                f"box one-legged rate {rate} ({one_legged}/{total}) > {A5_ONE_LEGGED_RATE_MAX}",
                {"one_legged": one_legged, "fires": total, "rate": str(rate)},
            )
            self._journal("box_a5",
                          {"one_legged": one_legged, "fires": total, "rate": str(rate)})

    # -----------------------------------------------------------------------
    # Settlement-backfill automation (spec item 5) — read-only, idempotent, fail-closed
    # -----------------------------------------------------------------------
    def _fetch_market_result(self, ticker: str) -> str | None:
        """The settled market result ('yes'/'no') for ``ticker`` via the proxy /markets (read-only),
        or None if not settled / unavailable (injectable for tests). Fail-closed to None."""
        if self._market_result_getter is not None:
            try:
                return self._market_result_getter(ticker)
            except Exception as e:  # noqa: BLE001
                logger.warning("[RUN] market-result fetch failed for %s: %s", ticker, e)
                return None
        try:
            # Single-market endpoint (/markets/{ticker}) returns {"market": {...}} for the exact
            # ticker. The list endpoint (/markets?ticker=) IGNORES the singular param and returns
            # 100 unrelated markets, so _parse_market_result's exact-ticker guard never matched and
            # the sweep silently never settled. (Plural ?tickers= also works but this shape is exact.)
            resp = self.proxy.rest_get(f"/markets/{ticker}")
        except Exception as e:  # noqa: BLE001
            logger.warning("[RUN] market-result fetch failed for %s: %s", ticker, e)
            return None
        return _parse_market_result(resp, ticker)

    def _settlement_backfill_sweep(self) -> dict[str, dict[str, Any]]:
        """At reconcile-first, backfill any prior ledger row still marked realized_unsettled once its
        held tickers have settled. Idempotent (skips already-backfilled windows) and fail-closed (an
        absent/unsettled result just waits for a later wake). Never raises out of startup.

        Returns the PENDING PICTURE it already computes — ``{window: {"legs": [(ticker, side, count,
        result_or_None), ...], "close_time": ...}}`` — for every row still ``realized_unsettled`` after
        the sweep (results fetched via ``_fetch_market_result``; None = not yet finalized). The S4 wake
        block uses it to bound the balance under EVERY resolution of the unfinalized legs (B2/B3) so a
        pending settlement can never move the number a latch is decided on (the 2026-08-28 16:40Z bug).
        Journaling is unchanged."""
        from service.ledger import settlement_payoff

        pending_picture: dict[str, dict[str, Any]] = {}
        try:
            entries = load_entries(self.ledger_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[RUN] settlement-backfill: ledger read failed: %s", e)
            return pending_picture
        pending: dict[str, dict[str, Any]] = {}
        for e in entries:
            if e.get("realized_unsettled") and e.get("unsettled_legs"):
                pending[str(e.get("close_time"))] = e  # most-recent row per window wins
        for window, entry in pending.items():
            if already_backfilled(entries, window):
                continue
            legs = entry.get("unsettled_legs") or []
            results: dict[str, str] = {}
            pending_leg: str | None = None
            for leg in legs:
                tk = leg["ticker"] if isinstance(leg, dict) else leg[0]
                res = self._fetch_market_result(tk)
                if res not in ("yes", "no"):
                    # Do NOT break: query EVERY leg so ``results`` holds each genuinely settled leg and
                    # a leg reads None in the pending picture iff it is truly unfinalized. Breaking on
                    # the first laggard would mark all legs AFTER it None even when settled, so
                    # s4_pending_value would credit an optimistic $1 to a settled leg and UNDERSTATE the
                    # loss (a certain >= cap loss could then read ``pending`` instead of ``latch``).
                    # ``pending_leg`` keeps the FIRST unfinalized leg for the journal (unchanged).
                    pending_leg = pending_leg or tk
                    continue
                results[tk] = res
            if pending_leg is not None:
                # A pending row we cannot yet settle (a leg unsettled/unavailable). Make the silent
                # wait visible in the journal — compact, and at most once per window per wake since
                # the sweep itself runs once per wake.
                tickers = [leg["ticker"] if isinstance(leg, dict) else leg[0] for leg in legs]
                self._journal(
                    "settlement_backfill_pending",
                    {"window": window, "tickers": tickers, "unsettled_leg": pending_leg,
                     "settled_legs": sorted(results.keys())},
                )
                # Record the pending picture: each leg with its fetched result (None = not finalized,
                # e.g. the leg that stopped the sweep and any leg after it). s4_pending_value counts
                # only the None legs (optimistic $1/leg), so a settled 'no' leg contributes nothing.
                def _leg_field(leg: Any, i: int) -> Any:
                    return leg[i] if isinstance(leg, (list, tuple)) else None
                picture_legs: list[tuple[Any, Any, Any, str | None]] = []
                for leg in legs:
                    if isinstance(leg, dict):
                        tk = leg.get("ticker"); side = leg.get("side"); cnt = leg.get("count")
                    else:
                        tk = _leg_field(leg, 0); side = _leg_field(leg, 1); cnt = _leg_field(leg, 2)
                    picture_legs.append((tk, side, cnt, results.get(str(tk))))
                pending_picture[str(window)] = {
                    "close_time": str(entry.get("close_time", window)),
                    "legs": picture_legs,
                }
                continue
            try:
                payoff = settlement_payoff(legs, results)
            except Exception as ex:  # noqa: BLE001 - a malformed row must not break startup
                logger.warning("[RUN] settlement-backfill payoff failed for %s: %s", window, ex)
                continue
            bf = build_backfill_entry(entry, results, payoff, self.clock())
            try:
                append_entry(bf, self.ledger_path)
            except Exception as ex:  # noqa: BLE001
                logger.warning("[RUN] settlement-backfill append failed for %s: %s", window, ex)
                continue
            entries.append(bf)  # so already_backfilled sees it within this sweep
            self._journal(
                "settlement_backfill",
                {"window": window, "results": results, "settlement_payoff": str(payoff),
                 "floor_netted": bf.get("floor_netted"), "realized_delta": bf.get("realized_delta")},
            )
        return pending_picture

    def _build_recorder(self, plan: Plan) -> WindowRecorder:
        wake, policy, state = plan.wake, plan.policy, plan.state
        assert wake is not None and policy is not None and state is not None
        if plan.effective_mode == "shakedown":
            recorder: WindowRecorder = ShakedownRecorder(
                wake, self.journal, policy, state, clock=self.clock, capture_tops=True
            )
        else:  # dry or armed — never ShakedownRecorder
            recorder = LiveWindowRecorder(
                wake, self.journal, policy, state, clock=self.clock,
                capture_tops=(not plan.armed), on_action=self._on_signal_action,
            )
        tickers = self._subscription_tickers(wake, state)
        recorder.ws_client = KalshiWebSocketClient(
            proxy_auth=self.proxy,
            tickers=tickers,
            callbacks=recorder.callbacks,
            include_private=plan.armed,
            record=recorder.tap,
            clock=self.clock,
        )
        return recorder

    def _subscription_tickers(self, wake: WakeResult, state: WindowState) -> list[str]:
        """The paired decision legs (ALWAYS) + the 15M market(s); the rest of the hourly ladder only
        when --max-hourly-strikes widens it. The decision pair is guaranteed present even under a
        cap (a bare first-N slice could drop the paired hourly strike)."""
        decision = [t for t in (state.high_ticker, state.low_ticker) if t]
        fifteen = list(wake.fifteen_leg.market_tickers)
        # F1: subscribe the CHOSEN market's generation ladder (for --max-hourly widening), not
        # necessarily the selected smallest-window one; the decision pair is always included below.
        chosen_leg = self._chosen_hourly_leg(wake, state.high_ticker, state.low_ticker)
        hourly = list(chosen_leg.market_tickers)
        if self.max_hourly is None:
            hourly_sub = hourly
        elif self.max_hourly <= 0:
            hourly_sub = [t for t in decision if t in hourly]
        else:
            hourly_sub = hourly[: self.max_hourly]
            for t in decision:
                if t in hourly and t not in hourly_sub:
                    hourly_sub.append(t)
        return list(dict.fromkeys(decision + fifteen + hourly_sub))

    # -----------------------------------------------------------------------
    # ARMED order routing (live-only; dry never emits FIRE)
    # -----------------------------------------------------------------------
    def _on_signal_action(self, action: Action) -> None:
        if action.kind != FIRE or not self.armed or self.executor is None:
            return
        state = self._current_state
        assert state is not None
        source = action.source or ""
        legs = tuple(
            IntentLeg(
                ticker=lg.ticker, side=lg.side, action="buy",
                count=int(lg.count) * self.pairs, limit_price=lg.limit_price,
                client_order_id=new_client_order_id(),
                exchange_index=self._xi(lg.ticker),
            )
            for lg in action.legs
        )
        intent = Intent(window=self.close_time, source=source, purpose=PURPOSE_ENTRY,
                        legs=legs, t_minus_s=action.t_minus_s)
        self.ledger_state = record_intent(
            new_ledger(self.close_time, source, state.high_ticker, state.low_ticker), intent
        )
        result = self.executor.execute(intent, t_minus_s=action.t_minus_s)
        for r in result.responses:
            self.ledger_state = record_response(self.ledger_state, r)
        self._post_entry_checks(intent, result, action.t_minus_s)

    def _fold_flatten_response(self, intent: Intent, result: Any) -> None:
        """F1: fold a stop-authorized flatten (its intent + fills) into the in-process ledger so a
        filled flatten is booked as a round-trip (the sold leg reduces net, dropping out of
        unsettled_legs and out of the naked cash-outlay booking). Called by StopController.trip for
        every flatten it dispatches."""
        if self.ledger_state is None:
            return
        self.ledger_state = record_intent(self.ledger_state, intent)
        for r in getattr(result, "responses", ()):
            self.ledger_state = record_response(self.ledger_state, r)

    def _post_entry_checks(self, intent: Intent, result: Any, t_minus_s: float | None) -> None:
        from service.stops import S1_ARITH, check_s1, check_slippage_alarms

        # A1 slippage alarms
        for note in check_slippage_alarms(intent, result.responses, self.stops.config):
            self.stops.raise_alarm(note.kind, note.reason, note.detail)
        # S1 arithmetic (n=1) on a completed sub-$1 pair
        assert self.ledger_state is not None
        reason = check_s1(self.ledger_state)
        if reason:
            self.stops.trip(S1_ARITH, reason, ledger_state=self.ledger_state, bids=self._bids(),
                            exchange_index=self._exchange_index_by_ticker)
            return
        # imbalance protocol
        self._maybe_rebalance(t_minus_s if t_minus_s is not None else 0.0)
        # F5: S3 reconcile once the entry + any rebalance have settled (no order in flight here).
        self._s3_poll_once()

    def _leg_top(self, which: str) -> TopOfBook | None:
        state = self._current_state
        if state is None or self._recorder is None:
            return None
        ticker = state.high_ticker if which == "high" else state.low_ticker
        book = self._recorder.books.get(ticker)
        return book.top_of_book() if book is not None else None

    @staticmethod
    def _held_ask(top: TopOfBook | None, side: str | None) -> Decimal | None:
        if top is None or side is None:
            return None
        return top.yes_ask if side == "yes" else top.no_ask

    @staticmethod
    def _held_bid(top: TopOfBook | None, side: str | None) -> Decimal | None:
        if top is None or side is None:
            return None
        return top.yes_bid if side == "yes" else top.no_bid

    def _rebalance_quotes(self) -> RebalanceQuotes:
        ls = self.ledger_state
        assert ls is not None
        hi, lo = self._leg_top("high"), self._leg_top("low")
        return RebalanceQuotes(
            high_buy=self._held_ask(hi, ls.high_side),
            high_sell=self._held_bid(hi, ls.high_side),
            low_buy=self._held_ask(lo, ls.low_side),
            low_sell=self._held_bid(lo, ls.low_side),
        )

    def _bids(self) -> dict[str, Decimal]:
        ls = self.ledger_state
        out: dict[str, Decimal] = {}
        if ls is None:
            return out
        hb = self._held_bid(self._leg_top("high"), ls.high_side)
        lb = self._held_bid(self._leg_top("low"), ls.low_side)
        if hb is not None:
            out[ls.high_ticker] = hb
        if lb is not None:
            out[ls.low_ticker] = lb
        return out

    def _xi(self, ticker: str) -> int | None:
        """The wake-captured exchange_index (shard) for ``ticker``, or None if the wake map did not
        carry it (fail closed: the Executor refuses an unrouted leg)."""
        return self._exchange_index_by_ticker.get(ticker)

    def _ceiling_per_pair(self, policy: PolicyParams, source: str) -> Decimal:
        if source == SUB_DOLLAR_FLIP:
            return policy.imbalance.pair_cost_ceiling_sub1
        state = self._current_state
        return state.fair_strangle_q if state is not None else Decimal(0)

    def _maybe_rebalance(self, t_minus_s: float) -> None:
        from service.stops import S2_IMBALANCE

        assert self.ledger_state is not None and self.stops is not None
        policy_params = self._plan_policy()
        for _ in range(_MAX_REBALANCE_STEPS):
            imb = detect_imbalance(self.ledger_state)
            if imb is None:
                break
            quotes = self._rebalance_quotes()
            ceiling = self._ceiling_per_pair(policy_params, self.ledger_state.source)
            prop = propose_rebalance(self.ledger_state, policy_params, t_minus_s, quotes, ceiling)
            if isinstance(prop, Balanced):
                break
            if isinstance(prop, RideToSettlement):
                self._journal("imbalance_ride", {"reason": prop.reason})
                break
            if isinstance(prop, GiveUp):
                self.stops.trip(S2_IMBALANCE, prop.reason,
                                ledger_state=self.ledger_state, bids=self._bids(),
                                exchange_index=self._exchange_index_by_ticker)
                break
            purpose = PURPOSE_REBALANCE_BUY if isinstance(prop, RetryBuy) else PURPOSE_REBALANCE_SELL
            action = "buy" if isinstance(prop, RetryBuy) else "sell"
            reduce_only = True if isinstance(prop, SellDown) else None
            leg = IntentLeg(
                ticker=prop.ticker, side=prop.side, action=action, count=int(prop.count),
                limit_price=prop.limit_price, client_order_id=new_client_order_id(),
                reduce_only=reduce_only, exchange_index=self._xi(prop.ticker),
            )
            intent = Intent(window=self.close_time, source=self.ledger_state.source,
                            purpose=purpose, legs=(leg,), t_minus_s=t_minus_s)
            self._journal("imbalance_proposal",
                          {"kind": type(prop).__name__, "reason": prop.reason})
            self.ledger_state = record_intent(self.ledger_state, intent)
            res = self.executor.execute(intent, t_minus_s=t_minus_s)
            for r in res.responses:
                self.ledger_state = record_response(self.ledger_state, r)

    def _plan_policy(self) -> PolicyParams:
        # policy is captured on the plan; re-load defensively if unavailable
        if getattr(self, "_policy_cache", None) is None:
            self._policy_cache = load_policy(self.policy_path)
        return self._policy_cache

    # -----------------------------------------------------------------------
    # WS driver (default): bounded exponential-backoff re-dial, clean give-up at deadline (F6)
    # -----------------------------------------------------------------------
    def _connect_gate(self, wake: WakeResult) -> float | None:
        """The earliest clock time to dial the WS: (15M leg open_time - _CONNECT_MARGIN_S). Returns
        None when that instant is already past (the leg is already open -> dial immediately) or the
        open_time is unparseable (fail-open to dialing; wake already vetted the leg is live and
        freshness gates still forbid any decision until real book data flows). See _CONNECT_MARGIN_S:
        run_window discovers at :40 while the 15M leg is still 'initialized', so this holds the dial
        until ~:45 (open_time) rather than subscribing a not-yet-open market."""
        try:
            open_epoch = close_epoch(wake.fifteen_leg.open_time)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        gate = open_epoch - _CONNECT_MARGIN_S
        return gate if gate > self.clock() else None

    async def _await_connect_gate(
        self, connect_not_before: float | None, deadline: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        """Hold before the FIRST WS dial until the 15M leg is about to open. A None gate (leg already
        open, or unparseable open_time) returns immediately. Bounded by the deadline so a bad gate can
        never park us past window end. `sleep` is injected so tests drive it with a fake clock; the
        window deadline/settle logic is unchanged — only the dial START is delayed."""
        if connect_not_before is None:
            return
        while True:
            now = self.clock()
            remaining = min(connect_not_before, deadline) - now
            if remaining <= 0:
                return
            await sleep(remaining)

    def _default_window_driver(self, recorder: WindowRecorder, deadline: float) -> None:
        asyncio.run(
            self._run_ws_window(recorder, deadline, connect_not_before=self._connect_not_before)
        )

    async def _run_ws_window(
        self,
        recorder: WindowRecorder,
        deadline: float,
        *,
        connect_not_before: float | None = None,
        lag_threshold: float = DEFAULT_LAG_THRESHOLD,
        silence_threshold: float = DEFAULT_SILENCE_THRESHOLD,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> WindowRecorder:
        ws = recorder.ws_client
        assert ws is not None, "recorder.ws_client must be set before running the window"
        # Hold the dial until the 15M leg is about to open (live finding 2026-08-21). Done BEFORE the
        # s3 poll task so nothing spins during the wait; the deadline bounds it.
        await self._await_connect_gate(connect_not_before, deadline, sleep)
        backoff = _INITIAL_BACKOFF
        consecutive_failures = 0
        # F5: armed mode runs a periodic gated S3 reconcile alongside the WS loop. The poll's blocking
        # rest_get is off-loaded to a thread; _s3_poll_once defers whenever an order is in flight.
        s3_task: asyncio.Task | None = None
        if self.armed and self.reconciler is not None:
            s3_task = asyncio.create_task(self._s3_poll_loop(deadline, sleep))
        try:
            return await self._dial_loop(recorder, ws, deadline, backoff, consecutive_failures,
                                         lag_threshold, silence_threshold, poll_seconds, sleep)
        finally:
            if s3_task is not None:
                s3_task.cancel()
                try:
                    await s3_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _s3_poll_loop(
        self, deadline: float, sleep: Callable[[float], Awaitable[None]]
    ) -> None:
        loop = asyncio.get_event_loop()
        while self.clock() < deadline:
            await sleep(3.0)
            if self.stops is not None and self.stops.state.is_stopped:
                return
            try:
                await loop.run_in_executor(None, self._s3_poll_once)
            except Exception as e:  # noqa: BLE001
                logger.warning("[RUN] S3 poll loop error: %s", e)

    async def _dial_loop(
        self, recorder: WindowRecorder, ws: Any, deadline: float, backoff: float,
        consecutive_failures: int, lag_threshold: float, silence_threshold: float,
        poll_seconds: float, sleep: Callable[[float], Awaitable[None]],
    ) -> WindowRecorder:
        while recorder.clock() < deadline:
            stop = asyncio.Event()

            async def supervise() -> None:
                while not stop.is_set():
                    await sleep(poll_seconds)
                    if stop.is_set():
                        return
                    act = watchdog_action(
                        recorder.clock(), deadline, ws.data_age_seconds(),
                        ws.silence_seconds(), lag_threshold, silence_threshold,
                    )
                    if act == CONTINUE:
                        continue
                    if act == FORCE_CLOSE:
                        recorder.record_alarm(
                            "watchdog_stale",
                            {"data_age_seconds": ws.data_age_seconds(),
                             "silence_seconds": ws.silence_seconds()},
                        )
                    await ws.force_close()
                    return

            sup = asyncio.create_task(supervise())
            connected = False
            try:
                await ws.connect()
                connected = True
            except Exception as e:  # noqa: BLE001 - a dial failure is journaled then backed off
                logger.warning("[RUN] WS dial error: %s", e)
                recorder.record_alarm("ws_error", {"error": str(e)})
            finally:
                stop.set()
                await sup
            if recorder.clock() >= deadline:
                break
            recorder.mark_all_suspect()
            if connected:
                backoff = _INITIAL_BACKOFF
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_REDIALS:
                recorder.record_alarm(
                    "ws_giveup",
                    {"consecutive_failures": consecutive_failures,
                     "note": "re-dial cap reached; giving up until window end (F6)"},
                )
                break
            wait = min(backoff, max(0.0, deadline - recorder.clock()))
            if wait > 0:
                await sleep(wait)
            backoff = min(backoff * 2, _MAX_BACKOFF)
        return recorder

    # -----------------------------------------------------------------------
    # finalize — flush + pilot-ledger summary (step g)
    # -----------------------------------------------------------------------
    def _finalize(self, plan: Plan, recorder: WindowRecorder | None, exit_code: int) -> None:
        if self._finalized:
            return
        self._finalized = True
        journal_path = os.path.join(self.journal_dir, _safe_close(self.close_time) + ".jsonl")
        summary_path = os.path.join(self.journal_dir, "summary.jsonl")
        records = 0
        try:
            if recorder is not None:
                summary = recorder.flush(journal_path, summary_path)
                records = int(summary.get("records", 0))
            else:
                records = self.journal.flush(journal_path)
        except Exception as e:  # noqa: BLE001 - finalize must never raise
            logger.error("[RUN] journal flush failed: %s", e)
        try:
            if plan.strategy == "box":
                entry = self._build_box_ledger_entry(plan, recorder, exit_code, journal_path, records)
            else:
                entry = self._build_ledger_entry(plan, recorder, exit_code, journal_path, records)
            append_entry(entry, self.ledger_path)
        except Exception as e:  # noqa: BLE001
            logger.error("[RUN] pilot-ledger append failed: %s", e)

    def _build_ledger_entry(
        self, plan: Plan, recorder: WindowRecorder | None, exit_code: int,
        journal_path: str, records: int,
    ) -> dict[str, Any]:
        actions: list[Action] = []
        if recorder is not None and hasattr(recorder, "driver"):
            actions = list(recorder.driver.actions)
        would_fires = sum(1 for a in actions if a.kind == WOULD_FIRE)
        fires = sum(1 for a in actions if a.kind == FIRE)
        fired_source = plan.state.fired_source if plan.state is not None else None

        # fills / imbalance / realized / slippage / S1 (armed only produces a ledger_state)
        fills: list[dict[str, Any]] = []
        filled = False
        imbalance_unresolved = False
        realized_delta = Decimal(0)
        realized_unsettled = False
        slippage: list[str] = []
        imbalance_events = 0
        s1_violation = False
        second_pair_book_walk: Any = None
        unsettled_legs: list[dict[str, Any]] = []
        ls = self.ledger_state
        if ls is not None:
            rec = fills_record(ls)
            filled = bool(rec["filled"])
            imbalance_unresolved = bool(rec["imbalance"])
            for lg in rec["legs"]:
                fills.append({
                    "ticker": lg["ticker"], "side": lg["side"],
                    "count": (int(lg["count"]) if isinstance(lg["count"], (int, Decimal)) else lg["count"]),
                    "avg_price": None if lg["avg_price"] is None else str(lg["avg_price"]),
                    "avg_fee": str(lg["avg_fee"]),
                })
            # BUG-2 repair: book a realized_delta for EVERY window with any fill.
            #   * ``realized_at_close`` = settlement-independent cash flow (proceeds - costs, all fees)
            #     + the sub-$1 flip's GUARANTEED >= $1/pair floor on matched pairs. This captures BOTH
            #     the flatten/round-trip case (formerly booked 0 when matched_pairs == 0) AND the naked
            #     leg's cash OUTLAY (formerly booked 0), always in the SAFE direction (never an assumed
            #     win). A Q1-STRANGLE has no floor, so its matched pairs contribute only cash flow here.
            #   * ``unsettled_legs`` are the held legs whose settlement PAYOFF is still pending; they
            #     ride into the pilot-ledger entry so the settlement backfill (pilot_ledger backfill)
            #     can add the winning payoff once the market result is known. S4 never sees an assumed
            #     win: the close booking is the worst case, the backfill only ever corrects it UP.
            if rec["any_fill"]:
                realized_delta = rec["realized_at_close"]
                unsettled_legs = list(rec["unsettled_legs"])
                realized_unsettled = bool(unsettled_legs)
            imbalance_events = sum(
                1 for i in ls.intents if i.purpose in (PURPOSE_REBALANCE_BUY, PURPOSE_REBALANCE_SELL)
            )
            from service.stops import check_s1

            s1_violation = check_s1(ls) is not None
            slippage = self._entry_slippage(ls)
            if self.pairs >= 2 and filled:
                second_pair_book_walk = self._book_walk(ls)

        # alarms / stops from the StopController state
        alarms: list[dict[str, str]] = []
        stops: list[dict[str, str]] = []
        if self.stops is not None:
            for n in self.stops.state.alarms:
                alarms.append({"kind": n.kind, "reason": n.reason})
            for n in self.stops.state.notifications:
                if n.kind.startswith("S"):
                    stops.append({"kind": n.kind, "reason": n.reason})
        if plan.wake is not None and plan.wake.ladder.alarm:
            alarms.append({"kind": "A2", "reason": f"ladder-map: {plan.wake.ladder.reason}"})

        orders_attempted = int(getattr(self.executor, "dispatch_count", 0)) if self.executor else 0
        guard_trips = int(self.stops.state.guard_trips) if self.stops is not None else 0
        s4_running = realized_delta
        try:
            s4_running = s4_running_loss(load_entries(self.ledger_path)) + realized_delta
        except Exception:  # noqa: BLE001
            pass

        arm = plan.arm_decision
        entry: dict[str, Any] = {
            "close_time": self.close_time,
            "strategy": plan.strategy,
            "mode": plan.effective_mode,
            "requested_mode": resolve_mode(self.cli_mode, self.mode_txt_path),
            "pairs": self.pairs,
            "armed": plan.armed,
            "degraded_to_dry": plan.degraded,
            "degrade_reason": plan.degrade_reason,
            "arm_decision": None if arm is None else {"would_arm": arm.armed, "reasons": list(arm.reasons)},
            "stand_down": plan.stand_down,
            "stand_down_reason": plan.stand_down_reason,
            "exit_code": exit_code,
            "policy_sha": plan.policy.sha256 if plan.policy is not None else None,
            "ladder_ok": plan.wake.ladder.ok if plan.wake is not None else None,
            "strangle_disabled": (plan.state.strangle_disabled if plan.state is not None else None),
            "quintile": plan.state.quintile if plan.state is not None else None,
            "quintile_reproduced": (plan.outcome.ok if plan.outcome is not None else None),
            "G": (None if plan.state is None or plan.state.G is None else str(plan.state.G)),
            "sigma_hat": (plan.state.sigma_hat if plan.state is not None else None),
            "high_ticker": plan.state.high_ticker if plan.state is not None else None,
            "low_ticker": plan.state.low_ticker if plan.state is not None else None,
            "signals": would_fires + fires,
            "would_fires": would_fires,
            "fires": fires,
            "fired_source": fired_source,
            "sub1_entry": fired_source == SUB_DOLLAR_FLIP,
            "orders_attempted": orders_attempted,
            "fills": fills,
            "filled": filled,
            "imbalance_events": imbalance_events,
            "imbalance_unresolved": imbalance_unresolved,
            "s1_violation": s1_violation,
            "slippage_abs_per_side": slippage,
            "guard_trips": guard_trips,
            "realized_delta": str(realized_delta),
            "realized_unsettled": realized_unsettled,
            "unsettled_legs": unsettled_legs,
            "day_realized_seed": str(self._day_totals[0]),
            "day_guard_trips_seed": self._day_totals[1],
            "s4_running_total": str(s4_running),
            "second_pair_book_walk": second_pair_book_walk,
            "alarms": alarms,
            "stops": stops,
            "inherited": (None if plan.inherited is None
                          else {t: str(v) for t, v in plan.inherited.items()}),
            "journal_path": os.path.abspath(journal_path),
            "records": records,
            "flushed_at": self.clock(),
        }
        return entry

    def _build_box_ledger_entry(
        self, plan: Plan, recorder: WindowRecorder | None, exit_code: int,
        journal_path: str, records: int,
    ) -> dict[str, Any]:
        """The box's pilot-ledger row. Mirrors the corridor row's shape for the shared fields (mode,
        arming, alarms/stops, s4 total) but records box-specifics: the anchor, the ledger high/low
        slot mapping (hourly leg / 15M leg), the $1 floor booked at close (``floor_booked``, so the
        settlement backfill nets it), ``box_one_legged`` (the A5 counter), and S1_box."""
        from service.ledger import fills_record

        actions: list[Action] = []
        if recorder is not None and hasattr(recorder, "driver"):
            actions = list(recorder.driver.actions)
        would_fires = sum(1 for a in actions if a.kind == WOULD_FIRE)
        fires = sum(1 for a in actions if a.kind == FIRE)
        fired_source = WIDE_BOX if fires > 0 else None
        # v1.1 implied-pin floor: a skip / paper rescan is neither a fire nor a would-fire (distinct
        # action kinds), so it never counts in fires/would_fires/signals. Surface the flags for the
        # report so a skipped hour is not mistaken for a no-signal hour.
        box_skipped = any(a.kind == BOX_SKIP for a in actions)
        box_rescan = any(a.kind == BOX_RESCAN_WOULD_FIRE for a in actions)

        fills: list[dict[str, Any]] = []
        filled = False
        realized_delta = Decimal(0)
        realized_unsettled = False
        unsettled_legs: list[dict[str, Any]] = []
        floor_booked = Decimal(0)
        s1_violation = False
        slippage: list[str] = []
        ls = self.ledger_state
        if ls is not None:
            rec = fills_record(ls)
            filled = bool(rec["filled"])
            for lg in rec["legs"]:
                fills.append({
                    "ticker": lg["ticker"], "side": lg["side"],
                    "count": (int(lg["count"]) if isinstance(lg["count"], (int, Decimal)) else lg["count"]),
                    "avg_price": None if lg["avg_price"] is None else str(lg["avg_price"]),
                    "avg_fee": str(lg["avg_fee"]),
                })
            if rec["any_fill"]:
                realized_delta = rec["realized_at_close"]
                unsettled_legs = list(rec["unsettled_legs"])
                realized_unsettled = bool(unsettled_legs)
                # The box's matched pair is floor-booked ($1) at close; record it so the settlement
                # backfill nets the floor (payoff $2 pinned -> +$1; $1 not pinned -> +$0).
                floor_booked = ls.matched_pairs() * Decimal("1.00")
            if self._box_policy is not None:
                from service.stops import check_s1_box
                s1_violation = check_s1_box(ls, self._box_policy.pair_cost_max) is not None
            slippage = self._entry_slippage(ls)

        alarms: list[dict[str, str]] = []
        stops: list[dict[str, str]] = []
        if self.stops is not None:
            for n in self.stops.state.alarms:
                alarms.append({"kind": n.kind, "reason": n.reason})
            for n in self.stops.state.notifications:
                if n.kind.startswith("S"):
                    stops.append({"kind": n.kind, "reason": n.reason})
        if plan.wake is not None and plan.wake.ladder.alarm:
            alarms.append({"kind": "A2", "reason": f"ladder-map: {plan.wake.ladder.reason}"})

        orders_attempted = int(getattr(self.executor, "dispatch_count", 0)) if self.executor else 0
        guard_trips = int(self.stops.state.guard_trips) if self.stops is not None else 0
        s4_running = realized_delta
        try:
            s4_running = s4_running_loss(load_entries(self.ledger_path)) + realized_delta
        except Exception:  # noqa: BLE001
            pass

        box_state = plan.box_state
        arm = plan.arm_decision
        entry: dict[str, Any] = {
            "close_time": self.close_time,
            "strategy": "box",
            "mode": plan.effective_mode,
            "requested_mode": resolve_mode(self.cli_mode, self.mode_txt_path),
            "pairs": self.pairs,
            "armed": plan.armed,
            "degraded_to_dry": plan.degraded,
            "degrade_reason": plan.degrade_reason,
            "arm_decision": None if arm is None else {"would_arm": arm.armed, "reasons": list(arm.reasons)},
            "stand_down": plan.stand_down,
            "stand_down_reason": plan.stand_down_reason,
            "exit_code": exit_code,
            "policy_sha": plan.box_policy.sha256 if plan.box_policy is not None else None,
            "roster": plan.box_policy.roster_name if plan.box_policy is not None else None,
            "ladder_ok": plan.wake.ladder.ok if plan.wake is not None else None,
            "anchor_A": None if box_state is None else str(box_state.anchor_A),
            "m15_ticker": None if box_state is None else box_state.m15_ticker,
            # ledger slot mapping: high_ticker = hourly leg, low_ticker = 15M leg.
            "hourly_ticker": (ls.high_ticker if ls is not None else None),
            "low_ticker": (ls.low_ticker if ls is not None else None),
            "signals": would_fires + fires,
            "would_fires": would_fires,
            "fires": fires,
            "box_skipped": box_skipped,
            "box_rescan": box_rescan,
            "fired_source": fired_source,
            "orders_attempted": orders_attempted,
            "fills": fills,
            "filled": filled,
            # F3: box_one_legged = ENTRY quality (exactly one leg filled), counted by A5 regardless of
            # whether the flatten later filled. box_flatten_filled = the flatten OUTCOME, recorded
            # separately (None = no flatten; True = flattened flat; False = held naked).
            "box_one_legged": bool(self._box_one_legged and fires > 0),
            "box_flatten_filled": self._box_flatten_filled,
            "s1_violation": s1_violation,
            "slippage_abs_per_side": slippage,
            "guard_trips": guard_trips,
            "realized_delta": str(realized_delta),
            "realized_unsettled": realized_unsettled,
            "unsettled_legs": unsettled_legs,
            "floor_booked": str(floor_booked),
            "s4_running_total": str(s4_running),
            "alarms": alarms,
            "stops": stops,
            "inherited": (None if plan.inherited is None
                          else {t: str(v) for t, v in plan.inherited.items()}),
            "journal_path": os.path.abspath(journal_path),
            "records": records,
            "flushed_at": self.clock(),
        }
        return entry

    def _entry_slippage(self, ls: LedgerState) -> list[str]:
        """Per filled leg: |avg fill price - the entry intent's observed-ask limit|, as strings."""
        entry = next((i for i in ls.intents if i.purpose == PURPOSE_ENTRY), None)
        if entry is None:
            return []
        limits = {(lg.ticker, lg.side): lg.limit_price for lg in entry.legs}
        out: list[str] = []
        for which in ("high", "low"):
            pos = ls.position(which)
            if pos is None or pos.avg_buy_price is None:
                continue
            lim = limits.get((pos.ticker, pos.side))
            if lim is not None:
                out.append(str(abs(pos.avg_buy_price - lim)))
        return out

    def _book_walk(self, ls: LedgerState) -> dict[str, Any]:
        """Second-pair book-walk proxy (pairs>=2): per leg, the average fill vs the entry ask.
        CONFESSED: at IOC-batch dual size the response gives a blended average_fill_price, not the
        1st-vs-2nd contract prints, so this reports the avg-vs-ask spread as the available proxy for
        the walk; a precise per-contract walk needs the per-fill records (live-verification item)."""
        entry = next((i for i in ls.intents if i.purpose == PURPOSE_ENTRY), None)
        limits = {(lg.ticker, lg.side): lg.limit_price for lg in (entry.legs if entry else ())}
        legs: list[dict[str, Any]] = []
        for which in ("high", "low"):
            pos = ls.position(which)
            if pos is None or pos.avg_buy_price is None:
                continue
            lim = limits.get((pos.ticker, pos.side))
            legs.append({
                "ticker": pos.ticker,
                "avg_fill": str(pos.avg_buy_price),
                "entry_ask": None if lim is None else str(lim),
                "count": int(pos.net) if pos.net == int(pos.net) else str(pos.net),
            })
        return {"measured": "avg_vs_ask_proxy", "legs": legs}


def _safe_close(close_iso: str) -> str:
    return close_iso.replace(":", "").replace("-", "")


def _parse_market_result(resp: Any, ticker: str) -> str | None:
    """Extract a settled market result ('yes'/'no') for ``ticker`` from a proxy /markets payload.
    Accepts {"market": {...}} or {"markets": [...]}; returns None until the market carries a
    ``result`` of exactly 'yes'/'no' (fail-closed: an empty/absent result means not-yet-settled).

    F1 (Phase-4): the record's ``ticker`` MUST equal ``ticker`` exactly, in BOTH shapes. There is NO
    fallback to ``markets[0]`` and NO trust of an unlabelled ``{"market": {...}}`` — a foreign
    market's ``result`` must never be attributed to our leg (that booked fictitious P&L). If the
    requested ticker is absent, return None and let the backfill wait for a later wake."""
    if not isinstance(resp, dict):
        return None
    rec: Any = None
    market = resp.get("market")
    markets = resp.get("markets")
    if isinstance(market, dict):
        rec = market if market.get("ticker") == ticker else None
    elif isinstance(markets, list):
        rec = next((m for m in markets if isinstance(m, dict) and m.get("ticker") == ticker), None)
    if not isinstance(rec, dict):
        return None
    result = rec.get("result")
    return result if result in ("yes", "no") else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run one pilot window (Phase 4 harness).")
    ap.add_argument("--mode", default=None, choices=list(VALID_MODES),
                    help="Override the rung. If omitted, read from --mode-file (mode.txt).")
    ap.add_argument("--strategy", default=None, choices=list(VALID_STRATEGIES),
                    help="Override the strategy. If omitted, read from --strategy-file (strategy.txt). "
                         "Unknown/missing -> corridor core, DRY (fail closed).")
    ap.add_argument("--strategy-file", default=DEFAULT_STRATEGY_TXT)
    ap.add_argument("--close-time", default=None, help="Target close ISO (UTC). Default: next :00.")
    ap.add_argument("--pairs", type=int, default=1, choices=[1, 2], help="Contract pairs per entry.")
    ap.add_argument("--max-hourly-strikes", type=int, default=0,
                    help="Hourly ladder tickers to subscribe (0 = the paired strike only; a positive "
                         "N widens; omit-as-negative for the full ladder). Decision pair always kept.")
    ap.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    ap.add_argument("--falsifier", default=DEFAULT_FALSIFIER_PATH,
                    help="Corridor falsifier (S5 when strategy=corridor).")
    ap.add_argument("--box-falsifier", default=DEFAULT_BOX_FALSIFIER_PATH,
                    help="Box falsifier (S5 when strategy=box). Its STATUS line must be FROZEN to arm.")
    ap.add_argument("--mode-file", default=DEFAULT_MODE_TXT)
    ap.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    ap.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--proxy-base", default="http://127.0.0.1:8642")
    return ap.parse_args(argv)


def _setup_logging(log_dir: str, close_iso: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(log_dir, _safe_close(close_iso) + ".log"),
                                            encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    close_iso = args.close_time or next_top_of_hour_iso(time.time())
    _setup_logging(args.log_dir, close_iso)
    max_hourly = None if args.max_hourly_strikes < 0 else args.max_hourly_strikes
    proxy = ProxyAuth(base_url=args.proxy_base)
    svc = WindowService(
        close_time=close_iso,
        cli_mode=args.mode,
        pairs=args.pairs,
        proxy=proxy,
        policy_path=args.policy,
        falsifier_path=args.falsifier,
        box_falsifier_path=args.box_falsifier,
        mode_txt_path=args.mode_file,
        journal_dir=args.journal_dir,
        ledger_path=args.ledger,
        proxy_base=args.proxy_base,
        max_hourly=max_hourly,
        cli_strategy=args.strategy,
        strategy_txt_path=args.strategy_file,
    )
    return svc.run()


if __name__ == "__main__":
    raise SystemExit(main())
