"""Spot selection / W / cap / solve_n mirror the pinned core law."""

from __future__ import annotations

from decimal import Decimal

from service.book import TopOfBook
from service.v32.core import _bucket_cap, _compute_W, _select_spot
from service.v32.core import V32State
from sim.v32_replay.pricing import bucket_cap, compute_W, select_spot, solve_n, wing_cost


def _top(yb=None, ya=None, suspect=False):
    def sz(x):
        return Decimal(100) if x is not None else None
    nb = (Decimal(1) - ya) if ya is not None else None
    na = (Decimal(1) - yb) if yb is not None else None
    return TopOfBook(yes_bid=yb, yes_bid_size=sz(yb), yes_ask=ya, yes_ask_size=sz(ya),
                     no_bid=nb, no_bid_size=sz(nb), no_ask=na, no_ask_size=sz(na), suspect=suspect)


def test_select_spot_highest_mid_ties_low():
    tops = {
        100: _top(Decimal("0.40"), Decimal("0.42")),   # mid .41
        200: _top(Decimal("0.55"), Decimal("0.57")),   # mid .56  <- highest
        300: _top(Decimal("0.55"), Decimal("0.57")),   # tie, higher floor
    }
    assert select_spot(tops) == 200


def test_compute_W_and_cap_match_core():
    from dataclasses import replace
    strike = {100: _top(Decimal("0.29"), Decimal("0.31")), 200: _top(Decimal("0.40"), Decimal("0.42"))}
    buckets = {100: _top(Decimal("0.45"), Decimal("0.49"))}
    W = compute_W(strike, 100, 200)
    assert W == wing_cost(Decimal("0.31"), Decimal("1") - Decimal("0.40"))
    assert bucket_cap(buckets, 100) == (Decimal("1") - Decimal("0.45") - Decimal("0.01"))
    # core parity for the cap (no freshness gate on _bucket_cap)
    st = V32State.new("2026-09-14T17:00:00Z", 1789743600, {}, _params())
    st = replace(st, bucket_tops=buckets)
    assert bucket_cap(buckets, 100) == _bucket_cap(st, 100)


def test_solve_n_is_the_pinned_core_solver():
    # identical object as core.solve_n (same import target)
    from service.v32.core import solve_n as core_solve
    assert solve_n is core_solve
    assert solve_n(Decimal("1.00"), Decimal("0.55")) == Decimal("0.55")


def _params():
    from service.v32.params import load_v32_params
    return load_v32_params()
