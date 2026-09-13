"""report.py — a read-only per-window report over the V3.2 ledger + journals.

``python -m service.v32.report [--days N]`` prints one row per window: mode, spot bucket, replaces,
shadow fills by E with locks, data-age (per-connection lag) stats, and stand-downs, then a totals
block. Reads ONLY ``ledger/v32_ledger.jsonl`` (and, when present, the summaries) — no network, no
sealed file, no orders. Once Phase 3 books real fills the same table gains the live columns from the
row's ``fills``/``settlement`` slots; Phase 2 shows the shadow (the ideal fill rule running live) and
the would-be order counts.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from service.v32.ledger import DEFAULT_V32_LEDGER_PATH, load_v32_rows


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _recent_days(rows: list[dict[str, Any]], days: int | None) -> list[dict[str, Any]]:
    """Rows on the most recent ``days`` UTC calendar days (by close_time). None -> all rows."""
    if days is None:
        return rows
    ds = sorted({str(r.get("close_time", ""))[:10] for r in rows if r.get("close_time")}, reverse=True)
    keep = set(ds[:days])
    return [r for r in rows if str(r.get("close_time", ""))[:10] in keep]


def _fmt(v: Any, width: int) -> str:
    return str("" if v is None else v).rjust(width)[:max(width, 3)] if isinstance(v, (int, float)) \
        else str("" if v is None else v).ljust(width)


def _shadow_cell(shadow: dict[str, Any], e_key: str) -> str:
    """A compact 'n@lock' cell for one E: e.g. '0.45/+10.4c', or '-' when no shadow fill."""
    sub = shadow.get(e_key)
    if not sub or not sub.get("filled"):
        return "-"
    n = _dec(sub.get("n"))
    lock = _dec(sub.get("lock"))
    n_s = f"{n:.2f}" if n is not None else "?"
    if lock is None:
        return f"{n_s}/pending"
    return f"{n_s}/{lock * 100:+.1f}c"


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the rows into per-window lines + totals (pure; the CLI renders it)."""
    # the union of shadow Es seen (sorted), for stable columns
    e_keys: list[str] = sorted({k for r in rows for k in (r.get("shadow") or {}).keys()})
    windows: list[dict[str, Any]] = []
    tot_would_places = 0
    tot_replaces = 0
    tot_stand_downs = 0
    tot_late_fills = 0
    shadow_fills: dict[str, int] = {k: 0 for k in e_keys}
    shadow_locks: dict[str, list[Decimal]] = {k: [] for k in e_keys}
    lags: list[Decimal] = []
    for r in rows:
        shadow = r.get("shadow") or {}
        for k in e_keys:
            sub = shadow.get(k)
            if sub and sub.get("filled"):
                shadow_fills[k] += 1
                lk = _dec(sub.get("lock"))
                if lk is not None:
                    shadow_locks[k].append(lk)
        for lag_field in ("strike_lag_seconds", "bucket_lag_seconds"):
            lg = _dec(r.get(lag_field))
            if lg is not None:
                lags.append(lg)
        tot_would_places += int(r.get("would_places", 0) or 0)
        tot_replaces += int(r.get("replaces", 0) or 0)
        tot_late_fills += int(r.get("late_fills", 0) or 0)
        if r.get("stand_down"):
            tot_stand_downs += 1
        windows.append(
            {
                "close_time": r.get("close_time"),
                "mode": r.get("effective_mode") or r.get("mode"),
                "bucket": r.get("spot_bucket_ticker") or ("STAND DOWN" if r.get("stand_down") else "-"),
                "replaces": r.get("replaces", 0),
                "would_places": r.get("would_places", 0),
                "late_fills": r.get("late_fills", 0),
                "shadow": {k: _shadow_cell(shadow, k) for k in e_keys},
                "strike_lag": r.get("strike_lag_seconds"),
                "bucket_lag": r.get("bucket_lag_seconds"),
                "stand_down_reason": r.get("stand_down_reason"),
            }
        )
    mean_locks = {
        k: (sum(v, Decimal(0)) / Decimal(len(v))) if v else None for k, v in shadow_locks.items()
    }
    mean_lag = (sum(lags, Decimal(0)) / Decimal(len(lags))) if lags else None
    return {
        "e_keys": e_keys,
        "windows": windows,
        "totals": {
            "windows": len(rows),
            "would_places": tot_would_places,
            "replaces": tot_replaces,
            "stand_downs": tot_stand_downs,
            "late_fills": tot_late_fills,
            "shadow_fills": shadow_fills,
            "shadow_mean_lock": mean_locks,
            "mean_lag_seconds": mean_lag,
        },
    }


def _render(report: dict[str, Any]) -> str:
    e_keys = report["e_keys"]
    lines: list[str] = []
    header = ["close_time".ljust(22), "mode".ljust(9), "bucket".ljust(26),
              "repl".rjust(5), "wPlc".rjust(5)]
    for k in e_keys:
        header.append(("sh_E" + k).rjust(14))
    header += ["sLag".rjust(6), "bLag".rjust(6)]
    lines.append("  ".join(header))
    lines.append("-" * (len(lines[0])))
    for w in report["windows"]:
        row = [str(w["close_time"]).ljust(22), str(w["mode"]).ljust(9),
               str(w["bucket"]).ljust(26), str(w["replaces"]).rjust(5),
               str(w["would_places"]).rjust(5)]
        for k in e_keys:
            row.append(str(w["shadow"].get(k, "-")).rjust(14))
        sl = w.get("strike_lag")
        bl = w.get("bucket_lag")
        row.append((f"{float(sl):.1f}" if sl is not None else "-").rjust(6))
        row.append((f"{float(bl):.1f}" if bl is not None else "-").rjust(6))
        line = "  ".join(row)
        if w.get("stand_down_reason"):
            line += f"   [stand down: {w['stand_down_reason']}]"
        lines.append(line)
    t = report["totals"]
    lines.append("-" * (len(lines[0]) if lines else 40))
    lines.append(
        f"windows={t['windows']}  would_places={t['would_places']}  replaces={t['replaces']}  "
        f"stand_downs={t['stand_downs']}  late_fills={t['late_fills']}"
    )
    for k in e_keys:
        ml = t["shadow_mean_lock"].get(k)
        ml_s = f"{ml * 100:+.2f}c" if ml is not None else "n/a"
        lines.append(f"  shadow E={k}: fills={t['shadow_fills'].get(k, 0)}  mean_lock={ml_s}")
    mlag = t["mean_lag_seconds"]
    lines.append(f"  mean data-age (lag) = {float(mlag):.2f}s" if mlag is not None
                 else "  mean data-age (lag) = n/a")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V3.2 per-window report (read-only).")
    ap.add_argument("--days", type=int, default=None,
                    help="Only the most recent N UTC days (default: all).")
    ap.add_argument("--ledger", default=DEFAULT_V32_LEDGER_PATH)
    ap.add_argument("--json", action="store_true", help="Emit the report as JSON instead of a table.")
    args = ap.parse_args(argv)

    rows = _recent_days(load_v32_rows(args.ledger), args.days)
    report = build_report(rows)
    if args.json:
        print(json.dumps(report, sort_keys=True, default=lambda o: str(o)))
    else:
        print(_render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
