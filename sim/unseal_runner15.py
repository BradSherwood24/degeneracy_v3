"""Rung 1.5 UNSEAL RUNNER — the ONE sealed read of the trades tape.

This module applies the FROZEN expanded quintile-conditional policy
(``sim/ceremony/unseal15_commission.md``) to the 17 sealed UTC days
(2026-08-02..2026-08-18) MINUS the two burned close_times, and emits AGGREGATES
ONLY to ``sim/out/unseal15_result.json``. It is the ONLY sanctioned caller of
``tape_sim.run(..., acknowledge_sealed_read=True)`` for this read. One execution,
on Brad's explicit go, run by the bridge — never by the builder.

Three entry points (``main``):
  * ``--dry-run-train``: run the policy evaluator over the TRAIN full60 tape
    (``sim/out/full60/tape_points.csv``, no sealed access) and write the dry-run
    certificate ``sim/out/unseal15_dryrun.json``. This MUST reproduce the frozen
    train reference numbers embedded below or it refuses.
  * ``--i-have-brads-explicit-go``: the one-shot sealed read. Fail-closed
    preflight (below) runs BEFORE any sealed byte; then tape_sim.run() is invoked
    ONE SEALED DAY AT A TIME (memory law), each day's tape_points.csv fed through
    the same policy evaluator, and aggregates written.

Fail-closed sealed preflight (any failure REFUSES before any sealed byte):
  1. ``sim/ceremony/falsifier.md`` exists and is FROZEN (loader.falsifier_is_frozen;
     the loader ALSO re-asserts this via A3.7 before any sealed open).
  2. ``sim/out/census_train.csv`` sha256 == the pinned constant.
  3. All sealed day-files present for BOTH series x {markets, candles, trades}.
  4. The train dry-run certificate exists and its numbers match the embedded
     reference constants and the policy-parameter sha.
  5. ``sim/out/unseal15_result.json`` does NOT already exist (no second read).

AGGREGATES DISCIPLINE: nothing row-level (no close_times, timestamps, prices, or
per-entry rows) leaves ``sim/out/sealed_eval/``. Per-day means are keyed by day
INDEX, not date. The result carries clause-verdict INPUTS only; the bridge applies
the falsifier SECTION 2 arithmetic to reach a verdict.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
from typing import Dict, List, Optional, Tuple

from loader import SEALED_DATES, data_path, falsifier_is_frozen, sha256_file
import loader as _loader
import tape_sim as _tape_sim
from gate_fit import percentile as _percentile

_SIM_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SIM_DIR)
_OUT_DIR = os.path.join(_SIM_DIR, "out")
_CEREMONY_DIR = os.path.join(_SIM_DIR, "ceremony")

# Overridable module paths (tests monkeypatch these) ------------------------
FALSIFIER_MD = os.path.join(_CEREMONY_DIR, "falsifier.md")
CENSUS_CSV = os.path.join(_OUT_DIR, "census_train.csv")
FULL60_TAPE = os.path.join(_OUT_DIR, "full60", "tape_points.csv")
DRYRUN_JSON = os.path.join(_OUT_DIR, "unseal15_dryrun.json")
RESULT_JSON = os.path.join(_OUT_DIR, "unseal15_result.json")
SEALED_EVAL_DIR = os.path.join(_OUT_DIR, "sealed_eval")
# F2: an atomic start-marker written BEFORE the first sealed byte is opened. A mid-run
# crash then leaves the ceremony fail-closed (the read counts as SPENT): preflight refuses
# while EITHER this marker OR the result JSON exists. Only Brad clears the marker by hand
# after a post-mortem — house law for a one-shot resource.
STARTED_MARKER = os.path.join(_OUT_DIR, "unseal15_STARTED.marker")
# The chunk receipts of the full60 TRAIN tape run (authoritative eligible-window truth for
# the dry-run; F1). Each chunk's tape_receipt.json carries n_eligible_windows.
FULL60_CHUNKS_DIR = os.path.join(_OUT_DIR, "full60_chunks")

# The census the EV curve / quintile edges are bound to (tape_sim re-verifies too).
CENSUS_TRAIN_SHA256 = (
    "580d143fa5d3581a8bbee9d5cc2b45f800d25fa84db7967c3cf591a8ac7bb247"
)

# ---------------------------------------------------------------------------
# THE FROZEN POLICY PARAMETERS (unseal15_commission.md, falsifier SECTION 2)
# ---------------------------------------------------------------------------
FRESH_MAX_LEG_AGE_S = 1.0        # entry only where max(both leg ages) <= 1.0 s
Q1_STRANGLE_EV_MIN = 0.05        # Q1 strangle at EV-5
SUB_DOLLAR_C_MAX = 1.0           # sub-$1 flip: C < $1.00
Q5_FLIP_EV_MIN = 0.10           # Q5 flip at EV-10
STALENESS_S = 60                 # commission staleness horizon (tape_sim default)

# The two burned close_times (Rung 1 contamination) excluded from all clause
# quantities on the SEALED read and reported only as an exclusion count.
BURNED_CLOSE_TIMES_SEALED = frozenset({
    "2026-08-18T19:00:00Z",
    "2026-08-18T20:00:00Z",
})

# Quintile routing (0-indexed buckets: Q1=0 ... Q5=4). The source labels a winning
# entry can carry per quintile. Q1 races strangle-EV-5 vs sub-$1 flip; Q2-Q4 are
# sub-$1 flip only; Q5 is flip-EV-10 only.
_ROUTING = {
    0: ("Q1-strangle", "sub$1-flip"),
    1: ("sub$1-flip",),
    2: ("sub$1-flip",),
    3: ("sub$1-flip",),
    4: ("Q5-flip",),
}

# A canonical fingerprint of the frozen policy (NOT the universe/burned set, which
# differs train-vs-sealed). Stored in the dry-run certificate and re-checked before
# the sealed read so the two runs provably share the same policy code.
POLICY_PARAMS = {
    "freshness_max_leg_age_s": FRESH_MAX_LEG_AGE_S,
    "q1_strangle_ev_min": Q1_STRANGLE_EV_MIN,
    "sub_dollar_C_max": SUB_DOLLAR_C_MAX,
    "q5_flip_ev_min": Q5_FLIP_EV_MIN,
    "staleness_s": STALENESS_S,
    "first_qualifying_wins_by": "t_minus_s_desc",
    "quintile_routing": {str(q): list(v) for q, v in _ROUTING.items()},
}
POLICY_PARAMS_SHA256 = hashlib.sha256(
    json.dumps(POLICY_PARAMS, sort_keys=True).encode("utf-8")
).hexdigest()

# The frozen TRAIN reference numbers the dry-run MUST reproduce (commission B).
# Quintile keys are strings for JSON round-trip stability.
REFERENCE = {
    "eligible_windows": 1142,
    "entered": 599,
    "pooled_mean_payoff_cents_2dp": 3.71,
    "pins_among_entered": 62,
    "by_source": {"Q1-strangle": 88, "Q5-flip": 201, "sub$1-flip": 310},
    "sub_dollar_by_quintile": {"0": 126, "1": 90, "2": 56, "3": 38, "4": 0},
}

_REQUIRED_COLUMNS = (
    "close_time", "direction", "t_minus_s", "quintile",
    "high_leg_age_s", "low_leg_age_s", "C", "ev", "pin", "payoff", "date",
)


class RefuseToRun(Exception):
    """Raised by the fail-closed preflight / gate; the sealed read never begins."""


class PolicyError(Exception):
    """Raised on a structural violation of the tape_points stream (non-contiguous
    windows, missing columns) — a fail-closed data-integrity stop."""


# ---------------------------------------------------------------------------
# The policy evaluator (streaming; no full-file memory)
# ---------------------------------------------------------------------------
class PolicyEvaluator:
    """Stream tape_points rows and apply the frozen expanded policy.

    Rows are grouped by CONTIGUOUS ``close_time`` (tape_sim emits each window's
    events contiguously: all strangle events, then all flip events, each
    chronological — i.e. DESCENDING ``t_minus_s``). Because a direction's rows are
    chronological, the FIRST qualifying row seen for a direction is the earliest
    (largest ``t_minus_s``). Across directions the winner is the qualifying event
    with the LARGEST ``t_minus_s`` (earliest in wall-clock time).

    Only aggregate-sized state is retained: the per-entry records list (bounded by
    #windows, ~1142 train / ~390 sealed), a set of seen close_times (contiguity
    guard), and the small per-window scratch. The 18M tape rows stream through.

    ``burned_close_times`` windows are dropped, counted, and never counted as
    eligible or entered.
    """

    def __init__(self, burned_close_times=(), strict_hardfloor: bool = False):
        self.burned = frozenset(burned_close_times)
        self.entries: List[dict] = []
        # windows_with_events counts distinct NON-burned close_times that emitted >=1 row.
        # This is a LOWER bound on census-eligible windows (event-less eligible windows are
        # invisible to a CSV reader — F1); the authoritative eligible denominator comes from
        # tape_sim's per-day n_eligible_windows on the sealed path / chunk receipts on train.
        self.eligible_windows = 0        # kept name for the CSV-derived (with-events) count
        self.burned_excluded: set = set()
        # F4: hard_floor (tape_sim's full-precision riskless flag) is the authoritative
        # sub-$1 indicator for flip rows; we also track where it disagrees with the
        # 4dp-rounded float(C) < 1.0 test. On train there are 0 divergences; in strict mode
        # (the dry-run) any divergence raises. On the sealed run we only COUNT them.
        self.strict_hardfloor = strict_hardfloor
        self.hardfloor_floatc_divergences = 0
        self._seen: set = set()
        self._reset_window(None)

    def _reset_window(self, ct: Optional[str]) -> None:
        self._cur = ct
        self._q: Optional[int] = None
        self._strangle: Optional[dict] = None   # first fresh strangle ev>=0.05
        self._sub: Optional[dict] = None        # first fresh flip C<1.0
        self._flip_ev: Optional[dict] = None    # first fresh flip ev>=0.10

    def _select(self) -> Optional[dict]:
        q = self._q
        routing = _ROUTING.get(q, ())
        cands: List[Tuple[dict, str]] = []
        if "Q1-strangle" in routing and self._strangle is not None:
            cands.append((self._strangle, "Q1-strangle"))
        if "sub$1-flip" in routing and self._sub is not None:
            cands.append((self._sub, "sub$1-flip"))
        if "Q5-flip" in routing and self._flip_ev is not None:
            cands.append((self._flip_ev, "Q5-flip"))
        if not cands:
            return None
        # Earliest qualifying event wins = largest t_minus_s. max() keeps the FIRST
        # listed candidate on an exact tie (strangle before sub-$1 in Q1); ties across
        # directions do not occur in the train reference (confessed tie-break).
        rec, src = max(cands, key=lambda c: c[0]["t_minus_s"])
        return {
            "payoff": rec["payoff"],
            "C": rec["C"],
            "pin": rec["pin"],
            "quintile": q,
            "source": src,
            "date": rec["date"],
        }

    def _finalize_window(self) -> None:
        if self._cur is None:
            return
        if self._cur in self.burned:
            self.burned_excluded.add(self._cur)
            return
        self.eligible_windows += 1
        entry = self._select()
        if entry is not None:
            self.entries.append(entry)

    def feed_row(self, r: dict) -> None:
        ct = r["close_time"]
        if ct != self._cur:
            self._finalize_window()
            if ct in self._seen:
                raise PolicyError(
                    f"non-contiguous close_time {ct!r}: a window's rows must be "
                    f"contiguous for the streaming evaluator"
                )
            self._seen.add(ct)
            self._reset_window(ct)
            self._q = int(r["quintile"])
        # Freshness law: consider only rows where BOTH leg ages <= 1.0 s.
        if max(float(r["high_leg_age_s"]), float(r["low_leg_age_s"])) > FRESH_MAX_LEG_AGE_S:
            return
        direction = r["direction"]
        tm = float(r["t_minus_s"])
        C = float(r["C"])
        ev = float(r["ev"])
        rec = {
            "t_minus_s": tm, "C": C, "payoff": float(r["payoff"]),
            "pin": r["pin"] == "1", "date": r["date"],
        }
        if direction == "strangle":
            if self._strangle is None and ev >= Q1_STRANGLE_EV_MIN:
                self._strangle = rec
        elif direction == "flip":
            if self._sub is None and self._is_sub_dollar(r, C):
                self._sub = rec
            if self._flip_ev is None and ev >= Q5_FLIP_EV_MIN:
                self._flip_ev = rec

    def _is_sub_dollar(self, r: dict, C: float) -> bool:
        """F4: authoritative sub-$1 test for a flip row. tape_sim emits ``hard_floor`` =
        (full-precision Decimal(C) < $1) for flip rows; use it when present. Track and (in
        strict mode) refuse on any disagreement with the 4dp-rounded ``float(C) < 1.0``."""
        float_test = C < SUB_DOLLAR_C_MAX
        hf = (r.get("hard_floor") or "").strip()
        if hf in ("0", "1"):
            authoritative = (hf == "1")
            if authoritative != float_test:
                self.hardfloor_floatc_divergences += 1
                if self.strict_hardfloor:
                    raise PolicyError(
                        f"hard_floor/float(C) sub-$1 disagreement at {r['close_time']}: "
                        f"hard_floor={hf!r} but float(C)={C} < 1.0 is {float_test}"
                    )
            return authoritative
        # hard_floor blank/absent for a flip row (unexpected) -> fall back to the C column.
        return float_test

    def feed_csv(self, path: str) -> None:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            missing = [c for c in _REQUIRED_COLUMNS if c not in (rd.fieldnames or [])]
            if missing:
                raise PolicyError(
                    f"tape_points {os.path.basename(path)} missing columns {missing}"
                )
            for r in rd:
                self.feed_row(r)

    def finalize(self) -> None:
        """Close the last open window (call once after all rows/files are fed)."""
        self._finalize_window()
        self._reset_window(None)


# ---------------------------------------------------------------------------
# Aggregation (aggregates only)
# ---------------------------------------------------------------------------
def _mean_cents(payoffs: List[float]) -> Optional[float]:
    if not payoffs:
        return None
    return (sum(payoffs) / len(payoffs)) * 100.0


def _day_clustered_bootstrap_cents(entries: List[dict], seed: int = 26,
                                   n_draws: int = 10_000
                                   ) -> Tuple[Optional[float], Optional[float]]:
    """Day-clustered 95% bootstrap of the pooled per-entry mean, in CENTS.

    Resample the DISTINCT days with entries (len(days) per draw) with replacement,
    pool every drawn day's per-entry payoffs (with multiplicity), score the pooled
    mean. Mirrors gate_fit.bootstrap_ci (fresh Random(seed), rng.choices, type-7
    percentile at 2.5 / 97.5)."""
    by_day: Dict[str, List[float]] = {}
    for e in entries:
        by_day.setdefault(e["date"], []).append(e["payoff"])
    days = sorted(by_day.keys())
    if not days:
        return None, None
    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(n_draws):
        drawn = rng.choices(days, k=len(days))
        pooled: List[float] = []
        for d in drawn:
            pooled.extend(by_day[d])
        if pooled:
            means.append(sum(pooled) / len(pooled))
    if not means:
        return None, None
    return _percentile(means, 2.5) * 100.0, _percentile(means, 97.5) * 100.0


def _by_source(entries: List[dict]) -> Dict[str, dict]:
    out = {}
    for src in ("Q1-strangle", "Q5-flip", "sub$1-flip"):
        es = [e for e in entries if e["source"] == src]
        pays = [e["payoff"] for e in es]
        out[src] = {
            "n": len(es),
            "mean_payoff_cents": _mean_cents(pays),
            "pins": sum(1 for e in es if e["pin"]),
        }
    return out


def _by_quintile(entries: List[dict]) -> Dict[str, dict]:
    out = {}
    for q in range(5):
        es = [e for e in entries if e["quintile"] == q]
        pays = [e["payoff"] for e in es]
        out[str(q)] = {
            "n": len(es),
            "mean_payoff_cents": _mean_cents(pays),
            "pins": sum(1 for e in es if e["pin"]),
        }
    return out


def _sub_dollar_by_quintile(entries: List[dict]) -> Dict[str, int]:
    out = {str(q): 0 for q in range(5)}
    for e in entries:
        if e["source"] == "sub$1-flip":
            out[str(e["quintile"])] += 1
    return out


# ---------------------------------------------------------------------------
# Train dry-run gate
# ---------------------------------------------------------------------------
def _train_eligible_from_receipts(chunks_dir: str = None) -> Optional[int]:
    """F1: authoritative TRAIN eligible-window count = sum of ``n_eligible_windows`` over
    the full60 chunk receipts (the tape run's own census truth, which — unlike the CSV —
    includes any event-less eligible windows). Returns None if the receipts are absent."""
    chunks_dir = chunks_dir or FULL60_CHUNKS_DIR
    if not os.path.isdir(chunks_dir):
        return None
    total = 0
    found = False
    for name in sorted(os.listdir(chunks_dir)):
        rp = os.path.join(chunks_dir, name, "tape_receipt.json")
        if not os.path.exists(rp):
            continue
        with open(rp, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if "n_eligible_windows" not in rec:
            continue
        total += int(rec["n_eligible_windows"])
        found = True
    return total if found else None


def run_dry_run_train(tape_path: str = None, out_path: str = None) -> Tuple[dict, bool]:
    """Run the evaluator over the TRAIN full60 tape (no sealed access) and write the
    dry-run certificate. Returns (certificate_dict, all_reference_matched)."""
    tape_path = tape_path or FULL60_TAPE
    out_path = out_path or DRYRUN_JSON
    if not os.path.exists(tape_path):
        raise RefuseToRun(f"REFUSE: train tape missing ({tape_path}); nothing to dry-run.")

    # strict_hardfloor: on train hard_floor and float(C)<1.0 must agree exactly (F4).
    ev = PolicyEvaluator(burned_close_times=(), strict_hardfloor=True)
    ev.feed_csv(tape_path)
    ev.finalize()

    payoffs = [e["payoff"] for e in ev.entries]
    mean_cents = _mean_cents(payoffs)
    by_source_counts = {k: v["n"] for k, v in _by_source(ev.entries).items()}
    sub_by_q = _sub_dollar_by_quintile(ev.entries)

    # F1: eligible count from the authoritative source (chunk receipts), NOT the CSV.
    eligible_authoritative = _train_eligible_from_receipts()
    eligible_csv = ev.eligible_windows
    if eligible_authoritative is not None:
        eligible_windows = eligible_authoritative
        eligible_source = "full60_chunk_receipts"
        # On train there are zero event-less eligible windows: the authoritative census
        # truth must equal the CSV with-events count. A mismatch is a real signal.
        if eligible_authoritative != eligible_csv:
            raise RefuseToRun(
                f"REFUSE: train authoritative eligible {eligible_authoritative} != "
                f"CSV-with-events {eligible_csv}; the full60 tape/receipts are inconsistent."
            )
    else:
        # Fallback: receipts absent. Use the CSV count but require it to reproduce the
        # frozen reference (train has provably zero event-less eligible windows).
        eligible_windows = eligible_csv
        eligible_source = "csv_with_events_fallback"

    cert = {
        "STATUS": "TRAIN DRY-RUN CERTIFICATE",
        "source_tape": os.path.relpath(tape_path, _ROOT),
        "eligible_windows": eligible_windows,
        "eligible_windows_source": eligible_source,
        "eligible_windows_csv_with_events": eligible_csv,
        "hardfloor_floatc_divergences": ev.hardfloor_floatc_divergences,
        "entered": len(ev.entries),
        "pooled_mean_payoff_cents_2dp": round(mean_cents, 2) if mean_cents is not None else None,
        "pooled_mean_payoff_cents_6dp": round(mean_cents, 6) if mean_cents is not None else None,
        "pins_among_entered": sum(1 for e in ev.entries if e["pin"]),
        "by_source": by_source_counts,
        "sub_dollar_by_quintile": sub_by_q,
        "burned_excluded_count": len(ev.burned_excluded),
        "policy_params": POLICY_PARAMS,
        "policy_params_sha256": POLICY_PARAMS_SHA256,
    }

    mismatches = _reference_mismatches(cert)
    cert["reference_match"] = (not mismatches)
    cert["reference_mismatches"] = mismatches

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cert, f, indent=2, sort_keys=True)
    return cert, (not mismatches)


def _reference_mismatches(cert: dict) -> List[str]:
    """Return a list of human-readable mismatch strings (empty == all match)."""
    out = []
    for key in ("eligible_windows", "entered", "pins_among_entered"):
        if cert.get(key) != REFERENCE[key]:
            out.append(f"{key}={cert.get(key)} != {REFERENCE[key]}")
    got = cert.get("pooled_mean_payoff_cents_2dp")
    if got is None or abs(got - REFERENCE["pooled_mean_payoff_cents_2dp"]) > 1e-6:
        out.append(f"pooled_mean_payoff_cents_2dp={got} != "
                   f"{REFERENCE['pooled_mean_payoff_cents_2dp']}")
    if cert.get("by_source") != REFERENCE["by_source"]:
        out.append(f"by_source={cert.get('by_source')} != {REFERENCE['by_source']}")
    if cert.get("sub_dollar_by_quintile") != REFERENCE["sub_dollar_by_quintile"]:
        out.append(f"sub_dollar_by_quintile={cert.get('sub_dollar_by_quintile')} != "
                   f"{REFERENCE['sub_dollar_by_quintile']}")
    return out


# ---------------------------------------------------------------------------
# Sealed-read preflight (fail-closed, in order — any failure REFUSES)
# ---------------------------------------------------------------------------
def _assert_dryrun_certificate() -> dict:
    if not os.path.exists(DRYRUN_JSON):
        raise RefuseToRun(
            f"REFUSE: train dry-run certificate missing ({DRYRUN_JSON}); run "
            f"--dry-run-train first (it must reproduce the train reference numbers)."
        )
    with open(DRYRUN_JSON, "r", encoding="utf-8") as f:
        cert = json.load(f)
    if cert.get("policy_params_sha256") != POLICY_PARAMS_SHA256:
        raise RefuseToRun(
            f"REFUSE: dry-run policy_params_sha256 {cert.get('policy_params_sha256')} != "
            f"{POLICY_PARAMS_SHA256}; the certificate was made by a different policy."
        )
    mismatches = _reference_mismatches(cert)
    if mismatches:
        raise RefuseToRun(
            "REFUSE: dry-run certificate does not match the train reference: "
            + "; ".join(mismatches)
        )
    return cert


def preflight_sealed() -> str:
    """Run every sealed gate. Return the falsifier text on success; else RefuseToRun."""
    # 1. falsifier present + FROZEN (line-based, A3.6).
    if not os.path.exists(FALSIFIER_MD):
        raise RefuseToRun(
            f"REFUSE: falsifier missing ({FALSIFIER_MD}); the read has no frozen clauses."
        )
    with open(FALSIFIER_MD, "r", encoding="utf-8") as f:
        text = f.read()
    if not falsifier_is_frozen(text):
        raise RefuseToRun(
            "REFUSE: falsifier.md is not FROZEN (no line reads exactly 'STATUS: FROZEN'). "
            "The clauses must be frozen on Brad's explicit go before the read."
        )
    # 2. census sha bound to the pinned constant.
    if not os.path.exists(CENSUS_CSV):
        raise RefuseToRun(f"REFUSE: census_train.csv missing ({CENSUS_CSV}).")
    actual_sha = sha256_file(CENSUS_CSV)
    if actual_sha != CENSUS_TRAIN_SHA256:
        raise RefuseToRun(
            f"REFUSE: census_train.csv sha256 {actual_sha} != pinned "
            f"{CENSUS_TRAIN_SHA256}; the quintile edges/EV curve are not the frozen ones."
        )
    # 3. every sealed day-file present for both series x {markets, candles, trades}.
    missing = []
    for series in ("15-minute", "1-hour"):
        for kind in ("markets", "candles", "trades"):
            for d in SEALED_DATES:
                p = data_path(series, kind, d)
                if not os.path.exists(p):
                    missing.append(os.path.relpath(p, _ROOT))
    if missing:
        raise RefuseToRun(
            f"REFUSE: {len(missing)} sealed day-file(s) missing; first few: "
            f"{missing[:5]}"
        )
    # 4. dry-run certificate valid.
    _assert_dryrun_certificate()
    # 5. one-shot guard (F2): refuse if EITHER the start marker OR the result JSON exists.
    #    The marker is written BEFORE the first sealed byte, so a mid-run crash still trips
    #    this gate — the read counts as SPENT until Brad clears the marker by hand.
    if os.path.exists(STARTED_MARKER):
        raise RefuseToRun(
            f"REFUSE: start marker {STARTED_MARKER} exists; a prior sealed read was started "
            f"(and may have crashed). The read is one-shot and SPENT. Only Brad may clear "
            f"this marker after a post-mortem / SEAL.md re-registration."
        )
    if os.path.exists(RESULT_JSON):
        raise RefuseToRun(
            f"REFUSE: {RESULT_JSON} already exists; the sealed read is one-shot and "
            f"refuses a second execution."
        )
    return text


# ---------------------------------------------------------------------------
# The one-shot sealed read
# ---------------------------------------------------------------------------
def _sealed_aggregates(ev: PolicyEvaluator, eligible_from_tape: int) -> dict:
    entries = ev.entries
    n = len(entries)
    payoffs = [e["payoff"] for e in entries]
    pooled_mean_cents = _mean_cents(payoffs)
    if n > 1:
        m = sum(payoffs) / n
        var = sum((p - m) ** 2 for p in payoffs) / (n - 1)
        se_cents = math.sqrt(var / n) * 100.0
    else:
        se_cents = None
    lb_cents, ub_cents = _day_clustered_bootstrap_cents(entries)

    by_source = _by_source(entries)
    by_quintile = _by_quintile(entries)

    # Per-day entry means keyed by day INDEX (0..16), NOT date (aggregates discipline).
    ordered_days = sorted(SEALED_DATES)
    day_payoffs: Dict[str, List[float]] = {}
    for e in entries:
        day_payoffs.setdefault(e["date"], []).append(e["payoff"])
    per_day = []
    for i, d in enumerate(ordered_days):
        pays = day_payoffs.get(d, [])
        per_day.append({
            "day_index": i,
            "n_entries": len(pays),
            "mean_payoff_cents": _mean_cents(pays),
        })
    n_days_with_entries = sum(1 for p in per_day if p["n_entries"] > 0)

    # F1: the C4 denominator is the AUTHORITATIVE census-eligible count from tape_sim's
    # per-day returns, MINUS the eligible burned windows we can prove eligible (those that
    # emitted >=1 event, hence were seen). Event-less eligible burned windows (<=2, unknown)
    # remain in the denominator -> entry_rate is a slight UNDER-estimate, bounded and safe.
    burned_seen = len(ev.burned_excluded)
    eligible = eligible_from_tape - burned_seen
    eligible_with_events_nonburned = ev.eligible_windows
    # Cross-check: authoritative truth must be >= observed windows-with-events; anything
    # less is an impossible accounting error -> hard fail.
    event_less_estimate = eligible - eligible_with_events_nonburned
    if event_less_estimate < 0:
        raise PolicyError(
            f"eligible accounting error: authoritative eligible {eligible} (tape "
            f"{eligible_from_tape} - burned-seen {burned_seen}) < windows-with-events "
            f"{eligible_with_events_nonburned}"
        )
    entry_rate = (n / eligible) if eligible else None

    result = {
        "STATUS": "SEALED READ EXECUTED",
        "policy": "Rung 1.5 frozen expanded quintile-conditional policy",
        "policy_params": POLICY_PARAMS,
        "policy_params_sha256": POLICY_PARAMS_SHA256,
        "census_csv_sha256": CENSUS_TRAIN_SHA256,
        "staleness_s": STALENESS_S,
        "n_sealed_days": len(SEALED_DATES),
        # F5: report burned hours CONFIGURED (always the 2 pinned close_times) AND the
        # number actually SEEN in the tape, so any under-reporting is visible. (Both burned
        # hours were hand-traded, so events almost certainly exist for them.)
        "burned_close_times_configured": sorted(BURNED_CLOSE_TIMES_SEALED),
        "burned_close_times_configured_count": len(BURNED_CLOSE_TIMES_SEALED),
        "burned_close_times_seen_count": len(ev.burned_excluded),
        "burned_hour_exclusion_count": len(ev.burned_excluded),
        # F1: eligible-window accounting (all counts are aggregate window counts).
        "eligible_windows": eligible,                       # C4 denominator (authoritative)
        "eligible_windows_source": "tape_sim_per_day_n_eligible_minus_burned_seen",
        "eligible_windows_tape_truth_all": eligible_from_tape,
        "eligible_windows_with_events_nonburned": eligible_with_events_nonburned,
        "eligible_windows_event_less_estimate": event_less_estimate,
        # F4: divergences between hard_floor and float(C)<1.0 on fresh flip rows (0 on train).
        "hardfloor_floatc_divergences": ev.hardfloor_floatc_divergences,
        "entries": n,
        "entry_rate": entry_rate,
        "pooled_mean_payoff_cents": pooled_mean_cents,
        "pooled_se_cents": se_cents,
        "bootstrap_ci95_cents": [lb_cents, ub_cents],
        "bootstrap_seed": 26,
        "bootstrap_draws": 10_000,
        "pins_among_entered": sum(1 for e in entries if e["pin"]),
        "by_source": by_source,
        "by_quintile": by_quintile,
        "sub_dollar_by_quintile": _sub_dollar_by_quintile(entries),
        "n_days_with_entries": n_days_with_entries,
        "per_day_entry_means_by_index": per_day,
        # Clause verdict INPUTS ONLY — the bridge applies falsifier SECTION 2.
        "clause_inputs": {
            "C1_pooled_mean_cents": pooled_mean_cents,
            "C2_bootstrap_lb_cents": lb_cents,
            "C2_bootstrap_ub_cents": ub_cents,
            "C3_Q1_strangle_mean_cents": by_source["Q1-strangle"]["mean_payoff_cents"],
            "C3_Q5_flip_mean_cents": by_source["Q5-flip"]["mean_payoff_cents"],
            "C4_entry_rate": entry_rate,
        },
        "_note": (
            "Aggregates only. Clause verdicts (C1-C4, graduation bar) are evaluated by "
            "the bridge against the FROZEN falsifier.md SECTION 2 thresholds. Burned "
            "hours are excluded from every clause quantity and reported as a count. All "
            "tape prices assume joinable prints (fillability caveat)."
        ),
    }
    return result


def run_sealed() -> dict:
    """Execute the one-shot sealed read. Preflight MUST pass first (before any sealed
    byte). Runs tape_sim.run() ONE SEALED DAY AT A TIME (memory law), feeds each day's
    tape_points.csv through the policy evaluator, writes AGGREGATES ONLY."""
    preflight_sealed()

    os.makedirs(SEALED_EVAL_DIR, exist_ok=True)
    # F2: write the atomic start-marker BEFORE any sealed byte. 'x' mode creates it
    # exclusively (fails if it somehow already exists, closing the preflight->open TOCTOU).
    # From here on a crash leaves the read SPENT and fail-closed.
    with open(STARTED_MARKER, "x", encoding="utf-8") as f:
        f.write(json.dumps({"STATUS": "IN_PROGRESS",
                            "policy_params_sha256": POLICY_PARAMS_SHA256}) + "\n")

    ev = PolicyEvaluator(burned_close_times=BURNED_CLOSE_TIMES_SEALED)
    eligible_from_tape = 0            # F1: authoritative census-eligible total across days
    for d in SEALED_DATES:
        day_out = os.path.join(SEALED_EVAL_DIR, f"d{d}")
        # THE SEALED READ — tape_sim opens the sealed byte here (loader A3.7 re-checks
        # the frozen falsifier). One day per call: the monolithic multi-day run
        # MemoryErrors, so we chunk by day and only the aggregate-sized evaluator state
        # persists across days.
        day_agg = _tape_sim.run([d], day_out, staleness_s=STALENESS_S,
                                acknowledge_sealed_read=True, census_csv=CENSUS_CSV)
        eligible_from_tape += int(day_agg["n_eligible_windows"])   # F1: census truth
        points = os.path.join(day_out, "tape_points.csv")
        if not os.path.exists(points):
            raise RefuseToRun(
                f"REFUSE: expected tape_points.csv for sealed day was not produced "
                f"({points}); aborting the read."
            )
        ev.feed_csv(points)

    ev.finalize()
    result = _sealed_aggregates(ev, eligible_from_tape)

    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rung 1.5 unseal runner — train dry-run gate + one-shot sealed read")
    ap.add_argument("--dry-run-train", action="store_true",
                    help="run the policy evaluator over the TRAIN full60 tape (no sealed "
                         "access) and write the dry-run certificate")
    ap.add_argument("--i-have-brads-explicit-go", action="store_true",
                    help="required for the sealed read; only the bridge passes this, once")
    args = ap.parse_args(argv)

    if args.dry_run_train:
        cert, ok = run_dry_run_train()
        print("TRAIN DRY-RUN — wrote", DRYRUN_JSON)
        print(f"  eligible windows : {cert['eligible_windows']} "
              f"(source: {cert['eligible_windows_source']}; "
              f"csv-with-events {cert['eligible_windows_csv_with_events']})")
        print(f"  hard_floor/floatC divergences: {cert['hardfloor_floatc_divergences']}")
        print(f"  entered          : {cert['entered']}")
        print(f"  pooled mean      : {cert['pooled_mean_payoff_cents_2dp']} cents "
              f"({cert['pooled_mean_payoff_cents_6dp']} 6dp)")
        print(f"  pins among entered: {cert['pins_among_entered']}")
        print(f"  by source        : {cert['by_source']}")
        print(f"  sub-$1 by quintile: {cert['sub_dollar_by_quintile']}")
        print(f"  policy sha        : {cert['policy_params_sha256']}")
        if not ok:
            raise RefuseToRun(
                "REFUSE: dry-run did NOT reproduce the train reference: "
                + "; ".join(cert["reference_mismatches"])
            )
        print("  reference match  : OK (all frozen train numbers reproduced)")
        return cert

    if args.i_have_brads_explicit_go:
        result = run_sealed()
        print("SEALED READ EXECUTED — aggregates written to", RESULT_JSON)
        print(f"  eligible windows : {result['eligible_windows']} "
              f"(tape-truth {result['eligible_windows_tape_truth_all']} - burned-seen "
              f"{result['burned_close_times_seen_count']}; "
              f"event-less est {result['eligible_windows_event_less_estimate']})")
        print(f"  entries          : {result['entries']} "
              f"(rate {result['entry_rate']})")
        print(f"  pooled mean      : {result['pooled_mean_payoff_cents']} cents "
              f"(SE {result['pooled_se_cents']})")
        print(f"  bootstrap 95% CI : {result['bootstrap_ci95_cents']} cents")
        print(f"  pins             : {result['pins_among_entered']}")
        print(f"  burned excluded  : {result['burned_hour_exclusion_count']} seen "
              f"(configured {result['burned_close_times_configured_count']})")
        print(f"  hard_floor/floatC divergences: {result['hardfloor_floatc_divergences']}")
        return result

    raise RefuseToRun(
        "REFUSE: pass --dry-run-train (train gate) or --i-have-brads-explicit-go "
        "(the one-shot sealed read; only the bridge does this, once)."
    )


if __name__ == "__main__":
    main()
