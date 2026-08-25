"""Rung 1 census builder.

One row per top-of-hour close time T over a requested TRAIN date range. Pair
construction, A2.2 epsilon exclusion, outcomes solely from ``result`` fields with the
A2.3 ``expiration_value`` integrity cross-check, economics per section 5 (BASE and
WORST columns, T-5min primary snapshot, descriptive T-13/T-10/T-2), sigma-hat per
A2.1/A2.4, and NO-PAIR / exclusion receipts per sections 2/7.

Every value the spec pins is enforced here with a refusal, not a comment:
  * 15M market with no floor_strike -> EXCL_NO_ANCHOR       (A3.1)
  * epsilon  G < $0.01           -> EXCL_G_LT_EPS          (A2.2)
  * empty expiration_value on a leg -> EXCL_EV_MISSING      (A3.5)
  * sigma-hat: 9 anchors/8 diffs -> EXCL_SIGMA_HEAD/GAP if <9 (A2.1/A3.4)
  * sigma-hat == 0 exactly       -> EXCL_SIGMA_ZERO        (A2.4)
  * any NaN / integrity mismatch / impossible outcome -> hard fail (raise)

Amendment A3 (frozen 2026-08-19, adjudicating adversarial reviews A & B):
  * A3.1 strike-less "up/down (Target price: TBD)" 15M products carry no floor_strike:
    they are SKIPPED in the anchor tape and route a grid hour to EXCL_NO_ANCHOR when no
    strike-bearing 15M market occupies that hour.
  * A3.2 the sigma-hat window (9 anchors at T, T-15m, ..., T-120m, INCLUDING A(T)) is now
    RATIFIED by the commission, not a builder choice.
  * A3.3 pair inclusion is decided ONLY on BASE-close quoting + C_base presence; a missing
    WORST field leaves the row OK with C_worst/payoff_worst blank (fidelity-limited).
  * A3.4 insufficient trailing tape splits into EXCL_SIGMA_HEAD (genuine head-of-corpus)
    vs EXCL_SIGMA_GAP (mid-tape gap).
  * A3.5 an EMPTY expiration_value on either leg -> EXCL_EV_MISSING (fail-closed);
    a present-but-UNEQUAL print (compared as Decimal) remains a hard fail; the result
    recompute check runs only when the print is present.

Confessed judgment calls (no spec pin exists for these; see build/fix report):
  * sigma-hat uses the SAMPLE standard deviation (ddof=1, statistics.stdev).
  * G is rounded to cents (both inputs are cent-precision) before the epsilon test.
  * economics use Decimal to reproduce the audited fee/C exactly.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import statistics
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from loader import (
    IntegrityError,
    SealError,  # noqa: F401  (re-exported for callers/tests)
    TRAIN_START,
    TRAIN_END,
    assign_day,
    close_epoch,
    load_candles,
    load_markets,
)

# ---- Frozen constants (spec-pinned) ---------------------------------------
EPSILON = Decimal("0.01")          # A2.2 degenerate-corridor cut
SIGMA_ANCHORS = 9                  # A2.1 trailing anchors (-> 8 diffs)
SIGMA_DIFFS = SIGMA_ANCHORS - 1
ANCHOR_STEP = 900                  # 15 minutes, seconds
FEE_RATE = Decimal("0.07")         # audited taker-fee coefficient
SNAPSHOTS = {                      # label -> seconds before T
    "t13": 780,
    "t10": 600,
    "t5": 300,   # PRIMARY
    "t2": 120,
}
PRIMARY = "t5"

_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


# ---------------------------------------------------------------------------
# Pure, unit-testable primitives
# ---------------------------------------------------------------------------
def fee(price) -> Decimal:
    """Taker fee per leg per contract: ceil(0.07*p*(1-p)*10000)/10000 dollars.

    Audited golden literals (2026-08-18 fills):
      0.57->0.0172, 0.46->0.0174, 0.24->0.0128, 0.11->0.0069.
    """
    p = Decimal(str(price)) if not isinstance(price, Decimal) else price
    raw = FEE_RATE * p * (Decimal(1) - p) * Decimal(10000)
    return Decimal(math.ceil(raw)) / Decimal(10000)


def _ev_empty(v) -> bool:
    """A3.5: True when an ``expiration_value`` is absent/None/blank (fail-closed EXCL),
    as opposed to a present print (which is checked for cross-leg equality and recompute)."""
    return v is None or str(v).strip() == ""


def recompute_result(strike_type: str, expiration_value, floor_strike) -> str:
    """Recompute the settled result from the print per each market's strike_type.

    A2.3: 15M is ``greater_or_equal`` (yes iff print >= strike);
          1H is ``greater``          (yes iff print >  strike).
    """
    ev = Decimal(str(expiration_value))
    ks = Decimal(str(floor_strike))
    if strike_type == "greater_or_equal":
        return "yes" if ev >= ks else "no"
    if strike_type == "greater":
        return "yes" if ev > ks else "no"
    raise IntegrityError(f"unexpected strike_type {strike_type!r}")


def classify_outcome(h_result: str, l_result: str) -> str:
    """PIN / ESCAPE from the two result fields (A2.3).

    We buy YES on the H-line market and NO on the L-line market.
      * PIN    <=> H:no  AND L:yes  (print inside the corridor; both legs lose)
      * ESCAPE <=> exactly one leg pays
      * impossible (H:yes AND L:no -> above H AND below L) -> hard fail
    """
    h_pays = (h_result == "yes")   # YES-on-H pays iff H settled yes
    l_pays = (l_result == "no")    # NO-on-L pays iff L settled no
    if h_pays and l_pays:
        raise IntegrityError(
            "impossible outcome: H:yes AND L:no (print above H and below L)"
        )
    if (not h_pays) and (not l_pays):
        return "PIN"
    return "ESCAPE"


def sigma_hat(anchor_by_epoch: Dict[int, float], T: int) -> float:
    """Sample std (ddof=1) of the 8 trailing anchor-to-anchor diffs on a continuous
    tape of the 9 anchors at T, T-900, ..., T-7200. Raises InsufficientTape if any of
    the 9 contiguous anchors is missing (A2.1).
    """
    anchors = []
    for k in range(SIGMA_ANCHORS):
        ep = T - k * ANCHOR_STEP
        if ep not in anchor_by_epoch:
            raise InsufficientTape(T)
        anchors.append(anchor_by_epoch[ep])
    # anchors[0] = A(T) (most recent) ... anchors[8] = oldest
    diffs = [anchors[i] - anchors[i + 1] for i in range(SIGMA_DIFFS)]
    s = statistics.stdev(diffs)  # ddof=1 (confessed choice)
    if math.isnan(s) or math.isinf(s):
        raise IntegrityError("sigma-hat is NaN/inf")
    return s


class InsufficientTape(Exception):
    def __init__(self, T):
        super().__init__(f"insufficient sigma tape for T={T}")
        self.T = T


def _cand(v) -> Optional[Decimal]:
    """Parse a candle price string to Decimal; None if absent/empty."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    d = Decimal(s)
    if d.is_nan():
        raise IntegrityError("NaN candle price")
    return d


def _candle_at(candle_rec: Optional[dict], end_ts: int) -> Optional[dict]:
    if candle_rec is None:
        return None
    for c in candle_rec.get("candlesticks", []):
        if c.get("end_period_ts") == end_ts:
            return c
    return None


def leg_prices(h_candle: Optional[dict], l_candle: Optional[dict]):
    """Return (yes_ask_base, yes_ask_worst, no_ask_base, no_ask_worst) as Decimals or
    None. YES leg is the H market (yes_ask); NO leg is the L market (1 - yes_bid).
    """
    ya_b = _cand(h_candle["yes_ask"].get("close_dollars")) if h_candle else None
    ya_w = _cand(h_candle["yes_ask"].get("high_dollars")) if h_candle else None
    yb_close = _cand(l_candle["yes_bid"].get("close_dollars")) if l_candle else None
    yb_low = _cand(l_candle["yes_bid"].get("low_dollars")) if l_candle else None
    na_b = (Decimal(1) - yb_close) if yb_close is not None else None
    na_w = (Decimal(1) - yb_low) if yb_low is not None else None
    return ya_b, ya_w, na_b, na_w


def cost_C(yes_ask: Decimal, no_ask: Decimal) -> Decimal:
    """C = legA + fee(legA) + legB + fee(legB) on each leg's own traded price."""
    return yes_ask + fee(yes_ask) + no_ask + fee(no_ask)


def hole_G(A, K) -> Decimal:
    """G = |K - A|, rounded to cents (both inputs are cent-precision). Returned as a
    Decimal so the A2.2 ``G < $0.01`` test is exact."""
    return Decimal(str(round(abs(float(K) - float(A)), 2)))


def is_degenerate(A, K) -> bool:
    """A2.2: pairs with G < $0.01 (anchor on the threshold, incl. exact equality) are
    excluded. This is the pure predicate build_census enforces with a receipt."""
    return hole_G(A, K) < EPSILON


def quoted_base(ya_b: Optional[Decimal], na_b: Optional[Decimal]) -> bool:
    """A2.5: quoting test on BASE close only. Both asks present and < $1."""
    if ya_b is None or na_b is None:
        return False
    return (Decimal(0) < ya_b < Decimal(1)) and (Decimal(0) < na_b < Decimal(1))


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _is_top_of_hour(close_time: str) -> bool:
    ep = close_epoch(close_time)
    return ep % 3600 == 0


def _iso(ep: int) -> str:
    return _dt.datetime.fromtimestamp(ep, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_census(dates: List[str], acknowledge_sealed_read: bool = False,
                 data_root: Optional[str] = None):
    """Build census rows + receipt over ``dates`` (a list of YYYY-MM-DD strings).

    Returns (rows, receipt). Never silently drops an hour: every top-of-hour T yields
    exactly one row whose ``status`` is OK / NO_PAIR / EXCL_*.

    ``data_root`` overrides the historical-data root (tests inject synthetic corpora);
    None uses the loader default. Real runs never pass it.
    """
    kw = {} if data_root is None else {"data_root": data_root}
    m15, sh_m15 = load_markets("15-minute", dates, acknowledge_sealed_read, **kw)
    m1h, sh_m1h = load_markets("1-hour", dates, acknowledge_sealed_read, **kw)
    c15, sh_c15 = load_candles("15-minute", dates, acknowledge_sealed_read, **kw)
    c1h, sh_c1h = load_candles("1-hour", dates, acknowledge_sealed_read, **kw)
    shas = {**sh_m15, **sh_m1h, **sh_c15, **sh_c1h}

    dateset = set(dates)

    # Continuous 15M anchor tape (all anchors across the loaded range).
    # A3.1: the strike-less "up/down (Target price: TBD)" product carries no floor_strike
    # (the key is entirely absent). It is NOT an anchor -> skip it here. A present-but-NaN
    # floor_strike is genuine corruption of a real strike and still hard-fails.
    anchor_by_epoch: Dict[int, float] = {}
    for r in m15:
        a = r.get("floor_strike")
        if a is None:
            continue                                   # A3.1: strike-less product, no anchor
        if isinstance(a, float) and math.isnan(a):
            raise IntegrityError(f"NaN anchor for {r['ticker']}")
        ep = close_epoch(r["close_time"])
        anchor_by_epoch[ep] = float(a)

    # A3.4: the genuine head-of-corpus anchor. An insufficient-tape exclusion whose oldest
    # required anchor (T-7200) precedes this is a head-of-corpus gap (EXCL_SIGMA_HEAD);
    # any other insufficient tape is a mid-tape gap (EXCL_SIGMA_GAP). For the frozen TRAIN
    # run (2026-06-11..08-01) this makes HEAD exactly the 2026-06-11 opening hours.
    min_anchor_ep = min(anchor_by_epoch) if anchor_by_epoch else None

    # 1H markets grouped by close_time (exact string), only for requested days.
    h1_by_ct: Dict[str, List[dict]] = {}
    for r in m1h:
        if r["_assigned_day"] in dateset:
            h1_by_ct.setdefault(r["close_time"], []).append(r)

    # 15M markets keyed by close_time (unique per 15-min window). A3.1: if both a
    # strike-bearing market and a strike-less "up/down" product close at the same time,
    # prefer the strike-bearing one so the hour is only EXCL_NO_ANCHOR when NO
    # strike-bearing 15M market occupies it. (In the observed corpus each close_time has
    # a single 15M record; the preference is defensive.)
    m15_by_ct: Dict[str, dict] = {}
    for r in m15:
        ct_key = r["close_time"]
        existing = m15_by_ct.get(ct_key)
        if existing is None:
            m15_by_ct[ct_key] = r
        elif existing.get("floor_strike") is None and r.get("floor_strike") is not None:
            m15_by_ct[ct_key] = r

    # Census hours = the FIXED 24-hour top-of-hour grid of each requested day
    # (T = D 01:00 .. D 23:00, plus D+1 00:00). Enumerating the grid rather than the
    # observed 15M markets guarantees one accounted row per hour: a missing 15M leg
    # (e.g. the 2026-07-16 06:45->09:15 tape gap) is receipted, not silently dropped.
    grid: List[Tuple[str, int, str]] = []
    for d in sorted(dateset):
        base = close_epoch(d + "T00:00:00Z")
        for h in range(1, 25):
            T = base + h * 3600
            grid.append((d, T, _iso(T)))
    grid.sort(key=lambda x: x[1])

    rows: List[dict] = []
    inventory: Dict[str, List[str]] = {}

    def excl(reason, day, ct):
        inventory.setdefault(reason, []).append(f"{day} {ct}")

    for day, T, ct in grid:
        row = {
            "date": day, "close_time": ct, "anchor_A": "",
            "threshold_K": "", "G": "", "orientation": "", "sigma_hat": "",
            "g_over_sigma": "", "pin_escape": "", "H_result": "", "L_result": "",
            "quoted_base": "",
            "C_t13_base": "", "C_t13_worst": "", "C_t10_base": "", "C_t10_worst": "",
            "C_base": "", "C_worst": "", "C_t2_base": "", "C_t2_worst": "",
            "payoff_base": "", "payoff_worst": "", "status": "",
        }

        m = m15_by_ct.get(ct)
        if m is None:
            row["status"] = "EXCL_NO_15M_LEG"; excl("EXCL_NO_15M_LEG", day, ct)
            rows.append(row); continue
        # A3.1: a 15M market exists but is the strike-less "up/down" product (no anchor).
        a_val = m.get("floor_strike")
        if a_val is None:
            row["status"] = "EXCL_NO_ANCHOR"; excl("EXCL_NO_ANCHOR", day, ct)
            rows.append(row); continue
        A = float(a_val)
        row["anchor_A"] = f"{A:.2f}"

        if m.get("strike_type") != "greater_or_equal":
            raise IntegrityError(f"15M {m['ticker']} strike_type != greater_or_equal")

        # ---- pair construction: nearest 1H strike ----
        cands = h1_by_ct.get(ct, [])
        if not cands:
            row["status"] = "EXCL_NO_1H_LEG"; excl("EXCL_NO_1H_LEG", day, ct)
            rows.append(row); continue
        # nearest strike with explicit tie detection (ties -> exclude, per spec)
        annotated = sorted(((abs(float(r["floor_strike"]) - A), r) for r in cands),
                           key=lambda t: t[0])
        if len(annotated) >= 2 and annotated[0][0] == annotated[1][0]:
            row["status"] = "EXCL_NEAREST_TIE"; excl("EXCL_NEAREST_TIE", day, ct)
            rows.append(row); continue
        h1 = annotated[0][1]
        if h1.get("strike_type") != "greater":
            raise IntegrityError(f"1H {h1['ticker']} strike_type != greater")
        K = float(h1["floor_strike"])
        G_dec = hole_G(A, K)       # cents precision, exact Decimal (confessed)
        G = float(G_dec)
        row["threshold_K"] = f"{K:.2f}"
        row["G"] = f"{G:.2f}"
        orientation = "A_above_K" if A > K else ("K_above_A" if K > A else "EQUAL")
        row["orientation"] = orientation

        # ---- A2.2 epsilon exclusion ----
        if is_degenerate(A, K):
            row["status"] = "EXCL_G_LT_EPS"; excl("EXCL_G_LT_EPS", day, ct)
            rows.append(row); continue

        # ---- sigma-hat (A2.1 / A2.4) ----
        try:
            sig = sigma_hat(anchor_by_epoch, T)
        except InsufficientTape:
            # A3.4: split head-of-corpus vs mid-tape gap. The oldest required anchor is at
            # T - SIGMA_DIFFS*ANCHOR_STEP (T-7200). If it precedes the corpus's earliest
            # anchor, the tape simply does not reach back far enough (HEAD); otherwise the
            # window straddles an interior hole (GAP).
            window_start = T - SIGMA_DIFFS * ANCHOR_STEP
            if min_anchor_ep is not None and window_start < min_anchor_ep:
                reason = "EXCL_SIGMA_HEAD"
            else:
                reason = "EXCL_SIGMA_GAP"
            row["status"] = reason; excl(reason, day, ct)
            rows.append(row); continue
        if sig == 0.0:
            row["status"] = "EXCL_SIGMA_ZERO"; excl("EXCL_SIGMA_ZERO", day, ct)
            rows.append(row); continue
        row["sigma_hat"] = f"{sig:.6f}"
        gos = G / sig
        if math.isnan(gos) or math.isinf(gos):
            raise IntegrityError("g_over_sigma NaN/inf")
        row["g_over_sigma"] = f"{gos:.6f}"

        # ---- H / L lines ----
        # H = higher strike (buy YES), L = lower strike (buy NO).
        if A > K:
            h_mkt, l_mkt = m, h1
            h_series, l_series = "15M", "1H"
        else:
            h_mkt, l_mkt = h1, m
            h_series, l_series = "1H", "15M"
        h_tk, l_tk = h_mkt["ticker"], l_mkt["ticker"]

        # ---- integrity cross-check (A2.3 + A3.5): expiration_value ----
        # A3.5: an EMPTY print on either leg is a data-completeness quirk on a settled
        # market -> EXCL_EV_MISSING (fail-closed, not a crash). A present-but-UNEQUAL print
        # (compared as Decimal, not string, so 64512.33 == 64512.3300) is a genuine
        # cross-leg disagreement -> hard fail. The result recompute runs only when present.
        ev_h_raw = m.get("expiration_value")
        ev_l_raw = h1.get("expiration_value")
        if _ev_empty(ev_h_raw) or _ev_empty(ev_l_raw):
            row["status"] = "EXCL_EV_MISSING"; excl("EXCL_EV_MISSING", day, ct)
            rows.append(row); continue
        if Decimal(str(ev_h_raw)) != Decimal(str(ev_l_raw)):
            raise IntegrityError(
                f"expiration_value mismatch at {ct}: 15M {ev_h_raw!r} vs 1H {ev_l_raw!r}"
            )
        for mk in (m, h1):
            rc = recompute_result(mk["strike_type"], mk["expiration_value"],
                                  mk["floor_strike"])
            if rc != mk["result"]:
                raise IntegrityError(
                    f"result integrity fail {mk['ticker']}: field={mk['result']} "
                    f"recomputed={rc}"
                )
        h_result = h_mkt["result"]
        l_result = l_mkt["result"]
        row["H_result"] = h_result
        row["L_result"] = l_result
        outcome = classify_outcome(h_result, l_result)  # raises on impossible
        row["pin_escape"] = outcome

        # ---- economics: candles for H and L legs ----
        hc = c15.get(h_tk) if h_series == "15M" else c1h.get(h_tk)
        lc = c15.get(l_tk) if l_series == "15M" else c1h.get(l_tk)

        # descriptive + primary snapshots
        primary_C = {}
        for label, off in SNAPSHOTS.items():
            end_ts = T - off
            hcnd = _candle_at(hc, end_ts)
            lcnd = _candle_at(lc, end_ts)
            ya_b, ya_w, na_b, na_w = leg_prices(hcnd, lcnd)
            cb = cw = None
            if ya_b is not None and na_b is not None:
                cb = cost_C(ya_b, na_b)
            if ya_w is not None and na_w is not None:
                cw = cost_C(ya_w, na_w)
            if label == "t13":
                row["C_t13_base"] = f"{cb:.4f}" if cb is not None else ""
                row["C_t13_worst"] = f"{cw:.4f}" if cw is not None else ""
            elif label == "t10":
                row["C_t10_base"] = f"{cb:.4f}" if cb is not None else ""
                row["C_t10_worst"] = f"{cw:.4f}" if cw is not None else ""
            elif label == "t5":
                row["C_base"] = f"{cb:.4f}" if cb is not None else ""
                row["C_worst"] = f"{cw:.4f}" if cw is not None else ""
                primary_C = {"ya_b": ya_b, "na_b": na_b, "cb": cb, "cw": cw}
            elif label == "t2":
                row["C_t2_base"] = f"{cb:.4f}" if cb is not None else ""
                row["C_t2_worst"] = f"{cw:.4f}" if cw is not None else ""

        # ---- quoting / NO-PAIR test on BASE close at T-5 (A2.5 / A3.3) ----
        # A3.3: inclusion is decided ONLY on BASE-close quoting + C_base presence. A
        # missing WORST field (cw None) leaves the row OK with C_worst/payoff_worst blank
        # (fidelity-limited), never NO_PAIR.
        q = quoted_base(primary_C.get("ya_b"), primary_C.get("na_b"))
        row["quoted_base"] = "1" if q else "0"
        if not q or primary_C.get("cb") is None:
            row["status"] = "NO_PAIR"; excl("NO_PAIR", day, ct)
            rows.append(row); continue

        cb = primary_C["cb"]; cw = primary_C["cw"]
        # payoffs per column: escape -> +(1-C); pin -> -C. WORST is blank when cw is None.
        if outcome == "ESCAPE":
            pb = Decimal(1) - cb
            pw = (Decimal(1) - cw) if cw is not None else None
        else:
            pb = -cb
            pw = (-cw) if cw is not None else None
        row["payoff_base"] = f"{pb:.4f}"
        row["payoff_worst"] = f"{pw:.4f}" if pw is not None else ""
        row["status"] = "OK"
        rows.append(row)

    # ---- receipt ----
    counts: Dict[str, int] = {}
    pins = escapes = 0
    ok_worst_blank = 0
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "OK":
            if r["pin_escape"] == "PIN":
                pins += 1
            elif r["pin_escape"] == "ESCAPE":
                escapes += 1
            if r["C_worst"] == "":            # A3.3: WORST fidelity-limited (blank)
                ok_worst_blank += 1
    receipt = {
        "dates": list(dates),
        "n_hours_total": len(rows),
        "status_counts": counts,
        "ok_pins": pins,
        "ok_escapes": escapes,
        "ok_rows_worst_blank": ok_worst_blank,   # A3.3 receipt flag
        "exclusion_inventory": {k: {"count": len(v), "hours": v}
                                for k, v in sorted(inventory.items())},
        "input_file_sha256": {os.path.relpath(p, os.path.dirname(_OUT_DIR)): s
                              for p, s in sorted(shas.items())},
        "constants": {
            "epsilon": str(EPSILON), "sigma_anchors": SIGMA_ANCHORS,
            "sigma_diffs": SIGMA_DIFFS, "sigma_ddof": 1, "fee_rate": str(FEE_RATE),
            "primary_snapshot": "T-5min (end_period_ts == T-300)",
        },
    }
    return rows, receipt


FIELDNAMES = [
    "date", "close_time", "anchor_A", "threshold_K", "G", "orientation",
    "sigma_hat", "g_over_sigma", "pin_escape", "H_result", "L_result",
    "quoted_base", "C_t13_base", "C_t13_worst", "C_t10_base", "C_t10_worst",
    "C_base", "C_worst", "C_t2_base", "C_t2_worst", "payoff_base", "payoff_worst",
    "status",
]


def write_outputs(rows, receipt, csv_path, receipt_path):
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, sort_keys=True)


def _daterange(start: str, end: str) -> List[str]:
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rung 1 census builder")
    ap.add_argument("--dates", nargs="+", help="explicit YYYY-MM-DD list")
    ap.add_argument("--start", help="range start YYYY-MM-DD")
    ap.add_argument("--end", help="range end YYYY-MM-DD")
    ap.add_argument("--out", default=os.path.join(_OUT_DIR, "census_train.csv"))
    ap.add_argument("--receipt", default=os.path.join(_OUT_DIR, "census_receipt.json"))
    ap.add_argument("--authorize-full-run", action="store_true",
                    help="required to run more than 7 days (the bridge's frozen run)")
    args = ap.parse_args(argv)

    if args.dates:
        dates = args.dates
    elif args.start and args.end:
        dates = _daterange(args.start, args.end)
    else:
        ap.error("provide --dates or --start/--end")

    # Safety guard (builder addition, confessed): the full TRAIN census is the
    # bridge's one frozen run, not the builder's. Refuse >7 days unless authorized.
    if len(dates) > 7 and not args.authorize_full_run:
        ap.error(
            f"refusing to build {len(dates)} days without --authorize-full-run "
            f"(the full TRAIN census is the bridge's frozen run)"
        )

    rows, receipt = build_census(dates)
    write_outputs(rows, receipt, args.out, args.receipt)
    print(f"wrote {args.out} ({len(rows)} rows)")
    print(f"wrote {args.receipt}")
    print("status_counts:", json.dumps(receipt["status_counts"]))


if __name__ == "__main__":
    main()
