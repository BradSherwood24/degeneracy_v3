"""SUB-$1 15M BOX (Brad 2026-09-15): bucket B = the $100 range bucket containing the 15M strike K15.
Variant A (Brad's): NO on hourly strike at B (pays close < B) + YES bucket [B,B+100) + YES 15M (pays close >= K15)
   payoff: <B $1 | [B,K15) $1 | [K15,B+100) $2 | >=B+100 $1  -> min $1, $2 in the gap above K15.
Variant M (mirror): YES on hourly strike at B+100 + YES bucket + NO 15M (pays close < K15)
   payoff: <B $1 | [B,K15) $2 | [K15,B+100) $1 | >=B+100 $1.
Fair cost = 1 + P(gap) = fair payoff, so cost < $1 (after fees) is an arbitrage if it exists. Price every minute T-20..T-1 of the forward
hours (8/30-9/04; tob for strikes+15M, range candles for the bucket) as TAKER (all asks + exact fees) and with the bucket leg as MAKER
(bid + 1c, fee 0). Also the realized payoff from results."""
import sys, os, gzip, json, glob, statistics as st, collections, bisect
from datetime import datetime
SCRATCH = r"C:/Users/Brads/AppData/Local/Temp/claude/C--Users-Brads-Python-stuff-degeneracy-v3/e7acb14c-8903-4c04-ae00-7d39ed05e898/scratchpad"
sys.path.insert(0, os.path.join(SCRATCH, "range"))
import rangelab as R
FEE = R.KALSHI_FEE_EXACT
HD = r"C:/Users/Brads/Python_stuff/degeneracy_v3/historical-data"
TOB = HD + "/tob"

def strike_of(tk):
    try: return round(float(tk.split("-T")[1]) + 0.01)
    except Exception: return None
def at(rows, times, ts):
    i = bisect.bisect_right(times, ts) - 1
    return rows[i] if i >= 0 else None
def load_range(day):
    mp, cp = f"{R.ROOT}/1-hour-range/markets/{day}.jsonl", f"{R.ROOT}/1-hour-range/candles/{day}.jsonl"
    if not (os.path.exists(mp) and os.path.exists(cp)): return None
    mkt = {}; bres = {}
    for m in R._load(mp):
        if m.get("floor_strike") is None or m.get("cap_strike") is None or abs(float(m["cap_strike"]) - float(m["floor_strike"]) - 99.99) > 0.5: continue
        mkt[m["ticker"]] = (m["close_time"], round(float(m["floor_strike"])))
        if m.get("result") in ("yes", "no"): bres[(m["close_time"], round(float(m["floor_strike"])))] = m["result"]
    quotes = collections.defaultdict(dict)
    for r in R._load(cp):
        k = mkt.get(r["ticker"])
        if not k: continue
        q = {c["end_period_ts"]: (float(c["yes_bid"]["close_dollars"]), float(c["yes_ask"]["close_dollars"])) for c in r["candlesticks"] if 0 < float(c["yes_ask"]["close_dollars"]) <= 1}
        if q: quotes[k[0]][k[1]] = q
    return quotes, bres
def load_results(day):
    sres = {}; m15 = {}
    p = f"{HD}/1-hour/markets/{day}.jsonl"
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = json.loads(line)
            if m.get("ticker", "").startswith("KXBTCD") and m.get("result") in ("yes", "no"):
                K = strike_of(m["ticker"])
                if K is not None: sres[(m["close_time"], K)] = m["result"]
    p = f"{HD}/15-minute/markets/{day}.jsonl"
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = json.loads(line)
            if m.get("ticker", "").startswith("KXBTC15M") and m.get("result") in ("yes", "no") and m.get("floor_strike") is not None:
                m15[m["close_time"]] = (float(m["floor_strike"]), m["result"])
    return sres, m15

tob_index = {}
for p in glob.glob(os.path.join(TOB, "*.json.gz")):
    name = os.path.basename(p).split(".")[0]
    tob_index[f"{name[:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}Z"] = p
days = sorted(d[:10] for d in (os.path.basename(x) for x in glob.glob(f"{R.ROOT}/1-hour-range/candles/2026-*.jsonl")) if d[:10] >= "2026-08-30")
def quote_at(q, ts):
    keys = [k for k in q if k <= ts + 60]
    return q[max(keys)] if keys else None

samples = []; hours = 0
for day in days:
    d = load_range(day)
    if not d: continue
    quotes, bres = d
    sres, m15 = load_results(day)
    nday = datetime.utcfromtimestamp(datetime.fromisoformat(day).timestamp() + 86400).strftime("%Y-%m-%d")
    s2, m2 = load_results(nday); sres.update(s2); m15.update(m2)
    d2 = load_range(nday)
    if d2: bres.update(d2[1])
    for ct, bk in quotes.items():
        if ct not in tob_index or ct not in m15: continue
        cts = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
        with gzip.open(tob_index[ct], "rt", encoding="utf-8") as f: mk = json.load(f)["markets"]
        strikes = {}; m15rows = None
        for tk, v in mk.items():
            if tk.startswith("KXBTC15M"):
                if v["tob"]: m15rows = (v["tob"], [r[0] for r in v["tob"]])
                continue
            K = strike_of(tk)
            if K is not None and v["tob"]: strikes[K] = (v["tob"], [r[0] for r in v["tob"]])
        if not strikes or m15rows is None: continue
        hours += 1
        K15, r15 = m15[ct]
        B = int(K15 // 100 * 100)
        rb = bres.get((ct, B)); rlo = sres.get((ct, B)); rhi = sres.get((ct, B + 100))
        if rb is None or rlo is None or rhi is None: continue
        bucket_yes = rb == "yes"; yes15 = r15 == "yes"
        pay_A = int(rlo == "no") + int(bucket_yes) + int(yes15)          # NO@B + YES bucket + YES 15M
        pay_M = int(rhi == "yes") + int(bucket_yes) + int(not yes15)     # YES@B+100 + YES bucket + NO 15M
        for tm in range(1200, 59, -60):
            ts = cts - tm; ts_ms = ts * 1000
            qb = quote_at(bk.get(B, {}), ts)
            c = at(*m15rows, ts_ms)
            if not qb or not c or not (0 < c[1] < 1 and 0 < c[2] < 1): continue
            yb_b, ya_b = qb
            if not (0 < ya_b <= 1 and 0 <= yb_b <= ya_b): continue
            bucket_taker = ya_b + FEE(ya_b)
            bucket_maker = (round(yb_b + 0.01, 2)) if yb_b + 0.01 < ya_b else None   # rest 1 tick inside, fee 0
            y15 = c[2] + FEE(c[2]); n15 = (1 - c[1]) + FEE(1 - c[1])
            row = dict(ct=ct, tm=tm, K15=K15, B=B)
            if B in strikes:
                h = at(*strikes[B], ts_ms)
                if h and 0 < h[1] < 1:
                    no_h = (1 - h[1]) + FEE(1 - h[1])
                    row["A_taker"] = no_h + bucket_taker + y15
                    row["A_maker"] = (no_h + bucket_maker + y15) if bucket_maker is not None else None
                    row["pay_A"] = pay_A
            if B + 100 in strikes:
                h = at(*strikes[B + 100], ts_ms)
                if h and 0 < h[2] < 1:
                    yes_h = h[2] + FEE(h[2])
                    row["M_taker"] = yes_h + bucket_taker + n15
                    row["M_maker"] = (yes_h + bucket_maker + n15) if bucket_maker is not None else None
                    row["pay_M"] = pay_M
            samples.append(row)

print(f"forward hours with 15M: {hours}; minute-samples: {len(samples)}")
for var in ("A", "M"):
    for leg in ("taker", "maker"):
        key = f"{var}_{leg}"
        rs = [s for s in samples if s.get(key) is not None]
        if not rs: continue
        costs = [s[key] for s in rs]
        sub1 = [s for s in rs if s[key] < 1.0]
        pays = [s[f"pay_{var}"] for s in rs]
        print(f"\n{key}: n={len(rs)} hours={len({s['ct'] for s in rs})}  cost mean ${st.mean(costs):.4f}  min ${min(costs):.4f}  p10 ${sorted(costs)[int(0.1*(len(costs)-1))]:.4f}"
              f"  | cost<$1: {len(sub1)} minutes ({len(sub1)/len(rs):.2%}) in {len({s['ct'] for s in sub1})} hours"
              f"  | payoff dist {collections.Counter(pays)}  P(pay=2)={st.mean(1 if p==2 else 0 for p in pays):.1%}"
              f"  | fair check: mean(cost) - (1 + P2) = {st.mean(costs) - (1 + st.mean(1 if p==2 else 0 for p in pays)):+.4f}")
        if sub1:
            print("   sub-$1 samples:", [(s['ct'][5:16], s['tm'], round(s[key],4), s[f'pay_{var}']) for s in sub1[:12]])
        # by time-to-close
        for lo, hi in ((1200, 900), (900, 300), (300, 60)):
            g = [s for s in rs if lo >= s["tm"] > hi]
            if g:
                print(f"   T-{lo//60}..T-{hi//60}: n={len(g)} cost mean ${st.mean(s[key] for s in g):.4f} min ${min(s[key] for s in g):.4f} sub-$1 {sum(1 for s in g if s[key]<1)}")
json.dump(samples, open(os.path.join(SCRATCH, "journals", "pf_15m_sub1.json"), "w"))
