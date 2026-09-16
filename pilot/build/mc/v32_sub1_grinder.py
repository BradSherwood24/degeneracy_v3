"""SUB-$1 BOX GRINDER on the ms journals (Brad 2026-09-15): can a maker on the bucket leg + two taker legs be assembled under $1 for real?
For each window, B = bucket containing K15 (15M strike via read-only proxy GET), legs:
  A: NO@hourly(B) [taker] + YES bucket B [MAKER: rest YES bid at best_bid+1c, fee 0] + YES 15M [taker]   (pays $2 on [K15, B+100))
  M: YES@hourly(B+100) [taker] + YES bucket B [MAKER] + NO 15M [taker]                                   (pays $2 on [B, K15))
Every 1 s tick T-20..T-0 the maker rests at bid+1c IF the full set at that instant costs < $1 (else no rest; re-solved each tick).
A MAKER FILL happens when a bucket YES print occurs with taker_side "no" (a YES seller hitting bids) at yes_price <= our bid (price priority:
we are alone one tick inside) while we rest. On fill we immediately TAKE the two other legs at their asks at that instant (+ exact fees);
realized cost = our bid + taker legs; lock = 1 - cost; payoff from results (proxy GET markets for bucket/strikes/15M results).
One set per window per variant (grinder = 1 lot). Also counts how many seconds we were resting and how many windows had any rest.
"""
import gzip, json, glob, os, sys, math, statistics as st, collections, urllib.request
sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot")
from service.book import BookMirror
JDIR = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32"
def fee(p): return math.ceil(0.07 * p * (1 - p) * 10000) / 10000
_MK = {}
def market(ticker):
    if ticker in _MK: return _MK[ticker]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8642/trade-api/v2/markets/{ticker}", timeout=8) as resp:
            m = json.load(resp).get("market", {})
    except Exception:
        m = {}
    _MK[ticker] = m
    return m

results = []
for path in sorted(glob.glob(os.path.join(JDIR, "*.jsonl.gz"))):
    close = os.path.basename(path).split(".")[0]
    mirrors = {}; m15tk = None; tag = None; close_epoch = None; last = None; K15 = None
    rest = {"A": None, "M": None}          # (bid_price, ts) while resting
    resting_s = {"A": 0, "M": 0}; fills = {"A": None, "M": None}; sub1_s = {"A": 0, "M": 0}
    B = None; bkt_tk = None
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"kalshi_ws"' in line[:40]:
                r = json.loads(line); o = r["obj"]; t = o.get("type"); m = o.get("msg", {}); tk = m.get("market_ticker", "")
                ts = r["local_ts"]
                if t in ("orderbook_snapshot", "orderbook_delta"):
                    bm = mirrors.get(tk)
                    if bm is None: bm = mirrors[tk] = BookMirror()
                    (bm.apply_snapshot if t == "orderbook_snapshot" else bm.apply_delta)(m)
                elif t == "trade" and bkt_tk and tk == bkt_tk and m.get("taker_side") == "no":
                    # a YES seller hit the bucket bids: fills our resting YES bid if print price <= our bid
                    p = float(m["yes_price_dollars"])
                    for v in ("A", "M"):
                        if rest[v] and fills[v] is None and p <= rest[v][0] + 1e-9:
                            lo = mirrors.get(f"KXBTCD-{tag}-T{B-1}.99"); hi = mirrors.get(f"KXBTCD-{tag}-T{B+99}.99"); m15 = mirrors.get(m15tk)
                            if not (lo and hi and m15): continue
                            tlo = lo.top_of_book(); thi = hi.top_of_book(); t15 = m15.top_of_book()
                            if v == "A":
                                if tlo.yes_bid is None or t15.yes_ask is None: continue
                                no_lo = 1 - float(tlo.yes_bid); y15 = float(t15.yes_ask)
                                cost = rest[v][0] + no_lo + fee(no_lo) + y15 + fee(y15)
                            else:
                                if thi.yes_ask is None or t15.yes_bid is None: continue
                                yes_hi = float(thi.yes_ask); n15 = 1 - float(t15.yes_bid)
                                cost = rest[v][0] + yes_hi + fee(yes_hi) + n15 + fee(n15)
                            fills[v] = {"ts": ts, "t_minus": close_epoch - ts if close_epoch else None, "bid": rest[v][0], "cost": cost, "print": p, "size": float(m.get("count_fp") or 0)}
                if K15 is None or close_epoch is None or (last is not None and ts - last < 1.0): continue
                last = ts; tm = close_epoch - ts
                if not (0 <= tm <= 1200): continue
                bkt = mirrors.get(bkt_tk); lo = mirrors.get(f"KXBTCD-{tag}-T{B-1}.99"); hi = mirrors.get(f"KXBTCD-{tag}-T{B+99}.99"); m15 = mirrors.get(m15tk)
                if not (bkt and lo and hi and m15) or any(x.suspect for x in (bkt, lo, hi, m15)): rest = {"A": None, "M": None}; continue
                tb = bkt.top_of_book(); t15 = m15.top_of_book(); tlo = lo.top_of_book(); thi = hi.top_of_book()
                if None in (tb.yes_ask, tb.yes_bid, t15.yes_ask, t15.yes_bid, tlo.yes_bid, thi.yes_ask): rest = {"A": None, "M": None}; continue
                ya_b = float(tb.yes_ask); yb_b = float(tb.yes_bid); bid = round(yb_b + 0.01, 2)
                if bid >= ya_b: rest = {"A": None, "M": None}; continue
                no_lo = 1 - float(tlo.yes_bid); yes_hi = float(thi.yes_ask); y15 = float(t15.yes_ask); n15 = 1 - float(t15.yes_bid)
                costA = bid + no_lo + fee(no_lo) + y15 + fee(y15); costM = bid + yes_hi + fee(yes_hi) + n15 + fee(n15)
                for v, c in (("A", costA), ("M", costM)):
                    if fills[v] is not None: rest[v] = None; continue
                    if c < 1.0:
                        sub1_s[v] += 1; resting_s[v] += 1; rest[v] = (bid, ts)
                    else:
                        rest[v] = None
                continue
            try: r = json.loads(line)
            except Exception: continue
            k = r.get("kind")
            if k == "window_meta":
                bl = r["obj"].get("buckets") or []; tag = bl[0]["event_ticker"].split("-")[1] if bl else None
            elif k == "m15_recording":
                tks = r["obj"].get("tickers") or []; m15tk = tks[0] if tks else None
                if m15tk:
                    mk = market(m15tk); fs = mk.get("floor_strike")
                    if fs is not None:
                        K15 = float(fs); B = int(K15 // 100 * 100); bkt_tk = f"KXBTC-{tag}-B{B+50}"
            elif k == "v32_eval":
                o = r["obj"]; tmv = o.get("t_minus_s")
                if tmv is not None: close_epoch = r["local_ts"] + tmv
    if K15 is None: print(f"== {close}: no K15"); continue
    r15 = market(m15tk).get("result"); rb = market(bkt_tk).get("result"); rlo = market(f"KXBTCD-{tag}-T{B-1}.99").get("result"); rhi = market(f"KXBTCD-{tag}-T{B+99}.99").get("result")
    payA = (int(rlo == "no") + int(rb == "yes") + int(r15 == "yes")) if None not in (rlo, rb, r15) else None
    payM = (int(rhi == "yes") + int(rb == "yes") + int(r15 == "no")) if None not in (rhi, rb, r15) else None
    row = {"close": close, "K15": K15, "B": B, "payA": payA, "payM": payM, "sub1_s": sub1_s, "resting_s": resting_s, "fills": fills}
    results.append(row)
    fa = fills["A"]; fm = fills["M"]
    print(f"== {close} K15={K15} B={B} pay A/M={payA}/{payM} sub-$1 seconds A={sub1_s['A']} M={sub1_s['M']} | "
          f"fill A: {('t-%.0f cost $%.4f lock %+.1fc print %.2f x%.0f' % (fa['t_minus'], fa['cost'], (1-fa['cost'])*100, fa['print'], fa['size'])) if fa else '-'} | "
          f"fill M: {('t-%.0f cost $%.4f lock %+.1fc print %.2f x%.0f' % (fm['t_minus'], fm['cost'], (1-fm['cost'])*100, fm['print'], fm['size'])) if fm else '-'}")

print("\n== pooled ==")
for v in ("A", "M"):
    rs = [r for r in results if r["fills"][v]]
    win = [r for r in results if r["sub1_s"][v] > 0]
    print(f"variant {v}: windows with any sub-$1 resting {len(win)}/{len(results)}; maker fills {len(rs)}; ", end="")
    if rs:
        locks = [(1 - r["fills"][v]["cost"]) * 100 for r in rs]
        pays = [r[f"pay{v}"] for r in rs if r[f"pay{v}"] is not None]
        realized = [(r[f"pay{v}"] - r["fills"][v]["cost"]) * 100 for r in rs if r[f"pay{v}"] is not None]
        print(f"lock at fill mean {st.mean(locks):+.1f}c min {min(locks):+.1f}c; payoffs {collections.Counter(pays)}; realized mean {st.mean(realized):+.1f}c total {sum(realized):+.0f}c over {len(results)} windows")
    else: print("no fills")
json.dump(results, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "v32_sub1_grinder.json"), "w"), default=str)
