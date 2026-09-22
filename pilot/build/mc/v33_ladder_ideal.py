"""V3.3 LADDER, IDEAL FILLS (Brad 2026-09-22): "1 contract order on the books at levels where profit is 5
through 15 cents ... any entry right now would then enter at that bucket price minus 5 cents through as far
as it walks the ladder."

For every ARMED window with a spot bucket (ledger rows since the 2026-09-14 re-arm), stream the gz journal
and replay the SPOT-BUCKET trade tape inside the quoting window T-15..T-5 against a ladder of resting NO
bids, one per rung E in {5..15}c. Rung n_E is solved exactly like the live core: the largest whole cent n
with n + fee(n) <= 2.00 - E - W (fee = exact taker formula reserved on the maker leg, per the frozen spec),
n >= n_min 0.05, using W (the two wings' taker cost) from the latest v32_eval record before the print
(no W -> live stands down -> no fill). A YES-taker print at yes_price >= 1 - n_E fills rung E (our NO bid
at n_E is a YES ask at 1 - n_E). IDEAL = no lag, no queue: this is the SHADOW's fill rule extended to 11
rungs; the live shadow at E in {0.08, 0.10, 0.12} is the sanity check (20 / 22 / 18 windows).

Streaming, low RAM: only lines containing "v32_eval" or the spot ticker + "trade" are parsed.
Output: per-rung windows filled, incremental windows, contracts per pump window, ladder lock per pump.
"""
import gzip, json, math, os, sys, glob, time, collections, statistics as st
from datetime import datetime, timezone

ROOT = r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot"
JDIR = os.path.join(ROOT, "journals_v32")
LEDGER = os.path.join(ROOT, "ledger", "v32_ledger.jsonl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v33_ladder_ideal.json")
RUNGS = list(range(5, 16))            # E in cents
N_MIN = 0.05
QUOTE_START, QUOTE_END = 900, 300     # T-15 .. T-5


def fee(p):  # exact taker fee per contract, rounded up to $0.0001
    return math.ceil(0.07 * p * (1 - p) * 10000 - 1e-9) / 10000


def solve_n(E_c, W):
    """Largest whole cent n with n + fee(n) <= 2 - E - W and n >= N_MIN; None if none."""
    budget = 2.0 - E_c / 100.0 - W
    n = math.floor(budget * 100 + 1e-9) / 100
    while n >= N_MIN:
        if n + fee(n) <= budget + 1e-9:
            return round(n, 2)
        n = round(n - 0.01, 2)
    return None


# armed windows with a spot bucket, from the ledger
windows = {}
for line in open(LEDGER, encoding="utf-8"):
    r = json.loads(line)
    if r.get("effective_mode") != "armed" or not r.get("spot_bucket_ticker"):
        continue
    windows[r["close_time"]] = {"ticker": r["spot_bucket_ticker"], "sets_done": r.get("sets_done") or 0,
                                "shadow": {E: bool(((r.get("shadow") or {}).get(E) or {}).get("filled"))
                                           for E in ("0.08", "0.10", "0.12")}}
print(f"armed+bucket windows in ledger: {len(windows)}", flush=True)

results = []
t_start = time.time()
for i, (close_iso, meta) in enumerate(sorted(windows.items())):
    stamp = close_iso.replace("-", "").replace(":", "")  # 2026-09-15T00:00:00Z -> 20260915T000000Z
    path = os.path.join(JDIR, f"{stamp}.jsonl.gz")
    if not os.path.exists(path):
        alt = os.path.join(JDIR, f"{stamp}.jsonl")
        if not os.path.exists(alt):
            results.append({"close": close_iso, "missing_journal": True}); continue
        path = alt
    close_epoch = datetime.strptime(close_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    tk = meta["ticker"]
    W_now = None; W_ts = None
    rung = {E: None for E in RUNGS}       # first fill per rung
    deepest = 0.0; lots_yes = 0.0; prints_yes = 0
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"v32_eval"' in line:
                try: r = json.loads(line)
                except Exception: continue
                o = r.get("obj") or {}
                W_now = o.get("W"); W_ts = r.get("local_ts")
                if W_now is not None:
                    try: W_now = float(W_now)
                    except Exception: W_now = None
                continue
            if '"trade"' not in line or tk not in line:
                continue
            try: r = json.loads(line)
            except Exception: continue
            m = (r.get("obj") or {}).get("msg") or {}
            if m.get("market_ticker") != tk or (r.get("obj") or {}).get("type") != "trade":
                continue
            ts = r.get("local_ts"); tm = close_epoch - ts
            if not (QUOTE_END <= tm <= QUOTE_START):
                continue
            if m.get("taker_side") != "yes":
                continue
            yp = float(m.get("yes_price_dollars") or m.get("yes_price") or 0)
            cnt = float(m.get("count_fp") or m.get("count") or 0)
            prints_yes += 1; lots_yes += cnt; deepest = max(deepest, yp)
            if W_now is None:
                continue
            for E in RUNGS:
                if rung[E] is not None: continue
                nE = solve_n(E, W_now)
                if nE is None: continue
                if yp + 1e-9 >= 1.0 - nE:
                    rung[E] = {"t_minus": round(tm, 1), "n": nE, "W": round(W_now, 4), "print": yp,
                               "size": cnt, "lock": round(2.0 - nE - fee(nE) - W_now, 4)}
    filled = [E for E in RUNGS if rung[E] is not None]
    results.append({"close": close_iso, "ticker": tk, "sets_done": meta["sets_done"], "shadow": meta["shadow"],
                    "prints_yes": prints_yes, "lots_yes": lots_yes, "deepest_yes_print": deepest,
                    "rungs_filled": filled, "rung": {str(E): rung[E] for E in RUNGS}})
    if (i + 1) % 10 == 0:
        print(f"  {i+1}/{len(windows)} windows, {time.time()-t_start:.0f}s", flush=True)

json.dump(results, open(OUT, "w"), default=str)
ok = [r for r in results if not r.get("missing_journal")]
print(f"\n== V3.3 LADDER IDEAL FILLS: {len(ok)} armed+bucket windows replayed ({len(results)-len(ok)} missing journals) ==")
# sanity vs live shadow
for E, key in ((8, "0.08"), (10, "0.10"), (12, "0.12")):
    ideal = sum(1 for r in ok if E in r["rungs_filled"]); shadow = sum(1 for r in ok if r["shadow"].get(key))
    both = sum(1 for r in ok if (E in r["rungs_filled"]) and r["shadow"].get(key))
    print(f"  sanity E={E}c: ideal-ladder fills {ideal} windows | live shadow {shadow} | both {both}")
print("\n  rung  windows_filled  incremental_vs_next_deeper  mean_lock_c  median_first_print_lots  mean_t_minus_s")
for E in RUNGS:
    w = [r for r in ok if E in r["rungs_filled"]]
    inc = [r for r in ok if E in r["rungs_filled"] and (E + 1) not in r["rungs_filled"]]
    locks = [r["rung"][str(E)]["lock"] * 100 for r in w]
    sizes = [r["rung"][str(E)]["size"] for r in w]
    tms = [r["rung"][str(E)]["t_minus"] for r in w]
    print(f"  E={E:2d}c  {len(w):>6}          {len(inc):>6}                 "
          f"{(st.mean(locks) if locks else 0):>6.2f}       {(st.median(sizes) if sizes else 0):>8.1f}            {(st.mean(tms) if tms else 0):>6.0f}")
pump = [r for r in ok if r["rungs_filled"]]
if pump:
    contracts = [len(r["rungs_filled"]) for r in pump]
    ladder_lock = [sum(r["rung"][str(E)]["lock"] for E in r["rungs_filled"]) * 100 for r in pump]
    e10 = [r for r in pump if 10 in r["rungs_filled"]]
    print(f"\n  pump windows (any rung filled): {len(pump)} / {len(ok)}")
    print(f"  windows filling E=10 (today's single rung): {len(e10)}  -> ladder adds {len(pump)-len(e10)} windows from shallow rungs only")
    print(f"  contracts per pump window: mean {st.mean(contracts):.1f}  median {st.median(contracts):.0f}  distribution {collections.Counter(contracts).most_common()}")
    print(f"  ladder lock per pump window (sum over filled rungs, 1 contract each): mean {st.mean(ladder_lock):.1f}c  median {st.median(ladder_lock):.1f}c  total {sum(ladder_lock):.0f}c")
    single = sum(r["rung"]["10"]["lock"] * 100 for r in e10)
    print(f"  vs single rung E=10 x1 contract: total {single:.0f}c over the same windows  (x2 contracts today = {2*single:.0f}c)")
    full = sum(1 for r in pump if len(r["rungs_filled"]) == len(RUNGS))
    print(f"  windows where the sweep walked ALL 11 rungs: {full}; deepest rung filled distribution: "
          f"{collections.Counter(max(r['rungs_filled']) for r in pump).most_common()}")
    print(f"  shallowest rung filled distribution: {collections.Counter(min(r['rungs_filled']) for r in pump).most_common()}")
print(f"\nDONE in {time.time()-t_start:.0f}s -> {OUT}")
