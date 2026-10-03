"""run_v33.py — the process spine for pilot V3.3 (the rolling K-rung ladder), running ALONGSIDE the
live V3.2 in DRY (Brad 2026-09-22: "run it along side V3.2 without it trading, then flip V3.3 to
contracts 10 and V3.2 to 0. Just to watch and compare. Make sure V3.3 is running exactly as expected
before $20+ are on the line").

One process == one window, mirroring ``run_v32``: same CLI (``--close``/``--mode``/``--proxy-base``/
``DV3_PROXY_BASE``/``DV3_DATA_DIR``), same two-connection WS topology, same discovery, the SAME reused
recorder/run-loop/lag-sampler — but its OWN mode file (``ops/v33_mode.txt``, missing -> ``dry``, never
armed by default), ledger (``ledger/v33_ledger.jsonl``), journal dir (``journals_v33/``), log dir
(``logs_v33/``) and day-guard file (``ops/v33_stops_<day>.json``), all routed through ``service.paths``.

DRY (the side-by-side watch — SENDS NOTHING): the full V3.3 core runs against the live feed every window;
the ``FrozenExecutor`` journals the WOULD_* order intents and synthesises the exchange acks so the
convergence state machine actually cycles; and the driver SIMULATES ladder fills with the IDEAL rule (a
spot-bucket YES-taker print at ``yes_price >= 1 - rung price`` fills that rung), feeding each simulated
Fill into the core so it books the rung + takes the coalesced wings (priced from the live strike book at
fill time, exactly as the V3.2 shadow does). Those fills are booked into the v33 ledger row as
``dry_sim`` — clearly labelled, NEVER counted as realised. The row is comparable to V3.2's for the
side-by-side report.

ARMED (L3 arms it; the gate lives in ``service.v33.stops.decide_v33_arming``): the ``V33LiveExecutor``
sends the real K-rung ladder. The amend cap is NOT yet applied at the proxy, so the roll's cancel->create
fallback per order is the default path (flipping to amend-first is a params/env value, not code).

House law throughout: money is Decimal, time comes only from server timestamps, every network edge is
injected (tests use fakes — this is NEVER dialed against the live proxy from an automated context), the
SEALED/holdout dates are never read, fail closed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

from service.book import TopOfBook
from service.proxy_auth import ProxyAuth
from service.proxy_writer import ProxyWriter
from service.record_range import RANGE_SERIES, StreamJournal, discover_range_markets, journal_filename
from service.record_window import (
    GRACE_SECONDS,
    _append_summary,
    arm_hard_stop,
    next_top_of_hour_iso,
    write_standdown_summary,
)
from service.wake import DEAD_STATUSES, FIFTEEN_SERIES, StandDown, close_epoch, coerce_exchange_index
from service.ws_client import KalshiWebSocketClient

from service.v33 import (
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderAmended,
    OrderCancelled,
    Trade,
    V33Params,
    V33ParamsInvalid,
    V33ParamsShaMismatch,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import ActionKind, V33ActionKind
from service.v33.core import _q_count, print_through_summary
from service.v33.events import V33Fill
from service.v33.executor import V33LiveExecutor, cancel_stale_open_orders
from service.v33.async_writer import AsyncOrderWriter
from service.v33.async_executor import V33AsyncExecutor
from service.v33.shadow import DeepObservationLadder, ideal_rung_crosses
from service.v33.ledger import (
    append_v33_ledger_row,
    build_v33_ledger_row,
    compute_ladder_money_math,
    load_v33_rows,
    v33_pending_credit,
    v33_settlement_backfill_sweep,
)
from service.v33.reconcile import (
    ExecFill,
    alarm_breakdown,
    reconcile_exec_truth_only,
    reconcile_live,
)
from service.v33.stops import (
    V33_S1_LEGGED_LATCH_THRESHOLD,
    decide_v33_arming,
    record_legged_occurrence,
    v33_day_guard_path,
    v33_s4_decision,
)
from service.paths import (
    checkout_ops_dir,
    data_dir,
    default_proxy_base,
    falsifier_path_v33,
    journal_dir_v33,
    ledger_path_v33,
    log_dir_v33,
    mode_path_v33,
    ops_dir_v33,
)
from service.stops import ensure_balance_start, parse_balance, read_day_guard

# Reuse the V3.2 process infrastructure UNCHANGED (discovery, recorder, run-loop, lag sampler, the dry
# FrozenExecutor, health/settlement reads, the trade/fill frame parsers) — the ladder differs only in
# the pure core + the money math + the roster's own files, so the spine is shared, not re-implemented.
import service.run_v32 as R

logger = logging.getLogger(__name__)


def _count_out(c: Any) -> Any:
    """D3: serialise a lot count for the journal/ledger — an INTEGRAL count as a bare int (so a dry
    1-lot window stays byte-identical), a fractional count (e.g. 0.44) as the 2dp Decimal (str-encoded by
    the journal's ``_json_default``). Old integer rows already parse as ints; new fractional rows parse
    as Decimal strings."""
    d = _q_count(Decimal(str(c)))
    return int(d) if d == d.to_integral_value() else d


def _count_dec(raw: Any) -> Decimal:
    """Parse a raw count/count_fp value to a 2dp Decimal (0 on any parse failure)."""
    try:
        return _q_count(Decimal(str(raw)))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal(0)


VALID_MODES_V33 = ("shakedown", "dry", "armed")
STRIKE_SERIES = R.STRIKE_SERIES
STRIKE_CHANNELS = R.STRIKE_CHANNELS
BUCKET_CHANNELS = R.BUCKET_CHANNELS
CONNECT_MARGIN_S = R.CONNECT_MARGIN_S
PUMP_INTERVAL_S = R.PUMP_INTERVAL_S
ORDER_POLL_INTERVAL_S = R.ORDER_POLL_INTERVAL_S
_HEARTBEAT_S = 10.0
_PUMP_GUARD = 100000

DEFAULT_FALSIFIER_PATH_V33 = falsifier_path_v33()

# The action kinds that carry an order to the venue (everything else — STAND_DOWN, the shadow-fill
# telemetry kinds — produces no HTTP; the async executor returns [] for them). The async dispatch path
# routes ONLY these to the off-loop writer; the sync path routes every kind through on_action (which
# returns [] for the rest), so the two are behaviourally equivalent.
_ORDER_ACTION_KINDS = frozenset({
    ActionKind.PLACE_REST, ActionKind.CANCEL_REST, ActionKind.AMEND_REST,
    ActionKind.TAKE_WINGS, ActionKind.RETRY_WING,
    V33ActionKind.TAKE_BUCKET_NO, V33ActionKind.UNWIND_WINGS,
})


# Informational core actions: journaled, NEVER routed to an executor (D5 netting record; gates A/B alarm).
_INFO_ACTION_KINDS = frozenset({V33ActionKind.WING_NETTED, V33ActionKind.ALARM})


def async_writer_enabled(cli_flag: bool) -> bool:
    """Whether the OFF-LOOP async order writer is selected. CLI --async-writer wins; else the
    DV3_V33_ASYNC_WRITER env var (1/true/yes/on). DEFAULT OFF — the synchronous V33LiveExecutor stays the
    live path until Brad shakes down the async path and flips it (mirrors the --batch-create / amend-first
    dormant-flag discipline; NOT a params field, so the pinned v33_params sha is untouched)."""
    if cli_flag:
        return True
    return os.environ.get("DV3_V33_ASYNC_WRITER", "").strip().lower() in ("1", "true", "yes", "on")


# ===========================================================================
# Mode lever (config-file driven; missing -> dry, NEVER armed by default)
# ===========================================================================
def read_v33_mode_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve_v33_mode(cli_mode: str | None, mode_txt_path: str) -> str:
    """Effective mode. CLI --mode wins; else read v33_mode.txt. Unknown/ABSENT -> ``dry`` (never armed
    by default; a missing mode file must not arm the live-adjacent ladder — Brad's dry side-by-side)."""
    raw = cli_mode if cli_mode else read_v33_mode_file(mode_txt_path)
    m = (raw or "").strip().lower()
    return m if m in VALID_MODES_V33 else "dry"


def _resolve_v33_guard_path(utc_day: str) -> str:
    """The V3.3 day-guard file for ``utc_day`` (mid-day-cutover safety mirrors run_v32's guard resolve)."""
    primary = v33_day_guard_path(ops_dir_v33(), utc_day)
    if data_dir() is None:
        return primary
    checkout = v33_day_guard_path(checkout_ops_dir(), utc_day)
    if primary != checkout and not os.path.exists(primary) and os.path.exists(checkout):
        logger.warning("[V33] DV3_DATA_DIR set but data-dir day-guard %s missing while checkout %s "
                       "exists -> using the CHECKOUT guard for %s.", primary, checkout, utc_day)
        return checkout
    return primary


# ===========================================================================
# Executor selection — the ONE place the executor kind is chosen
# ===========================================================================
def _proxy_max_contracts(health: Any) -> int | None:
    """The proxy's MAX_CONTRACTS_PER_ORDER from /health caps, or None if unreadable."""
    try:
        return int((health or {}).get("caps", {}).get("max_contracts_per_order"))
    except (TypeError, ValueError, AttributeError):
        return None


def build_executor_v33(
    effective_mode: str, *, bucket_map, exchange_index_by_ticker, journal, close_epoch_val,
    params: V33Params, writer: ProxyWriter | None, clock: Callable[[], float] = time.time,
    batch_create: bool = False, wing_cap: int | None = None,
    async_writer: AsyncOrderWriter | None = None,
) -> Any:
    """``armed`` -> the real ``V33LiveExecutor`` (requires a ProxyWriter); everything else -> the reused
    dry ``FrozenExecutor`` (refuses a real action kind). ``wing_cap`` = the chunk cap for coalesced wing
    takes = min(params.max_contracts_per_order_hint, proxy /health cap); the write pacer is sized from
    params. A LiveExecutor is NEVER constructed unless armed.

    BELT (L2 review R2-N2): an armed executor MUST NOT guess the venue's contract cap. If ``wing_cap`` is
    None -- the proxy /health cap was unreadable at window start -- REFUSE to build the armed executor
    (raise) so the window fails closed rather than sizing wing chunks against an assumed cap. In practice
    S5 (``v33_caps_agree``) already refuses to arm when /health has no caps, so armed implies a known cap;
    this belt catches the race where the cap read failed AFTER the S5 gate passed."""
    if effective_mode == "armed":
        if writer is None:
            raise ValueError("armed executor requires a ProxyWriter")
        if wing_cap is None:
            raise ValueError("armed executor requires a known proxy contract cap (wing_cap); the "
                             "/health cap was unreadable -- refusing to size wings against a guess")
        if async_writer is not None:
            # OFF-LOOP armed executor (2026-09-30): the SAME pacer/counter/invariant setup as the sync one,
            # but every round trip runs off the loop via ``async_writer``. Selected only when Brad's
            # --async-writer flag / DV3_V33_ASYNC_WRITER is set (default OFF — shakedown before arm).
            return V33AsyncExecutor(
                async_writer, writer, bucket_map, exchange_index_by_ticker, journal, close_epoch_val,
                params.quote_end_s, k_rungs=params.rungs, clock=clock, batch_create=batch_create,
                wing_cap=wing_cap, write_tokens_per_s=params.write_tokens_per_s,
                write_bucket_size=params.write_bucket_size,
                write_reserve_tokens=params.write_reserve_tokens,
                enable_print_through=params.print_through,
            )
        return V33LiveExecutor(
            writer, bucket_map, exchange_index_by_ticker, journal, close_epoch_val,
            params.quote_end_s, k_rungs=params.rungs, clock=clock, batch_create=batch_create,
            wing_cap=wing_cap, write_tokens_per_s=params.write_tokens_per_s,
            write_bucket_size=params.write_bucket_size,
            write_reserve_tokens=params.write_reserve_tokens,
            enable_print_through=params.print_through,
        )
    return R.FrozenExecutor(bucket_map)


# ===========================================================================
# The driver — wires decide_v33 onto the reused recorder pipeline + the DRY ladder-fill simulation
# ===========================================================================
class V33Driver:
    """Holds the live V33State and drives ``decide_v33`` on each event. Mirrors ``V32Driver`` (monotone
    cross-connection eval clock; journal every decision; route every order-bearing action to the
    executor) but reads the LADDER (K rungs) instead of a single rest, and — in DRY — SIMULATES ladder
    fills with the ideal rule so the side-by-side row carries the same shape as an armed one."""

    def __init__(self, params: V33Params, state: V33State, journal: StreamJournal, executor: Any,
                 *, dry_sim: bool, batch_create: bool = False,
                 clock: Callable[[], float] = time.time,
                 async_writer: AsyncOrderWriter | None = None) -> None:
        self.params = params
        self.state = state
        self.journal = journal
        self.executor = executor
        self.dry_sim = bool(dry_sim)
        self.batch_create = bool(batch_create)
        self.clock = clock
        # OFF-LOOP async writer (2026-09-30): when set, order-bearing actions are dispatched to the writer
        # as loop tasks (the loop never blocks on a round trip); when None, the byte-identical synchronous
        # path runs. The executor is a V33AsyncExecutor iff async_writer is set (wired in build_executor_v33).
        self._async = async_writer is not None
        self._aw = async_writer
        self._async_errors = 0
        self._standdown_sweep_scheduled = False   # gate C(ii): the executor stand-down sweep runs once
        # feed_gap_max_s (build brief §6): the largest WALL gap between consecutive WS feed frames while the
        # ladder is quoting (live/pending rungs) — the DIRECT proof the loop is not freezing the feed.
        self._last_feed_wall: float | None = None
        self._feed_gap_max_s: float = 0.0
        self.counts: dict[str, int] = defaultdict(int)
        self._last_server_ts: float | None = None
        self._last_wall: float | None = None
        self._last_eval_key: tuple | None = None
        self._last_eval_ts: float | None = None
        self._seen_trade_ids: set[str] = set()
        self._last_quoted_bucket_ticker: str | None = None
        self._last_quoted_Sd: int | None = None
        self._last_quoted_Su: int | None = None
        self._last_n_top: Decimal | None = None
        self._spot_buckets_quoted: list[int] = []
        self._last_stand_down_reason: str | None = None
        self._real_stand_downs: int = 0
        self._quote_end_cancel: bool = False
        self._dry_sim_fills: int = 0        # count of simulated rung fills (dry only)
        # GATE E (2026-10-03 02:00Z): EXECUTOR-TRUTH rest fills, captured as the driver journals each
        # ``rest_fill`` — INDEPENDENT of whether the core books it (the 02:00Z core lost two owned orders
        # whose fills the executor saw and journaled). Reconciled against core truth at _finalize.
        self._exec_truth_fills: list[ExecFill] = []
        # SO-3 deep-end observation ladder (19..28c): observation-only, runs in EVERY mode, never places.
        self.deep_obs = DeepObservationLadder(params, state.close_epoch)

    def _driver_alarm(self, name: str, detail: dict) -> None:
        """Journal a DRIVER operational alarm as kind ``alarm`` and count it under ``driver_alarm`` (gate E
        item 3: the ledger ``alarms`` field includes driver alarms, not just the WS recorder's). The
        journal record is identical to the pre-gate-E direct ``journal.append('alarm', ...)``; only the
        counter is new."""
        self.counts["driver_alarm"] += 1
        self.journal.append("alarm", {"alarm": name, **detail}, self.clock())

    # --- clock source ---
    def _stamp(self, server_ts: float) -> None:
        prev = self._last_server_ts
        self._last_server_ts = server_ts if prev is None else max(prev, server_ts)
        self._last_wall = self.clock()

    def server_now(self) -> float | None:
        if self._last_server_ts is None or self._last_wall is None:
            return None
        return self._last_server_ts + (self.clock() - self._last_wall)

    # --- event entry points (same signatures the reused V32Recorder drives) ---
    def _track_feed_gap(self) -> None:
        """Record the max wall gap between consecutive feed frames WHILE QUOTING (the ladder holds live or
        pending rungs). A frozen loop shows up here directly; a healthy off-loop writer keeps it near the
        WS cadence even through an 11-order cancel-all + re-place."""
        wall = self.clock()
        prev = self._last_feed_wall
        self._last_feed_wall = wall
        quoting = bool(self.state.ladder) or bool(self.state.rolls_in_flight)
        if prev is not None and quoting:
            gap = wall - prev
            if gap > self._feed_gap_max_s:
                self._feed_gap_max_s = gap

    def on_book_update(self, market: str, top: TopOfBook, server_ts: float) -> None:
        self._track_feed_gap()
        self._stamp(server_ts)
        eval_ts = self._last_server_ts
        self._pump([BookUpdate(market_ticker=market, top=top, server_ts=eval_ts, book_ts=server_ts)])

    def on_trade(self, market: str, payload: dict, server_ts: float) -> None:
        ev = R._trade_event(market, payload, server_ts)
        if ev is None:
            self.journal.append("v33_trade_unparsed", {"market": market}, self.clock())
            return
        self._track_feed_gap()
        self._stamp(server_ts)
        ev = replace(ev, server_ts=self._last_server_ts)
        self._pump([ev])
        # DRY ideal-fill simulation: a spot-bucket YES taker at yes_price crosses our NO rung at n iff
        # yes_price >= 1 - n. Fill each crossed LIVE rung on the ladder's bucket. Sends nothing.
        if self.dry_sim:
            self._simulate_ladder_fills(market, ev, self._last_server_ts)
        # SO-3 deep-end observation (19..28c), observation-only, EVERY mode. Sends nothing.
        self._observe_deep(market, ev, self._last_server_ts)

    def on_clock_tick(self, server_ts: float) -> None:
        self._pump([ClockTick(server_ts=server_ts)])

    def on_fill(self, market: str, payload: dict, server_ts: float) -> None:
        """Route a private fill (ARMED only) through the RestBook. A coid we never placed is FOREIGN
        (journaled + dropped, fail-closed). A tracked ladder rung feeds a normal Fill; a de-duped wing
        echo is skipped. (Dry never reaches here — the fill channel is subscribed only when armed.)"""
        pf = R._fill_event(payload) or {}
        coid = pf.get("client_order_id") or payload.get("client_order_id")
        oid = pf.get("order_id") or payload.get("order_id")
        trade_id = pf.get("trade_id") or payload.get("trade_id")
        if trade_id is not None and trade_id in self._seen_trade_ids:
            self.counts["fill_dup_ignored"] += 1
            return
        if coid is not None and coid in getattr(self.executor, "wing_coids", set()):
            if trade_id is not None:
                self._seen_trade_ids.add(trade_id)
            self.counts["wing_fill_ws_dup"] += 1
            return
        rec = self.executor.attribute(coid=coid, order_id=oid)
        if rec is None:
            self.counts["foreign_fill_ignored"] += 1
            self.journal.append("foreign_fill_ignored",
                                {"market": market, "client_order_id": coid, "order_id": oid},
                                self.clock())
            return
        self._stamp(server_ts)
        if trade_id is not None:
            self._seen_trade_ids.add(trade_id)
        # D3 (2026-09-30 incident): Kalshi crypto fills are FRACTIONAL — parse ``count_fp`` as Decimal.
        # The pre-fix ``int(pf.get("count") or 0) or rec.count`` truncated 0.44 -> 0 and then fell back to
        # the rung's FULL placed lot (a 0.44 fill became a whole lot; the wings were oversized). Prefer the
        # raw ``count_fp``; only if it is absent/zero fall back to the placed count.
        count = _count_dec(payload.get("count_fp"))
        if count <= 0:
            count = _count_dec(pf.get("count"))
        if count <= 0:
            count = _count_dec(rec.count)
        self.executor.mark_filled(rec.client_order_id)
        self.counts["rest_fill"] += 1
        self.journal.append("rest_fill",
                            {"market": market, "client_order_id": rec.client_order_id,
                             "order_id": rec.order_id, "rest_price": rec.price,
                             "exec_price": pf.get("price"), "count": _count_out(count), "path": "ws"},
                            self.clock())
        # GATE E: executor truth — this ws fill is recorded whether or not the core books it.
        self._exec_truth_fills.append(ExecFill(count=count, coid=rec.client_order_id,
                                               order_id=rec.order_id, price=rec.price,
                                               ticker=market, source="ws"))
        # D1: V33Fill carries the fill's OWN market ticker as the last-resort bucket attribution channel.
        self._pump([V33Fill(order_id=rec.order_id, client_order_id=rec.client_order_id,
                            count=count, price=rec.price, side="no", server_ts=server_ts,
                            market_ticker=market, source="ws")])

    def on_poll_fill(self, order_id: str, filled_count: Any, server_ts: float) -> None:
        """Book a rung fill discovered by the order-status poll (ARMED belt-and-braces). Feeds the DELTA
        over what the core has booked for this order (``rest_booked_by_coid``). D3: ``filled_count`` is the
        venue cumulative fill (fractional-safe), and the delta over what is booked is fed as Decimal."""
        filled = _count_dec(filled_count)
        if filled <= 0:
            return
        rec = self.executor.attribute(order_id=order_id)
        if rec is None:
            return
        already = _count_dec(self.state.rest_booked_by_coid.get(rec.client_order_id, 0))
        delta = filled - already
        if delta <= 0:
            return
        self._stamp(server_ts)
        self.executor.mark_filled(rec.client_order_id)
        self.counts["rest_fill_poll"] += 1
        self.journal.append("rest_fill", {"client_order_id": rec.client_order_id, "order_id": order_id,
                                          "rest_price": rec.price, "count": _count_out(delta),
                                          "path": "poll", "market": rec.ticker}, self.clock())
        # GATE E: executor truth records the venue CUMULATIVE ``filled`` (a MAX source), not the
        # core-relative delta — so the reconciled count never depends on what the core had booked.
        self._exec_truth_fills.append(ExecFill(count=filled, coid=rec.client_order_id,
                                               order_id=order_id, price=rec.price,
                                               ticker=rec.ticker, source="poll"))
        # D1: the poll knows the order's market via the retained RestRecord (``rec.ticker``).
        self._pump([V33Fill(order_id=order_id, client_order_id=rec.client_order_id,
                            count=delta, price=rec.price, side="no", server_ts=server_ts,
                            market_ticker=rec.ticker, source="poll")])

    # --- DRY ladder-fill simulation (the ideal rule; never in armed) ---
    def _simulate_ladder_fills(self, market: str, trade: Trade, now: float) -> None:
        st = self.state
        if trade.taker_side != "yes":
            return
        # only a print on the bucket the ladder rests on (the spot bucket) crosses our NO rungs.
        rest_sd = st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd
        if rest_sd is None:
            return
        bucket_ticker = st.bucket_tickers.get(rest_sd)
        if bucket_ticker is None or market != bucket_ticker:
            return
        # the SINGLE ideal fill predicate (shared with SO-3): a YES-taker print at/through our NO rung.
        crossed = [o for o in st.ladder if o.live and o.order_id is not None
                   and ideal_rung_crosses(trade.yes_price, o.price)]
        if not crossed:
            return
        # cross from the TOP (shallowest) down, deterministically.
        for o in sorted(crossed, key=lambda x: x.price, reverse=True):
            self._dry_sim_fills += 1
            self.counts["dry_sim_fill"] += 1
            self.journal.append("dry_sim_fill",
                                {"client_order_id": o.client_order_id, "order_id": o.order_id,
                                 "n": o.price, "rung": o.rung, "yes_print": trade.yes_price,
                                 "count": _count_out(o.count)}, self.clock())
            # DRY simulates whole-lot rungs; ``_count_out`` keeps the count an int so the dry journal is
            # byte-identical to the pre-D3 build. V33Fill's ticker is the rung's own bucket (the rest bucket).
            self._pump([V33Fill(order_id=o.order_id, client_order_id=o.client_order_id,
                                count=_q_count(o.count), price=o.price, side="no", server_ts=now,
                                market_ticker=bucket_ticker)])

    # --- SO-3 deep-end observation (19..28c; observation only, all modes) ---
    def _observe_deep(self, market: str, trade: Trade, now: float) -> None:
        """Fold a spot-bucket YES-taker print inside the quoting window into the deep observation ladder.
        Gated to the ladder's spot bucket and the live quoting window T-quote_start .. T-quote_end, so the
        deep observation measures exactly the counterfactual the live path could have reached. It NEVER
        emits a Fill or touches an order path -- SO-3 is measurement, not a position."""
        st = self.state
        if trade.taker_side != "yes":
            return
        rest_sd = st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd
        if rest_sd is None:
            return
        bucket_ticker = st.bucket_tickers.get(rest_sd)
        if bucket_ticker is None or market != bucket_ticker:
            return
        t_minus = st.close_epoch - now
        if not (self.params.quote_end_s <= t_minus <= self.params.quote_start_s):
            return
        self.deep_obs.observe(taker_side=trade.taker_side, yes_price=trade.yes_price,
                              count=trade.count, n_top=st.n_top, W=st.W, server_ts=now)

    # --- decide + route loop ---
    def _pump(self, events: list[Any]) -> None:
        if self._async:
            return self._pump_async(events)
        q: deque = deque(events)
        guard = 0
        while q:
            guard += 1
            if guard > _PUMP_GUARD:
                self._driver_alarm("pump_runaway", {"guard": guard})
                break
            ev = q.popleft()
            self.state, actions = decide_v33(self.params, self.state, ev)
            ts = getattr(ev, "server_ts", self.clock())
            # OPTIONAL batch create (default OFF): group consecutive PLACE_REST from this decide into ONE
            # place_batch call (armed only; the FrozenExecutor has no batch path). Default -> route singly.
            if (self.batch_create and hasattr(self.executor, "place_batch")
                    and sum(1 for a in actions if a.kind == ActionKind.PLACE_REST) > 1):
                places = [a for a in actions if a.kind == ActionKind.PLACE_REST]
                others = [a for a in actions if a.kind != ActionKind.PLACE_REST]
                for a in places:
                    self._journal_action(a, ts)
                q.extend(self.executor.place_batch(places, ts))
                for a in others:
                    self._journal_action(a, ts)
                    if a.kind not in _INFO_ACTION_KINDS:   # D5 / gate A: informational, no venue order
                        q.extend(self.executor.on_action(a, self.state, ts))
            else:
                for a in actions:
                    self._journal_action(a, ts)
                    if a.kind not in _INFO_ACTION_KINDS:   # D5 / gate A: informational, no venue order
                        q.extend(self.executor.on_action(a, self.state, ts))
            self._apply_executor_standdown(ts)
            self._maybe_eval(getattr(ev, "server_ts", None))

    # --- async (off-loop) decide + dispatch ---
    def _pump_async(self, events: list[Any]) -> None:
        """The async twin of ``_pump``: decide SYNCHRONOUSLY (decide_v33 never awaits, so state stays
        serialized on the loop), journal every action, then DISPATCH the order-bearing actions to the
        off-loop writer as a loop task and RETURN — the loop is never blocked on a round trip. The
        executor's result events (OrderAck / OrderCancelled / OrderAmended / Fill) re-enter decide later via
        ``_ingest_async`` (build brief §1). Non-order kinds are no-ops for the executor exactly as in the
        sync path (``on_action`` returns [] for them)."""
        q: deque = deque(events)
        guard = 0
        while q:
            guard += 1
            if guard > _PUMP_GUARD:
                self._driver_alarm("pump_runaway", {"guard": guard})
                break
            ev = q.popleft()
            self.state, actions = decide_v33(self.params, self.state, ev)
            ts = getattr(ev, "server_ts", self.clock())
            order_actions = [a for a in actions if a.kind in _ORDER_ACTION_KINDS]
            for a in actions:
                # 2026-10-02 fix: telemetry must never abort the pump. A journaling error on ONE action is
                # alarmed and skipped; the decided state is already applied and the remaining queued events
                # (e.g. the sibling wing-leg Fill of a netted pair) still get processed.
                try:
                    self._journal_action(a, ts)
                except Exception as e:  # noqa: BLE001
                    self._async_errors += 1
                    self._driver_alarm("journal_action_error", {"kind": str(a.kind), "error": str(e)})
            self._apply_executor_standdown(ts)
            self._maybe_eval(getattr(ev, "server_ts", None))
            if order_actions:
                snapshot = self.state
                asyncio.get_running_loop().create_task(
                    self._dispatch_async(order_actions, ts, snapshot))

    async def _dispatch_async(self, order_actions: list[Any], ts: float, snapshot: V33State) -> None:
        """Run the order-bearing actions of ONE decide tick CONCURRENTLY off the loop ("reprices of
        multiple levels can be sent without delay" — Brad), and feed each result back into decide as it
        completes. A failure in one action never wedges the driver (journaled, counted, skipped)."""
        try:
            places = [a for a in order_actions if a.kind == ActionKind.PLACE_REST]
            coros: list[Any] = []
            if self.batch_create and len(places) > 1 and hasattr(self.executor, "place_batch_async"):
                others = [a for a in order_actions if a.kind != ActionKind.PLACE_REST]
                coros.append(self.executor.place_batch_async(places, ts))
                coros += [self.executor.on_action_async(a, snapshot, ts) for a in others]
            else:
                coros = [self.executor.on_action_async(a, snapshot, ts) for a in order_actions]
            for fut in asyncio.as_completed(coros):
                try:
                    result = await fut
                except Exception as e:  # noqa: BLE001 — one action's failure must not wedge the loop
                    self._async_errors += 1
                    self._driver_alarm("async_dispatch_error", {"error": str(e)})
                    continue
                self._apply_executor_standdown(ts)
                if result:
                    # 2026-10-02 fix: ingest each result under its own guard so a failure while re-entering
                    # decide for ONE result never abandons the still-pending futures of this dispatch.
                    try:
                        self._ingest_async(result)
                    except Exception as e:  # noqa: BLE001
                        self._async_errors += 1
                        self._driver_alarm("async_ingest_error", {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._async_errors += 1
            self._driver_alarm("async_dispatch_fatal", {"error": str(e)})

    def _ingest_async(self, events: list[Any]) -> None:
        """Re-enter the async pump on the loop with the executor's result events. Synchronous (decide never
        awaits), so it serializes with every other decide on the single loop thread — no state race."""
        self._pump_async(list(events))

    def _apply_executor_standdown(self, server_ts: float) -> None:
        reason = getattr(self.executor, "stand_down_reason", None)
        if reason and not self.state.stood_down:
            self.state = replace(self.state, stood_down=True)
            self.counts["executor_standdown"] += 1
            self._driver_alarm("executor_standdown", {"reason": reason})
        # GATE C(ii) (2026-10-03 02:00Z naked fill): an executor stand-down CANCELS every order the executor
        # owns (one-shot, off the loop); the sweep's OrderCancelled events re-enter decide so any racing fill
        # is booked and hedged (gate A). The pre-fix stand-down cancelled nothing it owned.
        if (reason and self._async and not self._standdown_sweep_scheduled
                and hasattr(self.executor, "standdown_sweep_async")):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:   # no loop (a sync harness): nothing to schedule on; retried next call
                return
            self._standdown_sweep_scheduled = True
            loop.create_task(self._standdown_sweep_task(server_ts))

    async def _standdown_sweep_task(self, server_ts: float) -> None:
        try:
            events = await self.executor.standdown_sweep_async(server_ts)
        except Exception as e:  # noqa: BLE001 -- the sweep must never wedge the loop
            self._async_errors += 1
            self._driver_alarm("standdown_sweep_error", {"error": str(e)})
            return
        if events:
            try:
                self._ingest_async(events)
            except Exception as e:  # noqa: BLE001
                self._async_errors += 1
                self._driver_alarm("async_ingest_error", {"error": str(e)})

    # --- journaling ---
    def _journal_action(self, a, server_ts: float) -> None:
        k = a.kind
        if k in (ActionKind.WOULD_PLACE_REST, ActionKind.PLACE_REST):
            rk = "would_place_rest" if k == ActionKind.WOULD_PLACE_REST else "place_rest"
            payload = {"ticker": a.ticker, "side": a.side, "count": _count_out(a.count), "price": a.price,
                       "expiration_epoch": a.expiration_epoch, "client_order_id": a.client_order_id}
            self._capture_quote(a)
        elif k in (ActionKind.WOULD_CANCEL_REST, ActionKind.CANCEL_REST):
            rk = "would_cancel_rest" if k == ActionKind.WOULD_CANCEL_REST else "cancel_rest"
            payload = {"order_id": a.order_id, "client_order_id": a.client_order_id}
        elif k in (ActionKind.WOULD_AMEND_REST, ActionKind.AMEND_REST):
            rk = "would_amend_rest" if k == ActionKind.WOULD_AMEND_REST else "amend_rest"
            payload = {"order_id": a.order_id, "ticker": a.ticker, "count": _count_out(a.count),
                       "price": a.price, "client_order_id": a.client_order_id,
                       "updated_client_order_id": a.updated_client_order_id}
            self._capture_quote(a)
        elif k in (ActionKind.WOULD_TAKE_WINGS, ActionKind.TAKE_WINGS, ActionKind.RETRY_WING):
            legs = [{"ticker": lg.ticker, "side": lg.side, "action": lg.action,
                     "count": _count_out(lg.count), "limit": lg.limit} for lg in a.legs]
            is_retry = k == ActionKind.RETRY_WING or len(legs) == 1
            if k == ActionKind.WOULD_TAKE_WINGS or (k == ActionKind.RETRY_WING and self.state.shakedown):
                rk = "would_retry_wing" if is_retry else "would_take_wings"
            else:
                rk = "retry_wing" if is_retry else "take_wings"
            payload = {"legs": legs, "count": _count_out(a.count), "lock": a.lock}
        elif k == ActionKind.STAND_DOWN:
            # BUCKET-FLAP FIX (2026-09-23): the stale/missing-wing HOLD lifecycle journals under distinct
            # kinds and does NOT count as a real stand-down; only the terminal cancel (and other reasons) do.
            if a.reason == "stale_or_missing_wing_hold":
                rk = "stand_down_hold"
                payload = {"reason": "stale_or_missing_wing"}
            elif a.reason == "stale_or_missing_wing_resume":
                rk = "stand_down_resume"
                payload = {"reason": "stale_or_missing_wing"}
            elif a.reason == "stale_or_missing_wing_cancel":
                rk = "stand_down_cancel"
                payload = {"reason": "stale_or_missing_wing"}
                self._last_stand_down_reason = "stale_or_missing_wing"
                self._real_stand_downs += 1
            elif a.reason == "past_quote_end":
                rk = "stand_down"
                payload = {"reason": a.reason}
                self._quote_end_cancel = True
            else:
                rk = "stand_down"
                payload = {"reason": a.reason}
                self._last_stand_down_reason = a.reason
                self._real_stand_downs += 1
        elif k in (V33ActionKind.TAKE_BUCKET_NO, V33ActionKind.WOULD_TAKE_BUCKET_NO):
            # PRINT-THROUGH complete branch: buy the bucket-NO ourselves (IOC taker) to finish the set.
            rk = ("would_print_through_complete" if k == V33ActionKind.WOULD_TAKE_BUCKET_NO
                  else "print_through_complete")
            payload = {"ticker": a.ticker, "side": a.side, "count": _count_out(a.count), "price": a.price}
        elif k in (V33ActionKind.UNWIND_WINGS, V33ActionKind.WOULD_UNWIND_WINGS):
            # PRINT-THROUGH unwind / fail-closed: sell the pre-taken wings back (IOC).
            rk = ("would_print_through_unwind" if k == V33ActionKind.WOULD_UNWIND_WINGS
                  else "print_through_unwind")
            legs = [{"ticker": lg.ticker, "side": lg.side, "action": lg.action,
                     "count": _count_out(lg.count), "limit": lg.limit} for lg in a.legs]
            payload = {"legs": legs, "count": _count_out(a.count)}
        elif k == V33ActionKind.WING_NETTED:
            # D5: the venue netted a YES/NO wing pair on one market to flat (+$1/contract). Informational.
            rk = "wing_netted"
            # 2026-10-02 fix: the netted legs are LegOrder (field ``limit`` carries the fill price); reading
            # ``.price`` raised inside the loop-side ingest and dropped the sibling wing-fill event (00:00Z).
            legs = [{"ticker": lg.ticker, "side": lg.side, "count": _count_out(lg.count),
                     "fill_price": lg.limit} for lg in a.legs]
            payload = {"legs": legs, "count": _count_out(a.count), "realised": a.lock}
        elif k == V33ActionKind.ALARM:
            # GATES A/B (2026-10-03): a named alarm raised by the pure core (orphan_rung_fill_hedged,
            # cancel_unattributed, orphan_fill_not_bucket, orphan_rung_fill_unpriced). Journaled as ``alarm``.
            rk = "alarm"
            payload = {"alarm": a.reason, "client_order_id": a.client_order_id, "order_id": a.order_id,
                       "ticker": a.ticker, "count": _count_out(a.count), "price": a.price,
                       "stood_down": self.state.stood_down}
        elif k == ActionKind.SHADOW_FILL_OUTSIDE_WINDOW:
            rk = "shadow_fill_outside_window"
            payload = {"E": a.shadow_E, "offer": a.offer, "print": a.print_price, "count": a.count,
                       "t_to_close": a.t_to_close}
        elif k == ActionKind.SHADOW_FILL_BELOW_MIN:
            rk = "shadow_fill_below_min"
            payload = {"E": a.shadow_E, "offer": a.offer, "print": a.print_price, "count": a.count,
                       "t_to_close": a.t_to_close}
        else:
            rk = "v33_action"
            payload = {"kind": str(k)}
        self.counts[rk] += 1
        self.journal.append(rk, payload, self.clock())

    def _capture_quote(self, a) -> None:
        st = self.state
        if a.ticker:
            self._last_quoted_bucket_ticker = a.ticker
        if st.spot_Sd is not None:
            self._last_quoted_Sd = st.spot_Sd
            self._last_quoted_Su = st.spot_Su
            if st.spot_Sd not in self._spot_buckets_quoted:
                self._spot_buckets_quoted.append(st.spot_Sd)
        if st.n_top is not None:
            self._last_n_top = st.n_top

    def _maybe_eval(self, server_ts: float | None) -> None:
        if server_ts is None:
            return
        st = self.state
        key = (st.spot_Sd, str(st.n_top), len(st.ladder), st.stand_down_reason)
        heartbeat = self._last_eval_ts is None or (server_ts - self._last_eval_ts) >= _HEARTBEAT_S
        if key == self._last_eval_key and not heartbeat:
            return
        self._last_eval_key = key
        self._last_eval_ts = server_ts
        self.journal.append("v33_eval",
                            {"t_minus_s": st.close_epoch - server_ts, "shakedown": st.shakedown,
                             "spot_Sd": st.spot_Sd, "spot_Su": st.spot_Su, "W": st.W, "cap": st.cap,
                             "n_top": st.n_top, "ladder_live": len(st.ladder),
                             "rungs_filled": st.rungs_filled, "replace_count": st.replace_count,
                             "roll_count": st.roll_count, "sets_done": st.sets_done,
                             "stand_down_reason": st.stand_down_reason}, self.clock())


# ===========================================================================
# Run loop (reuses the V3.2 gate + clock-pump + recording; a v33-aware order poll)
# ===========================================================================
async def _order_status_poll_v33(
    driver: V33Driver, executor: Any, clock: Callable[[], float], deadline: float,
    sleep: Callable[[float], Awaitable[None]], interval: float = ORDER_POLL_INTERVAL_S,
) -> None:
    """ARMED belt-and-braces: every ``interval`` s, book any fill the WS ``fill`` channel missed (de-duped
    by order_id in ``driver.on_poll_fill``). With ``params.order_poll_batched`` (default ON for v33, NIT-d)
    it does ONE ``GET /portfolio/orders?ticker=<bucket>`` per tick instead of K per-rung GETs; else it
    polls each live rung. Never raises."""
    batched = getattr(driver.params, "order_poll_batched", True)
    is_async = getattr(driver, "_async", False)
    if is_async:
        batched = True   # the off-loop executor implements only the batched ASYNC poll (order_poll_batched
        # is True by default anyway); the per-rung sync poll would hit the sync-HTTP guard.
    while clock() < deadline:
        await sleep(interval)
        if clock() >= deadline:
            break
        st = driver.state
        if batched:
            # R2-N4: poll EVERY bucket that still has a live rung, not just the current one. After a
            # mid-window bucket change the core cancels the prior-bucket rungs, but one can FILL before its
            # cancel confirms; polling only the new bucket would miss that fill (an un-hedged rung). Gather
            # the current rest/spot bucket AND every distinct bucket a live ladder rung rests on.
            tickers: set[str] = set()
            cur_sd = st.rest_bucket_Sd if st.rest_bucket_Sd is not None else st.spot_Sd
            if cur_sd is not None and st.bucket_tickers.get(cur_sd):
                tickers.add(st.bucket_tickers[cur_sd])
            for o in st.ladder:
                if o.live and o.order_id is not None:
                    tk = st.bucket_tickers.get(o.bucket_Sd)
                    if tk:
                        tickers.add(tk)
            if not tickers:
                continue
            filled_by_oid: dict[str, Decimal] = {}
            for tk in tickers:
                try:
                    if is_async:
                        filled_by_oid.update(await executor.poll_orders_for_bucket_async(tk))
                    else:
                        filled_by_oid.update(executor.poll_orders_for_bucket(tk))
                except Exception as e:  # noqa: BLE001
                    logger.warning("[V33] batched order-status poll error for %s: %s", tk, e)
                    continue
            for o in list(st.ladder):
                if not o.live or o.order_id is None:
                    continue
                fc = filled_by_oid.get(str(o.order_id))
                # D3: fc is the cumulative Decimal fill; on_poll_fill parses it Decimal and feeds the delta.
                if fc and fc > 0:
                    driver.on_poll_fill(o.order_id, fc, driver.server_now() or clock())
            continue
        for o in list(st.ladder):
            if not o.live or o.order_id is None:
                continue
            try:
                stt = executor.order_status(o.order_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[V33] order-status poll error: %s", e)
                continue
            # D3: the EXACT fractional fill (fill_count_fp), not the int-truncated filled_count.
            if stt.available and stt.filled_count_fp > 0:
                driver.on_poll_fill(o.order_id, stt.filled_count_fp, driver.server_now() or clock())


async def run_v33_window(
    shared, strike_conn, bucket_conn, driver: V33Driver, clock: Callable[[], float], deadline: float,
    gate_epoch: float, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    pump_interval: float = PUMP_INTERVAL_S, order_poll: bool = False, lag_sampler=None,
) -> None:
    await R._await_gate(gate_epoch, deadline, clock, sleep)
    from service.record_window import run_recording
    tasks = [
        run_recording(strike_conn, deadline=deadline, sleep=sleep),
        run_recording(bucket_conn, deadline=deadline, sleep=sleep),
        R._clock_pump(driver, clock, deadline, sleep, pump_interval, lag_sampler=lag_sampler),
    ]
    if order_poll:
        tasks.append(_order_status_poll_v33(driver, driver.executor, clock, deadline, sleep))
    await asyncio.gather(*tasks)


# ===========================================================================
# Finalize
# ===========================================================================
def _compute_v33_money(driver: V33Driver) -> dict[str, Any]:
    """The rung/count-aware money math (dry_sim in dry, realised in armed) + the operational counters."""
    money = compute_ladder_money_math(driver.state, dry_sim=driver.dry_sim)
    counters = {
        "rests_placed": int(getattr(driver.executor, "rests_placed", 0)),
        "rests_rejected": int(getattr(driver.executor, "rests_rejected", 0)),
        "amends_attempted": int(getattr(driver.executor, "amends_attempted", 0)),
        "amends_confirmed": int(getattr(driver.executor, "amends_confirmed", 0)),
        "amends_failed": int(getattr(driver.executor, "amends_failed", 0)),
        "amend_fallbacks": int(getattr(driver.executor, "amend_fallbacks", 0)),
        "cancels_attempted": int(getattr(driver.executor, "cancels_attempted", 0)),
        "cancels_confirmed": int(getattr(driver.executor, "cancels_confirmed", 0)),
        "rest_invariant_violations": int(getattr(driver.executor, "rest_invariant_violations", 0)),
        "rest_invariant_overflow": int(getattr(driver.executor, "rest_invariant_overflow", 0)),
        "rest_invariant_dup_price": int(getattr(driver.executor, "rest_invariant_dup_price", 0)),
        "batch_creates": int(getattr(driver.executor, "batch_creates", 0)),
        "dry_sim_fills": int(driver._dry_sim_fills),
        # PRINT-THROUGH (2026-09-26): trigger/stall receipts (0 unless the feature is enabled).
        "print_through_triggers": len(driver.state.print_through),
        "print_through_completes": int(getattr(driver.executor, "pt_bucket_no_takes", 0)),
        "print_through_complete_fills": int(getattr(driver.executor, "pt_bucket_no_fills", 0)),
        "print_through_unwinds": int(getattr(driver.executor, "pt_unwinds", 0)),
        "print_through_unwind_shortfalls": int(getattr(driver.executor, "pt_unwind_shortfalls", 0)),
    }
    return {**money, **counters}


def _v33_writer_stats(driver: V33Driver) -> dict[str, Any]:
    """The off-loop-writer telemetry for the ledger row (build brief §6): the DIRECT feed-gap proof plus
    the writer's queue-depth / latency / class mix (present only when the async writer ran this window)."""
    out: dict[str, Any] = {
        "async_writer": bool(driver._async),
        "feed_gap_max_s": round(driver._feed_gap_max_s, 4),
        "async_errors": int(driver._async_errors),
    }
    if driver._async and driver._aw is not None:
        out["writer"] = driver._aw.stats.summary()
        out["async_rate_limited"] = int(getattr(driver.executor, "async_rate_limited", 0))
        # duplicate wing retries dropped by the transport belt (one in-flight IOC per missing leg).
        out["wing_retries_dropped"] = int(getattr(driver.executor, "wing_retries_dropped", 0))
        # 2026-10-02: venue-confirms after a lost wing response; 429 sub-batches and the retries the backoff
        # window swallowed (no send).
        out["wing_venue_confirms"] = int(getattr(driver.executor, "wing_venue_confirms", 0))
        out["wing_rate_limited_batches"] = int(getattr(driver.executor, "wing_rate_limited_batches", 0))
        out["wing_backoff_skips"] = int(getattr(driver.executor, "wing_backoff_skips", 0))
    return out


def _fetch_venue_fills_v33(proxy: Any, close_iso: str, driver: V33Driver, journal: StreamJournal,
                           clock: Callable[[], float]) -> list[dict[str, Any]] | None:
    """GATE E (armed only, BEST-EFFORT): the venue's ``/portfolio/fills`` since the window start, FILTERED
    to our bucket orders by ``order_id`` (the executor's RestBook / by-order-id map). It is a cross-check
    on the ws/poll executor truth; a failed or unreadable GET is journaled ``venue_fills_unavailable`` and
    the window still reconciles against the executor's own observed fills. NEVER called in dry / tests (the
    reconcile ``venue_fills`` arg is injected or absent there — no network from an automated context)."""
    try:
        min_ts = int(close_epoch(close_iso)) - 1800   # the quote window opens well within 30 min of close
        body = proxy.rest_get("/portfolio/fills", {"min_ts": min_ts})
    except Exception as e:  # noqa: BLE001 — best-effort; the executor's own fills carry the reconciliation
        journal.append("venue_fills_unavailable", {"error": str(e)}, clock())
        return None
    fills = (body or {}).get("fills") if isinstance(body, dict) else None
    if not isinstance(fills, list):
        journal.append("venue_fills_unavailable", {"reason": "no fills list in /portfolio/fills"}, clock())
        return None
    known_oids = set(getattr(driver.executor, "_by_order_id", {}) or {})
    out: list[dict[str, Any]] = []
    for f in fills:
        if not isinstance(f, dict):
            continue
        oid = f.get("order_id")
        # Only OUR bucket orders. The account is SHARED across pilots, so this filter FAILS CLOSED: an
        # empty ``known_oids`` (we acked nothing this window) means we own nothing to reconcile, so NO venue
        # fill is ours. The pre-fix ``known_oids and ...`` disabled the filter on an empty map and would
        # have ingested another pilot's fills as ours (false unbooked / mismatch / one-legged -> false KILL).
        if oid is None or oid not in known_oids:
            continue
        cnt = f.get("count_fp")
        if cnt is None:
            cnt = f.get("count")
        out.append({"order_id": oid, "client_order_id": f.get("client_order_id"),
                    "count": cnt if cnt is not None else 0, "price": f.get("price"),
                    "ticker": f.get("ticker") or f.get("market_ticker")})
    journal.append("venue_fills_fetched", {"min_ts": min_ts, "ours": len(out),
                                           "total": len(fills)}, clock())
    return out


def _finalize(*, journal: StreamJournal, shared, driver: V33Driver, close_iso: str, resolved_mode: str,
              effective_mode: str, degrade: str | None, params: V33Params, strike_disc, bucket_map,
              journal_path: str, summary_path: str, ledger_path: str, strike_lag, bucket_lag,
              clock: Callable[[], float], m15_tickers=None, lag_stats=None, armed: bool = False,
              degrade_reason: str | None = None, recon=None, reconcile_alarms: int = 0) -> dict:
    journal.close()
    gz = R._gzip_journal(journal_path)
    final_path = os.path.abspath(gz.get("final_path") or journal_path)
    m = _compute_v33_money(driver)
    dry_sim = m.get("dry_sim", not armed)
    # GATE E: the single ``alarms`` number is driver + core + executor + ws (+ this window's reconcile
    # mismatch), with an auditable breakdown. ``ws_counts['alarm']`` alone (the pre-gate-E field) read 0 on
    # 02:00Z while the executor had journaled nine invariant alarms and the driver one stand-down alarm.
    alarms = alarm_breakdown(driver_counts=dict(driver.counts),
                             executor_counts=dict(driver.executor.counts),
                             ws_counts=dict(shared.counts), reconcile_alarms=reconcile_alarms)
    # GATE E: the row's economic facts (lots_filled, one_legged) and the defence-in-depth receipts come
    # from the RECONCILED set, never the core alone. In dry / no-fill windows the executor-truth set is
    # empty, so the reconciled values equal the core's and the row is byte-identical to the pre-gate-E row.
    recon_fields = recon.as_row_fields() if recon is not None else {}
    row = build_v33_ledger_row(
        close_time=close_iso, resolved_mode=resolved_mode, effective_mode=effective_mode,
        degrade=degrade, params=params, state=driver.state, driver_counts=dict(driver.counts),
        executor_counts=dict(driver.executor.counts), ws_counts=dict(shared.counts),
        strike_count=len(strike_disc.tickers), bucket_count=len(bucket_map),
        journal_path=final_path, record_count=len(journal), stand_down_reason=None, now=clock(),
        armed=armed, dry_sim=bool(dry_sim), degrade_reason=degrade_reason,
        strike_lag_seconds=strike_lag, bucket_lag_seconds=bucket_lag, lag_stats=lag_stats,
        last_quoted_bucket_ticker=driver._last_quoted_bucket_ticker,
        last_quoted_Sd=driver._last_quoted_Sd, last_quoted_Su=driver._last_quoted_Su,
        rung_fills=m.get("rung_fills"), wing_batch_sets=m.get("wing_batch_sets"),
        held_legs=m.get("held_legs"), floor_booked=m.get("floor_booked"),
        realized_delta=m.get("realized_delta"), realized_lock=m.get("realized_lock"),
        one_legged=recon_fields.get("one_legged", m.get("one_legged")),
        realized_unsettled=m.get("realized_unsettled", False),
        lots_filled=recon_fields.get("lots_filled", m.get("lots_filled", 0)),
        m15_tickers=list(m15_tickers or []),
        m15_frames=shared.m15_frames, deep_obs=driver.deep_obs.summary(),
        print_through=print_through_summary(driver.state),
        netted_sets=m.get("netted_sets"),   # D5: venue-netted wing pairs
        writer_stats=_v33_writer_stats(driver),
        alarms_breakdown=alarms, reconcile=recon_fields,
    )
    append_v33_ledger_row(row, ledger_path)
    summary = {
        "roster": "DegeneracyV3_3", "close_time": close_iso, "stand_down": False,
        "resolved_mode": resolved_mode, "effective_mode": effective_mode, "degrade": degrade,
        "dry_sim": bool(dry_sim), "params_sha": params.sha256, "journal_path": final_path,
        "records": len(journal), "strike_count": len(strike_disc.tickers),
        "bucket_count": len(bucket_map), "would_places": driver.counts.get("would_place_rest", 0),
        "replaces": driver.state.replace_count, "roll_count": driver.state.roll_count,
        "rungs_filled": driver.state.rungs_filled, "sets_done": driver.state.sets_done,
        "dry_sim_fills": driver._dry_sim_fills,
        "ladder_lock": row["ladder"].get("ladder_lock"), "realized_delta": row.get("realized_delta"),
        "spot_bucket_ticker": driver._last_quoted_bucket_ticker, "Sd": driver._last_quoted_Sd,
        "Su": driver._last_quoted_Su, "stand_downs": driver._real_stand_downs,
        "stand_down_reason": driver._last_stand_down_reason, "m15_frames": shared.m15_frames,
        "gzipped": bool(gz.get("gzipped")), "strike_lag_seconds": strike_lag,
        "bucket_lag_seconds": bucket_lag, "lag_stats": lag_stats or {}, "flushed_at": clock(),
    }
    _append_summary(summary_path, summary)
    return summary


def _stand_down(summary_path: str, ledger_path: str, close_iso: str, reason: str, *,
                resolved_mode: str, effective_mode: str, degrade: str | None,
                params_sha: str | None, clock: Callable[[], float]) -> int:
    sd = StandDown(close_iso, reason)
    write_standdown_summary(summary_path, sd, clock)
    row = build_v33_ledger_row(
        close_time=close_iso, resolved_mode=resolved_mode, effective_mode=effective_mode,
        degrade=degrade, params=None, state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=0, bucket_count=0, journal_path=None, record_count=0, stand_down_reason=reason,
        params_sha=params_sha, now=clock())
    append_v33_ledger_row(row, ledger_path)
    logger.info("[V33] stand down %s: %s", close_iso, reason)
    return 0


# ===========================================================================
# main
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V3.3 rolling-ladder window process (shakedown/dry/armed; missing mode -> dry, "
                    "never armed; armed degrades to dry unless S5 + reconcile + day-latch + S4 pass).")
    parser.add_argument("--close", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--mode", default=None, choices=list(VALID_MODES_V33),
                        help="Override the mode (else read ops/v33_mode.txt; unknown/absent -> dry).")
    parser.add_argument("--journal-dir", default=journal_dir_v33())
    parser.add_argument("--log-dir", default=log_dir_v33())
    parser.add_argument("--ledger", default=ledger_path_v33())
    parser.add_argument("--mode-file", default=mode_path_v33())
    parser.add_argument("--falsifier", default=DEFAULT_FALSIFIER_PATH_V33)
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    parser.add_argument("--flush-every", type=int, default=200)
    parser.add_argument("--batch-create", action="store_true",
                        help="Group multi-place ticks into batch creates (armed only; default OFF).")
    parser.add_argument("--async-writer", action="store_true",
                        help="Send orders OFF the event loop via the async writer (armed only; default "
                             "OFF, also settable via DV3_V33_ASYNC_WRITER). Shake down in dry-adjacent "
                             "before arming with it.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    proxy_base_url = args.proxy_base or default_proxy_base()
    proxy = ProxyAuth(base_url=proxy_base_url)
    close_iso = args.close or next_top_of_hour_iso(clock())
    summary_path = os.path.join(args.journal_dir, "summary.jsonl")

    # HARD STOP armed HERE -- earliest point the deadline is known, BEFORE the first proxy call
    # (settlement-backfill sweep + discovery), so a synchronous wake/discovery hang is covered too.
    # V3.3 also runs under service.supervisor, whose external watchdog kills at close + 120 s; this
    # in-process stop lands a hair later (close + 130 s) so the supervisor, when present, always wins
    # the race -- but the guarantee still holds if V3.3 is ever run bare. The daemon timer fires at the
    # ABSOLUTE close + 130 s regardless of when armed; a stand-down returns long before that and the
    # daemon timer dies with the process. Cancelled on the normal exit path below.
    deadline = close_epoch(close_iso) + GRACE_SECONDS
    hard_stop = arm_hard_stop(deadline, close_iso)

    resolved_mode = resolve_v33_mode(args.mode, args.mode_file)

    try:
        params = load_v33_params()
    except (V33ParamsShaMismatch, V33ParamsInvalid, KeyError, OSError, ValueError) as e:
        return _stand_down(summary_path, args.ledger, close_iso, f"params_load_failed: {e}",
                           resolved_mode=resolved_mode, effective_mode="shakedown", degrade=None,
                           params_sha=None, clock=clock)

    # prepare(): settlement backfill for prior ARMED windows still realized_unsettled (dry_sim skipped).
    try:
        prior_rows = load_v33_rows(args.ledger)
        backfills = v33_settlement_backfill_sweep(
            prior_rows, lambda tk: R.fetch_market_result_v32(proxy, tk), clock())
        for bf in backfills:
            append_v33_ledger_row(bf, args.ledger)
        if backfills:
            logger.info("[V33] settlement backfill appended %d row(s)", len(backfills))
    except Exception as e:  # noqa: BLE001
        logger.warning("[V33] settlement backfill sweep failed: %s", e)

    effective_mode = resolved_mode
    degrade: str | None = None
    degrade_reason: str | None = None

    strike_disc = R.discover_strike_ladder(proxy, close_iso, clock())
    range_disc = discover_range_markets(proxy, close_iso, clock())
    m15_disc, m15_error = R.discover_co_settling_15m_safe(proxy, close_iso, clock())
    bucket_map = R.build_bucket_map(range_disc)
    bucket_map, dropped_width = R.filter_buckets_to_width(bucket_map, params.bucket_width)

    if not bucket_map:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"no KXBTC range buckets co-settling at {close_iso}",
                           resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
                           params_sha=params.sha256, clock=clock)
    if not strike_disc.tickers:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"no KXBTCD strike ladder co-settling at {close_iso}",
                           resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
                           params_sha=params.sha256, clock=clock)
    obs_width = R.observed_bucket_width(bucket_map)
    if obs_width is not None and obs_width != params.bucket_width:
        return _stand_down(summary_path, args.ledger, close_iso,
                           f"bucket width {obs_width} != params.bucket_width {params.bucket_width}",
                           resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
                           params_sha=params.sha256, clock=clock)

    logger.info("[V33] %s: %d strikes, %d buckets, mode=%s effective=%s", close_iso,
                len(strike_disc.tickers), len(bucket_map), resolved_mode, effective_mode)

    journal_path = os.path.join(args.journal_dir, journal_filename(close_iso))
    journal = StreamJournal(journal_path, flush_every=args.flush_every)
    journal.open()
    journal.append("window_meta",
                   {"roster": "DegeneracyV3_3", "close_time": close_iso, "resolved_mode": resolved_mode,
                    "effective_mode": effective_mode, "params_sha": params.sha256,
                    "strike_series": STRIKE_SERIES, "range_series": RANGE_SERIES,
                    "strike_count": len(strike_disc.tickers), "bucket_count": len(bucket_map),
                    "bucket_width": params.bucket_width, "rungs": params.rungs,
                    "m15_series": FIFTEEN_SERIES, "m15_tickers": list(m15_disc.tickers)}, clock())

    # --- arming resolution (S5 + reconcile-first + day latch + S4) ---
    writer: ProxyWriter | None = None
    health: Any = None
    if resolved_mode == "armed":
        writer = ProxyWriter(proxy_auth=proxy, base_url=proxy_base_url)
        health = R.get_health(proxy_base_url)
        try:
            positions = proxy.rest_get("/portfolio/positions")
        except Exception as e:  # noqa: BLE001
            positions = None
            logger.warning("[V33] positions read failed: %s", e)
        utc_day = close_iso[:10]
        guard_path = _resolve_v33_guard_path(utc_day)
        day_guard = read_day_guard(guard_path, utc_day)
        s4 = None
        try:
            bal = parse_balance(proxy.rest_get("/portfolio/balance"))
            if bal.ok and not day_guard.corrupt:
                start, _first = ensure_balance_start(guard_path, utc_day, bal.dollars, clock())
                if start is not None:
                    pending = v33_pending_credit(load_v33_rows(args.ledger), utc_day)
                    s4 = v33_s4_decision(start, bal.dollars, pending)
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33] balance/S4 read failed: %s", e)
        outcome = decide_v33_arming(
            resolved_mode=resolved_mode, falsifier_path=args.falsifier, health=health,
            positions=positions, params_verified=True, lots_per_rung=params.lots_per_rung,
            day_guard=day_guard, s4=s4, k_rungs=params.rungs, rung_lots=params.rung_lots)
        effective_mode = outcome.effective_mode
        if not outcome.armed:
            degrade = "degrade_to_dry"
            degrade_reason = "; ".join(outcome.reasons)
            journal.append("degrade_to_dry", {"reason": degrade_reason, "from_mode": resolved_mode,
                                               "reasons": list(outcome.reasons)}, clock())
            logger.warning("[V33] ARMED refused -> dry: %s", degrade_reason)
        elif _proxy_max_contracts(health) is None:
            # BELT (R2-N2): S5 passed but the /health contract cap read came back unreadable (a race). An
            # armed executor MUST NOT guess the venue cap -> degrade to dry rather than size wings against a
            # guess (build_executor_v33 would refuse to build it anyway; degrade so the window still runs).
            effective_mode = "dry"
            degrade = "degrade_to_dry"
            degrade_reason = "proxy contract cap (/health max_contracts_per_order) unreadable at arm"
            journal.append("degrade_to_dry", {"reason": degrade_reason, "from_mode": resolved_mode,
                                               "reasons": [degrade_reason]}, clock())
            logger.warning("[V33] ARMED refused -> dry: %s", degrade_reason)

    armed = effective_mode == "armed"
    shakedown = not armed
    include_private = armed
    dry_sim = not armed   # dry/shakedown -> simulate the ladder fills for the side-by-side row

    cts = close_epoch(close_iso)
    state = V33State.new(close_iso, cts, bucket_map, params, shakedown=shakedown)

    exch_map: dict[str, int | None] = dict(strike_disc.exchange_index_by_ticker)
    for b in range_disc.buckets:
        if getattr(b, "ticker", None):
            exch_map[b.ticker] = coerce_exchange_index(getattr(b, "exchange_index", None))

    # wing-take chunk cap = min(params hint, the proxy's live MAX_CONTRACTS_PER_ORDER). At the proxy's
    # 2 today, K rung fills chunk into ceil(K/2) IOC takes per wing; if Brad raises it to 11, 1 take/wing.
    # BELT (R2-N2): when the /health cap is UNREADABLE, wing_cap stays None so build_executor_v33 REFUSES
    # to build an armed executor (fail closed, never guess). Dry never reads the cap (FrozenExecutor).
    proxy_cap = _proxy_max_contracts(health)
    wing_cap = (min(params.max_contracts_per_order_hint, proxy_cap)
                if proxy_cap is not None else None)
    # OFF-LOOP writer (2026-09-30): armed + --async-writer/DV3_V33_ASYNC_WRITER -> route every order round
    # trip off the loop. The thread pools are created now (no loop needed); the first run_in_executor binds
    # the loop inside run_v33_window. DEFAULT OFF -> the synchronous path is unchanged.
    use_async = armed and writer is not None and async_writer_enabled(args.async_writer)
    async_writer = AsyncOrderWriter(writer) if use_async else None
    if use_async:
        journal.append("async_writer_enabled", {"from_mode": resolved_mode}, clock())
        logger.info("[V33] OFF-LOOP async order writer ENABLED for this armed window.")
    executor = build_executor_v33(effective_mode, bucket_map=bucket_map, exchange_index_by_ticker=exch_map,
                                  journal=journal, close_epoch_val=cts, params=params, writer=writer,
                                  clock=clock, batch_create=args.batch_create, wing_cap=wing_cap,
                                  async_writer=async_writer)
    driver = V33Driver(params, state, journal, executor, dry_sim=dry_sim,
                       batch_create=args.batch_create, clock=clock, async_writer=async_writer)
    shared = R.V32Recorder(journal, driver, clock=clock, m15_tickers=frozenset(m15_disc.tickers))

    if armed and writer is not None:
        try:
            cancel_stale_open_orders(writer, journal, clock)
        except Exception as e:  # noqa: BLE001
            logger.warning("[V33] startup open-order cancel failed: %s", e)

    strike_ws = KalshiWebSocketClient(proxy_auth=proxy, tickers=list(strike_disc.tickers),
                                      callbacks=shared.callbacks(False), include_private=False,
                                      record=shared.tap, clock=clock, channels=STRIKE_CHANNELS)
    bucket_sub_tickers = sorted(set(bucket_map) | set(m15_disc.tickers))
    bucket_ws = KalshiWebSocketClient(proxy_auth=proxy, tickers=bucket_sub_tickers,
                                      callbacks=shared.callbacks(include_private),
                                      include_private=include_private, record=shared.tap, clock=clock,
                                      channels=BUCKET_CHANNELS)
    strike_conn = R._ConnRecorder(shared, strike_ws, "strikes", list(strike_disc.tickers))
    bucket_conn = R._ConnRecorder(shared, bucket_ws, "buckets", bucket_sub_tickers)

    # `deadline` (= cts + GRACE_SECONDS) was computed and the hard stop armed at the top of main(),
    # before discovery; `cts` here is the same close_epoch(close_iso).
    gate = R.connect_gate_epoch(cts, params)
    lag_sampler = R.LagSampler({"strikes": strike_ws, "buckets": bucket_ws})
    # Outer try/finally so the hard stop is cancelled even if _finalize itself RAISES (F4); a wedge
    # (finalize HANGS) never reaches the cancel, so the timer still fires -- exactly what we want.
    try:
        try:
            asyncio.run(run_v33_window(shared, strike_conn, bucket_conn, driver, clock, deadline, gate,
                                       order_poll=armed, lag_sampler=lag_sampler))
        except KeyboardInterrupt:
            logger.warning("[V33] Ctrl+C — flushing streamed journal.")
        finally:
            # GATE E (2026-10-03 02:00Z): reconcile the window against EXECUTOR truth (the driver's observed
            # fills + executor.fills + a best-effort venue /portfolio/fills), while the journal is still
            # open so the defence-in-depth receipts (unbooked_fill, ledger_reconcile_mismatch) are recorded.
            # Fail-safe: a reconciliation error must NEVER wedge the window close -> fall back to core truth.
            recon = None
            reconcile_alarms = 0
            try:
                venue_fills = (_fetch_venue_fills_v33(proxy, close_iso, driver, journal, clock)
                               if armed else None)
                recon = reconcile_live(driver, venue_fills)
                for ub in recon.unbooked_fills:
                    journal.append("unbooked_fill", ub, clock())
                if recon.reconcile_mismatch:
                    reconcile_alarms = 1
                    journal.append("alarm", {"alarm": "ledger_reconcile_mismatch",
                                             **(recon.mismatch_detail or {})}, clock())
            except Exception as e:  # noqa: BLE001 -- gate E accounting never blocks the close/row write
                # The reconcile_failed alarm IS a kind-``alarm`` journal record, so it must be COUNTED or
                # the live alarms_breakdown.total would under-count it vs the journal / a rebuild.
                reconcile_alarms = 1
                journal.append("alarm", {"alarm": "reconcile_failed", "error": str(e)}, clock())
                # STRICTER-OF-THE-TWO fallback: a mid-reconcile error must NOT drop the window back to the
                # blind core (the 02:00Z failure mode read core one_legged False with two lots naked).
                # Reconcile executor truth against the completed-hedge coverage ALONE (no core, no venue) so
                # a naked fill is still surfaced on the row and counted one-legged; if even that raises,
                # recon stays None and the S1 belt below uses the raw executor-truth evidence.
                try:
                    recon = reconcile_exec_truth_only(driver)
                except Exception:  # noqa: BLE001
                    recon = None
            # S1_LEGGED comes from the RECONCILED one-legged (executor truth), never the core alone: the
            # 02:00Z core read one_legged False while the executor held two naked lots. If reconciliation
            # failed OUTRIGHT (recon None), the fallback is the STRICTER of the two -- the core mirror OR any
            # executor-truth rest fill observed this window (never the blind core alone), so a naked lot
            # still latches S1 even when every reconcile path raised.
            s1_one_legged = (recon.one_legged if recon is not None
                             else (bool(driver.state.one_legged) or bool(driver._exec_truth_fills)))
            if armed and s1_one_legged:
                try:
                    utc_day = close_iso[:10]
                    n = record_legged_occurrence(_resolve_v33_guard_path(utc_day), utc_day, close_iso,
                                                 "ladder set left one-legged below lock floor (reconciled "
                                                 "against executor truth)", clock())
                    olc = str(recon.one_legged_contracts) if recon is not None else None
                    journal.append("s1_legged_occurrence",
                                   {"count": n, "latch_threshold": V33_S1_LEGGED_LATCH_THRESHOLD,
                                    "one_legged_contracts": olc}, clock())
                except Exception as e:  # noqa: BLE001
                    logger.warning("[V33] S1_LEGGED record failed: %s", e)
            summary = _finalize(journal=journal, shared=shared, driver=driver, close_iso=close_iso,
                                resolved_mode=resolved_mode, effective_mode=effective_mode, degrade=degrade,
                                params=params, strike_disc=strike_disc, bucket_map=bucket_map,
                                journal_path=journal_path, summary_path=summary_path, ledger_path=args.ledger,
                                strike_lag=lag_sampler.mean("strikes"), bucket_lag=lag_sampler.mean("buckets"),
                                lag_stats=lag_sampler.summaries(), clock=clock,
                                m15_tickers=list(m15_disc.tickers), armed=armed, degrade_reason=degrade_reason,
                                recon=recon, reconcile_alarms=reconcile_alarms)
            logger.info("[V33] window done: %s", summary)
    finally:
        hard_stop.cancel()  # normal exit / _finalize error: disarm the belt-and-braces hard stop
        if async_writer is not None:
            async_writer.close()  # shut the off-loop pools at window end (idempotent; never raises)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
