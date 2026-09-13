"""stops.py — V3.2 arming gate, reconcile-first, and day-scoped stops/alarms.

Reuses the ``service.stops`` primitives (day guard, S4 balance decision, falsifier freeze check) but
keeps a SEPARATE day-guard file (``ops/v32_stops_YYYY-MM-DD.json``) so a V3.2 stop never latches the
box and vice versa. Every threshold is a NAMED constant here so Phase 4 can mirror each as a ``[pin]``
tag in ``ceremony/v32_falsifier.md``. Nothing here sends an order or reads a key.

The gates (all fail closed — any doubt refuses to arm and the caller degrades to dry):

  * S5 (arming): the falsifier ``ceremony/v32_falsifier.md`` carries ``STATUS: FROZEN`` (absent ->
    refuse), the params sha is verified, and the proxy ``/health`` shows ``orders_enabled`` true with
    caps that (a) allow ``params.contracts`` per order and no more than the V3.2 ceiling of 2, (b)
    cover BOTH the range series (``KXBTC-...``) and the strike series (``KXBTCD-...``) checked the way
    the proxy does — ``ticker.startswith(prefix)`` — and (c) leave at least
    ``V32_MIN_ORDER_BUDGET_AT_ARM`` creates in today's budget at :40.
  * Reconcile-first: GET positions; any un-settled KXBTC* position we would inherit -> refuse (a fresh
    window must never arm on top of an unknown open position).
  * S4 (day loss): the account-balance loss cap ``V32_S4_DAY_LOSS_CAP_DOLLARS`` via the pilot's
    pending-settlement-banded ``s4_balance_decision`` (a pending settlement never moves the number a
    latch is decided on).
  * S1_LEGGED: a completed-set left one-legged below the lock floor at T-1 s. One occurrence stands the
    hour down (core); the DAY latches after ``V32_S1_LEGGED_LATCH_THRESHOLD`` occurrences (day guard).
  * A_REPLACE / A_STALE: handled inside the pure core (it cancels the rest + stands the hour down); the
    driver journals the alarm. The named thresholds live in ``V32Params`` (``replace_rate_alarm_per_min``,
    ``freshness_max_age_s``); the alarm-kind constants are here for the ledger/report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from service.stops import (
    DayGuard,
    falsifier_is_frozen,
    read_day_guard,
    record_latched_stop,
    s4_balance_decision,
)

# ---------------------------------------------------------------------------
# Named thresholds ([pin] candidates for the Phase-4 falsifier)
# ---------------------------------------------------------------------------
V32_S4_DAY_LOSS_CAP_DOLLARS = Decimal("3.00")     # [pin] day balance-loss cap
V32_MIN_ORDER_BUDGET_AT_ARM = 200                 # [pin] creates left in today's budget required at :40
V32_MAX_CONTRACTS_PER_ORDER = 2                   # [pin] the proxy cap V3.2 requires (never exceeded)
V32_S1_LEGGED_LATCH_THRESHOLD = 2                 # [pin] one-legged sets below the floor before a DAY latch

# ticker series the proxy prefixes must cover (checked via startswith, the proxy's own test)
V32_RANGE_TICKER_PROBE = "KXBTC-"     # a range bucket ticker starts here
V32_STRIKE_TICKER_PROBE = "KXBTCD-"   # a strike ticker starts here

# alarm / stop kind strings (mirrored in the ledger + falsifier)
A_REPLACE = "A_REPLACE"
A_STALE = "A_STALE"
S1_LEGGED = "S1_LEGGED"
S4_DAY_LOSS = "S4"
S5_ARMING = "S5"

_DAY_GUARD_PREFIX_V32 = "v32_stops_"
_FROZEN_LINE = "STATUS: FROZEN"


def v32_day_guard_path(ops_dir: str, utc_day: str) -> str:
    """The V3.2 day-scoped guard file: ops/v32_stops_YYYY-MM-DD.json (SEPARATE from the box's)."""
    return os.path.join(ops_dir, f"{_DAY_GUARD_PREFIX_V32}{utc_day}.json")


# ---------------------------------------------------------------------------
# S5 caps agreement (the proxy /health caps must allow what V3.2 will send)
# ---------------------------------------------------------------------------
def v32_caps_agree(health: Any, contracts: int) -> tuple[bool, str]:
    """The /health caps + budget agree with the V3.2 order profile. Refuse on any disagreement.

    Requires: ``orders_enabled`` true; ``max_contracts_per_order`` in [contracts, 2]; the ticker
    prefixes cover BOTH ``KXBTC-...`` and ``KXBTCD-...`` (via startswith, exactly as the proxy caps);
    ``orders_remaining_today`` >= ``V32_MIN_ORDER_BUDGET_AT_ARM``."""
    if not isinstance(health, dict):
        return False, "no /health payload"
    if not health.get("orders_enabled"):
        return False, "proxy orders_enabled is not true"
    caps = health.get("caps")
    if not isinstance(caps, dict):
        return False, "no caps block in /health"
    try:
        proxy_max = int(caps.get("max_contracts_per_order"))
    except (TypeError, ValueError):
        return False, "proxy max_contracts_per_order missing/non-numeric"
    if proxy_max < int(contracts):
        return False, f"proxy max_contracts_per_order {proxy_max} < params.contracts {contracts}"
    if proxy_max > V32_MAX_CONTRACTS_PER_ORDER:
        return False, (f"proxy max_contracts_per_order {proxy_max} > V3.2 ceiling "
                       f"{V32_MAX_CONTRACTS_PER_ORDER}")
    prefixes = caps.get("ticker_prefixes")
    if not isinstance(prefixes, (list, tuple)):
        return False, "proxy ticker_prefixes missing"
    for probe in (V32_RANGE_TICKER_PROBE, V32_STRIKE_TICKER_PROBE):
        # mirror the proxy's own gate (a real ticker T is allowed iff T.startswith(prefix)); a ticker
        # of this series starts with ``probe``, so it is covered iff some configured prefix is itself a
        # prefix of ``probe``. (The prior code OR'd a clause identical to the first — dead code — which
        # only ever fail-closed; this is the exact, non-redundant condition.)
        if not any(probe.startswith(str(p)) for p in prefixes):
            return False, f"proxy prefixes do not cover a {probe!r} ticker: {list(prefixes)}"
    try:
        remaining = int(health.get("orders_remaining_today"))
    except (TypeError, ValueError):
        return False, "orders_remaining_today missing/non-numeric"
    if remaining < V32_MIN_ORDER_BUDGET_AT_ARM:
        return False, (f"orders_remaining_today {remaining} < required {V32_MIN_ORDER_BUDGET_AT_ARM}")
    return True, "ok"


@dataclass(frozen=True)
class V32ArmDecision:
    armed: bool
    reasons: tuple[str, ...] = ()


def v32_arming_check(
    falsifier_path: str,
    health: Any,
    params_verified: bool,
    contracts: int,
) -> V32ArmDecision:
    """S5. armed=True ONLY if the falsifier is FROZEN, the params sha is verified, and the /health caps
    + budget agree (``v32_caps_agree``). Any failure -> refuse with the reasons (the caller journals a
    ``degrade_to_dry`` and runs dry)."""
    reasons: list[str] = []
    if not params_verified:
        reasons.append("params sha not verified against the frozen pin")
    if not falsifier_is_frozen(falsifier_path):
        reasons.append(f"falsifier STATUS line is not exactly '{_FROZEN_LINE}' at "
                       f"{os.path.basename(falsifier_path)}")
    caps_ok, caps_reason = v32_caps_agree(health, contracts)
    if not caps_ok:
        reasons.append(caps_reason)
    return V32ArmDecision(armed=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Reconcile-first (no inherited un-settled position)
# ---------------------------------------------------------------------------
def reconcile_positions_clean(positions: Any) -> tuple[bool, str]:
    """(clean, detail). Clean iff no KXBTC* market position carries a non-zero size. A malformed /
    unreadable payload is NOT clean (fail closed). ``positions`` is the /portfolio/positions payload
    ({"market_positions": [...]}) or a bare list."""
    if positions is None:
        return False, "no positions payload (fail closed)"
    rows: Any
    if isinstance(positions, dict):
        rows = positions.get("market_positions")
        if rows is None:
            rows = positions.get("positions")
    else:
        rows = positions
    if not isinstance(rows, list):
        return False, "positions payload has no market_positions list (fail closed)"
    for p in rows:
        if not isinstance(p, dict):
            return False, "malformed position row (fail closed)"
        ticker = str(p.get("ticker") or p.get("market_ticker") or "")
        if not ticker.startswith("KXBTC"):
            continue
        pos = p.get("position")
        if pos is None:
            pos = p.get("position_fp")
        try:
            size = Decimal(str(pos)) if pos is not None else Decimal(0)
        except Exception:  # noqa: BLE001
            return False, f"un-parseable position size on {ticker} (fail closed)"
        if size != 0:
            return False, f"inherited un-settled position on {ticker}: {size}"
    return True, "ok"


# ---------------------------------------------------------------------------
# S4 day-loss decision (banded, reusing the pilot primitive)
# ---------------------------------------------------------------------------
def v32_s4_decision(balance_start: Decimal, balance_now: Decimal, pending_value: Decimal):
    """S4 with the pending-settlement band at the V3.2 cap. Returns the ``S4Decision``
    (kind clear|pending|latch)."""
    return s4_balance_decision(balance_start, balance_now, pending_value, V32_S4_DAY_LOSS_CAP_DOLLARS)


# ---------------------------------------------------------------------------
# S1_LEGGED — count one-legged-below-floor occurrences; latch the DAY at the threshold
# ---------------------------------------------------------------------------
def count_legged(guard: DayGuard) -> int:
    """How many S1_LEGGED occurrences are recorded in the day guard."""
    return sum(1 for e in guard.latched if e.get("kind") == S1_LEGGED)


def record_legged_occurrence(path: str, utc_day: str, window: str | None, reason: str,
                             ts: float) -> int:
    """Append one S1_LEGGED occurrence to the day guard; return the running count AFTER the append."""
    record_latched_stop(path, utc_day, S1_LEGGED, reason, window, ts)
    return count_legged(read_day_guard(path, utc_day))


def v32_latched_stop_kind(guard: DayGuard) -> str | None:
    """The day-halting stop latched in the V3.2 guard, else None. A corrupt guard is handled by the
    caller (fail closed). S4 latches immediately; S1_LEGGED latches only once its occurrence count
    reaches ``V32_S1_LEGGED_LATCH_THRESHOLD``."""
    for e in guard.latched:
        if e.get("kind") == S4_DAY_LOSS:
            return S4_DAY_LOSS
    if count_legged(guard) >= V32_S1_LEGGED_LATCH_THRESHOLD:
        return S1_LEGGED
    return None


# ---------------------------------------------------------------------------
# The one arm-or-degrade decision (S5 + reconcile-first + day latch + S4), testable in isolation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class V32ArmingOutcome:
    """The resolved decision for a window. ``effective_mode`` is what actually runs (``armed`` only if
    every gate passed, else ``dry``); ``degrade_reason`` + ``reasons`` explain a degrade."""

    effective_mode: str
    armed: bool
    degrade_reason: str | None
    reasons: tuple[str, ...]


def decide_v32_arming(
    *,
    resolved_mode: str,
    falsifier_path: str,
    health: Any,
    positions: Any,
    params_verified: bool,
    contracts: int,
    day_guard: DayGuard,
    s4: Any = None,
) -> V32ArmingOutcome:
    """The single arm-or-degrade gate. Only ``resolved_mode == "armed"`` is a candidate; anything else
    passes through unarmed. An armed candidate must clear ALL of: the day guard (not corrupt, no
    day-halting latch), S5 (frozen falsifier + params sha + /health caps & budget), reconcile-first (no
    inherited un-settled KXBTC* position), and S4 (the balance decision is not ``latch``). Any failure
    -> degrade to dry with the reasons; the caller journals ``degrade_to_dry`` and runs the dry loop."""
    if resolved_mode != "armed":
        return V32ArmingOutcome(effective_mode=resolved_mode, armed=False,
                                degrade_reason=None, reasons=())
    reasons: list[str] = []
    if day_guard.corrupt:
        reasons.append("day guard corrupt/unreadable (fail closed)")
    else:
        latched = v32_latched_stop_kind(day_guard)
        if latched is not None:
            reasons.append(f"day-halting stop already latched today: {latched}")
    dec = v32_arming_check(falsifier_path, health, params_verified, contracts)
    reasons.extend(dec.reasons)
    clean, detail = reconcile_positions_clean(positions)
    if not clean:
        reasons.append(f"reconcile-first: {detail}")
    if s4 is not None and getattr(s4, "kind", None) == "latch":
        reasons.append("S4 day-loss cap breached")
    if reasons:
        return V32ArmingOutcome(effective_mode="dry", armed=False,
                                degrade_reason="degrade_to_dry", reasons=tuple(reasons))
    return V32ArmingOutcome(effective_mode="armed", armed=True, degrade_reason=None, reasons=())
