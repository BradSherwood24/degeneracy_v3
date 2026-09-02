"""Tests for the gz-transparent journal helpers and the :40-wake rotation (service/journal_io.py),
plus a box_report aggregate-equality check proving a gzipped journal reads identically to raw.

No network, no sealed reads, tmp dirs only — never touches pilot/journals.
"""

from __future__ import annotations

import gzip
import json
import os
import time

from service import box_report as br
from service.journal_io import (
    journal_paths,
    open_journal,
    read_keep_list,
    rotate_closed_journals,
)

# A timestamp comfortably in the future so every file under test counts as "old" (mtime age > 30m)
# regardless of when the tmp files were written, for the rotation-eligibility tests.
_FUTURE = time.time() + 10 * 24 * 3600


def _write_lines(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln)
            f.write("\n")


def _write_gz_lines(path: str, lines: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln)
            f.write("\n")


# ---------------------------------------------------------------------------
# journal_paths: dedup / prefer-raw / summary exclusion
# ---------------------------------------------------------------------------
def test_journal_paths_dedup_prefer_raw_and_summary_exclusion(tmp_path):
    d = str(tmp_path)
    # window A: only raw; window B: only gz; window C: BOTH (raw must win); + summaries excluded.
    _write_lines(os.path.join(d, "20260826T040000Z.jsonl"), ["{}"])
    _write_gz_lines(os.path.join(d, "20260826T050000Z.jsonl.gz"), ["{}"])
    _write_lines(os.path.join(d, "20260826T060000Z.jsonl"), ["{}"])
    _write_gz_lines(os.path.join(d, "20260826T060000Z.jsonl.gz"), ["{}"])
    _write_lines(os.path.join(d, "summary.jsonl"), ["{}"])
    _write_gz_lines(os.path.join(d, "summary.jsonl.gz"), ["{}"])

    got = [os.path.basename(p) for p in journal_paths(d)]
    assert got == [
        "20260826T040000Z.jsonl",
        "20260826T050000Z.jsonl.gz",
        "20260826T060000Z.jsonl",  # raw preferred over the co-existing .gz
    ]
    # sorted chronologically by fixed-width stem
    assert got == sorted(got, key=lambda b: b[:16])


def test_journal_paths_empty_dir(tmp_path):
    assert journal_paths(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# open_journal: reads gz and raw identically
# ---------------------------------------------------------------------------
def test_open_journal_reads_gz_and_raw(tmp_path):
    lines = ['{"idx": 0, "kind": "box_fire", "obj": {"a": 1}}', '{"idx": 1, "kind": "x", "obj": {}}']
    raw = os.path.join(str(tmp_path), "r.jsonl")
    gz = os.path.join(str(tmp_path), "g.jsonl.gz")
    _write_lines(raw, lines)
    _write_gz_lines(gz, lines)
    with open_journal(raw) as f:
        raw_read = [ln.rstrip("\n") for ln in f]
    with open_journal(gz) as f:
        gz_read = [ln.rstrip("\n") for ln in f]
    assert raw_read == gz_read == lines


# ---------------------------------------------------------------------------
# box_report over (one raw + one gz) == both-raw
# ---------------------------------------------------------------------------
_HOURLY = "KXBTCD-26AUG2605-T76499.99"
_M15 = "KXBTC15M-26AUG260500-00"


def _selection():
    return {
        "hourly_ticker": _HOURLY, "hourly_side": "no", "hourly_ask": "0.93",
        "hourly_bid": "0.90", "hourly_mid": "0.915", "hourly_limit": "0.96", "hourly_ask_size": 10,
        "m15_ticker": _M15, "m15_side": "yes", "m15_ask": "0.90", "m15_bid": "0.88",
        "m15_limit": "0.93", "m15_ask_size": 8,
        "strike_K": "76500", "anchor_A": "76400", "C": "1.86", "C_mid": "1.88", "implied_pin": "0.88",
    }


def _fire_records(close_time):
    return [
        {"idx": 0, "kind": "window_start", "local_ts": 1.0, "obj": {"close_time": close_time, "pairs": 1}},
        {"idx": 1, "kind": "box_fire", "local_ts": 2.0,
         "obj": {"kind": "FIRE", "source": br.WIDE_BOX, "count": 1, "C": "1.86",
                 "t_minus_s": 300.0, "reason": None, "legs": [], "selection": _selection()}},
    ]


def _dump(records: list[dict]) -> list[str]:
    # Match Journal.flush: deterministic sort_keys so the fast keep-kinds token scan behaves identically.
    return [json.dumps(r, sort_keys=True) for r in records]


def _ledger_row(close_time):
    return {
        "close_time": close_time, "strategy": "box", "mode": "armed", "fires": 1,
        "fired_source": br.WIDE_BOX, "filled": True, "box_one_legged": False,
        "box_flatten_filled": None,
        "fills": [
            {"ticker": _HOURLY, "side": "no", "count": 1, "avg_price": "0.93", "avg_fee": "0.01"},
            {"ticker": _M15, "side": "yes", "count": 1, "avg_price": "0.90", "avg_fee": "0.01"},
        ],
        "realized_delta": "-0.86", "realized_unsettled": True, "floor_booked": "1.00",
        "unsettled_legs": [], "slippage_abs_per_side": [], "s1_violation": False,
        "alarms": [], "stops": [],
        "policy_sha": br.CURRENT_BOX_POLICY_SHA256, "roster": br.CURRENT_ROSTER,
    }


def test_box_report_gz_journal_matches_all_raw(tmp_path):
    ca = "2026-08-26T05:00:00Z"
    cb = "2026-08-26T06:00:00Z"
    lines_a = _dump(_fire_records(ca))
    lines_b = _dump(_fire_records(cb))
    ledger = [_ledger_row(ca), _ledger_row(cb)]

    # dir 1: both raw
    d_raw = tmp_path / "raw"
    d_raw.mkdir()
    _write_lines(str(d_raw / "20260826T050000Z.jsonl"), lines_a)
    _write_lines(str(d_raw / "20260826T060000Z.jsonl"), lines_b)

    # dir 2: window A raw, window B gzipped
    d_mixed = tmp_path / "mixed"
    d_mixed.mkdir()
    _write_lines(str(d_mixed / "20260826T050000Z.jsonl"), lines_a)
    _write_gz_lines(str(d_mixed / "20260826T060000Z.jsonl.gz"), lines_b)

    rep_raw = br.build_report(br.load_journals(str(d_raw)), ledger, {})
    rep_mixed = br.build_report(br.load_journals(str(d_mixed)), ledger, {})
    assert rep_mixed == rep_raw
    # sanity: the fixture actually produced fired windows (equality isn't trivially empty)
    total_fires = sum(len(day["fires"]) for day in rep_raw["days"].values())
    assert total_fires == 2


# ---------------------------------------------------------------------------
# read_keep_list
# ---------------------------------------------------------------------------
def test_read_keep_list_missing_and_comments(tmp_path):
    assert read_keep_list(str(tmp_path / "nope.txt")) == set()
    p = tmp_path / "journal_keep.txt"
    p.write_text("# a comment\n\n20260826T050000Z.jsonl\n  20260826T060000Z.jsonl  \n", encoding="utf-8")
    assert read_keep_list(str(p)) == {"20260826T050000Z.jsonl", "20260826T060000Z.jsonl"}


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------
def test_rotation_gzips_old_removes_raw_reads_back(tmp_path):
    d = str(tmp_path)
    lines = ['{"idx": 0, "kind": "box_fire", "obj": {"a": 1}}', '{"idx": 1, "kind": "x", "obj": {"b": 2}}']
    old = os.path.join(d, "20260826T040000Z.jsonl")
    _write_lines(old, lines)

    summary = rotate_closed_journals(d, exclude_basenames=set(), now=_FUTURE)

    assert summary["rotated"] == ["20260826T040000Z.jsonl"]
    assert summary["errors"] == []
    assert not os.path.exists(old)                        # raw deleted
    gz = old + ".gz"
    assert os.path.exists(gz)                             # gz written
    assert not os.path.exists(old + ".gz.tmp")            # temp cleaned
    with open_journal(gz) as f:                           # gz reads back to the same lines
        assert [ln.rstrip("\n") for ln in f] == lines


def test_rotation_excludes_current_summary_keeplist_and_young(tmp_path):
    d = str(tmp_path)
    current = "20260826T060000Z.jsonl"
    kept_name = "20260826T030000Z.jsonl"
    for name in (
        "20260826T040000Z.jsonl",  # eligible -> rotated
        current,                   # current window -> excluded by name
        "summary.jsonl",           # summary -> always excluded
        kept_name,                 # keep-list -> excluded
    ):
        _write_lines(os.path.join(d, name), ["{}"])

    keep_path = os.path.join(d, "journal_keep.txt")
    _write_lines(keep_path, [kept_name])

    summary = rotate_closed_journals(
        d, exclude_basenames={current}, keep_path=keep_path, now=_FUTURE,
    )

    assert summary["rotated"] == ["20260826T040000Z.jsonl"]
    assert os.path.exists(os.path.join(d, "20260826T040000Z.jsonl.gz"))
    # everything excluded stays raw and untouched
    assert os.path.exists(os.path.join(d, current))
    assert os.path.exists(os.path.join(d, "summary.jsonl"))
    assert os.path.exists(os.path.join(d, kept_name))
    assert not os.path.exists(os.path.join(d, kept_name + ".gz"))

    # A fresh file (young mtime) must be skipped under the DEFAULT now (age < 30 min).
    young = os.path.join(d, "20260826T070000Z.jsonl")
    _write_lines(young, ["{}"])
    fresh = rotate_closed_journals(d, exclude_basenames={current}, keep_path=keep_path)
    assert "20260826T070000Z.jsonl" not in fresh["rotated"]
    assert os.path.exists(young)
    assert not os.path.exists(young + ".gz")


def test_rotation_bad_file_logs_and_does_not_raise(tmp_path):
    d = str(tmp_path)
    good = os.path.join(d, "20260826T040000Z.jsonl")
    _write_lines(good, ['{"idx": 0, "kind": "box_fire", "obj": {}}'])
    # An unreadable "journal": a directory whose name matches *.jsonl. Opening it for read raises
    # (PermissionError on Windows / IsADirectoryError on POSIX) inside the crash-safe gzip step.
    bad = os.path.join(d, "20260826T050000Z.jsonl")
    os.mkdir(bad)

    # Must NOT raise despite the bad entry.
    summary = rotate_closed_journals(d, exclude_basenames=set(), now=_FUTURE)

    assert "20260826T040000Z.jsonl" in summary["rotated"]      # the good one still rotated
    assert os.path.exists(good + ".gz")
    assert not os.path.exists(good)
    err_files = {e["file"] for e in summary["errors"]}
    assert "20260826T050000Z.jsonl" in err_files              # the bad one recorded, not raised
    assert os.path.isdir(bad)                                  # left intact
    assert not os.path.exists(bad + ".gz")                    # no partial gz for it
    assert not os.path.exists(bad + ".gz.tmp")                # no orphaned temp


def test_rotation_is_idempotent(tmp_path):
    d = str(tmp_path)
    _write_lines(os.path.join(d, "20260826T040000Z.jsonl"), ["{}"])
    first = rotate_closed_journals(d, exclude_basenames=set(), now=_FUTURE)
    assert first["count"] == 1
    second = rotate_closed_journals(d, exclude_basenames=set(), now=_FUTURE)
    assert second["count"] == 0            # nothing left to compress (glob *.jsonl skips *.jsonl.gz)
    assert second["rotated"] == []
