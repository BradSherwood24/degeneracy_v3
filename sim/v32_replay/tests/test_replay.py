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
