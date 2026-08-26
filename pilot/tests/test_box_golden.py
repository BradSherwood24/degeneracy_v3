"""GOLDEN PARITY for the wide-box core.

For >=200 hourly closes drawn from the NON-SEALED historical-data candle record, this test
builds TopOfBook-equivalent quotes from the 1-minute candle close bid/ask at the window minutes
T-600, T-540, ..., T-60 and checks that ``select_box`` + the first-qualifying-minute scan
reproduces an INLINE reimplementation of the scratch wide_box study (the oracle) on every hour:
same (minute, strike, side, C) wherever the scratch fires, and no fire where it doesn't.

Numeric domain: the oracle is implemented in Decimal (house law: Decimal for money), matching
box.py's domain exactly, so the parity is bit-exact. A FLOAT-domain replica of the scratch's raw
arithmetic is also run to count hours whose fire would differ under float rounding (reported as a
CONFESSION metric; the scratch itself computed in float).

SEAL: the days 2026-08-02..2026-08-18 are the sealed holdout. This test refuses to open any file
for those days and asserts none was read (house law). It skips cleanly if historical-data is
absent.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
from decimal import Decimal

import pytest

from service._simlaw import fee
from service.book import TopOfBook
from service.box import (
    BUY_NO,
    BUY_YES,
    FIRE,
    BookUpdate,
    BoxSelection,
    BoxState,
    ClockTick,
    NoBox,
    load_box_policy,
    select_box,
)

# --- locations -------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HD = os.path.join(_REPO_ROOT, "historical-data")

# --- the SEALED holdout (house law): never opened by this test -------------
_SEALED_DAYS = frozenset(
    (dt.date(2026, 8, 2) + dt.timedelta(days=i)).isoformat() for i in range(17)  # 08-02..08-18
)

PARAMS = load_box_policy()
TARGET = PARAMS.target_mid            # 0.95
MIN15 = PARAMS.min15_ask              # 0.85
H_MIN = PARAMS.hourly_ask_min         # 0.90
H_MAX = PARAMS.hourly_ask_max         # 0.99
MAXSPREAD = PARAMS.max_spread         # 0.10
_ONE = Decimal(1)
_TWO = Decimal(2)
_HALF = Decimal("0.5")
_START_MIN = 10                       # scratch start_min


# --- candle IO (refuses sealed days) ---------------------------------------
_files_read: list[str] = []


def _load_candles(kind: str, day: str) -> dict[str, dict[int, dict]]:
    if day in _SEALED_DAYS:
        raise AssertionError(f"golden test attempted to read SEALED day {day} ({kind})")
    path = os.path.join(HD, kind, "candles", f"{day}.jsonl")
    out: dict[str, dict[int, dict]] = {}
    if not os.path.exists(path):
        return out
    _files_read.append(path)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out[r["ticker"]] = {c["end_period_ts"]: c for c in r.get("candlesticks", [])}
    return out


def _non_sealed_days() -> list[str]:
    days = sorted(
        os.path.basename(p)[:-6]
        for p in glob.glob(os.path.join(HD, "15-minute", "markets", "*.jsonl"))
    )
    return [d for d in days if d not in _SEALED_DAYS]


def _quote_str(candle: dict | None) -> tuple[str, str] | None:
    """Raw (yes_ask, yes_bid) close-dollar STRINGS from a candle, or None if absent."""
    if not candle:
        return None
    a = candle.get("yes_ask", {}).get("close_dollars")
    b = candle.get("yes_bid", {}).get("close_dollars")
    if a is None or b is None:
        return None
    return a, b


def _top_from_candle(candle: dict | None) -> TopOfBook | None:
    q = _quote_str(candle)
    if q is None:
        return None
    a, b = Decimal(q[0]), Decimal(q[1])
    return TopOfBook(
        yes_bid=b, yes_bid_size=_ONE, yes_ask=a, yes_ask_size=_ONE,
        no_bid=_ONE - a, no_bid_size=_ONE, no_ask=_ONE - b, no_ask_size=_ONE,
        suspect=False,
    )


# ---------------------------------------------------------------------------
# HOURS builder — mirrors the scratch HOURS construction (non-sealed only)
# ---------------------------------------------------------------------------
def _build_hours() -> list[dict]:
    hours: list[dict] = []
    hc_cache: dict[str, dict] = {}
    for day in _non_sealed_days():
        c15 = _load_candles("15-minute", day)
        mpath = os.path.join(HD, "15-minute", "markets", f"{day}.jsonl")
        if not os.path.exists(mpath):
            continue
        with open(mpath, "r", encoding="utf-8") as f:
            for line in f:
                m = json.loads(line)
                if (
                    m.get("floor_strike") is None
                    or not m.get("open_time", "").endswith(":45:00Z")
                    or not m.get("expiration_value")
                ):
                    continue
                A = Decimal(str(m["floor_strike"]))
                tk15 = m["ticker"]
                hev = "KXBTCD-" + m["event_ticker"].split("-")[1][:-2]
                open_ts = int(
                    dt.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp()
                )
                close_ts = open_ts + 900
                # find the day-file holding this hourly event's ladder (same/next/prev day)
                hc = None
                for d in (
                    day,
                    (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat(),
                    (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat(),
                ):
                    if d in _SEALED_DAYS:
                        continue
                    if d not in hc_cache:
                        hc_cache[d] = _load_candles("1-hour", d)
                    if any(t.startswith(hev + "-T") for t in hc_cache[d]):
                        hc = hc_cache[d]
                        break
                if hc is None:
                    continue
                ladder = {
                    t: (Decimal(t.split("-T")[1]), cc)
                    for t, cc in hc.items()
                    if t.startswith(hev + "-T")
                }
                hours.append(
                    dict(
                        m15_ticker=tk15,
                        A=A,
                        close_ts=close_ts,
                        c15=c15.get(tk15, {}),
                        ladder=ladder,  # ticker -> (K, {ts: candle})
                    )
                )
    return hours


# ---------------------------------------------------------------------------
# THE ORACLE — inline reimplementation of the scratch run() selection (Decimal)
# Returns (mins, strike_K, hourly_side, C) for the first qualifying minute, or None.
# ---------------------------------------------------------------------------
def _oracle_decimal(h: dict) -> tuple[int, Decimal, str, Decimal] | None:
    A = h["A"]
    for mins in range(_START_MIN, 0, -1):
        ts = h["close_ts"] - 60 * mins
        q15 = _quote_str(h["c15"].get(ts))
        if q15 is None:
            continue
        ya, yb = Decimal(q15[0]), Decimal(q15[1])
        if ya - yb > MAXSPREAD:
            continue
        mid15 = (ya + yb) / _TWO
        if mid15 < _HALF:
            below = True
            leg15_ask = _ONE - yb
            hourly_side = BUY_YES
        else:
            below = False
            leg15_ask = ya
            hourly_side = BUY_NO
        cands: list[tuple[Decimal, Decimal, Decimal]] = []  # (K, h_ask, h_mid)
        for _tk, (K, cc) in h["ladder"].items():
            q = _quote_str(cc.get(ts))
            if q is None:
                continue
            hya, hyb = Decimal(q[0]), Decimal(q[1])
            if hya - hyb > MAXSPREAD:
                continue
            if below:
                if K >= A:
                    continue
                h_ask, h_mid = hya, (hya + hyb) / _TWO
            else:
                if K <= A:
                    continue
                h_ask, h_mid = _ONE - hyb, _ONE - (hya + hyb) / _TWO
            cands.append((K, h_ask, h_mid))
        if leg15_ask < MIN15 or not cands:
            continue
        K, h_ask, h_mid = min(cands, key=lambda x: (abs(x[2] - TARGET), abs(x[0] - A)))
        if not (H_MIN <= h_ask <= H_MAX):
            continue
        C = h_ask + fee(h_ask) + leg15_ask + fee(leg15_ask)
        return mins, K, hourly_side, C
    return None


# ---------------------------------------------------------------------------
# FLOAT replica of the scratch raw arithmetic (for the confession metric only).
# Returns (mins, strike_float, side) or None — selection identity in float.
# ---------------------------------------------------------------------------
def _oracle_float(h: dict) -> tuple[int, float, str] | None:
    A = float(h["A"])
    for mins in range(_START_MIN, 0, -1):
        ts = h["close_ts"] - 60 * mins
        q15 = _quote_str(h["c15"].get(ts))
        if q15 is None:
            continue
        ya, yb = float(q15[0]), float(q15[1])
        if ya - yb > 0.10:
            continue
        mid15 = (ya + yb) / 2
        if mid15 < 0.5:
            below = True
            leg15_ask = 1 - yb
            side = BUY_YES
        else:
            below = False
            leg15_ask = ya
            side = BUY_NO
        cands = []
        for _tk, (K, cc) in h["ladder"].items():
            Kf = float(K)
            q = _quote_str(cc.get(ts))
            if q is None:
                continue
            hya, hyb = float(q[0]), float(q[1])
            if hya - hyb > 0.10:
                continue
            if below:
                if Kf >= A:
                    continue
                cands.append((Kf, hya, (hya + hyb) / 2))
            else:
                if Kf <= A:
                    continue
                cands.append((Kf, 1 - hyb, 1 - (hya + hyb) / 2))
        if leg15_ask < 0.85 or not cands:
            continue
        Kf, h_ask, h_mid = min(cands, key=lambda x: (abs(x[2] - 0.95), abs(x[0] - A)))
        if not (0.90 <= h_ask <= 0.99):
            continue
        return mins, Kf, side
    return None


# ---------------------------------------------------------------------------
# box.py side — select_box + first-qualifying-minute scan
# ---------------------------------------------------------------------------
def _box_scan(h: dict) -> tuple[int, Decimal, str, Decimal] | None:
    for mins in range(_START_MIN, 0, -1):
        ts = h["close_ts"] - 60 * mins
        m15_top = _top_from_candle(h["c15"].get(ts))
        if m15_top is None:
            continue
        ladder = {}
        for tk, (K, cc) in h["ladder"].items():
            top = _top_from_candle(cc.get(ts))
            if top is not None:
                ladder[tk] = (K, top)
        sel = select_box(h["A"], h["m15_ticker"], m15_top, ladder, PARAMS)
        if isinstance(sel, BoxSelection):
            return mins, sel.strike_K, sel.hourly_side, sel.C
    return None


# ---------------------------------------------------------------------------
# THE TEST
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.isdir(os.path.join(HD, "15-minute", "markets")),
    reason="historical-data absent",
)
def test_golden_parity_select_box_vs_scratch_oracle():
    hours = _build_hours()
    # house-law assertion: no sealed day file was opened
    assert all(
        os.path.basename(p)[:-6] not in _SEALED_DAYS for p in _files_read
    ), "a sealed-day candle file was opened"

    if len(hours) < 200:
        pytest.skip(f"only {len(hours)} hours available (< 200)")

    mismatches: list[str] = []
    fires = 0
    float_divergences = 0
    for h in hours:
        oracle = _oracle_decimal(h)
        box = _box_scan(h)
        if oracle != box:
            mismatches.append(
                f"{h['m15_ticker']}: oracle={oracle} box={box}"
            )
        if oracle is not None:
            fires += 1
        # confession metric: float-domain selection identity vs Decimal
        fo = _oracle_float(h)
        do = None if oracle is None else (oracle[0], float(oracle[1]), oracle[2])
        if fo != do:
            float_divergences += 1

    # PRIMARY: box.py reproduces the Decimal oracle exactly on every hour.
    assert not mismatches, (
        f"{len(mismatches)} mismatch(es) of {len(hours)} hours; first few:\n"
        + "\n".join(mismatches[:10])
    )

    # informational receipts (visible with -s / -rA)
    print(
        f"\n[golden] hours_compared={len(hours)} fires={fires} "
        f"no_fire={len(hours) - fires} float_vs_decimal_divergences={float_divergences}"
    )
    assert len(hours) >= 200


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(HD, "15-minute", "markets")),
    reason="historical-data absent",
)
def test_golden_decide_box_reproduces_scan_on_winning_snapshot():
    """decide_box, fed the winning minute's full snapshot into a fresh state (ladder ticks first,
    the 15M tick last so no partial-ladder early fire), fires exactly the scan's selection with
    IOC limits = observed ask + margin (capped 0.99). This exercises the stateful engine, the
    freshness gate, the entry-window bound, and the action/limit construction on REAL data,
    without the cross-minute staleness confound (documented in CONFESSIONS)."""
    hours = _build_hours()
    if len(hours) < 200:
        pytest.skip(f"only {len(hours)} hours available (< 200)")

    checked = 0
    for h in hours:
        box = _box_scan(h)
        if box is None:
            continue
        mins, K, side, C = box
        ts = h["close_ts"] - 60 * mins
        strikes = {tk: kk for tk, (kk, _cc) in h["ladder"].items()}
        st = BoxState.new(
            close_time="1970-01-01T00:00:00Z",  # unused: T passed explicitly
            anchor_A=h["A"], m15_ticker=h["m15_ticker"], strikes=strikes,
            T=h["close_ts"],
        )
        acts_all = []
        # ladder ticks first (M15 absent -> cannot fire), then the M15 tick last
        for tk, (kk, cc) in h["ladder"].items():
            top = _top_from_candle(cc.get(ts))
            if top is not None:
                st, a = decide_box_or_import(st, BookUpdate(tk, top, ts))
                acts_all += a
        m15_top = _top_from_candle(h["c15"].get(ts))
        st, a = decide_box_or_import(st, BookUpdate(h["m15_ticker"], m15_top, ts))
        acts_all += a

        fire = [x for x in acts_all if x.kind == FIRE]
        assert len(fire) == 1, f"{h['m15_ticker']}: expected 1 FIRE, got {len(fire)}"
        act = fire[0]
        sel = st.fired_selection
        assert (sel.strike_K, sel.hourly_side, sel.C) == (K, side, C)
        assert act.C == C
        # limits = ask + margin, capped at 0.99
        want_h = min(sel.hourly_ask + PARAMS.limit_margin, Decimal("0.99"))
        want_m = min(sel.m15_ask + PARAMS.limit_margin, Decimal("0.99"))
        limits = {leg.ticker: leg.limit_price for leg in act.legs}
        assert limits[sel.hourly_ticker] == want_h
        assert limits[sel.m15_ticker] == want_m
        checked += 1
    assert checked > 0
    print(f"\n[golden decide_box] winning-snapshot fires checked={checked}")


# import decide_box lazily so the module-level import list stays focused
def decide_box_or_import(st, event):
    from service.box import decide_box
    return decide_box(PARAMS, st, event)
