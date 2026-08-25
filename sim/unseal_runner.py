"""Rung 1 UNSEAL RUNNER — WRITTEN, NEVER RUN IN THIS CAMPAIGN.

This is the ONLY module that passes ``acknowledge_sealed_read=True`` to the loader. It
applies a FROZEN ``sim/out/gate.json`` (fit on TRAIN) to the sealed 17 days
(2026-08-02..2026-08-18), computes the falsifier quantities defined in
``sim/ceremony/falsifier.md`` at runtime, and emits AGGREGATES ONLY. One execution, on
Brad's explicit morning go, run by the bridge — not the builder.

Fail-closed preflight (any failure REFUSES to start, before any sealed byte is read):
  1. ``sim/out/gate.json`` must exist (the frozen train gate).
  2. ``sim/out/census_train.csv`` and ``sim/out/census_receipt.json`` must exist.
  3. (A3.9) ``census_train.csv``'s sha256 must equal ``gate.json["census_csv_sha256"]`` —
     the gate is bound to the exact census it was fit on.
  4. ``sim/ceremony/falsifier.md`` must exist (the runner reads its clauses at runtime).
  5. (A3.6) ``falsifier.md`` must be FROZEN: a line stripping to exactly ``STATUS: FROZEN``
     must be present (parsed as a line, not a whole-file substring). The loader ALSO
     asserts this before any sealed open (A3.7 defense-in-depth).
  6. ``main`` additionally requires the explicit ``--i-have-brads-explicit-go`` flag.

The result document carries AGGREGATES ONLY (A3.10): per-reason exclusion COUNTS and
day/hour COUNTS — no per-hour sealed rows, no sealed timestamps or date lists.

There is NO code path that reads a sealed day without all of the above holding.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List

from loader import SEALED_DATES
import loader as _loader
import census as _census
import gate_fit as _gate

_SIM_DIR = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_SIM_DIR, "out")
_CEREMONY_DIR = os.path.join(_SIM_DIR, "ceremony")

GATE_JSON = os.path.join(_OUT_DIR, "gate.json")
CENSUS_CSV = os.path.join(_OUT_DIR, "census_train.csv")
CENSUS_RECEIPT = os.path.join(_OUT_DIR, "census_receipt.json")
FALSIFIER_MD = os.path.join(_CEREMONY_DIR, "falsifier.md")
RESULT_JSON = os.path.join(_OUT_DIR, "unseal_result.json")


class RefuseToRun(Exception):
    """Raised by the fail-closed preflight; the sealed read never begins."""


def preflight() -> str:
    """Run every gate. Return the falsifier text on success; raise RefuseToRun otherwise."""
    if not os.path.exists(GATE_JSON):
        raise RefuseToRun(f"REFUSE: frozen gate missing ({GATE_JSON}); nothing to apply.")
    if not os.path.exists(CENSUS_CSV) or not os.path.exists(CENSUS_RECEIPT):
        raise RefuseToRun(
            f"REFUSE: train census artifacts missing "
            f"({CENSUS_CSV} / {CENSUS_RECEIPT}); the gate is not reproducible."
        )
    # A3.9: bind the frozen gate to the exact census it was fit on.
    with open(GATE_JSON, "r", encoding="utf-8") as f:
        gate = json.load(f)
    recorded_sha = gate.get("census_csv_sha256")
    if not recorded_sha:
        raise RefuseToRun(
            "REFUSE: gate.json carries no census_csv_sha256; cannot bind the gate to its "
            "train census (A3.9)."
        )
    actual_sha = _loader.sha256_file(CENSUS_CSV)
    if actual_sha != recorded_sha:
        raise RefuseToRun(
            f"REFUSE: census_train.csv sha256 {actual_sha} != gate.json census_csv_sha256 "
            f"{recorded_sha} (A3.9); the on-disk census does not match the frozen gate."
        )
    if not os.path.exists(FALSIFIER_MD):
        raise RefuseToRun(
            f"REFUSE: falsifier missing ({FALSIFIER_MD}); the read has no pre-committed "
            f"clauses. Draft and FREEZE it before any sealed access."
        )
    with open(FALSIFIER_MD, "r", encoding="utf-8") as f:
        text = f.read()
    # A3.6: freeze state keyed on a dedicated STATUS line, not a whole-file substring, so
    # prose that mentions FROZEN / NOT FROZEN cannot flip the state (fixes the B1 deadlock).
    if not _loader.falsifier_is_frozen(text):
        raise RefuseToRun(
            "REFUSE: falsifier.md is not FROZEN (no line reads exactly 'STATUS: FROZEN'). "
            "The clauses must be pinned and frozen on Brad's explicit go before the read."
        )
    return text


def _rows_to_gatefmt(rows: List[dict]) -> List[dict]:
    """OK census rows (dicts from build_census) -> gate_fit row shape."""
    out = []
    for r in rows:
        if r.get("status") != "OK":
            continue
        out.append({
            "date": r["date"],
            "close_time": r["close_time"],
            "gos": float(r["g_over_sigma"]),
            "C_base": float(r["C_base"]),
            "C_worst": _gate._optfloat(r["C_worst"]),        # A3.3: may be blank
            "pin": r["pin_escape"] == "PIN",
            "payoff_base": float(r["payoff_base"]),
            "payoff_worst": _gate._optfloat(r["payoff_worst"]),  # A3.3: may be blank
        })
    return out


def run() -> dict:
    """Execute the one-shot sealed read. Preflight MUST pass first."""
    falsifier_text = preflight()

    with open(GATE_JSON, "r", encoding="utf-8") as f:
        gate = json.load(f)
    edges = gate["quintile_edges_gos"]
    gate_buckets = set(gate["gate_buckets"])
    if not gate_buckets:
        raise RefuseToRun("REFUSE: frozen gate is EMPTY; there is nothing to read.")

    # THE ONE SEALED READ — the only acknowledge_sealed_read=True in the codebase.
    rows, receipt = _census.build_census(list(SEALED_DATES), acknowledge_sealed_read=True)
    ok = _rows_to_gatefmt(rows)

    # Apply the FROZEN train boundaries — no refit on sealed data.
    gated = [r for r in ok if _gate.bucket_of(r["gos"], edges) in gate_buckets]

    # Falsifier quantities (aggregates only, A3.10) -------------------------
    n = len(gated)
    by_day: Dict[str, List[float]] = {}
    for r in gated:
        by_day.setdefault(r["date"], []).append(r["payoff_base"])
    n_days_negative = sum(1 for ps in by_day.values() if (sum(ps) / len(ps)) < 0)

    # A3.10: reduce sealed exclusions to per-reason COUNTS. No per-hour sealed rows, no
    # sealed timestamps or date lists anywhere in the result artifact.
    exclusion_counts = {
        reason: entry.get("count", len(entry.get("hours", [])))
        for reason, entry in receipt.get("exclusion_inventory", {}).items()
    }
    result = {
        "STATUS": "SEALED READ EXECUTED",
        "sealed_dates": list(SEALED_DATES),
        "gate_buckets_applied": sorted(gate_buckets),
        "quintile_edges_gos": edges,
        "participation_gated_hours": n,                   # C4
        "n_sealed_days_with_rows": len(by_day),           # aggregate count only (A3.10)
        "n_days_negative_EV_base": n_days_negative,       # C3 (count only, A3.10)
        "census_receipt_exclusion_counts": exclusion_counts,  # A3.10
    }
    if n:
        pin_rate = sum(1 for r in gated if r["pin"]) / n
        cbar_base = sum(r["C_base"] for r in gated) / n
        lb_b, ub_b, _ = _gate.bootstrap_ci(gated, "payoff_base")
        lb_w, ub_w, _ = _gate.bootstrap_ci(gated, "payoff_worst")
        pw = [r["payoff_worst"] for r in gated if r["payoff_worst"] is not None]  # A3.3
        result.update({
            "EV_base": sum(r["payoff_base"] for r in gated) / n,          # C1
            "EV_base_ci95": [lb_b, ub_b],                                  # C1
            "EV_worst": (sum(pw) / len(pw)) if pw else None,              # C5
            "EV_worst_ci95": [lb_w, ub_w],
            "n_worst": len(pw),                                           # A3.3
            "pin_rate": pin_rate,                                          # C2
            "Cbar_base_sealed": cbar_base,                                 # C2
            "breakeven_1_minus_Cbar": 1.0 - cbar_base,                    # C2
        })
    else:
        result["VERDICT_HINT"] = "VOID-THIN: gate played zero sealed hours."

    result["falsifier_present"] = True
    result["falsifier_frozen"] = True
    result["_note"] = ("Aggregates only. Clause verdicts (C1-C5) are evaluated by the "
                       "bridge against the frozen falsifier.md thresholds.")

    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rung 1 unseal runner (NEVER RUN in this campaign)")
    ap.add_argument("--i-have-brads-explicit-go", action="store_true",
                    help="required; the sealed read runs only on Brad's explicit morning go")
    args = ap.parse_args(argv)
    if not args.i_have_brads_explicit_go:
        raise RefuseToRun(
            "REFUSE: the unseal runner executes ONLY on Brad's explicit go. "
            "Pass --i-have-brads-explicit-go (and only the bridge does this, once)."
        )
    result = run()
    print("SEALED READ EXECUTED — aggregates written to", RESULT_JSON)
    print("participation:", result["participation_gated_hours"])


if __name__ == "__main__":
    main()
