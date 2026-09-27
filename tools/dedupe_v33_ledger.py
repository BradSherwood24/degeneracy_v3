"""dedupe_v33_ledger.py -- collapse duplicate V3.3 ledger rows to one row per close_time.

Why this exists: the supervisor respawn bug (fixed in service/supervisor.py) respawned run_v33 hundreds
-to-thousands of times per hour at closes with no co-settling $100 KXBTC range buckets (the 21:00Z
close every day). Every respawn appended an identical stand-down row to ``ledger/v33_ledger.jsonl``, so
the ledger accumulated thousands of duplicate stand-down rows for the same close_time.

The V3.3 ledger has AT MOST ONE window row per close_time, but an ARMED window can ALSO get a later
settlement BACKFILL row for the same close_time (``mode == "backfill"``, ``backfill_of`` set; it carries
settlement_results/payoff/floor_netted/realized_delta only -- NO rung_fills/wing_batch_sets/ladder/
lock_solved). The reporter/falsifier read the WINDOW row (report._is_window_row = mode != "backfill") and
the backfill separately, so a backfill row must NEVER be collapsed away and never be used as the survivor.

So this tool collapses ONLY the duplicate NON-backfill window rows: for each close_time it keeps the LAST
non-backfill row (in that close's first-occurrence slot, so window order is unchanged). Backfill rows and
rows without a close_time pass through UNTOUCHED and are never deduped. If a close_time has two or more
non-backfill rows that differ by more than ``flushed_at`` / ``record_count`` (i.e. it would be choosing
between two genuinely different window rows), it REFUSES (exit 2, no write) and prints the offending
closes -- unless ``--force`` (which then keeps the last non-backfill row anyway).

Safety:
  - Idempotent: a second run over an already-clean file changes nothing (0 removed).
  - Crash-safe: writes a temp file in the same dir, fsyncs, then atomically ``os.replace`` over the
    ledger; a ``.bak`` copy of the original is kept first.
  - REFUSES to run when a ``run_v33`` window could be mid-write -- i.e. outside the safe UTC minute band
    [:02, :33]. run_v33 windows run :40 -> :00. Pass ``--force`` to override (document: run it between
    :02 and :33). ``--dry-run`` never writes and never refuses on the time band.
  - This tool NEVER touches the proxy and NEVER reads key material. It is a pure file rewrite.

Run:  python tools/dedupe_v33_ledger.py --dry-run
      python tools/dedupe_v33_ledger.py            # between :02 and :33 UTC
      python tools/dedupe_v33_ledger.py --force     # override the time guard
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

# This file lives at <repo>/tools/dedupe_v33_ledger.py; pilot/ holds service.paths.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PILOT = os.path.join(_REPO_ROOT, "pilot")
if _PILOT not in sys.path:
    sys.path.insert(0, _PILOT)

# The UTC minute band it is safe to rewrite in (run_v33 windows run :40 -> :00; safe = [:02, :33]).
SAFE_MIN_LO = 2
SAFE_MIN_HI = 33


def default_ledger_path() -> str:
    """The live V3.3 ledger path (respects DV3_DATA_DIR routing, same as run_v33)."""
    from service.paths import ledger_path_v33
    return ledger_path_v33()


def _load_rows(path: str) -> tuple[list[dict], int]:
    """Return (rows, bad_line_count). Tolerates only a truncated trailing line (matches
    ledger.load_v33_rows); a malformed interior line raises."""
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        nonempty = [ln.strip() for ln in f if ln.strip()]
    bad = 0
    for i, line in enumerate(nonempty):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(nonempty) - 1:
                bad = 1  # tolerate a single truncated trailing line
                break
            raise
    return rows, bad


# Fields allowed to differ between two rows of the same close_time WITHOUT it counting as a conflict:
# a respawn re-runs discovery so these two vary even between otherwise-identical stand-down rows.
_IGNORE_ON_COMPARE = ("flushed_at", "record_count")


def _is_backfill(row: dict) -> bool:
    """A settlement backfill row (mode == 'backfill' or carries backfill_of): NEVER deduped, never a
    survivor -- the reporter/falsifier read it separately from the window row."""
    return isinstance(row, dict) and (row.get("mode") == "backfill" or row.get("backfill_of") is not None)


def _compare_key(row: dict) -> dict:
    """The row minus the fields that legitimately vary across respawns, for conflict detection."""
    return {k: v for k, v in row.items() if k not in _IGNORE_ON_COMPARE}


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], dict, list[str]]:
    """Collapse duplicate NON-backfill window rows: for each close_time keep the LAST non-backfill row,
    in that close's first-occurrence slot (window order preserved). Backfill rows and rows lacking a
    close_time pass through UNTOUCHED and are never deduped or used as a survivor.

    Returns (out_rows, stats, conflicts) where:
      - stats has: read, unique_close, removed, no_close_kept, backfill_kept, written;
      - conflicts is the sorted list of close_times that had >= 2 non-backfill rows differing by more
        than flushed_at/record_count (would be choosing between two genuinely different window rows).
    """
    out: list[dict] = []
    slot: dict[str, int] = {}          # close_time -> index of its surviving non-backfill row in out
    variants: dict[str, list[dict]] = {}   # close_time -> distinct _compare_key dicts seen
    no_close = 0
    backfill_kept = 0
    for row in rows:
        if _is_backfill(row):
            out.append(row)            # passthrough; never a survivor, never deduped
            backfill_kept += 1
            continue
        ct = row.get("close_time") if isinstance(row, dict) else None
        if not ct:
            out.append(row)
            no_close += 1
            continue
        ct = str(ct)
        key = _compare_key(row)
        seen = variants.setdefault(ct, [])
        if key not in seen:
            seen.append(key)
        if ct in slot:
            out[slot[ct]] = row        # later non-backfill row wins, in the original slot
        else:
            slot[ct] = len(out)
            out.append(row)
    conflicts = sorted(ct for ct, seen in variants.items() if len(seen) > 1)
    stats = {
        "read": len(rows),
        "unique_close": len(slot),
        "removed": len(rows) - len(out),
        "no_close_kept": no_close,
        "backfill_kept": backfill_kept,
        "written": len(out),
    }
    return out, stats, conflicts


def _atomic_write(path: str, out_rows: list[dict]) -> None:
    """Write out_rows as JSONL to a temp file in the same dir, fsync, then atomically replace ``path``.
    Serialises rows byte-identically to the ledger writer (sort_keys, Decimals as str)."""
    from service.v33.ledger import _json_default  # same Decimal-aware serializer the writer uses

    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".dedupe_v33_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in out_rows:
                f.write(json.dumps(row, sort_keys=True, default=_json_default))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def in_safe_band(now: datetime) -> bool:
    """True iff ``now`` (UTC) is inside the safe rewrite band [:02, :33] -- no run_v33 window active."""
    return SAFE_MIN_LO <= now.minute <= SAFE_MIN_HI


def run(path: str, *, dry_run: bool, force: bool, now: datetime,
        log=print) -> int:
    """Dedupe the ledger at ``path``. Exit codes: 0 ok/no-op, 1 refused (time band), 2 refused
    (window-row conflict)."""
    if not os.path.exists(path):
        log(f"[dedupe_v33] ledger not found: {path} (nothing to do)")
        return 0

    if not dry_run and not force and not in_safe_band(now):
        log(f"[dedupe_v33] REFUSING: current UTC minute {now.minute:02d} is outside the safe band "
            f"[:{SAFE_MIN_LO:02d}, :{SAFE_MIN_HI:02d}] -- a run_v33 window may be mid-write. Re-run "
            f"between :{SAFE_MIN_LO:02d} and :{SAFE_MIN_HI:02d} UTC, or pass --force.")
        return 1

    rows, bad = _load_rows(path)
    out_rows, stats, conflicts = dedupe_rows(rows)
    if bad:
        log("[dedupe_v33] note: tolerated one truncated trailing line (dropped from the rewrite)")

    log(f"[dedupe_v33] path={path}")
    log(f"[dedupe_v33] read={stats['read']} unique_close={stats['unique_close']} "
        f"removed={stats['removed']} no_close_kept={stats['no_close_kept']} "
        f"backfill_kept={stats['backfill_kept']} written={stats['written']}")

    if conflicts and not force:
        log(f"[dedupe_v33] REFUSING: {len(conflicts)} close_time(s) have >= 2 DIFFERENT non-backfill "
            f"window rows (differ beyond flushed_at/record_count); refusing to choose. Inspect these, "
            f"then re-run with --force to keep the LAST non-backfill row per close:")
        for ct in conflicts:
            log(f"[dedupe_v33]   conflict close_time={ct}")
        return 2
    if conflicts and force:
        log(f"[dedupe_v33] --force: {len(conflicts)} conflicting close(s) collapsed to their LAST "
            f"non-backfill row: {', '.join(conflicts)}")

    if dry_run:
        log("[dedupe_v33] --dry-run: no files written")
        return 0

    if stats["removed"] == 0 and bad == 0:
        log("[dedupe_v33] already clean: nothing to rewrite (no .bak written)")
        return 0

    bak = path + ".bak"
    shutil.copy2(path, bak)
    log(f"[dedupe_v33] backup written: {bak}")
    _atomic_write(path, out_rows)
    log(f"[dedupe_v33] rewrote {path}: {stats['read']} -> {stats['written']} rows "
        f"({stats['removed']} removed)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Collapse duplicate V3.3 ledger rows to one per close_time (respawn-bug cleanup). "
                    "Run between :02 and :33 UTC; never touches the proxy.",
    )
    p.add_argument("--path", default=None,
                   help="Ledger path (default: the live v33_ledger.jsonl via service.paths).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report counts only; write nothing (and skip the time band guard).")
    p.add_argument("--force", action="store_true",
                   help="Override the :02-:33 UTC safe-band guard (use only when no run_v33 is active).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.path or default_ledger_path()
    return run(path, dry_run=args.dry_run, force=args.force, now=datetime.now(timezone.utc))


if __name__ == "__main__":
    raise SystemExit(main())
