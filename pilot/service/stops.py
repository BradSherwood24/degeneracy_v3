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

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from service.ledger import (
    PURPOSE_FLATTEN,
    SUB_DOLLAR_FLIP,
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
    daily_loss_cap_dollars: Decimal = Decimal("5.00")
    guard_trips_standdown: int = 5
    # PENDING-BRAD (F8) — see module docstring.
    hold_complete_floor_pairs_to_settlement: bool = True
    flatten_unprotected_exposure: bool = True


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
    ) -> None:
        import time as _time

        from service.orders.envelope import new_client_order_id as _mint

        self.executor = executor
        self._journal = journal
        self.config = config or StopConfig()
        self._clock = clock or _time.time
        self._mint = new_client_order_id or _mint
        self.state = StopState()

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
            self.executor.execute(intent, stop_authorized=True)
        return actions
