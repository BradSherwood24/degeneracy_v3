"""Policy loader tests: Decimal exactness, the frozen roster shape, canonical-sha stability, and
the fail-closed sha refusal (falsifier S5)."""

from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest

from service.policy import (
    FROZEN_POLICY_SHA256,
    Q1_STRANGLE,
    SUB_DOLLAR_FLIP,
    PolicyShaMismatch,
    canonical_sha256,
    load_policy,
)


def test_default_load_self_verifies():
    p = load_policy()
    assert p.sha256 == FROZEN_POLICY_SHA256
    assert p.sub_dollar_C_max == Decimal("1.00")
    assert p.q1_strangle_ev_min == Decimal("0.05")
    assert p.freshness_max_leg_age_s == 1.0
    assert p.staleness_s == 60


def test_roster_shape():
    p = load_policy()
    assert p.sources_for_quintile(0) == (Q1_STRANGLE, SUB_DOLLAR_FLIP)
    for q in (1, 2, 3, 4):
        assert p.sources_for_quintile(q) == (SUB_DOLLAR_FLIP,)
    # out-of-range quintile -> no sources (fail closed)
    assert p.sources_for_quintile(9) == ()


def test_imbalance_bounds():
    p = load_policy()
    assert p.imbalance.pair_cost_ceiling_sub1 == Decimal("1.0320")
    assert p.imbalance.max_retries_per_side == 5
    assert p.imbalance.no_rebalance_after_s_to_settle == 3
    assert p.imbalance.no_orders_after_s_to_settle == 1
    assert p.no_orders_after_s_to_settle == 1


def _default_path():
    from service.policy import DEFAULT_POLICY_PATH
    return DEFAULT_POLICY_PATH


def test_canonical_sha_is_key_order_and_whitespace_invariant():
    obj = json.load(open(_default_path(), encoding="utf-8"))
    shuffled = dict(reversed(list(obj.items())))
    assert canonical_sha256(obj) == canonical_sha256(shuffled) == FROZEN_POLICY_SHA256


def test_wrong_expected_sha_refuses():
    with pytest.raises(PolicyShaMismatch):
        load_policy(expected_sha="0" * 64)


def test_tampered_file_refuses_but_loads_without_check(tmp_path):
    obj = json.load(open(_default_path(), encoding="utf-8"))
    obj["sub_dollar_C_max"] = "0.99"  # tamper
    p = tmp_path / "tampered.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    # default expected sha (the frozen roster) -> refuse
    with pytest.raises(PolicyShaMismatch):
        load_policy(str(p))
    # expected_sha=None -> loads, and its canonical sha differs from the frozen one
    loaded = load_policy(str(p), expected_sha=None)
    assert loaded.sub_dollar_C_max == Decimal("0.99")
    assert loaded.sha256 != FROZEN_POLICY_SHA256
