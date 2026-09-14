"""pricing.py — the V3.2 pin-strangle pricing, sourced from reconstructed ms tops.

Every money primitive is the PINNED law, imported never retyped:
  * ``service.v32.core.solve_n``  — largest whole-cent n with n+fee(n) <= budget, capped.
  * ``service.v32.core.wing_cost``— W = yes_ask(Sd)+fee + no_ask(Su)+fee.
  * ``service.v32.core.lock_value``— lock = 2 - (n+fee(n)) - W_paid.
  * ``service._simlaw.fee``       — the audited census taker fee.

Spot selection + cap mirror the live core (``_select_spot`` / ``_bucket_cap`` / ``_compute_W``) but
WITHOUT the core's freshness gate: the replay lab reproduces the forward sim's rules (``pf_ms_requote2``
has no freshness gate — it reads the nearest book), differing from the sim only in the DATA SOURCE (ms
bucket book vs minute candle). Freshness is instead REPORTED as a metric, not used to suppress quotes.
The two-sided validity check IS reused from the core so a malformed/one-sided book is never priced.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from service.book import TopOfBook
from service.v32.core import _valid_two_sided, lock_value, solve_n, wing_cost

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)
_CENT = Decimal("0.01")

__all__ = [
    "select_spot", "compute_W", "bucket_cap", "yes_mid",
    "solve_n", "wing_cost", "lock_value", "_valid_two_sided",
]


def yes_mid(top: TopOfBook) -> Decimal | None:
    if not _valid_two_sided(top):
        return None
    return (top.yes_bid + top.yes_ask) / _TWO  # type: ignore[operator]


def select_spot(bucket_tops: Mapping[int, TopOfBook]) -> int | None:
    """Highest-YES-mid bucket among valid two-sided books; ties -> lowest floor. Mirrors
    ``core._select_spot`` (regardless of age; freshness is a reported metric here, not a gate)."""
    best_floor: int | None = None
    best_mid: Decimal | None = None
    for floor in sorted(bucket_tops):
        m = yes_mid(bucket_tops[floor])
        if m is None:
            continue
        if best_mid is None or m > best_mid:
            best_mid = m
            best_floor = floor
    return best_floor


def compute_W(strike_tops: Mapping[int, TopOfBook], Sd: int, Su: int) -> Decimal | None:
    """W from the strike books at Sd/Su, or None if either is missing/suspect/invalid. Mirrors
    ``core._compute_W`` minus the freshness gate. no_ask(Su) = 1 - yes_bid(Su) is already derived in
    the BookMirror top (``no_ask``)."""
    sd = strike_tops.get(Sd)
    su = strike_tops.get(Su)
    if sd is None or su is None or sd.suspect or su.suspect:
        return None
    ya = sd.yes_ask
    na = su.no_ask
    if ya is None or na is None:
        return None
    if not (_ZERO < ya < _ONE) or not (_ZERO < na < _ONE):
        return None
    return wing_cost(ya, na)


def bucket_cap(bucket_tops: Mapping[int, TopOfBook], Sd: int) -> Decimal | None:
    """cap = no_ask(B) - 0.01 = (1 - yes_bid(B)) - 0.01, whole cents; None if the bucket is invalid.
    Mirrors ``core._bucket_cap``."""
    top = bucket_tops.get(Sd)
    if not _valid_two_sided(top):
        return None
    return ((_ONE - top.yes_bid) - _CENT).quantize(_CENT)  # type: ignore[union-attr]
