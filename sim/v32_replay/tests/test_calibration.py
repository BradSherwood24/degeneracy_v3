"""Calibration aggregation + forward-apply helpers (no heavy IO)."""

from __future__ import annotations

from conftest import HEAD_FIXTURE
from sim.v32_replay.calibration import aggregate
from sim.v32_replay.forward import _at, _lock_stats, _strike_of
from sim.v32_replay.frames import iter_frames, read_window_meta
from sim.v32_replay.replay import WindowEngine


def _params():
    from service.v32.params import load_v32_params
    return load_v32_params()


def test_aggregate_on_head_fixture():
    params = _params()
    meta = read_window_meta(HEAD_FIXTURE)
    eng = WindowEngine(meta, params, width=params.bucket_width)
    r = eng.run(iter_frames(HEAD_FIXTURE))
    cal = aggregate([r])
    assert cal["n_windows"] == 1
    # the head is ~10 s; the single spot-bucket yes trade is measured
    assert cal["n_trades"] == r.calib["n_trades"]
    for key in ("spot_agreement", "cap_error_c", "regime_frac", "p_fill_maker",
                "p_fill_strict", "fill_factor", "B_resid_c", "replaces_per_window_mean"):
        assert key in cal
    if cal["n_eval"]:
        assert abs(sum(v for v in cal["regime_frac"].values() if v is not None) - 1.0) < 1e-9
    assert cal["sim_replaces_per_hour_reference"] == 77


def test_aggregate_empty():
    cal = aggregate([])
    assert cal["n_windows"] == 0
    assert cal["n_trades"] == 0
    assert cal["spot_agreement"] is None


def test_forward_helpers():
    assert _strike_of("KXBTCD-26AUG3006-T77799.99") == 77800
    assert _strike_of("garbage") is None
    rows = [[1000, 0.4, 0.42], [2000, 0.41, 0.43], [3000, 0.44, 0.46]]
    times = [r[0] for r in rows]
    assert _at(rows, times, 2500) == [2000, 0.41, 0.43]   # last <= 2500
    assert _at(rows, times, 500) is None                  # before first
    stats = _lock_stats([10.0, 8.0, 6.0, 12.0], n_hours=48, thin=0.5)
    assert stats["n_fills"] == 4
    assert stats["fills_per_day"] == 4 / (48 / 24) * 0.5   # thinned rate
    assert stats["mean_c"] == 9.0
