"""Can the 15M-wing set be entered for less than $2? (Brad 2026-09-15)
At each minute T-15..T-5 of the forward hours (8/30-9/04, tob incl. 15M + range candles), cost of the set if our bucket-NO rests one tick
inside the spread (cap = no_ask - 0.01) and gets filled:
   cost = (cap + fee(cap)) + wing_1 + wing_2 ;  lock = 2 - cost   (enterable iff lock > 0; our threshold E = 10c)
Variants:
  H_spot   : spot bucket (highest yes-mid), hourly wings both sides                      <- the live strategy
  M_spot   : spot bucket, K15 outside; 15M leg replaces the hourly wing on the K15 side  (gap pays $3)
  M_adj_lo : bucket immediately BELOW K15's own bucket; wings = YES@Sd hourly + NO@K15 15M (gap = [Su, K15))
  M_adj_hi : bucket immediately ABOVE K15's own bucket; wings = NO@Su hourly + YES@K15 15M (gap = [K15, Sd))
  H_adj_lo / H_adj_hi : the same adjacent buckets with hourly wings (is the adjacency itself the problem, or the 15M price?)"""
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
    mkt = {}
    for m in R._load(mp):
        if m.get("floor_strike") is None or m.get("cap_strike") is None or abs(float(m["cap_strike"]) - float(m["floor_strike"]) - 99.99) > 0.5: continue
        mkt[m["ticker"]] = (m["close_time"], round(float(m["floor_strike"])))
    quotes = collections.defaultdict(dict)
    for r in R._load(cp):
        k = mkt.get(r["ticker"])
        if not k: continue
        q = {c["end_period_ts"]: (float(c["yes_bid"]["close_dollars"]), float(c["yes_ask"]["close_dollars"])) for c in r["candlesticks"] if 0 < float(c["yes_ask"]["close_dollars"]) <= 1}
        if q: quotes[k[0]][k[1]] = q
    return quotes
def load_m15(day):
    m15 = {}
    p = f"{HD}/15-minute/markets/{day}.jsonl"
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = json.loads(line)
            if m.get("ticker", "").startswith("KXBTC15M") and m.get("floor_strike") is not None:
                m15[m["close_time"]] = float(m["floor_strike"])
    return m15

tob_index = {}
for p in glob.glob(os.path.join(TOB, "*.json.gz")):
    name = os.path.basename(p).split(".")[0]
    tob_index[f"{name[:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}Z"] = p
days = sorted(d[:10] for d in (os.path.basename(x) for x in glob.glob(f"{R.ROOT}/1-hour-range/candles/2026-*.jsonl")) if d[:10] >= "2026-08-30")

def quote_at(q, ts):
    keys = [k for k in q if k <= ts + 60]
    return q[max(keys)] if keys else None

res = collections.defaultdict(list)   # variant -> list of (ct, lock)
hours = 0
for day in days:
    quotes = load_range(day)
    if not quotes: continue
    m15 = load_m15(day)
    nday = datetime.utcfromtimestamp(datetime.fromisoformat(day).timestamp() + 86400).strftime("%Y-%m-%d")
    m15.update(load_m15(nday))
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
        K15 = m15[ct]
        for tm in range(900, 299, -60):
            ts = cts - tm; ts_ms = ts * 1000
            c = at(*m15rows, ts_ms)
            if not c or not (0 < c[1] < 1 and 0 < c[2] < 1): continue
            yes15 = c[2] + FEE(c[2]); no15 = (1 - c[1]) + FEE(1 - c[1])
            def hourly(K, side):
                if K not in strikes: return None
                h = at(*strikes[K], ts_ms)
                if not h: return None
                if side == "yes": return h[2] + FEE(h[2]) if 0 < h[2] < 1 else None
                return (1 - h[1]) + FEE(1 - h[1]) if 0 < h[1] < 1 else None
            def cap_cost(Sd):
                q = quote_at(bk.get(Sd, {}), ts)
                if not q: return None
                cap = round((1 - q[0]) - 0.01, 2)
                return cap + FEE(cap) if cap > 0 else None
            # spot bucket
            best = None
            for Sd, q in bk.items():
                qq = quote_at(q, ts)
                if qq:
                    mid = (qq[0] + qq[1]) / 2
                    if best is None or mid > best[0]: best = (mid, Sd)
            if best is None: continue
            S = best[1]; Su = S + 100
            cc = cap_cost(S); ya = hourly(S, "yes"); na = hourly(Su, "no")
            if cc is not None and ya is not None and na is not None:
                res["H_spot"].append((ct, 2 - cc - ya - na))
                if K15 >= Su:      res["M_spot"].append((ct, 2 - cc - ya - no15))     # NO@K15 replaces NO@Su
                elif K15 < S:      res["M_spot"].append((ct, 2 - cc - yes15 - na))    # YES@K15 replaces YES@Sd
            # K15-adjacent buckets
            Kb = int(K15 // 100 * 100)              # K15's own bucket floor
            lo = Kb - 100; hi = Kb + 100            # bucket below K15's bucket (gap above it), bucket above (gap below it)
            cc = cap_cost(lo); ya = hourly(lo, "yes"); na = hourly(lo + 100, "no")
            if cc is not None and ya is not None:
                if na is not None: res["H_adj_lo"].append((ct, 2 - cc - ya - na))
                res["M_adj_lo"].append((ct, 2 - cc - ya - no15))
            cc = cap_cost(hi); ya = hourly(hi, "yes"); na = hourly(hi + 100, "no")
            if cc is not None and na is not None:
                if ya is not None: res["H_adj_hi"].append((ct, 2 - cc - ya - na))
                res["M_adj_hi"].append((ct, 2 - cc - yes15 - na))

print(f"forward hours with 15M books: {hours}")
print(f"{'variant':10s} {'minutes':>8s} {'hours':>6s} {'lock>0':>8s} {'lock>=10c':>10s} {'hours w/ lock>=10c':>19s} {'mean lock (c)':>14s} {'p90 lock (c)':>13s} {'max (c)':>8s}")
for v in ("H_spot", "M_spot", "H_adj_lo", "M_adj_lo", "H_adj_hi", "M_adj_hi"):
    rs = res.get(v, [])
    if not rs: print(v, "none"); continue
    locks = [l for _, l in rs]
    pos = sum(1 for l in locks if l > 0); e10 = sum(1 for l in locks if l >= 0.10)
    h10 = len({ct for ct, l in rs if l >= 0.10})
    s = sorted(locks)
    print(f"{v:10s} {len(rs):8d} {len({ct for ct,_ in rs}):6d} {pos/len(rs):8.1%} {e10/len(rs):10.1%} {h10:19d} {st.mean(locks)*100:+14.1f} {s[int(0.9*(len(s)-1))]*100:+13.1f} {s[-1]*100:+8.1f}")
