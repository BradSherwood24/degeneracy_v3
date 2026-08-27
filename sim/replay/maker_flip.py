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
only the scalars the policy needs: no_ask_H, yes_ask_L (=> C, target prices, chase asks), the
depth resting at our two target bid levels (=> queue_ahead), and the depth at each leg's best ask
(=> chase size). Trades are kept as light tuples. The policy configs
(rest_mode x theta x fill_rule) then replay those events in memory with no re-folding.

rest_mode (spec) — three independent state machines over the same folded event stream:
  * ``cancel``  — v1: rest both bids while C(t) <= theta, cancel both the instant C(t) > theta.
  * ``leave``   — place both bids ONCE at the first C(t) <= theta frame; never cancel, never
    re-quote (C<=1.00 "episodes" are ms-long flickers; a left-resting bid still catches a later
    through-print — 844/1031 episodes, median 6.2 s).
  * ``requote`` — place once like ``leave``, then track the latest crossing's ask-1c (replace a
    leg only when C<=theta and its target price moved); never cancel on C > theta.
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
THETAS = (1.00, 0.99, 0.98)
DELTAS = (0.10, 0.15, 0.30, 0.50, 1.00)
FILL_RULES = ("strict", "lenient")
MAKER_MULTS = (0.25, 0.0)
REST_MODES = ("cancel", "leave", "requote")
# Single active period only: every journal records < WINDOW_S seconds before close (largest
# observed pre-close span ~886 s < 900 s), so t_ready_ms >= close_ms - WINDOW_S*1000 always
# and the old "full" vs "last_window" periods coincide. process_window asserts this per window
# (out["period_identical"]) and main reports whether the identity held for all active windows.

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

    events = []            # journal order:
    #   ('B', ts, idx, no_ask_H, yes_ask_L, qH, qL, dqH, dqL, aszH, aszL)  book frame
    #   ('T', ts, idx, leg('H'/'L'), taker, yes_price_mils, count)         trade print
    # aszH/aszL = size (hundredths) resting at leg H's NO ask / leg L's YES ask (chase depth).
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
        aszH = tH.no_ask_sz if (tH and tH.no_ask is not None) else 0    # depth at H's NO ask
        aszL = tL.yes_ask_sz if (tL and tL.yes_ask is not None) else 0  # depth at L's YES ask
        qH = (no_ask_H - 10) if no_ask_H is not None else None
        qL = (yes_ask_L - 10) if yes_ask_L is not None else None
        dqH = wb.size_at(H, "no", qH) if qH is not None else 0     # our-side depth at target
        dqL = wb.size_at(L, "yes", qL) if qL is not None else 0
        return no_ask_H, yes_ask_L, qH, qL, dqH, dqL, aszH, aszL

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
    ask_Hsz = []
    ask_Lsz = []
    tl = sorted((e for e in events if e[0] == "B" and e[1] is not None),
                key=lambda e: e[1])
    for e in tl:
        ask_ts.append(e[1])
        ask_H.append(e[3])    # no_ask_H mils (or None)
        ask_L.append(e[4])    # yes_ask_L mils (or None)
        ask_Hsz.append(e[9])  # depth (hundredths) at H's NO ask
        ask_Lsz.append(e[10]) # depth (hundredths) at L's YES ask
    ask_timeline = (ask_ts, ask_H, ask_L, ask_Hsz, ask_Lsz)

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

def _tl_index(ask_ts, t_ms):
    """Index of the last book frame with ts <= t_ms (-1 if none)."""
    lo, hi = 0, len(ask_ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ask_ts[mid] <= t_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _ask_at(ask_timeline, leg, t_ms):
    """Best ask (mils) for leg ('H'/'L') as of the last book frame with ts <= t_ms; None if absent."""
    ask_ts, ask_H, ask_L = ask_timeline[0], ask_timeline[1], ask_timeline[2]
    if not ask_ts or t_ms is None:
        return None
    i = _tl_index(ask_ts, t_ms)
    if i < 0:
        return None
    return (ask_H if leg == "H" else ask_L)[i]


def _asksz_at(ask_timeline, leg, t_ms):
    """Depth (hundredths) resting at leg's best ask as of the last frame with ts <= t_ms.

    Returns None if the timeline carries no size columns (short synthetic timelines) or if
    there is no frame at/ before t_ms."""
    if len(ask_timeline) < 5:
        return None
    ask_ts = ask_timeline[0]
    if not ask_ts or t_ms is None:
        return None
    i = _tl_index(ask_ts, t_ms)
    if i < 0:
        return None
    return (ask_timeline[3] if leg == "H" else ask_timeline[4])[i]


def _run_policy(events, theta, fill_rule, t_start_ms, t_end_ms, rest_mode="cancel"):
    """Replay one policy config over the compact event stream, for one rest_mode.

    rest_mode:
      * ``cancel``  — v1: while C(t) <= theta rest both bids at ask-0.01 (replace on price
        change, keep queue on no change); cancel BOTH the instant C(t) > theta.
      * ``leave``   — on the FIRST frame where C(t) <= theta (both legs quotable) place both
        bids at ask-0.01 once; never cancel, never re-quote. ONE placement per window.
      * ``requote`` — place both once like ``leave``; thereafter whenever C(t) <= theta and a
        leg's target (ask-0.01) differs from its resting price, replace that leg (queue reset);
        never cancel on C > theta. Bids track the most recent crossing's ask-1c.

    Returns dict with: first (leg, t_A, q_A, qB_rest_at_tA or None), other (t_B, qB) or None,
    first_place_ts / first_place_C / first_ttf_ms (placement time, taker-model C at placement,
    and time placement->fill for leg A), and rest stats (episodes, rest_time_ms, replaces,
    cancels, any_fill).
    """
    lenient = (fill_rule == "lenient")
    restH = restL = None                 # our resting bid price (mils) or None
    pH = pL = None                       # placement (ts, idx)
    qaH = qaL = 0.0                      # queue_ahead (contracts) captured at placement
    cumH = cumL = 0.0                    # lenient cumulative print counts since placement
    place_ts = {"H": None, "L": None}    # engine ts of each leg's current placement
    place_C = {"H": None, "L": None}     # taker-model C at each leg's current placement
    placed_once = False                  # leave/requote: initial both-bid placement done

    first = None                         # (leg, t_A, q_A, qB_rest_at_tA)
    first_place_ts = None                # place_ts of leg A at fill
    first_place_C = None                 # place_C of leg A at fill
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

    def place(leg, price, ts, idx, qa, C):
        """(Re)place one leg's bid. Episode counted on a fresh placement (rest was None);
        the caller counts a replace when it overwrites a live order at a new price."""
        nonlocal restH, restL, pH, pL, qaH, qaL, cumH, cumL
        if leg == "H":
            if restH is None:
                epis["H"] += 1
                rest_since["H"] = ts
            restH, pH, qaH, cumH = price, (ts, idx), qa, 0.0
        else:
            if restL is None:
                epis["L"] += 1
                rest_since["L"] = ts
            restL, pL, qaL, cumL = price, (ts, idx), qa, 0.0
        place_ts[leg] = ts
        place_C[leg] = C

    def cancel_leg(leg, ts):
        nonlocal restH, restL, cancels
        if leg == "H" and restH is not None:
            cancels += 1
            stop_rest("H", ts)
            restH = None
        elif leg == "L" and restL is not None:
            cancels += 1
            stop_rest("L", ts)
            restL = None

    def register(leg, ts, price):
        nonlocal first, other, filledH, filledL, first_place_ts, first_place_C
        if leg == "H":
            if filledH:
                return
            filledH = True
        else:
            if filledL:
                return
            filledL = True
        stop_rest(leg, ts)
        if first is None:
            qB_rest = restL if leg == "H" else restH   # leg B's resting price at t_A (or None)
            first = (leg, ts, price, qB_rest)
            first_place_ts = place_ts[leg]
            first_place_C = place_C[leg]
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
        idx = ev[2]
        no_ask_H, yes_ask_L, qH, qL, dqH, dqL = ev[3:9]
        if no_ask_H is None or yes_ask_L is None:
            C = float("inf")
        else:
            C = _dollars(no_ask_H) + _fee_mils(no_ask_H) + _dollars(yes_ask_L) + _fee_mils(yes_ask_L)
        crossing = (C <= theta + _EPS and qH is not None and qL is not None and qH > 0 and qL > 0)

        if rest_mode == "cancel":
            if crossing:
                if restH != qH:
                    if restH is not None:
                        replaces += 1
                    place("H", qH, ts, idx, dqH / 100.0, C)
                if restL != qL:
                    if restL is not None:
                        replaces += 1
                    place("L", qL, ts, idx, dqL / 100.0, C)
            else:
                cancel_leg("H", ts)
                cancel_leg("L", ts)

        elif rest_mode == "leave":
            if not placed_once and crossing:
                place("H", qH, ts, idx, dqH / 100.0, C)
                place("L", qL, ts, idx, dqL / 100.0, C)
                placed_once = True
            # after placement: never cancel, never re-quote.

        elif rest_mode == "requote":
            if not placed_once:
                if crossing:
                    place("H", qH, ts, idx, dqH / 100.0, C)
                    place("L", qL, ts, idx, dqL / 100.0, C)
                    placed_once = True
            elif C <= theta + _EPS:
                # track the latest crossing's ask-1c per leg; never cancel on C > theta.
                if qH is not None and qH > 0 and restH is not None and restH != qH:
                    replaces += 1
                    place("H", qH, ts, idx, dqH / 100.0, C)
                if qL is not None and qL > 0 and restL is not None and restL != qL:
                    replaces += 1
                    place("L", qL, ts, idx, dqL / 100.0, C)
        else:
            raise ValueError(f"unknown rest_mode {rest_mode!r}")

    # close any still-open rest intervals at t_end
    stop_rest("H", t_end_ms)
    stop_rest("L", t_end_ms)

    return {
        "first": first,
        "other": other,
        "first_place_ts": first_place_ts,
        "first_place_C": first_place_C,
        "first_ttf_ms": ((first[1] - first_place_ts)
                         if (first is not None and first_place_ts is not None) else None),
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
               "ask_B_tA": ask_B_tA, "ask_B_target": None, "ask_B_sz": None,
               "chase_gap": None, "chase_gap_vs_rest": None, "costs": {}}
        if both_maker:
            qB = other[1]
            for mm in MAKER_MULTS:
                rec["costs"][mm] = (_dollars(q_A) + mm * _fee_mils(q_A)
                                    + _dollars(qB) + mm * _fee_mils(qB))
        else:
            ask_B_t = _ask_at(ask_timeline, legB, t_target)
            rec["ask_B_target"] = ask_B_t
            rec["ask_B_sz"] = _asksz_at(ask_timeline, legB, t_target)   # chase depth (record-only)
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
#  Worker: parse one window and run all rest_mode x theta x fill_rule configs; compact result.
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
    t_start = t_ready_ms
    active = t_start is not None and t_end is not None and t_start <= t_end
    # Single active period: full == last_window here because every journal records < WINDOW_S s
    # before close. Record whether the identity holds so main can assert it across the corpus.
    out["period_identical"] = (t_start is not None
                               and t_start >= close_ms - WINDOW_S * 1000.0)

    for rest_mode in REST_MODES:
        for theta in THETAS:
            for fr in FILL_RULES:
                cfg = {"rest_mode": rest_mode, "theta": theta, "fill_rule": fr,
                       "active": active, "first": None, "rest": None, "outcomes": []}
                if active:
                    pol = _run_policy(events, theta, fr, t_start, t_end, rest_mode=rest_mode)
                    cfg["rest"] = {k: pol[k] for k in
                                   ("episodes", "rest_time_ms", "replaces", "cancels", "any_fill")}
                    if pol["first"] is not None:
                        legA, t_A, q_A, qB_rest = pol["first"]
                        cfg["first"] = {
                            "legA": legA, "t_A_to_close_s": (close_ms - t_A) / 1000.0,
                            "q_A": _dollars(q_A),
                            "C_at_tA": _C_at(ask_timeline, t_A),          # taker-model C at fill
                            "C_at_place": pol["first_place_C"],           # C at placement
                            "time_to_fill_s": (pol["first_ttf_ms"] / 1000.0
                                               if pol["first_ttf_ms"] is not None else None),
                        }
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
    """Build the aggregate table.

    Returns (rows, active_windows) where rows is keyed
    ``(split, rest_mode, fill_rule, mult, theta, delta)`` and active_windows is keyed
    ``(split, rest_mode)`` (identical across rest_modes, but kept split-aware).
    """
    active_win_sets = {}
    ttf = {}     # (split, rest_mode, fill_rule, mult, theta) -> [time_to_fill_s per window fill]
    agg = {}
    for r in results:
        if r.get("skip") or r.get("no_active"):
            continue
        split = _split_label(r.get("close_epoch"), cutoff)
        for cfg in r["configs"]:
            if not cfg["active"]:
                continue
            rm = cfg["rest_mode"]
            active_win_sets.setdefault((split, rm), set()).add(r["window"])
            for mm in MAKER_MULTS:
                if cfg.get("first") is not None:
                    t2f = cfg["first"].get("time_to_fill_s")
                    if t2f is not None:
                        ttf.setdefault((split, rm, cfg["fill_rule"], mm, cfg["theta"]),
                                       []).append(t2f)
                for rec in cfg["outcomes"]:
                    k = (split, rm, cfg["fill_rule"], mm, cfg["theta"], rec["delta"])
                    a = agg.setdefault(k, {
                        "n_first": 0, "n_both": 0, "n_unhedged": 0, "costs": [], "chase_gaps": [],
                        "lt100": 0, "lt102": 0, "gt105": 0, "n_cost": 0,
                    })
                    # outcomes only exist when this config had a first fill; one window ->
                    # one increment per (rest_mode,theta,fill_rule,mult,delta) key.
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
                        if c > 1.05:
                            a["gt105"] += 1
                    if (not rec["both_maker"]) and rec["chase_gap"] is not None:
                        a["chase_gaps"].append(rec["chase_gap"])
    # finalize
    active_windows = {k: len(v) for k, v in active_win_sets.items()}
    rows = {}
    for k, a in agg.items():
        split, rm = k[0], k[1]
        naw = active_windows.get((split, rm), 0)
        nf = a["n_first"]
        med_ttf = _pctl(ttf.get((split, rm, k[2], k[3], k[4]), []), 0.50)
        rows[k] = {
            "n_windows_active": naw,
            "n_first_fills": nf,
            "n_both_maker": a["n_both"],
            # Conditioned on a first fill: an unhedged outcome (no completing ask) is a loss,
            # so it counts against P(cost<X) — denominator is n_first, not just costed outcomes.
            "p_cost_lt100": (a["lt100"] / nf) if nf else None,
            "p_cost_lt102": (a["lt102"] / nf) if nf else None,
            "p_cost_gt105": (a["gt105"] / nf) if nf else None,
            "mean_cost": (statistics.fmean(a["costs"]) if a["costs"] else None),
            "median_cost": (statistics.median(a["costs"]) if a["costs"] else None),
            "chase_gap_p10": _pctl(a["chase_gaps"], 0.10),
            "chase_gap_p50": _pctl(a["chase_gaps"], 0.50),
            "chase_gap_p90": _pctl(a["chase_gaps"], 0.90),
            "median_ttf_s": med_ttf,
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


def render_markdown(rows, active_windows, splits=("TRAIN", "HOLDOUT"),
                    thetas=THETAS, deltas=DELTAS):
    lines = []
    for split in splits:
        for rm in REST_MODES:
            naw = active_windows.get((split, rm), 0)
            lines.append(f"\n### {split} — rest_mode={rm}  (n_windows_active={naw})\n")
            header = ("| fill | mult | θ | Δ | n_first | n_both | P<1.00 | P<1.02 | P>1.05 | "
                      "mean | median | gap_p10 | gap_p50 | gap_p90 | med_ttf_s | unhedged |")
            lines.append(header)
            lines.append("|" + "---|" * 16)
            for fr in FILL_RULES:
                for mm in MAKER_MULTS:
                    for theta in thetas:
                        for delta in deltas:
                            k = (split, rm, fr, mm, theta, delta)
                            if k not in rows:
                                continue
                            r = rows[k]
                            lines.append(
                                f"| {fr} | {mm} | {theta:.2f} | {delta:.2f} | "
                                f"{r['n_first_fills']} | {r['n_both_maker']} | "
                                f"{_fmt(r['p_cost_lt100'],3)} | {_fmt(r['p_cost_lt102'],3)} | "
                                f"{_fmt(r['p_cost_gt105'],3)} | "
                                f"{_fmt(r['mean_cost'])} | {_fmt(r['median_cost'])} | "
                                f"{_fmt(r['chase_gap_p10'])} | {_fmt(r['chase_gap_p50'])} | "
                                f"{_fmt(r['chase_gap_p90'])} | {_fmt(r['median_ttf_s'],2)} | "
                                f"{r['n_unhedged']} |")
    return "\n".join(lines)


def render_per_fill(results, cutoff, rest_mode, theta=1.00, fill_rule="strict",
                    delta=0.15, mult=0.25):
    """Every first-fill for one (rest_mode, θ, fill_rule, Δ, mult), as a markdown table."""
    lines = [f"\n### Per-fill — rest_mode={rest_mode} θ={theta:.2f} {fill_rule.upper()} "
             f"Δ={delta:.2f} mult={mult}\n",
             "| window | split | leg | rest_q | ttf_s | C_place | C_fill | ask_B_fill | cost |",
             "|" + "---|" * 9]
    n = 0
    for r in sorted(results, key=lambda x: x["window"]):
        if r.get("skip") or r.get("no_active"):
            continue
        split = _split_label(r.get("close_epoch"), cutoff)
        for cfg in r["configs"]:
            if (cfg.get("rest_mode") != rest_mode or cfg["theta"] != theta
                    or cfg["fill_rule"] != fill_rule or not cfg.get("first")):
                continue
            rec = next((o for o in cfg["outcomes"] if abs(o["delta"] - delta) < 1e-9), None)
            if rec is None:
                continue
            f = cfg["first"]
            ask_b = rec["ask_B_tA"]
            lines.append(
                f"| {r['window']} | {split} | {f['legA']} | {_fmt(f['q_A'],3)} | "
                f"{_fmt(f['time_to_fill_s'],2)} | {_fmt(f['C_at_place'],4)} | "
                f"{_fmt(f['C_at_tA'],4)} | {_dollars(ask_b) if ask_b is not None else '-'} | "
                f"{_fmt(rec['costs'].get(mult),4)} |")
            n += 1
    if n == 0:
        lines.append("| _(no fills)_ | | | | | | | | |")
    return "\n".join(lines)


def write_per_window_csv(results, path):
    cols = ["window", "split", "close_time", "high_ticker", "low_ticker", "leg_source",
            "rest_mode", "theta", "fill_rule", "maker_mult", "active", "any_fill",
            "episodes", "rest_time_ms", "replaces", "cancels",
            "first_leg", "t_A_to_close_s", "q_A", "C_at_tA", "C_at_place", "time_to_fill_s",
            "delta", "both_maker", "unhedged", "cost", "chase_gap", "chase_gap_vs_rest",
            "ask_B_tA", "ask_B_target", "ask_B_sz"]
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
                        r["leg_source"], cfg["rest_mode"], cfg["theta"], cfg["fill_rule"]]
                for mm in MAKER_MULTS:
                    common = base + [mm, cfg["active"], rest.get("any_fill"),
                                     rest.get("episodes"), rest.get("rest_time_ms"),
                                     rest.get("replaces"), rest.get("cancels"),
                                     (first or {}).get("legA"), (first or {}).get("t_A_to_close_s"),
                                     (first or {}).get("q_A"), (first or {}).get("C_at_tA"),
                                     (first or {}).get("C_at_place"),
                                     (first or {}).get("time_to_fill_s")]
                    if not cfg["outcomes"]:
                        w.writerow(common + ["", "", "", "", "", "", "", "", ""])
                    else:
                        for rec in cfg["outcomes"]:
                            w.writerow(common + [rec["delta"], rec["both_maker"], rec["unhedged"],
                                                 _fmt(rec["costs"].get(mm)) if rec["costs"].get(mm) is not None else "",
                                                 _fmt(rec["chase_gap"]) if rec["chase_gap"] is not None else "",
                                                 _fmt(rec["chase_gap_vs_rest"]) if rec["chase_gap_vs_rest"] is not None else "",
                                                 rec["ask_B_tA"], rec["ask_B_target"], rec["ask_B_sz"]])


def _find_journals(journal_dir):
    import glob
    files = sorted(glob.glob(os.path.join(journal_dir, "2026*.jsonl")))
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maker-flip backtest over pilot WS journals.")
    ap.add_argument("--journals", default=os.path.join(_REPO_ROOT, "pilot", "journals"))
    ap.add_argument("--out", default=os.path.join(_SIM_DIR, "out", "replay"))
    ap.add_argument("--workers", type=int, default=8)
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
    # period identity: full == last_window whenever every active window records < WINDOW_S s
    active = [r for r in processed if not r.get("no_active")]
    non_identical = [r["window"] for r in active if not r.get("period_identical")]
    md.append(f"- Period dimension DROPPED (reported once): `full` == `last_window` for all "
              f"{len(active)} active windows because each records < WINDOW_S={int(WINDOW_S)} s "
              f"before close (t_ready >= close - WINDOW_S). "
              f"Windows where identity FAILS: {len(non_identical)}"
              + ("" if not non_identical else " -> " + ", ".join(non_identical)) + "\n")
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

    # per-fill lists for the pinned config (θ=1.00 STRICT Δ=0.15 mult=0.25), leave + requote
    md.append("\n## Per-fill detail (θ=1.00 STRICT Δ=0.15 mult=0.25)\n")
    for rm in ("leave", "requote"):
        md.append(render_per_fill(results, cutoff, rm, theta=1.00, fill_rule="strict",
                                  delta=0.15, mult=0.25))

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
