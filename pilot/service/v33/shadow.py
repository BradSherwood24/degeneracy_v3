"""shadow.py — the V3.3 observation ladders (L3). Neither observation ever places an order.

Two observations run over the SAME live tape the live ladder trades on:

(1) The IDEAL K-rung ladder shadow at the LIVE margins (E_min .. E_min+rungs-1 = 5..15c). This is NOT
    rebuilt here (don't build it twice, per the L3 brief). In DRY it IS the ``dry_sim`` the ``run_v33``
    driver already books (``V33Driver._simulate_ladder_fills``); in ARMED the realised-vs-ideal comparison
    is the per-rung ``lock_solved`` (the ideal price/W value) vs ``realized_lock`` (wings actually paid)
    that the ledger already records for every rung fill. ``ideal_rung_crosses`` is the ONE predicate that
    fill rule uses (a spot-bucket YES-taker print at/through our NO rung), and
    ``dry_sim_equivalence_rungs`` re-derives which live rungs a print set of the golden fixture would fill
    so a test can assert the driver's dry_sim == this ideal rule (SHADOW == DRY_SIM equivalence).

(2) SO-3 — the DEEP-END observation ladder at margins E_min+rungs .. +deep_obs_rungs-1 (= 16..25c).
    Observation ONLY, in EVERY mode: for each deep rung it records whether the tape REACHED it (a YES
    taker lifted at/through the rung's NO price while the rung was placeable, i.e. its solved n >= n_min),
    the IDEAL lock there (``lock_value(deep_price, W_at_reach)``), and the ABSORPTION — how many lots the
    tape printed at/through that deep rung. This MEASURES the deep end (whose absorption the study could
    only infer) before anyone sizes into it (PLAN_V33 sec 6 promotion path). It never emits a Fill and
    never touches the executor.

Pure: no network, no clock, no disk. Time is the injected server timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from service.v33.core import lock_value

_CENT = Decimal("0.01")
_ONE = Decimal("1")
_EPS = Decimal("0.000000001")


def ideal_rung_crosses(yes_price: Decimal, rung_price: Decimal) -> bool:
    """The ideal fill predicate (the study's rule, the dry_sim's rule): our resting bucket-NO bid at
    ``rung_price`` (n) is a YES ask at ``1 - n``; a YES-taker print at ``yes_price`` lifts it iff
    ``yes_price >= 1 - n``. This is the SINGLE source of truth for both the live-margin shadow (dry_sim)
    and the SO-3 deep ladder, so the two cannot drift."""
    return yes_price + _EPS >= (_ONE - rung_price)


def dry_sim_equivalence_rungs(ladder_prices: list[Decimal], yes_price: Decimal) -> list[Decimal]:
    """The subset of ``ladder_prices`` a single YES-taker print at ``yes_price`` would fill under the ideal
    rule — used by the SHADOW==DRY_SIM equivalence test to check the driver's dry_sim fills exactly these.
    """
    return [p for p in ladder_prices if ideal_rung_crosses(yes_price, p)]


@dataclass
class DeepRungObs:
    """One deep observation rung (margin in whole cents). ``first`` is the snapshot at FIRST reach
    (t_minus, n, W, print, size, ideal lock); ``absorption_lots`` sums every lot the tape printed at or
    through this rung's NO price; ``prints_through`` counts those prints."""

    margin_c: int
    reached: bool = False
    first: dict[str, Any] | None = None
    absorption_lots: Decimal = Decimal(0)
    prints_through: int = 0


class DeepObservationLadder:
    """SO-3: the deep-end observation ladder (16..25c by default). Fed every spot-bucket YES-taker print
    inside the quoting window; records per deep rung whether the tape reached it, the ideal lock there,
    and the absorption. Observation only — it NEVER emits a Fill or touches an order path."""

    def __init__(self, params: Any, close_epoch: int) -> None:
        self.close_epoch = int(close_epoch)
        self.e_min_c = int((Decimal(str(params.E_min)) / _CENT).to_integral_value())
        top_margin_c = self.e_min_c + int(params.rungs) - 1            # 15 (the deepest LIVE rung margin)
        depth = int(getattr(params, "deep_obs_rungs", 0))
        self.margins: list[int] = list(range(top_margin_c + 1, top_margin_c + 1 + depth))  # 16..25
        self.n_min = Decimal(str(params.n_min))
        self.obs: dict[int, DeepRungObs] = {m: DeepRungObs(margin_c=m) for m in self.margins}
        self.deepest_yes_print = Decimal(0)
        self.prints_observed = 0

    def observe(self, *, taker_side: str | None, yes_price: Any, count: Any,
                n_top: Decimal | None, W: Decimal | None, server_ts: float) -> None:
        """Fold one spot-bucket trade into the deep ladder. Only a YES-taker print with a known ``n_top``
        can lift a deep NO rung; a rung whose solved deep price is below ``n_min`` is not placeable (the
        live path could never rest there) and is skipped for that print."""
        if not self.margins or taker_side != "yes" or n_top is None:
            return
        try:
            yp = yes_price if isinstance(yes_price, Decimal) else Decimal(str(yes_price))
            cnt = count if isinstance(count, Decimal) else Decimal(str(count))
        except (TypeError, ValueError, ArithmeticError):
            return
        self.prints_observed += 1
        if yp > self.deepest_yes_print:
            self.deepest_yes_print = yp
        t_minus = self.close_epoch - server_ts
        for m in self.margins:
            deep_price = n_top - Decimal(m - self.e_min_c) * _CENT
            if deep_price < self.n_min:
                continue  # not placeable (below n_min) -> not a live-reachable counterfactual
            if not ideal_rung_crosses(yp, deep_price):
                continue
            o = self.obs[m]
            o.absorption_lots += cnt
            o.prints_through += 1
            if not o.reached:
                o.reached = True
                lock = lock_value(deep_price, W) if W is not None else None
                o.first = {
                    "t_minus_s": round(float(t_minus), 1), "n": str(deep_price),
                    "W": (str(W) if W is not None else None), "print": str(yp), "size": str(cnt),
                    "lock_solved": (str(lock) if lock is not None else None),
                }

    def summary(self) -> dict[str, Any]:
        """The per-window SO-3 record (goes on the ledger row; the report aggregates it)."""
        rungs = [
            {"margin_c": m, "reached": o.reached, "first": o.first,
             "absorption_lots": str(o.absorption_lots), "prints_through": o.prints_through}
            for m, o in ((m, self.obs[m]) for m in self.margins)
        ]
        return {
            "margins_c": list(self.margins),
            "deepest_yes_print": str(self.deepest_yes_print),
            "prints_observed": self.prints_observed,
            "reached_count": sum(1 for o in self.obs.values() if o.reached),
            "rungs": rungs,
        }
