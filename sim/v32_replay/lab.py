"""lab.py — the V3.2 replay-lab CLI.

    python -m sim.v32_replay.lab --journals <dir> [--since YYYY-MM-DD] [--out <dir>]

Default journals dir = ``pilot/journals_v32`` (relative to the repo root), default out =
``sim/out/v32_replay/``. Reads only ``*.jsonl.gz`` (never the live ``.jsonl``), refuses sealed dates,
runs the replay engine on each window, writes ``report_<YYYYMMDD>.md`` + ``per_window.jsonl`` +
``fills.jsonl`` and prints the report to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal

from . import REPO_ROOT
from .calibration import aggregate as aggregate_calibration
from .estimates import build_estimates
from .frames import discover_journals, iter_frames, read_window_meta
from .models import BASE_CELL
from .replay import WindowEngine, WindowResult

DEFAULT_JOURNALS = os.path.join(REPO_ROOT, "pilot", "journals_v32")
DEFAULT_OUT = os.path.join(REPO_ROOT, "sim", "out", "v32_replay")


def _load_params():
    from service.v32.params import load_v32_params
    return load_v32_params()


def run_window(path: str, params) -> WindowResult:
    meta = read_window_meta(path)
    width = getattr(params, "bucket_width", 100)
    engine = WindowEngine(meta, params, width=width)
    return engine.run(iter_frames(path))


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _fmt(x, nd=2, suffix=""):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}{suffix}"
    return f"{x}{suffix}"


def _fmt_c(x):
    return "n/a" if x is None else f"{x:+.1f}c"


def render_report(results: list[WindowResult], est: dict, run_date: str) -> str:
    L: list[str] = []
    L.append(f"# V3.2 Replay Lab — report {run_date}")
    L.append("")
    L.append(f"Windows replayed: **{len(results)}**  "
             f"(base cell E={BASE_CELL[0]}, TOL={BASE_CELL[1]}, DEB={BASE_CELL[2]} ms, LAT=200 ms)")
    L.append("")
    L.append("Money math is the pinned law (`service.v32.core.solve_n` / `wing_cost` / `lock_value`, "
             "`_simlaw.fee`). Fills come from the reconstructed ms feed only; decision records are "
             "ignored except `window_meta`. No freshness gate (the sim's rule set), reported as metrics.")
    L.append("")

    # --- per-window book metrics ---
    L.append("## Per-window book metrics (quoting window T-15..T-5)")
    L.append("")
    L.append("| close | mode | frames | 15M share | strike share | bucket share | "
             "bkt gap ms | spot gap ms | strike gap ms | spot spread c | spot depth | "
             "bkt trades | spot yes trades |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        m = r.metrics
        L.append(
            f"| {r.close_time[11:19]} | {r.effective_mode}"
            f"{'/ARMED' if r.armed else ''} | {r.total_frames:,} | "
            f"{_fmt(m['m15_frame_share']*100,1,'%')} | {_fmt(m['strike_frame_share']*100,1,'%')} | "
            f"{_fmt(m['bucket_frame_share']*100,1,'%')} | "
            f"{_fmt(m['bucket_tick_gap_ms_median_overall'],0)} | "
            f"{_fmt(m['spot_tick_gap_ms_median'],0)} | "
            f"{_fmt(m['strike_tick_gap_ms_median'],0)} | "
            f"{_fmt(m['spot_spread_cents_median'],1)} | "
            f"{_fmt(m['spot_top_depth_median'],0)} | "
            f"{m['n_bucket_trades']} | {m['n_spot_bucket_yes_trades']} |"
        )
    L.append("")
    L.append("Spot bucket (modal Sd) per window: "
             + ", ".join(f"{r.close_time[11:16]}={r.metrics['modal_spot_Sd']}" for r in results))
    L.append("")

    # --- sim vs ms comparison ---
    L.append("## SIM-vs-MS comparison (minute-candle OLD vs ms-continuous MS, base cell)")
    L.append("")
    L.append("| close | spot differ % | cap absdiff med c | cap absdiff p90 c | "
             "MS base fill | OLD base fill | base lock c | old lock c | lock diff c | "
             "B resid med c | B resid p90 c |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        c = r.comparison
        L.append(
            f"| {r.close_time[11:19]} | "
            f"{_fmt((c['spot_differ_frac'] or 0)*100,1)} | "
            f"{_fmt(c['cap_diff_cents_median'],2)} | {_fmt(c['cap_diff_cents_p90'],2)} | "
            f"{'yes' if c['base_fill'] else '-'} | {'yes' if c['old_fill'] else '-'} | "
            f"{_fmt(c['base_lock_cents'],1)} | {_fmt(c['old_lock_cents'],1)} | "
            f"{_fmt(c['lock_diff_cents'],1)} | "
            f"{_fmt(c['wing_drift_cents_median'],2)} | {_fmt(c['wing_drift_cents_p90'],2)} |"
        )
    L.append("")

    # --- fills ---
    all_fills = _collect_fills(results)
    L.append(f"## Fills found: {len(all_fills)} (all models, all grid cells)")
    L.append("")
    if all_fills:
        L.append("| close | model | E | TOL | DEB | spot Sd | n | offer | print | size | "
                 "regime | W compl | lock c |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for row in all_fills:
            L.append(
                f"| {row['close'][11:19]} | {row['model']} | {row['E']} | {_fmt(row['tol'])} | "
                f"{_fmt(row['deb'])} | {row['spot_Sd']} | {row['n']} | {row['offer']} | "
                f"{row['print_price']} | {row['print_size']} | {row['regime']} | "
                f"{_fmt(row['W_completion'])} | {_fmt_c(row['lock_c'])} |"
            )
        L.append("")
    else:
        L.append("_No fills across any model/cell in these windows (bucket-trade flow is sparse; "
                 "the base fill rate is ~3.6/day)._")
        L.append("")

    # maker-rule regime note (replaces the retired book-swept diagnostic)
    L.append("_Fill rule = spread-aware maker rule: regime (i) o<a fills iff print >= offer (we are the "
             "best ask by price priority); (ii) o==a fills iff print > offer; (iii) o>a is a no-quote "
             "(our offer would be above the market ask). 'swept' is retired._")
    L.append("")

    # --- estimates ---
    L.append("## Estimates (optimistic / base / pessimistic)")
    L.append("")
    L.append("| estimate | n fills | windows | fills/day | mean c | median c | p10 c | min c | "
             "% pos | c/day | sd c | windows for +/-2c |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key in ("optimistic", "base", "pessimistic"):
        e = est[key]
        L.append(
            f"| {e['name']} | {e['n_fills']} | {e['n_windows']} | {_fmt(e['fills_per_day'],2)} | "
            f"{_fmt_c(e['mean_c'])} | {_fmt_c(e['median_c'])} | {_fmt_c(e['p10_c'])} | "
            f"{_fmt_c(e['min_c'])} | {_fmt((e['pct_positive'] or 0)*100,0,'%') if e['pct_positive'] is not None else 'n/a'} | "
            f"{_fmt(e['c_per_day'],1)} | {_fmt(e['sd_c'],2)} | {_fmt(e['windows_for_2c_band'],0)} |"
        )
    L.append("")
    for key in ("optimistic", "base", "pessimistic"):
        e = est[key]
        L.append(f"- **{e['name']}** windows-for-band basis: {e['windows_note']}")
    L.append(f"- PESSIMISTIC dropped {est['pessimistic_dropped_small_prints']} sub-1-lot fills and "
             f"{est.get('pessimistic_dropped_fresh_lat', 0)} fills placed within LAT=200 ms of the "
             f"print; budget haircut hit {est['pessimistic_budget_haircut_windows']} windows "
             f"(per-window replace budget ~{est['per_window_replace_budget']:.0f}).")
    L.append(f"- At the assumed 3.6 fills/day the base rate is ~1 fill per 6.7 windows; "
             f"{len(results)} windows collected so far — too few for a stable mean (report firms up as "
             f"journals accumulate).")
    L.append("")
    L.append("_Note: the maker (resting) leg fee is charged in `lock_value` for parity with the pinned "
             "sim, but Kalshi crypto maker fee is 0 — so the realized lock is ~fee(n) (~1.7c) HIGHER "
             "than reported (conservative)._")
    L.append("")
    return "\n".join(L)


def render_calibration(calib: dict) -> str:
    L: list[str] = []
    L.append("## Calibration (ms bucket books vs the sim's candle-side assumptions)")
    L.append("")
    L.append(f"Calibration windows: **{calib['n_windows']}**; spot-bucket YES trades measured: "
             f"**{calib['n_trades']}**; evaluated (n_shadow defined): **{calib['n_eval']}**.")
    L.append("")
    ce = calib["cap_error_c"]; ps = calib["print_size"]; B = calib["B_resid_c"]
    rf = calib["regime_frac"]
    L.append("| quantity | value |")
    L.append("|---|---|")
    L.append(f"| (a) spot-bucket agreement (candle vs ms) | {_fmt((calib['spot_agreement'] or 0)*100,1,'%') if calib['spot_agreement'] is not None else 'n/a'} |")
    L.append(f"| (b) cap error mean / p10 / p90 (c) | {_fmt(ce['mean'],2)} / {_fmt(ce['p10'],2)} / {_fmt(ce['p90'],2)}  (n={ce['n']}) |")
    L.append(f"| (c) maker-rule regimes i / ii / iii | {_fmt((rf['i'] or 0)*100,1,'%')} / {_fmt((rf['ii'] or 0)*100,1,'%')} / {_fmt((rf['iii'] or 0)*100,1,'%')}  (n={calib['n_eval']}) |")
    L.append(f"| (c) P(fill) maker vs strict rule | {_fmt((calib['p_fill_maker'] or 0)*100,1,'%') if calib['p_fill_maker'] is not None else 'n/a'} vs {_fmt((calib['p_fill_strict'] or 0)*100,1,'%') if calib['p_fill_strict'] is not None else 'n/a'}  (maker {calib['n_maker_fills']} / strict {calib['n_strict_fills']}); fill factor {_fmt(calib['fill_factor'],3)} (p10 {_fmt(calib['fill_factor_p10'],3)}) |")
    L.append(f"| (c) print size median / p10 (lots) | {_fmt(ps['median'],1)} / {_fmt(ps['p10'],1)}  (n={ps['n']}) |")
    L.append(f"| (d) wing residual B = W(+1.5s)-W(trade) mean / p90 (c) | {_fmt(B['mean'],2)} / {_fmt(B['p90'],2)}  (n={B['n']}) |")
    L.append(f"| (e) replaces/window (ms) vs sim's ~77 | {_fmt(calib['replaces_per_window_mean'],0)} vs 77 |")
    L.append("")
    return "\n".join(L)


def render_forward(fwd: dict) -> str:
    L: list[str] = []
    L.append("## APPLY — corrected sim over the forward 139 h (2026-08-30..09-04)")
    L.append("")
    L.append(f"Forward hours: **{fwd['n_hours']}** over {fwd['n_days']} days. "
             f"Corrections from the calibration above: cap shift base {_fmt(fwd['cap_shift_base_c'],2)}c / "
             f"pess {_fmt(fwd['cap_shift_pess_c'],2)}c; maker-rule fill factor base "
             f"x{_fmt(fwd['fill_factor_base'],3)} / pess x{_fmt(fwd['fill_factor_pess'],3)} "
             f"(maker fills / strict fills, measured on the ms journals; the forward set has no ms bucket "
             f"book so the rule is carried as this factor, not re-evaluated on the stale candle ask); "
             f"sim tape B {_fmt(fwd['sim_tape_B_c'],2)}c, lock B-correction base "
             f"{_fmt(fwd['base_B_corr_c'],2)}c / pess {_fmt(fwd['pess_B_corr_c'],2)}c (+2c wing haircut pess).")
    L.append("")
    L.append("| E | estimate | n fills | fills/day | mean c | median c | p10 c | min c | % pos | c/day |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for E, cell in fwd["by_E"].items():
        for key in ("optimistic", "base", "pessimistic"):
            e = cell[key]
            L.append(
                f"| {E} | {key.upper()} | {e['n_fills']} | {_fmt(e['fills_per_day'],2)} | "
                f"{_fmt_c(e['mean_c'])} | {_fmt_c(e['median_c'])} | {_fmt_c(e['p10_c'])} | "
                f"{_fmt_c(e['min_c'])} | "
                f"{_fmt((e['pct_positive'] or 0)*100,0,'%') if e['pct_positive'] is not None else 'n/a'} | "
                f"{_fmt(e['c_per_day'],1)} |"
            )
    L.append("")
    L.append("_OPTIMISTIC = uncorrected sim; BASE = cap+mean-error, rate×maker-fill-factor, "
             "lock−(live B−tape B); PESSIMISTIC = cap+p10-error, rate×p10-factor, ≥1-lot & "
             "LAT-freshness drop, lock−(p90 B−tape B)−2c._")
    L.append("")
    # cap-correction-ALONE effect (the calibration that actually moves the number)
    L.append("**Cap correction alone (strict rule both sides, mean cap shift):**")
    L.append("")
    L.append("| E | opt fills/day | cap fills/day | Δ fills | opt mean c | cap mean c | Δ mean c |")
    L.append("|---|---|---|---|---|---|---|")
    for E, ce in fwd["cap_effect"].items():
        L.append(
            f"| {E} | {_fmt(ce['opt_fills_per_day'],2)} | {_fmt(ce['cap_fills_per_day'],2)} | "
            f"{ce['d_fills']:+d} | {_fmt_c(ce['opt_mean_c'])} | {_fmt_c(ce['cap_mean_c'])} | "
            f"{_fmt_c(ce['d_mean_c'])} |"
        )
    L.append("")
    return "\n".join(L)


def _collect_fills(results: list[WindowResult]) -> list[dict]:
    rows: list[dict] = []
    for r in results:
        fills = list(r.ideal_fills)
        for f in r.lag_fills.values():
            if f is not None:
                fills.append(f)
        if r.old_fill is not None:
            fills.append(r.old_fill)
        for f in fills:
            rows.append({
                "close": r.close_time, "model": f.model, "E": str(f.E),
                "tol": str(f.tol) if f.tol is not None else None,
                "deb": f.deb, "spot_Sd": f.spot_Sd, "spot_Su": f.spot_Su,
                "n": str(f.n), "offer": str(f.offer), "print_price": str(f.print_price),
                "print_size": str(f.print_size), "trade_ts": f.trade_ts,
                "regime": f.regime, "since_replace_ms": f.since_replace_ms,
                "W_completion": str(f.W_completion) if f.W_completion is not None else None,
                "lock_c": float(f.lock * 100) if f.lock is not None else None,
                "complete": f.complete,
            })
    return rows


def _window_row(r: WindowResult) -> dict:
    return {
        "close_time": r.close_time, "resolved_mode": r.resolved_mode,
        "effective_mode": r.effective_mode, "armed": r.armed, "params_sha": r.params_sha,
        "total_frames": r.total_frames, "driving_frames": r.driving_frames,
        "m15_frames": r.m15_frames, "strike_frames": r.strike_frames,
        "bucket_frames": r.bucket_frames, "bucket_trades": r.bucket_trades,
        "spot_bucket_yes_trades": r.spot_bucket_yes_trades,
        "metrics": r.metrics, "comparison": r.comparison,
        "base_replaces": r.lag_replaces.get(BASE_CELL, 0),
        "base_fill_lock_c": (float(r.base_fill.lock * 100)
                             if r.base_fill and r.base_fill.lock is not None else None),
    }


def _utf8_stdout() -> None:
    """Make stdout tolerant of non-cp1252 chars (Windows console / file redirects default to the
    locale codepage, which crashes on e.g. em-dashes)."""
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="V3.2 replay lab")
    ap.add_argument("--journals", default=DEFAULT_JOURNALS)
    ap.add_argument("--since", default=None, help="only windows with close date >= YYYY-MM-DD")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--calibrate-from", default=None,
                    help="journals dir to calibrate the sim's bucket assumptions from "
                         "(default: --journals)")
    ap.add_argument("--apply-to-forward", action="store_true",
                    help="run the corrected sim over the forward 139 h (needs historical-data/tob "
                         "+ 1-hour-range + the scratchpad range loader)")
    ap.add_argument("--range-loader", default=None,
                    help="path to the scratchpad range/ dir (rangelab.py) for --apply-to-forward")
    args = ap.parse_args(argv)

    params = _load_params()
    cal_dir = args.calibrate_from or args.journals
    paths = discover_journals(cal_dir, since=args.since)
    if not paths:
        print(f"[replay-lab] no *.jsonl.gz journals in {cal_dir}"
              f"{' since ' + args.since if args.since else ''}")
        return 1

    results: list[WindowResult] = []
    for p in paths:
        print(f"[replay-lab] replaying {os.path.basename(p)} ...", flush=True)
        results.append(run_window(p, params))

    est = build_estimates(results)
    calib = aggregate_calibration(results)
    run_date = max(r.close_time[:10] for r in results).replace("-", "")

    fwd = None
    if args.apply_to_forward:
        print("[replay-lab] applying corrected sim to the forward 139 h ...", flush=True)
        from .forward import build_forward_estimates, _RANGE_DIR
        fwd = build_forward_estimates(calib, range_dir=args.range_loader or _RANGE_DIR)

    os.makedirs(args.out, exist_ok=True)
    report = render_report(results, est, run_date)
    report += "\n" + render_calibration(calib)
    if fwd is not None:
        report += "\n" + render_forward(fwd)
    report_path = os.path.join(args.out, f"report_{run_date}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    with open(os.path.join(args.out, "per_window.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(_window_row(r), default=str) + "\n")
    with open(os.path.join(args.out, "fills.jsonl"), "w", encoding="utf-8") as f:
        for row in _collect_fills(results):
            f.write(json.dumps(row, default=str) + "\n")
    with open(os.path.join(args.out, "calibration.json"), "w", encoding="utf-8") as f:
        json.dump(calib, f, indent=1, default=str)
    if fwd is not None:
        with open(os.path.join(args.out, "forward.json"), "w", encoding="utf-8") as f:
            json.dump(fwd, f, indent=1, default=str)

    print()
    print(report)
    print(f"\n[replay-lab] wrote {report_path}")
    print(f"[replay-lab] wrote {os.path.join(args.out, 'per_window.jsonl')}")
    print(f"[replay-lab] wrote {os.path.join(args.out, 'fills.jsonl')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
