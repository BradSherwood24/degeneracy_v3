"""The lagging + ideal fill models: gate, promotion, spread-aware maker fill rule."""

from __future__ import annotations

from decimal import Decimal

from sim.v32_replay.models import IdealModel, LaggingModel, maker_fill_decision


def test_maker_fill_decision_regimes():
    o = Decimal("0.45")
    # (i) o < a: we are the best ask alone; fill iff p >= o (including equality)
    assert maker_fill_decision(o, Decimal("0.47"), Decimal("0.45")) == ("i", True)
    assert maker_fill_decision(o, Decimal("0.47"), Decimal("0.44")) == ("i", False)
    assert maker_fill_decision(o, None, Decimal("0.45")) == ("i", True)   # a unknown -> alone
    # (ii) o == a: joined the level; fill iff p > o (strict sweep-through)
    assert maker_fill_decision(o, Decimal("0.45"), Decimal("0.46")) == ("ii", True)
    assert maker_fill_decision(o, Decimal("0.45"), Decimal("0.45")) == ("ii", False)
    # (iii) o > a: our offer above the market ask -> no fill (no-quote)
    assert maker_fill_decision(o, Decimal("0.44"), Decimal("0.50")) == ("iii", False)


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
    # offer = 0.45; market ask 0.47 is ABOVE it -> regime (i): a YES print >= offer fills us
    m.on_trade(1001.0, 100, Decimal("0.50"), Decimal("5"), Decimal("0.90"), Decimal("0.47"))
    assert m.fill is not None
    assert m.fill.n == Decimal("0.55") and m.fill.offer == Decimal("0.45")
    assert m.fill.regime == "i"
    assert m.fill.since_replace_ms is not None and m.fill.since_replace_ms >= 200
    assert m.fill.completion_target_ts == 1001.0 + 1.5


def test_lagging_no_fill_below_offer():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))
    # regime (i) (ask 0.47 > offer 0.45); a print below the offer does NOT fill
    m.on_trade(1001.0, 100, Decimal("0.44"), Decimal("5"), Decimal("0.90"), Decimal("0.47"))
    assert m.fill is None


def test_lagging_regime_iii_no_fill():
    m = LaggingModel(E=Decimal("0.10"), tol=Decimal("0.02"), deb=5000, lat=200)
    m.on_tick(1000.0, 100, 200, Decimal("0.55"))
    m.on_tick(1000.3, 100, 200, Decimal("0.55"))
    # offer 0.45 ABOVE market ask 0.40 -> regime (iii): no fill even on a high print
    m.on_trade(1001.0, 100, Decimal("0.60"), Decimal("5"), Decimal("0.90"), Decimal("0.40"))
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
    # ideal keeps the strict rule (p > offer); regime recorded for info (ask 0.46 > offer 0.45 -> i)
    m.on_trade(1001.0, 100, Decimal("0.50"), Decimal("5"), Decimal("0.90"), Decimal("0.46"))
    assert m.fill is not None
    assert m.fill.completion_target_ts == 1001.0     # no lag
    assert m.fill.regime == "i"
