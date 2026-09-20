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
    """SUPERSEDED by AMENDMENT 1 (2026-09-20): ``params.contracts`` is Brad's lever and he raised it
    1 -> 2 on his dated go. The function name is kept for history (add-only law); it now asserts the
    amendment -- the frozen policy rests 2 lots and self-verifies the new pinned sha. (The 2026-09-18
    Registration text -- "``params.contracts`` remains BRAD'S lever and stays 1 here" -- is unchanged in
    the doc as the record of the pre-amendment state; the AMENDMENT 1 Registration entry supersedes it.)"""
    p = load_v32_params()
    assert p.contracts == 2   # AMENDMENT 1 (was 1)
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


# ===========================================================================
# ADD-ONLY: AMENDMENT 1 (2026-09-20, Brad's dated go). params.contracts 1 -> 2; the params sha is
# re-pinned; the previous freeze sha stays DEFINED in code as history. New assertions only -- no
# assertion above is edited or deleted (add-only law). The ONLY policy value changed is contracts.
# ===========================================================================
_NEW_SHA_AMENDMENT_1 = "a2a58787bb88a6ded644c2ff6a22c5e75fbb1b41882ca7f76e40d9405a139a9c"
_OLD_SHA_FREEZE_2026_09_14 = "0ac697957c69a004e45d49505cce1084aaeb2e50bbaea45fe60bfbe0911c80dc"


def test_amendment1_params_sha_repinned_and_previous_defined():
    """The new params sha is the pinned literal; the 2026-09-14 freeze sha stays DEFINED in code as
    history (add-only law). Hard-coded literals so any future drift is caught."""
    from service.v32.params import (
        FROZEN_V32_PARAMS_SHA256 as PIN,
        PREVIOUS_V32_PARAMS_SHA256_2026_09_14 as PREV,
    )
    assert PIN == _NEW_SHA_AMENDMENT_1
    assert PREV == _OLD_SHA_FREEZE_2026_09_14
    assert PIN != PREV


def test_amendment1_loader_self_verifies_contracts_two():
    """The shipped policy loads at contracts=2 and self-verifies the new pinned sha (plain call)."""
    p = load_v32_params()
    assert p.contracts == 2
    assert p.sha256 == FROZEN_V32_PARAMS_SHA256 == _NEW_SHA_AMENDMENT_1


def test_amendment1_registration_entry_present():
    """The Registration section carries the AMENDMENT 1 entry: the title, BOTH shas, and Brad's exact
    words. Placed after MEASUREMENT CLARIFICATION 3 and before the shadow observations section."""
    doc = _doc()
    reg = doc.split("## Registration", 1)[1].split("## Pre-registered shadow observations", 1)[0]
    assert "AMENDMENT 1" in reg
    assert "contracts 1 -> 2" in reg
    # both shas recorded (old -> new)
    assert _OLD_SHA_FREEZE_2026_09_14 in reg
    assert _NEW_SHA_AMENDMENT_1 in reg
    # Brad's exact words (2026-09-20 go, and the 2026-09-18 reasoning)
    assert "You have my go to build the multi-contract. Lets size up!" in reg
    assert "not proof of a coin flip is weighted on one side" in reg
    # the STATUS line is untouched by the amendment
    assert doc.splitlines()[2].strip() == "STATUS: FROZEN"
    # the entry sits between Registration 3 and the shadow observations section
    assert reg.index("MEASUREMENT CLARIFICATION 3") < reg.index("AMENDMENT 1")


def test_amendment1_promotion_pins_still_defined_and_in_doc():
    """The Promotion section text and its pins are NOT edited (add-only law): n >= 60 and 10 lots still
    appear in the doc and the code constants are unchanged. The AMENDMENT 1 entry supersedes the
    Promotion CLAUSE by pointer (Brad's 2026-09-18 re-frame), it does not delete the pins."""
    doc = _doc()
    assert f"n >= {V32_PROMOTION_MIN_N}" in doc   # n >= 60
    assert f"{V32_PROMOTION_MIN_DEPTH_LOTS} lots" in doc  # 10 lots
    assert V32_PROMOTION_MIN_N == 60
    assert V32_PROMOTION_MIN_DEPTH_LOTS == 10


def _health_shape(*, maxc=2, prefixes=("KXBTC", "KXBTC15M"), enabled=True, remaining=4000):
    return {"orders_enabled": enabled,
            "caps": {"max_contracts_per_order": maxc, "ticker_prefixes": list(prefixes),
                     "daily_order_budget": 4000},
            "orders_remaining_today": remaining}


def test_amendment1_caps_agree_at_two_refuses_above():
    """The proxy cap MAX_CONTRACTS_PER_ORDER = 2 stands: params.contracts=2 agrees at proxy max 2;
    contracts=2 with proxy max 1 refuses; a params.contracts of 3 is refused (exceeds proxy max, and
    exceeds the V3.2 ceiling when the proxy would even allow it)."""
    from service.v32.stops import v32_caps_agree, V32_MAX_CONTRACTS_PER_ORDER
    assert V32_MAX_CONTRACTS_PER_ORDER == 2
    ok, _ = v32_caps_agree(_health_shape(maxc=2), contracts=2)
    assert ok
    ok, why = v32_caps_agree(_health_shape(maxc=1), contracts=2)
    assert not ok and "max_contracts_per_order" in why
    # params.contracts 3 against a real proxy (max 2): refused (proxy max < params.contracts)
    ok, why = v32_caps_agree(_health_shape(maxc=2), contracts=3)
    assert not ok and "params.contracts" in why
    # and if the proxy itself advertised max 3, the V3.2 ceiling refuses it
    ok, why = v32_caps_agree(_health_shape(maxc=3), contracts=3)
    assert not ok and "ceiling" in why


def test_amendment1_arming_check_passes_at_contracts_two():
    """S5 arms at contracts=2 with the real frozen doc + a healthy /health shape (mirrors
    test_v32_stops.py::test_arming_ok_when_all_pass at the new size)."""
    from service.v32.stops import v32_arming_check
    d = v32_arming_check(_DOC, _health_shape(maxc=2), params_verified=True, contracts=2)
    assert d.armed and d.reasons == ()
