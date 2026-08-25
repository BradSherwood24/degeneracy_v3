"""Pytest fixtures: put sim/ on sys.path and build synthetic corpora on disk so the real
build_census path (loader + census) can be exercised with injected data_root.
"""
import datetime as _dt
import json
import os
import sys

import pytest

_SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SIM_DIR not in sys.path:
    sys.path.insert(0, _SIM_DIR)


def _iso(ep: int) -> str:
    return _dt.datetime.fromtimestamp(ep, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_jsonl(path: str, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


def _mk_candle(ticker, ts_list, ya_close, ya_high, yb_close, yb_low,
               worst_missing=False):
    sticks = []
    for ts in ts_list:
        yes_ask = {"close_dollars": ya_close, "low_dollars": ya_close,
                   "open_dollars": ya_close}
        yes_bid = {"close_dollars": yb_close, "high_dollars": yb_close,
                   "open_dollars": yb_close}
        if not worst_missing:                         # A3.3: WORST fields present by default
            yes_ask["high_dollars"] = ya_high         # H-leg WORST = yes_ask.high
            yes_bid["low_dollars"] = yb_low           # L-leg WORST = 1 - yes_bid.low
        sticks.append({
            "end_period_ts": ts,
            "price": {"close_dollars": "0.5000", "high_dollars": "0.5000",
                      "low_dollars": "0.5000", "open_dollars": "0.5000"},
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
        })
    return {"ticker": ticker, "candlesticks": sticks}


@pytest.fixture
def corpus(tmp_path):
    """Build a one-hour synthetic corpus and return (data_root, meta).

    meta: date D, hour T (epoch + iso), and both leg tickers. Default is a valid OK
    PIN hour with A>K, G=50.00. Tests can rebuild with different params via corpus.make.
    """
    def make(k_offset=50.0, print_value="60200.00",
             yb_close="0.5500", yb_low="0.4000", ya_close="0.3000", ya_high="0.4000",
             dup_second_day=None, anchor_strikeless=False, ev_h=None, ev_l=None,
             worst_missing=False):
        root = str(tmp_path / f"corp_{k_offset}_{print_value}")
        D = "2026-07-01"
        # T = 03:00 UTC on D
        T = int(_dt.datetime(2026, 7, 1, 3, 0, 0, tzinfo=_dt.timezone.utc).timestamp())
        # 9 anchors at T, T-900, ..., T-7200 with varying diffs (sigma > 0)
        bumps = [0, 10, 25, 45, 70, 100, 135, 175, 220]  # oldest..newest cumulative
        m15 = []
        c15 = []
        for k in range(9):
            ep = T - (8 - k) * 900          # k=0 oldest ... k=8 newest (== T)
            strike = 60000.0 + bumps[k]
            tk = f"KXBTC15M-SYN-{ep}"
            m15.append({
                "ticker": tk, "close_time": _iso(ep), "floor_strike": strike,
                "strike_type": "greater_or_equal", "expiration_value": print_value,
                "result": "yes" if float(print_value) >= strike else "no",
            })
        A = 60000.0 + bumps[-1]             # anchor at T
        anchor_tk = m15[-1]["ticker"]
        # A3.5: override the print (expiration_value) on either leg (None -> the real print).
        if ev_h is not None:
            m15[-1]["expiration_value"] = ev_h
        # A3.1: the T-market may be the strike-less "up/down" product (no floor_strike).
        if anchor_strikeless:
            m15[-1].pop("floor_strike", None)
            m15[-1].pop("strike_type", None)
        K = A - k_offset
        h1_tk = f"KXBTCD-SYN-T{K:.2f}"
        m1h = [{
            "ticker": h1_tk, "close_time": _iso(T), "floor_strike": K,
            "strike_type": "greater",
            "expiration_value": ev_l if ev_l is not None else print_value,
            "result": "yes" if float(print_value) > K else "no",
        }]
        # H = max(A,K) leg gets yes_ask; L = min leg gets yes_bid.
        ts_list = [T - 780, T - 600, T - 300, T - 120]
        if A >= K:      # H is the 15M anchor market, L is the 1H market
            c15.append(_mk_candle(anchor_tk, ts_list, ya_close, ya_high, "0.9900", "0.9900",
                                  worst_missing=worst_missing))
            c1h = [_mk_candle(h1_tk, ts_list, "0.9900", "0.9900", yb_close, yb_low,
                              worst_missing=worst_missing)]
        else:           # H is the 1H market, L is the 15M anchor market
            c15.append(_mk_candle(anchor_tk, ts_list, "0.9900", "0.9900", yb_close, yb_low,
                                  worst_missing=worst_missing))
            c1h = [_mk_candle(h1_tk, ts_list, ya_close, ya_high, "0.9900", "0.9900",
                              worst_missing=worst_missing)]

        _write_jsonl(os.path.join(root, "15-minute", "markets", f"{D}.jsonl"), m15)
        _write_jsonl(os.path.join(root, "15-minute", "candles", f"{D}.jsonl"), c15)
        _write_jsonl(os.path.join(root, "1-hour", "markets", f"{D}.jsonl"), m1h)
        _write_jsonl(os.path.join(root, "1-hour", "candles", f"{D}.jsonl"), c1h)
        return root, {"D": D, "T": T, "T_iso": _iso(T), "anchor_tk": anchor_tk,
                      "h1_tk": h1_tk, "A": A, "K": K}

    make.tmp_path = tmp_path
    return make
