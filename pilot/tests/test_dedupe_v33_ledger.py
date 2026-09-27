"""Tests for tools/dedupe_v33_ledger.py -- the V3.3 ledger duplicate-row cleanup (respawn-bug fix).

All offline, on synthetic files in tmp_path; the live ledger is never touched. Injected clock for the
:02-:33 UTC safe-band guard.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dedupe_v33_ledger as dd  # noqa: E402


def _utc(minute: int) -> datetime:
    return datetime(2026, 9, 27, 12, minute, 0, tzinfo=timezone.utc)


def _standdown_row(close: str, tag: str = "no KXBTC range buckets co-settling",
                   record_count: int | None = None, flushed_at: float | None = None) -> dict:
    row = {"roster": "DegeneracyV3_3", "close_time": close, "stand_down": True,
           "stand_down_reason": tag, "mode": "dry"}
    if record_count is not None:
        row["record_count"] = record_count
    if flushed_at is not None:
        row["flushed_at"] = flushed_at
    return row


def _window_row(close: str, *, realized_lock: str = "3.10", mode: str = "armed") -> dict:
    return {"roster": "DegeneracyV3_3", "close_time": close, "mode": mode, "armed": True,
            "stand_down": False, "rung_fills": [{"E": 10}], "ladder": {"rungs": 3},
            "realized_lock": realized_lock, "realized_unsettled": True,
            "held_legs": [{"ticker": "KXBTC-X"}]}


def _backfill_row(close: str) -> dict:
    return {"roster": "DegeneracyV3_3", "close_time": close, "mode": "backfill",
            "backfill_of": close, "armed": True, "settlement_results": {"KXBTC-X": "yes"},
            "settlement_payoff": "5.00", "floor_netted": "3.10", "realized_delta": "1.90"}


def _write_jsonl(path, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _read_jsonl(path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


# ---------------------------------------------------------------------------
# pure dedupe
# ---------------------------------------------------------------------------
def test_dedupe_keeps_last_per_close_preserving_slot():
    # dups differ ONLY in record_count/flushed_at (the respawn signature) -> NOT a conflict; last wins.
    rows = [
        _standdown_row("2026-09-23T20:00:00Z", record_count=1),
        _standdown_row("2026-09-23T21:00:00Z", record_count=1, flushed_at=1.0),   # dup #1
        _standdown_row("2026-09-23T21:00:00Z", record_count=2, flushed_at=2.0),   # dup #2
        _standdown_row("2026-09-23T21:00:00Z", record_count=3, flushed_at=3.0),   # dup #3 (last wins)
        _standdown_row("2026-09-23T22:00:00Z", record_count=1),
    ]
    out, stats, conflicts = dd.dedupe_rows(rows)
    assert conflicts == []
    assert stats == {"read": 5, "unique_close": 3, "removed": 2, "no_close_kept": 0,
                     "backfill_kept": 0, "written": 3}
    closes = [r["close_time"] for r in out]
    assert closes == ["2026-09-23T20:00:00Z", "2026-09-23T21:00:00Z", "2026-09-23T22:00:00Z"]
    # the 21:00Z slot holds the LAST row (record_count == 3)
    assert out[1]["record_count"] == 3


def test_dedupe_idempotent_on_clean_input():
    rows = [_standdown_row("2026-09-23T20:00:00Z"), _standdown_row("2026-09-23T21:00:00Z")]
    out1, s1, c1 = dd.dedupe_rows(rows)
    out2, s2, c2 = dd.dedupe_rows(out1)
    assert s1["removed"] == 0 and s2["removed"] == 0
    assert c1 == [] and c2 == []
    assert out1 == out2 == rows


def test_dedupe_keeps_rows_without_close_time():
    rows = [
        {"note": "header, no close_time"},
        _standdown_row("2026-09-23T21:00:00Z"),
        _standdown_row("2026-09-23T21:00:00Z"),
        {"note": "footer, no close_time"},
    ]
    out, stats, conflicts = dd.dedupe_rows(rows)
    assert conflicts == []
    assert stats["no_close_kept"] == 2
    assert stats["removed"] == 1
    assert out[0] == {"note": "header, no close_time"}
    assert out[-1] == {"note": "footer, no close_time"}


# ---------------------------------------------------------------------------
# F1: backfill rows are never collapsed; window-row conflicts are refused
# ---------------------------------------------------------------------------
def test_window_row_and_later_backfill_both_kept_order_preserved():
    """An armed window row + its later settlement backfill row (same close_time) BOTH survive, in order:
    keep-last must never destroy the window row the reporter/falsifier read."""
    rows = [
        _window_row("2026-09-23T21:00:00Z"),      # the armed window row
        _standdown_row("2026-09-23T22:00:00Z"),
        _backfill_row("2026-09-23T21:00:00Z"),    # its settlement backfill, appended LATER, same close
    ]
    out, stats, conflicts = dd.dedupe_rows(rows)
    assert conflicts == []
    assert stats["removed"] == 0
    assert stats["backfill_kept"] == 1
    # BOTH the 21:00Z rows survive, window first then backfill (append order preserved)
    ct21 = [r for r in out if r["close_time"] == "2026-09-23T21:00:00Z"]
    assert len(ct21) == 2
    assert ct21[0]["mode"] == "armed" and "rung_fills" in ct21[0]
    assert ct21[1]["mode"] == "backfill" and ct21[1]["backfill_of"] == "2026-09-23T21:00:00Z"
    assert [r.get("mode") for r in out] == ["armed", "dry", "backfill"]


def test_backfill_never_used_as_survivor_for_standdown_storm():
    """A stand-down storm collapses to one; a same-close backfill (if any) still passes through."""
    rows = [_standdown_row("2026-09-23T21:00:00Z", record_count=i) for i in range(3500)]
    rows.append(_backfill_row("2026-09-23T21:00:00Z"))  # armed-path backfill, must survive untouched
    out, stats, conflicts = dd.dedupe_rows(rows)
    assert conflicts == []
    assert stats["read"] == 3501
    assert stats["written"] == 2                 # one collapsed stand-down + the backfill
    assert stats["removed"] == 3499
    assert stats["backfill_kept"] == 1
    modes = [r.get("mode") for r in out]
    assert modes == ["dry", "backfill"]


def test_conflict_two_different_window_rows_refused(tmp_path):
    """Two GENUINELY different non-backfill rows for one close (differ beyond flushed_at/record_count)
    -> refuse (exit 2), write nothing, no .bak; --force overrides and keeps the last."""
    p = tmp_path / "v33_ledger.jsonl"
    _write_jsonl(p, [
        _window_row("2026-09-23T21:00:00Z", realized_lock="3.10"),
        _window_row("2026-09-23T21:00:00Z", realized_lock="9.99"),   # different money math
    ])
    logs = []
    rc = dd.run(str(p), dry_run=False, force=False, now=_utc(10), log=logs.append)
    assert rc == 2
    assert len(_read_jsonl(p)) == 2                       # untouched
    assert not os.path.exists(str(p) + ".bak")
    assert any("conflict close_time=2026-09-23T21:00:00Z" in ln for ln in logs)

    # --force collapses to the LAST row (realized_lock 9.99)
    rc2 = dd.run(str(p), dry_run=False, force=True, now=_utc(45), log=lambda *_: None)
    assert rc2 == 0
    kept = _read_jsonl(p)
    assert len(kept) == 1 and kept[0]["realized_lock"] == "9.99"
    assert os.path.exists(str(p) + ".bak")


def test_no_conflict_when_only_flushed_at_and_record_count_differ():
    """Two rows of the same close differing ONLY in flushed_at/record_count are NOT a conflict."""
    rows = [
        _standdown_row("2026-09-23T21:00:00Z", record_count=1, flushed_at=1.0),
        _standdown_row("2026-09-23T21:00:00Z", record_count=2, flushed_at=2.0),
    ]
    out, stats, conflicts = dd.dedupe_rows(rows)
    assert conflicts == []
    assert stats["written"] == 1


# ---------------------------------------------------------------------------
# safe-band guard
# ---------------------------------------------------------------------------
def test_in_safe_band_boundaries():
    assert dd.in_safe_band(_utc(1)) is False
    assert dd.in_safe_band(_utc(2)) is True
    assert dd.in_safe_band(_utc(33)) is True
    assert dd.in_safe_band(_utc(34)) is False
    assert dd.in_safe_band(_utc(45)) is False   # mid-window
    assert dd.in_safe_band(_utc(0)) is False


def test_run_refuses_outside_band_without_force(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    _write_jsonl(p, [_standdown_row("2026-09-23T21:00:00Z"),
                     _standdown_row("2026-09-23T21:00:00Z")])
    rc = dd.run(str(p), dry_run=False, force=False, now=_utc(45), log=lambda *_: None)
    assert rc == 1
    # untouched: still 2 rows, no .bak
    assert len(_read_jsonl(p)) == 2
    assert not os.path.exists(str(p) + ".bak")


def test_run_force_overrides_band(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    _write_jsonl(p, [_standdown_row("2026-09-23T21:00:00Z"),
                     _standdown_row("2026-09-23T21:00:00Z")])
    rc = dd.run(str(p), dry_run=False, force=True, now=_utc(45), log=lambda *_: None)
    assert rc == 0
    assert len(_read_jsonl(p)) == 1
    assert os.path.exists(str(p) + ".bak")


def test_dry_run_writes_nothing_and_ignores_band(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    _write_jsonl(p, [_standdown_row("2026-09-23T21:00:00Z"),
                     _standdown_row("2026-09-23T21:00:00Z")])
    rc = dd.run(str(p), dry_run=True, force=False, now=_utc(45), log=lambda *_: None)
    assert rc == 0
    assert len(_read_jsonl(p)) == 2          # unchanged
    assert not os.path.exists(str(p) + ".bak")


# ---------------------------------------------------------------------------
# real rewrite: atomic replace + backup + idempotency on file
# ---------------------------------------------------------------------------
def test_run_rewrites_and_backs_up(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    rows = ([_standdown_row("2026-09-23T21:00:00Z")] * 3500
            + [_standdown_row("2026-09-23T22:00:00Z")])
    _write_jsonl(p, rows)
    rc = dd.run(str(p), dry_run=False, force=False, now=_utc(10), log=lambda *_: None)
    assert rc == 0
    kept = _read_jsonl(p)
    assert [r["close_time"] for r in kept] == ["2026-09-23T21:00:00Z", "2026-09-23T22:00:00Z"]
    # backup holds the original 3501 rows
    assert len(_read_jsonl(str(p) + ".bak")) == 3501


def test_run_already_clean_no_bak(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    _write_jsonl(p, [_standdown_row("2026-09-23T21:00:00Z"),
                     _standdown_row("2026-09-23T22:00:00Z")])
    rc = dd.run(str(p), dry_run=False, force=False, now=_utc(10), log=lambda *_: None)
    assert rc == 0
    assert len(_read_jsonl(p)) == 2
    assert not os.path.exists(str(p) + ".bak")   # nothing removed -> no backup churn


def test_run_missing_file_is_noop(tmp_path):
    p = tmp_path / "does_not_exist.jsonl"
    rc = dd.run(str(p), dry_run=False, force=False, now=_utc(45), log=lambda *_: None)
    assert rc == 0
    assert not os.path.exists(p)


def test_run_tolerates_truncated_trailing_line(tmp_path):
    p = tmp_path / "v33_ledger.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(_standdown_row("2026-09-23T21:00:00Z")) + "\n")
        f.write(json.dumps(_standdown_row("2026-09-23T21:00:00Z")) + "\n")
        f.write('{"close_time": "2026-09-23T22:00:00Z", "stand_')  # truncated (crash mid-write)
    rc = dd.run(str(p), dry_run=False, force=True, now=_utc(45), log=lambda *_: None)
    assert rc == 0
    kept = _read_jsonl(p)
    assert [r["close_time"] for r in kept] == ["2026-09-23T21:00:00Z"]
