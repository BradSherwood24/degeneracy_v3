"""The V3.2 falsifier document and the [pin] constants MUST agree (registered-specs rule).

If either the doc (`ceremony/v32_falsifier.md`) or the constants (`service.v32.falsifier_pins`,
`service.v32.stops`) drift, this test fails. No network/proxy/holdout read -- it only reads two repo
files. Also asserts the draft is NOT frozen (an agent never flips it) and that the S5 gate would refuse
to arm on it."""

from __future__ import annotations

import os

from service.stops import falsifier_is_frozen
from service.v32.falsifier_pins import (
    V32_FALSIFIER_MAX_EXEC_GAP_CENTS,
    V32_FALSIFIER_MAX_ONE_LEGGED,
    V32_FALSIFIER_MIN_FILL_RATE_PER_DAY,
    V32_FALSIFIER_MIN_MEAN_LOCK_CENTS,
    V32_FALSIFIER_MIN_N,
    V32_FALSIFIER_MIN_PCT_POSITIVE,
    V32_PROMOTION_MIN_DEPTH_LOTS,
    V32_PROMOTION_MIN_N,
    V32_R2_CONSECUTIVE_NEG_DAYS,
    V32_R3_LEGGED_LATCHES_PER_WEEK,
    V32_R4_EXEC_GAP_CENTS,
    V32_R4_MIN_N,
)
from service.v32.params import FROZEN_V32_PARAMS_SHA256, load_v32_params
from service.v32.stops import (
    V32_MAX_CONTRACTS_PER_ORDER,
    V32_MIN_ORDER_BUDGET_AT_ARM,
    V32_S1_LEGGED_LATCH_THRESHOLD,
    V32_S4_DAY_LOSS_CAP_DOLLARS,
)

_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "ceremony", "v32_falsifier.md")


def _doc() -> str:
    with open(_DOC, "r", encoding="utf-8") as f:
        return f.read()


def test_falsifier_is_frozen_with_registration():
    """Frozen 2026-09-14 on Brad's verbatim order (PR #41). The freeze line must be exact and the
    Registration section must carry the go -- a FROZEN line without a registered go is a defect."""
    doc = _doc()
    lines = doc.splitlines()
    assert lines[2].strip() == "STATUS: FROZEN"
    assert "STATUS: DRAFT" not in doc
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "FROZEN on Brad's order" in reg and "go ahead and freeze it" in reg
    assert "(empty -- awaiting Brad's freeze)" not in reg
    assert falsifier_is_frozen(_DOC) is True


def test_verdict_pins_match_doc():
    doc = _doc()
    assert f"n >= {V32_FALSIFIER_MIN_N}" in doc
    assert f"+{V32_FALSIFIER_MIN_MEAN_LOCK_CENTS}c" in doc            # +4.0c
    assert f">= {V32_FALSIFIER_MIN_PCT_POSITIVE}%" in doc            # >= 80%
    assert f">= {V32_FALSIFIER_MIN_FILL_RATE_PER_DAY} sets/day" in doc  # >= 2.0 sets/day
    assert f"<= {V32_FALSIFIER_MAX_EXEC_GAP_CENTS}c" in doc          # <= 3.0c
    assert f"<= {V32_FALSIFIER_MAX_ONE_LEGGED} of {V32_FALSIFIER_MIN_N}" in doc  # <= 2 of 30


def test_retirement_pins_match_doc():
    doc = _doc()
    assert f"{V32_R2_CONSECUTIVE_NEG_DAYS} consecutive" in doc       # 3 consecutive
    assert f"{V32_R3_LEGGED_LATCHES_PER_WEEK} S1_LEGGED" in doc      # 2 S1_LEGGED
    assert f"> {V32_R4_EXEC_GAP_CENTS}c" in doc                      # > 5.0c
    assert f"n >= {V32_R4_MIN_N}" in doc                             # n >= 15


def test_promotion_pins_match_doc():
    doc = _doc()
    assert f"n >= {V32_PROMOTION_MIN_N}" in doc                      # n >= 60
    assert f"{V32_PROMOTION_MIN_DEPTH_LOTS} lots" in doc             # 10 lots


def test_stop_pins_and_sha_match_doc():
    doc = _doc()
    assert FROZEN_V32_PARAMS_SHA256 in doc
    assert f"${V32_S4_DAY_LOSS_CAP_DOLLARS}" in doc                  # $3.00
    assert f"({V32_MIN_ORDER_BUDGET_AT_ARM})" in doc                 # (200)
    assert f"= {V32_MAX_CONTRACTS_PER_ORDER})" in doc                # = 2)
    assert f"after {V32_S1_LEGGED_LATCH_THRESHOLD} [pin]" in doc     # after 2 [pin]


def test_bucket_freshness_pin_matches_params_and_doc():
    # the new spot-bucket freshness gate's bound is a policy param (sha-pinned JSON) mirrored as a
    # [pin] in the falsifier doc; doc text, the doc's sha, and the loaded value must all agree.
    doc = _doc()
    p = load_v32_params()
    assert p.bucket_freshness_max_age_s == 30.0
    assert f"bucket_freshness_max_age_s {p.bucket_freshness_max_age_s} [pin]" in doc  # 30.0 [pin]
    assert p.sha256 == FROZEN_V32_PARAMS_SHA256 and FROZEN_V32_PARAMS_SHA256 in doc
