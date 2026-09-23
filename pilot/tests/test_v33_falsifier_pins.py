"""The V3.3 falsifier document and the [pin] constants MUST agree (registered-specs rule), AND while the
document is a DRAFT the S5 arming gate must refuse to arm on it. No network/proxy/holdout read -- it only
reads two repo files. If either the doc (`ceremony/v33_falsifier.md`) or the constants
(`service.v33.falsifier_pins`, `service.v33.stops`) drift, this test fails."""

from __future__ import annotations

import os
from decimal import Decimal

from service.stops import DayGuard, falsifier_is_frozen
from service.v33.falsifier_pins import (
    FROZEN_V33_PARAMS_SHA256,
    V33_CAPTURE_RATIO_MIN,
    V33_FALSIFIER_CAPTURE_MARGIN_C,
    V33_FALSIFIER_MAX_ONE_LEGGED,
    V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS,
    V33_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V33_FALSIFIER_MIN_N,
    V33_FALSIFIER_MIN_PCT_POSITIVE,
    V33_FALSIFIER_MIN_RUNG_FILLS_FOR_SHORTFALL,
    V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO,
    V33_KILL_MEAN_LOCK_CENTS,
    V33_KILL_MIN_N,
    V33_PROMOTION_MIN_N,
)
from service.v33.params import load_v33_params
from service.v33.stops import (
    V33_MIN_ORDER_BUDGET_AT_ARM,
    V33_S1_LEGGED_LATCH_THRESHOLD,
    V33_S4_DAY_LOSS_CAP_DOLLARS,
    decide_v33_arming,
)

_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "ceremony", "v33_falsifier.md")


def _doc() -> str:
    with open(_DOC, "r", encoding="utf-8") as f:
        return f.read()


def test_draft_status_line_and_not_frozen():
    """The DRAFT status line is exact and the file is NOT frozen (an agent never flips it)."""
    lines = _doc().splitlines()
    assert lines[2].strip() == "STATUS: DRAFT -- NOT FROZEN"
    assert falsifier_is_frozen(_DOC) is False


def test_verdict_pins_match_doc():
    doc = _doc()
    assert f"n >= {V33_FALSIFIER_MIN_N}" in doc                       # n >= 30
    assert f"+{V33_FALSIFIER_MIN_MEAN_LOCK_CENTS}c" in doc            # +6.0c
    assert f"<= {V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS}c" in doc     # <= 3.0c
    assert f">= {V33_FALSIFIER_MIN_RUNG_FILLS_FOR_SHORTFALL} [pin] fills" in doc  # >= 3 [pin] fills
    assert f">= {V33_FALSIFIER_MIN_PCT_POSITIVE}%" in doc             # >= 80%
    assert f">= {V33_CAPTURE_RATIO_MIN} [pin]" in doc                 # >= 0.50 [pin]
    assert f"{V33_FALSIFIER_CAPTURE_MARGIN_C}c margin" in doc         # 10c margin
    assert f"one-legged <= {V33_FALSIFIER_MAX_ONE_LEGGED} [pin]" in doc  # <= 2 [pin]
    # roll integrity >= 90% [pin]
    assert f">= {int(V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO * 100)}% [pin]" in doc


def test_kill_pins_match_doc():
    doc = _doc()
    assert f"+{V33_KILL_MEAN_LOCK_CENTS}c [pin]" in doc               # +2.0c [pin]
    assert f"`n >= {V33_KILL_MIN_N}` [pin]" in doc                    # `n >= 15` [pin]
    assert f"> {V33_FALSIFIER_MAX_ONE_LEGGED} [pin] contracts" in doc  # > 2 [pin] contracts


def test_promotion_pin_matches_doc():
    doc = _doc()
    assert f"`n >= {V33_PROMOTION_MIN_N}` [pin]" in doc               # `n >= 30` [pin]
    assert V33_PROMOTION_MIN_N == 30


def test_stop_pins_and_sha_match_doc():
    doc = _doc()
    assert FROZEN_V33_PARAMS_SHA256 in doc
    assert FROZEN_V33_PARAMS_SHA256 == load_v33_params().sha256
    assert f"${V33_S4_DAY_LOSS_CAP_DOLLARS}" in doc                   # $3.00
    assert f"{V33_MIN_ORDER_BUDGET_AT_ARM} [pin]" in doc              # 500 [pin]
    assert f"after {V33_S1_LEGGED_LATCH_THRESHOLD} [pin]" in doc      # after 2 [pin]


def test_caps_range_pin_in_doc():
    """The S5 caps range [lots_per_rung, K*lots_per_rung] = [1, 11] is pinned in the doc."""
    p = load_v33_params()
    assert p.lots_per_rung == 1 and p.rungs == 11
    assert "`[1, 11]` [pin]" in _doc()


def test_bucket_freshness_pin_matches_params_and_doc():
    p = load_v33_params()
    assert p.bucket_freshness_max_age_s == 30.0
    assert f"bucket_freshness_max_age_s 30.0 [pin]" in _doc()
    assert p.sha256 == FROZEN_V33_PARAMS_SHA256


def _health(*, maxc=2, prefixes=("KXBTC", "KXBTCD"), enabled=True, remaining=4000):
    return {"orders_enabled": enabled,
            "caps": {"max_contracts_per_order": maxc, "ticker_prefixes": list(prefixes),
                     "daily_order_budget": 8000},
            "orders_remaining_today": remaining}


def test_arming_refuses_while_draft():
    """S5 refuses to arm V3.3 while the falsifier is a DRAFT, even with a perfect /health + clean guard."""
    outcome = decide_v33_arming(
        resolved_mode="armed", falsifier_path=_DOC, health=_health(), positions=[],
        params_verified=True, lots_per_rung=1, day_guard=DayGuard("2026-09-23", None, [], False),
        s4=None, k_rungs=11)
    assert outcome.armed is False
    assert any("falsifier" in r.lower() for r in outcome.reasons)


def test_promotion_and_kill_constants_values():
    assert V33_FALSIFIER_MIN_MEAN_LOCK_CENTS == Decimal("6.0")
    assert V33_CAPTURE_RATIO_MIN == Decimal("0.50")
    assert V33_KILL_MEAN_LOCK_CENTS == Decimal("2.0")
    assert V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO == Decimal("0.90")
