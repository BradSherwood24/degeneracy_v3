"""stops.py — the alarm/stop state machine (pure core) + a thin controller that freezes the
Executor and dispatches the stop's position policy.

Alarms (notify, keep running):
  A1 slippage > 2c per fill side (|response.average_fill_price - intent.limit_price|).
  A2 ladder-map deviation (carried from the WakeContext ladder flag).
  A3 entry-signal rate out of [12%,55%] (rolling 3 days) — a counter fed by the harness.
  A4 >= 5 book-guard trips in one UTC day -> stand down for the day.

Stops (freeze orders; then apply the position policy):
  S1 arithmetic (n=1): a FILLED sub-$1 pair whose worst-case realized < 0 using ACTUAL fees.
  S2 imbalance the protocol could not restore to 1:1/0:0 within its bounds (Reconciler GiveUp / an
     imbalanced end-state at settlement).
  S3 a non-fill believed a fill, or ANY reconciliation mismatch vs exchange truth.
  S4 daily realized-loss cap ($5.00).
  S5 ARMING refusal: policy sha unverified, falsifier STATUS line not exactly `STATUS: FROZEN`, or
     proxy /health missing caps / orders_enabled -> refuse to arm (never fires an order to begin
     with). Same discipline as the sealed-read loader.

A tripped stop LATCHES (never un-trips within a window), FREEZES the executor (armed=False), applies
the position policy per the two PENDING-BRAD flags, and emits a notification record.

==================== PENDING-BRAD (review F4/F8 DEFERRED; falsifier draft) ====================
Two position-action flags on a stop are NOT YET RULED by Brad. Defaults implemented here:
  * hold_complete_floor_pairs_to_settlement = True  -> a COMPLETE sub-$1 pair is held to expiry
    (strictly safe: a filled sub-$1 pair pays >= $1; flattening it would realize a needless loss).
  * flatten_unprotected_exposure          = True  -> strangle legs and unpaired flip overhang are
    flattened (reduce_only sells) to remove exposure the floor does not protect.
"Freeze all order placement" is read as: freeze all STRATEGY placement (entries/rebalances) ALWAYS;
the flatten above is stop-authorized RISK REDUCTION, dispatched via the Executor's stop_authorized
flatten path (reduce_only, still proxy-capped), not strategy. Both flags and this reading MUST be
confirmed by Brad before Phase 5.
===============================================================================================
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_FLATTEN,
    SUB_DOLLAR_FLIP,
    WIDE_BOX,
    Intent,
    IntentLeg,
    LedgerState,
)

# --- alarm / stop kinds ---
A1_SLIPPAGE = "A1"
A2_LADDER = "A2"
A3_RATE = "A3"
A4_GUARD = "A4"
S1_ARITH = "S1"
S2_IMBALANCE = "S2"
S3_RECON = "S3"
S4_DAILY_LOSS = "S4"
S5_ARM = "S5"

_FROZEN_LINE = "STATUS: FROZEN"


@dataclass(frozen=True)
class StopConfig:
    slippage_alarm_dollars: Decimal = Decimal("0.02")
    # S4 daily-loss cap (dollars). Brad's ruling 2026-08-26: S4's SOURCE OF TRUTH is the ACCOUNT
    # BALANCE (this account runs no other strategy, so balance delta == P&L), checked at each wake;
    # the repaired ledger realized is the SECONDARY, reported figure. Default $3.00 as a constant for
    # now — the box falsifier will pin it.
    daily_loss_cap_dollars: Decimal = Decimal("3.00")
    guard_trips_standdown: int = 5
    # PENDING-BRAD (F8) — see module docstring.
    hold_complete_floor_pairs_to_settlement: bool = True
    flatten_unprotected_exposure: bool = True


# Stops that HALT THE DAY (the falsifier: a stop halts the DAY, not just the window). These latch to
# the day-scoped guard file and refuse arming for the rest of the UTC day. S5 is an arming refusal
# (never a trip), so it is not in this set.
DAY_HALTING_STOPS = (S1_ARITH, S2_IMBALANCE, S3_RECON, S4_DAILY_LOSS)


@dataclass(frozen=True)
class Notification:
    kind: str  # an A* alarm or S* stop
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StopState:
    """Immutable alarm/stop state. ``tripped`` latches; ``armed`` is False whenever tripped."""

    tripped: tuple[str, ...] = ()
    alarms: tuple[Notification, ...] = ()
    notifications: tuple[Notification, ...] = ()
    guard_trips: int = 0
    daily_realized: Decimal = Decimal(0)

    @property
    def is_stopped(self) -> bool:
        return bool(self.tripped)

    def has(self, stop: str) -> bool:
        return stop in self.tripped


# ---------------------------------------------------------------------------
# Pure transitions
# ---------------------------------------------------------------------------
def apply_stop(state: StopState, stop: str, reason: str, detail: dict | None = None) -> StopState:
    """Latch a stop (idempotent) and emit its notification. Freezing the executor is the
    controller's side effect."""
    note = Notification(kind=stop, reason=reason, detail=detail or {})
    tripped = state.tripped if stop in state.tripped else state.tripped + (stop,)
    return replace(state, tripped=tripped, notifications=state.notifications + (note,))


def apply_alarm(state: StopState, alarm: str, reason: str, detail: dict | None = None) -> StopState:
    note = Notification(kind=alarm, reason=reason, detail=detail or {})
    return replace(
        state,
        alarms=state.alarms + (note,),
        notifications=state.notifications + (note,),
    )


def check_slippage_alarms(
    intent: Intent, responses: tuple[Any, ...], config: StopConfig
) -> tuple[Notification, ...]:
    """A1: for each FILLED leg, |average_fill_price - intent limit| > threshold => a slippage alarm.
    (For an IOC buy at the observed ask the fill cannot exceed the limit, so this is typically the
    price-improvement magnitude; the alarm flags any >2c per-side deviation, bin-4 magnitude.)"""
    by_cid = {leg.client_order_id: leg for leg in intent.legs}
    out: list[Notification] = []
    for r in responses:
        if r.no_fill or r.fill_count <= 0 or r.average_fill_price is None:
            continue
        leg = by_cid.get(r.client_order_id)
        if leg is None:
            continue
        slip = abs(r.average_fill_price - leg.limit_price)
        if slip > config.slippage_alarm_dollars:
            out.append(
                Notification(
                    kind=A1_SLIPPAGE,
                    reason=f"slippage {slip} > {config.slippage_alarm_dollars} on {leg.ticker}",
                    detail={
                        "ticker": leg.ticker,
                        "limit": str(leg.limit_price),
                        "avg_fill": str(r.average_fill_price),
                        "slippage": str(slip),
                    },
                )
            )
    return tuple(out)


def record_guard_trip(state: StopState, config: StopConfig) -> tuple[StopState, Notification | None]:
    """A4: increment the book-guard trip counter; at the threshold emit a stand-down-for-the-day
    alarm (an alarm, not a stop — the day stands down, orders are not force-frozen by A4 itself)."""
    n = state.guard_trips + 1
    state = replace(state, guard_trips=n)
    if n == config.guard_trips_standdown:
        note = Notification(
            kind=A4_GUARD,
            reason=f"{n} book-guard trips today -> stand down for the day",
            detail={"guard_trips": n},
        )
        return replace(state, alarms=state.alarms + (note,), notifications=state.notifications + (note,)), note
    return state, None


def on_realized(
    state: StopState, delta: Decimal, config: StopConfig
) -> tuple[StopState, bool]:
    """Fold a realized-P&L delta into the daily tally. If the daily realized loss reaches the cap
    (daily_realized <= -cap), latch S4. Returns (state, tripped_s4)."""
    total = state.daily_realized + delta
    state = replace(state, daily_realized=total)
    if total <= -config.daily_loss_cap_dollars:
        return apply_stop(
            state, S4_DAILY_LOSS,
            f"daily realized {total} <= -{config.daily_loss_cap_dollars}",
            {"daily_realized": str(total)},
        ), True
    return state, False


def check_s1(state: LedgerState) -> str | None:
    """S1 (arithmetic, n=1): a FILLED sub-$1 flip pair whose worst-case realized < 0 with ACTUAL
    fees. Only sub-$1 flip is a guaranteed >= $1/pair floor, so S1 is scoped to that source. Returns
    a reason string on violation, else None."""
    if state.source != SUB_DOLLAR_FLIP:
        return None
    if state.matched_pairs() <= 0:
        return None
    realized = state.realized_min()
    if realized < 0:
        return (
            f"sub-$1 flip pair matched={state.matched_pairs()} realized_min={realized} < 0 "
            f"(net cash out {state.pair_net_cash_out()} with ACTUAL fees; floor violated)"
        )
    return None


def check_s1_box(state: LedgerState, pair_cost_max: Decimal) -> str | None:
    """S1 for the WIDE BOX (units + booked-cost tripwire). The corridor ``check_s1`` (whose
    ``realized_min < 0`` test would trip on EVERY normal box pair, since a box costs ~$1.85 for a $1
    floor) is scoped to sub-$1 flip and returns None here; the box uses THIS check instead. Trips iff:

      * any FILLED leg's average fill price exceeds its OWN entry limit (a units tripwire — an IOC
        buy can never fill above its limit, so this can only mean a units/side-space corruption like
        the 2026-08-23 NO-in-YES-space bug), OR
      * the pair's BOOKED COST (both legs, fees in) exceeds ``pair_cost_max`` (the roster ceiling
        $1.99) — such a box is a guaranteed loss against the $2 pinned ceiling.

    Returns a reason string on violation, else None. Scoped to the box source (fail-closed None for
    any other source)."""
    if state.source != WIDE_BOX:
        return None
    entry = next((i for i in state.intents if i.purpose == PURPOSE_ENTRY), None)
    limits = {(lg.ticker, lg.side): lg.limit_price for lg in (entry.legs if entry else ())}
    cost = Decimal(0)
    for which in ("high", "low"):
        pos = state.position(which)
        if pos is None:
            continue
        cost += pos.bought_cost
        if pos.avg_buy_price is not None:
            lim = limits.get((pos.ticker, pos.side))
            if lim is not None and pos.avg_buy_price > lim:
                return (
                    f"box units tripwire: {pos.ticker} ({pos.side}) avg fill {pos.avg_buy_price} "
                    f"> own limit {lim} (an IOC buy cannot fill above its limit — units/side-space "
                    f"corruption suspected)"
                )
    if cost > pair_cost_max:
        return (
            f"box pair booked cost {cost} (both legs, fees in) > pair_cost_max {pair_cost_max} "
            f"(guaranteed loss against the $2 pinned ceiling)"
        )
    return None


# ---------------------------------------------------------------------------
# Position policy on a stop (the two PENDING-BRAD flags)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionAction:
    kind: str  # "hold" | "flatten"
    ticker: str
    side: str
    count: int
    reason: str


def position_policy(state: LedgerState, config: StopConfig) -> tuple[PositionAction, ...]:
    """Compute the per-leg hold/flatten actions for a tripped stop (PENDING-BRAD F8 defaults)."""
    actions: list[PositionAction] = []
    matched = state.matched_pairs()
    is_flip = state.source == SUB_DOLLAR_FLIP
    for which in ("high", "low"):
        pos = state.position(which)
        if pos is None or pos.net <= 0:
            continue
        if is_flip and config.hold_complete_floor_pairs_to_settlement:
            protected = min(pos.net, matched)
        else:
            protected = Decimal(0)
        unprotected = pos.net - protected
        if protected > 0:
            actions.append(
                PositionAction(
                    "hold", pos.ticker, pos.side, int(protected),
                    "complete sub-$1 floor pair held to settlement (PENDING-BRAD F8)",
                )
            )
        if unprotected > 0:
            if config.flatten_unprotected_exposure:
                actions.append(
                    PositionAction(
                        "flatten", pos.ticker, pos.side, int(unprotected),
                        ("strangle leg" if not is_flip else "unpaired flip overhang")
                        + " flattened as unprotected exposure (PENDING-BRAD F8)",
                    )
                )
            else:
                actions.append(
                    PositionAction(
                        "hold", pos.ticker, pos.side, int(unprotected),
                        "unprotected exposure but flatten disabled (PENDING-BRAD F8)",
                    )
                )
    return tuple(actions)


def build_flatten_intent(
    action: PositionAction,
    window: str,
    source: str,
    bid_price: Decimal,
    client_order_id: str,
) -> Intent:
    """Turn a 'flatten' PositionAction into a reduce_only SELL intent at the observed bid (IOC).
    A sell of the held outcome reduces the position; reduce_only guarantees it can never open the
    opposite side."""
    leg = IntentLeg(
        ticker=action.ticker,
        side=action.side,
        action="sell",
        count=action.count,
        limit_price=bid_price,
        client_order_id=client_order_id,
        reduce_only=True,
    )
    return Intent(window=window, source=source, purpose=PURPOSE_FLATTEN, legs=(leg,))


# ---------------------------------------------------------------------------
# Arming (S5) — refuse unless every precondition holds
# ---------------------------------------------------------------------------
def falsifier_is_frozen(falsifier_path: str) -> bool:
    """True iff the falsifier file has a line that is EXACTLY `STATUS: FROZEN` (after strip). Same
    fail-closed discipline as the sealed-read loader: absence or any other value => not frozen."""
    try:
        with open(falsifier_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip() == _FROZEN_LINE:
                    return True
    except OSError:
        return False
    return False


def health_has_caps(health: Any) -> bool:
    """True iff a proxy /health payload shows the caps block AND orders_enabled True."""
    if not isinstance(health, dict):
        return False
    if not health.get("orders_enabled"):
        return False
    caps = health.get("caps")
    if not isinstance(caps, dict):
        return False
    return all(
        k in caps for k in ("max_contracts_per_order", "ticker_prefixes", "daily_order_budget")
    )


@dataclass(frozen=True)
class ArmDecision:
    armed: bool
    reasons: tuple[str, ...] = ()


def arming_check(
    falsifier_path: str,
    health: Any,
    policy_verified: bool,
) -> ArmDecision:
    """S5 gate. armed=True ONLY if the falsifier is FROZEN, the policy sha is verified, and the
    proxy /health shows caps + orders_enabled. Any failure => refuse (armed=False) with reasons."""
    reasons: list[str] = []
    if not policy_verified:
        reasons.append("policy sha not verified (load_policy must succeed against the frozen pin)")
    if not falsifier_is_frozen(falsifier_path):
        reasons.append(
            f"falsifier STATUS line is not exactly '{_FROZEN_LINE}' at {os.path.basename(falsifier_path)}"
        )
    if not health_has_caps(health):
        reasons.append("proxy /health missing caps block or orders_enabled is not true")
    return ArmDecision(armed=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Day-scoped guard file (BUG-3 repair): stop LATCHING + S4 balance baseline across the
# process-per-window boundary. One file per UTC day at ops/stops_YYYY-MM-DD.json holds:
#   * ``latched``: the day-halting stops (S1-S4) tripped so far today -> refuse to arm for the rest
#     of the day (the falsifier: a stop halts the DAY). Each window is a fresh :40 process, so this
#     FILE is the only thing that persists a trip; nothing else does.
#   * ``balance_start_dollars``: the account balance snapshot at the FIRST wake of the day (Decimal
#     dollars, sub-cent precision). S4's source of truth is the account balance (Brad 2026-08-26):
#     loss = balance_start - balance_now at every wake; loss >= cap -> latch S4.
# A NEW UTC day gets a fresh file (path is day-scoped). A CORRUPT/unreadable file FAILS CLOSED: the
# arming check treats it as "cannot confirm no latch" and refuses to arm, and NOTHING overwrites it
# (no self-heal) — it keeps refusing every wake that UTC day until a human repairs it (F3 review).
# ---------------------------------------------------------------------------
_DAY_GUARD_PREFIX = "stops_"
# The proxy /portfolio/balance carries BOTH cents (int ``balance``) and dollars (str
# ``balance_dollars``, sub-cent). We parse dollars as the primary value and require the two agree to
# within this tolerance, else fail closed (units-confusion guard, F2 review).
_BALANCE_MISMATCH_TOL = Decimal("0.01")


@dataclass(frozen=True)
class DayGuard:
    """Parsed day-guard file. ``corrupt`` True means the file existed but could not be trusted
    (unparseable / wrong shape) -> callers must fail closed (refuse to arm) and MUST NOT overwrite
    it. ``balance_start_dollars`` is the day's S4 baseline as a Decimal-dollar string."""

    utc_day: str
    balance_start_dollars: str | None = None
    latched: tuple[dict, ...] = ()
    corrupt: bool = False
    exists: bool = False

    def balance_start(self) -> Decimal | None:
        """The baseline as a Decimal (None if unset)."""
        return None if self.balance_start_dollars is None else Decimal(self.balance_start_dollars)


def day_guard_path(ops_dir: str, utc_day: str) -> str:
    """The day-scoped guard file path: ops/stops_YYYY-MM-DD.json (UTC day)."""
    return os.path.join(ops_dir, f"{_DAY_GUARD_PREFIX}{utc_day}.json")


def read_day_guard(path: str, utc_day: str) -> DayGuard:
    """Read the day-guard file. Missing -> a fresh empty guard (not corrupt). Present but
    unparseable/wrong-shape/wrong-day -> corrupt=True (fail closed at the call site)."""
    if not os.path.exists(path):
        return DayGuard(utc_day=utc_day, exists=False)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.error("[STOPS] day-guard file unreadable/corrupt: %s", path)
        return DayGuard(utc_day=utc_day, corrupt=True, exists=True)
    if not isinstance(data, dict) or data.get("utc_day") != utc_day:
        logger.error("[STOPS] day-guard file malformed or wrong day: %s", path)
        return DayGuard(utc_day=utc_day, corrupt=True, exists=True)
    latched = data.get("latched")
    if not isinstance(latched, list) or not all(isinstance(x, dict) for x in latched):
        logger.error("[STOPS] day-guard 'latched' malformed: %s", path)
        return DayGuard(utc_day=utc_day, corrupt=True, exists=True)
    bsd = data.get("balance_start_dollars")
    if bsd is not None:
        try:
            bsd = str(Decimal(str(bsd)))  # validate it is a real decimal; keep as string
        except (InvalidOperation, ValueError, TypeError):
            logger.error("[STOPS] day-guard 'balance_start_dollars' malformed: %s", path)
            return DayGuard(utc_day=utc_day, corrupt=True, exists=True)
    return DayGuard(
        utc_day=utc_day, balance_start_dollars=bsd, latched=tuple(latched), exists=True
    )


def _write_day_guard(path: str, guard: DayGuard) -> None:
    """Atomically write the guard file (temp + os.replace)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    payload = {
        "utc_day": guard.utc_day,
        "balance_start_dollars": guard.balance_start_dollars,
        "latched": list(guard.latched),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
    os.replace(tmp, path)


def latched_stop_kind(guard: DayGuard) -> str | None:
    """The first day-halting stop kind latched in the guard, else None. (A corrupt guard is handled
    separately by the caller — this only reports explicit latches.)"""
    for entry in guard.latched:
        kind = entry.get("kind")
        if kind in DAY_HALTING_STOPS:
            return kind
    return None


def record_latched_stop(
    path: str, utc_day: str, kind: str, reason: str, window: str | None, ts: float
) -> None:
    """Append a day-halting stop latch to the guard file (read-modify-write). Preserves any existing
    balance baseline and prior latches. Idempotent per kind is NOT required — repeated trips just
    append; ``latched_stop_kind`` reports the first day-halting kind regardless."""
    guard = read_day_guard(path, utc_day)
    # Even a corrupt guard must not swallow a latch: rebuild a minimal guard carrying THIS latch so
    # the day still refuses to arm (the corrupt read already fails closed independently).
    latched = tuple(guard.latched) if not guard.corrupt else ()
    bsd = guard.balance_start_dollars if not guard.corrupt else None
    new = DayGuard(
        utc_day=utc_day,
        balance_start_dollars=bsd,
        latched=latched + ({"kind": kind, "reason": reason, "window": window, "ts": ts},),
    )
    _write_day_guard(path, new)


def ensure_balance_start(
    path: str, utc_day: str, balance_now: Decimal, ts: float
) -> tuple[Decimal | None, bool]:
    """Ensure the day's balance baseline exists. If the guard has no baseline yet (first wake of the
    day), snapshot ``balance_now`` (Decimal dollars) and persist it. Returns (balance_start,
    first_wake).

    F3 (review): a CORRUPT guard is NEVER overwritten here — self-healing would erase the day's
    latches/baseline and let arming resume next window. On corruption this returns (None, False) and
    writes nothing; the caller already refuses to arm on a corrupt guard and keeps refusing all day."""
    guard = read_day_guard(path, utc_day)
    if guard.corrupt:
        return None, False
    existing = guard.balance_start()
    if existing is not None:
        return existing, False
    new = DayGuard(
        utc_day=utc_day, balance_start_dollars=str(balance_now), latched=tuple(guard.latched)
    )
    _write_day_guard(path, new)
    return balance_now, True


@dataclass(frozen=True)
class BalanceRead:
    """Parsed /portfolio/balance. ``ok`` (dollars present AND cents-vs-dollars agree) gates the S4
    check; ``status`` names the fail-closed reason otherwise. ``portfolio_value`` is journaled (0 ==
    flat) as a corroborating open-positions signal."""

    dollars: Decimal | None
    cents: int | None = None
    portfolio_value: Any = None
    status: str = "ok"

    @property
    def ok(self) -> bool:
        return self.dollars is not None and self.status == "ok"


def parse_balance(payload: Any) -> BalanceRead:
    """Parse a proxy /portfolio/balance payload, fail-closed. The REAL payload carries BOTH
    ``balance_dollars`` (str, sub-cent — the PRIMARY value) and ``balance`` (int CENTS). We require
    both to be present and to agree to within ``_BALANCE_MISMATCH_TOL``; otherwise we return a
    not-ok read (status names the reason). We NEVER ``int()`` a field that might be dollars, and we
    do not guess alternate field names — an unexpected shape fails closed (no arm)."""
    if not isinstance(payload, dict):
        return BalanceRead(None, status="malformed")
    pv = payload.get("portfolio_value")
    bd_raw = payload.get("balance_dollars")
    bc_raw = payload.get("balance")
    if bd_raw is None:
        return BalanceRead(None, portfolio_value=pv, status="missing_balance_dollars")
    if bc_raw is None:
        return BalanceRead(None, portfolio_value=pv, status="missing_balance_cents")
    try:
        dollars = Decimal(str(bd_raw))
    except (InvalidOperation, ValueError, TypeError):
        return BalanceRead(None, portfolio_value=pv, status="malformed")
    try:
        cents = int(bc_raw)
    except (TypeError, ValueError):
        return BalanceRead(None, portfolio_value=pv, status="malformed")
    if abs(dollars - Decimal(cents) / 100) >= _BALANCE_MISMATCH_TOL:
        return BalanceRead(None, cents=cents, portfolio_value=pv, status="mismatch")
    return BalanceRead(dollars=dollars, cents=cents, portfolio_value=pv, status="ok")


def balance_loss_dollars(balance_start: Decimal, balance_now: Decimal) -> Decimal:
    """The realized loss in DOLLARS since the day's baseline: start - now. Positive = a loss
    (balance fell); negative = a gain."""
    return Decimal(balance_start) - Decimal(balance_now)


def s4_balance_breached(
    balance_start: Decimal, balance_now: Decimal, cap_dollars: Decimal
) -> tuple[bool, Decimal]:
    """S4 (balance): (breached, loss_dollars). Breached iff loss >= cap. loss = start - now."""
    loss = balance_loss_dollars(balance_start, balance_now)
    return (loss >= cap_dollars, loss)


# ---------------------------------------------------------------------------
# Controller — the thin shell: pure state + Executor freeze + flatten dispatch + journal
# ---------------------------------------------------------------------------
class StopController:
    """Owns the StopState and performs the stop side effects: freeze the Executor (armed=False),
    dispatch the position policy's flatten actions via the Executor's stop-authorized path, and
    journal every alarm/stop notification. Never dispatches strategy orders."""

    def __init__(
        self,
        executor: Any,
        journal: Any,
        config: StopConfig | None = None,
        clock: Callable[[], float] = None,  # type: ignore[assignment]
        new_client_order_id: Callable[[], str] | None = None,
        latch_path: str | None = None,
        utc_day: str | None = None,
        window: str | None = None,
        flatten_sink: Callable[[Any, Any], None] | None = None,
    ) -> None:
        import time as _time

        from service.orders.envelope import new_client_order_id as _mint

        self.executor = executor
        self._journal = journal
        self.config = config or StopConfig()
        self._clock = clock or _time.time
        self._mint = new_client_order_id or _mint
        # BUG-3 repair: persist day-halting trips (S1-S4) to the day-scoped guard file so the NEXT
        # window's arming check refuses to arm for the rest of the UTC day.
        self._latch_path = latch_path
        self._utc_day = utc_day
        self._window = window
        # F1 (review): fold every stop-authorized flatten's (intent, ExecResult) back into the caller's
        # ledger so a flatten that FILLS is booked as a round-trip (not left in unsettled_legs).
        self._flatten_sink = flatten_sink
        self.state = StopState()

    def _persist_latch(self, stop: str, reason: str, window: str | None) -> None:
        if self._latch_path is None or self._utc_day is None or stop not in DAY_HALTING_STOPS:
            return
        try:
            record_latched_stop(
                self._latch_path, self._utc_day, stop, reason,
                window or self._window, self._clock(),
            )
        except Exception as e:  # noqa: BLE001 - a persist failure must never break the freeze
            logger.error("[STOPS] failed to persist %s latch to %s: %s", stop, self._latch_path, e)

    def _journal_note(self, note: Notification) -> None:
        if self._journal is None:
            return
        self._journal.append(
            "alarm" if note.kind.startswith("A") else "stop",
            {"kind": note.kind, "reason": note.reason, "detail": note.detail},
            self._clock(),
        )

    def raise_alarm(self, alarm: str, reason: str, detail: dict | None = None) -> None:
        self.state = apply_alarm(self.state, alarm, reason, detail)
        self._journal_note(self.state.alarms[-1])

    def trip(
        self,
        stop: str,
        reason: str,
        ledger_state: LedgerState | None = None,
        bids: dict[str, Decimal] | None = None,
        detail: dict | None = None,
    ) -> tuple[PositionAction, ...]:
        """Latch ``stop``, freeze the executor, journal the notification, and (if a ledger_state is
        given) compute + dispatch the position policy. ``bids`` maps ticker -> observed bid used to
        price each flatten (a flatten with no bid is emitted as a HOLD-and-alert, never a blind
        market sell). Returns the PositionActions taken."""
        self.state = apply_stop(self.state, stop, reason, detail)
        self._journal_note(self.state.notifications[-1])
        # ALWAYS freeze strategy order placement.
        if self.executor is not None:
            self.executor.set_armed(False)
        # BUG-3 repair: LATCH the day-halting trip to the day-scoped guard file (persists across the
        # process-per-window boundary so the next window refuses to arm).
        self._persist_latch(
            stop, reason, ledger_state.window if ledger_state is not None else None
        )
        if ledger_state is None:
            return ()
        actions = position_policy(ledger_state, self.config)
        for act in actions:
            if act.kind != "flatten":
                continue
            bid = (bids or {}).get(act.ticker)
            if bid is None:
                # cannot price the flatten -> do NOT blind-sell; alert and hold.
                self.raise_alarm(
                    "A_FLATTEN_NO_BID",
                    f"flatten of {act.ticker} skipped: no bid to price against (held, alerting)",
                    {"ticker": act.ticker, "count": act.count},
                )
                continue
            intent = build_flatten_intent(
                act, ledger_state.window, ledger_state.source, bid, self._mint()
            )
            # stop-authorized flatten bypasses the arm freeze (risk reduction only, reduce_only).
            result = self.executor.execute(intent, stop_authorized=True)
            # F1: fold the flatten (intent + fills) back into the ledger so a filled flatten is a
            # booked round-trip, not a phantom unsettled leg. Fail-soft: never break the freeze.
            if self._flatten_sink is not None:
                try:
                    self._flatten_sink(intent, result)
                except Exception as e:  # noqa: BLE001
                    logger.error("[STOPS] flatten_sink failed for %s: %s", act.ticker, e)
        return actions
