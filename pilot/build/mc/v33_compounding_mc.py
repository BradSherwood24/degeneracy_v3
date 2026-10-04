"""Compounding Monte Carlo for Brad's 2026-10-04 question: 11 rungs, 1/3 of the balance per full ladder,
lots per rung scaled to keep that ratio. Bootstrap of the live V3.3 armed-window outcomes at 1 lot/rung.

Sample = the 48 armed V3.3 windows with ledger rows 09-30 .. 10-04 22:00Z, EXCLUDING 10-02 02:00Z (the pre-gate
11-naked incident; the defect is fixed) and INCLUDING 10-03 02:00Z as -$0.40 (2 naked, the pin event; also fixed
by gates A-E, kept as an honest tail). P&L is the ledger realized_delta (= venue revenue - cost - fees).
"""
import math, random, statistics, sys, json

random.seed(20261004)
NONZERO = [  # (pnl at 1 lot/rung, lots filled, one_legged_contracts)
    (0.3110, 2, 0),    # 09-30 22:00Z
    (0.0891, 1, 0),    # 10-03 00:00Z
    (0.3158, 3, 0),    # 10-03 01:00Z
    (-0.40, 2, 2),     # 10-03 02:00Z naked (fixed defect; kept as tail)
    (-0.2256, 8, 0),   # 10-04 06:00Z adverse repricing
    (1.5797, 11, 0),   # 10-04 14:00Z
    (0.2662, 3, 0),    # 10-04 18:00Z
    (1.6421, 11, 0),   # 10-04 20:00Z
]
N_WINDOWS = 48
SAMPLE = NONZERO + [(0.0, 0, 0)] * (N_WINDOWS - len(NONZERO))
WINDOWS_PER_DAY = 23
LADDER_COST = 22.0        # $ tied up by one full 11-rung ladder at 1 lot/rung (Brad's figure)
FRACTION = 1 / 3
B0 = 69.41
PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
HORIZONS = (30, 60, 90)


def lots_per_rung(balance, cap):
    m = int((balance * FRACTION) // LADDER_COST)
    return max(1, min(m, cap))


def run(cap=10**9, edge_scale=1.0, absorb_cap_lots=None):
    """Returns dict of results. cap = max lots/rung (proxy cap etc.), edge_scale multiplies every non-zero
    window P&L (sensitivity), absorb_cap_lots = max total lots filled per window (print-size wall)."""
    end = {h: [] for h in HORIZONS}
    m_end = {h: [] for h in HORIZONS}
    s4_kill = 0          # any UTC day with loss > $3.00 (current S4 pin, in dollars)
    oneleg_kill = 0      # any one-legged event with > 2 contracts (current pin, in contracts)
    t_to = {200: [], 500: [], 1000: []}
    maxdd = []
    for _ in range(PATHS):
        b = B0; peak = b; dd = 0.0
        s4 = False; ol = False
        reached = {k: None for k in t_to}
        for day in range(1, HORIZONS[-1] + 1):
            day_pnl = 0.0
            for _w in range(WINDOWS_PER_DAY):
                pnl1, lots1, naked1 = random.choice(SAMPLE)
                if lots1 == 0:
                    continue
                m = lots_per_rung(b, cap)
                if absorb_cap_lots is not None and lots1 * m > absorb_cap_lots:
                    m_eff = max(1, absorb_cap_lots // lots1)
                else:
                    m_eff = m
                pnl = pnl1 * edge_scale * m_eff
                b += pnl; day_pnl += pnl
                if naked1 * m_eff > 2:
                    ol = True
                peak = max(peak, b); dd = max(dd, peak - b)
                for k in t_to:
                    if reached[k] is None and b >= k:
                        reached[k] = day
            if day_pnl < -3.0:
                s4 = True
            if day in end:
                end[day].append(b); m_end[day].append(lots_per_rung(b, cap))
        s4_kill += s4; oneleg_kill += ol; maxdd.append(dd)
        for k in t_to:
            t_to[k].append(reached[k])
    q = lambda xs, p: sorted(xs)[int(p * (len(xs) - 1))]
    out = {"paths": PATHS}
    for h in HORIZONS:
        out[f"d{h}"] = {"p10": round(q(end[h], .10), 1), "p50": round(q(end[h], .50), 1), "p90": round(q(end[h], .90), 1),
                        "mean": round(statistics.mean(end[h]), 1), "lots_per_rung_p50": q(m_end[h], .5)}
    out["P_day_loss_gt_3"] = round(s4_kill / PATHS, 3)
    out["P_oneleg_gt_2_contracts"] = round(oneleg_kill / PATHS, 3)
    out["max_drawdown_p50"] = round(q(maxdd, .5), 2); out["max_drawdown_p90"] = round(q(maxdd, .9), 2)
    for k, v in t_to.items():
        hit = [d for d in v if d is not None]
        out[f"days_to_{k}"] = {"P_within_90d": round(len(hit) / PATHS, 3), "median_days": (statistics.median(hit) if hit else None)}
    return out


if __name__ == "__main__":
    mean_w = sum(p for p, _, _ in SAMPLE) / N_WINDOWS
    print(f"sample: {N_WINDOWS} windows, {len(NONZERO)} with fills; mean P&L/window at 1 lot/rung = {mean_w*100:+.2f}c "
          f"-> {mean_w*WINDOWS_PER_DAY:+.3f} $/day; ladder cost {LADDER_COST}, fraction {FRACTION:.3f}, B0 {B0}")
    sd = statistics.pstdev([p for p, _, _ in SAMPLE])
    print(f"per-window sd {sd:.3f}; SE of the mean over 48 windows {sd/math.sqrt(48)*100:.1f}c (the mean is NOT well known)")
    scen = {
        "A_unconstrained": dict(),
        "B_proxy_cap_2": dict(cap=2),
        "C_absorb_100_lots_window": dict(absorb_cap_lots=100),
        "D_half_edge": dict(edge_scale=0.5),
        "E_flat_1_lot": dict(cap=1),
    }
    res = {k: run(**v) for k, v in scen.items()}
    print(json.dumps(res, indent=1))
