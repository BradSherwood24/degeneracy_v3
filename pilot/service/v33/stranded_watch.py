"""Stranded-fill watch (the ADVERSE-REPRICING watch) -- OBSERVATIONAL. Reads the V3.3 ledger only.

Registered 2026-10-05 00:45Z (ceremony/v33_falsifier.md, Registration). Brad, on the 00:00Z close that paid
$4.15 for a $4.00 floor: "Lets take a note of this window and watch for any others like it." This module is
the watch. It changes nothing, places nothing, and is not a falsifier quantity.

Vocabulary (from service/v33/core.py): a rung fill's ``rung`` label is its live position vs ``n_top`` at the
fill, ``round((n_top - price)/1c)``. A fill with ``rung < 0`` rested ABOVE the top of the ladder when it filled
-- "stranded above the top" -- i.e. the wings had already re-priced against the rest and the roll had not yet
moved it (the START debounce ``deb_ms`` with sign-flip re-debounce holds it for up to 5 s). A stranded fill is
routine (35% of armed fills as of the registration) and often locks POSITIVE; a window is ADVERSE when its
stranded fills' realised lock sums below zero -- the 10-04 06:00Z (8/8, -$0.35) and 10-05 00:00Z (3/3,
-$0.18) shape. That is what this watch counts.

Usage (from ``pilot/``)::

    python -m service.v33.stranded_watch --days 7            # table on stdout
    python -m service.v33.stranded_watch --days 7 --write    # also pilot/ops/v33_stranded_watch.{txt,json}
    python -m service.v33.stranded_watch --json

Only ARMED, REALISED rows count (``effective_mode == "armed"`` and not ``dry_sim``): dry rows carry simulated
fills under the ideal fill rule and would both inflate and mis-shape the census.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from service.paths import ledger_path_v33, ops_dir_v33

WATCH_TXT_NAME = "v33_stranded_watch.txt"
WATCH_JSON_NAME = "v33_stranded_watch.json"

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _close_date(row: dict[str, Any]) -> date | None:
    ct = row.get("close_time")
    if not isinstance(ct, str) or len(ct) < 10:
        return None
    try:
        return date.fromisoformat(ct[:10])
    except ValueError:
        return None


def is_armed_realised(row: dict[str, Any]) -> bool:
    """True for a window row that ran ARMED and books real fills (never a dry/simulated row)."""
    return (row.get("effective_mode") == "armed" and not bool(row.get("dry_sim"))
            and _close_date(row) is not None)


def classify_window(row: dict[str, Any]) -> dict[str, Any] | None:
    """Per-window census of stranded vs in-ladder rung fills. None when the row booked no rung fills."""
    rf = row.get("rung_fills")
    if not isinstance(rf, list) or not rf:
        return None
    stranded = 0
    ladder = 0
    stranded_lock = _ZERO
    ladder_lock = _ZERO
    depth_c = 0
    lock_missing = 0
    for f in rf:
        if not isinstance(f, dict):
            continue
        try:
            rung = int(f.get("rung"))
        except (TypeError, ValueError):
            continue
        cnt = _dec(f.get("count"))
        cnt = cnt if cnt is not None and cnt > _ZERO else _ONE
        lock = _dec(f.get("realized_lock"))
        if lock is None:
            lock_missing += 1
        if rung < 0:
            stranded += 1
            depth_c = max(depth_c, -rung)
            if lock is not None:
                stranded_lock += lock * cnt
        else:
            ladder += 1
            if lock is not None:
                ladder_lock += lock * cnt
    adverse = stranded > 0 and stranded_lock < _ZERO
    return {
        "close_time": row.get("close_time"),
        "bucket": row.get("spot_bucket_ticker"),
        "lots_filled": row.get("lots_filled"),
        "fills": stranded + ladder,
        "stranded": stranded,
        "ladder": ladder,
        "all_stranded": stranded > 0 and ladder == 0,
        "depth_c": depth_c,                       # deepest stranded fill, cents above the top at fill
        "stranded_lock": str(stranded_lock),
        "ladder_lock": str(ladder_lock),
        "lock_missing": lock_missing,
        "realized_delta": row.get("realized_delta"),
        "one_legged_contracts": row.get("one_legged_contracts"),
        "adverse": adverse,
    }


def _in_range(d: date, days: int | None, as_of: date) -> bool:
    if days is None:
        return True
    return (as_of - d).days < days


def build_watch(rows: list[dict[str, Any]], days: int | None = None,
                as_of: date | None = None) -> dict[str, Any]:
    """The watch over ARMED realised rows: per-window census (windows with fills only), totals, and the
    list of ADVERSE windows (stranded lock < 0). ``days`` trails back from ``as_of`` (default today UTC)."""
    as_of = as_of or datetime.now(timezone.utc).date()
    windows: list[dict[str, Any]] = []
    armed_windows = 0
    for r in rows:
        if not is_armed_realised(r):
            continue
        d = _close_date(r)
        assert d is not None
        if not _in_range(d, days, as_of):
            continue
        armed_windows += 1
        c = classify_window(r)
        if c is not None:
            windows.append(c)
    windows.sort(key=lambda w: str(w.get("close_time")))
    fills = sum(w["fills"] for w in windows)
    stranded = sum(w["stranded"] for w in windows)
    s_lock = sum((Decimal(w["stranded_lock"]) for w in windows), _ZERO)
    l_lock = sum((Decimal(w["ladder_lock"]) for w in windows), _ZERO)
    adverse = [w for w in windows if w["adverse"]]
    verdict = (f"WATCH: {len(adverse)} adverse-repricing window(s) in range"
               if adverse else "OK: no adverse-repricing window in range")
    return {
        "as_of": as_of.isoformat(),
        "days": days,
        "armed_windows": armed_windows,
        "windows_with_fills": len(windows),
        "fills": fills,
        "stranded": stranded,
        "stranded_share": (f"{(100.0 * stranded / fills):.0f}%" if fills else None),
        "stranded_lock": str(s_lock),
        "ladder_lock": str(l_lock),
        "adverse_windows": [w["close_time"] for w in adverse],
        "verdict": verdict,
        "windows": windows,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _money(s: str | None) -> str:
    d = _dec(s)
    return "   n/a " if d is None else f"{d:+8.4f}"


def render_txt(w: dict[str, Any]) -> str:
    rng = f"last {w['days']} d" if w.get("days") is not None else "all history"
    L = [f"STRANDED-FILL WATCH (adverse repricing) -- {w['verdict']}",
         "",
         f"as of {w['as_of']} UTC | {rng} | armed windows {w['armed_windows']}, with fills "
         f"{w['windows_with_fills']} | fills {w['fills']}, stranded {w['stranded']} "
         f"({w['stranded_share'] or 'n/a'}) | stranded lock {_money(w['stranded_lock']).strip()}, "
         f"ladder lock {_money(w['ladder_lock']).strip()}",
         "",
         "  close_time            lots  fills  stranded  depth_c  stranded_lock  ladder_lock  realized   flag",
         "  --------------------  ----  -----  --------  -------  -------------  -----------  ---------  -------"]
    for x in w["windows"]:
        flag = "ADVERSE" if x["adverse"] else ("all-str" if x["all_stranded"] else "")
        L.append(f"  {str(x['close_time']):<20}  {str(x['lots_filled']):>4}  {x['fills']:>5}  "
                 f"{x['stranded']:>8}  {x['depth_c']:>7}  {_money(x['stranded_lock']):>13}  "
                 f"{_money(x['ladder_lock']):>11}  {_money(x['realized_delta']):>9}  {flag}")
    if not w["windows"]:
        L.append("  (no armed window with fills in range)")
    L += ["",
          "  stranded = rung fill with rung < 0 (rested ABOVE n_top when it filled: the wings had re-priced",
          "  against it and the roll had not moved it yet). ADVERSE = the window's stranded lock sums < 0.",
          "  This watch only REPORTS (registered 2026-10-05 00:45Z). Levers remain Brad's.",
          f"  generated {w['generated']}"]
    return "\n".join(L) + "\n"


def write_outputs(w: dict[str, Any], ops_dir: str) -> tuple[str, str]:
    os.makedirs(ops_dir, exist_ok=True)
    txt = os.path.join(ops_dir, WATCH_TXT_NAME)
    js = os.path.join(ops_dir, WATCH_JSON_NAME)
    with open(txt, "w", encoding="utf-8") as f:
        f.write(render_txt(w))
    with open(js, "w", encoding="utf-8") as f:
        json.dump(w, f, sort_keys=True, indent=1, default=str)
    return txt, js


def load_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="V3.3 stranded-fill (adverse repricing) watch -- reads the "
                                             "ledger, reports, changes nothing.")
    ap.add_argument("--days", type=int, default=None, help="Trailing N UTC days (default: all history).")
    ap.add_argument("--ledger", default=None, help="V3.3 ledger path (default: the resolved live ledger).")
    ap.add_argument("--ops-dir", default=None, help="Where --write puts the txt/json (default: live ops dir).")
    ap.add_argument("--write", action="store_true", help="Also write v33_stranded_watch.{txt,json}.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    ledger = args.ledger if args.ledger is not None else ledger_path_v33()
    w = build_watch(load_rows(ledger), days=args.days)
    if args.write:
        write_outputs(w, args.ops_dir if args.ops_dir is not None else ops_dir_v33())
    if args.json:
        print(json.dumps(w, sort_keys=True, default=str))
    else:
        print(render_txt(w), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
