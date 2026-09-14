"""forward.py — the APPLY stage: re-run the corrected sim over the forward 139 h.

Reads the forward window 2026-08-30..09-04 (post-holdout; the exact days ``pf_ms_requote2.py`` already
uses) via the guarded ``scratchpad/range/rangelab.py`` loader (range candles + tape) and the ms strike
top-of-book under ``historical-data/tob/``. NEVER passes ``oos=True`` (the 2026-08-20..29 holdout stays
sealed); the forward days are not in any holdout, so the guard admits them without a flag.

The model is ``pf_ms_requote2.py``'s lagging-quote executor (spot + cap from the minute candle, wings
from ms strike books, fill = a 1-s YES print above 1 - n_rest, completion at print + 1.5 s), at
E in {0.08, 0.10, 0.12}, TOL = 0.02, DEB = 5000, LAT = 200. Money math is the pinned law
(``service.v32.core.solve_n`` / ``wing_cost`` / ``lock_value``, ``_simlaw.fee``), which reproduces the
scratch float sim to the cent.

Estimates, using the journal-side calibration (``calibration.aggregate``):
  * OPTIMISTIC  = uncorrected sim.
  * BASE        = cap shifted by the MEAN cap error; fills thinned by P(swept); lock reduced by
                  (mean live B residual - the sim's own tape B).
  * PESSIMISTIC = cap at the P10 cap error; fills thinned by the P10 of P(swept) and a 2-lot minimum
                  print; lock reduced by the P90 B residual and 1 extra tick per wing (2c).
"""

from __future__ import annotations

import bisect
import collections
import glob
import gzip
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from decimal import Decimal

from .pricing import lock_value, solve_n, wing_cost

# The scratchpad range loader location (session scratchpad); override via ``--range-loader``.
_RANGE_DIR = (
    r"C:/Users/Brads/AppData/Local/Temp/claude/C--Users-Brads-Python-stuff-degeneracy-v3/"
    r"e7acb14c-8903-4c04-ae00-7d39ed05e898/scratchpad/range"
)
TOB_DIR = r"C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data/tob"
FORWARD_START = "2026-08-30"
GRID_E = (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
TOL = Decimal("0.02")
DEB_MS = 5000
LAT_MS = 200
WIDTH = 100


def _load_rangelab(range_dir: str = _RANGE_DIR):
    if range_dir not in sys.path:
        sys.path.insert(0, range_dir)
    import rangelab as R  # noqa: E402
    return R


def _dec(x) -> Decimal | None:
    try:
        d = Decimal(str(x))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def _strike_of(tk: str) -> int | None:
    try:
        return round(float(tk.split("-T")[1]) + 0.01)
    except Exception:  # noqa: BLE001
        return None


def _at(rows, times, ts):
    i = bisect.bisect_right(times, ts) - 1
    return rows[i] if i >= 0 else None


def _tob_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for p in glob.glob(os.path.join(TOB_DIR, "*.json.gz")):
        name = os.path.basename(p).split(".")[0]        # 20260830T000000Z
        if len(name) < 15:
            continue
        iso = f"{name[:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}Z"
        idx[iso] = p
    return idx


def _load_range_day(R, day: str):
    """Per-close range candles (spot + bid/ask) and 1-s YES prints, mirroring pf_ms_requote2."""
    mp = f"{R.ROOT}/1-hour-range/markets/{day}.jsonl"
    cp = f"{R.ROOT}/1-hour-range/candles/{day}.jsonl"
    tp = f"{R.ROOT}/1-hour-range/trades/{day}.jsonl.gz"
    if not (os.path.exists(mp) and os.path.exists(cp) and os.path.exists(tp)):
        return None
    mkt = {}
    for m in R._load(mp):
        if m.get("floor_strike") is None or m.get("cap_strike") is None:
            continue
        if abs(float(m["cap_strike"]) - float(m["floor_strike"]) - 99.99) > 0.5:
            continue                                    # $100 buckets only
        mkt[m["ticker"]] = (m["close_time"], round(float(m["floor_strike"])))
    quotes: dict = collections.defaultdict(dict)        # ct -> lvl -> {ts: (bid, ask)}
    for r in R._load(cp):
        k = mkt.get(r["ticker"])
        if not k:
            continue
        q = {c["end_period_ts"]: (float(c["yes_bid"]["close_dollars"]),
                                  float(c["yes_ask"]["close_dollars"]))
             for c in r["candlesticks"] if 0 < float(c["yes_ask"]["close_dollars"]) <= 1}
        if q:
            quotes[k[0]][k[1]] = q
    prints: dict = collections.defaultdict(list)
    for r in R.trades("1-hour-range", day):
        k = mkt.get(r["ticker"])
        if not k:
            continue
        for t in r["trades"]:
            ts = int(datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp())
            prints[k].append((ts, float(t["yes_price_dollars"]), t.get("taker_side"),
                              float(t.get("count_fp") or 0)))
    for v in prints.values():
        v.sort()
    return quotes, prints


def _forward_days(R) -> list[str]:
    days = sorted(os.path.basename(x)[:10]
                  for x in glob.glob(f"{R.ROOT}/1-hour-range/trades/2026-*.jsonl.gz"))
    return [d for d in days if d >= FORWARD_START and not R.is_holdout(d)]


def run_forward(cap_shift_c: float = 0.0, min_print: int = 0,
                range_dir: str = _RANGE_DIR) -> dict:
    """Run the lagging model over the forward hours for every E. Returns per-E fill lists + n_hours.

    ``cap_shift_c`` shifts the (candle) cap by that many cents (the calibration cap correction);
    ``min_print`` drops fills whose through-print size is below it (the pessimistic 2-lot floor)."""
    R = _load_rangelab(range_dir)
    tob = _tob_index()
    cap_shift = Decimal(str(cap_shift_c)) / Decimal(100)
    days = _forward_days(R)
    fills_by_E: dict = {E: [] for E in GRID_E}
    n_hours = 0

    for day in days:
        d = _load_range_day(R, day)
        if not d:
            continue
        quotes, prints = d
        for ct, bk in quotes.items():
            if ct not in tob:
                continue
            cts = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
            with gzip.open(tob[ct], "rt", encoding="utf-8") as f:
                mk = json.load(f)["markets"]
            strikes: dict = {}
            for tk, v in mk.items():
                K = _strike_of(tk)
                if K is not None and v.get("tob"):
                    rows = v["tob"]
                    strikes[K] = (rows, [r[0] for r in rows])
            if not strikes:
                continue
            n_hours += 1

            def W_ms(Sd, t_ms):
                if Sd not in strikes or Sd + WIDTH not in strikes:
                    return None
                a = _at(*strikes[Sd], t_ms)
                b = _at(*strikes[Sd + WIDTH], t_ms)
                if not a or not b:
                    return None
                ya = _dec(a[2]); yb = _dec(b[1])
                if ya is None or yb is None or not (0 < ya < 1) or not (0 < (1 - yb) < 1):
                    return None
                return wing_cost(ya, 1 - yb)

            # per arm minute: candle spot, cap, wing tick times, spot-bucket prints
            allgrid = sorted({t for q in bk.values() for t in q})
            grid = [t for t in allgrid if cts - 60 * 15 <= t <= cts - 60 * 5]
            minutes = []
            for ts in grid:
                s, sm, qB = None, None, None
                for lvl, q in bk.items():
                    if ts in q:
                        m = (q[ts][0] + q[ts][1]) / 2
                        if sm is None or m > sm:
                            sm, s, qB = m, lvl, q[ts]
                if s is None or not (0 < qB[1] <= 1 and 0 <= qB[0] <= qB[1]):
                    continue
                if s not in strikes or s + WIDTH not in strikes:
                    continue
                cap = ((Decimal(1) - _dec(qB[0])) - Decimal("0.01")).quantize(Decimal("0.01"))
                cap = (cap + cap_shift)
                ticks = sorted(
                    {r[0] for r in strikes[s][0] if ts * 1000 <= r[0] < (ts + 60) * 1000}
                    | {r[0] for r in strikes[s + WIDTH][0] if ts * 1000 <= r[0] < (ts + 60) * 1000}
                    | {ts * 1000}
                )
                pr = [(pts, yp, sz) for pts, yp, sd, sz in prints.get((ct, s), [])
                      if ts < pts <= ts + 60 and sd == "yes"]
                minutes.append((ts, s, cap, ticks, pr))

            for E in GRID_E:
                fill = _run_hour(E, minutes, W_ms, min_print, ct)
                if fill is not None:
                    fills_by_E[E].append(fill)

    return {"fills_by_E": fills_by_E, "n_hours": n_hours, "days": days}


def _run_hour(E: Decimal, minutes, W_ms, min_print: int, ct: str):
    """One hour of the lagging executor; returns the first fill dict or None."""
    n_rest = None; live_at = None; pending = None; last_rep = -1e18; prev_s = None
    for ts, s, cap, ticks, pr in minutes:
        if s != prev_s:
            n_rest = None; pending = None; forced = True; prev_s = s
        else:
            forced = False
        events = [(t, "tick", None) for t in ticks] + [(p[0] * 1000, "print", p) for p in pr]
        events.sort(key=lambda e: e[0])
        for tv, kind, payload in events:
            if pending is not None and tv >= live_at:
                n_rest = pending; pending = None
            if kind == "tick":
                if pending is not None:
                    continue
                W = W_ms(s, tv)
                nd = solve_n(Decimal(2) - E - W, cap) if W is not None else None
                if nd is None:
                    continue
                if forced or n_rest is None or (abs(nd - n_rest) >= TOL and tv - last_rep >= DEB_MS):
                    if n_rest is not None and nd == n_rest:
                        continue
                    pending = nd; live_at = tv + LAT_MS; last_rep = tv; forced = False
            else:
                pts, yp, sz = payload
                ypd = _dec(yp)
                if n_rest is None or ypd is None or not (ypd > (Decimal(1) - n_rest)):
                    continue
                if min_print and sz < min_print:
                    continue
                W_trade = W_ms(s, tv)
                W15 = W_ms(s, tv + 1500)
                if W15 is None:
                    continue
                lock = lock_value(n_rest, W15)
                return {
                    "ct": ct, "s": s, "n": n_rest, "yp": ypd, "size": sz,
                    "lock_c": float(lock * 100),
                    "W_trade_c": float(W_trade * 100) if W_trade is not None else None,
                    "W15_c": float(W15 * 100),
                    "B_tape_c": (float((W15 - W_trade) * 100) if W_trade is not None else None),
                }
    return None


# ---------------------------------------------------------------------------
# corrected estimates
# ---------------------------------------------------------------------------
def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[min(n - 1, max(0, int(round(q * (n - 1)))))]


def _lock_stats(locks, n_hours, thin=1.0):
    if not locks:
        return {"n_fills": 0, "fills_per_day": 0.0, "mean_c": None, "median_c": None,
                "p10_c": None, "min_c": None, "pct_positive": None, "c_per_day": 0.0}
    days = n_hours / 24 if n_hours else None
    raw_rate = (len(locks) / days) if days else None
    return {
        "n_fills": len(locks),
        "fills_per_day": (raw_rate * thin) if raw_rate is not None else None,
        "mean_c": statistics.mean(locks), "median_c": statistics.median(locks),
        "p10_c": _pct(locks, 0.10), "min_c": min(locks),
        "pct_positive": sum(1 for x in locks if x > 0) / len(locks),
        "c_per_day": ((sum(locks) / days) * thin) if days else None,
    }


def build_forward_estimates(calib: dict, range_dir: str = _RANGE_DIR) -> dict:
    """Run the three corrected forward estimates for each E, using the journal-side calibration."""
    cap_err = calib.get("cap_error_c") or {}
    cap_err_mean = cap_err.get("mean") or 0.0
    cap_err_p10 = cap_err.get("p10") or 0.0
    p_swept = calib.get("p_swept")
    p_swept_p10 = calib.get("p_swept_p10")
    B = calib.get("B_resid_c") or {}
    B_live_mean = B.get("mean") or 0.0
    B_live_p90 = B.get("p90") or 0.0
    thin_base = p_swept if p_swept is not None else 1.0
    thin_pess = p_swept_p10 if p_swept_p10 is not None else thin_base

    opt = run_forward(cap_shift_c=0.0, range_dir=range_dir)
    base = run_forward(cap_shift_c=cap_err_mean, range_dir=range_dir)
    pess = run_forward(cap_shift_c=cap_err_p10, min_print=2, range_dir=range_dir)

    def _sim_tape_B(run) -> float:
        vals = [f["B_tape_c"] for E in GRID_E for f in run["fills_by_E"][E]
                if f.get("B_tape_c") is not None]
        return statistics.mean(vals) if vals else 0.0

    sim_tape_B_opt = _sim_tape_B(opt)
    base_B_corr = B_live_mean - sim_tape_B_opt          # cents subtracted from each lock
    pess_B_corr = B_live_p90 - sim_tape_B_opt

    out: dict = {
        "n_hours": opt["n_hours"], "n_days": len(opt["days"]),
        "cap_shift_base_c": cap_err_mean, "cap_shift_pess_c": cap_err_p10,
        "thin_base": thin_base, "thin_pess": thin_pess,
        "sim_tape_B_c": sim_tape_B_opt, "base_B_corr_c": base_B_corr, "pess_B_corr_c": pess_B_corr,
        "by_E": {},
    }
    for E in GRID_E:
        opt_locks = [f["lock_c"] for f in opt["fills_by_E"][E]]
        base_locks = [f["lock_c"] - base_B_corr for f in base["fills_by_E"][E]]
        pess_locks = [f["lock_c"] - pess_B_corr - 2.0 for f in pess["fills_by_E"][E]]
        out["by_E"][str(E)] = {
            "optimistic": _lock_stats(opt_locks, opt["n_hours"], thin=1.0),
            "base": _lock_stats(base_locks, base["n_hours"], thin=thin_base),
            "pessimistic": _lock_stats(pess_locks, pess["n_hours"], thin=thin_pess),
        }
    return out
