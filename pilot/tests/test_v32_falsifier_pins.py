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


def test_registration_carries_2026_09_15_amend_mechanics_clarification():
    """MECHANICS CLARIFICATION (amend-first replace, 2026-09-15 ~18:10Z, Brad's verbatim go): the
    mid-window replace is amend-first (Kalshi Amend Order V2, same order_id, queue position forfeited
    exactly as cancel+create), with cancel -> confirm -> create as the fallback. A MECHANICS
    CLARIFICATION, not a threshold change -- the frozen [pin] gates, the params sha, and the STATUS line
    are untouched (asserted by the other tests in this file, which stay green)."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "MECHANICS CLARIFICATION" in reg
    assert "amend" in reg


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


# ===========================================================================
# ADD-ONLY: MEASUREMENT CLARIFICATION 3 (2026-09-19, fill-rate gate -> capture ratio). New assertions
# only -- no existing assertion above is edited or deleted (the fill-rate pin stays DEFINED as add-only
# law and every existing test, including test_verdict_pins_match_doc's `>= 2.0 sets/day`, stays green).
# ===========================================================================
def test_capture_ratio_pin_value():
    """The new capture-ratio [pin] (Claude's proposal; Brad confirms 0.50 at merge). The old fill-rate
    pin stays DEFINED (add-only law)."""
    from decimal import Decimal

    from service.v32.falsifier_pins import (
        V32_CAPTURE_RATIO_MIN,
        V32_FALSIFIER_MIN_FILL_RATE_PER_DAY,
    )
    assert V32_CAPTURE_RATIO_MIN == Decimal("0.50")
    # the superseded fill-rate pin is NOT removed (add-only law)
    assert V32_FALSIFIER_MIN_FILL_RATE_PER_DAY == Decimal("2.0")


def test_registration_carries_2026_09_19_capture_ratio_clarification():
    """MEASUREMENT CLARIFICATION 3 (2026-09-19 ~22:40Z, Brad's verbatim go): the fill-rate gate becomes
    a CAPTURE RATIO against pump availability (live fills / ideal-shadow fills at the threshold). A
    MEASUREMENT DEFINITION registered at n=7 BEFORE the n>=30 verdict -- the STATUS line and the frozen
    params sha are untouched (asserted elsewhere in this file)."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "MEASUREMENT CLARIFICATION 3" in reg
    # Brad's verbatim go is quoted
    assert "I agree with the second there" in reg
    assert "Looks like our dry spell has ended" in reg
    # the definition + the superseded gate + the proposed pin (Brad confirms the number at merge)
    assert "capture ratio" in reg.lower()
    assert "0.50" in reg
    assert "confirmed by Brad at merge" in reg or "number to be confirmed by Brad at merge" in reg
    # STATUS line + frozen params sha untouched by this MEASUREMENT entry
    assert doc.splitlines()[2].strip() == "STATUS: FROZEN"
    assert FROZEN_V32_PARAMS_SHA256 in doc


def test_verdict_uses_capture_ratio_not_fill_rate():
    """The scoreboard verdict at n >= 30 now gates on the capture ratio, NOT the fill rate: a low fill
    rate whose shadow availability was all captured is ALIVE, and a capture ratio below the pin is a
    KILL naming 'capture ratio' (never 'fill rate')."""
    from decimal import Decimal

    from service.v32.falsifier_pins import V32_CAPTURE_RATIO_MIN
    from service.v32.report import build_falsifier_scoreboard

    def _set(day: int, hour: int, lock: str | None, shadow: str | None, bucket: bool = True) -> dict:
        row = {
            "armed": True, "effective_mode": "armed",
            "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
            "realized_lock": lock, "one_legged": False, "realized_unsettled": True,
            "shadow": {"0.10": {"filled": True, "lock": shadow}} if shadow is not None else {},
        }
        if bucket:
            row["spot_bucket_ticker"] = f"KXBTC-T{day:02d}{hour:02d}-B0"
        return row

    def _miss(day: int, hour: int, shadow: str) -> dict:
        return {
            "armed": True, "effective_mode": "armed",
            "spot_bucket_ticker": f"KXBTC-M{day:02d}{hour:02d}-B0",
            "close_time": f"2026-09-{day:02d}T{hour:02d}:00:00Z",
            "realized_lock": None, "one_legged": False, "realized_unsettled": False,
            "shadow": {"0.10": {"filled": True, "lock": shadow}},
        }

    # (1) 30 clean sets across only 3 armed days (10/day span) -> fill rate is HIGH here, but the point
    # is the verdict no longer references it; capture 30/30 = 100% -> ALIVE.
    alive = [_set(d, h, "0.09", "0.10") for d in range(1, 4) for h in range(10)]
    sb_alive = build_falsifier_scoreboard(alive)
    assert sb_alive["n"] == 30
    assert sb_alive["capture_ratio"] == Decimal(1)
    assert sb_alive["verdict"] == "ALIVE-so-far"
    assert "fill rate" not in sb_alive["verdict"]

    # (2) same 30 clean sets, but the shadow also filled 40 windows the live path missed -> capture
    # 30/70 = 42.8% < the pin -> KILL naming capture ratio, not fill rate.
    kill = list(alive)
    m = 0
    d, h = 20, 0
    while m < 40:
        kill.append(_miss(d, h, "0.10"))
        m += 1
        h += 1
        if h == 24:
            h, d = 0, d + 1
    sb_kill = build_falsifier_scoreboard(kill)
    assert sb_kill["capture_ratio"] == Decimal(30) / Decimal(70)
    assert sb_kill["capture_ratio"] < V32_CAPTURE_RATIO_MIN
    assert sb_kill["verdict"].startswith("KILL")
    assert "capture ratio" in sb_kill["verdict"] and "fill rate" not in sb_kill["verdict"]
