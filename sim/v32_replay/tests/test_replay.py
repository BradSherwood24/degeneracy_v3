"""End-to-end replay on the real head fixture: deterministic metrics + comparison, and the CLI."""

from __future__ import annotations

import json
import os

from conftest import FIXTURE_DIR, HEAD_FIXTURE
from sim.v32_replay.estimates import build_estimates, summarize
from sim.v32_replay.frames import iter_frames, read_window_meta
from sim.v32_replay.replay import WindowEngine


def _params():
    from service.v32.params import load_v32_params
    return load_v32_params()


def _run():
    params = _params()
    meta = read_window_meta(HEAD_FIXTURE)
    eng = WindowEngine(meta, params, width=params.bucket_width)
    return eng.run(iter_frames(HEAD_FIXTURE))


def test_replay_head_metrics():
    r = _run()
    assert r.close_time == "2026-09-14T17:00:00Z"
    assert r.total_frames == 9773
    assert r.m15_frames + r.strike_frames + r.bucket_frames <= r.total_frames
    m = r.metrics
    assert m["modal_spot_Sd"] == 78600
    assert m["n_bucket_trades"] == 3
    assert m["n_spot_bucket_yes_trades"] == 1
    assert 0.0 <= m["m15_frame_share"] <= 1.0
    # the head is ~10 s of tape at window open: the pump never crosses the offer -> no fills anywhere
    assert r.base_fill is None
    assert r.ideal_fills == []
    # comparison keys present
    for k in ("spot_differ_frac", "cap_diff_cents_median", "wing_drift_cents_median"):
        assert k in r.comparison


def test_replay_is_deterministic():
    a = _run()
    b = _run()
    assert a.metrics == b.metrics
    assert a.comparison == b.comparison
    assert a.total_frames == b.total_frames


def test_estimates_shapes():
    est = build_estimates([_run()])
    assert est["n_windows"] == 1
    for key in ("optimistic", "base", "pessimistic"):
        e = est[key]
        assert e["n_fills"] == 0
        assert e["windows_note"]  # a non-empty explanation

    # a synthetic locks vector exercises the stats path
    e = summarize("X", [5.0, 9.0, 7.0, 3.0], n_windows=4)
    assert e.n_fills == 4
    assert e.pct_positive == 1.0
    assert e.mean_c == 6.0
    assert e.windows_for_2c_band is not None


def test_pessimistic_drops_sub_1_lot_prints_only():
    """PESSIMISTIC drops through-prints below PESSIMISTIC_MIN_PRINT_LOTS (= 1 lot), and keeps 1-lot
    prints — pins the retired 'book-swept / 2-lot' rule to the ruled 1-lot floor."""
    from decimal import Decimal

    from sim.v32_replay.estimates import PESSIMISTIC_MIN_PRINT_LOTS, build_estimates
    from sim.v32_replay.models import BASE_CELL, Fill
    from sim.v32_replay.replay import WindowResult

    assert PESSIMISTIC_MIN_PRINT_LOTS == 1

    def _win(print_size):
        f = Fill(
            model="lag", E=BASE_CELL[0], tol=BASE_CELL[1], deb=BASE_CELL[2],
            spot_Sd=79000, spot_Su=79100, n=Decimal("0.45"), offer=Decimal("0.55"),
            print_price=Decimal("0.60"), print_size=Decimal(str(print_size)),
            trade_ts=0.0, completion_target_ts=0.0, since_replace_ms=5000.0,
            W_completion=Decimal("1.30"), lock=Decimal("0.10"), complete=True,
        )
        r = WindowResult(close_time="2026-09-14T17:00:00Z", resolved_mode="dry",
                         effective_mode="dry", params_sha=None, armed=False)
        r.base_fill = f
        r.lag_replaces = {BASE_CELL: 1}
        return r

    kept = build_estimates([_win(1)])          # exactly 1 lot -> kept
    assert kept["pessimistic"]["n_fills"] == 1
    assert kept["pessimistic_dropped_small_prints"] == 0

    dropped = build_estimates([_win(0.5)])     # below 1 lot -> dropped
    assert dropped["pessimistic"]["n_fills"] == 0
    assert dropped["pessimistic_dropped_small_prints"] == 1
    # BASE keeps the sub-lot fill (only PESSIMISTIC applies the size floor)
    assert dropped["base"]["n_fills"] == 1


def test_cli_writes_outputs(tmp_path):
    from sim.v32_replay import lab
    out = os.path.join(tmp_path, "out")
    rc = lab.main(["--journals", FIXTURE_DIR, "--out", out])
    assert rc == 0
    report = os.path.join(out, "report_20260914.md")
    assert os.path.exists(report)
    assert os.path.exists(os.path.join(out, "per_window.jsonl"))
    assert os.path.exists(os.path.join(out, "fills.jsonl"))
    text = open(report, encoding="utf-8").read()
    assert "V3.2 Replay Lab" in text
    assert "Estimates" in text
    # per_window row parses
    with open(os.path.join(out, "per_window.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(l) for l in f]
    assert rows and rows[0]["close_time"] == "2026-09-14T17:00:00Z"
