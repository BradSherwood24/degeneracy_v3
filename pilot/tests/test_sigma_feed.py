"""Tests for sigma_feed: trailing-tape fetch + quintile assignment with the fail-closed fallback."""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from service.sigma_feed import (
    SigmaFeed,
    assign_quintile,
    fallback_pair,
)

CLOSE = "2026-08-21T22:00:00Z"
_SYN_EDGES = [0.1, 0.2, 0.3, 0.4]


def _iso(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _T() -> int:
    return int(_dt.datetime.fromisoformat(CLOSE.replace("Z", "+00:00")).timestamp())


def _trailing_15m(strikes: list[float]) -> list[dict]:
    """9 15M markets at T, T-900, ..., T-7200 with the given floor strikes (index 0 == T == anchor)."""
    T = _T()
    out = []
    for i, s in enumerate(strikes):
        ep = T - 900 * i
        out.append({
            "ticker": f"KXBTC15M-{i}",
            "floor_strike": s,
            "close_time": _iso(ep),
            "open_time": _iso(ep - 900),
            "status": "finalized" if i > 0 else "active",
            "event_ticker": "KXBTC15M-EV",
        })
    return out


def _hourly(strikes: list[float]) -> list[dict]:
    return [
        {
            "ticker": f"KXBTCD-{int(s)}",
            "floor_strike": s,
            "close_time": CLOSE,
            "open_time": _iso(_T() - 3600),
            "status": "active",
            "event_ticker": "KXBTCD-EV",
        }
        for s in strikes
    ]


# A non-flat anchor tape (varying diffs -> non-zero sigma-hat); anchor A at T == 100000.
_GOOD_STRIKES = [100000, 99970, 100020, 99980, 100050, 99990, 100030, 99960, 100010]
# Hourly ladder offset by 40 so the nearest strike (100040) is unambiguous, G = 40 >= eps.
_GOOD_HOURLY = [99840, 99940, 100040, 100140, 100240]


def test_assign_happy_full_quintile():
    out = assign_quintile(CLOSE, _trailing_15m(_GOOD_STRIKES), _hourly(_GOOD_HOURLY), _SYN_EDGES)
    assert out.ok is True
    assert out.stand_down is False
    assert out.strangle_disabled is False
    assert out.quintile in (0, 1, 2, 3, 4)
    assert out.high_ticker and out.low_ticker
    assert out.G is not None and out.sigma_hat is not None


def test_assign_missing_tape_falls_back_to_sub_only():
    """Only the current-close anchor present (no trailing) -> EXCL_SIGMA -> fallback pair resolves,
    strangle stands down, sub-$1 unaffected."""
    only_anchor = _trailing_15m(_GOOD_STRIKES)[:1]  # just the T market
    out = assign_quintile(CLOSE, only_anchor, _hourly(_GOOD_HOURLY), _SYN_EDGES)
    assert out.ok is False
    assert out.stand_down is False
    assert out.strangle_disabled is True
    assert out.quintile is None
    assert out.high_ticker and out.low_ticker
    assert out.sigma_hat is None
    assert out.G is not None and out.G >= Decimal("0.01")


def test_assign_no_anchor_stands_down_whole_window():
    """No strike-bearing 15M market -> no pair at all -> whole-window stand down."""
    out = assign_quintile(CLOSE, [], _hourly(_GOOD_HOURLY), _SYN_EDGES)
    assert out.ok is False
    assert out.stand_down is True
    assert out.high_ticker is None and out.low_ticker is None


def test_assign_no_hourly_leg_stands_down():
    out = assign_quintile(CLOSE, _trailing_15m(_GOOD_STRIKES)[:1], [], _SYN_EDGES)
    assert out.stand_down is True


def test_assign_nearest_tie_stands_down():
    """Two hourly strikes equidistant from the anchor -> EXCL_NEAREST_TIE -> no clean pair."""
    tie_hourly = _hourly([99950, 100050])  # both 50 from A=100000
    out = assign_quintile(CLOSE, _trailing_15m(_GOOD_STRIKES)[:1], tie_hourly, _SYN_EDGES)
    assert out.stand_down is True


def test_assign_degenerate_G_stands_down():
    """Anchor exactly on an hourly strike -> G < eps -> degenerate corridor -> stand down."""
    on_strike_hourly = _hourly([99800, 100000, 100200])  # 100000 == A
    out = assign_quintile(CLOSE, _trailing_15m(_GOOD_STRIKES)[:1], on_strike_hourly, _SYN_EDGES)
    assert out.stand_down is True


def test_fallback_pair_orientation():
    """high leg = higher strike. A=100000, K=100040 -> hourly is the high leg."""
    out = fallback_pair(CLOSE, _trailing_15m(_GOOD_STRIKES)[:1], _hourly(_GOOD_HOURLY), "EXCL_SIGMA")
    assert out.stand_down is False
    assert out.high_ticker == "KXBTCD-100040"  # K > A
    assert out.low_ticker == "KXBTC15M-0"       # the anchor


class _FakeProxy:
    def __init__(self, markets, fail=False):
        self._markets = markets
        self._fail = fail
        self.calls = []

    def rest_get(self, path, params=None):
        self.calls.append((path, params))
        if self._fail:
            raise RuntimeError("proxy down")
        return {"markets": self._markets, "cursor": None}


def test_sigmafeed_fetch_trailing_returns_markets():
    markets = _trailing_15m(_GOOD_STRIKES)
    feed = SigmaFeed(_FakeProxy(markets), edges=_SYN_EDGES)
    got = feed.fetch_trailing_15m(CLOSE)
    assert len(got) == len(markets)
    # NO status filter is sent (settled trailing anchors must be returned)
    _, params = feed._proxy.calls[0]
    assert "status" not in params
    assert params["min_close_ts"] == _T() - 900 * 8
    assert params["max_close_ts"] == _T()


def test_sigmafeed_fetch_failure_is_fail_closed_empty():
    feed = SigmaFeed(_FakeProxy([], fail=True), edges=_SYN_EDGES)
    assert feed.fetch_trailing_15m(CLOSE) == []


def test_sigmafeed_assign_merges_current_close_market():
    """Trailing fetch fails -> [] ; the current-close market from wake keeps the anchor available so
    sub-$1 still resolves via the fallback (strangle disabled)."""
    feed = SigmaFeed(_FakeProxy([], fail=True), edges=_SYN_EDGES)
    current = _trailing_15m(_GOOD_STRIKES)[:1]
    out = feed.assign(CLOSE, _hourly(_GOOD_HOURLY), current_15m_markets=current)
    assert out.ok is False
    assert out.stand_down is False
    assert out.strangle_disabled is True
    assert out.high_ticker and out.low_ticker


def test_sigmafeed_assign_happy_via_shell():
    feed = SigmaFeed(_FakeProxy(_trailing_15m(_GOOD_STRIKES)), edges=_SYN_EDGES)
    out = feed.assign(CLOSE, _hourly(_GOOD_HOURLY), current_15m_markets=[])
    assert out.ok is True
    assert out.quintile is not None
