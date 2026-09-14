"""calibration.py — aggregate the per-window calibration samples from the V3.2 ms journals.

The ms bucket books are a CALIBRATION SET for the sim's bucket-side assumptions. From every V3.2
journal window (T-15..T-5) we measure, at each spot-bucket YES trade:

  (a) spot-bucket agreement: the candle proxy (highest yes-mid at the minute boundary) vs the ms choice
      -> agreement rate.
  (b) cap error: cap_ms - cap_candle (cents), mean / p10 / p90, at each trade.
  (c) fill-rule sweep haircut: over yes prints above the offer (1 - n_shadow, the sim's fill rule), the
      fraction where the bucket's best YES ask immediately before the print was <= our offer (genuinely
      swept) -> P(swept | print rule); plus the print-size distribution.
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
    qualify = sum(r.calib.get("qualify", 0) for r in results)
    qualify_swept = sum(r.calib.get("qualify_swept", 0) for r in results)
    print_sizes = [x for r in results for x in r.calib.get("print_sizes", [])]
    B = [x for r in results for x in r.calib.get("B_resid_c", [])]
    replaces = [r.calib.get("base_replaces", 0) for r in results]

    # per-window P(swept) for the pessimistic p10
    p_swept_by_window: list[float] = []
    for r in results:
        q, s = r.calib.get("qualify_window", (0, 0))
        if q > 0:
            p_swept_by_window.append(s / q)

    p_swept = (qualify_swept / qualify) if qualify else None
    p_swept_p10 = _pct(p_swept_by_window, 0.10) if p_swept_by_window else (
        p_swept if p_swept is not None else None
    )

    return {
        "n_windows": n_windows,
        "n_trades": trades,
        "spot_agreement": (spot_agree / trades) if trades else None,
        "cap_error_c": _stats(cap_err),
        "p_swept": p_swept,
        "p_swept_p10": p_swept_p10,
        "n_qualify": qualify,
        "n_qualify_swept": qualify_swept,
        "print_size": _stats(print_sizes),
        "B_resid_c": _stats(B),
        "replaces_per_window_mean": statistics.mean(replaces) if replaces else None,
        "sim_replaces_per_hour_reference": 77,
    }
