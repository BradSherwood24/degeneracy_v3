"""BookMirror tests — adapted from degeneracy_v2 tests/signals/test_order_book.py.

Adaptations: Decimal representation (prices = Decimal dollars, sizes = Decimal contracts); the pilot
API (best_yes_ask/best_no_ask/depth_at/top_of_book); the suspect flag (fail-closed on malformed
delta / seq-gap resnapshot). The REST/freshest/wsreal families are not ported (see build report),
so their tests are dropped; the WS snapshot/delta arithmetic and crossed-book faithfulness carry
over.
"""

from __future__ import annotations

from decimal import Decimal

from service.book import BookMirror, Level


def D(x):
    return Decimal(str(x))


def test_starts_suspect_until_first_snapshot() -> None:
    b = BookMirror()
    assert b.suspect is True
    assert b.top_of_book().suspect is True
    b.apply_snapshot({"yes_dollars_fp": [["0.44", "100"]], "no_dollars_fp": []})
    assert b.suspect is False


def test_snapshot_drops_zero_count_levels() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100], [0.45, 0]], "no_dollars_fp": [[0.53, 50]]})
    assert b.yes_bids == {D("0.44"): D("100")}
    assert b.no_bids == {D("0.53"): D("50")}
    assert b.best_bid("yes") == Level(D("0.44"), D("100"))
    # YES ask via 1 - opposite (NO) bid; size = NO bid size
    assert b.best_yes_ask() == Level(D("0.47"), D("50"))


def test_delta_updates_and_removes_levels() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": []})
    b.apply_delta({"side": "yes", "price_dollars": 0.44, "delta_fp": -100})
    assert D("0.44") not in b.yes_bids
    b.apply_delta({"side": "yes", "price_dollars": 0.45, "delta_fp": 30})
    assert b.yes_bids[D("0.45")] == D("30")


def test_decimal_delta_arithmetic_is_exact_no_residue() -> None:
    """Decimal makes a level that returns to exactly 0 disappear cleanly — V2's float-residue class
    of bug cannot arise (0.1 + 0.2 - 0.3 == 0 exactly in Decimal-from-str)."""
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [["0.44", "0.1"]], "no_dollars_fp": []})
    b.apply_delta({"side": "yes", "price_dollars": "0.44", "delta_fp": "0.2"})
    b.apply_delta({"side": "yes", "price_dollars": "0.44", "delta_fp": "-0.3"})
    assert D("0.44") not in b.yes_bids  # exactly zero -> popped, no phantom residue


def test_deci_cent_prices_preserved() -> None:
    """The 15-minute tapered ladder trades in deci-cents (0.001 dollars); Decimal keeps it exact."""
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [["0.001", "5"]], "no_dollars_fp": [["0.002", "9"]]})
    assert b.best_bid("yes") == Level(D("0.001"), D("5"))
    assert b.best_yes_ask() == Level(D("0.998"), D("9"))  # 1 - 0.002


def test_malformed_delta_marks_suspect() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": []})
    assert b.suspect is False
    b.apply_delta({"side": "bogus", "price_dollars": 0.44, "delta_fp": 5})  # bad side
    assert b.suspect is True
    assert b.malformed_delta_count == 1
    # the good level is untouched (bad delta did not corrupt state, only flagged it)
    assert b.yes_bids == {D("0.44"): D("100")}


def test_malformed_price_or_delta_marks_suspect() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [], "no_dollars_fp": []})
    b.apply_delta({"side": "yes", "price_dollars": "nan-ish?", "delta_fp": 5})
    assert b.suspect is True


def test_snapshot_heals_suspect() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [], "no_dollars_fp": []})
    b.apply_delta({"side": "bogus"})
    assert b.suspect is True
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 1]], "no_dollars_fp": []})
    assert b.suspect is False


def test_mark_suspect_does_not_clear_levels() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 10]], "no_dollars_fp": []})
    b.mark_suspect()
    assert b.suspect is True
    assert b.yes_bids == {D("0.44"): D("10")}  # kept; a fresh snapshot will replace wholesale


def test_depth_at() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100], [0.42, 30]], "no_dollars_fp": []})
    assert b.depth_at("yes", D("0.44")) == D("100")
    assert b.depth_at("yes", 0.42) == D("30")  # numeric coerced
    assert b.depth_at("yes", D("0.99")) is None
    assert b.depth_at("bogus", D("0.44")) is None


def test_best_bid_counts_only_positive() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": []})
    b.apply_delta({"side": "yes", "price_dollars": 0.46, "delta_fp": 0})  # net zero -> no level
    assert b.best_bid("yes") == Level(D("0.44"), D("100"))


def test_best_ask_none_when_opposite_empty() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": []})
    assert b.best_no_ask() == Level(D("0.56"), D("100"))  # 1 - 0.44
    assert b.best_yes_ask() is None  # no NO bids -> no YES ask


def test_top_of_book_full() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100]], "no_dollars_fp": [[0.53, 50]]})
    top = b.top_of_book()
    assert top.yes_bid == D("0.44") and top.yes_bid_size == D("100")
    assert top.no_bid == D("0.53") and top.no_bid_size == D("50")
    assert top.yes_ask == D("0.47") and top.yes_ask_size == D("50")
    assert top.no_ask == D("0.56") and top.no_ask_size == D("100")
    assert top.suspect is False


def test_crossed_book_reported_not_hidden() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.48, 10]], "no_dollars_fp": [[0.60, 10]]})
    assert b.best_bid("yes") == Level(D("0.48"), D("10"))
    ask = b.best_yes_ask()
    assert ask.price == D("0.40")  # 1 - 0.60 -> crossed
    assert ask.price < b.best_bid("yes").price


def test_snapshot_ignores_malformed_pairs() -> None:
    b = BookMirror()
    b.apply_snapshot({"yes_dollars_fp": [[0.44, 100], ["bad"], [0.42], [None, 5]], "no_dollars_fp": None})
    assert b.yes_bids == {D("0.44"): D("100")}
    assert b.no_bids == {}
