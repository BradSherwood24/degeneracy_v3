"""Aggregate tape_points.csv into a compact JS data file for the surface viz.

Grid: dataset (staleness 60/5) x direction (strangle/flip) x freshness bracket
(max leg age <=1s / <=5s / any) x quintile x time bucket (minutes-remaining 0..14,
plus ALL=15) x margin x (enter at first moment ev >= x cents, x = 0..30; slot 31 =
flip sub-$1 special: first moment C < 1.00).

Policy per cell: per-window FIRST qualifying event (rows are chronological within
a window — verified: t_minus_s strictly decreasing). Windows are the unit.
Cell stats: [n_windows, sum_payoff_dollars, pins].

Cross-check targets (published Rung 1.5 ladder, ds=60, fresh<=1s, all quintiles,
bucket=ALL): strangle x=0 -> 124w +0.63c 19p; x=10 -> 71w +2.58c 19p;
flip x=15 -> 61w +3.36c 7p. Run with --check to assert these.

Usage: python aggregate.py --out data.js [--check]
       [--src60 PATH --src5 PATH]   (defaults: sim/out/week60_r2|week5_r2)
"""
import argparse, csv, json, os, sys
from collections import defaultdict

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

N_X = 31          # margins 0..30 cents
SUB1 = 31         # special slot: flip C < 1.00
ALL_BUCKET = 15   # buckets 0..14 = minutes remaining, 15 = whole window


def minute_bucket(t_minus_s):
    m = int(t_minus_s // 60)
    return 14 if m > 14 else m


def aggregate(path, ds_label, cells, win_meta, win_counts):
    """Stream one tape_points.csv. Mutates the three accumulators."""
    # covered[key] = highest x already entered (prefix property: entries fill 0..k)
    covered = {}
    sub1_done = set()
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.reader(f)
        header = next(rdr)
        ix = {name: i for i, name in enumerate(header)}
        c_dir, c_ct, c_t = ix["direction"], ix["close_time"], ix["t_minus_s"]
        c_q, c_ha, c_la = ix["quintile"], ix["high_leg_age_s"], ix["low_leg_age_s"]
        c_C, c_ev, c_pin, c_pay = ix["C"], ix["ev"], ix["pin"], ix["payoff"]
        c_date, c_G = ix["date"], ix["G"]

        for row in rdr:
            d = row[c_dir]
            ct = row[c_ct]
            q = int(row[c_q])
            wkey = (ds_label, d, ct)
            if wkey not in win_counts:
                win_counts[wkey] = q
                win_meta[wkey] = {
                    "date": row[c_date], "G": float(row[c_G]), "q": q,
                    "pin": int(row[c_pin]), "best_ev": None, "t_best": None,
                    "minC": None, "sub1": False,
                }
            t = float(row[c_t])
            ev = float(row[c_ev])
            C = float(row[c_C])
            pin = int(row[c_pin])
            pay = float(row[c_pay])
            age = max(float(row[c_ha]), float(row[c_la]))
            mb = minute_bucket(t)
            # cumulative freshness brackets this row qualifies for
            brackets = (0, 1, 2) if age <= 1.0 else (1, 2) if age <= 5.0 else (2,)

            # window meta (fresh <=1s only, matching the honest view)
            if age <= 1.0:
                m = win_meta[wkey]
                if m["best_ev"] is None or ev > m["best_ev"]:
                    m["best_ev"] = ev
                    m["t_best"] = t
                if m["minC"] is None or C < m["minC"]:
                    m["minC"] = C
                if d == "flip" and C < 1.0:
                    m["sub1"] = True

            x_hi = int(ev * 100 + 1e-9) if ev >= 0 else -1
            if x_hi > 30:
                x_hi = 30
            for br in brackets:
                for b in (mb, ALL_BUCKET):
                    key = (ds_label, d, br, ct, b)
                    k = covered.get(key, -1)
                    if x_hi > k:
                        for x in range(k + 1, x_hi + 1):
                            cell = cells[(ds_label, d, br, q, b, x)]
                            cell[0] += 1
                            cell[1] += pay
                            cell[2] += pin
                        covered[key] = x_hi
                    if d == "flip" and C < 1.0 and key not in sub1_done:
                        sub1_done.add(key)
                        cell = cells[(ds_label, d, br, q, b, SUB1)]
                        cell[0] += 1
                        cell[1] += pay
                        cell[2] += pin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js"))
    ap.add_argument("--src60", default=os.path.join(ROOT, "sim", "out", "week60_r2", "tape_points.csv"))
    ap.add_argument("--src5", default=os.path.join(ROOT, "sim", "out", "week5_r2", "tape_points.csv"))
    ap.add_argument("--label", default="train week 2026-06-13..19 (definitive r2 outputs)")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    cells = defaultdict(lambda: [0, 0.0, 0])
    win_meta, win_counts = {}, {}
    for ds_label, path in (("60", args.src60), ("5", args.src5)):
        print(f"[aggregate] {ds_label}s: {path}", flush=True)
        aggregate(path, ds_label, cells, win_meta, win_counts)

    if args.check:
        def cell_of(x):
            n = s = p = 0
            for q in range(0, 5):
                c = cells.get(("60", "strangle" if x[0] == "s" else "flip", 0, q, ALL_BUCKET, x[1]), [0, 0, 0])
                n += c[0]; s += c[1]; p += c[2]
            return n, round(100 * s / n, 2) if n else None, p
        for tag, x, want in (("s", 0, (124, 0.63, 19)), ("s", 10, (71, 2.58, 19)), ("f", 15, (61, 3.36, 7))):
            got = cell_of((tag, x))
            status = "OK" if got == want else "MISMATCH"
            print(f"[check] {'strangle' if tag=='s' else 'flip'} x={x}: got {got}, want {want} -> {status}")
            if got != want:
                sys.exit(f"[check] FAILED — do not trust the viz data")

    # window totals per (ds, dir, quintile) for availability metric
    totals = defaultdict(int)
    for (ds, d, ct), q in win_counts.items():
        totals[f"{ds}|{d}|{q}"] += 1

    # serialize cells: nested ds -> dir -> bracket -> quintile -> bucket -> x -> [n,sum,pins]
    out = {}
    for (ds, d, br, q, b, x), (n, s, p) in cells.items():
        out.setdefault(ds, {}).setdefault(d, {}).setdefault(str(br), {}) \
           .setdefault(str(q), {}).setdefault(str(b), {})[str(x)] = [n, round(s, 4), p]

    windows = []
    for (ds, d, ct), m in win_meta.items():
        windows.append({
            "ds": ds, "dir": d, "ct": ct, "date": m["date"], "G": m["G"], "q": m["q"],
            "pin": m["pin"],
            "best_ev": round(m["best_ev"], 4) if m["best_ev"] is not None else None,
            "t_best": round(m["t_best"], 1) if m["t_best"] is not None else None,
            "minC": round(m["minC"], 4) if m["minC"] is not None else None,
            "sub1": m["sub1"],
        })

    payload = {"label": args.label, "n_x": N_X, "sub1_slot": SUB1, "all_bucket": ALL_BUCKET,
               "totals": dict(totals), "cells": out, "windows": windows}
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("window.TAPE_DATA = ")
        json.dump(payload, f, separators=(",", ":"))
        f.write(";\n")
    print(f"[aggregate] wrote {args.out} ({os.path.getsize(args.out)/1e6:.1f} MB), "
          f"{len(win_meta)} window-series entries")


if __name__ == "__main__":
    main()
