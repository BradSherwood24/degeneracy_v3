"""report.py — a read-only per-window report over the V3.3 ledger, with a SIDE-BY-SIDE block vs V3.2.

``python -m service.v33.report [--days N]`` prints one LADDER row per window (mode/dry_sim, spot bucket,
rungs filled, shallowest/deepest margin, contracts, ladder lock, roll integrity), a totals block, and —
Brad's "watch and compare" (2026-09-22) — a SIDE-BY-SIDE block: for each hour BOTH rosters wrote a row,
V3.2's realised (or dry) set lock vs V3.3's dry_sim / realised ladder lock, with running totals.

Reads ONLY ``ledger/v33_ledger.jsonl`` (and, for the side-by-side, ``ledger/v32_ledger.jsonl``) plus
nothing else — no network, no sealed file, no orders. L3 extends this with the LADDER SCOREBOARD /
per-rung falsifier; L2 ships the minimum the comparison needs.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from service.v33.ledger import DEFAULT_V33_LEDGER_PATH, load_v33_rows
from service.v32.ledger import DEFAULT_V32_LEDGER_PATH, load_v32_rows

_ZERO = Decimal(0)


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


def build_v33_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the V3.3 window rows into per-window LADDER lines + totals (pure; the CLI renders it)."""
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


def _v32_set_lock(r: dict[str, Any]) -> Decimal | None:
    """The V3.2 row's total realised set lock in dollars (Σ per-batch realized_lock x fill_count), or
    the single-set ``realized_lock`` for an older/legacy row. None when nothing filled."""
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


def build_side_by_side(v33_rows: list[dict[str, Any]], v32_rows: list[dict[str, Any]]
                       ) -> dict[str, Any]:
    """For each hour BOTH rosters wrote a window row, compare V3.2's realised (or dry) set lock vs
    V3.3's dry_sim / realised ladder lock, contracts, rungs filled, shallowest/deepest margin, with
    running totals. The comparison Brad asked for — watch the ladder track the live V3.2."""
    v32_by: dict[str, dict[str, Any]] = {
        str(r.get("close_time")): r for r in v32_rows if _is_window_row(r)
    }
    rows_out: list[dict[str, Any]] = []
    tot_v32 = _ZERO
    tot_v33 = _ZERO
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
            "shallowest_c": lad.get("shallowest_margin_c"),
            "deepest_c": lad.get("deepest_margin_c"),
        })
    return {
        "rows": rows_out,
        "totals": {"windows": len(rows_out), "v32_lock": tot_v32, "v33_lock": tot_v33,
                   "delta": tot_v33 - tot_v32},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _c(v: Decimal | None) -> str:
    return f"{v * 100:+.2f}c" if v is not None else "n/a"


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
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V3.3 per-window ladder report + side-by-side vs V3.2.")
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
