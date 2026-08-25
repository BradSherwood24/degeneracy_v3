"""Loader: day-assignment boundary (A2.7), dedupe byte-identity, THE SEAL."""
import json
import os

import pytest

import loader
from loader import (IntegrityError, SealError, SEALED_DATES, assign_day,
                    load_markets)


def test_day_assignment_midnight_belongs_to_prior_day():
    # close exactly at 00:00:00 UTC -> prior day
    assert assign_day("2026-07-16T00:00:00Z") == "2026-07-15"
    # one second later -> that day
    assert assign_day("2026-07-15T00:00:01Z") == "2026-07-15"
    # mid-day -> that day
    assert assign_day("2026-07-15T12:00:00Z") == "2026-07-15"
    # the last hourly close of a day (next-midnight) rolls back
    assert assign_day("2026-07-02T00:00:00Z") == "2026-07-01"


def _mkt_line(ticker, close_time, strike):
    return {"ticker": ticker, "close_time": close_time, "floor_strike": strike,
            "strike_type": "greater_or_equal", "expiration_value": "1.0", "result": "no"}


def _write(root, series, kind, date, objs):
    path = os.path.join(root, series, kind, f"{date}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


def test_dedupe_byte_identical_across_files(tmp_path):
    root = str(tmp_path)
    obj = _mkt_line("KXBTC15M-DUP", "2026-07-02T00:00:00Z", 60000.0)
    # identical line appears in two adjacent day-files (a real boundary duplicate)
    _write(root, "15-minute", "markets", "2026-07-01", [obj])
    _write(root, "15-minute", "markets", "2026-07-02", [obj])
    recs, shas = load_markets("15-minute", ["2026-07-01", "2026-07-02"], data_root=root)
    tickers = [r["ticker"] for r in recs]
    assert tickers.count("KXBTC15M-DUP") == 1     # deduped to one
    assert len(shas) == 2                         # both files hashed


def test_dedupe_conflicting_copies_hard_fail(tmp_path):
    root = str(tmp_path)
    a = _mkt_line("KXBTC15M-DUP", "2026-07-02T00:00:00Z", 60000.0)
    b = _mkt_line("KXBTC15M-DUP", "2026-07-02T00:00:00Z", 60001.0)   # differs -> conflict
    _write(root, "15-minute", "markets", "2026-07-01", [a])
    _write(root, "15-minute", "markets", "2026-07-02", [b])
    with pytest.raises(IntegrityError):
        load_markets("15-minute", ["2026-07-01", "2026-07-02"], data_root=root)


def test_seal_refuses_without_acknowledge():
    # A sealed day fires BEFORE any file read (default acknowledge_sealed_read=False).
    for d in SEALED_DATES:
        with pytest.raises(SealError):
            load_markets("15-minute", [d])
        break  # one is enough; the guard is uniform


def test_seal_covers_all_seventeen_days():
    assert len(SEALED_DATES) == 17
    assert SEALED_DATES[0] == "2026-08-02"
    assert SEALED_DATES[-1] == "2026-08-18"


def test_seal_allows_with_acknowledge_flag_when_falsifier_frozen(tmp_path, monkeypatch):
    # A3.7: acknowledge_sealed_read=True passes ONLY when falsifier.md is FROZEN (the
    # loader's own defense-in-depth). Point the loader at a frozen copy.
    frozen = tmp_path / "falsifier_frozen.md"
    frozen.write_text("STATUS: FROZEN\nclauses...", encoding="utf-8")
    monkeypatch.setattr(loader, "FALSIFIER_MD", str(frozen))
    root = str(tmp_path)
    obj = _mkt_line("KXBTC15M-SEALED", "2026-08-05T12:00:00Z", 60000.0)
    _write(root, "15-minute", "markets", "2026-08-05", [obj])
    recs, _ = load_markets("15-minute", ["2026-08-05"], acknowledge_sealed_read=True,
                           data_root=root)
    assert recs[0]["ticker"] == "KXBTC15M-SEALED"


def test_seal_refuses_acknowledge_when_falsifier_not_frozen(tmp_path, monkeypatch):
    # A3.7: even with acknowledge_sealed_read=True, a non-frozen falsifier refuses the read.
    draft = tmp_path / "falsifier_draft.md"
    draft.write_text("STATUS: DRAFT — NOT FROZEN\nclauses...", encoding="utf-8")
    monkeypatch.setattr(loader, "FALSIFIER_MD", str(draft))
    root = str(tmp_path)
    obj = _mkt_line("KXBTC15M-SEALED", "2026-08-05T12:00:00Z", 60000.0)
    _write(root, "15-minute", "markets", "2026-08-05", [obj])
    with pytest.raises(SealError, match="not FROZEN"):
        load_markets("15-minute", ["2026-08-05"], acknowledge_sealed_read=True,
                     data_root=root)
