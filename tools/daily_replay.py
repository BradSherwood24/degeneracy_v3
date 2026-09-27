"""daily_replay.py -- the DAILY REPLAY + TRIPWIRE runner (offline, over the ms journals).

Brad, 2026-09-27: "lets get an automated process to run the replay every morning."

What it does, once a morning:
  1. List ``pilot/journals_v32/*.jsonl.gz`` (only the gz -- never the live ``.jsonl`` a window is still
     writing). Compare against a manifest of journals already replayed (by filename + size). Run the
     V3.2 replay lab (``sim.v32_replay.lab``) over ONLY the new windows (oldest first, capped by
     ``--max-windows`` so a first run after a long gap cannot run for hours unattended).
  2. Merge the run's ``fills.jsonl`` into ``sim/out/v32_replay/fills_all.jsonl`` (dedupe on
     close+model+E+tol+deb+trade_ts) and ``per_window.jsonl`` into ``per_window_all.jsonl`` (dedupe on
     close+cell).
  3. Compute the TRIPWIRE from the base cell (model="lag", E="0.10"): trailing 7-day / 3-day n, mean,
     median, share negative, wing-drift p90. Verdict: TRIP if 7-day n>=15 and mean < +2.0c; WATCH if
     3-day n>=8 and mean < +2.0c; else OK. Writes ``pilot/ops/replay_tripwire.txt`` +
     ``replay_tripwire.json`` and appends a line to ``pilot/ops/replay_daily.log``.

+2.0c is the FROZEN V3.2 falsifier kill line (mean lock < +2.0c at n>=15). The tripwire only REPORTS;
standing V3.2 down remains Brad's lever.

Exit codes: 0 OK, 2 WATCH, 3 TRIP, 1 error. main() never raises -- it logs and returns 1 on any error.

House law: offline only (never the proxy), reads only ``*.jsonl.gz``, and the lab it drives refuses the
sealed/holdout dates (this runner never passes the acknowledge flag).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

# --- paths (this file lives at <repo>/tools/daily_replay.py) ---
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_JOURNALS = os.path.join(REPO_ROOT, "pilot", "journals_v32")
OUT_ROOT = os.path.join(REPO_ROOT, "sim", "out", "v32_replay")
DAILY_ROOT = os.path.join(OUT_ROOT, "daily")
MANIFEST_PATH = os.path.join(DAILY_ROOT, "manifest.json")
FILLS_ALL = os.path.join(OUT_ROOT, "fills_all.jsonl")
PER_WINDOW_ALL = os.path.join(OUT_ROOT, "per_window_all.jsonl")
OPS_DIR = os.path.join(REPO_ROOT, "pilot", "ops")
TRIPWIRE_TXT = os.path.join(OPS_DIR, "replay_tripwire.txt")
TRIPWIRE_JSON = os.path.join(OPS_DIR, "replay_tripwire.json")
DAILY_LOG = os.path.join(OPS_DIR, "replay_daily.log")

# The base cell the tripwire watches (mirrors sim.v32_replay.models.BASE_CELL, E leg only).
BASE_MODEL = "lag"
BASE_E = "0.10"
KILL_MEAN_C = 2.0        # the frozen V3.2 falsifier kill line (mean lock < +2.0c)
TRIP_MIN_N_7D = 15       # falsifier n>=15 over the trailing 7 days
WATCH_MIN_N_3D = 8       # early-warning n over the trailing 3 days
DEFAULT_MAX_WINDOWS = 60

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WATCH = 2
EXIT_TRIP = 3
_VERDICT_EXIT = {"OK": EXIT_OK, "WATCH": EXIT_WATCH, "TRIP": EXIT_TRIP}


# ---------------------------------------------------------------------------
# jsonl helpers
# ---------------------------------------------------------------------------
def read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file into a list of dicts; missing file -> []. Blank lines skipped."""
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                rows.append(json.loads(s))
    return rows


def write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


# ---------------------------------------------------------------------------
# manifest + journal discovery
# ---------------------------------------------------------------------------
def list_gz_journals(journals_dir: str) -> list[str]:
    """Sorted ``*.jsonl.gz`` basenames in ``journals_dir`` (NEVER the live ``.jsonl``)."""
    if not os.path.isdir(journals_dir):
        return []
    return sorted(b for b in os.listdir(journals_dir) if b.endswith(".jsonl.gz"))


def load_manifest(path: str | None = None) -> dict:
    """Load the replay manifest: ``{"files": {basename: {"size": int, "replayed_at": iso}}}``.
    ``path`` defaults to the module-level MANIFEST_PATH (resolved at call time so tests can override)."""
    path = path or MANIFEST_PATH
    if not os.path.exists(path):
        return {"files": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "files" not in data or not isinstance(data.get("files"), dict):
        data = {"files": {}}
    return data


def save_manifest(manifest: dict, path: str | None = None) -> None:
    path = path or MANIFEST_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)


def select_new(journals_dir: str, manifest: dict, max_windows: int, full: bool = False) -> tuple[list[str], int]:
    """Return ``(to_run, deferred)`` -- basenames not yet replayed (oldest first), capped at
    ``max_windows``; ``deferred`` = how many eligible windows were left for a later run by the cap.

    A journal counts as done when the manifest has its basename with the SAME size (a re-rotated or
    still-growing file with a different size is treated as new). ``full=True`` ignores the manifest
    (rebuild), still honoring the cap."""
    files = list_gz_journals(journals_dir)
    done = manifest.get("files", {})
    eligible: list[str] = []
    for b in files:
        if full:
            eligible.append(b)
            continue
        entry = done.get(b)
        try:
            size = os.path.getsize(os.path.join(journals_dir, b))
        except OSError:
            size = None
        if entry is None or (size is not None and entry.get("size") != size):
            eligible.append(b)
    to_run = eligible[:max_windows]
    deferred = len(eligible) - len(to_run)
    return to_run, deferred


def mark_done(manifest: dict, journals_dir: str, basenames: list[str], when: str | None = None) -> None:
    """Record ``basenames`` as replayed in the manifest (basename -> size + timestamp)."""
    when = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = manifest.setdefault("files", {})
    for b in basenames:
        try:
            size = os.path.getsize(os.path.join(journals_dir, b))
        except OSError:
            size = None
        files[b] = {"size": size, "replayed_at": when}


# ---------------------------------------------------------------------------
# filename <-> close mapping (record_range.journal_filename inverse)
# ---------------------------------------------------------------------------
def close_iso_from_filename(basename: str) -> str | None:
    """``20260914T170000Z.jsonl.gz`` -> ``2026-09-14T17:00:00Z`` (inverse of
    ``record_range.journal_filename``). Returns None if the stem is not that fixed-width shape."""
    stem = os.path.basename(basename).split(".", 1)[0]
    if len(stem) < 16 or stem[8] != "T" or stem[15] != "Z":
        return None
    if not (stem[:8].isdigit() and stem[9:15].isdigit()):
        return None
    return f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}T{stem[9:11]}:{stem[11:13]}:{stem[13:15]}Z"


# ---------------------------------------------------------------------------
# merge / dedupe
# ---------------------------------------------------------------------------
def _fill_key(row: dict):
    return (row.get("close"), row.get("model"), row.get("E"),
            row.get("tol"), row.get("deb"), row.get("trade_ts"))


def _pw_key(row: dict):
    # per_window rows are one per window; keep close+cell so a future cell split still dedupes.
    return (row.get("close_time") or row.get("close"), row.get("cell"))


def merge_rows(existing: list[dict], new: list[dict], keyfn) -> tuple[list[dict], int, int]:
    """Merge ``new`` into ``existing`` de-duplicating on ``keyfn`` (existing wins on collision).
    Returns ``(merged, n_added, n_dup)``."""
    seen = {keyfn(r) for r in existing}
    merged = list(existing)
    added = dup = 0
    for r in new:
        k = keyfn(r)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        merged.append(r)
        added += 1
    return merged, added, dup


def merge_fills(fills_all_path: str, new_fills_path: str) -> tuple[int, int, int]:
    existing = read_jsonl(fills_all_path)
    new = read_jsonl(new_fills_path)
    merged, added, dup = merge_rows(existing, new, _fill_key)
    write_jsonl(fills_all_path, merged)
    return len(merged), added, dup


def merge_per_window(pw_all_path: str, new_pw_path: str) -> tuple[int, int, int]:
    existing = read_jsonl(pw_all_path)
    new = read_jsonl(new_pw_path)
    merged, added, dup = merge_rows(existing, new, _pw_key)
    write_jsonl(pw_all_path, merged)
    return len(merged), added, dup


# ---------------------------------------------------------------------------
# tripwire math
# ---------------------------------------------------------------------------
def _base_fills(fills: list[dict]) -> list[dict]:
    out = []
    for f in fills:
        if f.get("model") != BASE_MODEL or str(f.get("E")) != BASE_E:
            continue
        if f.get("lock_c") is None:
            continue
        out.append(f)
    return out


def _wing_drift_c(f: dict) -> float | None:
    """drift = W_completion - (2 - E - n), in cents. None if either is missing."""
    W = f.get("W_completion")
    n = f.get("n")
    if W is None or n is None:
        return None
    try:
        return (float(W) - (2.0 - float(BASE_E) - float(n))) * 100.0
    except (TypeError, ValueError):
        return None


def _percentile(vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0,1]); None if empty."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _stats_for(fills: list[dict]) -> dict:
    locks = [float(f["lock_c"]) for f in fills]
    drifts = [d for d in (_wing_drift_c(f) for f in fills) if d is not None]
    n = len(locks)
    return {
        "n": n,
        "mean_c": round(statistics.fmean(locks), 3) if n else None,
        "median_c": round(statistics.median(locks), 3) if n else None,
        "share_negative": round(sum(1 for x in locks if x < 0) / n, 3) if n else None,
        "n_negative": sum(1 for x in locks if x < 0),
        "wing_drift_p90_c": round(_percentile(drifts, 0.90), 3) if drifts else None,
    }


def compute_tripwire(fills: list[dict], as_of: date) -> dict:
    """Trailing 7-day / 3-day tripwire on the base cell. ``as_of`` anchors the trailing windows (the
    UTC date the run happens); the windows are [as_of-6, as_of] and [as_of-2, as_of] inclusive."""
    base = _base_fills(fills)

    def in_window(days: int) -> list[dict]:
        lo = as_of - timedelta(days=days - 1)
        return [f for f in base if lo <= _to_date(f["close"]) <= as_of]

    w7 = in_window(7)
    w3 = in_window(3)
    s7 = _stats_for(w7)
    s3 = _stats_for(w3)

    verdict = "OK"
    reason = "no kill/watch condition met"
    if s7["n"] >= TRIP_MIN_N_7D and s7["mean_c"] is not None and s7["mean_c"] < KILL_MEAN_C:
        verdict = "TRIP"
        reason = (f"7-day n={s7['n']} (>= {TRIP_MIN_N_7D}) and mean {s7['mean_c']:+.2f}c "
                  f"< +{KILL_MEAN_C:.1f}c kill line")
    elif s3["n"] >= WATCH_MIN_N_3D and s3["mean_c"] is not None and s3["mean_c"] < KILL_MEAN_C:
        verdict = "WATCH"
        reason = (f"3-day n={s3['n']} (>= {WATCH_MIN_N_3D}) and mean {s3['mean_c']:+.2f}c "
                  f"< +{KILL_MEAN_C:.1f}c (early warning; 7-day not yet at n>={TRIP_MIN_N_7D} or > kill)")

    per_day = []
    for i in range(6, -1, -1):
        d = as_of - timedelta(days=i)
        day_fills = [f for f in base if _to_date(f["close"]) == d]
        per_day.append({"date": d.isoformat(), **_stats_for(day_fills)})

    return {
        "as_of": as_of.isoformat(),
        "verdict": verdict,
        "reason": reason,
        "kill_mean_c": KILL_MEAN_C,
        "base_cell": {"model": BASE_MODEL, "E": BASE_E},
        "trailing_7d": s7,
        "trailing_3d": s3,
        "per_day_last7": per_day,
        "n_base_fills_total": len(base),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _to_date(close_iso: str) -> date:
    return date.fromisoformat(close_iso[:10])


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _c(x) -> str:
    return "n/a" if x is None else f"{x:+.2f}c"


def render_tripwire_txt(tw: dict) -> str:
    L: list[str] = []
    L.append(f"TRIPWIRE: {tw['verdict']}  ({tw['reason']})")
    L.append("")
    L.append(f"as of {tw['as_of']} UTC | base cell model={tw['base_cell']['model']} "
             f"E={tw['base_cell']['E']} | kill line = mean < +{tw['kill_mean_c']:.1f}c at n>=15 (7d)")
    s7, s3 = tw["trailing_7d"], tw["trailing_3d"]
    L.append(f"  7-day: n={s7['n']:<3d} mean={_c(s7['mean_c'])} median={_c(s7['median_c'])} "
             f"neg={s7['n_negative']}/{s7['n']} drift_p90={_c(s7['wing_drift_p90_c'])}")
    L.append(f"  3-day: n={s3['n']:<3d} mean={_c(s3['mean_c'])} median={_c(s3['median_c'])} "
             f"neg={s3['n_negative']}/{s3['n']} drift_p90={_c(s3['wing_drift_p90_c'])}")
    L.append("")
    L.append("  date        fills   mean      negatives")
    L.append("  ----------  -----   -------   ---------")
    for d in tw["per_day_last7"]:
        L.append(f"  {d['date']}  {d['n']:>5d}   {_c(d['mean_c']):>7}   {d['n_negative']}/{d['n']}")
    L.append("")
    L.append(f"  base fills tracked (all history): {tw['n_base_fills_total']}")
    L.append(f"  generated {tw['generated_at']}")
    L.append("")
    L.append("  Verdict rules: TRIP = 7-day n>=15 and mean < +2.0c (the frozen V3.2 falsifier kill).")
    L.append("                 WATCH = 3-day n>=8 and mean < +2.0c (early warning).  else OK.")
    L.append("  The tripwire only REPORTS. Standing V3.2 down remains Brad's lever.")
    L.append("")
    return "\n".join(L)


def write_tripwire_outputs(tw: dict) -> None:
    os.makedirs(OPS_DIR, exist_ok=True)
    with open(TRIPWIRE_TXT, "w", encoding="utf-8") as f:
        f.write(render_tripwire_txt(tw))
    with open(TRIPWIRE_JSON, "w", encoding="utf-8") as f:
        json.dump(tw, f, indent=1)


def append_daily_log(line: str) -> None:
    os.makedirs(OPS_DIR, exist_ok=True)
    with open(DAILY_LOG, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# lab invocation (subprocess; isolated so a heavy run cannot poison this process, and so tests can
# monkeypatch it)
# ---------------------------------------------------------------------------
def run_lab(journals_dir: str, files: list[str], out_dir: str) -> int:
    """Run ``python -m sim.v32_replay.lab --journals <dir> --files-from <list> --out <out_dir>`` over
    ONLY ``files``. Returns the lab's exit code."""
    os.makedirs(out_dir, exist_ok=True)
    list_path = os.path.join(out_dir, "files_to_run.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("# journals replayed this run (one basename per line)\n")
        for b in files:
            f.write(b + "\n")
    cmd = [sys.executable, "-m", "sim.v32_replay.lab",
           "--journals", journals_dir, "--files-from", list_path, "--out", out_dir]
    print(f"[daily-replay] running lab over {len(files)} window(s) -> {out_dir}", flush=True)
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    return proc.returncode


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------
def seed_from(seed_dir: str, journals_dir: str) -> dict:
    """Import an existing lab output dir (``fills.jsonl`` + ``per_window.jsonl``) into fills_all /
    per_window_all, and mark every journal in ``journals_dir`` whose close is present in the imported
    per_window as done in the manifest (so the first scheduled run only picks up NEW windows).

    Returns a small summary dict."""
    fills_src = os.path.join(seed_dir, "fills.jsonl")
    pw_src = os.path.join(seed_dir, "per_window.jsonl")
    if not os.path.exists(fills_src) or not os.path.exists(pw_src):
        raise FileNotFoundError(f"seed dir must contain fills.jsonl and per_window.jsonl: {seed_dir}")

    _, f_added, f_dup = merge_fills(FILLS_ALL, fills_src)
    _, p_added, p_dup = merge_per_window(PER_WINDOW_ALL, pw_src)

    seeded_closes = {(r.get("close_time") or r.get("close")) for r in read_jsonl(pw_src)}
    seeded_closes.discard(None)

    manifest = load_manifest()
    marked: list[str] = []
    for b in list_gz_journals(journals_dir):
        c = close_iso_from_filename(b)
        if c is not None and c in seeded_closes:
            marked.append(b)
    mark_done(manifest, journals_dir, marked)
    save_manifest(manifest)

    return {
        "fills_added": f_added, "fills_dup": f_dup,
        "per_window_added": p_added, "per_window_dup": p_dup,
        "closes_seeded": len(seeded_closes), "journals_marked_done": len(marked),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _finish(tw: dict, log_prefix: str) -> int:
    """Write the tripwire outputs, append the daily log line, return the verdict exit code."""
    write_tripwire_outputs(tw)
    s7, s3 = tw["trailing_7d"], tw["trailing_3d"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    append_daily_log(
        f"{stamp} {log_prefix} verdict={tw['verdict']} "
        f"7d(n={s7['n']},mean={_c(s7['mean_c'])},neg={s7['n_negative']}) "
        f"3d(n={s3['n']},mean={_c(s3['mean_c'])}) "
        f"base_fills_total={tw['n_base_fills_total']}"
    )
    print(f"[daily-replay] verdict={tw['verdict']} -- {tw['reason']}")
    print(f"[daily-replay] wrote {TRIPWIRE_TXT}")
    return _VERDICT_EXIT[tw["verdict"]]


def _run(args) -> int:
    as_of = (datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of
             else datetime.now(timezone.utc).date())

    # --- seed mode: import + mark manifest, then report the tripwire and exit ---
    if args.seed_from:
        summary = seed_from(args.seed_from, args.journals)
        print(f"[daily-replay] seeded: {summary}")
        tw = compute_tripwire(read_jsonl(FILLS_ALL), as_of)
        return _finish(tw, f"seed(from={os.path.basename(args.seed_from.rstrip(os.sep))},"
                           f"added={summary['fills_added']},marked={summary['journals_marked_done']})")

    manifest = load_manifest()
    to_run, deferred = select_new(args.journals, manifest, args.max_windows, full=args.full)

    # --- dry-run: list the selection, do not replay ---
    if args.dry_run:
        print(f"[daily-replay] DRY RUN -- would replay {len(to_run)} window(s) "
              f"(cap={args.max_windows}, deferred={deferred}, full={args.full}):")
        for b in to_run:
            print(f"  {b}")
        if not to_run:
            print("  (none -- manifest is current)")
        return EXIT_OK

    # --- nothing new: refresh the tripwire from existing history and exit ---
    if not to_run:
        print("[daily-replay] no new windows to replay; refreshing tripwire from existing history.")
        tw = compute_tripwire(read_jsonl(FILLS_ALL), as_of)
        return _finish(tw, "run(new=0,replayed=0)")

    # --- replay the new windows into a timestamped dir ---
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    run_out = os.path.join(DAILY_ROOT, run_stamp)
    rc = run_lab(args.journals, to_run, run_out)
    if rc != 0:
        print(f"[daily-replay] lab exited {rc}; NOT updating the manifest (windows will be retried "
              f"next run). Refreshing tripwire from existing history.")
        tw = compute_tripwire(read_jsonl(FILLS_ALL), as_of)
        write_tripwire_outputs(tw)
        append_daily_log(
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"run(new={len(to_run)},lab_rc={rc}) ERROR verdict={tw['verdict']}"
        )
        return EXIT_ERROR

    # --- merge outputs, update manifest ---
    _, f_added, f_dup = merge_fills(FILLS_ALL, os.path.join(run_out, "fills.jsonl"))
    _, p_added, p_dup = merge_per_window(PER_WINDOW_ALL, os.path.join(run_out, "per_window.jsonl"))
    mark_done(manifest, args.journals, to_run)
    save_manifest(manifest)
    print(f"[daily-replay] merged fills(+{f_added}, dup {f_dup}) "
          f"per_window(+{p_added}, dup {p_dup}); manifest now {len(manifest['files'])} windows.")

    tw = compute_tripwire(read_jsonl(FILLS_ALL), as_of)
    return _finish(tw, f"run(new={len(to_run)},replayed={len(to_run)},deferred={deferred},"
                       f"fills+{f_added})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily incremental V3.2 replay + falsifier tripwire")
    ap.add_argument("--journals", default=DEFAULT_JOURNALS,
                    help="journals dir (default pilot/journals_v32); only *.jsonl.gz are read")
    ap.add_argument("--max-windows", type=int, default=DEFAULT_MAX_WINDOWS,
                    help="cap on windows replayed per run (default 60) so a first run after a long gap "
                         "cannot run for hours; the rest carry to the next morning")
    ap.add_argument("--full", action="store_true",
                    help="ignore the manifest and replay all discovered windows (still capped by "
                         "--max-windows)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the windows that would be replayed, then exit without running")
    ap.add_argument("--seed-from", default=None,
                    help="import an existing lab output dir (fills.jsonl + per_window.jsonl) into "
                         "fills_all/per_window_all and mark those journals done in the manifest")
    ap.add_argument("--as-of", default=None,
                    help="anchor date YYYY-MM-DD for the trailing tripwire windows (default: today UTC)")
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 -- main never raises; a scheduled task must not crash
        import traceback
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[daily-replay] ERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            append_daily_log(f"{stamp} ERROR {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
