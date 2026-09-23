"""report.py — a read-only per-window report over the V3.3 ledger.

``python -m service.v33.report [--days N]`` prints:
  * one per-window LADDER line (mode/dry_sim, spot bucket, rungs filled, shallowest/deepest margin,
    contracts, ladder lock, roll integrity) + totals;
  * the LADDER SCOREBOARD — per margin/rung n, mean solved E, mean realised lock, shortfall, %positive,
    with the DRY (dry_sim) rows in a clearly separate section, NEVER pooled with realised; pooled
    per-contract stats; the capture ratio at the 10c margin (Registration-3 definition, V3.2-comparable);
  * the FALSIFIER GATE TABLE (§6) computed live from realised rows: each gate -> value/threshold/status;
  * the SIDE-BY-SIDE block (Brad's watch-and-compare): per hour V3.2 vs V3.3, day totals, and the
    windows where V3.3 (dry) would have entered and V3.2 did not, and vice versa;
  * the DEEP END (SO-3, observation only) block: the deep 16..25c rungs the tape reached + absorption.

Reads ONLY ``ledger/v33_ledger.jsonl`` (and, for the side-by-side, ``ledger/v32_ledger.jsonl``) plus the
frozen V3.3 params (for n_min) — no network, no sealed file, no orders. The V3.2 report is unchanged and
keeps printing for the V3.2 rows.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from service.v33.falsifier_pins import (
    V33_CAPTURE_RATIO_MIN,
    V33_FALSIFIER_CAPTURE_MARGIN_C,
    V33_FALSIFIER_MAX_ONE_LEGGED,
    V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS,
    V33_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V33_FALSIFIER_MIN_N,
    V33_FALSIFIER_MIN_PCT_POSITIVE,
    V33_FALSIFIER_MIN_RUNG_FILLS_FOR_SHORTFALL,
    V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO,
    V33_FALSIFIER_SHADOW_GAP_E,
    V33_KILL_MEAN_LOCK_CENTS,
    V33_KILL_MIN_N,
)
from service.v33.ledger import DEFAULT_V33_LEDGER_PATH, load_v33_rows
from service.v33.params import load_v33_params
from service.v32.ledger import DEFAULT_V32_LEDGER_PATH, load_v32_rows

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _recent_days(rows: list[dict[str, Any]], days: int | None) -> list[dict[str, Any]]:
    if days is None:
        return rows
    ds = sorted({str(r.get("close_time", ""))[:10] for r in rows if r.get("close_time")}, reverse=True)
    keep = set(ds[:days])
    return [r for r in rows if str(r.get("close_time", ""))[:10] in keep]


def _is_window_row(r: dict[str, Any]) -> bool:
    """A window row (not a settlement backfill row)."""
    return r.get("mode") != "backfill"


def _is_realised(r: dict[str, Any]) -> bool:
    """A REALISED window row: armed AND not a dry_sim (real money on the venue)."""
    return _is_window_row(r) and bool(r.get("armed")) and not bool(r.get("dry_sim"))


def _is_dry(r: dict[str, Any]) -> bool:
    """A DRY_SIM window row (simulated ideal fills; never realised money)."""
    return _is_window_row(r) and bool(r.get("dry_sim"))


def _row_rung_fills(r: dict[str, Any]) -> list[dict[str, Any]]:
    rf = r.get("rung_fills")
    return rf if isinstance(rf, list) else []


def _margin_c(rf: dict[str, Any]) -> int | None:
    """A rung fill's margin in whole cents = round(E_rung * 100). E_rung is the derived margin label
    (E_min + (n_top - price)); the 10c rung is margin_c 10, etc."""
    e = _dec(rf.get("E_rung"))
    if e is None:
        return None
    return int((e * 100).to_integral_value())


def _shadow_below_min(sub: dict[str, Any], n_min: Decimal) -> bool:
    """A shadow fill whose derived n (1 - offer) is below n_min is NOT a live-reachable counterfactual
    (same exclusion as V3.2 Registration 3 / n_min gate)."""
    offer = _dec(sub.get("offer"))
    if offer is None:
        return False
    return (_ONE - offer) < n_min


# ---------------------------------------------------------------------------
# per-window ladder lines + totals (L2 shape kept; the tests pin these)
# ---------------------------------------------------------------------------
def build_v33_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the V3.3 window rows into per-window LADDER lines + totals + the ladder scoreboard, the
    falsifier gate table, and the deep-end (SO-3) summary (pure; the CLI renders it)."""
    windows: list[dict[str, Any]] = []
    tot_rungs = 0
    tot_contracts = 0
    tot_ladder_lock = _ZERO
    tot_rolls = 0
    tot_single = 0
    tot_dry_sim_fills = 0
    tot_stand_downs = 0
    for r in rows:
        if not _is_window_row(r):
            continue
        lad = r.get("ladder") or {}
        ll = _dec(lad.get("ladder_lock")) or _ZERO
        tot_rungs += int(lad.get("rungs_filled", 0) or 0)
        tot_contracts += int(lad.get("contracts", 0) or 0)
        tot_ladder_lock += ll
        tot_rolls += int(lad.get("roll_count", 0) or 0)
        tot_single += int(lad.get("roll_single_order_count", 0) or 0)
        tot_dry_sim_fills += int((r.get("synth_counts") or {}).get("dry_sim_fill", 0) or 0)
        if r.get("stand_down"):
            tot_stand_downs += 1
        windows.append({
            "close_time": r.get("close_time"),
            "mode": r.get("effective_mode") or r.get("mode"),
            "dry_sim": bool(r.get("dry_sim")),
            "bucket": r.get("spot_bucket_ticker") or ("STAND DOWN" if r.get("stand_down") else "-"),
            "rungs_filled": lad.get("rungs_filled", 0),
            "contracts": lad.get("contracts", 0),
            "shallowest_c": lad.get("shallowest_margin_c"),
            "deepest_c": lad.get("deepest_margin_c"),
            "ladder_lock": lad.get("ladder_lock"),
            "roll_count": lad.get("roll_count", 0),
            "roll_single_order_ratio": lad.get("roll_single_order_ratio"),
            "stand_down_reason": r.get("stand_down_reason"),
        })
    roll_ratio = (Decimal(tot_single) / Decimal(tot_rolls)) if tot_rolls else None
    return {
        "windows": windows,
        "scoreboard": build_ladder_scoreboard(rows),
        "gate_table": build_falsifier_gate_table(rows),
        "deep_end": build_deep_end(rows),
        "totals": {
            "windows": sum(1 for r in rows if _is_window_row(r)),
            "rungs_filled": tot_rungs,
            "contracts": tot_contracts,
            "ladder_lock": tot_ladder_lock,
            "roll_count": tot_rolls,
            "roll_single_order_count": tot_single,
            "roll_single_order_ratio": roll_ratio,
            "dry_sim_fills": tot_dry_sim_fills,
            "stand_downs": tot_stand_downs,
        },
    }


# ---------------------------------------------------------------------------
# LADDER SCOREBOARD (per margin; DRY and realised separated, never pooled)
# ---------------------------------------------------------------------------
def _margin_stats(rung_fills: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per-margin (whole cents) aggregate over a flat list of rung fills: n contracts, mean solved E
    (lock_solved), mean realised lock, shortfall (solved - realised), %positive. Locks in CENTS,
    count-weighted (each contract one observation)."""
    by: dict[int, dict[str, list[Decimal]]] = defaultdict(
        lambda: {"solved": [], "realised": [], "n": [Decimal(0)]})
    for rf in rung_fills:
        m = _margin_c(rf)
        if m is None:
            continue
        cnt = int(rf.get("count", 1) or 1)
        by[m]["n"][0] += Decimal(cnt)
        ls = _dec(rf.get("lock_solved"))
        if ls is not None:
            by[m]["solved"].extend([ls * 100] * cnt)
        rl = _dec(rf.get("realized_lock"))
        if rl is not None:
            by[m]["realised"].extend([rl * 100] * cnt)
    out: dict[int, dict[str, Any]] = {}
    for m in sorted(by):
        solved = by[m]["solved"]
        realised = by[m]["realised"]
        ms = (sum(solved, _ZERO) / Decimal(len(solved))) if solved else None
        mr = (sum(realised, _ZERO) / Decimal(len(realised))) if realised else None
        sf = (ms - mr) if (ms is not None and mr is not None) else None
        pos = sum(1 for x in realised if x > 0)
        pctp = (Decimal(pos) * 100 / Decimal(len(realised))) if realised else None
        out[m] = {"margin_c": m, "n_contracts": int(by[m]["n"][0]), "n_completed": len(realised),
                  "mean_solved_c": ms, "mean_realised_c": mr, "shortfall_c": sf, "pct_positive": pctp}
    return out


def _capture_at_margin(rows: list[dict[str, Any]], margin_c: int, n_min: Decimal
                       ) -> tuple[int, int, Decimal | None]:
    """Registration-3 capture ratio at ``margin_c``, V3.2-comparable: over ARMED+bucket REALISED windows,
    numerator = windows with BOTH a valid shadow E=0.10 fill AND a COMPLETED rung fill at ``margin_c``;
    denominator = windows with a valid shadow E=0.10 fill (a below-n_min shadow fill is excluded)."""
    num = 0
    den = 0
    for r in rows:
        if not (r.get("effective_mode") == "armed" and r.get("spot_bucket_ticker")):
            continue
        if r.get("dry_sim"):
            continue
        sub = (r.get("shadow") or {}).get(V33_FALSIFIER_SHADOW_GAP_E)
        if not (sub and sub.get("filled")) or _shadow_below_min(sub, n_min):
            continue
        den += 1
        if any(_margin_c(rf) == margin_c and _dec(rf.get("realized_lock")) is not None
               for rf in _row_rung_fills(r)):
            num += 1
    ratio = (Decimal(num) / Decimal(den)) if den else None
    return num, den, ratio


def _pooled_ladder(rows_subset: list[dict[str, Any]]) -> dict[str, Any]:
    """Pooled per-contract stats over a set of window rows (realised OR dry): contracts, ladder lock,
    mean realised lock, %positive, rolls + single-order-roll ratio, mean venue activity per window."""
    all_rf = [rf for r in rows_subset for rf in _row_rung_fills(r)]
    realised_locks: list[Decimal] = []
    contracts = 0
    for rf in all_rf:
        cnt = int(rf.get("count", 1) or 1)
        contracts += cnt
        rl = _dec(rf.get("realized_lock"))
        if rl is not None:
            realised_locks.extend([rl * 100] * cnt)
    ladder_lock = _ZERO
    for r in rows_subset:
        ladder_lock += _dec((r.get("ladder") or {}).get("ladder_lock")) or _ZERO
    mean_lock = (sum(realised_locks, _ZERO) / Decimal(len(realised_locks))) if realised_locks else None
    pos = sum(1 for x in realised_locks if x > 0)
    pctp = (Decimal(pos) * 100 / Decimal(len(realised_locks))) if realised_locks else None
    tot_rolls = sum(int((r.get("ladder") or {}).get("roll_count", 0) or 0) for r in rows_subset)
    tot_single = sum(int((r.get("ladder") or {}).get("roll_single_order_count", 0) or 0)
                     for r in rows_subset)
    roll_ratio = (Decimal(tot_single) / Decimal(tot_rolls)) if tot_rolls else None
    nwin = len(rows_subset)
    creates = sum(int((r.get("ladder") or {}).get("creates", 0) or 0) for r in rows_subset)
    amends = sum(int((r.get("ladder") or {}).get("amends_attempted", 0) or 0) for r in rows_subset)
    cancels = sum(int((r.get("ladder") or {}).get("cancels", 0) or 0) for r in rows_subset)
    return {
        "windows": nwin, "contracts": contracts, "ladder_lock_c": ladder_lock * 100,
        "n_completed": len(realised_locks), "mean_realised_lock_c": mean_lock, "pct_positive": pctp,
        "roll_count": tot_rolls, "roll_single_order_count": tot_single,
        "roll_single_order_ratio": roll_ratio,
        "creates_per_window": (Decimal(creates) / Decimal(nwin)) if nwin else None,
        "amends_per_window": (Decimal(amends) / Decimal(nwin)) if nwin else None,
        "cancels_per_window": (Decimal(cancels) / Decimal(nwin)) if nwin else None,
    }


def _margins_list(subset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats = _margin_stats([rf for r in subset for rf in _row_rung_fills(r)])
    return [stats[m] for m in sorted(stats)]


def build_ladder_scoreboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The LADDER SCOREBOARD: per-margin stats for REALISED rows and (separately) DRY rows; per-day and
    pooled; the 10c-margin capture ratio. dry_sim rows are NEVER pooled into realised."""
    realised = [r for r in rows if _is_realised(r)]
    dry = [r for r in rows if _is_dry(r)]
    n_min = load_v33_params().n_min
    cap_num, cap_den, cap_ratio = _capture_at_margin(rows, V33_FALSIFIER_CAPTURE_MARGIN_C, n_min)

    def per_day(subset: list[dict[str, Any]]) -> list[dict[str, Any]]:
        days: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in subset:
            days[str(r.get("close_time", ""))[:10]].append(r)
        return [{"day": day, **_pooled_ladder(days[day])} for day in sorted(days)]

    return {
        "realised": {"margins": _margins_list(realised), "pooled": _pooled_ladder(realised),
                     "per_day": per_day(realised)},
        "dry": {"margins": _margins_list(dry), "pooled": _pooled_ladder(dry),
                "per_day": per_day(dry)},
        "capture_10c": {"margin_c": V33_FALSIFIER_CAPTURE_MARGIN_C, "live": cap_num, "shadow": cap_den,
                        "ratio": cap_ratio},
    }


# ---------------------------------------------------------------------------
# FALSIFIER GATE TABLE (§6) — realised rows only
# ---------------------------------------------------------------------------
def build_falsifier_gate_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The §6 gate table computed LIVE from realised rows. Each gate -> value, threshold, PASS/FAIL/
    n-too-small; the n>=30 rung-fill counter; the kill conditions. Realised (armed, not dry_sim) only."""
    realised = [r for r in rows if _is_realised(r)]
    all_rf = [rf for r in realised for rf in _row_rung_fills(r)]
    n = sum(int(rf.get("count", 1) or 1) for rf in all_rf)                 # rung-fill contracts
    realised_locks: list[Decimal] = []
    for rf in all_rf:
        rl = _dec(rf.get("realized_lock"))
        if rl is not None:
            realised_locks.extend([rl * 100] * int(rf.get("count", 1) or 1))
    mean_lock = (sum(realised_locks, _ZERO) / Decimal(len(realised_locks))) if realised_locks else None
    pos = sum(1 for x in realised_locks if x > 0)
    pct_pos = (Decimal(pos) * 100 / Decimal(len(realised_locks))) if realised_locks else None

    mstats = _margin_stats(all_rf)
    worst_shortfall: Decimal | None = None
    worst_margin: int | None = None
    judged_rungs = 0
    for m, d in mstats.items():
        if (d["n_completed"] >= V33_FALSIFIER_MIN_RUNG_FILLS_FOR_SHORTFALL
                and d["shortfall_c"] is not None):
            judged_rungs += 1
            if worst_shortfall is None or d["shortfall_c"] > worst_shortfall:
                worst_shortfall = d["shortfall_c"]
                worst_margin = m

    n_min = load_v33_params().n_min
    cap_num, cap_den, cap_ratio = _capture_at_margin(rows, V33_FALSIFIER_CAPTURE_MARGIN_C, n_min)

    one_legged = 0
    for r in realised:
        for b in (r.get("wing_batch_sets") or []):
            if b.get("one_legged"):
                one_legged += int(b.get("fill_count", 1) or 1)

    tot_rolls = sum(int((r.get("ladder") or {}).get("roll_count", 0) or 0) for r in realised)
    tot_single = sum(int((r.get("ladder") or {}).get("roll_single_order_count", 0) or 0)
                     for r in realised)
    roll_ratio = (Decimal(tot_single) / Decimal(tot_rolls)) if tot_rolls else None

    gates: list[dict[str, Any]] = []

    def add(gate: str, value: Any, threshold: str, ok: bool, n_ok: bool) -> None:
        status = "n-too-small" if (not n_ok or value is None) else ("PASS" if ok else "FAIL")
        gates.append({"gate": gate, "value": value, "threshold": threshold, "status": status})

    n_ok = n >= V33_FALSIFIER_MIN_N
    add("mean true lock", mean_lock, f">= +{V33_FALSIFIER_MIN_MEAN_LOCK_CENTS}c",
        (mean_lock is not None and mean_lock >= V33_FALSIFIER_MIN_MEAN_LOCK_CENTS), n_ok)
    add("per-rung shortfall (worst, >=3 fills)", worst_shortfall,
        f"<= {V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS}c",
        (worst_shortfall is not None and worst_shortfall <= V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS),
        judged_rungs > 0)
    add("%positive", pct_pos, f">= {V33_FALSIFIER_MIN_PCT_POSITIVE}%",
        (pct_pos is not None and pct_pos >= V33_FALSIFIER_MIN_PCT_POSITIVE), n_ok)
    # capture (F1, fail-closed): BELOW n>=MIN_N it is n-too-small; AT n>=MIN_N a None ratio (no valid
    # shadow availability to measure execution against) is a FAIL, not a pass -- matching the doc's
    # "ALIVE iff ALL of ..." clause and V3.2's verdict logic (a None capture failed there too).
    if not n_ok:
        cap_status = "n-too-small"
    elif cap_ratio is None:
        cap_status = "FAIL"
    else:
        cap_status = "PASS" if cap_ratio >= V33_CAPTURE_RATIO_MIN else "FAIL"
    gates.append({"gate": f"capture ratio @ {V33_FALSIFIER_CAPTURE_MARGIN_C}c", "value": cap_ratio,
                  "threshold": f">= {V33_CAPTURE_RATIO_MIN}", "status": cap_status})
    add("one-legged contracts", Decimal(one_legged), f"<= {V33_FALSIFIER_MAX_ONE_LEGGED}",
        (one_legged <= V33_FALSIFIER_MAX_ONE_LEGGED), True)
    add("single-order-roll ratio", roll_ratio, f">= {V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO}",
        (roll_ratio is not None and roll_ratio >= V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO),
        tot_rolls > 0)

    # verdict + kill (kills apply even before n>=30).
    kills: list[str] = []
    if mean_lock is not None and n >= V33_KILL_MIN_N and mean_lock < V33_KILL_MEAN_LOCK_CENTS:
        kills.append(f"mean lock {mean_lock:.2f}c < +{V33_KILL_MEAN_LOCK_CENTS}c at n={n} "
                     f"(>= {V33_KILL_MIN_N})")
    if one_legged > V33_FALSIFIER_MAX_ONE_LEGGED:
        kills.append(f"one-legged {one_legged} > {V33_FALSIFIER_MAX_ONE_LEGGED}")
    if kills:
        verdict = "KILL: " + "; ".join(kills)
    elif n < V33_FALSIFIER_MIN_N:
        verdict = f"n<{V33_FALSIFIER_MIN_N} pending (n={n})"
    else:
        fails = [g["gate"] for g in gates if g["status"] == "FAIL"]
        verdict = "ALIVE-so-far" if not fails else ("KILL: " + "; ".join(fails))

    return {
        "n_rung_fills": n, "min_n": V33_FALSIFIER_MIN_N, "mean_lock_c": mean_lock,
        "pct_positive": pct_pos, "worst_shortfall_c": worst_shortfall,
        "worst_shortfall_margin_c": worst_margin,
        "capture_live": cap_num, "capture_shadow": cap_den, "capture_ratio": cap_ratio,
        "one_legged": one_legged, "roll_count": tot_rolls, "roll_single_order_ratio": roll_ratio,
        "gates": gates, "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# DEEP END (SO-3, observation only)
# ---------------------------------------------------------------------------
def build_deep_end(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the SO-3 deep-observation ladder (16..25c) across windows: for each deep margin, the
    windows the tape reached it, the mean ideal lock at first reach, and total absorption (lots the tape
    printed at/through the rung). Observation only — this never reflects a position we held."""
    by: dict[int, dict[str, Any]] = {}
    windows_with_deep = 0
    for r in rows:
        if not _is_window_row(r):
            continue
        deep = r.get("deep_obs") or {}
        rungs = deep.get("rungs")
        if not isinstance(rungs, list) or not rungs:
            continue
        windows_with_deep += 1
        for o in rungs:
            m = int(o.get("margin_c", 0) or 0)
            slot = by.setdefault(m, {"margin_c": m, "reached_windows": 0, "absorption_lots": _ZERO,
                                     "prints_through": 0, "locks_c": []})
            if o.get("reached"):
                slot["reached_windows"] += 1
                lk = _dec((o.get("first") or {}).get("lock_solved"))
                if lk is not None:
                    slot["locks_c"].append(lk * 100)
            slot["absorption_lots"] += _dec(o.get("absorption_lots")) or _ZERO
            slot["prints_through"] += int(o.get("prints_through", 0) or 0)
    margins = []
    for m in sorted(by):
        s = by[m]
        locks = s.pop("locks_c")
        s["mean_ideal_lock_c"] = (sum(locks, _ZERO) / Decimal(len(locks))) if locks else None
        margins.append(s)
    return {"windows_with_deep_obs": windows_with_deep, "margins": margins}


# ---------------------------------------------------------------------------
# SIDE-BY-SIDE (extended: entered-vs-not lists)
# ---------------------------------------------------------------------------
def _v32_set_lock(r: dict[str, Any]) -> Decimal | None:
    batches = r.get("wing_batch_sets")
    if isinstance(batches, list) and batches:
        total = _ZERO
        any_lock = False
        for b in batches:
            lk = _dec(b.get("realized_lock"))
            if lk is not None:
                total += lk * Decimal(int(b.get("fill_count", 1) or 1))
                any_lock = True
        return total if any_lock else None
    return _dec(r.get("realized_lock"))


def _v32_entered(r: dict[str, Any]) -> bool:
    return (_v32_set_lock(r) is not None) or int(r.get("lots_filled", 0) or 0) > 0


def _v33_entered(r: dict[str, Any]) -> bool:
    return int((r.get("ladder") or {}).get("contracts", 0) or 0) > 0


def build_side_by_side(v33_rows: list[dict[str, Any]], v32_rows: list[dict[str, Any]]
                       ) -> dict[str, Any]:
    """Per hour BOTH rosters wrote a window row: V3.2's realised (or dry) set lock vs V3.3's dry_sim /
    realised ladder lock, contracts, rungs filled, shallowest/deepest margin, with running totals; plus
    per-day totals and the windows where V3.3 (dry_sim or realised) ENTERED and V3.2 did NOT, and vice
    versa (Brad's watch-and-compare)."""
    v32_by: dict[str, dict[str, Any]] = {
        str(r.get("close_time")): r for r in v32_rows if _is_window_row(r)
    }
    rows_out: list[dict[str, Any]] = []
    day_tot: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"v32": _ZERO, "v33": _ZERO})
    tot_v32 = _ZERO
    tot_v33 = _ZERO
    v33_only: list[str] = []
    v32_only: list[str] = []
    for r in v33_rows:
        if not _is_window_row(r):
            continue
        ct = str(r.get("close_time"))
        v32 = v32_by.get(ct)
        if v32 is None:
            continue
        lad = r.get("ladder") or {}
        v33_lock = _dec(lad.get("ladder_lock")) or _ZERO
        v32_lock = _v32_set_lock(v32) or _ZERO
        tot_v33 += v33_lock
        tot_v32 += v32_lock
        day_tot[ct[:10]]["v32"] += v32_lock
        day_tot[ct[:10]]["v33"] += v33_lock
        e33 = _v33_entered(r)
        e32 = _v32_entered(v32)
        if e33 and not e32:
            v33_only.append(ct)
        if e32 and not e33:
            v32_only.append(ct)
        rows_out.append({
            "close_time": ct,
            "v32_mode": v32.get("effective_mode") or v32.get("mode"),
            "v33_mode": r.get("effective_mode") or r.get("mode"),
            "v33_dry_sim": bool(r.get("dry_sim")),
            "v32_contracts": int(v32.get("lots_filled", 0) or 0),
            "v33_contracts": int(lad.get("contracts", 0) or 0),
            "v33_rungs": int(lad.get("rungs_filled", 0) or 0),
            "v32_lock": v32_lock,
            "v33_lock": v33_lock,
            "v32_entered": e32,
            "v33_entered": e33,
            "shallowest_c": lad.get("shallowest_margin_c"),
            "deepest_c": lad.get("deepest_margin_c"),
        })
    per_day = [{"day": d, "v32_lock": day_tot[d]["v32"], "v33_lock": day_tot[d]["v33"],
                "delta": day_tot[d]["v33"] - day_tot[d]["v32"]} for d in sorted(day_tot)]
    return {
        "rows": rows_out,
        "per_day": per_day,
        "v33_entered_v32_did_not": v33_only,
        "v32_entered_v33_did_not": v32_only,
        "totals": {"windows": len(rows_out), "v32_lock": tot_v32, "v33_lock": tot_v33,
                   "delta": tot_v33 - tot_v32},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _c(v: Decimal | None) -> str:
    return f"{v * 100:+.2f}c" if v is not None else "n/a"


def _cc(v: Decimal | None) -> str:
    """A value already in cents."""
    return f"{v:+.2f}c" if v is not None else "n/a"


def _pctv(v: Decimal | None) -> str:
    return f"{v:.1f}%" if v is not None else "n/a"


def _ratio_pct(v: Decimal | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _num(v: Decimal | None) -> str:
    return f"{v:.1f}" if v is not None else "n/a"


def _render_scoreboard(sb: dict[str, Any]) -> list[str]:
    lines = ["", "LADDER SCOREBOARD (DegeneracyV3_3, rolling K-rung ladder) -- per margin, "
             "DRY separated from realised", "-" * 92]
    cap = sb["capture_10c"]
    lines.append(f"  capture ratio @ {cap['margin_c']}c = live {cap['live']} / shadow {cap['shadow']} = "
                 f"{_ratio_pct(cap['ratio'])}  (>= {V33_CAPTURE_RATIO_MIN * 100:.0f}% [pin], "
                 f"Registration-3 definition, V3.2-comparable)")

    def _section(title: str, sec: dict[str, Any]) -> None:
        lines.append("")
        lines.append(f"  [{title}]")
        margins = sec["margins"]
        if not margins:
            lines.append("    (no rung fills)")
        else:
            lines.append("    " + "margin".rjust(7) + "n".rjust(6) + "cmpl".rjust(6)
                         + "solvedE".rjust(10) + "realised".rjust(10) + "shortfall".rjust(11)
                         + "%pos".rjust(7))
            for d in margins:
                lines.append("    " + f"{d['margin_c']}c".rjust(7) + str(d["n_contracts"]).rjust(6)
                             + str(d["n_completed"]).rjust(6) + _cc(d["mean_solved_c"]).rjust(10)
                             + _cc(d["mean_realised_c"]).rjust(10) + _cc(d["shortfall_c"]).rjust(11)
                             + _pctv(d["pct_positive"]).rjust(7))
        p = sec["pooled"]
        lines.append(f"    pooled: windows={p['windows']} contracts={p['contracts']} "
                     f"ladder_lock={_cc(p['ladder_lock_c'])} "
                     f"mean_realised={_cc(p['mean_realised_lock_c'])} %pos={_pctv(p['pct_positive'])}")
        lines.append(f"            rolls={p['roll_count']} single-order={p['roll_single_order_count']} "
                     f"ratio={_ratio_pct(p['roll_single_order_ratio'])} "
                     f"(creates/win={_num(p['creates_per_window'])} "
                     f"amends/win={_num(p['amends_per_window'])} "
                     f"cancels/win={_num(p['cancels_per_window'])})")

    _section("REALISED (armed, real money)", sb["realised"])
    _section("DRY (dry_sim -- simulated ideal fills, NEVER realised)", sb["dry"])
    return lines


def _render_gate_table(gt: dict[str, Any]) -> list[str]:
    lines = ["", "FALSIFIER GATE TABLE (DegeneracyV3_3, sec 6 -- computed from REALISED rows only) "
             "-- [pin]",
             "-" * 92,
             f"  realised rung-fills n = {gt['n_rung_fills']} (verdict at n >= {gt['min_n']})"]
    lines.append("  " + "gate".ljust(34) + "value".rjust(12) + "threshold".rjust(14) + "  status")
    for g in gt["gates"]:
        v = g["value"]
        if isinstance(v, Decimal):
            vs = _cc(v) if "c" in g["threshold"] else f"{v:.2f}"
        else:
            vs = str(v)
        lines.append("  " + str(g["gate"]).ljust(34) + vs.rjust(12) + str(g["threshold"]).rjust(14)
                     + "  " + g["status"])
    lines.append(f"  VERDICT: {gt['verdict']}   (kill: mean < +{V33_KILL_MEAN_LOCK_CENTS}c at "
                 f"n >= {V33_KILL_MIN_N}, or one-legged > {V33_FALSIFIER_MAX_ONE_LEGGED})")
    return lines


def _render_deep_end(de: dict[str, Any]) -> list[str]:
    lines = ["", "DEEP END (SO-3, observation only -- 16..25c; measures the deep rungs before sizing)",
             "-" * 92,
             f"  windows with deep observation = {de['windows_with_deep_obs']}"]
    if not de["margins"]:
        lines.append("  (no deep-end observations yet)")
        return lines
    lines.append("  " + "margin".rjust(7) + "reached_win".rjust(13) + "mean_ideal_lock".rjust(17)
                 + "absorption_lots".rjust(17) + "prints".rjust(8))
    for d in de["margins"]:
        lines.append("  " + f"{d['margin_c']}c".rjust(7) + str(d["reached_windows"]).rjust(13)
                     + _cc(d["mean_ideal_lock_c"]).rjust(17)
                     + f"{d['absorption_lots']}".rjust(17) + str(d["prints_through"]).rjust(8))
    return lines


def _render(report: dict[str, Any], sxs: dict[str, Any]) -> str:
    lines: list[str] = []
    header = ["close_time".ljust(22), "mode".ljust(10), "bucket".ljust(26), "rungs".rjust(6),
              "cts".rjust(4), "shal".rjust(6), "deep".rjust(6), "ladderLk".rjust(10), "rolls".rjust(6)]
    lines.append("  ".join(header))
    lines.append("-" * len(lines[0]))
    for w in report["windows"]:
        mode = str(w["mode"]) + (" (sim)" if w["dry_sim"] else "")
        ll = _dec(w.get("ladder_lock"))
        row = [str(w["close_time"]).ljust(22), mode.ljust(10), str(w["bucket"])[:26].ljust(26),
               str(w["rungs_filled"]).rjust(6), str(w["contracts"]).rjust(4),
               (str(w.get("shallowest_c") or "-"))[:6].rjust(6),
               (str(w.get("deepest_c") or "-"))[:6].rjust(6),
               (_c(ll) if ll is not None else "-").rjust(10), str(w["roll_count"]).rjust(6)]
        line = "  ".join(row)
        if w.get("stand_down_reason"):
            line += f"   [stand down: {w['stand_down_reason']}]"
        lines.append(line)
    t = report["totals"]
    lines.append("-" * len(lines[0]))
    lines.append(
        f"windows={t['windows']}  rungs_filled={t['rungs_filled']}  contracts={t['contracts']}  "
        f"ladder_lock={_c(t['ladder_lock'])}  dry_sim_fills={t['dry_sim_fills']}  "
        f"stand_downs={t['stand_downs']}")
    rr = t.get("roll_single_order_ratio")
    lines.append(
        f"  rolls={t['roll_count']}  single-order={t['roll_single_order_count']}  "
        f"single-order-ratio={'n/a' if rr is None else f'{rr * 100:.1f}%'} "
        f"(>= 90% is the roll-integrity gate)")

    lines.extend(_render_scoreboard(report["scoreboard"]))
    lines.extend(_render_gate_table(report["gate_table"]))
    lines.extend(_render_deep_end(report["deep_end"]))

    # --- SIDE-BY-SIDE ---
    lines.append("")
    lines.append("SIDE-BY-SIDE (V3.2 realised/dry vs V3.3 dry_sim/realised ladder) -- Brad's watch")
    lines.append("-" * 92)
    lines.append("  " + "close_time".ljust(22) + "v32".rjust(8) + "v33".rjust(8)
                 + "v32_lock".rjust(12) + "v33_lock".rjust(12) + "rungs".rjust(7)
                 + "shal".rjust(6) + "deep".rjust(6))
    for e in sxs["rows"]:
        v33m = "sim" if e["v33_dry_sim"] else str(e["v33_mode"])[:6]
        lines.append(
            "  " + str(e["close_time"]).ljust(22)
            + (str(e["v32_contracts"]) + "/" + str(e["v32_mode"])[:3]).rjust(8)
            + (str(e["v33_contracts"]) + "/" + v33m[:3]).rjust(8)
            + _c(e["v32_lock"]).rjust(12) + _c(e["v33_lock"]).rjust(12)
            + str(e["v33_rungs"]).rjust(7)
            + (str(e.get("shallowest_c") or "-"))[:6].rjust(6)
            + (str(e.get("deepest_c") or "-"))[:6].rjust(6))
    st = sxs["totals"]
    lines.append("-" * 92)
    lines.append(
        f"  matched windows = {st['windows']}   V3.2 total lock = {_c(st['v32_lock'])}   "
        f"V3.3 total lock = {_c(st['v33_lock'])}   delta(v33-v32) = {_c(st['delta'])}")
    lines.append(f"  V3.3 entered / V3.2 did NOT: {len(sxs['v33_entered_v32_did_not'])} "
                 f"{sxs['v33_entered_v32_did_not']}")
    lines.append(f"  V3.2 entered / V3.3 did NOT: {len(sxs['v32_entered_v33_did_not'])} "
                 f"{sxs['v32_entered_v33_did_not']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V3.3 per-window ladder report + scoreboard + gate table "
                                             "+ deep-end + side-by-side vs V3.2.")
    ap.add_argument("--days", type=int, default=None, help="Only the most recent N UTC days.")
    ap.add_argument("--ledger", default=DEFAULT_V33_LEDGER_PATH)
    ap.add_argument("--v32-ledger", default=DEFAULT_V32_LEDGER_PATH)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    v33_rows = _recent_days(load_v33_rows(args.ledger), args.days)
    v32_rows = _recent_days(load_v32_rows(args.v32_ledger), args.days)
    report = build_v33_report(v33_rows)
    sxs = build_side_by_side(v33_rows, v32_rows)
    if args.json:
        print(json.dumps({"report": report, "side_by_side": sxs}, sort_keys=True,
                         default=lambda o: str(o)))
    else:
        print(_render(report, sxs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
