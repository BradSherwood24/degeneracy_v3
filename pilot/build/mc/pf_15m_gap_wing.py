"""15M AS THE OUTSIDE WING (Brad 2026-09-15): when the 15M strike K15 sits OUTSIDE the spot bucket, replace the hourly wing on
that side with the 15M leg of the same direction. Payoff min $2, $3 in the gap between the bucket edge and K15.
  K15 >= Su : NO@K15 replaces NO@Su  (gap = [Su, K15))      K15 < Sd : YES@K15 replaces YES@Sd  (gap = [K15, Sd))
The 15M leg covers a SUPERSET of the hourly leg's outcomes, so at fair prices it costs MORE by exactly P(close in gap). The question is
whether the market prices it that way: cost_diff = ask(15M leg) - ask(hourly leg) vs the realized gap hit rate. Net = P(gap) - cost_diff.
Samples every 60 s over T-15..T-5, forward hours 8/30-9/04 (tob incl. the 15M + range candles for the spot bucket + results)."""
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
                m15[m["close_time"]] = (float(m["floor_strike"]), m["result"], m["ticker"])
    return sres, m15

tob_index = {}
for p in glob.glob(os.path.join(TOB, "*.json.gz")):
    name = os.path.basename(p).split(".")[0]
    tob_index[f"{name[:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}Z"] = p
days = sorted(d[:10] for d in (os.path.basename(x) for x in glob.glob(f"{R.ROOT}/1-hour-range/candles/2026-*.jsonl")) if d[:10] >= "2026-08-30")
print("forward days:", days)
samples = []; hours = 0; hours15 = 0; pos = collections.Counter()
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
        if ct not in tob_index: continue
        cts = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
        with gzip.open(tob_index[ct], "rt", encoding="utf-8") as f: mk = json.load(f)["markets"]
        strikes = {}; m15rows = None
        for tk, v in mk.items():
            if tk.startswith("KXBTC15M"):
                if v["tob"]: m15rows = (v["tob"], [r[0] for r in v["tob"]])
                continue
            K = strike_of(tk)
            if K is not None and v["tob"]: strikes[K] = (v["tob"], [r[0] for r in v["tob"]])
        if not strikes: continue
        hours += 1
        info15 = m15.get(ct)
        if info15 is None or m15rows is None: continue
        hours15 += 1
        K15, r15, _ = info15
        for tm in range(900, 299, -60):            # T-15 .. T-5 every 60 s
            ts = cts - tm; ts_ms = ts * 1000
            # spot bucket = highest yes-mid bucket at this minute (candle close of the minute containing ts)
            best = None
            for Sd, q in bk.items():
                keys = [k for k in q if k <= ts + 60]
                if not keys: continue
                yb, ya = q[max(keys)]
                mid = (yb + ya) / 2
                if best is None or mid > best[0]: best = (mid, Sd)
            if best is None: continue
            Sd = best[1]; Su = Sd + 100
            if K15 >= Su: side = "above"; gap = K15 - Su; Kh = Su
            elif K15 < Sd: side = "below"; gap = Sd - K15; Kh = Sd
            else: pos["inside"] += 1; continue
            pos[side] += 1
            if Kh not in strikes: continue
            h = at(*strikes[Kh], ts_ms); c = at(*m15rows, ts_ms)
            if not h or not c: continue
            # tob rows: (ts_ms, yes_bid, yes_ask)
            if side == "above":   # NO legs: ask = 1 - yes_bid
                if not (0 < h[1] < 1 and 0 < c[1] < 1): continue
                cost_h = (1 - h[1]) + FEE(1 - h[1]); cost_15 = (1 - c[1]) + FEE(1 - c[1])
            else:                 # YES legs: ask
                if not (0 < h[2] < 1 and 0 < c[2] < 1): continue
                cost_h = h[2] + FEE(h[2]); cost_15 = c[2] + FEE(c[2])
            # the OTHER hourly wing + the bucket cap -> is the hourly set enterable at this minute (lock at cap >= 0)?
            Ko = Sd if side == "above" else Su
            if Ko not in strikes: continue
            o = at(*strikes[Ko], ts_ms)
            if not o: continue
            if side == "above":
                if not (0 < o[2] < 1): continue
                cost_o = o[2] + FEE(o[2])
            else:
                if not (0 < o[1] < 1): continue
                cost_o = (1 - o[1]) + FEE(1 - o[1])
            keys = [k for k in bk[Sd] if k <= ts + 60]
            yb_b = bk[Sd][max(keys)][0]
            cap = round((1 - yb_b) - 0.01, 2)
            if cap <= 0: continue
            W_h = cost_h + cost_o
            lock_cap_h = 2 - (cap + FEE(cap)) - W_h
            rb = bres.get((ct, Sd)); rd = sres.get((ct, Sd)); ru = sres.get((ct, Su))
            if rb is None or rd is None or ru is None: continue
            bucket_no = rb == "no"; yes_sd = rd == "yes"; no_su = ru == "no"; yes_15 = r15 == "yes"
            if side == "above": pay = int(bucket_no) + int(yes_sd) + int(not yes_15)
            else:               pay = int(bucket_no) + int(yes_15) + int(no_su)
            samples.append(dict(ct=ct, tm=tm, side=side, gap=gap, cost_h=cost_h, cost_15=cost_15, diff=cost_15 - cost_h, pay=pay, lock_cap_h=lock_cap_h, W_h=W_h))

print(f"hours {hours} with 15M books+result {hours15}; minute-samples K15 position: {dict(pos)}")
print(f"samples with both legs priced + results: {len(samples)}  (payoffs: {collections.Counter(s['pay'] for s in samples)})")
def bucketize(g):
    for lo, hi, lab in ((0, 25, "<$25"), (25, 50, "$25-50"), (50, 100, "$50-100"), (100, 200, "$100-200"), (200, 1e9, ">$200")):
        if lo <= g < hi: return lab
print(f"\n{'gap':>9s} {'n':>5s} {'hours':>5s} {'15M-hourly cost (c)':>20s} {'P(pay=3)':>9s} {'gap value (c)':>13s} {'NET (c)':>8s}  {'pay<2':>5s}")
for lab in ("<$25", "$25-50", "$50-100", "$100-200", ">$200"):
    rs = [s for s in samples if bucketize(s["gap"]) == lab]
    if not rs: continue
    diff = st.mean(s["diff"] for s in rs) * 100; p3 = st.mean(1 if s["pay"] == 3 else 0 for s in rs)
    print(f"{lab:>9s} {len(rs):5d} {len({s['ct'] for s in rs}):5d} {diff:+20.2f} {p3:9.1%} {p3*100:13.1f} {p3*100-diff:+8.1f}  {sum(1 for s in rs if s['pay']<2):5d}")
rs = samples
if rs:
    diff = st.mean(s["diff"] for s in rs) * 100; p3 = st.mean(1 if s["pay"] == 3 else 0 for s in rs)
    print(f"{'ALL':>9s} {len(rs):5d} {len({s['ct'] for s in rs}):5d} {diff:+20.2f} {p3:9.1%} {p3*100:13.1f} {p3*100-diff:+8.1f}  {sum(1 for s in rs if s['pay']<2):5d}")
for side in ("above", "below"):
    rs = [s for s in samples if s["side"] == side]
    if rs:
        diff = st.mean(s["diff"] for s in rs) * 100; p3 = st.mean(1 if s["pay"] == 3 else 0 for s in rs)
        print(f"{side:>9s} {len(rs):5d} {len({s['ct'] for s in rs}):5d} {diff:+20.2f} {p3:9.1%} {p3*100:13.1f} {p3*100-diff:+8.1f}")
json.dump(samples, open(os.path.join(SCRATCH, "journals", "pf_15m_gap_wing.json"), "w"))

print("")
print("== CONDITIONAL on the hourly set being enterable at that minute (lock at cap >= 0) ==")
for thr, lab in ((0.0, "lock@cap >= 0"), (0.10, "lock@cap >= 10c (E)")):
    rs = [x for x in samples if x["lock_cap_h"] >= thr]
    if not rs:
        print(lab, "none")
        continue
    diff = st.mean(x["diff"] for x in rs) * 100
    p3 = st.mean(1 if x["pay"] == 3 else 0 for x in rs)
    print(f"{lab}: n={len(rs)} hours={len({x['ct'] for x in rs})}  15M-hourly cost {diff:+.2f}c  P(pay=3) {p3:.1%}  NET {p3*100-diff:+.1f}c  pay<2: {sum(1 for x in rs if x['pay']<2)}")
    for glab in ("<$25", "$25-50", "$50-100", "$100-200", ">$200"):
        g = [x for x in rs if bucketize(x["gap"]) == glab]
        if g:
            d2 = st.mean(x["diff"] for x in g) * 100
            q3 = st.mean(1 if x["pay"] == 3 else 0 for x in g)
            print(f"    {glab:>9s} n={len(g):4d} hours={len({x['ct'] for x in g}):3d} cost {d2:+6.2f}c  P3 {q3:6.1%}  NET {q3*100-d2:+6.1f}c")
