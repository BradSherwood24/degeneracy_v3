"""estimates.py — turn per-window fills into OPTIMISTIC / BASE / PESSIMISTIC estimates.

Definitions (task):
  * OPTIMISTIC  = the ideal no-lag rule (``IdealModel``), completion at the ask (no +1.5 s lag).
  * BASE        = the lagging model at E=0.10, TOL=0.02, DEB=5000, counting spread-aware maker-rule
                  fills (the model only records a fill when the maker rule fires; regime iii excluded).
  * PESSIMISTIC = BASE minus 1 tick per wing at completion (2c off the lock), dropping fills whose
                  through-print size < 1 lot, plus a replace-budget haircut (drop a window's fill when
                  its BASE replaces would exceed the per-window proxy budget).

Each estimate reports fills/day, mean/median/p10/min lock, % positive, c/day (all in CENTS), with n and
the number of windows, plus how many windows are needed for a +/-2c band on the mean (SE from the
observed sd; 3.6 fills/day assumed when n < 5).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal

from .models import BASE_CELL

# Proxy DAILY_ORDER_BUDGET target before arming (PLAN_V32 "Requote policy": raise to 4000, 2x margin).
DEFAULT_DAILY_ORDER_BUDGET = 4000
WINDOWS_PER_DAY = 24
ASSUMED_FILLS_PER_DAY = 3.6         # PLAN_V32 forward-sim fill rate, used when n < 5
PESSIMISTIC_WING_HAIRCUT_C = 2.0    # 1 tick per wing at completion
PESSIMISTIC_MIN_PRINT_LOTS = 1      # drop fills whose through-print size is below this (lots)


def _cents(lock: Decimal | None) -> float | None:
    return float(lock * 100) if lock is not None else None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[min(n - 1, max(0, int(round(q * (n - 1)))))]


@dataclass
class Estimate:
    name: str
    n_fills: int
    n_windows: int
    fills_per_day: float | None
    mean_c: float | None
    median_c: float | None
    p10_c: float | None
    min_c: float | None
    pct_positive: float | None
    c_per_day: float | None
    sd_c: float | None
    windows_for_2c_band: int | None
    windows_note: str

    def as_dict(self) -> dict:
        return {
            "name": self.name, "n_fills": self.n_fills, "n_windows": self.n_windows,
            "fills_per_day": self.fills_per_day, "mean_c": self.mean_c, "median_c": self.median_c,
            "p10_c": self.p10_c, "min_c": self.min_c, "pct_positive": self.pct_positive,
            "c_per_day": self.c_per_day, "sd_c": self.sd_c,
            "windows_for_2c_band": self.windows_for_2c_band, "windows_note": self.windows_note,
        }


def _windows_for_band(sd_c: float | None, n_fills: int, n_windows: int,
                      band_c: float = 2.0) -> tuple[int | None, str]:
    """Windows needed for a +/-``band_c`` cent half-width CI on the mean lock (95%)."""
    if sd_c is None:
        return None, "need >= 2 fills to estimate the sd"
    n_needed_fills = (1.96 * sd_c / band_c) ** 2
    if n_fills < 5 or n_windows == 0:
        fills_per_window = ASSUMED_FILLS_PER_DAY / WINDOWS_PER_DAY
        basis = f"assumed {ASSUMED_FILLS_PER_DAY}/day (n<5)"
    else:
        fills_per_window = n_fills / n_windows
        basis = f"observed {n_fills}/{n_windows} windows"
    if fills_per_window <= 0:
        return None, "no fill-rate basis"
    windows = math.ceil(n_needed_fills / fills_per_window)
    return windows, f"needs ~{n_needed_fills:.0f} fills at {basis} = {fills_per_window*24:.2f}/day"


def summarize(name: str, locks_c: list[float], n_windows: int) -> Estimate:
    n = len(locks_c)
    if n == 0:
        return Estimate(name, 0, n_windows, 0.0 if n_windows else None,
                        None, None, None, None, None, 0.0 if n_windows else None,
                        None, None, "no fills observed")
    mean_c = statistics.mean(locks_c)
    median_c = statistics.median(locks_c)
    sd_c = statistics.stdev(locks_c) if n >= 2 else None
    days = n_windows / WINDOWS_PER_DAY if n_windows else None
    fills_per_day = (n / days) if days else None
    c_per_day = (sum(locks_c) / days) if days else None
    windows, note = _windows_for_band(sd_c, n, n_windows)
    return Estimate(
        name=name, n_fills=n, n_windows=n_windows, fills_per_day=fills_per_day,
        mean_c=mean_c, median_c=median_c, p10_c=_pct(locks_c, 0.10), min_c=min(locks_c),
        pct_positive=sum(1 for x in locks_c if x > 0) / n, c_per_day=c_per_day,
        sd_c=sd_c, windows_for_2c_band=windows, windows_note=note,
    )


def build_estimates(results: list, daily_budget: int = DEFAULT_DAILY_ORDER_BUDGET) -> dict:
    """Compute the three estimates + a per-fill breakdown from a list of ``WindowResult``."""
    n_windows = len(results)
    base_E = BASE_CELL[0]

    # OPTIMISTIC: ideal fills at the base E, completion at the ask.
    opt_locks: list[float] = []
    for r in results:
        for f in r.ideal_fills:
            if f.E == base_E and f.lock is not None:
                opt_locks.append(_cents(f.lock))

    # BASE: lagging base cell, spread-aware maker-rule fills (regime iii already excluded by the model,
    # which only records a fill when the maker rule fires).
    base_locks: list[float] = []
    base_haircut_windows = 0
    per_window_budget = daily_budget / WINDOWS_PER_DAY
    for r in results:
        f = r.base_fill
        if f is not None and f.lock is not None:
            base_locks.append(_cents(f.lock))

    # PESSIMISTIC: BASE minus 2c per set; drop < 1-lot prints and fills whose offer was (re)placed
    # within LAT=200 ms of the print (not yet reliably live); budget haircut.
    pess_locks: list[float] = []
    dropped_small = 0
    dropped_fresh = 0
    for r in results:
        f = r.base_fill
        reps = r.lag_replaces.get(BASE_CELL, 0)
        if f is None or f.lock is None:
            continue
        if float(f.print_size) < PESSIMISTIC_MIN_PRINT_LOTS:
            dropped_small += 1
            continue
        if f.since_replace_ms is not None and f.since_replace_ms < 200:
            dropped_fresh += 1
            continue
        if reps > per_window_budget:
            base_haircut_windows += 1
            continue
        pess_locks.append(_cents(f.lock) - PESSIMISTIC_WING_HAIRCUT_C)

    return {
        "n_windows": n_windows,
        "optimistic": summarize("OPTIMISTIC", opt_locks, n_windows).as_dict(),
        "base": summarize("BASE", base_locks, n_windows).as_dict(),
        "pessimistic": summarize("PESSIMISTIC", pess_locks, n_windows).as_dict(),
        "pessimistic_dropped_small_prints": dropped_small,
        "pessimistic_dropped_fresh_lat": dropped_fresh,
        "pessimistic_budget_haircut_windows": base_haircut_windows,
        "per_window_replace_budget": per_window_budget,
    }
