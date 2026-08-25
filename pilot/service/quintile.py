"""quintile.py — reproduce the sim's sigma-hat (A3.2) + G/sigma quintile at WAKE, from
REST-available market data (via the proxy), so the pilot assigns the SAME quintile the frozen
census/tape-sim would assign for the same window.

WHAT THE SIM ACTUALLY USES (census.build_census, A3.2): the sigma-hat "anchor tape" is NOT
candle data — it is the sequence of 15-minute-market FLOOR STRIKES (the up/down anchor A) at the
9 contiguous top-of-window epochs T, T-900, ..., T-7200. sigma-hat = sample stdev (ddof=1) of the
8 consecutive anchor-to-anchor diffs. G = |K - A| rounded to cents, where A is the 15M floor
strike at T and K is the NEAREST hourly floor strike to A (ties excluded, as census does). The
quintile is bucket_of(G/sigma, edges) with edges = quintile_edges over the census_train OK rows.

So this module reproduces census from the /markets endpoint (the 15M anchor tape + the hourly
ladder), importing hole_G / sigma_hat / bucket_of / quintile_edges from the frozen law. Everything
is pure given the market lists; the caller's WakeContext supplies them via the proxy REST path.

CONFESSION (build report): the task framed the input as "REST candle data", but the census's
sigma-hat input is the 15M floor-strike sequence, so the faithful reproduction reads /markets
(floor_strike per co-settling 15M window), NOT candles. For a live wake the 8 trailing 15M
markets have already settled; querying them by close-ts is a live-verification item (they must
still be returned by /markets after settlement, or fetched without a status filter).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from service._simlaw import (
    ANCHOR_STEP,
    SIGMA_ANCHORS,
    EVCurve,
    InsufficientTape,
    bucket_of,
    close_epoch,
    hole_G,
    load_ev_curve,
    quintile_edges,
    read_ok_rows,
    sigma_hat,
)


@dataclass(frozen=True)
class QuintileResult:
    close_time: str
    T: int
    anchor_A: float
    threshold_K: float
    G: Decimal
    sigma_hat: float
    g_over_sigma: float
    quintile: int
    orientation: str          # "A_above_K" | "K_above_A" | "EQUAL"
    high_ticker: str          # higher-strike leg (buy YES for strangle)
    low_ticker: str           # lower-strike leg


@dataclass(frozen=True)
class NoQuintile:
    """A clean refusal to assign a quintile (mirrors census EXCL_* / NO_PAIR reasons)."""

    close_time: str
    reason: str


def edges_from_census(census_csv: str | None = None) -> list[float]:
    """The 4 G/sigma quintile edges from census_train OK rows — identical to gate.json's
    ``quintile_edges_gos`` (both derive from quintile_edges over the same OK rows)."""
    if census_csv is None:
        return list(load_ev_curve().edges)
    return quintile_edges([r["gos"] for r in read_ok_rows(census_csv)])


def anchor_tape_from_markets(markets_15m: list[dict]) -> dict[int, float]:
    """Build {close_epoch -> floor_strike} from 15M markets (skipping strike-less 'up/down'
    products, exactly as census's anchor tape does). A recurring epoch keeps the strike-bearing
    record (defensive, matching census's m15_by_ct preference)."""
    tape: dict[int, float] = {}
    seen_strikeful: set[int] = set()
    for m in markets_15m:
        fs = m.get("floor_strike")
        if fs is None:
            continue
        ct = m.get("close_time")
        if ct is None:
            continue
        ep = close_epoch(ct)
        if ep in seen_strikeful:
            continue
        tape[ep] = float(fs)
        seen_strikeful.add(ep)
    return tape


def _anchor_at(markets_15m: list[dict], close_time_iso: str) -> tuple[float | None, str | None]:
    """(floor_strike, ticker) of the strike-bearing 15M market co-settling at the target."""
    target = close_epoch(close_time_iso)
    for m in markets_15m:
        ct = m.get("close_time")
        if ct is None:
            continue
        if close_epoch(ct) == target and m.get("floor_strike") is not None:
            return float(m["floor_strike"]), str(m.get("ticker", ""))
    return None, None


def nearest_hourly_strike(
    anchor_A: float, hourly_markets: list[dict], close_time_iso: str
) -> tuple[float, str] | None | str:
    """Nearest hourly floor strike to A among the markets co-settling at the target.

    Returns (K, ticker), or None if there are no candidates, or the string "TIE" if the two
    nearest strikes are equidistant (census excludes these — EXCL_NEAREST_TIE)."""
    target = close_epoch(close_time_iso)
    cands = [
        m
        for m in hourly_markets
        if m.get("floor_strike") is not None
        and m.get("close_time") is not None
        and close_epoch(m["close_time"]) == target
    ]
    if not cands:
        return None
    annotated = sorted(
        ((abs(float(m["floor_strike"]) - anchor_A), m) for m in cands), key=lambda t: t[0]
    )
    if len(annotated) >= 2 and annotated[0][0] == annotated[1][0]:
        return "TIE"
    m = annotated[0][1]
    return float(m["floor_strike"]), str(m.get("ticker", ""))


def compute_window_stats(
    close_time_iso: str,
    markets_15m: list[dict],
    hourly_markets: list[dict],
    edges: list[float],
) -> QuintileResult | NoQuintile:
    """Reproduce (G, sigma_hat, g_over_sigma, quintile) + the leg pairing for one window.

    ``markets_15m`` must include the trailing 15M windows (T-7200..T) for the sigma-hat tape;
    ``hourly_markets`` the hourly ladder co-settling at the target. ``edges`` come from
    ``edges_from_census``. Returns NoQuintile (with a census-style reason) on any exclusion.
    """
    T = close_epoch(close_time_iso)

    A, a_ticker = _anchor_at(markets_15m, close_time_iso)
    if A is None or not a_ticker:
        return NoQuintile(close_time_iso, "EXCL_NO_ANCHOR (no strike-bearing 15M market at close)")

    near = nearest_hourly_strike(A, hourly_markets, close_time_iso)
    if near is None:
        return NoQuintile(close_time_iso, "EXCL_NO_1H_LEG (no hourly market co-settling)")
    if near == "TIE":
        return NoQuintile(close_time_iso, "EXCL_NEAREST_TIE (two hourly strikes equidistant)")
    K, k_ticker = near  # type: ignore[misc]

    G = hole_G(A, K)
    if G < Decimal("0.01"):
        return NoQuintile(close_time_iso, "EXCL_G_LT_EPS (degenerate corridor)")

    tape = anchor_tape_from_markets(markets_15m)
    try:
        sig = sigma_hat(tape, T)
    except InsufficientTape:
        return NoQuintile(
            close_time_iso,
            f"EXCL_SIGMA (insufficient 15M anchor tape for {SIGMA_ANCHORS} anchors "
            f"at step {ANCHOR_STEP}s)",
        )
    if sig == 0.0:
        return NoQuintile(close_time_iso, "EXCL_SIGMA_ZERO (flat anchor tape)")

    gos = float(G) / sig
    quintile = bucket_of(gos, edges)

    if A > K:
        orientation, high_ticker, low_ticker = "A_above_K", a_ticker, k_ticker
    elif K > A:
        orientation, high_ticker, low_ticker = "K_above_A", k_ticker, a_ticker
    else:
        orientation, high_ticker, low_ticker = "EQUAL", a_ticker, k_ticker

    return QuintileResult(
        close_time=close_time_iso,
        T=T,
        anchor_A=A,
        threshold_K=K,
        G=G,
        sigma_hat=sig,
        g_over_sigma=gos,
        quintile=quintile,
        orientation=orientation,
        high_ticker=high_ticker,
        low_ticker=low_ticker,
    )


def make_window_state_inputs(
    result: QuintileResult, ev_curve: EVCurve
) -> dict:
    """Convenience bridge to signal.WindowState.new(...): returns the kwargs a caller needs
    (quintile, fair_strangle_q, high/low tickers, G, sigma_hat) from a QuintileResult + EV curve."""
    return {
        "close_time": result.close_time,
        "high_ticker": result.high_ticker,
        "low_ticker": result.low_ticker,
        "quintile": result.quintile,
        "fair_strangle_q": ev_curve.fair_for("strangle", result.quintile),
        "G": result.G,
        "sigma_hat": result.sigma_hat,
        "T": result.T,
    }
