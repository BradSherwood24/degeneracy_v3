"""Tests for pilot_ledger: append/load, S4 total, promotion counters + P1/P2 gates (boundaries)."""

from __future__ import annotations

import json
import os
from decimal import Decimal

from service.pilot_ledger import (
    append_entry,
    compute_counters,
    entries_for_day,
    load_entries,
    p1_gate,
    p2_gate,
    s4_running_loss,
)


def _entry(
    *,
    close_time="2026-08-22T14:00:00Z",
    pairs=1,
    fires=1,
    filled=True,
    imbalance_unresolved=False,
    s1_violation=False,
    sub1_entry=True,
    slippage=("0.005",),
    realized_delta="0.03",
    second_pair_book_walk=None,
):
    return {
        "close_time": close_time,
        "pairs": pairs,
        "fires": fires,
        "filled": filled,
        "imbalance_unresolved": imbalance_unresolved,
        "s1_violation": s1_violation,
        "sub1_entry": sub1_entry,
        "slippage_abs_per_side": list(slippage),
        "realized_delta": realized_delta,
        "second_pair_book_walk": second_pair_book_walk,
    }


def test_append_and_load_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "pilot_ledger.jsonl")
    append_entry({"close_time": "2026-08-22T14:00:00Z", "realized_delta": "0.03"}, path)
    append_entry({"close_time": "2026-08-22T15:00:00Z", "realized_delta": "-0.02"}, path)
    rows = load_entries(path)
    assert len(rows) == 2
    assert rows[0]["close_time"] == "2026-08-22T14:00:00Z"


def test_load_missing_file_is_empty(tmp_path):
    assert load_entries(os.path.join(tmp_path, "nope.jsonl")) == []


def test_load_entries_tolerates_truncated_trailing_line(tmp_path):
    # a crash mid-append leaves a truncated FINAL line; the good prefix must still load (so the
    # operator CLI and the S4/A4 day-lock seed are not broken by one bad write).
    path = os.path.join(tmp_path, "pilot_ledger.jsonl")
    append_entry({"close_time": "2026-08-22T14:00:00Z", "realized_delta": "-1.00"}, path)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"close_time": "2026-08-22T15:00:00Z", "realized_de')  # truncated, no newline
    rows = load_entries(path)
    assert len(rows) == 1
    assert rows[0]["realized_delta"] == "-1.00"


def test_load_entries_raises_on_midfile_corruption(tmp_path):
    # corruption that is NOT the trailing line is real damage -> surfaced loudly, never silently skipped.
    import pytest
    path = os.path.join(tmp_path, "pilot_ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"close_time": "a"}\n')
        f.write('{ this is not json }\n')
        f.write('{"close_time": "b"}\n')
    with pytest.raises(json.JSONDecodeError):
        load_entries(path)


def test_s4_running_loss_sums_realized():
    entries = [_entry(realized_delta="0.05"), _entry(realized_delta="-0.20"), _entry(realized_delta="0.10")]
    assert s4_running_loss(entries) == Decimal("-0.05")


def test_entries_for_day_filters_by_utc_date():
    a = _entry(close_time="2026-08-22T14:00:00Z")
    b = _entry(close_time="2026-08-23T14:00:00Z")
    assert entries_for_day([a, b], "2026-08-22") == [a]


def test_compute_counters_folds_raw_fields():
    entries = [
        _entry(fires=1, filled=True, slippage=("0.004",), sub1_entry=True,
               close_time="2026-08-22T14:00:00Z"),
        _entry(fires=1, filled=False, slippage=(), sub1_entry=False,
               close_time="2026-08-23T15:00:00Z"),
        _entry(fires=0),  # a non-fired window contributes nothing
    ]
    c = compute_counters(entries)
    assert c.fired_count == 2
    assert c.filled_count == 1
    assert c.fill_rate == Decimal("0.5")
    assert c.sub1_entries == 1
    assert c.mean_abs_slippage == Decimal("0.004")
    assert c.calendar_days == 2


def _p1_pass_set():
    # 10 fired 1-pair windows, all filled, tiny slippage, one calendar day.
    return [
        _entry(pairs=1, fires=1, filled=True, slippage=("0.005",),
               close_time=f"2026-08-22T{h:02d}:00:00Z")
        for h in range(10)
    ]


def test_p1_gate_passes_at_thresholds():
    result = p1_gate(_p1_pass_set())
    assert result.passed, result.reasons
    assert result.counters.fired_count == 10


def test_p1_gate_fill_rate_boundary():
    # exactly 6/10 filled -> 0.60 -> passes; 5/10 -> 0.50 -> fails.
    six = [_entry(pairs=1, fires=1, filled=(i < 6), slippage=("0.005",),
                  close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(10)]
    assert p1_gate(six).passed
    five = [_entry(pairs=1, fires=1, filled=(i < 5), slippage=("0.005",),
                   close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(10)]
    r = p1_gate(five)
    assert not r.passed
    assert any("fill_rate" in x for x in r.reasons)


def test_p1_gate_insufficient_fired():
    nine = [_entry(pairs=1, fires=1, filled=True, slippage=("0.005",),
                   close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(9)]
    r = p1_gate(nine)
    assert not r.passed
    assert any("fired" in x for x in r.reasons)


def test_p1_gate_slippage_boundary():
    # mean |slip| exactly 0.01 passes; 0.011 fails.
    ok = [_entry(pairs=1, fires=1, filled=True, slippage=("0.010",),
                 close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(10)]
    assert p1_gate(ok).passed
    bad = [_entry(pairs=1, fires=1, filled=True, slippage=("0.011",),
                  close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(10)]
    r = p1_gate(bad)
    assert not r.passed
    assert any("slippage" in x for x in r.reasons)


def test_p1_gate_imbalance_and_s1_fail():
    base = _p1_pass_set()
    base[0]["imbalance_unresolved"] = True
    r = p1_gate(base)
    assert not r.passed
    assert any("imbalance" in x for x in r.reasons)

    base2 = _p1_pass_set()
    base2[0]["s1_violation"] = True
    r2 = p1_gate(base2)
    assert not r2.passed
    assert any("S1" in x for x in r2.reasons)


def test_p2_gate_requires_two_pair_evidence_and_p1_holding():
    entries = list(_p1_pass_set())  # P1 holds on the 1-pair rung
    # 10 two-pair fired windows, >= 5 sub-$1, book-walk measured on at least one, all filled.
    for i in range(10):
        entries.append(_entry(
            pairs=2, fires=1, filled=True, slippage=("0.004",),
            sub1_entry=(i < 6), close_time=f"2026-08-23T{i:02d}:00:00Z",
            second_pair_book_walk={"measured": "avg_vs_ask_proxy", "legs": []},
        ))
    r = p2_gate(entries)
    assert r.passed, r.reasons


def test_p2_gate_fails_without_book_walk():
    entries = list(_p1_pass_set())
    for i in range(10):
        entries.append(_entry(pairs=2, fires=1, filled=True, slippage=("0.004",),
                              sub1_entry=(i < 6), close_time=f"2026-08-23T{i:02d}:00:00Z",
                              second_pair_book_walk=None))
    r = p2_gate(entries)
    assert not r.passed
    assert any("book-walk" in x for x in r.reasons)


def test_p2_gate_fails_when_p1_broken():
    # P1 rung has only 9 fired -> P1 fails -> P2 must fail too even with good 2-pair evidence.
    entries = [_entry(pairs=1, fires=1, filled=True, slippage=("0.005",),
                      close_time=f"2026-08-22T{i:02d}:00:00Z") for i in range(9)]
    for i in range(10):
        entries.append(_entry(pairs=2, fires=1, filled=True, slippage=("0.004",),
                              sub1_entry=(i < 6), close_time=f"2026-08-23T{i:02d}:00:00Z",
                              second_pair_book_walk={"measured": "x", "legs": []}))
    r = p2_gate(entries)
    assert not r.passed
    assert any("P1 conditions" in x for x in r.reasons)
