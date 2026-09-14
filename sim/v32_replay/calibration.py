"""calibration.py — aggregate the per-window calibration samples from the V3.2 ms journals.

The ms bucket books are a CALIBRATION SET for the sim's bucket-side assumptions. From every V3.2
journal window (T-15..T-5) we measure, at each spot-bucket YES trade:

  (a) spot-bucket agreement: the candle proxy (highest yes-mid at the minute boundary) vs the ms choice
      -> agreement rate.
  (b) cap error: cap_ms - cap_candle (cents), mean / p10 / p90, at each trade.
  (c) spread-aware maker rule vs the sim's strict rule: over spot-bucket yes prints (offer = 1 -
      n_shadow), the maker-rule regime split (i/ii/iii) and P(fill) under the maker rule vs the strict
      rule (p > offer), giving the fill factor = maker fills / strict fills; plus the print-size
      distribution of the strict-qualifying prints.
  (d) wing residual B on live timing: W(trade + 1.5 s) - W(trade), mean / p90.
  (e) requote cadence: replaces/window (ms model) vs the sim's 77.

These corrections are applied to the forward 139 h in ``forward.py``. Re-runnable: the same command
tomorrow, with more journal windows, tightens every estimate.
"""

from __future__ import annotations

import statistics


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[min(n - 1, max(0, int(round(q * (n - 1)))))]


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "p10": None, "p90": None,
                "min": None, "max": None}
    return {
        "n": len(xs), "mean": statistics.mean(xs), "median": statistics.median(xs),
        "p10": _pct(xs, 0.10), "p90": _pct(xs, 0.90), "min": min(xs), "max": max(xs),
    }


def aggregate(results: list) -> dict:
    """Pool the per-window ``WindowResult.calib`` samples into the calibration table."""
    n_windows = len(results)
    trades = sum(r.calib.get("n_trades", 0) for r in results)
    spot_agree = sum(r.calib.get("spot_agree", 0) for r in results)
    cap_err = [x for r in results for x in r.calib.get("cap_err_c", [])]
    n_eval = sum(r.calib.get("eval", 0) for r in results)
    regime = {"i": 0, "ii": 0, "iii": 0}
    for r in results:
        for k in regime:
            regime[k] += r.calib.get("regime", {}).get(k, 0)
    maker_fills = sum(r.calib.get("maker_fills", 0) for r in results)
    strict_fills = sum(r.calib.get("strict_fills", 0) for r in results)
    print_sizes = [x for r in results for x in r.calib.get("print_sizes", [])]
    B = [x for r in results for x in r.calib.get("B_resid_c", [])]
    replaces = [r.calib.get("base_replaces", 0) for r in results]

    p_fill_maker = (maker_fills / n_eval) if n_eval else None
    p_fill_strict = (strict_fills / n_eval) if n_eval else None
    # how the maker rule reshapes the strict-rule fill count (the forward thinning factor)
    fill_factor = (maker_fills / strict_fills) if strict_fills else (
        1.0 if maker_fills else None)
    factor_by_window: list[float] = []
    for r in results:
        m, s, _ = r.calib.get("fill_window", (0, 0, 0))
        if s > 0:
            factor_by_window.append(m / s)
    fill_factor_p10 = _pct(factor_by_window, 0.10) if factor_by_window else fill_factor

    return {
        "n_windows": n_windows,
        "n_trades": trades,
        "n_eval": n_eval,
        "spot_agreement": (spot_agree / trades) if trades else None,
        "cap_error_c": _stats(cap_err),
        "regime": regime,
        "regime_frac": {k: (v / n_eval if n_eval else None) for k, v in regime.items()},
        "p_fill_maker": p_fill_maker,
        "p_fill_strict": p_fill_strict,
        "fill_factor": fill_factor,
        "fill_factor_p10": fill_factor_p10,
        "n_maker_fills": maker_fills,
        "n_strict_fills": strict_fills,
        "print_size": _stats(print_sizes),
        "B_resid_c": _stats(B),
        "replaces_per_window_mean": statistics.mean(replaces) if replaces else None,
        "sim_replaces_per_hour_reference": 77,
    }
