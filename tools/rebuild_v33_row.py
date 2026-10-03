"""rebuild_v33_row.py -- rebuild a V3.3 window's RECONCILED accounting from its JOURNAL (gate E).

Why this exists: gate E (the 2026-10-03 02:00Z naked fill) requires the ledger's economic facts to come
from EXECUTOR truth, not the core's bookkeeping. The live path reconciles at window end; this tool does
the same AFTER the fact, from a journal, so any historical row can be audited / rebuilt. It reads the
journal's ``rest_fill`` records (executor truth), ``take_wings`` records (completed-hedge coverage) and
``alarm`` records, and prints the reconciled ``lots_filled`` / ``one_legged`` / ``one_legged_contracts``
and the alarm breakdown.

Rebuilding the 02:00Z window reads lots 2 (0.40 + 0.60 + 1), one_legged True (no take_wings), alarms 10
(nine rest_invariant_violation + one executor_standdown; the lone rest_invariant_phantom is journaled
under its own kind and surfaced as ``executor_phantom``, not in the headline).

House law: READ-ONLY. It never writes the ledger, never touches the proxy, never reads key material, and
refuses any journal whose close date falls in the SEALED holdout window (2026-08-02..2026-08-18) unless an
explicit one-shot acknowledge flag is passed (which this tool's operator does NOT set in normal use).

Run:  python tools/rebuild_v33_row.py pilot/journals_v33/20261003T020000Z.jsonl.gz
      python tools/rebuild_v33_row.py <journal.jsonl[.gz]> --json
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys

# This file lives at <repo>/tools/rebuild_v33_row.py; pilot/ holds service.v33.reconcile.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PILOT = os.path.join(_REPO_ROOT, "pilot")
if _PILOT not in sys.path:
    sys.path.insert(0, _PILOT)

# The SEAL window (SEAL.md): the latest 17 UTC days of historical-data. A journal in this window is
# refused without the explicit one-shot ack (never set in normal operation) -- defence, even though
# operational journals_v33 start well after it.
_SEAL_LO = "2026-08-02"
_SEAL_HI = "2026-08-18"


def close_iso_from_filename(basename: str) -> str | None:
    """``20261003T020000Z.jsonl[.gz]`` -> ``2026-10-03T02:00:00Z``; None if it is not a window journal."""
    name = basename
    for suf in (".jsonl.gz", ".jsonl"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    else:
        return None
    if len(name) != 16 or name[8] != "T" or name[-1] != "Z":
        return None
    try:
        return f"{name[0:4]}-{name[4:6]}-{name[6:8]}T{name[9:11]}:{name[11:13]}:{name[13:15]}Z"
    except Exception:  # noqa: BLE001
        return None


def _records(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                yield r


def rebuild(path: str, *, acknowledge_sealed: bool = False) -> dict:
    from service.v33.reconcile import rebuild_from_records

    close_iso = close_iso_from_filename(os.path.basename(path))
    if close_iso is not None and _SEAL_LO <= close_iso[:10] <= _SEAL_HI and not acknowledge_sealed:
        raise SystemExit(f"REFUSED: {close_iso[:10]} is in the SEALED holdout [{_SEAL_LO}..{_SEAL_HI}]; "
                         f"this tool does not read it.")
    out = rebuild_from_records(_records(path))
    out["close_time"] = close_iso
    out["journal_path"] = os.path.abspath(path)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild a V3.3 window's reconciled accounting from a journal.")
    ap.add_argument("journal", help="Path to a window journal (.jsonl or .jsonl.gz).")
    ap.add_argument("--json", action="store_true", help="Print the reconciled row as JSON (default: human).")
    # NEVER set in normal operation; here only so the refusal is a one-shot ack, not a hard wall.
    ap.add_argument("--acknowledge-sealed", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    row = rebuild(args.journal, acknowledge_sealed=args.acknowledge_sealed)
    if args.json:
        print(json.dumps(row, sort_keys=True, default=str))
        return 0
    br = row.get("alarms_breakdown") or {}
    print(f"close_time           : {row.get('close_time')}")
    print(f"lots_filled          : {row.get('lots_filled')}")
    print(f"one_legged           : {row.get('one_legged')}")
    print(f"one_legged_contracts : {row.get('one_legged_contracts')}")
    print(f"exec_lots / hedged   : {row.get('exec_lots')} / {row.get('hedged_lots')}")
    print(f"alarms               : {row.get('alarms')}")
    print(f"  by_name            : {br.get('by_name')}")
    print(f"  executor_phantom   : {br.get('executor_phantom')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
