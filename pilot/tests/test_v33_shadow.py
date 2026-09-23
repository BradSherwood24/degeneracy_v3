"""V3.3 observation ladders (L3): the SO-3 deep-end observation ladder (16..25c) and the SHADOW==DRY_SIM
equivalence. FAKES ONLY -- no network, no proxy, no sealed/holdout read. The deep-end integration replays
the 2026-09-20T04:00Z golden fixture through the real V33Driver (in dry) and checks the deep rungs the
sweep reached + their absorption."""

from __future__ import annotations

import json
import os
from dataclasses import replace as dr
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.run_v32 import FrozenExecutor
from service.v33 import V33State, load_v33_params
from service.v33.shadow import (
    DeepObservationLadder,
    dry_sim_equivalence_rungs,
    ideal_rung_crosses,
)
import service.run_v33 as RUN

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v33", "golden_20260920T040000Z.json")
_ONE = Decimal(1)
BK = {"KXBTC-RANGE-B80400": (80400.0, 80499.99), "KXBTC-RANGE-B80500": (80500.0, 80599.99)}
B_SD = "KXBTC-RANGE-B80400"
STK_SD = "KXBTCD-26SEP2000-T80399.99"
STK_SU = "KXBTCD-26SEP2000-T80499.99"


class J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))


def _top(bid, ask):
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=_ONE - ya, no_bid_size=Decimal(100), no_ask=_ONE - yb,
                     no_ask_size=Decimal(100), suspect=False)


def _load_fixture():
    if not os.path.exists(FIXTURE):
        pytest.skip("v33 golden fixture absent")
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _dry_driver():
    fix = _load_fixture()
    p = dr(load_v33_params(), tol=Decimal("0.01"), deb_ms=0,
           freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)
    cts = fix["close_epoch"]
    st = V33State.new(fix["close_time"], cts, BK, p, shakedown=True)
    drv = RUN.V33Driver(p, st, J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    return fix, p, cts, drv


def _bring_up(drv, cts):
    t0 = cts - 900 + 1
    for m, tb in ((B_SD, ("0.10", "0.11")), (STK_SU, ("0.10", "0.11")), (STK_SD, ("0.54", "0.55"))):
        drv.on_book_update(m, _top(*tb), t0)


# ---------------------------------------------------------------------------
# the ideal predicate + SHADOW == DRY_SIM equivalence
# ---------------------------------------------------------------------------
def test_ideal_rung_crosses_predicate():
    # our NO rung at n=0.45 is a YES ask at 0.55; a 0.55 print lifts it, a 0.54 does not.
    assert ideal_rung_crosses(Decimal("0.55"), Decimal("0.45")) is True
    assert ideal_rung_crosses(Decimal("0.54"), Decimal("0.45")) is False
    assert ideal_rung_crosses(Decimal("0.99"), Decimal("0.35")) is True


def test_shadow_equals_dry_sim_on_the_golden_prints():
    """SHADOW == DRY_SIM: for every print, the live rungs the driver's dry_sim FILLS are exactly the rungs
    the ideal predicate (``dry_sim_equivalence_rungs``) says a print at that yes_price would fill. Proven
    print-by-print against the golden tape (the ladder starts full at n_top..n_top-10c)."""
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    for dt, yp_s, _cnt in sorted(fix["prints"], key=lambda r: r[0]):
        yp = Decimal(str(yp_s))
        prices_before = [o.price for o in drv.state.ladder if o.live and o.order_id is not None]
        expected = set(dry_sim_equivalence_rungs(prices_before, yp))
        fills_before = drv._dry_sim_fills
        drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": yp_s, "count_fp": "1.00"},
                     cts + float(dt))
        # exactly the predicted rungs filled on this print
        assert drv._dry_sim_fills - fills_before == len(expected)
    assert drv._dry_sim_fills == 11   # the full sweep still fills all 11 live rungs


# ---------------------------------------------------------------------------
# SO-3 DeepObservationLadder -- direct unit
# ---------------------------------------------------------------------------
def test_deep_ladder_margins_are_16_to_25():
    lad = DeepObservationLadder(load_v33_params(), close_epoch=1789876800)
    assert lad.margins == list(range(16, 26))   # E_min_c(5) + rungs(11) = 16 .. 16+10-1 = 25


def test_deep_ladder_records_reach_lock_and_absorption():
    p = load_v33_params()
    lad = DeepObservationLadder(p, close_epoch=1000)
    n_top = Decimal("0.45")     # deep rung m=16 -> price 0.34 (YES ask 0.66); m=25 -> 0.25 (ask 0.75)
    W = Decimal("1.4737")
    # a 0.66 print reaches ONLY the shallowest deep rung (m=16, price 0.34); deeper rungs need >= 0.67..
    lad.observe(taker_side="yes", yes_price=Decimal("0.66"), count=Decimal(3), n_top=n_top, W=W,
                server_ts=500)   # t_minus 500, inside T-15..T-5
    o16 = lad.obs[16]
    assert o16.reached and o16.prints_through == 1 and o16.absorption_lots == Decimal(3)
    assert not lad.obs[17].reached
    # its ideal lock = lock_value(0.34, W)
    from service.v33.core import lock_value
    assert Decimal(o16.first["lock_solved"]) == lock_value(Decimal("0.34"), W)
    # a 0.99 print reaches ALL deep rungs and adds absorption
    lad.observe(taker_side="yes", yes_price=Decimal("0.99"), count=Decimal(5), n_top=n_top, W=W,
                server_ts=490)
    assert all(lad.obs[m].reached for m in range(16, 26))
    assert lad.obs[16].absorption_lots == Decimal(8)   # 3 + 5
    s = lad.summary()
    assert s["reached_count"] == 10 and s["margins_c"] == list(range(16, 26))


def test_deep_ladder_skips_below_n_min_and_no_side():
    p = load_v33_params()
    lad = DeepObservationLadder(p, close_epoch=1000)
    # a NO-side taker never lifts a NO rung
    lad.observe(taker_side="no", yes_price=Decimal("0.99"), count=Decimal(5),
                n_top=Decimal("0.45"), W=Decimal("1.47"), server_ts=500)
    assert lad.summary()["reached_count"] == 0
    # at a very low n_top the deep rungs fall below n_min (0.05) and are skipped
    lad.observe(taker_side="yes", yes_price=Decimal("0.999"), count=Decimal(5),
                n_top=Decimal("0.10"), W=Decimal("1.8"), server_ts=500)
    # n_top 0.10: m=16 -> 0.10-11c = -0.01 < n_min -> skipped; nothing reachable
    assert lad.summary()["reached_count"] == 0


# ---------------------------------------------------------------------------
# SO-3 through the driver on the golden fixture
# ---------------------------------------------------------------------------
def test_driver_deep_obs_reached_and_absorption_on_golden_sweep():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    for dt, yp_s, cnt in sorted(fix["prints"], key=lambda r: r[0]):
        drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": yp_s, "count_fp": cnt},
                     cts + float(dt))
    s = drv.deep_obs.summary()
    assert s["margins_c"] == list(range(16, 26))
    # the golden sweep prints to yes 0.98, so every deep rung (down to 0.25 = ask 0.75) is reached
    assert s["reached_count"] == 10
    # absorption at the shallowest deep rung is > 0 (real lots printed through it)
    r16 = next(r for r in s["rungs"] if r["margin_c"] == 16)
    assert Decimal(r16["absorption_lots"]) > 0 and r16["reached"] and r16["first"] is not None


def test_deep_ladder_reach_with_unknown_W_records_none_lock():
    p = load_v33_params()
    lad = DeepObservationLadder(p, close_epoch=1000)
    lad.observe(taker_side="yes", yes_price=Decimal("0.99"), count=Decimal(2),
                n_top=Decimal("0.45"), W=None, server_ts=500)   # W unknown -> lock_solved None
    o16 = lad.obs[16]
    assert o16.reached and o16.first["lock_solved"] is None and o16.first["W"] is None


def test_deep_ladder_disabled_when_depth_zero():
    p = dr(load_v33_params(), deep_obs_rungs=0)
    lad = DeepObservationLadder(p, close_epoch=1000)
    assert lad.margins == []
    lad.observe(taker_side="yes", yes_price=Decimal("0.99"), count=Decimal(5),
                n_top=Decimal("0.45"), W=Decimal("1.47"), server_ts=500)
    s = lad.summary()
    assert s["margins_c"] == [] and s["reached_count"] == 0


def test_dry_sim_equivalence_rungs_empty_when_no_cross():
    prices = [Decimal("0.45"), Decimal("0.40")]
    # a 0.50 print lifts neither (0.45 rung is a 0.55 ask, 0.40 rung a 0.60 ask)
    assert dry_sim_equivalence_rungs(prices, Decimal("0.50")) == []
    # a 0.55 print lifts only the 0.45 rung (ask 0.55), not the 0.40 rung (ask 0.60)
    assert dry_sim_equivalence_rungs(prices, Decimal("0.55")) == [Decimal("0.45")]


def test_driver_deep_obs_gated_to_bucket_and_window():
    fix, p, cts, drv = _dry_driver()
    _bring_up(drv, cts)
    # a print on a STRIKE ticker (not the ladder bucket) is ignored by SO-3
    drv.on_trade(STK_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "5.00"},
                 cts - 500)
    # a bucket print OUTSIDE the quoting window (t_minus 100 < quote_end 300) is ignored
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.99", "count_fp": "5.00"},
                 cts - 100)
    assert drv.deep_obs.summary()["reached_count"] == 0
