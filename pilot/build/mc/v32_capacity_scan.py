"""Capacity scan for V3.2 from the live ms journals (pilot/journals_v32/*.jsonl.gz).

Per armed/dry window, T-15..T-5, using window_meta (spot bucket from v32_eval records; wings = the KXBTCD
strikes at Sd-0.01 and Su-0.01):
  (a) spot-bucket YES taker prints: count, total lots, size distribution, lots at price >= 0.60 (a proxy
      for prints deep enough to reach a pump-fader offer) -> the maker CAPTURE CEILING per hour.
  (b) wing ask-side depth at the two wing strikes: lots available within +0c, +1c, +2c, +5c of best ask
      (YES ask for the lower wing, NO ask = 1 - YES bid for the upper wing), sampled every 30 s.
Reads gz only; one file at a time.
"""
import gzip, json, os, sys, glob, statistics, collections
from decimal import Decimal

JDIR = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32"
files = sorted(glob.glob(os.path.join(JDIR, "*.jsonl.gz")))
out_rows = []
all_sizes = []; all_depths = {k: [] for k in ("0", "1", "2", "5")}

def book_apply(book, msg):
    # kalshi ws orderbook_snapshot / orderbook_delta -> book[ticker] = {"yes": {price_cents: qty}, "no": {...}}
    t = msg.get("type")
    m = msg.get("msg", {})
    tk = m.get("market_ticker")
    if not tk: return
    b = book.setdefault(tk, {"yes": {}, "no": {}})
    if t == "orderbook_snapshot":
        b["yes"] = {int(round(float(p)*100)): float(q) for p, q in (m.get("yes_dollars_fp") or [])}
        b["no"] = {int(round(float(p)*100)): float(q) for p, q in (m.get("no_dollars_fp") or [])}
    elif t == "orderbook_delta":
        side = m.get("side"); p = int(round(float(m.get("price_dollars"))*100)); d = float(m.get("delta_fp", 0))
        lv = b[side]; lv[p] = lv.get(p, 0.0) + d
        if lv[p] <= 0: lv.pop(p, None)

def ask_depth(levels_yes, levels_no, side):
    """Ask-side depth for buying `side` of a strike: buying YES takes NO-bid levels (Kalshi books list bids
    per side: yes dict = YES bids, no dict = NO bids). Best YES ask = 100 - best NO bid. Returns list of
    (ask_price_cents, qty) ascending."""
    if side == "yes":
        return sorted(((100 - p, q) for p, q in levels_no.items()), key=lambda x: x[0])
    return sorted(((100 - p, q) for p, q in levels_yes.items()), key=lambda x: x[0])

def within(asks, cents):
    if not asks: return 0.0
    best = asks[0][0]
    return sum(q for p, q in asks if p <= best + cents)

for path in files:
    name = os.path.basename(path)
    close = name.split(".")[0]
    book = {}; spot = None; sd = su = None; mode = None; close_epoch = None
    prints = []  # (ts, price, size)
    depth_samples = collections.defaultdict(list); last_sample = None
    strike_lo = strike_hi = None
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try: r = json.loads(line)
            except Exception: continue
            k = r.get("kind"); ts = r.get("local_ts")
            if k == "window_meta":
                mode = r["obj"].get("effective_mode")
                ct = r["obj"].get("close_time") or close
            elif k == "v32_eval":
                o = r["obj"]
                if o.get("spot_Sd") is not None:
                    sd, su = int(o["spot_Sd"]), int(o["spot_Su"])
                    tm = o.get("t_minus_s")
                    if tm is not None: close_epoch = ts + tm
            elif k == "kalshi_ws":
                msg = r["obj"] if "obj" in r else r.get("msg", r)
                t = msg.get("type")
                if t in ("orderbook_snapshot", "orderbook_delta"):
                    book_apply(book, msg)
                elif t == "trade":
                    m = msg.get("msg", {})
                    tk = m.get("market_ticker", "")
                    if sd is not None and tk.startswith("KXBTC-") and tk.endswith(f"-B{sd+50}"):
                        if m.get("taker_side") == "yes":
                            prints.append((ts, int(round(float(m.get("yes_price_dollars", 0))*100)), float(m.get("count_fp") or 0)))
                # sample wing depth every 30 s once spot known
                if sd is not None and close_epoch and (last_sample is None or ts - last_sample >= 30):
                    tminus = close_epoch - ts
                    if 300 <= tminus <= 900:
                        last_sample = ts
                        lo = f"-T{sd-1}.99"; hi = f"-T{su-1}.99"
                        for tk, b in book.items():
                            if tk.startswith("KXBTCD-") and tk.endswith(lo):
                                asks = ask_depth(b["yes"], b["no"], "yes")
                                for c in ("0", "1", "2", "5"): depth_samples[("lo", c)].append(within(asks, int(c)))
                            elif tk.startswith("KXBTCD-") and tk.endswith(hi):
                                asks = ask_depth(b["yes"], b["no"], "no")
                                for c in ("0", "1", "2", "5"): depth_samples[("hi", c)].append(within(asks, int(c)))
    # restrict prints to T-15..T-5
    if close_epoch:
        prints = [p for p in prints if 300 <= close_epoch - p[0] <= 900]
    sizes = [s for _, _, s in prints]
    deep = [s for _, pr, s in prints if pr >= 60]
    all_sizes += sizes
    row = {"close": close, "mode": mode, "spot_Sd": sd, "prints": len(prints), "lots": round(sum(sizes), 1),
           "lots_ge60": round(sum(deep), 1), "size_med": statistics.median(sizes) if sizes else None,
           "size_max": max(sizes) if sizes else None}
    for side in ("lo", "hi"):
        for c in ("0", "1", "2", "5"):
            v = depth_samples.get((side, c), [])
            row[f"{side}_depth_+{c}c_med"] = round(statistics.median(v), 0) if v else None
            row[f"{side}_depth_+{c}c_min"] = round(min(v), 0) if v else None
            if v: all_depths[c] += v
    out_rows.append(row)
    print(json.dumps(row), flush=True)

print("\n== pooled ==")
if all_sizes:
    s = sorted(all_sizes)
    q = lambda p: s[min(len(s)-1, int(p*(len(s)-1)))]
    print(f"spot-bucket YES prints T-15..T-5: n={len(s)} lots total={sum(s):.0f} per-window mean lots={sum(s)/len(out_rows):.0f} "
          f"size p50={q(.5)} p90={q(.9)} p99={q(.99)} max={s[-1]}")
for c in ("0", "1", "2", "5"):
    v = sorted(all_depths[c])
    if v:
        print(f"wing ask depth within +{c}c: p10={v[int(.1*(len(v)-1))]:.0f} p50={v[len(v)//2]:.0f} p90={v[int(.9*(len(v)-1))]:.0f} min={v[0]:.0f} (n={len(v)} samples)")
json.dump(out_rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "v32_capacity_scan.json"), "w"), indent=1)
