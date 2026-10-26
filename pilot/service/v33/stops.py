"""stops.py — V3.3 arming gate, reconcile-first, and day-scoped stops/alarms (the ladder roster).

A FORK of ``service.v32.stops`` for the ``DegeneracyV3_3`` roster: a SEPARATE day-guard file
(``ops/v33_stops_YYYY-MM-DD.json``) so a V3.3 stop never latches the V3.2 guard (the two rosters run
against one account through the whole V3.3 build), the SAME banded S4 balance decision and S1_LEGGED
occurrence mechanics (reused from ``service.stops``), and the SAME reconcile-first (no inherited
un-settled KXBTC* position — ``reconcile_positions_clean`` is generic and reused from V3.2). Nothing
here sends an order or reads a key.

The gates carry the ladder's numbers (PLAN_V33 Q5/Q6):
  * S4 day-loss cap ``$5.00`` (TF#4 2026-10-17: at the 3-lot ladder a worst-case one-legged K-rung sweep is
    ~$5.79 = 3 x the 1-lot ~$1.93, so the cap no longer bounds a full bad sweep BY DESIGN -- $5.00 is
    $1.67/1-lot-equivalent, TIGHTER than #3's $3.00, and a 10-10-style window latches it and KILLs the
    campaign; Brad: "A 10-10 event should kill a test fire"). Banded by the pending-settlement credit as V3.2.
  * S1_LEGGED per contract; the DAY latches after ``V33_S1_LEGGED_LATCH_THRESHOLD`` occurrences.
  * The replace-rate alarm is per LADDER inside the pure core (``replace_rate_alarm_per_min`` = 120);
    the kind constant is here for the ledger/report.
  * S5 arming: the V3.3 falsifier carries ``STATUS: FROZEN`` (L3 draft), the params sha is verified, and
    the proxy /health caps allow the 3-lot ladder (cap in [max(rung_lots)=3, sum(rung_lots)=33]) with
    enough of today's raised budget left at :40.
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
# reconcile-first is series-generic (any non-zero KXBTC* position) -> reuse the V3.2 implementation.
from service.v32.stops import reconcile_positions_clean  # noqa: F401  (re-exported for run_v33/tests)

# ---------------------------------------------------------------------------
# Named thresholds ([pin] candidates for the L3 falsifier)
# ---------------------------------------------------------------------------
V33_S4_DAY_LOSS_CAP_DOLLARS = Decimal("5.00")     # [pin] day balance-loss cap (TF#4 2026-10-17: 3.00 -> 5.00
                                                  # for the 3-lot ladder -- $1.67 per 1-lot-equivalent,
                                                  # TIGHTER than #3's $3.00; a 10-10-style window
                                                  # (-$3.84 at 1 lot = ~-$11.50 at 3) latches S4 and KILLs)
V33_MIN_ORDER_BUDGET_AT_ARM = 500                 # [pin] creates left in today's budget required at :40
V33_MAX_CONTRACTS_PER_ORDER = 2                   # the proxy's per-order cap recorded at the last freeze (1 lot).
# NOTE: the S5 ceiling is K*lots_per_rung (a coalesced wing take is CHUNKED to <= cap, so any cap in
# [max(rung_lots), sum(rung_lots)] arms; ``v33_caps_agree`` uses the resolved weights, not this constant).
# At TF#4's 3-lot ladder the band is [3, 33]: the Render proxy cap (2 at the last freeze) MUST be raised to
# >= 3 before the 3-lot params arm, else S5 degrades to dry. This value just records the setting for the runbook.
V33_S1_LEGGED_LATCH_THRESHOLD = 6                 # [pin] one-legged contracts below floor before a DAY latch
                                                  # (TF#4 2026-10-17: 2 -> 6; 6 contracts = two 3-lot rung
                                                  # events. S1 is still counted PER CONTRACT -- no logic change)

V33_RANGE_TICKER_PROBE = "KXBTC-"     # a range bucket ticker starts here
V33_STRIKE_TICKER_PROBE = "KXBTCD-"   # a strike ticker starts here

# alarm / stop kind strings (mirrored in the ledger + report)
A_REPLACE = "A_REPLACE"
A_STALE = "A_STALE"
S1_LEGGED = "S1_LEGGED"
S4_DAY_LOSS = "S4"
S5_ARMING = "S5"

_DAY_GUARD_PREFIX_V33 = "v33_stops_"
# The ETH V3.3 roster's own day-guard prefix -> ops/v33_eth_stops_YYYY-MM-DD.json. SEPARATE from the BTC
# guard so an ETH S1_LEGGED latch never halts the BTC day and vice versa; the ACCOUNT-WIDE S4 is instead
# shared by a cross-read (``guard_has_s4_latch`` on the sibling guard), per Brad's one-account-S4 ruling.
_DAY_GUARD_PREFIX_V33_ETH = "v33_eth_stops_"
_FROZEN_LINE = "STATUS: FROZEN"


def v33_day_guard_path(ops_dir: str, utc_day: str, prefix: str = _DAY_GUARD_PREFIX_V33) -> str:
    """The V3.3 day-scoped guard file: ops/<prefix>YYYY-MM-DD.json. DEFAULT ``v33_stops_`` is the BTC guard
    (SEPARATE from V3.2's and the box's). The ETH roster passes ``prefix=_DAY_GUARD_PREFIX_V33_ETH``."""
    return os.path.join(ops_dir, f"{prefix}{utc_day}.json")


def v33_eth_day_guard_path(ops_dir: str, utc_day: str) -> str:
    """The ETH V3.3 roster's day-scoped guard file: ops/v33_eth_stops_YYYY-MM-DD.json."""
    return v33_day_guard_path(ops_dir, utc_day, _DAY_GUARD_PREFIX_V33_ETH)


def guard_has_s4_latch(guard: DayGuard) -> bool:
    """True iff ``guard`` carries an S4 day-loss latch. Used for the ACCOUNT-WIDE S4: each roster reads the
    SIBLING roster's guard for an S4 latch and, if present, refuses to arm (an S4 on EITHER roster stops
    BOTH — Brad's one-account-S4 ruling). A corrupt/missing sibling guard has NO latch here, so the caller
    reads a fresh ``DayGuard`` (absent file -> empty, not latched) and the cross-stop is fail-safe: an
    unreadable sibling guard never spuriously halts this roster (its OWN ``day_guard.corrupt`` check still
    fails this roster closed on its OWN unreadable guard)."""
    return any(e.get("kind") == S4_DAY_LOSS for e in guard.latched)


# ---------------------------------------------------------------------------
# S5 caps agreement (the proxy /health caps must allow what V3.3 will send)
# ---------------------------------------------------------------------------
def v33_caps_agree(
    health: Any, lots_per_rung: int, k_rungs: int = 1, *, rung_lots: Any = None
) -> tuple[bool, str]:
    """The /health caps + budget agree with the V3.3 order profile. Requires: ``orders_enabled`` true;
    ``max_contracts_per_order`` in [min_cap, ceiling] (MUST-FIX-1: a rung needs cap >= its own lots; a
    COALESCED wing take is chunked to <= cap, so any cap in range works — Brad may set the proxy cap to 2
    (wings go as chunks) or to the full ceiling (wings go as 2 orders), and BOTH must arm); the ticker
    prefixes cover BOTH ``KXBTC-...`` and ``KXBTCD-...`` (via startswith, exactly as the proxy);
    ``orders_remaining_today`` >= ``V33_MIN_ORDER_BUDGET_AT_ARM``.

    L5 (2026-09-29): when the RESOLVED per-rung weight vector ``rung_lots`` is supplied, the per-order cap
    must cover the LARGEST single rung ``max(rung_lots)`` (a rung of weight w is PLACED as ONE order of w
    lots — the venue rejects the place if the cap is below it) and stay within the largest coalesced wing
    take ``sum(rung_lots)`` (the allotment, chunked to <= cap). Absent (uniform) -> the identical
    ``[lots_per_rung, K*lots_per_rung]`` bounds — this is exactly the pre-L5 check."""
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
    # L5: derive the [min_cap, ceiling] band from the per-rung weights when supplied (max/sum), else from
    # the uniform lots_per_rung (identical numbers at weight 1). ``min_cap`` is the largest single-rung
    # PLACE size; ``ceiling`` is the largest coalesced wing take.
    if rung_lots is not None and len(rung_lots) > 0:
        weights = [int(x) for x in rung_lots]
        min_cap = max(weights)
        ceiling = sum(weights)
        low_label, high_label = f"max(rung_lots)={min_cap}", f"sum(rung_lots)={ceiling}"
    else:
        min_cap = int(lots_per_rung)
        ceiling = int(k_rungs) * int(lots_per_rung)
        low_label, high_label = f"lots_per_rung {min_cap}", f"K*lots_per_rung={ceiling}"
    if proxy_max < min_cap:
        return False, f"proxy max_contracts_per_order {proxy_max} < {low_label}"
    if proxy_max > ceiling:
        return False, (f"proxy max_contracts_per_order {proxy_max} > V3.3 ceiling "
                       f"{high_label}")
    prefixes = caps.get("ticker_prefixes")
    if not isinstance(prefixes, (list, tuple)):
        return False, "proxy ticker_prefixes missing"
    for probe in (V33_RANGE_TICKER_PROBE, V33_STRIKE_TICKER_PROBE):
        if not any(probe.startswith(str(p)) for p in prefixes):
            return False, f"proxy prefixes do not cover a {probe!r} ticker: {list(prefixes)}"
    try:
        remaining = int(health.get("orders_remaining_today"))
    except (TypeError, ValueError):
        return False, "orders_remaining_today missing/non-numeric"
    if remaining < V33_MIN_ORDER_BUDGET_AT_ARM:
        return False, (f"orders_remaining_today {remaining} < required {V33_MIN_ORDER_BUDGET_AT_ARM}")
    return True, "ok"


@dataclass(frozen=True)
class V33ArmDecision:
    armed: bool
    reasons: tuple[str, ...] = ()


def v33_arming_check(
    falsifier_path: str, health: Any, params_verified: bool, lots_per_rung: int, k_rungs: int = 1,
    *, rung_lots: Any = None
) -> V33ArmDecision:
    """S5. armed=True ONLY if the falsifier is FROZEN, the params sha is verified, and the /health caps
    + budget agree (``v33_caps_agree``). Any failure -> refuse with the reasons. L5: ``rung_lots`` (the
    resolved per-rung weight vector) tightens the cap band to [max(rung_lots), sum(rung_lots)]."""
    reasons: list[str] = []
    if not params_verified:
        reasons.append("params sha not verified against the frozen pin")
    if not falsifier_is_frozen(falsifier_path):
        reasons.append(f"falsifier STATUS line is not exactly '{_FROZEN_LINE}' at "
                       f"{os.path.basename(falsifier_path)}")
    caps_ok, caps_reason = v33_caps_agree(health, lots_per_rung, k_rungs, rung_lots=rung_lots)
    if not caps_ok:
        reasons.append(caps_reason)
    return V33ArmDecision(armed=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# S4 day-loss decision (banded, reusing the pilot primitive) — same law as V3.2
# ---------------------------------------------------------------------------
def v33_s4_decision(
    balance_start: Decimal, balance_now: Decimal, pending_credit: tuple[Decimal, Decimal]
):
    """S4 with the pending-settlement band at the V3.3 cap. The guaranteed floor is netted into the
    balance for BOTH bounds; only the upside separates them (identical banding to ``v32_s4_decision``).
    ``pending_credit`` = the (pessimistic, optimistic) credit band from ``v33_pending_credit``."""
    pess_credit, opt_credit = pending_credit
    upside = opt_credit - pess_credit
    return s4_balance_decision(
        balance_start, balance_now + pess_credit, upside, V33_S4_DAY_LOSS_CAP_DOLLARS
    )


# ---------------------------------------------------------------------------
# S1_LEGGED — count one-legged-below-floor occurrences; latch the DAY at the threshold
# ---------------------------------------------------------------------------
def count_legged(guard: DayGuard) -> int:
    return sum(1 for e in guard.latched if e.get("kind") == S1_LEGGED)


def record_legged_occurrence(path: str, utc_day: str, window: str | None, reason: str,
                             ts: float) -> int:
    record_latched_stop(path, utc_day, S1_LEGGED, reason, window, ts)
    return count_legged(read_day_guard(path, utc_day))


def v33_latched_stop_kind(guard: DayGuard) -> str | None:
    """The day-halting stop latched in the V3.3 guard, else None. S4 latches immediately; S1_LEGGED
    latches only once its occurrence count reaches ``V33_S1_LEGGED_LATCH_THRESHOLD``."""
    for e in guard.latched:
        if e.get("kind") == S4_DAY_LOSS:
            return S4_DAY_LOSS
    if count_legged(guard) >= V33_S1_LEGGED_LATCH_THRESHOLD:
        return S1_LEGGED
    return None


# ---------------------------------------------------------------------------
# The one arm-or-degrade decision (S5 + reconcile-first + day latch + S4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class V33ArmingOutcome:
    effective_mode: str
    armed: bool
    degrade_reason: str | None
    reasons: tuple[str, ...]


def decide_v33_arming(
    *,
    resolved_mode: str,
    falsifier_path: str,
    health: Any,
    positions: Any,
    params_verified: bool,
    lots_per_rung: int,
    day_guard: DayGuard,
    s4: Any = None,
    k_rungs: int = 1,
    rung_lots: Any = None,
    position_prefix: str = "KXBTC",
    cross_s4_latched: bool = False,
) -> V33ArmingOutcome:
    """The single arm-or-degrade gate (mirrors ``decide_v32_arming``). Only ``resolved_mode == "armed"``
    is a candidate; anything else passes through unarmed. An armed candidate must clear ALL of: the day
    guard (not corrupt, no day-halting latch), S5 (frozen falsifier + params sha + /health caps &
    budget), reconcile-first (no inherited un-settled ``<position_prefix>*`` position), and S4 (not
    ``latch``). Any failure -> degrade to dry with the reasons (the caller journals ``degrade_to_dry``).

    ``position_prefix`` scopes reconcile-first to this roster's series (BTC default ``KXBTC``; ETH
    ``KXETH``). ``cross_s4_latched`` is the ACCOUNT-WIDE S4 cross-stop: True when the SIBLING roster's day
    guard carries an S4 latch, which halts THIS roster too (an S4 on either roster stops both). BTC passes
    False when the ETH guard is absent/has no S4 latch, so the BTC path is byte-identical until ETH ever
    latches S4."""
    if resolved_mode != "armed":
        return V33ArmingOutcome(effective_mode=resolved_mode, armed=False,
                                degrade_reason=None, reasons=())
    reasons: list[str] = []
    if day_guard.corrupt:
        reasons.append("day guard corrupt/unreadable (fail closed)")
    else:
        latched = v33_latched_stop_kind(day_guard)
        if latched is not None:
            reasons.append(f"day-halting stop already latched today: {latched}")
    if cross_s4_latched:
        reasons.append("account-wide S4 day-loss latched on the sibling roster")
    dec = v33_arming_check(falsifier_path, health, params_verified, lots_per_rung, k_rungs,
                           rung_lots=rung_lots)
    reasons.extend(dec.reasons)
    clean, detail = reconcile_positions_clean(positions, prefix=position_prefix)
    if not clean:
        reasons.append(f"reconcile-first: {detail}")
    if s4 is not None:
        s4_kind = getattr(s4, "kind", None)
        if s4_kind == "latch":
            reasons.append("S4 day-loss cap breached")
        elif s4_kind == "pending":
            reasons.append("S4 day-loss pending settlement (stand down, no latch)")
    if reasons:
        return V33ArmingOutcome(effective_mode="dry", armed=False,
                                degrade_reason="degrade_to_dry", reasons=tuple(reasons))
    return V33ArmingOutcome(effective_mode="armed", armed=True, degrade_reason=None, reasons=())
