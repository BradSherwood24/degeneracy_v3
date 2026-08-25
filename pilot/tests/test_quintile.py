"""quintile.py reproduction test: given the SAME market inputs the census pipeline used, the
pilot's wake-time (G, sigma_hat, g_over_sigma, quintile) must match census_train.csv EXACTLY for
>= 20 windows spanning all five quintiles.

Uses TRAIN-day market files only (never a sealed day). Markets are loaded from 2026-06-11 so the
sigma-hat anchor tape (T-7200..T) is present for every tested window (2026-06-13 onward).
"""

from __future__ import annotations

import bisect
import csv
import os
import sys

import pytest

_SIM = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sim")
if _SIM not in sys.path:
    sys.path.append(_SIM)

import loader  # noqa: E402  (frozen sim loader; TRAIN days only)

from service import quintile  # noqa: E402

_CENSUS = os.path.join(_SIM, "out", "census_train.csv")
_LOAD_DATES = [f"2026-06-{d:02d}" for d in range(11, 22)]  # 06-11..06-21 (trailing tape included)
_TEST_DATES = {f"2026-06-{d:02d}" for d in range(13, 21)}  # windows tested: 06-13..06-20


@pytest.fixture(scope="module")
def market_inputs():
    m15, _ = loader.load_markets("15-minute", _LOAD_DATES)
    m1h, _ = loader.load_markets("1-hour", _LOAD_DATES)
    return m15, m1h


@pytest.fixture(scope="module")
def edges():
    return quintile.edges_from_census()


def _picked_rows(edges):
    rows = [
        r
        for r in csv.DictReader(open(_CENSUS, encoding="utf-8"))
        if r["status"] == "OK" and r["date"] in _TEST_DATES
    ]
    by_q: dict[int, list[dict]] = {}
    for r in rows:
        q = bisect.bisect_right(edges, float(r["g_over_sigma"]))
        by_q.setdefault(q, []).append(r)
    picks: list[dict] = []
    for q in range(5):
        picks.extend(by_q.get(q, [])[:5])
    return picks, by_q


def test_edges_match_gate_json(edges):
    # gate.json quintile_edges_gos, verified numerically.
    assert edges == [0.1014128, 0.20857860000000003, 0.3440502, 0.5661136]


def test_quintile_reproduction_exact(market_inputs, edges):
    m15, m1h = market_inputs
    picks, by_q = _picked_rows(edges)
    assert len(picks) >= 20, f"only {len(picks)} windows picked"
    assert set(by_q.keys()) == {0, 1, 2, 3, 4}, f"quintiles covered: {sorted(by_q)}"

    for r in picks:
        res = quintile.compute_window_stats(r["close_time"], m15, m1h, edges)
        assert not isinstance(res, quintile.NoQuintile), (
            f"{r['close_time']} unexpectedly excluded: "
            f"{getattr(res, 'reason', None)}"
        )
        census_q = bisect.bisect_right(edges, float(r["g_over_sigma"]))
        assert res.quintile == census_q, f"{r['close_time']} quintile {res.quintile} != {census_q}"
        # Decimal-exact G (2dp), sigma-hat (6dp), g_over_sigma (6dp) vs the census CSV strings.
        assert f"{float(res.G):.2f}" == r["G"], f"{r['close_time']} G {res.G} != {r['G']}"
        assert f"{res.sigma_hat:.6f}" == r["sigma_hat"], f"{r['close_time']} sigma"
        assert f"{res.g_over_sigma:.6f}" == f"{float(r['g_over_sigma']):.6f}", f"{r['close_time']} gos"


def test_head_of_corpus_insufficient_tape_is_noquintile(market_inputs, edges):
    # A window whose T-7200 anchor precedes the loaded tape -> clean NoQuintile (fail closed).
    m15, m1h = market_inputs
    early = [
        r
        for r in csv.DictReader(open(_CENSUS, encoding="utf-8"))
        if r["close_time"] == "2026-06-11T02:00:00Z"
    ]
    res = quintile.compute_window_stats("2026-06-11T02:00:00Z", m15, m1h, edges)
    # 06-11 02:00 needs anchors back to 06-11 00:00 which are not in the corpus head.
    assert isinstance(res, quintile.NoQuintile)


# ---------------------------------------------------------------------------
# Census-faithful pairing on the LIVE strike-at-open example (2026-08-21).
# The anchor A(T) is BTC spot at window open (a NON-round number, e.g. 77315.17); the paired hourly
# threshold K is the NEAREST hourly floor strike to A among the co-settling ladder — candidates BOTH
# BELOW and ABOVE A, ties excluded — reproducing sim/census.py's nearest-1H-strike selection exactly.
# ---------------------------------------------------------------------------
_LIVE_CLOSE = "2026-08-21T16:00:00Z"


def _hourly_markets(strikes):
    return [
        {"ticker": f"KXBTCD-{s}", "floor_strike": s, "close_time": _LIVE_CLOSE,
         "open_time": "2026-08-21T15:00:00Z", "status": "active", "event_ticker": "KXBTCD-EV"}
        for s in strikes
    ]


def test_live_pairing_nearest_below_on_100_step_ladder():
    # Observed live: anchor 77315.17 pairs to the hourly threshold 77299.99 on a $100-step ladder.
    A = 77315.17
    ladder = _hourly_markets([77099.99, 77199.99, 77299.99, 77399.99, 77499.99])
    near = quintile.nearest_hourly_strike(A, ladder, _LIVE_CLOSE)
    K, ticker = near
    assert K == 77299.99 and ticker == "KXBTCD-77299.99"  # nearest is BELOW the anchor (dist 15.18)


def test_live_pairing_nearest_above_case():
    # A ladder whose nearest threshold to the SAME anchor is ABOVE it (census picks it too).
    A = 77315.17
    ladder = _hourly_markets([77250.0, 77350.0, 77450.0])
    K, ticker = quintile.nearest_hourly_strike(A, ladder, _LIVE_CLOSE)
    assert K == 77350.0 and ticker == "KXBTCD-77350.0"  # nearest is ABOVE (dist 34.83 < 65.17)


def test_live_pairing_equidistant_is_tie_excluded():
    # Two thresholds equidistant from A -> census EXCL_NEAREST_TIE (the pilot returns "TIE").
    A = 77300.0
    ladder = _hourly_markets([77250.0, 77350.0])  # both 50 from A
    assert quintile.nearest_hourly_strike(A, ladder, _LIVE_CLOSE) == "TIE"
