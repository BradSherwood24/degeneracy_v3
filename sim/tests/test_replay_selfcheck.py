"""Reconciler tests: trade<->delta matching (both taker sides, order-insensitive within the
match window), receipt-lag percentiles, and a real-excerpt BookMirror agreement if a journal
is present (skipped otherwise so the suite never depends on the gitignored 14 GB corpus)."""

import os
import sys

_SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SIM_DIR)
for p in (_SIM_DIR, _REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest   # noqa: E402
from replay.selfcheck import Reconciler   # noqa: E402


def _delta(tkr, ts, side, price, dfp, lts=None):
    o = {"type": "orderbook_delta",
         "msg": {"market_ticker": tkr, "ts_ms": ts, "side": side,
                 "price_dollars": price, "delta_fp": dfp}}
    if lts is not None:
        o["_local_ts"] = lts
    return o


def _trade(tkr, ts, taker, yes_price, no_price, cnt, lts=None):
    o = {"type": "trade",
         "msg": {"market_ticker": tkr, "ts_ms": ts, "taker_side": taker,
                 "yes_price_dollars": yes_price, "no_price_dollars": no_price, "count_fp": cnt}}
    if lts is not None:
        o["_local_ts"] = lts
    return o


def test_reconcile_taker_yes_hits_no_bid():
    r = Reconciler(("H",))
    # taker bought YES @0.56 -> hits NO bid @ (1-0.56)=0.44
    r.feed(_delta("H", 1000, "no", "0.44", "-2.00"))
    r.feed(_trade("H", 1000, "yes", "0.56", "0.44", "2.00"))
    s = r.finalize()
    assert s.n_trades == 1 and s.n_matched == 1 and s.rate == 1.0


def test_reconcile_taker_no_hits_yes_bid_delta_after_trade():
    # trade arrives BEFORE its delta (same ts) -> the sliding buffer must still match
    r = Reconciler(("L",))
    r.feed(_trade("L", 2000, "no", "0.48", "0.52", "1.00"))
    r.feed(_delta("L", 2000, "yes", "0.48", "-1.00"))
    s = r.finalize()
    assert s.n_matched == 1 and s.rate == 1.0


def test_reconcile_unmatched_when_no_delta():
    r = Reconciler(("H",))
    r.feed(_trade("H", 3000, "yes", "0.56", "0.44", "2.00"))
    # advance engine time past the match window so the pending trade is finalized unmatched
    r.feed(_delta("H", 3000 + 5000, "no", "0.10", "-1.00"))
    s = r.finalize()
    assert s.n_trades == 1 and s.n_matched == 0 and s.rate == 0.0


def test_receipt_lag_percentiles():
    r = Reconciler(("H",))
    for i, lag in enumerate([0.0, 0.010, 0.020, 0.030, 0.040]):
        ts = 1000 + i
        r.feed(_delta("H", ts, "no", "0.44", "-1.00", lts=(ts + lag * 1000) / 1000.0))
    r.finalize()
    assert abs(r.stats.lag_p50() - 20.0) < 1.0
    assert r.stats.lag_p99() >= 39.0


def _find_journal():
    jdir = os.path.join(_REPO, "pilot", "journals")
    if not os.path.isdir(jdir):
        return None
    import glob
    files = sorted(glob.glob(os.path.join(jdir, "2026*.jsonl")), key=os.path.getsize)
    return files[-1] if files else None


def test_real_excerpt_bookmirror_agreement():
    jr = _find_journal()
    if jr is None:
        pytest.skip("no journal corpus present")
    pilot = os.path.join(_REPO, "pilot")
    if pilot not in sys.path:
        sys.path.insert(0, pilot)
    from service.book import BookMirror
    from replay.book import IntBook, dollars_to_mils
    from replay.journal import iter_ws, read_window_header

    hdr = read_window_header(jr)
    tkr = hdr.high_ticker
    ib, bm = IntBook(), BookMirror()
    checked = 0
    for obj in iter_ws(jr):
        m = obj.get("msg") or {}
        if m.get("market_ticker") != tkr:
            continue
        t = obj.get("type")
        if t == "orderbook_snapshot":
            ib.apply_snapshot(m); bm.apply_snapshot(m)
        elif t == "orderbook_delta":
            ib.apply_delta(m); bm.apply_delta(m)
        else:
            continue
        top = bm.top_of_book()
        iyb, inb = ib.best_yes_bid(), ib.best_no_bid()
        assert (top.yes_bid is None) == (iyb is None)
        if iyb is not None:
            assert dollars_to_mils(top.yes_bid) == iyb[0]
        assert (top.no_bid is None) == (inb is None)
        if inb is not None:
            assert dollars_to_mils(top.no_bid) == inb[0]
        checked += 1
        if checked >= 20000:
            break
    assert checked > 100
