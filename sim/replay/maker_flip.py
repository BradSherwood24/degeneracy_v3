"""Maker-flip backtest over the pilot WS journals, on the engine-time replayer.

The pilot's sub-$1 FLIP: leg H = BUY NO on ``high_ticker``; leg L = BUY YES on ``low_ticker``.
Taker-model  C(t) = no_ask_H + fee(no_ask_H) + yes_ask_L + fee(yes_ask_L).

Policy (ONE pair per window): while active and no pair completed, if C(t) <= theta rest a NO bid
on H at (no_ask_H - 0.01) and a YES bid on L at (yes_ask_L - 0.01), 1 contract each; if C(t) > theta
cancel both. On the FIRST resting-bid fill (leg A at t_A, price q_A), for each Delta in the grid we
branch counterfactually: if leg B's resting bid also fills by t_A+Delta -> both-maker; else take leg B
as a taker at its best ask at engine time t_A+Delta.

Fill rules (both run):
  STRICT (trade-through): NO bid at q fills iff a taker_side "yes" print at yes_price p has (1-p) < q;
    YES bid at q fills iff a taker_side "no" print at yes_price p has p < q. (A floor: only through-trades.)
  LENIENT (queue): NO bid at q also fills once cumulative taker_side "yes" prints at yes_price == 1-q
    reach queue_ahead + 1 contracts; symmetric for the YES bid (taker_side "no" prints at yes_price == q).

Fees: taker = fee(price) IMPORTED from sim/census.py; maker = maker_mult * fee(price),
maker_mult in {0.25, 0.0} (both columns). Nothing here is retyped.

Single-pass design: all events are the pair's 2 legs. One fold produces, per book-mutating frame,
only the scalars the policy needs: no_ask_H, yes_ask_L (=> C, target prices, chase asks) and the
depth resting at our two target bid levels (=> queue_ahead). Trades are kept as light tuples. The 16
policy configs (theta x fill_rule x period) then replay those events in memory with no re-folding.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
from functools import lru_cache

# --- frozen law imports (single choke point, mirrors pilot/service/_simlaw.py) ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SIM_DIR = os.path.join(_REPO_ROOT, "sim")
if _SIM_DIR not in sys.path:
    sys.path.append(_SIM_DIR)
import census as _census          # noqa: E402
import tape_sim as _tape_sim      # noqa: E402

fee = _census.fee                 # taker fee per contract: ceil(0.07 p (1-p) 1e4)/1e4 dollars
WINDOW_S = _tape_sim.WINDOW_S     # 900 s — the pilot's final window

from .journal import iter_ws, read_window_header   # noqa: E402
from .book import WindowBook, mils_to_dollars       # noqa: E402
from .selfcheck import Reconciler                    # noqa: E402

# --- parameter grids (spec) ---
THETAS = (1.00, 0.99, 0.98, 0.97)
DELTAS = (0.10, 0.15, 0.30, 0.50, 1.00)
FILL_RULES = ("strict", "lenient")
MAKER_MULTS = (0.25, 0.0)
PERIODS = ("full", "last_window")

TRAIN_CUTOFF_ISO = "2026-08-23T13:00:00Z"
SANITY_WINDOW = "20260824T050000Z"
SANITY_H_YESBID_MILS = 540   # yes-bid 0.54 on the hourly = the NO-leg target level (NO ask 0.46)

_EPS = 1e-9


@lru_cache(maxsize=4096)
def _fee_mils(price_mils: int) -> float:
    """Taker fee in dollars for a price given in mils (cached; exact via census.fee/Decimal)."""
    return float(fee(mils_to_dollars(price_mils)))


def _dollars(mils: int) -> float:
    return mils / 1000.0


def _cutoff_epoch() -> float:
    s = TRAIN_CUTOFF_ISO[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s).timestamp()


# ======================================================================================
#  Single-pass window parse: build the compact event stream + reconciliation + sanity.
# ======================================================================================

def _parse_window(path: str):
    """One journal pass. Returns (header, events, ask_timeline, t_ready_ms, recon_stats, sanity)."""
    hdr = read_window_header(path)
    if hdr.skip:
        return hdr, None, None, None, None, None

    H, L = hdr.high_ticker, hdr.low_ticker
    wb = WindowBook((H, L))
    recon = Reconciler((H, L))

    events = []            # journal order: ('B', ts, idx, no_ask_H, yes_ask_L, qH, qL, dqH, dqL)
    #                                   or  ('T', ts, idx, leg('H'/'L'), taker, yes_price_mils, count)
    pending_snap_idx = []  # indices into events whose ts must be back-filled from next real ts
    cur_ts = None
    t_ready_ms = None
    last_scalars = None

    is_sanity = os.path.basename(path).replace(".jsonl", "") == SANITY_WINDOW
    sanity_540 = []        # (ts, size_hundredths) whenever depth at H yes-bid 540 mils changes
    last_540 = None

    def scalars():
        tH = wb.top_of_book(H)
        tL = wb.top_of_book(L)
        no_ask_H = tH.no_ask if tH else None          # 1 - best_yes_bid(H)
        yes_ask_L = tL.yes_ask if tL else None         # 1 - best_no_bid(L)
        qH = (no_ask_H - 10) if no_ask_H is not None else None
        qL = (yes_ask_L - 10) if yes_ask_L is not None else None
        dqH = wb.size_at(H, "no", qH) if qH is not None else 0     # our-side depth at target
        dqL = wb.size_at(L, "yes", qL) if qL is not None else 0
        return no_ask_H, yes_ask_L, qH, qL, dqH, dqL

    for obj in iter_ws(path):
        recon.feed(obj)
        t = obj.get("type")
        msg = obj.get("msg") or {}
        tkr = msg.get("market_ticker")
        idx = obj.get("_idx")

        if t == "trade":
            if tkr == H:
                leg = "H"
            elif tkr == L:
                leg = "L"
            else:
                continue
            ts = msg.get("ts_ms")
            if ts is not None:
                cur_ts = ts
                if pending_snap_idx:
                    for i in pending_snap_idx:
                        ev = events[i]
                        events[i] = ("B", ts) + ev[2:]
                    pending_snap_idx.clear()
            try:
                ypm = int(round(float(msg.get("yes_price_dollars")) * 1000))
                cnt = float(msg.get("count_fp"))
            except (TypeError, ValueError):
                continue
            events.append(("T", ts if ts is not None else cur_ts, idx, leg,
                           msg.get("taker_side"), ypm, cnt))
            continue

        touched = wb.feed(obj)
        if touched is None:
            continue

        if t == "orderbook_delta":
            ts = msg.get("ts_ms")
            if ts is not None:
                cur_ts = ts
                if pending_snap_idx:
                    for i in pending_snap_idx:
                        ev = events[i]
                        events[i] = ("B", ts) + ev[2:]
                    pending_snap_idx.clear()
        else:  # snapshot: no ts_ms -> back-fill from next real ts
            ts = None

        if t_ready_ms is None and wb.ready():
            t_ready_ms = cur_ts   # ts at which both legs first have a snapshot

        if is_sanity:
            s540 = wb.size_at(H, "yes", SANITY_H_YESBID_MILS)
            if s540 != last_540:
                sanity_540.append((cur_ts, s540))
                last_540 = s540

        sc = scalars()
        if sc != last_scalars:
            frame = ("B", ts if ts is not None else cur_ts, idx) + sc
            events.append(frame)
            if ts is None:
                pending_snap_idx.append(len(events) - 1)
            last_scalars = sc

    recon.finalize()

    # ask timeline sorted by ts (ts can backstep ~1s); for chase queries at arbitrary engine time.
    ask_ts = []
    ask_H = []
    ask_L = []
    tl = sorted((e for e in events if e[0] == "B" and e[1] is not None),
                key=lambda e: e[1])
    for e in tl:
        ask_ts.append(e[1])
        ask_H.append(e[3])   # no_ask_H mils (or None)
        ask_L.append(e[4])   # yes_ask_L mils (or None)
    ask_timeline = (ask_ts, ask_H, ask_L)

    sanity = None
    if is_sanity:
        sanity = _build_sanity(hdr, sanity_540)

    return hdr, events, ask_timeline, t_ready_ms, recon.stats, sanity


def _build_sanity(hdr, sanity_540):
    """Locate the 1->0 vanish of the NO-leg target level (H yes-bid 0.54) near the live fire."""
    fire_ms = None
    if hdr.close_epoch is not None and hdr.fire_t_minus_s is not None:
        fire_ms = (hdr.close_epoch - hdr.fire_t_minus_s) * 1000.0
    transitions = []
    for i in range(1, len(sanity_540)):
        prev_ts, prev_sz = sanity_540[i - 1]
        ts, sz = sanity_540[i]
        if prev_sz is not None and prev_sz >= 100 and (sz is None or sz == 0):
            transitions.append((prev_ts, prev_sz, ts, sz, ts - prev_ts if (ts and prev_ts) else None))
    # the transition whose drop-time is closest to the fire engine time
    best = None
    if fire_ms is not None and transitions:
        best = min(transitions, key=lambda tr: abs((tr[2] or 0) - fire_ms))
    return {
        "fire_C": hdr.fire_C,
        "fire_t_minus_s": hdr.fire_t_minus_s,
        "fire_ms": fire_ms,
        "changelog_len": len(sanity_540),
        "transitions": transitions[:12],
        "closest_to_fire": best,
        "sample_head": sanity_540[:6],
    }


# ======================================================================================
#  Policy replay over the compact events (per theta x fill_rule x period).
# ======================================================================================

def _ask_at(ask_timeline, leg, t_ms):
    """Best ask (mils) for leg ('H'/'L') as of the last book frame with ts <= t_ms; None if absent."""
    ask_ts, ask_H, ask_L = ask_timeline
    if not ask_ts or t_ms is None:
        return None
    lo, hi = 0, len(ask_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ask_ts[mid] <= t_ms:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if i < 0:
        return None
    return (ask_H if leg == "H" else ask_L)[i]


def _run_policy(events, theta, fill_rule, t_start_ms, t_end_ms):
    """Replay one policy config over the compact event stream.

    Returns dict with: first (leg, t_A, q_A, qB_rest_at_tA or None), other (t_B, qB) or None,
    and rest stats (episodes, rest_time_ms, replaces, cancels, any_fill).
    """
    lenient = (fill_rule == "lenient")
    restH = restL = None                 # our resting bid price (mils) or None
    pH = pL = None                       # placement (ts, idx)
    qaH = qaL = 0.0                      # queue_ahead (contracts) captured at placement
    cumH = cumL = 0.0                    # lenient cumulative print counts since placement

    first = None                         # (leg, t_A, q_A, qB_rest_at_tA)
    other = None                         # (t_B, qB) — the leg that is NOT leg A
    filledH = filledL = False

    epis = {"H": 0, "L": 0}
    rest_time = {"H": 0, "L": 0}
    rest_since = {"H": None, "L": None}
    replaces = cancels = 0

    def stop_rest(leg, ts):
        if rest_since[leg] is not None and ts is not None:
            rest_time[leg] += max(0, ts - rest_since[leg])
            rest_since[leg] = None

    def register(leg, ts, price):
        nonlocal first, other, filledH, filledL
        if leg == "H":
            if filledH:
                return
            filledH = True
        else:
            if filledL:
                return
            filledL = True
        if first is None:
            qB_rest = restL if leg == "H" else restH   # leg B's resting price at t_A (or None)
            first = (leg, ts, price, qB_rest)
        elif other is None and leg != first[0]:
            other = (ts, price)

    for ev in events:
        ts = ev[1]
        if ts is None or ts < t_start_ms or ts > t_end_ms:
            continue

        if ev[0] == "T":
            _, _, idx, leg, taker, ypm, cnt = ev
            if leg == "H" and restH is not None and (ts > pH[0] or (ts == pH[0] and idx > pH[1])):
                hit = (taker == "yes" and ypm > 1000 - restH)          # STRICT trade-through
                if lenient and taker == "yes" and ypm == 1000 - restH:  # LENIENT queue
                    cumH += cnt
                    if cumH >= qaH + 1.0:
                        hit = True
                if hit:
                    register("H", ts, restH)
            elif leg == "L" and restL is not None and (ts > pL[0] or (ts == pL[0] and idx > pL[1])):
                hit = (taker == "no" and ypm < restL)                   # STRICT trade-through
                if lenient and taker == "no" and ypm == restL:          # LENIENT queue
                    cumL += cnt
                    if cumL >= qaL + 1.0:
                        hit = True
                if hit:
                    register("L", ts, restL)
            continue

        # 'B' frame — placement/replace/cancel, frozen once a pair's first leg has filled.
        if first is not None:
            continue
        _, _, idx, no_ask_H, yes_ask_L, qH, qL, dqH, dqL = ev
        if no_ask_H is None or yes_ask_L is None:
            C = float("inf")
        else:
            C = _dollars(no_ask_H) + _fee_mils(no_ask_H) + _dollars(yes_ask_L) + _fee_mils(yes_ask_L)

        if C <= theta + _EPS and qH is not None and qL is not None and qH > 0 and qL > 0:
            if restH != qH:
                if restH is None:
                    epis["H"] += 1
                    rest_since["H"] = ts
                else:
                    replaces += 1
                restH, pH, qaH, cumH = qH, (ts, idx), dqH / 100.0, 0.0
            if restL != qL:
                if restL is None:
                    epis["L"] += 1
                    rest_since["L"] = ts
                else:
                    replaces += 1
                restL, pL, qaL, cumL = qL, (ts, idx), dqL / 100.0, 0.0
        else:
            if restH is not None:
                cancels += 1
                stop_rest("H", ts)
                restH = None
            if restL is not None:
                cancels += 1
                stop_rest("L", ts)
                restL = None

    # close any still-open rest intervals at t_end
    stop_rest("H", t_end_ms)
    stop_rest("L", t_end_ms)

    return {
        "first": first,
        "other": other,
        "episodes": epis["H"] + epis["L"],
        "rest_time_ms": rest_time["H"] + rest_time["L"],
        "replaces": replaces,
        "cancels": cancels,
        "any_fill": filledH or filledL,
    }


def _outcomes_for_config(pol, ask_timeline):
    """Given a policy result with a first fill, compute per-Delta x maker_mult outcomes.

    Yields dicts: delta, both_maker, unhedged, cost{mult}, chase_gap, chase_gap_vs_rest, ask_B_tA,
    ask_B_target.
    """
    first = pol["first"]
    if first is None:
        return
    legA, t_A, q_A, qB_rest = first
    legB = "L" if legA == "H" else "H"
    other = pol["other"]                      # (t_B, qB) natural maker fill of leg B, or None
    ask_B_tA = _ask_at(ask_timeline, legB, t_A)

    for delta in DELTAS:
        t_target = t_A + delta * 1000.0
        both_maker = bool(other is not None and other[0] <= t_target)
        rec = {"delta": delta, "both_maker": both_maker, "unhedged": False,
               "ask_B_tA": ask_B_tA, "ask_B_target": None,
               "chase_gap": None, "chase_gap_vs_rest": None, "costs": {}}
        if both_maker:
            qB = other[1]
            for mm in MAKER_MULTS:
                rec["costs"][mm] = (_dollars(q_A) + mm * _fee_mils(q_A)
                                    + _dollars(qB) + mm * _fee_mils(qB))
        else:
            ask_B_t = _ask_at(ask_timeline, legB, t_target)
            rec["ask_B_target"] = ask_B_t
            if ask_B_t is None:
                rec["unhedged"] = True
                for mm in MAKER_MULTS:
                    rec["costs"][mm] = None
            else:
                if ask_B_tA is not None:
                    rec["chase_gap"] = _dollars(ask_B_t) - _dollars(ask_B_tA)
                if qB_rest is not None:
                    rec["chase_gap_vs_rest"] = _dollars(ask_B_t) - (_dollars(qB_rest) + 0.01)
                for mm in MAKER_MULTS:
                    rec["costs"][mm] = (_dollars(q_A) + mm * _fee_mils(q_A)
                                        + _dollars(ask_B_t) + _fee_mils(ask_B_t))
        yield rec


# ======================================================================================
#  Worker: parse one window and run all 16 policy configs; return compact result.
# ======================================================================================

def process_window(path: str) -> dict:
    hdr, events, ask_timeline, t_ready_ms, recon, sanity = _parse_window(path)
    name = os.path.basename(path).replace(".jsonl", "")
    out = {
        "window": name, "path": path,
        "close_time": hdr.close_time, "close_epoch": hdr.close_epoch,
        "high_ticker": hdr.high_ticker, "low_ticker": hdr.low_ticker,
        "leg_source": hdr.leg_source, "skip": hdr.skip, "skip_reason": hdr.skip_reason,
        "fire_kind": hdr.fire_kind, "fire_C": hdr.fire_C,
        "recon": None, "configs": [], "sanity": sanity,
    }
    if hdr.skip:
        return out
    if recon is not None:
        out["recon"] = {
            "n_trades": recon.n_trades, "n_matched": recon.n_matched,
            "rate": recon.rate, "n_deltas": recon.n_deltas, "n_neg_deltas": recon.n_neg_deltas,
            "lag_p50": recon.lag_p50(), "lag_p99": recon.lag_p99(),
        }
    if hdr.close_epoch is None or t_ready_ms is None or not events:
        out["no_active"] = True
        return out

    close_ms = hdr.close_epoch * 1000.0
    t_end = close_ms - 1000.0
    periods = {
        "full": (t_ready_ms, t_end),
        "last_window": (max(t_ready_ms, close_ms - WINDOW_S * 1000.0), t_end),
    }

    for period, (t_start, t_end_p) in periods.items():
        active = t_start is not None and t_end_p is not None and t_start <= t_end_p
        for theta in THETAS:
            for fr in FILL_RULES:
                cfg = {"period": period, "theta": theta, "fill_rule": fr,
                       "active": active, "first": None, "rest": None, "outcomes": []}
                if active:
                    pol = _run_policy(events, theta, fr, t_start, t_end_p)
                    cfg["rest"] = {k: pol[k] for k in
                                   ("episodes", "rest_time_ms", "replaces", "cancels", "any_fill")}
                    if pol["first"] is not None:
                        legA, t_A, q_A, qB_rest = pol["first"]
                        cfg["first"] = {
                            "legA": legA, "t_A_to_close_s": (close_ms - t_A) / 1000.0,
                            "q_A": _dollars(q_A),
                            "C_at_tA": None,  # taker-model at t_A (filled below)
                        }
                        cfg["first"]["C_at_tA"] = _C_at(ask_timeline, t_A)
                        for rec in _outcomes_for_config(pol, ask_timeline):
                            cfg["outcomes"].append(rec)
                out["configs"].append(cfg)
    return out


def _C_at(ask_timeline, t_ms):
    aH = _ask_at(ask_timeline, "H", t_ms)
    aL = _ask_at(ask_timeline, "L", t_ms)
    if aH is None or aL is None:
        return None
    return _dollars(aH) + _fee_mils(aH) + _dollars(aL) + _fee_mils(aL)


# ======================================================================================
#  Aggregation + reporting.
# ======================================================================================

def _pctl(vals, q):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] * (1 - (pos - lo)) + v[hi] * (pos - lo)


def _split_label(close_epoch, cutoff):
    return "TRAIN" if (close_epoch is not None and close_epoch < cutoff) else "HOLDOUT"


def aggregate(results, cutoff):
    """Build the aggregate table. Returns dict[(split, period, fill_rule, mult, theta, delta)] -> row."""
    # distinct active windows per (split, period)
    active_win_sets = {}
    agg = {}
    for r in results:
        if r.get("skip") or r.get("no_active"):
            continue
        split = _split_label(r.get("close_epoch"), cutoff)
        for cfg in r["configs"]:
            if not cfg["active"]:
                continue
            active_win_sets.setdefault((split, cfg["period"]), set()).add(r["window"])
            for mm in MAKER_MULTS:
                for rec in cfg["outcomes"]:
                    k = (split, cfg["period"], cfg["fill_rule"], mm, cfg["theta"], rec["delta"])
                    a = agg.setdefault(k, {
                        "n_first": 0, "n_both": 0, "n_unhedged": 0,
                        "costs": [], "chase_gaps": [], "lt100": 0, "lt102": 0, "n_cost": 0,
                    })
                    # outcomes only exist when this config had a first fill; one window ->
                    # one increment per (theta,fill_rule,mult,delta) key.
                    a["n_first"] += 1
                    if rec["both_maker"]:
                        a["n_both"] += 1
                    if rec["unhedged"]:
                        a["n_unhedged"] += 1
                    c = rec["costs"].get(mm)
                    if c is not None:
                        a["costs"].append(c)
                        a["n_cost"] += 1
                        if c < 1.00:
                            a["lt100"] += 1
                        if c < 1.02:
                            a["lt102"] += 1
                    if (not rec["both_maker"]) and rec["chase_gap"] is not None:
                        a["chase_gaps"].append(rec["chase_gap"])
    # finalize
    active_windows = {k: len(v) for k, v in active_win_sets.items()}
    rows = {}
    for k, a in agg.items():
        split, period = k[0], k[1]
        naw = active_windows.get((split, period), 0)
        nf = a["n_first"]
        rows[k] = {
            "n_windows_active": naw,
            "n_first_fills": nf,
            "n_both_maker": a["n_both"],
            # Conditioned on a first fill: an unhedged outcome (no completing ask) is a loss,
            # so it counts against P(cost<X) — denominator is n_first, not just costed outcomes.
            "p_cost_lt100": (a["lt100"] / nf) if nf else None,
            "p_cost_lt102": (a["lt102"] / nf) if nf else None,
            "mean_cost": (statistics.fmean(a["costs"]) if a["costs"] else None),
            "median_cost": (statistics.median(a["costs"]) if a["costs"] else None),
            "chase_gap_p10": _pctl(a["chase_gaps"], 0.10),
            "chase_gap_p50": _pctl(a["chase_gaps"], 0.50),
            "chase_gap_p90": _pctl(a["chase_gaps"], 0.90),
            "n_unhedged": a["n_unhedged"],
            "n_cost": a["n_cost"],
        }
    return rows, active_windows


def _fmt(x, nd=4):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_markdown(rows, active_windows, splits=("TRAIN", "HOLDOUT")):
    lines = []
    for split in splits:
        for period in PERIODS:
            naw = active_windows.get((split, period), 0)
            lines.append(f"\n### {split} — period={period}  (n_windows_active={naw})\n")
            header = ("| fill | mult | θ | Δ | n_first | n_both | P<1.00 | P<1.02 | mean | "
                      "median | gap_p10 | gap_p50 | gap_p90 | unhedged |")
            lines.append(header)
            lines.append("|" + "---|" * 13)
            for fr in FILL_RULES:
                for mm in MAKER_MULTS:
                    for theta in THETAS:
                        for delta in DELTAS:
                            k = (split, period, fr, mm, theta, delta)
                            if k not in rows:
                                continue
                            r = rows[k]
                            lines.append(
                                f"| {fr} | {mm} | {theta:.2f} | {delta:.2f} | "
                                f"{r['n_first_fills']} | {r['n_both_maker']} | "
                                f"{_fmt(r['p_cost_lt100'],3)} | {_fmt(r['p_cost_lt102'],3)} | "
                                f"{_fmt(r['mean_cost'])} | {_fmt(r['median_cost'])} | "
                                f"{_fmt(r['chase_gap_p10'])} | {_fmt(r['chase_gap_p50'])} | "
                                f"{_fmt(r['chase_gap_p90'])} | {r['n_unhedged']} |")
    return "\n".join(lines)


def write_per_window_csv(results, path):
    cols = ["window", "split", "close_time", "high_ticker", "low_ticker", "leg_source",
            "period", "theta", "fill_rule", "maker_mult", "active", "any_fill",
            "episodes", "rest_time_ms", "replaces", "cancels",
            "first_leg", "t_A_to_close_s", "q_A", "C_at_tA",
            "delta", "both_maker", "unhedged", "cost", "chase_gap", "chase_gap_vs_rest",
            "ask_B_tA", "ask_B_target"]
    cutoff = _cutoff_epoch()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in results:
            if r.get("skip"):
                continue
            split = _split_label(r.get("close_epoch"), cutoff)
            for cfg in r.get("configs", []):
                first = cfg.get("first")
                rest = cfg.get("rest") or {}
                base = [r["window"], split, r["close_time"], r["high_ticker"], r["low_ticker"],
                        r["leg_source"], cfg["period"], cfg["theta"], cfg["fill_rule"]]
                for mm in MAKER_MULTS:
                    common = base + [mm, cfg["active"], rest.get("any_fill"),
                                     rest.get("episodes"), rest.get("rest_time_ms"),
                                     rest.get("replaces"), rest.get("cancels"),
                                     (first or {}).get("legA"), (first or {}).get("t_A_to_close_s"),
                                     (first or {}).get("q_A"), (first or {}).get("C_at_tA")]
                    if not cfg["outcomes"]:
                        w.writerow(common + ["", "", "", "", "", "", "", ""])
                    else:
                        for rec in cfg["outcomes"]:
                            w.writerow(common + [rec["delta"], rec["both_maker"], rec["unhedged"],
                                                 _fmt(rec["costs"].get(mm)) if rec["costs"].get(mm) is not None else "",
                                                 _fmt(rec["chase_gap"]) if rec["chase_gap"] is not None else "",
                                                 _fmt(rec["chase_gap_vs_rest"]) if rec["chase_gap_vs_rest"] is not None else "",
                                                 rec["ask_B_tA"], rec["ask_B_target"]])


def _find_journals(journal_dir):
    import glob
    files = sorted(glob.glob(os.path.join(journal_dir, "2026*.jsonl")))
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maker-flip backtest over pilot WS journals.")
    ap.add_argument("--journals", default=os.path.join(_REPO_ROOT, "pilot", "journals"))
    ap.add_argument("--out", default=os.path.join(_SIM_DIR, "out", "replay"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N windows (debug)")
    ap.add_argument("--only", default="", help="comma-separated window names to process (debug)")
    args = ap.parse_args(argv)

    files = _find_journals(args.journals)
    if args.only:
        want = set(args.only.split(","))
        files = [f for f in files if os.path.basename(f).replace(".jsonl", "") in want]
    if args.limit:
        files = files[:args.limit]

    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    results = []
    if args.workers <= 1:
        for f in files:
            results.append(process_window(f))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_window, f): f for f in files}
            for fut in as_completed(futs):
                results.append(fut.result())
    elapsed = time.time() - t0
    results.sort(key=lambda r: r["window"])

    cutoff = _cutoff_epoch()
    rows, active_windows = aggregate(results, cutoff)

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = os.path.join(args.out, f"maker_flip_{ts}.csv")
    md_path = os.path.join(args.out, f"maker_flip_{ts}.md")
    write_per_window_csv(results, csv_path)
    # raw results for cheap re-aggregation without reprocessing 14 GB of journals
    import pickle
    with open(os.path.join(args.out, f"results_{ts}.pkl"), "wb") as fh:
        pickle.dump(results, fh)

    # selfcheck summary
    processed = [r for r in results if not r.get("skip")]
    skipped = [r for r in results if r.get("skip")]
    flagged = [r for r in processed if r.get("recon") and r["recon"]["rate"] is not None
               and r["recon"]["rate"] < 0.99]

    md = []
    md.append(f"# Maker-flip backtest — {ts}\n")
    md.append(f"- journals: `{args.journals}`  windows found: {len(files)}  "
              f"processed: {len(processed)}  skipped: {len(skipped)}  workers: {args.workers}  "
              f"runtime: {elapsed:.1f}s\n")
    md.append(f"- TRAIN cutoff: close_time < {TRAIN_CUTOFF_ISO}\n")
    if skipped:
        md.append("\n## Skipped windows\n")
        for r in skipped:
            md.append(f"- {r['window']}: {r['skip_reason']}")
    md.append("\n## Selfcheck (trade<->delta reconciliation, receipt lag)\n")
    md.append("| window | split | trades | matched | rate | deltas | lag_p50_ms | lag_p99_ms |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in processed:
        rc = r.get("recon")
        if not rc:
            continue
        split = _split_label(r.get("close_epoch"), cutoff)
        md.append(f"| {r['window']} | {split} | {rc['n_trades']} | {rc['n_matched']} | "
                  f"{_fmt(rc['rate'],4)} | {rc['n_deltas']} | {_fmt(rc['lag_p50'],1)} | "
                  f"{_fmt(rc['lag_p99'],1)} |")
    md.append(f"\nWindows flagged (< 99% reconciliation): {len(flagged)}"
              + ("" if not flagged else " -> " + ", ".join(r['window'] for r in flagged)))

    md.append("\n## Aggregate\n")
    md.append(render_markdown(rows, active_windows))

    # sanity
    san = next((r["sanity"] for r in results if r.get("sanity")), None)
    md.append("\n## 05:00Z sanity anchor\n")
    if san:
        md.append(f"- fire C={san['fire_C']}  t_minus_s={san['fire_t_minus_s']}  "
                  f"fire_engine_ms={_fmt(san['fire_ms'],0)}")
        md.append(f"- H yes-bid 0.54 change-log length: {san['changelog_len']}")
        cf = san["closest_to_fire"]
        if cf:
            md.append(f"- 1->0 vanish nearest fire: before ts={cf[0]} size={cf[1]/100.0}c -> "
                      f"after ts={cf[2]} size={(cf[3] or 0)/100.0}c  (dt={cf[4]}ms)")
        md.append(f"- first transitions: {san['transitions'][:6]}")
    else:
        md.append("- sanity window not in this run")

    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print(f"WROTE {csv_path}")
    print(f"WROTE {md_path}")
    print(f"runtime={elapsed:.1f}s processed={len(processed)} skipped={len(skipped)} "
          f"flagged={len(flagged)}")
    return csv_path, md_path


if __name__ == "__main__":
    main()
