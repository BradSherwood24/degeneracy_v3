"""Adversarial review probes (SEPARATE opus48 reviewer, Phase 1 spine).

These are regression guards for defects found in review, plus coverage the builder's suite omitted.
Everything here uses fakes / in-memory journals; no network, no proxy, no sealed-day file.
"""

from __future__ import annotations

import os
from decimal import Decimal

from service.book import BookMirror, _to_decimal
from service.journal import Journal, load_journal
from service.replay import replay_books


# === Finding 1: non-finite wire values must fail CLOSED (NaN/Inf) ===


def test_to_decimal_rejects_nonfinite() -> None:
    for bad in ("nan", "inf", "-inf", "Infinity", float("nan"), float("inf")):
        assert _to_decimal(bad) is None, f"non-finite {bad!r} must coerce to None (fail closed)"
    # finite values still parse
    assert _to_decimal("0.44") == Decimal("0.44")
    assert _to_decimal(0.001) == Decimal("0.001")


def test_nan_delta_marks_suspect_not_raises() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [["0.44", "100"]], "no_dollars_fp": []})
    assert b.suspect is False
    b.apply_delta({"side": "yes", "price_dollars": "0.50", "delta_fp": "nan"})  # must NOT raise
    assert b.suspect is True
    assert b.malformed_delta_count == 1
    assert b.yes_bids == {Decimal("0.44"): Decimal("100")}  # untouched


def test_inf_snapshot_level_dropped_no_garbage_top() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [["inf", "5"], ["0.44", "100"]], "no_dollars_fp": [["0.30", "9"]]})
    assert b.yes_bids == {Decimal("0.44"): Decimal("100")}  # inf level dropped
    assert b.best_bid("yes") == b.best_bid("yes")  # finite
    top = b.top_of_book()
    assert top.yes_bid == Decimal("0.44")
    assert top.no_ask == Decimal("0.56")  # 1 - 0.44, finite (no Infinity leak)


def test_nan_delta_in_journal_does_not_crash_replay() -> None:
    """A single poisoned frame must not abort the whole window's golden replay / paired report."""
    j = Journal()
    j.append("kalshi_ws", {"type": "orderbook_snapshot",
                           "msg": {"market_ticker": "MK", "yes_dollars_fp": [["0.44", "100"]], "no_dollars_fp": []}}, 0.0)
    j.append("kalshi_ws", {"type": "orderbook_delta",
                           "msg": {"market_ticker": "MK", "side": "yes", "price_dollars": "0.50", "delta_fp": "nan"}}, 0.0)
    out = list(replay_books(j))  # must not raise
    assert len(out) == 2
    assert out[-1][2].suspect is True  # replay-visible malformed-delta suspect


# === Coverage gap: disk round-trip replay parity (builder tested only in-memory) ===


def test_flush_reload_replay_matches_inmemory_with_float_wire_values(tmp_path) -> None:
    """The paired report reloads the journal from DISK days later. Prove flush -> load -> replay is
    byte-identical to the in-memory replay even when wire numerics arrived as Python floats (the
    journal stores what json.loads produced live; only str()-based Decimal coercion is trusted)."""
    j = Journal()
    j.append("kalshi_ws", {"type": "orderbook_snapshot",
                           "msg": {"market_ticker": "MK", "yes_dollars_fp": [[0.44, 100], [0.001, 5]],
                                   "no_dollars_fp": [[0.53, 50]]}}, 1.0)
    j.append("kalshi_ws", {"type": "orderbook_delta",
                           "msg": {"market_ticker": "MK", "side": "yes", "price_dollars": 0.45, "delta_fp": 20}}, 2.0)
    in_memory = list(replay_books(j))
    path = os.path.join(tmp_path, "rt.jsonl")
    j.flush(path)
    reloaded = list(replay_books(load_journal(path)))
    assert in_memory == reloaded


def test_replay_two_processes_would_match_via_stable_serialization(tmp_path) -> None:
    """Determinism proxy: the journal flushed twice is byte-identical (sort_keys + str Decimals),
    so a replay in another process reads identical bytes and produces the identical sequence."""
    j = Journal()
    j.append("kalshi_ws", {"type": "orderbook_snapshot",
                           "msg": {"market_ticker": "MK", "yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": [[0.53, 50]]}}, 0.0)
    j.append("kalshi_ws", {"type": "orderbook_delta",
                           "msg": {"market_ticker": "MK", "side": "no", "price_dollars": 0.53, "delta_fp": -50}}, 0.0)
    p1 = os.path.join(tmp_path, "a.jsonl")
    p2 = os.path.join(tmp_path, "b.jsonl")
    j.flush(p1)
    j.flush(p2)
    assert open(p1, "rb").read() == open(p2, "rb").read()
    assert list(replay_books(load_journal(p1))) == list(replay_books(load_journal(p2)))
