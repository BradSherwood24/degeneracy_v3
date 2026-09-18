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


def test_registration_carries_2026_09_15_clarification():
    """The 2026-09-15 measurement clarification (armed-days = armed_windows/24 + the T-4 expiry wording
    fix) is registered on Brad's verbatim go. A MEASUREMENT DEFINITION, not a threshold change -- the
    frozen [pin] gates and the STATUS line are untouched (asserted elsewhere in this file)."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "MEASUREMENT CLARIFICATION" in reg
    assert "Lets do option 2" in reg
    assert "armed_windows / 24" in reg
    # the T-4 expiry wording fix landed in MUST CONFIRM item 1: that section no longer claims the rest
    # "auto-expires at T-5" (the Registration entry may still quote the OLD phrase to record the change)
    confirm = doc.split("## FIRST ARMED WINDOW MUST CONFIRM", 1)[1].split("## Registration", 1)[0]
    assert "auto-expires at T-5" not in confirm
    assert "quote-end cancel at T-5 is the PRIMARY path" in confirm
    assert "EXPIRATION_GRACE_S` = 60, PR #50" in doc


def test_registration_carries_2026_09_15_shadow_window_clarification():
    """MEASUREMENT CLARIFICATION 2 (shadow window gate, 2026-09-15 ~18:10Z, Brad's verbatim go, PR #54):
    the shadow records a fill only on prints inside the live quoting window T-15..T-5; prints outside
    are journaled/counted (``shadow_fills_outside_window``) but never fill. A MEASUREMENT DEFINITION,
    not a threshold change -- the frozen [pin] gates and the STATUS line are untouched (asserted
    elsewhere in this file)."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "MEASUREMENT CLARIFICATION 2" in reg
    assert "shadow_fills_outside_window" in reg


def test_registration_carries_2026_09_18_partial_fill_clarification():
    """MECHANICS + MEASUREMENT CLARIFICATION (partial fills / sizing step, 2026-09-18 ~18:30Z, Brad's
    verbatim ruling): on each rest fill event take both wings sized to the fill, the remainder stays
    resting, a completed SET = one rest-fill event with both wings filled, lock reported per contract,
    fill rate = set events / armed day. A MECHANICS + MEASUREMENT DEFINITION registered at n=6 live sets
    BEFORE any size change -- no [pin], threshold, params sha, or the STATUS line is touched (asserted
    elsewhere in this file). ``params.contracts`` remains Brad's lever and stays 1 here; a raise gets its
    own params-sha Registration entry."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "MECHANICS + MEASUREMENT CLARIFICATION" in reg
    # Brad's verbatim ruling is quoted
    assert "we open 8 wings and leave the 2 unfilled" in reg
    assert "Hopefully another taker comes and fills the remainder" in reg
    # the mechanics: wings sized to the fill, remainder stays resting, delta booking
    assert "sized to the fill" in reg
    assert "REMAINDER stays on the book" in reg
    assert "CUMULATIVE per order" in reg and "DELTA" in reg
    # the measurement: set = rest-fill event with both wings filled; lock per contract; set-event rate
    assert "one rest-fill EVENT" in reg
    assert "PER CONTRACT" in reg
    assert "SET EVENTS per armed evaluation day" in reg
    # ladders/levels out of scope; contracts stays 1 (the params sha pin gets its own entry on a raise)
    assert "OUT of scope" in reg
    assert "params.contracts` remains BRAD'S lever and stays 1" in reg
    # STATUS line + the frozen params sha untouched by this MEASUREMENT/MECHANICS entry
    assert doc.splitlines()[2].strip() == "STATUS: FROZEN"
    assert FROZEN_V32_PARAMS_SHA256 in doc


def test_partial_fill_contracts_lever_unchanged_at_one():
    """The partial-fill clarification does NOT change ``contracts`` -- the frozen policy still rests 1
    lot (any raise is a separate amendment with its own pinned sha)."""
    p = load_v32_params()
    assert p.contracts == 1
    assert p.sha256 == FROZEN_V32_PARAMS_SHA256


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
