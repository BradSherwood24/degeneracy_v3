"""The lagging + ideal fill models: gate, promotion, fill rule, book-swept flag."""

from __future__ import annotations

from decimal import Decimal

from sim.v32_replay.models import IdealModel, LaggingModel


def test_lagging_places_promotes_and_fills():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    # first tick: forced place -> pending, live at +200 ms
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    assert m.pending == Decimal("0.55") and m.n_rest is None
    # a tick before live_at does NOT promote and (pending in flight) does not re-quote
    m.on_tick(1000.1, 100, 200, Decimal("0.55"))
    assert m.n_rest is None
    # a tick at/after live_at promotes
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))
    assert m.n_rest == Decimal("0.55")
    # a YES print strictly above the offer (1-n = 0.45) fills; book was swept (ask <= offer)
    m.on_trade(1001.0, 100, Decimal("0.50"), Decimal("5"), Decimal("0.90"), Decimal("0.44"))
    assert m.fill is not None
    assert m.fill.n == Decimal("0.55") and m.fill.offer == Decimal("0.45")
    assert m.fill.book_swept is True
    assert m.fill.completion_target_ts == 1001.0 + 1.5


def test_lagging_no_fill_below_offer():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))
    # print exactly at offer (0.45) is NOT strictly above -> no fill
    m.on_trade(1001.0, 100, Decimal("0.45"), Decimal("5"), Decimal("0.90"), Decimal("0.44"))
    assert m.fill is None


def test_lagging_debounce_blocks_replace():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))          # promote
    assert m.n_rest == Decimal("0.55")
    # desired moves by >= tol but only 1 s elapsed (< 5 s DEB) -> no replace
    m.on_tick(1001.3, 100, 200, Decimal("0.50"))
    assert m.n_rest == Decimal("0.55") and m.pending is None
    # after DEB elapses, the replace is emitted
    m.on_tick(1006.0, 100, 200, Decimal("0.50"))
    assert m.pending == Decimal("0.50")
    assert m.replaces == 2


def test_bucket_change_forces_replace():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))
    assert m.n_rest == Decimal("0.55")
    # spot bucket changes -> forced fresh place regardless of tol/deb
    m.on_tick(1000.5, 300, 400, Decimal("0.53"))
    assert m.pending == Decimal("0.53") and m.prev_s == 300


def test_ideal_completes_at_trade_tick():
    m = IdealModel(E=Decimal("0.10"))
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_trade(1001.0, 100, Decimal("0.50"), Decimal("5"), Decimal("0.90"), Decimal("0.46"))
    assert m.fill is not None
    assert m.fill.completion_target_ts == 1001.0     # no lag
    assert m.fill.book_swept is False                # ask 0.46 > offer 0.45
