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


def _standdown_row(close: str, tag: str = "no KXBTC range buckets co-settling") -> dict:
    return {"roster": "DegeneracyV3_3", "close_time": close, "stand_down": True,
            "stand_down_reason": tag, "mode": "dry"}


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
    rows = [
        _standdown_row("2026-09-23T20:00:00Z"),           # a real earlier close
        _standdown_row("2026-09-23T21:00:00Z"),           # 21:00Z dup #1
        _standdown_row("2026-09-23T21:00:00Z"),           # dup #2
        {"close_time": "2026-09-23T21:00:00Z", "stand_down": True, "seq": "LAST"},  # dup #3 (last wins)
        _standdown_row("2026-09-23T22:00:00Z"),           # a later close
    ]
    out, stats = dd.dedupe_rows(rows)
    assert stats == {"read": 5, "unique_close": 3, "removed": 2, "no_close_kept": 0, "written": 3}
    closes = [r["close_time"] for r in out]
    assert closes == ["2026-09-23T20:00:00Z", "2026-09-23T21:00:00Z", "2026-09-23T22:00:00Z"]
    # the 21:00Z slot holds the LAST row's content
    assert out[1]["seq"] == "LAST"


def test_dedupe_idempotent_on_clean_input():
    rows = [_standdown_row("2026-09-23T20:00:00Z"), _standdown_row("2026-09-23T21:00:00Z")]
    out1, s1 = dd.dedupe_rows(rows)
    out2, s2 = dd.dedupe_rows(out1)
    assert s1["removed"] == 0 and s2["removed"] == 0
    assert out1 == out2 == rows


def test_dedupe_keeps_rows_without_close_time():
    rows = [
        {"note": "header, no close_time"},
        _standdown_row("2026-09-23T21:00:00Z"),
        _standdown_row("2026-09-23T21:00:00Z"),
        {"note": "footer, no close_time"},
    ]
    out, stats = dd.dedupe_rows(rows)
    assert stats["no_close_kept"] == 2
    assert stats["removed"] == 1
    assert out[0] == {"note": "header, no close_time"}
    assert out[-1] == {"note": "footer, no close_time"}


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
