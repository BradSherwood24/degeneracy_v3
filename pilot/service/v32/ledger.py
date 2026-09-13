"""ledger.py — the append-only per-window V3.2 ledger (one JSON line per window).

Each ``run_v32`` process appends ONE object to ``pilot/ledger/v32_ledger.jsonl`` at close. The report
(``service.v32.report``) reads ONLY this artifact plus the journals. Money is kept as Decimal-safe
strings on disk; readers re-parse to Decimal so no float wobble enters the totals.

House law: this module places no orders, opens no socket, reads no sealed file — it is pure data +
file append/read. Phase 2 records shadow observations and would-be order counts; the REAL fills and
settlement backfill are Phase 3 — those field slots are present and left empty here so the schema is
stable across phases (a Phase-3 backfill appends its own row; the report tolerates the empty slots).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

DEFAULT_V32_LEDGER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ledger"
)
DEFAULT_V32_LEDGER_PATH = os.path.join(DEFAULT_V32_LEDGER_DIR, "v32_ledger.jsonl")


def _json_default(obj: object) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def append_v32_ledger_row(row: dict[str, Any], path: str = DEFAULT_V32_LEDGER_PATH) -> None:
    """Append one window row as a single JSON line (Decimals rendered as strings)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, default=_json_default))
        f.write("\n")


def load_v32_rows(path: str = DEFAULT_V32_LEDGER_PATH) -> list[dict[str, Any]]:
    """Read all rows in append order. Missing file -> []. Tolerates only a truncated trailing line
    (a crash mid-append; single writer, append-only)."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        nonempty = [ln.strip() for ln in f if ln.strip()]
    out: list[dict[str, Any]] = []
    for i, line in enumerate(nonempty):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(nonempty) - 1:
                break
            raise
    return out


def _shadow_summary(state) -> dict[str, Any]:
    """Per-E shadow fills + locks read off the final V32State (the in-process ideal fill rule).

    ``{E: {filled, n, offer, print, lock, W_at_completion}}`` — the sim statistic on live data. locks
    are Decimal-safe strings; an incomplete (awaiting) shadow leaves ``lock`` None."""
    out: dict[str, Any] = {}
    if state is None:
        return out
    for key, sub in getattr(state, "shadows", {}).items():
        entry: dict[str, Any] = {"E": sub.E, "filled": sub.filled, "n": sub.n}
        f = sub.fill
        if f is not None:
            entry.update({
                "offer": f.offer,
                "print": f.print_price,
                "count": f.count,
                "W_at_completion": f.W_at_completion,
                "lock": f.lock,
            })
        out[key] = entry
    return out


def build_v32_ledger_row(
    *,
    close_time: str,
    resolved_mode: str,
    effective_mode: str,
    degrade: str | None,
    params,
    state,
    driver_counts: dict[str, int],
    executor_counts: dict[str, int],
    ws_counts: dict[str, int],
    strike_count: int,
    strike_generations: int,
    bucket_count: int,
    bucket_generations: int,
    strike_lag_seconds: float | None,
    bucket_lag_seconds: float | None,
    journal_path: str | None,
    record_count: int,
    stand_down_reason: str | None,
    now: float,
    params_sha: str | None = None,
) -> dict[str, Any]:
    """One window row. ``armed`` is always False in Phase 2 (armed degrades to dry); ``fills`` and
    ``settlement`` are the Phase-3 slots (left empty). Discovery counts, would-be order counts,
    replaces, shadow fills-per-E with locks, alarms, per-connection lag, and the stand-down reason are
    all recorded so the report and the arming checklist read from one artifact."""
    spot_Sd = getattr(state, "spot_Sd", None) if state is not None else None
    spot_Su = getattr(state, "spot_Su", None) if state is not None else None
    bucket_ticker = None
    if state is not None and spot_Sd is not None:
        bucket_ticker = getattr(state, "bucket_tickers", {}).get(spot_Sd)
    row: dict[str, Any] = {
        "close_time": close_time,
        "mode": resolved_mode,
        "effective_mode": effective_mode,
        "armed": False,  # Phase 2: never armed (armed degrades to dry)
        "degrade": degrade,
        "params_sha": params.sha256 if params is not None else params_sha,
        "stand_down": stand_down_reason is not None,
        "stand_down_reason": stand_down_reason,
        # discovery
        "strike_count": strike_count,
        "strike_generations": strike_generations,
        "bucket_count": bucket_count,
        "bucket_generations": bucket_generations,
        # spot bucket at window end
        "spot_bucket_ticker": bucket_ticker,
        "Sd": spot_Sd,
        "Su": spot_Su,
        # would-be order activity (Phase 2 sends none)
        "would_places": int(driver_counts.get("would_place_rest", 0)),
        "would_cancels": int(driver_counts.get("would_cancel_rest", 0)),
        "would_takes": int(driver_counts.get("would_take_wings", 0)),
        "would_retries": int(driver_counts.get("would_retry_wing", 0)),
        "replaces": getattr(state, "replace_count", 0) if state is not None else 0,
        "sets_done": getattr(state, "sets_done", 0) if state is not None else 0,
        "stand_downs": int(driver_counts.get("stand_down", 0)),
        "late_fills": int(driver_counts.get("late_fill", 0)),
        # shadow (the ideal fill rule running live) — per E, with locks
        "shadow": _shadow_summary(state),
        # alarms
        "alarms": int(ws_counts.get("alarm", 0)),
        "synth_counts": dict(executor_counts),
        # per-connection lag (the two-connection topology's measured gauges)
        "strike_lag_seconds": strike_lag_seconds,
        "bucket_lag_seconds": bucket_lag_seconds,
        # journal
        "journal_path": journal_path,
        "record_count": record_count,
        "ws_counts": dict(ws_counts),
        # Phase-3 slots (real fills + settlement) — intentionally empty in Phase 2
        "fills": [],
        "settlement": None,
        "realized_delta": None,
        "flushed_at": now,
    }
    return row
