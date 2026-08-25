"""Rung 1 gate fit (TRAIN only).

Reads a census CSV (OK rows only), buckets hours by G/sigma-hat QUINTILES (boundaries
from train), and per bucket reports n / pin rate / mean C (BASE and WORST) / EV per pair,
with a day-clustered bootstrap 95% CI on EV (resample DAYS with replacement, 10,000 draws,
fixed seed 26). THE GATE is the largest contiguous set of buckets from the low-G/sigma end
whose POOLED day-clustered EV lower bound > 0 in the BASE column (reported alongside WORST).

Anti-overfit (commission section 8): ONE bucketing (quintiles), ONE proxy (sigma-hat), ONE
primary snapshot (T-5). No sweeps. The train CI is SELECTION-BIASED and DESCRIPTIVE only
(A2.6); the only inferential read is the sealed falsifier.

Pure stdlib. EV/pair == mean per-pair payoff, which equals (1 - pin_rate) - mean(C) exactly
(the commission's (1-pin)(1-C) - pin*C reduces to (1-pin) - C per pair).

Confessed judgment calls (see build report):
  * quintile edges via the type-7 (linear-interpolation) empirical quantile; bucket
    membership by bisect_right on the 4 edges (ties land in the upper bucket).
  * bootstrap draws that pool ZERO rows for a bucket/gate (no drawn day has a row there)
    are skipped and counted separately, not scored as EV=0.
  * a fresh random.Random(26) is created for EACH CI so results are independent of call
    order and fully reproducible.
  * "largest contiguous set from the low end" = the biggest prefix bucket set {0..k-1}
    whose pooled BASE lower bound > 0 (scanned k = 5..1, first hit wins).
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import random
from typing import Dict, List, Optional, Tuple

N_BUCKETS = 5
N_DRAWS = 10_000
SEED = 26


class DegenerateQuintiles(Exception):
    """A3.8: quintile edges are not strictly increasing, or a bucket is empty. This is a
    hard fail carrying a degeneracy receipt, never a silent play-everything gate."""

    def __init__(self, receipt: dict):
        self.receipt = receipt
        super().__init__("degenerate quintiles: " + json.dumps(receipt, sort_keys=True))

_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# A2.6 — carried VERBATIM into gate.json and gate_report.md.
DESCRIPTIVE_CI_NOTICE = (
    "The train gate's day-clustered CI is selection-biased at the chosen boundary and "
    "is labeled DESCRIPTIVE in all artifacts. The only inferential read of the gate is "
    "the sealed falsifier."
)


# ---------------------------------------------------------------------------
# Pure primitives
# ---------------------------------------------------------------------------
def quantile_linear(sorted_xs: List[float], p: float) -> float:
    """Type-7 (linear) empirical quantile of an already-sorted list, p in [0, 1]."""
    if not sorted_xs:
        raise ValueError("empty sequence")
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    h = (n - 1) * p
    lo = int(h)
    frac = h - lo
    if lo + 1 >= n:
        return sorted_xs[-1]
    return sorted_xs[lo] + frac * (sorted_xs[lo + 1] - sorted_xs[lo])


def percentile(xs: List[float], q: float) -> float:
    """Percentile q in [0, 100] via type-7 linear interpolation."""
    return quantile_linear(sorted(xs), q / 100.0)


def quintile_edges(gos: List[float]) -> List[float]:
    """The 4 interior quintile boundaries of G/sigma."""
    s = sorted(gos)
    return [quantile_linear(s, p) for p in (0.2, 0.4, 0.6, 0.8)]


def bucket_of(value: float, edges: List[float]) -> int:
    """Bucket index 0..N_BUCKETS-1; ties on an edge fall into the upper bucket."""
    return bisect.bisect_right(edges, value)


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs)


# ---------------------------------------------------------------------------
# Census I/O
# ---------------------------------------------------------------------------
def _optfloat(v) -> Optional[float]:
    """A3.3: WORST columns are fidelity-limited and may be blank on an OK row. Parse to
    float, or None when absent/blank. BASE columns are never blank on an OK row."""
    if v is None or str(v).strip() == "":
        return None
    return float(v)


def read_ok_rows(path: str) -> List[dict]:
    import csv
    rows = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "OK":
                continue
            rows.append({
                "date": r["date"],
                "close_time": r["close_time"],
                "gos": float(r["g_over_sigma"]),
                "C_base": float(r["C_base"]),
                "C_worst": _optfloat(r["C_worst"]),        # A3.3: may be blank
                "pin": r["pin_escape"] == "PIN",
                "payoff_base": float(r["payoff_base"]),
                "payoff_worst": _optfloat(r["payoff_worst"]),  # A3.3: may be blank
            })
    return rows


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Day-clustered bootstrap
# ---------------------------------------------------------------------------
def bootstrap_ci(rows: List[dict], key: str,
                 n_draws: int = N_DRAWS, seed: int = SEED
                 ) -> Tuple[Optional[float], Optional[float], int]:
    """Day-clustered bootstrap 95% CI on EV = mean(row[key]).

    Resample the DISTINCT DAYS with replacement (len(days) per draw), pool every row from
    each drawn day (with multiplicity), score EV = mean(pooled). Returns (lb, ub, n_used).
    Draws that pool zero rows are skipped. A fresh Random(seed) is used each call.

    A3.3: rows whose ``key`` is None (a fidelity-limited blank WORST value) contribute
    nothing; a day with only blank values for ``key`` drops out of the resample pool for
    that key. BASE keys are never None, so BASE CIs are unaffected.
    """
    if not rows:
        return None, None, 0
    by_day: Dict[str, List[float]] = {}
    for r in rows:
        v = r[key]
        if v is None:
            continue
        by_day.setdefault(r["date"], []).append(v)
    if not by_day:
        return None, None, 0
    days = sorted(by_day.keys())
    rng = random.Random(seed)
    evs: List[float] = []
    for _ in range(n_draws):
        drawn = rng.choices(days, k=len(days))
        pooled: List[float] = []
        for d in drawn:
            pooled.extend(by_day[d])
        if pooled:
            evs.append(mean(pooled))
    if not evs:
        return None, None, 0
    return percentile(evs, 2.5), percentile(evs, 97.5), len(evs)


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------
def bucket_stats(rows: List[dict]) -> dict:
    n = len(rows)
    pins = sum(1 for r in rows if r["pin"])
    cw = [r["C_worst"] for r in rows if r["C_worst"] is not None]        # A3.3
    pw = [r["payoff_worst"] for r in rows if r["payoff_worst"] is not None]
    st = {
        "n": n,
        "pin_rate": (pins / n) if n else None,
        "mean_C_base": mean([r["C_base"] for r in rows]) if n else None,
        "mean_C_worst": mean(cw) if cw else None,
        "EV_base": mean([r["payoff_base"] for r in rows]) if n else None,
        "EV_worst": mean(pw) if pw else None,
        "n_worst": len(pw),          # A3.3: OK rows in this bucket with a WORST value
        "n_days": len(set(r["date"] for r in rows)),
    }
    lb_b, ub_b, used_b = bootstrap_ci(rows, "payoff_base")
    lb_w, ub_w, used_w = bootstrap_ci(rows, "payoff_worst")
    st["EV_base_ci95"] = [lb_b, ub_b]
    st["EV_worst_ci95"] = [lb_w, ub_w]
    st["boot_draws_used_base"] = used_b
    st["boot_draws_used_worst"] = used_w
    return st


def fit_gate(rows: List[dict]) -> dict:
    if not rows:
        raise ValueError("no OK census rows to fit")
    gos = [r["gos"] for r in rows]
    edges = quintile_edges(gos)
    buckets: List[List[dict]] = [[] for _ in range(N_BUCKETS)]
    for r in rows:
        buckets[bucket_of(r["gos"], edges)].append(r)

    # A3.8: degenerate quintiles (tied/non-increasing edges, or any empty bucket from
    # heavy G/sigma ties) are a HARD FAIL with a degeneracy receipt — never a silent
    # "play everything" gate.
    bucket_ns = [len(b) for b in buckets]
    tied = [i for i in range(len(edges) - 1) if edges[i] >= edges[i + 1]]
    empty = [i for i, k in enumerate(bucket_ns) if k == 0]
    if tied or empty:
        raise DegenerateQuintiles({
            "quintile_edges_gos": edges,
            "bucket_ns": bucket_ns,
            "tied_or_nonincreasing_edge_pairs": tied,
            "empty_buckets": empty,
            "n_ok_rows": len(rows),
        })

    per_bucket = []
    for i, b in enumerate(buckets):
        st = bucket_stats(b)
        st["bucket"] = i
        st["gos_lo"] = min([r["gos"] for r in b]) if b else None
        st["gos_hi"] = max([r["gos"] for r in b]) if b else None
        per_bucket.append(st)

    # Gate: largest prefix {0..k-1} whose POOLED base LB > 0. Scan k = 5..1.
    gate_indices: List[int] = []
    gate_pooled = None
    for k in range(N_BUCKETS, 0, -1):
        pooled_rows = [r for i in range(k) for r in buckets[i]]
        if not pooled_rows:
            continue
        lb_b, ub_b, used_b = bootstrap_ci(pooled_rows, "payoff_base")
        if lb_b is not None and lb_b > 0:
            lb_w, ub_w, used_w = bootstrap_ci(pooled_rows, "payoff_worst")
            cw_p = [r["C_worst"] for r in pooled_rows if r["C_worst"] is not None]   # A3.3
            pw_p = [r["payoff_worst"] for r in pooled_rows if r["payoff_worst"] is not None]
            gate_indices = list(range(k))
            gate_pooled = {
                "n": len(pooled_rows),
                "n_days": len(set(r["date"] for r in pooled_rows)),
                "pin_rate": sum(1 for r in pooled_rows if r["pin"]) / len(pooled_rows),
                "mean_C_base": mean([r["C_base"] for r in pooled_rows]),
                "mean_C_worst": mean(cw_p) if cw_p else None,
                "EV_base": mean([r["payoff_base"] for r in pooled_rows]),
                "EV_worst": mean(pw_p) if pw_p else None,
                "EV_base_ci95": [lb_b, ub_b],
                "EV_worst_ci95": [lb_w, ub_w],
                "boot_draws_used_base": used_b,
                "boot_draws_used_worst": used_w,
            }
            break

    if gate_indices:
        top = gate_indices[-1]
        g_star = edges[top] if top < len(edges) else max(gos)
        gate_rows = [r for i in gate_indices for r in buckets[i]]
        c_cap_observed = max(r["C_base"] for r in gate_rows)
        gos_max_in_gate = max(r["gos"] for r in gate_rows)
        top_bucket_lb = per_bucket[top]["EV_base_ci95"][0]   # A3.11 dilution check
    else:
        top = None
        g_star = None
        c_cap_observed = None
        gos_max_in_gate = None
        top_bucket_lb = None

    return {
        "n_ok_rows": len(rows),
        "n_days": len(set(r["date"] for r in rows)),
        "bucketing": "quintiles",
        "quintile_edges_gos": edges,
        "buckets": per_bucket,
        "gate_buckets": gate_indices,
        "gate_empty": not gate_indices,
        "g_star": g_star,
        "gos_max_in_gate": gos_max_in_gate,
        "c_cap_observed_base": c_cap_observed,
        "gate_top_bucket": top,                               # A3.11
        "gate_top_bucket_individual_lb_base": top_bucket_lb,  # A3.11
        "gate_top_bucket_lb_le_zero": (top_bucket_lb is not None
                                       and top_bucket_lb <= 0),  # A3.11
        "gate_pooled": gate_pooled,
        "bootstrap": {"n_draws": N_DRAWS, "seed": SEED,
                      "method": "day-clustered, resample distinct days w/ replacement"},
        "descriptive_ci_notice": DESCRIPTIVE_CI_NOTICE,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _fmt(x, nd=4):
    return "n/a" if x is None else f"{x:.{nd}f}"


def render_report(fit: dict, census_csv: str, census_sha: str) -> str:
    L = []
    L.append("# GATE REPORT — Rung 1 (TRAIN)\n")
    L.append(f"Source census: `{census_csv}`  ")
    L.append(f"sha256: `{census_sha}`  ")
    L.append(f"OK rows: {fit['n_ok_rows']} over {fit['n_days']} days  ")
    L.append(f"Bucketing: {fit['bucketing']} (5); "
             f"bootstrap: {fit['bootstrap']['n_draws']} draws, seed {fit['bootstrap']['seed']}, "
             f"{fit['bootstrap']['method']}\n")
    L.append("> **" + DESCRIPTIVE_CI_NOTICE + "**\n")
    L.append("## Per-bucket (G/sigma quintiles, low -> high)\n")
    L.append("| bkt | n | days | gos_lo | gos_hi | pin_rate | meanC_base | meanC_worst "
             "| EV_base | EV_base_CI95 | EV_worst | EV_worst_CI95 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for b in fit["buckets"]:
        cb = b["EV_base_ci95"]; cw = b["EV_worst_ci95"]
        L.append(
            f"| {b['bucket']} | {b['n']} | {b['n_days']} | {_fmt(b['gos_lo'])} | "
            f"{_fmt(b['gos_hi'])} | {_fmt(b['pin_rate'])} | {_fmt(b['mean_C_base'])} | "
            f"{_fmt(b['mean_C_worst'])} | {_fmt(b['EV_base'])} | "
            f"[{_fmt(cb[0])}, {_fmt(cb[1])}] | {_fmt(b['EV_worst'])} | "
            f"[{_fmt(cw[0])}, {_fmt(cw[1])}] |"
        )
    L.append("")
    L.append(f"Quintile edges (G/sigma): "
             f"{', '.join(_fmt(e, 6) for e in fit['quintile_edges_gos'])}\n")
    L.append("## The gate\n")
    if fit["gate_empty"]:
        L.append("**GATE EMPTY** — no low-end contiguous bucket set has a pooled "
                 "day-clustered BASE EV lower bound > 0. Per commission section 8 this is "
                 "the result, reported plainly: no widening, no re-bucketing, no alternate "
                 "proxies.\n")
    else:
        gp = fit["gate_pooled"]
        L.append(f"Gate buckets (from low end): {fit['gate_buckets']}  ")
        L.append(f"g* (G/sigma cutoff): {_fmt(fit['g_star'], 6)}  ")
        L.append(f"max G/sigma observed in gate: {_fmt(fit['gos_max_in_gate'], 6)}  ")
        L.append(f"C-cap observed (max C_base in gate): {_fmt(fit['c_cap_observed_base'])}  ")
        L.append(f"pooled n: {gp['n']} over {gp['n_days']} days; "
                 f"pin_rate {_fmt(gp['pin_rate'])}\n")
        L.append(f"pooled EV_base: {_fmt(gp['EV_base'])}  "
                 f"CI95 [{_fmt(gp['EV_base_ci95'][0])}, {_fmt(gp['EV_base_ci95'][1])}]  ")
        L.append(f"pooled EV_worst: {_fmt(gp['EV_worst'])}  "
                 f"CI95 [{_fmt(gp['EV_worst_ci95'][0])}, {_fmt(gp['EV_worst_ci95'][1])}]  ")
        L.append("(WORST is fidelity-bounded; a gate that clears only in BASE is "
                 "candle-fidelity-limited.)\n")
        # A3.11: surface dilution honestly — the top gated bucket may itself be -EV while
        # the pooled prefix clears (carried by the lower-G/sigma buckets). The gate rule
        # (largest low-end prefix whose POOLED base LB>0) is unchanged.
        if fit.get("gate_top_bucket_lb_le_zero"):
            top_lb = fit.get("gate_top_bucket_individual_lb_base")
            L.append(
                f"> **DILUTION ADMISSION (A3.11):** the top gated bucket "
                f"(bucket {fit.get('gate_top_bucket')}) has an individual day-clustered "
                f"BASE EV lower bound of {_fmt(top_lb)} (<= 0), yet the pooled prefix "
                f"clears. The pooled LB>0 is carried by the lower-G/sigma buckets; the top "
                f"included bucket does not individually clear.\n"
            )
    return "\n".join(L) + "\n"


def run(census_csv: str, gate_json: str, gate_md: str) -> dict:
    rows = read_ok_rows(census_csv)
    fit = fit_gate(rows)
    census_sha = sha256_file(census_csv)
    fit["census_csv"] = os.path.basename(census_csv)
    fit["census_csv_sha256"] = census_sha
    os.makedirs(os.path.dirname(gate_json), exist_ok=True)
    with open(gate_json, "w", encoding="utf-8") as f:
        json.dump(fit, f, indent=2, sort_keys=True)
    with open(gate_md, "w", encoding="utf-8") as f:
        f.write(render_report(fit, os.path.basename(census_csv), census_sha))
    return fit


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rung 1 gate fit (train only)")
    ap.add_argument("--census", default=os.path.join(_OUT_DIR, "census_train.csv"))
    ap.add_argument("--gate", default=os.path.join(_OUT_DIR, "gate.json"))
    ap.add_argument("--report", default=os.path.join(_OUT_DIR, "gate_report.md"))
    args = ap.parse_args(argv)
    fit = run(args.census, args.gate, args.report)
    print(f"wrote {args.gate} and {args.report}")
    print("gate_empty:", fit["gate_empty"], "gate_buckets:", fit["gate_buckets"],
          "g_star:", fit["g_star"])


if __name__ == "__main__":
    main()
