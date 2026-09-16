"""SUB-$1 15M BOX on the ms journals (all three legs from live BookMirrors, same instant).
B = bucket containing K15 (from the m15 ticker's floor via the 15M book? K15 comes from the KXBTC15M market floor_strike; the journal's
m15_recording gives the ticker; strike = we infer from the journal's 15M orderbook ticker name? Not present -> read historical-data/15-minute
markets for that close if available, else skip.) Variant A: NO@hourly(B) + YES bucket B + YES 15M. Variant M: YES@hourly(B+100) + YES bucket
+ NO 15M. Sample every 1 s T-20..T-0. Taker = all asks + exact fees; maker = bucket leg at bid+1c (fee 0), others taker.
Reports: cost distribution, share < $1, depth at the legs when < $1 (min lots across the three asks), and leg ages (all live).
"""
import gzip, json, glob, os, sys, math, statistics as st, collections
from decimal import Decimal
sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot")
from service.book import BookMirror
JDIR = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32"
HD = r"C:\Users\Brads\Python_stuff\degeneracy_v3\historical-data"
def fee(p): return math.ceil(0.07 * p * (1 - p) * 10000) / 10000

def k15_for(close_iso):
    """15M strike for the 15M market closing at close_iso, from historical-data/15-minute/markets/<day>.jsonl (if fetched)."""
    day = close_iso[:10]
    for d in (day,):
        p = os.path.join(HD, "15-minute", "markets", f"{d}.jsonl")
        if not os.path.exists(p): continue
        for line in open(p, encoding="utf-8"):
            m = json.loads(line)
            if m.get("ticker", "").startswith("KXBTC15M") and m.get("close_time") == close_iso and m.get("floor_strike") is not None:
                return float(m["floor_strike"]), m.get("result")
    return None, None

import urllib.request
_K15_CACHE = {}
def k15_from_proxy(ticker):
    """Read-only GET via the local signing proxy: the 15M market's floor_strike (+ result if settled)."""
    if ticker in _K15_CACHE: return _K15_CACHE[ticker]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8642/trade-api/v2/markets/{ticker}", timeout=8) as resp:
            m = json.load(resp).get("market", {})
        fs = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("strike")
        val = (float(fs), m.get("result")) if fs is not None else (None, None)
    except Exception as e:
        val = (None, None)
    _K15_CACHE[ticker] = val
    return val

out = []
for path in sorted(glob.glob(os.path.join(JDIR, "*.jsonl.gz"))):
    close = os.path.basename(path).split(".")[0]
    close_iso = f"{close[:4]}-{close[4:6]}-{close[6:8]}T{close[9:11]}:{close[11:13]}:{close[13:15]}Z"
    K15, r15 = k15_for(close_iso)
    mirrors = {}; m15tk = None; tag = None; close_epoch = None; last = None
    rowsA = []; rowsM = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"kalshi_ws"' in line[:40]:
                r = json.loads(line); o = r["obj"]; t = o.get("type"); m = o.get("msg", {}); tk = m.get("market_ticker", "")
                if t in ("orderbook_snapshot", "orderbook_delta"):
                    bm = mirrors.get(tk)
                    if bm is None: bm = mirrors[tk] = BookMirror()
                    (bm.apply_snapshot if t == "orderbook_snapshot" else bm.apply_delta)(m)
                ts = r["local_ts"]
                if K15 is None or close_epoch is None or (last is not None and ts - last < 1.0): continue
                last = ts; tm = close_epoch - ts
                if not (0 <= tm <= 1200): continue
                B = int(K15 // 100 * 100)
                bkt = mirrors.get(f"KXBTC-{tag}-B{B+50}"); lo = mirrors.get(f"KXBTCD-{tag}-T{B-1}.99"); hi = mirrors.get(f"KXBTCD-{tag}-T{B+99}.99"); m15 = mirrors.get(m15tk) if m15tk else None
                if not (bkt and lo and hi and m15) or any(x.suspect for x in (bkt, lo, hi, m15)): continue
                tb = bkt.top_of_book(); t15 = m15.top_of_book(); tlo = lo.top_of_book(); thi = hi.top_of_book()
                if None in (tb.yes_ask, tb.yes_bid, t15.yes_ask, t15.yes_bid, tlo.yes_bid, thi.yes_ask): continue
                ya_b = float(tb.yes_ask); yb_b = float(tb.yes_bid)
                bucket_taker = ya_b + fee(ya_b); bucket_maker = (yb_b + 0.01) if yb_b + 0.01 < ya_b else None
                y15 = float(t15.yes_ask); n15 = 1 - float(t15.yes_bid)
                no_lo = 1 - float(tlo.yes_bid); yes_hi = float(thi.yes_ask)
                depthA = min(float(tb.yes_ask_size or 0), float(t15.yes_ask_size or 0), float(tlo.yes_bid_size or 0))
                depthM = min(float(tb.yes_ask_size or 0), float(t15.yes_bid_size or 0), float(thi.yes_ask_size or 0))
                A_t = no_lo + fee(no_lo) + bucket_taker + y15 + fee(y15)
                M_t = yes_hi + fee(yes_hi) + bucket_taker + n15 + fee(n15)
                A_m = (no_lo + fee(no_lo) + bucket_maker + y15 + fee(y15)) if bucket_maker else None
                M_m = (yes_hi + fee(yes_hi) + bucket_maker + n15 + fee(n15)) if bucket_maker else None
                rowsA.append((tm, A_t, A_m, depthA)); rowsM.append((tm, M_t, M_m, depthM))
                continue
            try: r = json.loads(line)
            except Exception: continue
            k = r.get("kind")
            if k == "window_meta":
                bl = r["obj"].get("buckets") or []; tag = bl[0]["event_ticker"].split("-")[1] if bl else None
            elif k == "m15_recording":
                tks = r["obj"].get("tickers") or []; m15tk = tks[0] if tks else None
                if K15 is None and m15tk: K15, r15 = k15_from_proxy(m15tk)
            elif k == "v32_eval":
                o = r["obj"]; tmv = o.get("t_minus_s")
                if tmv is not None: close_epoch = r["local_ts"] + tmv
    def summ(name, rows, idx):
        vals = [(tm, c, d) for tm, ct, cm, d in rows for c in [ct if idx == 1 else cm] if c is not None]
        if not vals: return f"{name}: no samples"
        costs = [c for _, c, _ in vals]; sub = [(tm, c, d) for tm, c, d in vals if c < 1.0]
        s = f"{name}: n={len(vals)} mean ${st.mean(costs):.4f} min ${min(costs):.4f} sub-$1 {len(sub)} ({len(sub)/len(vals):.1%})"
        if sub: s += f" | sub-$1 min depth lots: {sorted(d for _,_,d in sub)[len(sub)//2]:.0f} median, {min(d for _,_,d in sub):.0f} min | t_minus of sub-$1: {sorted(round(tm) for tm,_,_ in sub)[:6]}...{sorted(round(tm) for tm,_,_ in sub)[-3:]}"
        return s
    print(f"== {close} K15={K15} result15={r15} samples={len(rowsA)}")
    print("  " + summ("A_taker", rowsA, 1)); print("  " + summ("A_maker", rowsA, 2))
    print("  " + summ("M_taker", rowsM, 1)); print("  " + summ("M_maker", rowsM, 2))
    out.append({"close": close, "K15": K15, "A": rowsA, "M": rowsM})
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "v32_sub1_ms.json"), "w"))
