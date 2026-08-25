"""Journal: append/index, iter_records, flush + reload round-trip, Decimal serialization."""

from __future__ import annotations

import json
import os
from decimal import Decimal

from service.journal import Journal, load_journal


def test_append_assigns_gapfree_indices() -> None:
    j = Journal()
    assert j.append("kalshi_ws", {"a": 1}, 100.0) == 0
    assert j.append("alarm", {"b": 2}, 101.0) == 1
    assert len(j) == 2
    recs = list(j.iter_records())
    assert [r["idx"] for r in recs] == [0, 1]
    assert recs[0] == {"idx": 0, "kind": "kalshi_ws", "local_ts": 100.0, "obj": {"a": 1}}


def test_flush_writes_jsonl_in_order(tmp_path) -> None:
    j = Journal()
    for i in range(3):
        j.append("kalshi_ws", {"type": "orderbook_delta", "msg": {"i": i}}, float(i))
    path = os.path.join(tmp_path, "sub", "w.jsonl")
    n = j.flush(path)
    assert n == 3
    with open(path) as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert [r["idx"] for r in lines] == [0, 1, 2]
    assert not os.path.exists(path + ".tmp")  # temp cleaned via os.replace


def test_flush_reload_round_trip(tmp_path) -> None:
    j = Journal()
    j.append("kalshi_ws", {"type": "trade", "msg": {"p": 1}}, 5.0)
    j.append("order_intent", {"count": 2}, 6.0)
    path = os.path.join(tmp_path, "w.jsonl")
    j.flush(path)
    j2 = load_journal(path)
    assert [r["obj"] for r in j2.iter_records()] == [
        {"type": "trade", "msg": {"p": 1}},
        {"count": 2},
    ]
    assert [r["idx"] for r in j2.iter_records()] == [0, 1]


def test_decimal_serialized_losslessly(tmp_path) -> None:
    j = Journal()
    j.append("alarm", {"slippage": Decimal("0.02"), "price": Decimal("0.001")}, 1.0)
    path = os.path.join(tmp_path, "w.jsonl")
    j.flush(path)
    with open(path) as f:
        rec = json.loads(f.readline())
    # Decimal rendered as a string (not a float) -> no binary-float drift on round-trip
    assert rec["obj"]["slippage"] == "0.02"
    assert rec["obj"]["price"] == "0.001"


def test_deterministic_serialization_sorted_keys(tmp_path) -> None:
    j = Journal()
    j.append("kalshi_ws", {"z": 1, "a": 2, "m": 3}, 0.0)
    p1 = os.path.join(tmp_path, "a.jsonl")
    p2 = os.path.join(tmp_path, "b.jsonl")
    j.flush(p1)
    j.flush(p2)
    assert open(p1).read() == open(p2).read()  # sort_keys -> byte-identical


def test_records_returns_copy() -> None:
    j = Journal()
    j.append("k", {"x": 1}, 0.0)
    recs = j.records()
    recs.append("garbage")
    assert len(j) == 1  # internal buffer untouched
