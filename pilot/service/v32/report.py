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

from service.v32.falsifier_pins import (
    V32_FALSIFIER_MAX_EXEC_GAP_CENTS,
    V32_FALSIFIER_MAX_ONE_LEGGED,
    V32_FALSIFIER_MIN_FILL_RATE_PER_DAY,
    V32_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V32_FALSIFIER_MIN_N,
    V32_FALSIFIER_MIN_PCT_POSITIVE,
    V32_FALSIFIER_SHADOW_GAP_E,
)
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
    tot_invariant_violations = 0
    tot_invariant_phantoms = 0
    tot_invariant_rechecks = 0
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
        tot_invariant_violations += int(r.get("rest_invariant_violations", 0) or 0)
        tot_invariant_phantoms += int(r.get("rest_invariant_phantoms", 0) or 0)
        tot_invariant_rechecks += int(r.get("rest_invariant_rechecks", 0) or 0)
        if r.get("stand_down"):
            tot_stand_downs += 1
        windows.append(
            {
                "close_time": r.get("close_time"),
                "mode": r.get("effective_mode") or r.get("mode"),
                "bucket": r.get("spot_bucket_ticker") or ("STAND DOWN" if r.get("stand_down") else "-"),
                "Sd": r.get("Sd"),
                "last_rest": r.get("last_rest_price"),
                "replaces": r.get("replaces", 0),
                "would_places": r.get("would_places", 0),
                "late_fills": r.get("late_fills", 0),
                "shadow": {k: _shadow_cell(shadow, k) for k in e_keys},
                "m15_frames": int(r.get("m15_frames", 0) or 0),
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
        "falsifier": build_falsifier_scoreboard(rows),
        "totals": {
            "windows": len(rows),
            "would_places": tot_would_places,
            "replaces": tot_replaces,
            "stand_downs": tot_stand_downs,
            "late_fills": tot_late_fills,
            "rest_invariant_violations": tot_invariant_violations,
            "rest_invariant_phantoms": tot_invariant_phantoms,
            "rest_invariant_rechecks": tot_invariant_rechecks,
            "shadow_fills": shadow_fills,
            "shadow_mean_lock": mean_locks,
            "mean_lag_seconds": mean_lag,
        },
    }


def _percentile(sorted_vals: list[Decimal], pct: Decimal) -> Decimal | None:
    """Nearest-rank percentile of an already-sorted list (pct in [0,100]). None on empty."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    # nearest-rank: rank = ceil(pct/100 * n), clamped to [1, n]
    import math
    rank = int(math.ceil(float(pct) / 100.0 * n))
    rank = max(1, min(n, rank))
    return sorted_vals[rank - 1]


def _lag_stats_p99(rows: list[dict[str, Any]], conn: str) -> Decimal | None:
    """The MAX over windows of one connection's per-window ``lag_stats[conn]['p99']`` (the honest
    worst-tail data-age across the reported windows). None when NO row carries a lag_stats p99 for
    ``conn`` (legacy rows predating the field, or a run that never sampled)."""
    vals: list[Decimal] = []
    for r in rows:
        sub = (r.get("lag_stats") or {}).get(conn)
        if isinstance(sub, dict):
            v = _dec(sub.get("p99"))
            if v is not None:
                vals.append(v)
    return max(vals) if vals else None


def build_falsifier_scoreboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The pre-registered falsifier scoreboard, computed from the SAME [pin] constants the document
    commits to (``service.v32.falsifier_pins``). Pure; the CLI renders it.

    A *completed set* is an armed window whose ``realized_lock`` is present (both wings taken). A
    *one-legged set* is an armed window flagged ``one_legged``. The verdict (``ALIVE-so-far`` / ``KILL``
    / ``n<MIN_N pending``) is decided ONLY once ``n`` completed sets exist and applies every pinned
    threshold; any miss at n >= MIN_N is a KILL (no re-spec on the same window)."""
    e = V32_FALSIFIER_SHADOW_GAP_E
    completed: list[dict[str, Any]] = []
    legged = 0
    fill_days: set[str] = set()
    armed_days: set[str] = set()
    live_locks_c: list[Decimal] = []          # realized lock in cents (completed sets)
    shadow_locks_c: list[Decimal] = []        # shadow E=0.10 lock in cents (any window it filled)
    gaps_c: list[Decimal] = []                # shadow E=0.10 - live lock in cents (both present)
    replaces: list[Decimal] = []
    strike_lags: list[Decimal] = []
    bucket_lags: list[Decimal] = []
    for r in rows:
        if not r.get("armed"):
            continue
        day = str(r.get("close_time", ""))[:10]
        armed_days.add(day)
        rlock = _dec(r.get("realized_lock"))
        is_fill = bool(r.get("realized_unsettled")) or rlock is not None or bool(r.get("one_legged"))
        if is_fill:
            fill_days.add(day)
        if r.get("one_legged"):
            legged += 1
        rep = _dec(r.get("replaces"))
        if rep is not None:
            replaces.append(rep)
        for bucket, lag_field in ((strike_lags, "strike_lag_seconds"),
                                  (bucket_lags, "bucket_lag_seconds")):
            lg = _dec(r.get(lag_field))
            if lg is not None:
                bucket.append(lg)
        shadow = r.get("shadow") or {}
        sub = shadow.get(e)
        slock = _dec(sub.get("lock")) if (sub and sub.get("filled")) else None
        if slock is not None:
            shadow_locks_c.append(slock * 100)
        if rlock is not None:
            completed.append(r)
            live_locks_c.append(rlock * 100)
            if slock is not None:
                gaps_c.append(slock * 100 - rlock * 100)

    n = len(completed)
    n_days = len(armed_days)  # distinct armed UTC calendar days (retained key; NOT the fill-rate denom)
    # armed_windows = number of armed windows the pilot actually RAN (ledger rows whose effective_mode
    # is armed, whatever their stand-down reason). A dark hour (reboot/proxy-down/task-not-started)
    # leaves no row and so contributes 0. Backfill rows carry no effective_mode, so they never count
    # here (they would double-count a window otherwise). Registered clarification 2026-09-15 ~13:30Z.
    armed_windows = sum(1 for r in rows if r.get("effective_mode") == "armed")
    # "armed evaluation days" = armed_windows / 24 (an hour is a 24th of a day). fill_rate is the
    # sets-per-day the >= 2.0 [pin] gate reads; None only when NO armed window ran.
    armed_days = Decimal(armed_windows) / Decimal(24)
    # fills_total = number of armed windows with a rest fill (completed + one-legged)
    fills_total = sum(
        1 for r in rows if r.get("armed") and (
            bool(r.get("realized_unsettled")) or _dec(r.get("realized_lock")) is not None
            or bool(r.get("one_legged")))
    )
    slocks = sorted(live_locks_c)
    mean_lock = (sum(live_locks_c, Decimal(0)) / Decimal(n)) if n else None
    median_lock = _percentile(slocks, Decimal(50)) if n else None
    p10_lock = _percentile(slocks, Decimal(10)) if n else None
    min_lock = slocks[0] if n else None
    pos = sum(1 for x in live_locks_c if x > 0)
    pct_positive = (Decimal(pos) * 100 / Decimal(n)) if n else None
    fill_rate = (Decimal(fills_total) / armed_days) if armed_windows else None
    shadow_mean = ((sum(shadow_locks_c, Decimal(0)) / Decimal(len(shadow_locks_c)))
                   if shadow_locks_c else None)
    exec_gap = (sum(gaps_c, Decimal(0)) / Decimal(len(gaps_c))) if gaps_c else None
    replaces_mean = (sum(replaces, Decimal(0)) / Decimal(len(replaces))) if replaces else None
    strike_p99 = _percentile(sorted(strike_lags), Decimal(99)) if strike_lags else None
    bucket_p99 = _percentile(sorted(bucket_lags), Decimal(99)) if bucket_lags else None
    # data-age p99 from the per-window lag_stats (max across windows) — read over ALL rows (not just
    # armed), since it is an operational health gauge; the scoreboard prefers these and falls back to
    # the legacy per-row *_lag_seconds p99 (armed rows) only when no row carries lag_stats.
    strike_stats_p99 = _lag_stats_p99(rows, "strikes")
    bucket_stats_p99 = _lag_stats_p99(rows, "buckets")

    # --- verdict from the [pin] constants -------------------------------------------------------
    if n < V32_FALSIFIER_MIN_N:
        verdict = f"n<{V32_FALSIFIER_MIN_N} pending (n={n})"
    else:
        fails: list[str] = []
        if mean_lock is None or mean_lock < V32_FALSIFIER_MIN_MEAN_LOCK_CENTS:
            fails.append(f"mean lock {mean_lock}c < +{V32_FALSIFIER_MIN_MEAN_LOCK_CENTS}c")
        if pct_positive is None or pct_positive < V32_FALSIFIER_MIN_PCT_POSITIVE:
            fails.append(f"%positive {pct_positive} < {V32_FALSIFIER_MIN_PCT_POSITIVE}")
        if fill_rate is None or fill_rate < V32_FALSIFIER_MIN_FILL_RATE_PER_DAY:
            fails.append(f"fill rate {fill_rate}/day < {V32_FALSIFIER_MIN_FILL_RATE_PER_DAY}")
        if exec_gap is None or exec_gap > V32_FALSIFIER_MAX_EXEC_GAP_CENTS:
            fails.append(f"exec gap {exec_gap}c > {V32_FALSIFIER_MAX_EXEC_GAP_CENTS}c")
        if legged > V32_FALSIFIER_MAX_ONE_LEGGED:
            fails.append(f"one-legged {legged} > {V32_FALSIFIER_MAX_ONE_LEGGED}")
        verdict = "ALIVE-so-far" if not fails else ("KILL: " + "; ".join(fails))

    return {
        "shadow_gap_E": e,
        "n": n,
        "fills_total": fills_total,
        "one_legged": legged,
        "n_days": n_days,
        "armed_windows": armed_windows,
        "armed_days": armed_days,
        "mean_lock_c": mean_lock,
        "median_lock_c": median_lock,
        "p10_lock_c": p10_lock,
        "min_lock_c": min_lock,
        "pct_positive": pct_positive,
        "fill_rate_per_day": fill_rate,
        "shadow_mean_lock_c": shadow_mean,
        "exec_gap_c": exec_gap,
        "replaces_per_hour_mean": replaces_mean,
        "strike_lag_p99_s": strike_p99,
        "bucket_lag_p99_s": bucket_p99,
        "strike_lag_stats_p99_s": strike_stats_p99,
        "bucket_lag_stats_p99_s": bucket_stats_p99,
        "verdict": verdict,
    }


def _c(v: Decimal | None, prec: int = 1) -> str:
    return f"{v:+.{prec}f}c" if v is not None else "n/a"


def _num(v: Decimal | None, prec: int, suffix: str = "") -> str:
    return f"{v:.{prec}f}{suffix}" if v is not None else "n/a"


def _render_scoreboard(sb: dict[str, Any]) -> list[str]:
    e = sb["shadow_gap_E"]
    pct = sb["pct_positive"]
    rate = sb["fill_rate_per_day"]
    # data-age p99 = MAX of the per-window lag_stats p99 across windows (the honest worst tail);
    # falls back to the legacy per-row mean-lag p99 only for rows predating the lag_stats field.
    strike_age = sb.get("strike_lag_stats_p99_s")
    if strike_age is None:
        strike_age = sb.get("strike_lag_p99_s")
    bucket_age = sb.get("bucket_lag_stats_p99_s")
    if bucket_age is None:
        bucket_age = sb.get("bucket_lag_p99_s")
    return [
        "",
        "FALSIFIER SCOREBOARD (DegeneracyV3_2, continuous-requote pump-fader, E=0.10) -- [pin] gates",
        "-" * 78,
        f"  completed sets n = {sb['n']}   (rest fills total = {sb['fills_total']}, one-legged = "
        f"{sb['one_legged']})   armed windows = {sb['armed_windows']}   "
        f"armed days = {sb['armed_windows']}/24 = {_num(sb['armed_days'], 2)}",
        f"  realized lock: mean {_c(sb['mean_lock_c'])}  median {_c(sb['median_lock_c'])}  "
        f"p10 {_c(sb['p10_lock_c'])}  min {_c(sb['min_lock_c'])}",
        f"  %positive = {_num(pct, 1) if pct is not None else 'n/a'}   "
        f"fill rate = {_num(rate, 2, '/day') if rate is not None else 'n/a'}",
        f"  shadow E={e}: mean lock {_c(sb['shadow_mean_lock_c'])}   "
        f"execution gap (shadow-live) {_c(sb['exec_gap_c'])}",
        f"  replaces/hour mean = {_num(sb['replaces_per_hour_mean'], 1)}   "
        f"data-age p99 (max/window): strike {_num(strike_age, 2, 's')}  "
        f"bucket {_num(bucket_age, 2, 's')}",
        f"  VERDICT: {sb['verdict']}",
    ]


def _render(report: dict[str, Any]) -> str:
    e_keys = report["e_keys"]
    lines: list[str] = []
    header = ["close_time".ljust(22), "mode".ljust(9), "bucket".ljust(26),
              "Sd".rjust(7), "lastRest".rjust(8), "repl".rjust(5), "wPlc".rjust(5)]
    for k in e_keys:
        header.append(("sh_E" + k).rjust(14))
    header += ["m15".rjust(6), "sLag".rjust(6), "bLag".rjust(6)]
    lines.append("  ".join(header))
    lines.append("-" * (len(lines[0])))
    for w in report["windows"]:
        lr = w.get("last_rest")
        lr_s = (f"{float(lr):.2f}" if lr not in (None, "") else "-")
        row = [str(w["close_time"]).ljust(22), str(w["mode"]).ljust(9),
               str(w["bucket"]).ljust(26),
               (str(w.get("Sd")) if w.get("Sd") is not None else "-").rjust(7),
               lr_s.rjust(8),
               str(w["replaces"]).rjust(5),
               str(w["would_places"]).rjust(5)]
        for k in e_keys:
            row.append(str(w["shadow"].get(k, "-")).rjust(14))
        row.append(str(w.get("m15_frames", 0)).rjust(6))
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
    lines.append(
        f"  rest_invariant: violations={t.get('rest_invariant_violations', 0)}  "
        f"phantoms={t.get('rest_invariant_phantoms', 0)}  "
        f"rechecks={t.get('rest_invariant_rechecks', 0)} (read-path lag, no stand-down)"
    )
    for k in e_keys:
        ml = t["shadow_mean_lock"].get(k)
        ml_s = f"{ml * 100:+.2f}c" if ml is not None else "n/a"
        lines.append(f"  shadow E={k}: fills={t['shadow_fills'].get(k, 0)}  mean_lock={ml_s}")
    mlag = t["mean_lag_seconds"]
    lines.append(f"  mean data-age (lag) = {float(mlag):.2f}s" if mlag is not None
                 else "  mean data-age (lag) = n/a")
    if report.get("falsifier") is not None:
        lines.extend(_render_scoreboard(report["falsifier"]))
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
