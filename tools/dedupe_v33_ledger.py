"""dedupe_v33_ledger.py -- collapse duplicate V3.3 ledger rows to one row per close_time.

Why this exists: the supervisor respawn bug (fixed in service/supervisor.py) respawned run_v33 hundreds
-to-thousands of times per hour at closes with no co-settling $100 KXBTC range buckets (the 21:00Z
close every day). Every respawn appended an identical stand-down row to ``ledger/v33_ledger.jsonl``, so
the ledger accumulated thousands of duplicate stand-down rows for the same close_time.

The V3.3 ledger is append-ONE-row-per-window by contract, so for any given close_time the LAST row is
the one to keep (the duplicates for a stood-down close are byte-identical anyway). This tool rewrites the
ledger keeping, for each close_time, only the last row -- preserving the original slot of that close so
window order is unchanged. Rows without a close_time are passed through untouched and never deduped.

Safety:
  - Idempotent: a second run over an already-clean file changes nothing (0 removed).
  - Crash-safe: writes a temp file in the same dir, fsyncs, then atomically ``os.replace`` over the
    ledger; a ``.bak`` copy of the original is kept first.
  - REFUSES to run when a ``run_v33`` window could be mid-write -- i.e. outside the safe UTC minute band
    [:02, :33]. run_v33 windows run :40 -> :00. Pass ``--force`` to override (document: run it between
    :02 and :33). ``--dry-run`` never writes and never refuses.
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


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    """Keep, for each close_time, only the LAST row (replaced in the FIRST occurrence's slot so window
    order is preserved). Rows lacking a close_time are kept untouched, never deduped.

    Returns (out_rows, stats) where stats has: read, unique_close, removed, no_close_kept, written.
    """
    out: list[dict] = []
    slot: dict[str, int] = {}
    no_close = 0
    for row in rows:
        ct = row.get("close_time") if isinstance(row, dict) else None
        if not ct:
            out.append(row)
            no_close += 1
            continue
        ct = str(ct)
        if ct in slot:
            out[slot[ct]] = row  # later row wins, in the original slot
        else:
            slot[ct] = len(out)
            out.append(row)
    stats = {
        "read": len(rows),
        "unique_close": len(slot),
        "removed": len(rows) - len(out),
        "no_close_kept": no_close,
        "written": len(out),
    }
    return out, stats


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
    """Dedupe the ledger at ``path``. Returns an exit code (0 ok, 1 refused, 2 error-shaped is unused)."""
    if not os.path.exists(path):
        log(f"[dedupe_v33] ledger not found: {path} (nothing to do)")
        return 0

    if not dry_run and not force and not in_safe_band(now):
        log(f"[dedupe_v33] REFUSING: current UTC minute {now.minute:02d} is outside the safe band "
            f"[:{SAFE_MIN_LO:02d}, :{SAFE_MIN_HI:02d}] -- a run_v33 window may be mid-write. Re-run "
            f"between :{SAFE_MIN_LO:02d} and :{SAFE_MIN_HI:02d} UTC, or pass --force.")
        return 1

    rows, bad = _load_rows(path)
    out_rows, stats = dedupe_rows(rows)
    if bad:
        log("[dedupe_v33] note: tolerated one truncated trailing line (dropped from the rewrite)")

    log(f"[dedupe_v33] path={path}")
    log(f"[dedupe_v33] read={stats['read']} unique_close={stats['unique_close']} "
        f"removed={stats['removed']} no_close_kept={stats['no_close_kept']} written={stats['written']}")

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
                   help="Report counts only; write nothing (and skip the time guard).")
    p.add_argument("--force", action="store_true",
                   help="Override the :02-:33 UTC safe-band guard (use only when no run_v33 is active).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.path or default_ledger_path()
    return run(path, dry_run=args.dry_run, force=args.force, now=datetime.now(timezone.utc))


if __name__ == "__main__":
    raise SystemExit(main())
