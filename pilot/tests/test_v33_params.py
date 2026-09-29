"""V3.3 policy loader tests: the frozen-policy canonical sha pin, fail-closed on drift / missing key,
the ladder-range shadow-E invariant, and agreement of the sha scheme with V3.2 / box.

No network, no disk beyond the shipped policy + a tmp mutated copy. 2026-08-20..29 (holdout) and the
2026-08-02..18 seal are never touched.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service.box import canonical_sha256 as box_canonical_sha256
from service.v32.params import canonical_sha256 as v32_canonical_sha256
from service.v33.params import (
    DEFAULT_V33_PARAMS_PATH,
    FROZEN_V33_PARAMS_SHA256,
    V33ParamsInvalid,
    V33ParamsShaMismatch,
    canonical_sha256,
    load_v33_params,
)


def test_params_load_and_sha_pin():
    p = load_v33_params()
    assert p.sha256 == FROZEN_V33_PARAMS_SHA256
    assert p.E_min == Decimal("0.08")   # L4 (2026-09-29): ladder shift 5..15c -> 8..18c
    assert p.rungs == 11
    assert p.lots_per_rung == 1
    assert p.tol == Decimal("0.01")
    assert p.deb_ms == 5000
    assert p.wing_coalesce_ms == 150
    assert p.refill_in_window is False
    assert p.max_amends_in_flight == 3
    assert p.max_sets_per_hour == 11
    assert p.replace_rate_alarm_per_min == 120
    assert p.n_min == Decimal("0.05")
    assert p.bucket_width == 100
    # L2 R2 additions
    assert p.max_contracts_per_order_hint == 11
    assert p.write_tokens_per_s == 100
    assert p.write_bucket_size == 100
    assert p.order_poll_batched is True
    assert p.shadow_Es == (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))


def test_l4_ladder_shift_8_to_18():
    """L4 (2026-09-29): the shipped ladder rests margins 8..18c. Assert E_min 0.08, K=11, the live margin
    range [0.08, 0.18] = [E_min, E_min+(rungs-1)c], every shadow E inside it, and the SO-3 deep-obs band
    19..28c (E_min_c+rungs .. +rungs+deep_obs_rungs-1)."""
    p = load_v33_params()
    assert p.E_min == Decimal("0.08")
    assert p.rungs == 11
    E_max = p.E_min + (p.rungs - 1) * Decimal("0.01")
    assert (p.E_min, E_max) == (Decimal("0.08"), Decimal("0.18"))       # ladder margins 8..18c
    assert all(p.E_min <= E <= E_max for E in p.shadow_Es)              # shadows track live rungs
    assert p.shadow_Es == (Decimal("0.08"), Decimal("0.10"), Decimal("0.12"))
    # SO-3 deep-observation band follows automatically: margins E_min_c+rungs .. +rungs+deep_obs_rungs-1.
    E_min_c = int(p.E_min * 100)
    assert (E_min_c + p.rungs, E_min_c + p.rungs + p.deep_obs_rungs - 1) == (19, 28)


def test_hint_below_lots_per_rung_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["max_contracts_per_order_hint"] = 0  # below lots_per_rung (1)
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)


def test_write_tokens_below_one_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["write_tokens_per_s"] = 0
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)


def test_sha_pin_matches_file_on_disk():
    # the pinned sha is the canonical sha of the SHIPPED json (a re-freeze must re-pin both together).
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    assert canonical_sha256(raw) == FROZEN_V33_PARAMS_SHA256


def test_params_sha_mismatch_refused(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["deb_ms"] = 4999  # any drift
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsShaMismatch):
        load_v33_params(str(q))


def test_params_missing_key_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    del raw["rungs"]
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    # pass expected_sha=None so we reach the KeyError (not the sha gate).
    with pytest.raises(KeyError):
        load_v33_params(str(q), expected_sha=None)


def test_canonical_sha_matches_v32_and_box():
    # one sha convention across the pilots.
    obj = {"b": 2, "a": 1, "shadow_Es": ["0.08"]}
    assert canonical_sha256(obj) == v32_canonical_sha256(obj) == box_canonical_sha256(obj)


def test_shadow_E_outside_ladder_range_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    # E_min 0.05, rungs 11 -> ladder range [0.05, 0.15]; 0.20 is outside.
    raw["shadow_Es"] = ["0.10", "0.20"]
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)


def test_shadow_E_at_ladder_bottom_edge_ok(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["shadow_Es"] = ["0.08", "0.18"]  # L4: exactly the new ladder endpoints [E_min, E_min+(rungs-1)c] -> valid
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    p = load_v33_params(str(q), expected_sha=None)
    assert p.shadow_Es == (Decimal("0.08"), Decimal("0.18"))


def test_rungs_below_one_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["rungs"] = 0
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)


def test_refill_in_window_true_fails_closed(tmp_path):
    # NIT #7 (reviewer): refill_in_window is ENFORCED — L1 supports only no-refill (Q3); True is L2.
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["refill_in_window"] = True
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)


def test_max_amends_in_flight_below_one_fails_closed(tmp_path):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw["max_amends_in_flight"] = 0
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(str(q), expected_sha=None)
