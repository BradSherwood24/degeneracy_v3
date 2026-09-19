"""DRY SPELLS of the spot-bucket pump-fader on TRAIN (42 range days, clean window T-15..T-5, exact fees).
Question (Brad 2026-09-19): does the sim show 36+ hour stretches with no fill? Records the HOURS with a fill at each E
(stale minute-candle model: rest at n = 2-E-W budget, fill = yes print through the level within the arm minute), then
gaps between fill hours in QUOTING hours, longest gaps, per-day fill counts, share of zero-fill days.
Fill TIMING only -- the lock/P&L of the stale model is not the question (live is the requote variant)."""
import sys, os, collections, json
from datetime import datetime
SCRATCH = r"C:/Users/Brads/AppData/Local/Temp/claude/C--Users-Brads-Python-stuff-degeneracy-v3/e7acb14c-8903-4c04-ae00-7d39ed05e898/scratchpad"
sys.path.insert(0, os.path.join(SCRATCH, "range"))
import rangelab as R
FEE = R.KALSHI_FEE_EXACT
ES = [0.10, 0.15, 0.20, 0.25]
K0, K1 = 15, 5
def solve_n(budget, cap):
    n = min(round(budget, 2), cap)
    while n >= 0.01 and n + FEE(n) > budget + 1e-9: n = round(n - 0.01, 2)
    return n if n >= 0.01 else None
def load_prints(day):
    if not (os.path.exists(f"{R.ROOT}/1-hour-range/markets/{day}.jsonl") and os.path.exists(f"{R.ROOT}/1-hour-range/trades/{day}.jsonl.gz")): return None
    mkt = {}
    for m in R.markets("1-hour-range", day):
        if m.get("floor_strike") is None or m.get("cap_strike") is None or abs(float(m["cap_strike"]) - float(m["floor_strike"]) - 99.99) > 0.5: continue
        mkt[m["ticker"]] = (m["close_time"], round(float(m["floor_strike"])))
    out = collections.defaultdict(list)
    for r in R.trades("1-hour-range", day):
        k = mkt.get(r["ticker"])
        if not k: continue
        for t in r["trades"]:
            out[k].append((int(datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp()), float(t["yes_price_dollars"]), t.get("taker_side"), float(t.get("count_fp") or 0)))
    for v in out.values(): v.sort()
    return out
def wings(g, Sd, ts):
    qd, qu = g["h"].get(Sd, {}).get(ts), g["h"].get(Sd + 100, {}).get(ts)
    if not (qd and qu) or not (0 < qd[1] <= 1 and 0 <= qu[0] < 1): return None
    ya, nu = qd[1], 1 - qu[0]
    if not (0 < ya < 1 and 0 < nu < 1): return None
    return (ya, nu)
hours = []  # (close_ts, close_iso)
fill_hours = {E: set() for E in ES}
for day in R.train_days():
    rp = load_prints(day)
    if rp is None: continue
    for ct, g in R.day_ladders(day).items():
        cts = g["close_ts"]; hours.append((cts, ct))
        allgrid = sorted({t for q in g["r"].values() for t in q})
        grid = [t for t in allgrid if cts - 60 * K0 <= t <= cts - 60 * K1]
        for E in ES:
            for ts in grid:
                s, sm, qB = None, -1, None
                for lvl, q in g["r"].items():
                    if ts in q:
                        m = (q[ts][0] + q[ts][1]) / 2
                        if m > sm: sm, s, qB = m, lvl, q[ts]
                if s is None or not (0 < qB[1] <= 1 and 0 <= qB[0] <= qB[1]): continue
                w = wings(g, s, ts)
                if w is None: continue
                W = w[0] + FEE(w[0]) + w[1] + FEE(w[1])
                n = solve_n(2.0 - E - W, round((1 - qB[0]) - 0.01, 2))
                if n is None: continue
                if any(ts < pts <= ts + 60 and sd == "yes" and yp > (1 - n) + 1e-9 for pts, yp, sd, sz in rp.get((ct, s), [])):
                    fill_hours[E].add(cts); break
hours.sort()
print(f"train quoting hours {len(hours)} over {len({h[1][:10] for h in hours})} days ({hours[0][1][:10]}..{hours[-1][1][:10]}); note gaps in the corpus itself are skipped (gaps counted in QUOTING hours)")
idx = {cts: i for i, (cts, _) in enumerate(hours)}
res = {}
for E in ES:
    fh = sorted(fill_hours[E]); pos = [idx[c] for c in fh]
    gaps = [b - a for a, b in zip(pos, pos[1:])]           # quoting hours between consecutive fills
    # dry runs = gap-1 hours with no fill between fills; also leading/trailing
    runs = sorted([g - 1 for g in gaps] + [pos[0], len(hours) - 1 - pos[-1]]) if pos else []
    byday = collections.Counter(c[:10] for c in (hours[i][1] for i in pos))
    days = sorted({h[1][:10] for h in hours}); zero = [d for d in days if byday[d] == 0]
    wk = collections.Counter(datetime.fromisoformat(hours[i][1].replace("Z", "+00:00")).strftime("%G-W%V") for i in pos)
    res[E] = dict(fills=len(fh), per_day=len(fh) / len(days), longest_dry=max(runs) if runs else None,
                  runs_ge24=sum(1 for r in runs if r >= 24), runs_ge36=sum(1 for r in runs if r >= 36), runs_ge48=sum(1 for r in runs if r >= 48),
                  zero_days=len(zero), days=len(days), zero_day_list=zero, per_week=dict(sorted(wk.items())),
                  gap_pcts={q: sorted(runs)[int(q * (len(runs) - 1))] for q in (0.5, 0.75, 0.9, 0.95)} if runs else {})
    r = res[E]
    print(f"\nE={E*100:.0f}c: fills {r['fills']} ({r['per_day']:.2f}/day over {r['days']} days); zero-fill days {r['zero_days']}/{r['days']}: {r['zero_day_list']}")
    print(f"   dry runs (quoting hours with no fill between fills): median {r['gap_pcts'].get(0.5)} p75 {r['gap_pcts'].get(0.75)} p90 {r['gap_pcts'].get(0.9)} p95 {r['gap_pcts'].get(0.95)} LONGEST {r['longest_dry']}; runs >=24h: {r['runs_ge24']}, >=36h: {r['runs_ge36']}, >=48h: {r['runs_ge48']}")
    print(f"   fills per ISO week: {r['per_week']}")
json.dump({str(E): {**v, "fill_hours": sorted(fill_hours[E])} for E, v in res.items()}, open(os.path.join(SCRATCH, "range", "pf_dry_spells.json"), "w"), default=str)
