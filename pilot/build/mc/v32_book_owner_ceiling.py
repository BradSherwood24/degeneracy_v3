"""Book-owner ceiling for Brad's ladder idea (2026-09-15): if we rested bucket-NO at EVERY profitable level with
unlimited size, and took the wings at the ask (no slippage) the instant each taker print hit us, how much lock
per hour is there in the spot bucket's YES taker flow, T-15..T-5?

Per YES taker print (price p, size s) on the spot bucket at time t, with W(t) = yes_ask(strike Sd-0.01)+fee +
no_ask(strike Su-0.01)+fee from the pilot's BookMirror on the strike books:
  n = 1 - p (our NO rest that this print would have hit), lock(p,t) = 2 - (n + fee(n)) - W(t)   [pinned law shape]
Two bounds:
  A (taker pays the printed price): sum over prints of s * lock(p,t) for lock >= E_min  -- optimistic ladder
  B (taker fills our lowest profitable level): sum of s over prints with lock(p,t) >= E_min, times E_min   -- pessimistic
Also the lots and per-day (x23) equivalents. Wing slippage NOT charged (see depth scan: +2c median 2,800 lots).
"""
import gzip, json, glob, os, sys, math
from decimal import Decimal
sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot")
from service.book import BookMirror
JDIR = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32"
EMINS = (0.04, 0.08, 0.10)

def fee(p):  # exact taker fee per contract, ceil to $0.0001
    return math.ceil(0.07 * p * (1 - p) * 10000) / 10000

rows = []
for path in sorted(glob.glob(os.path.join(JDIR, "*.jsonl.gz"))):
    close = os.path.basename(path).split(".")[0]
    mirrors = {}; sd = su = None; close_epoch = None; tag = None; mode = None
    prints = []  # (ts, p, s, W)
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"kalshi_ws"' in line[:40]:
                if "KXBTCD-" in line:
                    r = json.loads(line); o = r["obj"]; t = o.get("type"); m = o.get("msg", {}); tk = m.get("market_ticker", "")
                    if tk.startswith("KXBTCD-"):
                        bm = mirrors.get(tk)
                        if bm is None: bm = mirrors[tk] = BookMirror()
                        if t == "orderbook_snapshot": bm.apply_snapshot(m)
                        elif t == "orderbook_delta": bm.apply_delta(m)
                    continue
                if sd is None or '"trade"' not in line or f"-B{sd+50}" not in line: continue
                r = json.loads(line); o = r["obj"]; m = o.get("msg", {})
                if o.get("type") != "trade" or m.get("taker_side") != "yes": continue
                tk = m.get("market_ticker", "")
                if not (tk.startswith("KXBTC-") and tk.endswith(f"-B{sd+50}")): continue
                ts = r["local_ts"]
                if not close_epoch or not (300 <= close_epoch - ts <= 900): continue
                lo = mirrors.get(f"KXBTCD-{tag}-T{sd-1}.99"); hi = mirrors.get(f"KXBTCD-{tag}-T{su-1}.99")
                if lo is None or hi is None or lo.suspect or hi.suspect: continue
                ya = lo.best_yes_ask(); na = hi.best_no_ask()
                if ya is None or na is None: continue
                W = float(ya.price) + fee(float(ya.price)) + float(na.price) + fee(float(na.price))
                prints.append((ts, float(m["yes_price_dollars"]), float(m.get("count_fp") or 0), W))
                continue
            try: r = json.loads(line)
            except Exception: continue
            k = r.get("kind")
            if k == "window_meta":
                mode = r["obj"].get("effective_mode"); bl = r["obj"].get("buckets") or []
                tag = bl[0]["event_ticker"].split("-")[1] if bl else None
            elif k == "v32_eval":
                o = r["obj"]
                if o.get("spot_Sd") is not None:
                    sd, su = int(o["spot_Sd"]), int(o["spot_Su"]); tm = o.get("t_minus_s")
                    if tm is not None: close_epoch = r["local_ts"] + tm
    row = {"close": close, "mode": mode, "prints": len(prints), "lots": round(sum(s for _, _, s, _ in prints))}
    for emin in EMINS:
        A = 0.0; lots = 0.0; best = 0.0
        for ts, p, s, W in prints:
            n = 1 - p
            lock = 2 - (n + fee(n)) - W
            if lock >= emin:
                A += s * lock; lots += s; best = max(best, lock)
        row[f"E{int(emin*100)}_lots"] = round(lots); row[f"E{int(emin*100)}_A$"] = round(A, 2)
        row[f"E{int(emin*100)}_B$"] = round(lots * emin, 2); row[f"E{int(emin*100)}_maxlock_c"] = round(best * 100, 1)
    rows.append(row); print(json.dumps(row), flush=True)

n = len(rows)
print(f"\n== pooled over {n} windows (per-day = x23 windows) ==")
for emin in EMINS:
    e = int(emin * 100)
    lots = sum(r[f"E{e}_lots"] for r in rows) / n; A = sum(r[f"E{e}_A$"] for r in rows) / n; B = sum(r[f"E{e}_B$"] for r in rows) / n
    hit = sum(1 for r in rows if r[f"E{e}_lots"] > 0)
    print(f"E_min={e}c: windows with any profitable flow {hit}/{n}; mean profitable lots/window {lots:.0f}; "
          f"A ${A:.2f}/window (${A*23:.0f}/day)  B ${B:.2f}/window (${B*23:.0f}/day)")
json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "v32_book_owner_ceiling.json"), "w"), indent=1)
