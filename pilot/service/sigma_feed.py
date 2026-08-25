"""sigma_feed.py — the trailing-15M-markets fetch for sigma-hat (Phase-2 confessed gap), wired to
the quintile assignment at WAKE.

Phase 2 (build report CONFESSION #1) established: census's A3.2 sigma-hat anchor tape is the
sequence of 15M-market FLOOR STRIKES at the 9 contiguous epochs T, T-900, ..., T-7200 — NOT
candles. ``quintile.compute_window_stats`` reproduces the sim's (G, sigma_hat, quintile, leg
pairing) from a ``markets_15m`` list that MUST include those trailing windows. WakeContext fetches
only the co-settling close, so THIS module supplies the trailing anchor tape via REST ``/markets``
through the proxy, and hands it to the quintile computation.

FAIL-CLOSED CONTRACT (the task's rule):
  * ``NoQuintile`` (any census exclusion) -> the STRANGLE stands down (it needs a reproduced
    quintile + fair value). The sub-$1 flip is arithmetic-floor and does NOT depend on sigma-hat or
    the quintile, so it is left UNAFFECTED wherever a clean high/low PAIR can still be resolved.
  * When even the pair cannot be resolved (no strike-bearing anchor, no hourly leg, an equidistant
    nearest-strike tie, or a degenerate G < $0.01) there is nothing to trade -> the WHOLE window
    stands down cleanly (the harness exits 0).

TRAILING-TAPE QUERY (live-verification item, confessed): the 8 trailing 15M markets have ALREADY
SETTLED by wake, so — unlike ``WakeContext._fetch_series_markets``, which filters ``status=open`` —
this fetch does NOT constrain status (a settled market must still be returned by close-ts). If the
live ``/markets`` endpoint will not return settled markets by close-ts, sigma-hat cannot be built
live and every window degrades to sub-$1-only via the fallback pair; that is the safe direction and
is surfaced by the first passive/dry runs.

The current-close 15M market(s) already fetched by WakeContext are merged in for anchor resolution,
so the anchor A at T is available even if the trailing fetch returns nothing (sub-$1 still runs).

Pure logic (``assign_quintile``, ``fallback_pair``) is separated from the REST shell (``SigmaFeed``)
so the whole assignment is testable without a network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from service._simlaw import ANCHOR_STEP, SIGMA_ANCHORS, close_epoch, hole_G
from service.quintile import (
    NoQuintile,
    QuintileResult,
    _anchor_at,
    compute_window_stats,
    edges_from_census,
    nearest_hourly_strike,
)

logger = logging.getLogger(__name__)

FIFTEEN_SERIES = "KXBTC15M"
MARKETS_PATH = "/markets"
_MAX_PAGES = 50
_G_EPS = Decimal("0.01")


@dataclass(frozen=True)
class QuintileOutcome:
    """The wake-time quintile assignment for one window.

    ``ok`` True  -> a full census-faithful ``QuintileResult`` (quintile + fair value reproduced;
                    the strangle MAY run subject to the ladder gate).
    ``ok`` False, ``stand_down`` False -> NoQuintile, but a clean sub-$1 PAIR was resolved by the
                    fallback: ``strangle_disabled`` is forced True; ``quintile`` is None (the harness
                    routes sub-$1 via a sub-only bucket).
    ``ok`` False, ``stand_down`` True  -> no tradeable pair at all: the whole window stands down.
    """

    close_time: str
    ok: bool
    stand_down: bool
    quintile: int | None
    high_ticker: str | None
    low_ticker: str | None
    G: Decimal | None
    sigma_hat: float | None
    strangle_disabled: bool
    reason: str
    result: QuintileResult | None = None


def fallback_pair(
    close_time_iso: str, markets_15m: list[dict], hourly_markets: list[dict], no_quintile_reason: str
) -> QuintileOutcome:
    """Resolve the sub-$1 high/low pair when sigma-hat/quintile could not be reproduced.

    Mirrors ``quintile.compute_window_stats``'s pairing (anchor A = strike-bearing 15M market at T;
    K = nearest hourly floor strike; high leg = higher strike) WITHOUT sigma-hat. Returns a
    stand-down outcome when no clean, non-degenerate pair exists (fail closed)."""
    A, a_ticker = _anchor_at(markets_15m, close_time_iso)
    if A is None or not a_ticker:
        return QuintileOutcome(
            close_time_iso, ok=False, stand_down=True, quintile=None,
            high_ticker=None, low_ticker=None, G=None, sigma_hat=None,
            strangle_disabled=True,
            reason=f"{no_quintile_reason}; and no strike-bearing 15M anchor -> stand down",
        )
    near = nearest_hourly_strike(A, hourly_markets, close_time_iso)
    if near is None or near == "TIE":
        why = "no hourly leg" if near is None else "equidistant nearest-strike tie"
        return QuintileOutcome(
            close_time_iso, ok=False, stand_down=True, quintile=None,
            high_ticker=None, low_ticker=None, G=None, sigma_hat=None,
            strangle_disabled=True,
            reason=f"{no_quintile_reason}; and {why} -> no clean pair -> stand down",
        )
    K, k_ticker = near  # type: ignore[misc]
    G = hole_G(A, K)
    if G < _G_EPS:
        return QuintileOutcome(
            close_time_iso, ok=False, stand_down=True, quintile=None,
            high_ticker=None, low_ticker=None, G=None, sigma_hat=None,
            strangle_disabled=True,
            reason=f"{no_quintile_reason}; and degenerate corridor G={G} < {_G_EPS} -> stand down",
        )
    if A > K:
        high_ticker, low_ticker = a_ticker, k_ticker
    elif K > A:
        high_ticker, low_ticker = k_ticker, a_ticker
    else:  # A == K cannot reach here (G >= eps guards it); keep deterministic anyway
        high_ticker, low_ticker = a_ticker, k_ticker
    return QuintileOutcome(
        close_time_iso, ok=False, stand_down=False, quintile=None,
        high_ticker=high_ticker, low_ticker=low_ticker, G=G, sigma_hat=None,
        strangle_disabled=True,
        reason=(
            f"{no_quintile_reason}; sub-$1 pair resolved via fallback (strangle stands down, "
            f"sub-$1 unaffected)"
        ),
    )


def assign_quintile(
    close_time_iso: str,
    markets_15m: list[dict],
    hourly_markets: list[dict],
    edges: list[float],
) -> QuintileOutcome:
    """Reproduce the sim's quintile from the trailing 15M tape + hourly ladder, or fall back to the
    sub-$1 pair. Pure given the market lists."""
    res = compute_window_stats(close_time_iso, markets_15m, hourly_markets, edges)
    if isinstance(res, QuintileResult):
        return QuintileOutcome(
            close_time_iso, ok=True, stand_down=False, quintile=res.quintile,
            high_ticker=res.high_ticker, low_ticker=res.low_ticker, G=res.G,
            sigma_hat=res.sigma_hat, strangle_disabled=False, reason="ok", result=res,
        )
    assert isinstance(res, NoQuintile)
    return fallback_pair(close_time_iso, markets_15m, hourly_markets, res.reason)


class SigmaFeed:
    """I/O shell: fetch the trailing 15M anchor tape via the proxy and run the pure assignment.

    ``edges`` (the 4 G/sigma quintile boundaries) default to the frozen census edges, loaded once
    and cached. ``rest_get`` is the proxy's authenticated GET (injected in tests).
    """

    def __init__(self, proxy: Any, edges: list[float] | None = None) -> None:
        self._proxy = proxy
        self._edges = list(edges) if edges is not None else None

    def edges(self) -> list[float]:
        if self._edges is None:
            self._edges = list(edges_from_census())
        return self._edges

    def fetch_trailing_15m(
        self, close_time_iso: str, anchors: int = SIGMA_ANCHORS, step: int = ANCHOR_STEP
    ) -> list[dict]:
        """Fetch KXBTC15M markets co-settling in [T-step*(anchors-1), T]. NO status filter (the
        trailing anchors have settled). Returns [] fail-closed on any REST failure (the assignment
        then degrades to sub-$1-only via the merged current-close market)."""
        T = close_epoch(close_time_iso)
        lo = T - step * (anchors - 1)
        out: list[dict] = []
        cursor: str | None = None
        try:
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "series_ticker": FIFTEEN_SERIES,
                    "min_close_ts": lo,
                    "max_close_ts": T,
                    "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                resp = self._proxy.rest_get(MARKETS_PATH, params)
                out.extend(resp.get("markets", []) or [])
                cursor = resp.get("cursor") or None
                if not cursor:
                    break
        except Exception as e:  # noqa: BLE001 - a trailing-tape fetch failure is fail-closed
            logger.warning("[SIGMA] trailing 15M fetch failed for %s: %s", close_time_iso, e)
            return out
        return out

    def assign(
        self,
        close_time_iso: str,
        hourly_markets: list[dict],
        current_15m_markets: list[dict] | None = None,
        trailing_markets: list[dict] | None = None,
    ) -> QuintileOutcome:
        """Merge the trailing anchor tape + the current-close 15M market(s) and assign the quintile.

        Two-phase wake (live finding 2026-08-21): at the :40 wake the co-settling 15M market is
        "initialized" with NO floor_strike (the strike materializes only when it flips "active" at
        open_time, ~:45). So the trailing anchors (T-900..T-7200, all present with strikes at :40)
        are PREFETCHED in phase A and passed here as ``trailing_markets``; ``current_15m_markets``
        is the freshly-POLLED current market (phase B, post-open) carrying the anchor A(T). When
        ``trailing_markets`` is None the tape is fetched here (single-phase / back-compat path).
        Merging the polled current keeps the anchor present even if the trailing fetch was empty."""
        trailing = trailing_markets if trailing_markets is not None else self.fetch_trailing_15m(close_time_iso)
        merged = list(trailing)
        if current_15m_markets:
            seen = {id(m) for m in merged}
            for m in current_15m_markets:
                if id(m) not in seen:
                    merged.append(m)
        return assign_quintile(close_time_iso, merged, list(hourly_markets or []), self.edges())
