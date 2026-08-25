"""Golden replay: determinism, reconstruction correctness, multi-market keying, gap-heal."""

from __future__ import annotations

from decimal import Decimal

from service.journal import Journal
from service.replay import replay_books

MK = "KXBTC15M-A"
MK2 = "KXBTCD-B-T100"


def snap(journal, market, yes, no):
    journal.append("kalshi_ws", {"type": "orderbook_snapshot",
                                 "msg": {"market_ticker": market, "yes_dollars_fp": yes, "no_dollars_fp": no}}, 0.0)


def delta(journal, market, side, price, d):
    journal.append("kalshi_ws", {"type": "orderbook_delta",
                                 "msg": {"market_ticker": market, "side": side, "price_dollars": price, "delta_fp": d}}, 0.0)


def test_replay_deterministic_same_journal_twice() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 100]], [[0.53, 50]])
    delta(j, MK, "yes", 0.45, 20)
    delta(j, MK, "no", 0.53, -50)
    a = list(replay_books(j))
    b = list(replay_books(j))
    assert a == b
    assert len(a) == 3  # one yield per book-affecting frame


def test_replay_reconstructs_expected_tops() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 100]], [[0.53, 50]])
    idx0, m0, t0 = list(replay_books(j))[0]
    assert (idx0, m0) == (0, MK)
    assert t0.yes_bid == Decimal("0.44") and t0.yes_ask == Decimal("0.47")
    assert t0.no_bid == Decimal("0.53") and t0.no_ask == Decimal("0.56")
    assert t0.suspect is False


def test_replay_delta_progression() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 100]], [])
    delta(j, MK, "yes", 0.46, 30)  # new higher bid
    seq = list(replay_books(j))
    assert seq[0][2].yes_bid == Decimal("0.44")
    assert seq[1][2].yes_bid == Decimal("0.46")  # top moved up


def test_replay_multi_market_keyed() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 100]], [])
    snap(j, MK2, [[0.10, 5]], [])
    delta(j, MK, "yes", 0.44, -100)  # empties MK yes
    out = list(replay_books(j))
    assert out[0][1] == MK and out[1][1] == MK2
    # MK delta only affects MK's book; MK2 untouched
    assert out[2][1] == MK and out[2][2].yes_bid is None


def test_replay_skips_missing_market_ticker() -> None:
    j = Journal()
    j.append("kalshi_ws", {"type": "orderbook_delta", "msg": {"side": "yes", "price_dollars": 0.4, "delta_fp": 1}}, 0.0)
    snap(j, MK, [[0.44, 100]], [])
    out = list(replay_books(j))
    assert len(out) == 1 and out[0][0] == 1  # the unattributed frame (idx 0) is skipped


def test_replay_gap_resnapshot_heals() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 100]], [[0.53, 50]])
    delta(j, MK, "yes", 0.46, 10)
    # a fresh snapshot (post-reconnect) replaces the book wholesale
    snap(j, MK, [[0.30, 7]], [[0.60, 3]])
    out = list(replay_books(j))
    final = out[-1][2]
    assert final.yes_bid == Decimal("0.30") and final.yes_ask == Decimal("0.40")
    assert final.suspect is False  # snapshot healed it


def test_replay_ignores_non_book_records() -> None:
    j = Journal()
    j.append("window_meta", {"close_time": "x"}, 0.0)
    snap(j, MK, [[0.44, 1]], [])
    j.append("kalshi_ws", {"type": "trade", "msg": {"market_ticker": MK, "taker_side": "yes"}}, 0.0)
    j.append("kalshi_ws", {"type": "ticker", "msg": {"market_ticker": MK, "price_dollars": 0.4}}, 0.0)
    j.append("alarm", {"alarm": "x"}, 0.0)
    out = list(replay_books(j))
    assert len(out) == 1 and out[0][0] == 1  # only the snapshot frame yields


def test_replay_malformed_delta_marks_suspect_in_replay() -> None:
    j = Journal()
    snap(j, MK, [[0.44, 1]], [])
    delta(j, MK, "bogus", 0.44, 1)  # malformed side -> suspect (journaled + replay-visible)
    out = list(replay_books(j))
    assert out[-1][2].suspect is True
