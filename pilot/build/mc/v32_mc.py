"""Monte Carlo for V3.2 (continuous-requote pump-fader, E=0.10) at the $52 live balance.

Structure differs from the V1.1 box MC: a COMPLETED set pays $2 in every close, so the per-set outcome is
the lock (always positive in every sim fill and the one live set). The risks that replace V1.1's "miss":
  * fill-count variance (how many quoting windows fill per day),
  * leg failure (rest fills, a wing does not -> retry under the -10c lock floor, or a naked bucket-NO hold),
  * partial fills when sizing above 1 contract (a taker print of size s fills min(c, s) of our c).

Inputs (all measured, sources in brackets):
  lock per fill   -> the calibrated forward sim's 21 fills at E=0.10 (2026-08-30..09-04, 139 h)
                     [sim/v32_replay forward stage, dumped to forward_fills_dump.json]; OPT = as-sim,
                     BASE = OPT + base_B_corr shift, PESS = OPT - pess_B_corr - 2c  [forward.json];
                     plus the live set (2026-09-15 00:00Z) +10.07c ledger / +11.6c true.
                     Locks are LEDGER-basis (taker fee reserved on the maker rest); true locks run ~1.5-1.7c higher.
  P(fill | window) -> posterior Beta(1 + 21 + 1, 1 + (139-21) + 1): 21 sim fills in 139 forward hours + 1/1 live.
  windows/day      -> 23 (the $250/$500 bucket hours stand down; forward set averaged 23.2 h/day).
  leg failure      -> NOT measured (sim: wing depth never binds; live 1/1). Scenarios p_fail in {0.5%, 2%, 5%}.
                     Given failure: 70% the wing retry completes at a lock ~ U[-10c, +4c] (lock_floor -0.10 pin);
                     30% no wings -> naked bucket-NO at n: +(1-n) w.p. q, -n w.p. 1-q, q = n - 0.05 (adverse).
  print size       -> empirical from the replay lab's lagging-model fills on the live ms journals (fills.jsonl,
                      n=78: min 1, p10 1, median 4, p90 80). Fill count = min(c, size).
Sizing grid: contracts per set c in {1, 2, 5, 10, 20}; NOTE the proxy/pilot pin V32_MAX_CONTRACTS_PER_ORDER = 2
(Brad's lever) — c > 2 is a what-if. Capital: cost per contract-set ~ n + W ~ $1.88; sets settle hourly so
capital recycles; c is clipped to floor(balance / 1.90) each set.
Horizons 30 / 90 days from $52.11; N paths = 20000. Ruin = balance < one contract-set ($1.90).
Also reports days-to-n=30 (the falsifier's verdict gate) from the fill-rate posterior.
"""
import json, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
rng = np.random.default_rng(20260915)

B0 = 52.11
COST = 1.90
N = 20000
HORIZONS = (30, 90)
CONTRACTS = [1, 2, 5, 10, 20]
WINDOWS_PER_DAY = 23
P_FAIL = {"leg-fail 0.5%": 0.005, "leg-fail 2%": 0.02, "leg-fail 5%": 0.05}
LOCK_FLOOR_C = -10.0
LIVE_LEDGER_LOCK_C = 10.07

# ---------- inputs ----------
fwd = json.load(open(os.path.join(HERE, "forward_fills_dump.json")))
fills = fwd["fills_by_E"]["0.10"]
opt_locks = np.array([float(f["lock_c"]) for f in fills])
n_vals = np.array([float(f.get("n", 0.5)) for f in fills])
n_hours = int(fwd["n_hours"])
fj = json.load(open(os.path.join(HERE, "replay_all", "forward.json")))
base_shift = -float(fj["base_B_corr_c"])          # BASE = OPT - base_B_corr
pess_shift = -float(fj["pess_B_corr_c"]) - 2.0     # PESS = OPT - pess_B_corr - 2c
LOCKS = {
    "OPTIMISTIC": np.append(opt_locks, LIVE_LEDGER_LOCK_C),
    "BASE": np.append(opt_locks + base_shift, LIVE_LEDGER_LOCK_C),
    "PESSIMISTIC": np.append(opt_locks + pess_shift, LIVE_LEDGER_LOCK_C),
}
# print sizes from the replay lab's lagging fills on the live journals
sizes = []
for line in open(os.path.join(HERE, "replay_all", "fills.jsonl")):
    r = json.loads(line)
    if r.get("model") == "lag":
        sizes.append(float(r["print_size"]))
SIZES = np.array(sizes) if sizes else np.array([80.0])
# fill-rate posterior
A_FILL, B_FILL = 1 + len(opt_locks) + 1, 1 + (n_hours - len(opt_locks)) + 1


def simulate(c_target, lock_pool, p_fail, days, n=N):
    T = days * WINDOWS_PER_DAY
    p_fill = rng.beta(A_FILL, B_FILL, size=n)            # one fill-rate per path (parameter uncertainty)
    bal = np.full(n, B0)
    peak = bal.copy(); maxdd = np.zeros(n); ruined = np.zeros(n, bool)
    sets = np.zeros(n); fails = np.zeros(n); naked = np.zeros(n)
    for t in range(T):
        alive = ~ruined & (bal >= COST)
        fill = (rng.random(n) < p_fill) & alive
        if not fill.any():
            continue
        c = np.minimum(c_target, np.floor(bal / COST)).astype(int)
        size = rng.choice(SIZES, size=n)
        k = np.minimum(c, np.floor(size)).astype(int)     # contracts actually filled this set
        k = np.where(fill, np.maximum(k, 0), 0)
        lock = rng.choice(lock_pool, size=n) / 100.0
        idx = rng.integers(0, len(n_vals), size=n)
        nn = n_vals[idx]
        u = rng.random(n)
        failed = fill & (u < p_fail)
        retry = failed & (rng.random(n) < 0.70)
        nak = failed & ~retry
        retry_lock = rng.uniform(LOCK_FLOOR_C, 4.0, size=n) / 100.0
        q = np.clip(nn - 0.05, 0.02, 0.98)
        naked_pnl = np.where(rng.random(n) < q, 1.0 - nn, -nn)
        per = np.where(retry, retry_lock, np.where(nak, naked_pnl, lock))
        pnl = np.where(fill, per * k, 0.0)
        bal = bal + pnl
        sets += fill; fails += failed; naked += nak
        peak = np.maximum(peak, bal)
        maxdd = np.maximum(maxdd, (peak - bal) / peak)
        ruined |= bal < COST
    ret = bal / B0 - 1
    return {
        "median_bal": float(np.median(bal)), "p5": float(np.percentile(bal, 5)), "p95": float(np.percentile(bal, 95)),
        "mean_bal": float(np.mean(bal)), "p_loss": float(np.mean(bal < B0)), "p_ruin": float(np.mean(ruined)),
        "p_double": float(np.mean(bal >= 2 * B0)), "median_ret": float(np.median(ret)),
        "median_maxdd": float(np.median(maxdd)), "p95_maxdd": float(np.percentile(maxdd, 95)),
        "mean_sets": float(sets.mean()), "mean_fails": float(fails.mean()), "mean_naked": float(naked.mean()),
    }


def main():
    out = {"params": dict(B0=B0, COST=COST, N=N, WINDOWS_PER_DAY=WINDOWS_PER_DAY, CONTRACTS=CONTRACTS,
                          P_FAIL=P_FAIL, LOCK_FLOOR_C=LOCK_FLOOR_C, fill_posterior=[A_FILL, B_FILL],
                          base_shift_c=base_shift, pess_shift_c=pess_shift, n_forward_fills=int(len(opt_locks)),
                          n_hours=n_hours, size_n=int(len(SIZES))),
           "locks": {k: sorted(float(x) for x in v) for k, v in LOCKS.items()},
           "sizes": sorted(float(x) for x in SIZES), "grid": []}
    post = rng.beta(A_FILL, B_FILL, size=200000)
    fpd = post * WINDOWS_PER_DAY
    out["fill_rate"] = {"mean_per_window": float(post.mean()), "fills_per_day_mean": float(fpd.mean()),
                        "fills_per_day_ci90": [float(np.percentile(fpd, 5)), float(np.percentile(fpd, 95))]}
    # days to the falsifier's n=30 verdict gate (negative binomial over windows, one path per posterior draw)
    d30 = rng.negative_binomial(30, post[:20000]) + 30
    d30 = d30 / WINDOWS_PER_DAY
    out["days_to_n30"] = {"median": float(np.median(d30)), "p5": float(np.percentile(d30, 5)),
                          "p95": float(np.percentile(d30, 95))}
    print(f"forward fills E=0.10: {len(opt_locks)} in {n_hours} h; live set +{LIVE_LEDGER_LOCK_C}c (ledger)")
    for k, v in LOCKS.items():
        print(f"  {k:12s} lock mean {v.mean():+.2f}c  median {np.median(v):+.2f}c  min {v.min():+.2f}c  sd {v.std(ddof=1):.2f}c")
    print(f"P(fill|window) posterior mean {post.mean():.3f} -> {fpd.mean():.2f} fills/day (90% CI "
          f"{np.percentile(fpd,5):.2f}..{np.percentile(fpd,95):.2f}); print size n={len(SIZES)} median {np.median(SIZES):.0f}")
    print(f"days to n=30 sets: median {np.median(d30):.1f}  90% CI {np.percentile(d30,5):.1f}..{np.percentile(d30,95):.1f}")
    for days in HORIZONS:
        for est, pool in LOCKS.items():
            for fl, pf in P_FAIL.items():
                for c in CONTRACTS:
                    m = simulate(c, pool, pf, days)
                    m.update(days=days, estimate=est, fail=fl, p_fail=pf, contracts=c)
                    out["grid"].append(m)
        print(f"\n== {days} days from ${B0:.2f} ==")
        print(f"{'estimate':12s} {'leg-fail':13s} {'c':>3s} {'median$':>8s} {'p5$':>7s} {'p95$':>8s} {'P(loss)':>8s} {'P(ruin)':>8s} {'P(2x)':>6s} {'medDD':>6s} {'sets':>6s} {'fails':>6s}")
        for g in out["grid"]:
            if g["days"] != days:
                continue
            print(f"{g['estimate']:12s} {g['fail']:13s} {g['contracts']:3d} {g['median_bal']:8.2f} {g['p5']:7.2f} {g['p95']:8.2f} "
                  f"{g['p_loss']:8.1%} {g['p_ruin']:8.1%} {g['p_double']:6.1%} {g['median_maxdd']:6.1%} {g['mean_sets']:6.1f} {g['mean_fails']:6.2f}")
    json.dump(out, open(os.path.join(HERE, "v32_mc_results.json"), "w"), indent=1)
    print("\nwrote v32_mc_results.json")


if __name__ == "__main__":
    main()
