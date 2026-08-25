"""Concatenate chunked tape_sim tape_points.csv files into one CSV (header once).

Chunks are law-equivalent to one big run: tape_sim assigns windows to days via
_assigned_day filtered to the requested dateset (tape_sim.py:419), so consecutive
date ranges partition windows exactly; chunk boundaries behave identically to the
reviewed week-run boundary. Usage:
  python concat_chunks.py --chunks-root sim/out/full60_chunks --out sim/out/full60/tape_points.csv
"""
import argparse, csv, glob, os, sys

ap = argparse.ArgumentParser()
ap.add_argument("--chunks-root", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()

srcs = sorted(glob.glob(os.path.join(args.chunks_root, "c*", "tape_points.csv")))
if not srcs:
    sys.exit(f"no chunk CSVs under {args.chunks_root}")
os.makedirs(os.path.dirname(args.out), exist_ok=True)
n = 0
with open(args.out, "w", newline="", encoding="utf-8") as fo:
    header = None
    for src in srcs:
        with open(src, newline="", encoding="utf-8") as fi:
            h = fi.readline()
            if header is None:
                header = h
                fo.write(h)
            elif h != header:
                sys.exit(f"header mismatch in {src}")
            for line in fi:
                fo.write(line)
                n += 1
print(f"[concat] {len(srcs)} chunks -> {args.out} ({n} rows)")
