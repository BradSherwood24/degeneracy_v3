"""Wing ask-side depth at the V3.2 wings, T-15..T-5, every 30 s, using the pilot's own BookMirror.
Lower wing = buy YES at strike Sd-0.01 (depth = NO bids at price >= best_no_bid - k);
upper wing = buy NO at strike Su-0.01 (depth = YES bids at price >= best_yes_bid - k)."""
import gzip, json, glob, os, sys, statistics
from decimal import Decimal
sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot")
from service.book import BookMirror
JDIR = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\journals_v32"
KS = (0, 1, 2, 5)
pooled = {("thin", k): [] for k in KS}; pooled.update({("lo", k): [] for k in KS}); pooled.update({("hi", k): [] for k in KS})
def depth_within(bids: dict, k_cents: int):
    if not bids: return 0.0
    best = max(bids); floor = best - Decimal(k_cents) / 100
    return float(sum(s for p, s in bids.items() if p >= floor))
rows = []
for path in sorted(glob.glob(os.path.join(JDIR, "*.jsonl.gz"))):
    close = os.path.basename(path).split(".")[0]
    mirrors = {}; sd = su = None; close_epoch = None; last = None; mode = None
    samples = {("lo", k): [] for k in KS}; samples.update({("hi", k): [] for k in KS}); samples.update({("thin", k): [] for k in KS})
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"kalshi_ws"' in line[:40]:
                if "KXBTCD-" not in line: continue
                r = json.loads(line); o = r["obj"]; t = o.get("type"); m = o.get("msg", {}); tk = m.get("market_ticker", "")
                if not tk.startswith("KXBTCD-"): continue
                bm = mirrors.get(tk)
                if bm is None: bm = mirrors[tk] = BookMirror()
                if t == "orderbook_snapshot": bm.apply_snapshot(m)
                elif t == "orderbook_delta": bm.apply_delta(m)
                ts = r["local_ts"]
                if sd is not None and close_epoch and (last is None or ts - last >= 30):
                    tm = close_epoch - ts
                    if 300 <= tm <= 900:
                        last = ts
                        lo = mirrors.get(f"KXBTCD-{close_tag}-T{sd-1}.99"); hi = mirrors.get(f"KXBTCD-{close_tag}-T{su-1}.99")
                        if lo is not None and hi is not None and not lo.suspect and not hi.suspect:
                            for k in KS:
                                dl = depth_within(lo.no_bids, k); dh = depth_within(hi.yes_bids, k)
                                samples[("lo", k)].append(dl); samples[("hi", k)].append(dh); samples[("thin", k)].append(min(dl, dh))
                continue
            try: r = json.loads(line)
            except Exception: continue
            k = r.get("kind")
            if k == "window_meta":
                mode = r["obj"].get("effective_mode")
                bl = r["obj"].get("buckets") or []
                close_tag = bl[0]["event_ticker"].split("-")[1] if bl else None
            elif k == "v32_eval":
                o = r["obj"]
                if o.get("spot_Sd") is not None:
                    sd, su = int(o["spot_Sd"]), int(o["spot_Su"]); tm = o.get("t_minus_s")
                    if tm is not None: close_epoch = r["local_ts"] + tm
    row = {"close": close, "mode": mode, "n_samples": len(samples[("thin", 0)])}
    for k in KS:
        v = samples[("thin", k)]
        row[f"thin_+{k}c_p10"] = round(sorted(v)[int(0.1*(len(v)-1))]) if v else None
        row[f"thin_+{k}c_med"] = round(statistics.median(v)) if v else None
        row[f"thin_+{k}c_min"] = round(min(v)) if v else None
        for key in (("lo", k), ("hi", k), ("thin", k)): pooled[key] += samples[key]
    rows.append(row); print(json.dumps(row), flush=True)
print("\n== pooled (thinner wing = min(lower, upper) at each sample) ==")
for k in KS:
    v = sorted(pooled[("thin", k)])
    if v: print(f"within +{k}c of the ask: p10={v[int(.1*(len(v)-1))]:.0f}  p25={v[int(.25*(len(v)-1))]:.0f}  p50={v[len(v)//2]:.0f}  p90={v[int(.9*(len(v)-1))]:.0f}  min={v[0]:.0f}  (n={len(v)})")
for side in ("lo", "hi"):
    v = sorted(pooled[(side, 2)])
    if v: print(f"{side} wing within +2c: p10={v[int(.1*(len(v)-1))]:.0f} p50={v[len(v)//2]:.0f} p90={v[int(.9*(len(v)-1))]:.0f}")
json.dump(rows, open("v32_wing_depth_scan.json", "w"), indent=1)
