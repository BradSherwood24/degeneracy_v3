"""Wide-box core tests: side logic both directions, nearest-0.95 selection + tie-break, every
filter, the spread rule, absent quotes, freshness fail-closed, entry-window bounds, one-pair
latch, strike switching before the fire, shakedown -> WOULD_FIRE, the sha loader refusal, and C
arithmetic against the imported census fee."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import close_epoch, fee
from service.book import TopOfBook
from service.box import (
    BUY_NO,
    BUY_YES,
    FIRE,
    STAND_DOWN,
    WIDE_BOX,
    WOULD_FIRE,
    BookUpdate,
    BoxParams,
    BoxPolicyShaMismatch,
    BoxSelection,
    BoxState,
    ClockTick,
    NoBox,
    canonical_sha256,
    decide_box,
    load_box_policy,
    select_box,
    FROZEN_BOX_POLICY_SHA256,
)

CT = "2026-06-14T20:00:00Z"
T = close_epoch(CT)
PARAMS = load_box_policy()
A = Decimal("65000")


def top(*, yes_ask=None, yes_bid=None, suspect=False) -> TopOfBook:
    """A top-of-book carrying just the YES bid/ask the box reads."""
    def d(x):
        return None if x is None else Decimal(str(x))
    ya, yb = d(yes_ask), d(yes_bid)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=(None if yb is None else Decimal(1)),
        yes_ask=ya, yes_ask_size=(None if ya is None else Decimal(1)),
        no_bid=(None if ya is None else Decimal(1) - ya),
        no_bid_size=(None if ya is None else Decimal(1)),
        no_ask=(None if yb is None else Decimal(1) - yb),
        no_ask_size=(None if yb is None else Decimal(1)),
        suspect=suspect,
    )


# ---------------------------------------------------------------------------
# select_box: side logic
# ---------------------------------------------------------------------------
def test_below_anchor_buys_15m_no_and_hourly_yes_below():
    m15 = top(yes_ask="0.20", yes_bid="0.14")  # mid 0.17 < 0.5 -> below
    ladder = {
        "H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95")),  # YES mid .96
        "H-T63500": (Decimal("63500"), top(yes_ask="0.99", yes_bid="0.985")),  # YES mid .9875
    }
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, BoxSelection)
    assert sel.m15_side == BUY_NO
    assert sel.hourly_side == BUY_YES
    assert sel.strike_K == Decimal("64000")     # nearest mid to 0.95
    assert sel.m15_ask == Decimal("0.86")       # 1 - yes_bid
    assert sel.m15_bid == Decimal("0.80")       # 1 - yes_ask


def test_above_anchor_buys_15m_yes_and_hourly_no_above():
    m15 = top(yes_ask="0.86", yes_bid="0.80")  # mid 0.83 >= 0.5 -> above
    ladder = {
        # NO mid = 1 - yes_mid.  yes 0.03/0.05 -> yes_mid .04 -> NO mid .96
        "H-T66000": (Decimal("66000"), top(yes_ask="0.05", yes_bid="0.03")),
        # yes .01/.015 -> yes_mid .0125 -> NO mid .9875
        "H-T66500": (Decimal("66500"), top(yes_ask="0.015", yes_bid="0.01")),
    }
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, BoxSelection)
    assert sel.m15_side == BUY_YES
    assert sel.hourly_side == BUY_NO
    assert sel.strike_K == Decimal("66000")     # NO mid .96 nearest 0.95
    assert sel.hourly_ask == Decimal("0.97")    # NO ask = 1 - yes_bid(.03)
    assert sel.hourly_bid == Decimal("0.95")    # NO bid = 1 - yes_ask(.05)
    assert sel.hourly_mid == Decimal("0.96")


# ---------------------------------------------------------------------------
# nearest-target selection + tie-break
# ---------------------------------------------------------------------------
def test_selection_is_nearest_target_mid():
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {
        "H-T64000": (Decimal("64000"), top(yes_ask="0.94", yes_bid="0.92")),   # mid .93
        "H-T64200": (Decimal("64200"), top(yes_ask="0.96", yes_bid="0.94")),   # mid .95 (exact)
        "H-T63000": (Decimal("63000"), top(yes_ask="0.99", yes_bid="0.98")),   # mid .985
    }
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert sel.strike_K == Decimal("64200")     # mid exactly 0.95


def test_tie_break_prefers_widest_gap_from_anchor():
    # Brad 2026-08-26: when two candidates are equally near 0.95 by mid, the WIDEST gap from A wins
    # (the wider box gives the larger pin region). Candidates on BOTH sides of the target mid (0.94
    # below and 0.96 above) both have |mid-0.95|=0.01 -> tie -> the strike farther from A (63000).
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {
        "H-T63000": (Decimal("63000"), top(yes_ask="0.95", yes_bid="0.93")),   # mid .94, |A-K|=2000
        "H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95")),   # mid .96, |A-K|=1000
    }
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert sel.strike_K == Decimal("63000")     # both |mid-0.95|=0.01 -> WIDEST gap from A


def test_tie_break_only_applies_among_equal_mid_candidates():
    # A strictly-nearer-mid candidate still wins outright; the widest-gap tie-break only decides ties.
    # Here 64000 (mid 0.95, exact) beats 63000 (mid 0.94) even though 63000 has the wider gap.
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {
        "H-T63000": (Decimal("63000"), top(yes_ask="0.95", yes_bid="0.93")),   # mid .94  |mid-t|=.01
        "H-T64000": (Decimal("64000"), top(yes_ask="0.96", yes_bid="0.94")),   # mid .95  |mid-t|=.00
        "H-T64200": (Decimal("64200"), top(yes_ask="0.97", yes_bid="0.93")),   # mid .95  |mid-t|=.00
    }
    # 64000 and 64200 tie on mid (both exactly 0.95); among the tie, 64000 has the wider gap from A.
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert sel.strike_K == Decimal("64000")


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------
def test_min15_ask_filter_blocks():
    # 15M NO ask = 1 - yes_bid.  yes_bid 0.20 -> NO ask 0.80 < 0.85 -> no fire.
    m15 = top(yes_ask="0.25", yes_bid="0.20")
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "min15_ask" in sel.reason


def test_hourly_ask_below_min_blocks():
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    # only candidate: YES ask 0.89 < hourly_ask_min 0.90
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.89", yes_bid="0.85"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "hourly ask" in sel.reason


def test_hourly_ask_above_max_blocks():
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    # only candidate: YES ask 0.995 > hourly_ask_max 0.99 (spread 0.005 ok)
    ladder = {"H-T63000": (Decimal("63000"), top(yes_ask="0.995", yes_bid="0.99"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "hourly ask" in sel.reason


# ---------------------------------------------------------------------------
# spread rule + absent quotes
# ---------------------------------------------------------------------------
def test_m15_spread_too_wide_blocks():
    m15 = top(yes_ask="0.30", yes_bid="0.14")  # spread 0.16 > 0.10
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "spread" in sel.reason


def test_hourly_spread_too_wide_disqualifies_candidate():
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {
        "H-T64000": (Decimal("64000"), top(yes_ask="0.99", yes_bid="0.80")),   # spread .19 -> out
        "H-T63500": (Decimal("63500"), top(yes_ask="0.98", yes_bid="0.95")),   # spread .03 ok, mid .965
    }
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, BoxSelection)
    assert sel.strike_K == Decimal("63500")     # the wide-spread candidate was dropped


def test_absent_15m_quote_blocks():
    m15 = top(yes_ask=None, yes_bid="0.14")
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "missing" in sel.reason


def test_no_qualifying_candidate_blocks():
    m15 = top(yes_ask="0.20", yes_bid="0.14")  # below -> needs K < A
    ladder = {"H-T66000": (Decimal("66000"), top(yes_ask="0.97", yes_bid="0.95"))}  # K > A only
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "candidate" in sel.reason


# ---------------------------------------------------------------------------
# C arithmetic uses the imported census fee
# ---------------------------------------------------------------------------
def test_C_arithmetic_uses_imported_fee():
    m15 = top(yes_ask="0.20", yes_bid="0.14")   # NO ask 0.86, NO bid 0.80
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.97", yes_bid="0.95"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    h_ask, m15_ask = Decimal("0.97"), Decimal("0.86")
    expected_C = h_ask + fee(h_ask) + m15_ask + fee(m15_ask)
    assert sel.C == expected_C
    # informational mids (fee-free), mirroring the scratch
    assert sel.C_mid == Decimal("0.96") + (Decimal("0.86") + Decimal("0.80")) / 2
    assert sel.implied_pin == sel.C_mid - Decimal("1")


# ---------------------------------------------------------------------------
# limit_margin: IOC limits get margin; filters + C stay on observed asks
# ---------------------------------------------------------------------------
def test_limit_is_ask_plus_margin_when_below_ceiling():
    # hourly ask 0.94 (<= 0.96) -> limit 0.97 ; m15 NO ask 0.86 -> limit 0.89
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.94", yes_bid="0.92"))}  # mid .93
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert sel.hourly_ask == Decimal("0.94") and sel.hourly_limit == Decimal("0.97")
    assert sel.m15_ask == Decimal("0.86") and sel.m15_limit == Decimal("0.89")


def test_limit_capped_at_ceiling_when_ask_high():
    # hourly ask 0.98 (>= 0.97) -> 0.98 + 0.03 = 1.01 capped to 0.99
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {"H-T63000": (Decimal("63000"), top(yes_ask="0.98", yes_bid="0.97"))}  # mid .975, ask ok
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert sel.hourly_ask == Decimal("0.98")
    assert sel.hourly_limit == Decimal("0.99")


def test_margin_does_not_affect_filters():
    # hourly ask 0.995 fails the max filter even though ask+margin would also be capped;
    # the filter is on the OBSERVED ask, so this is a NoBox.
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {"H-T63000": (Decimal("63000"), top(yes_ask="0.995", yes_bid="0.99"))}
    sel = select_box(A, "M15", m15, ladder, PARAMS)
    assert isinstance(sel, NoBox) and "hourly ask" in sel.reason


def test_legorders_use_limits_not_observed_asks():
    st = _state()
    now = T - 300
    st, acts = _drive(st, now, hourly=("0.94", "0.92"))
    assert [a.kind for a in acts] == [FIRE]
    limits = {leg.ticker: leg.limit_price for leg in acts[0].legs}
    assert limits["H-T64000"] == Decimal("0.97")   # 0.94 + 0.03
    assert limits["M15"] == Decimal("0.89")         # 0.86 + 0.03
    # C on the action is still the fee-inclusive observed-ask cost
    assert acts[0].C == fee(Decimal("0.94")) + Decimal("0.94") + fee(Decimal("0.86")) + Decimal("0.86")


def test_margin_read_from_sha_pinned_roster():
    # a roster with a different margin (loaded without the sha check) changes the limit only
    from service.box import DEFAULT_BOX_POLICY_PATH
    obj = json.load(open(DEFAULT_BOX_POLICY_PATH, encoding="utf-8"))
    obj["limit_margin"] = "0.01"
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    open(path, "w", encoding="utf-8").write(json.dumps(obj))
    p2 = load_box_policy(path, expected_sha=None)
    os.unlink(path)
    m15 = top(yes_ask="0.20", yes_bid="0.14")
    ladder = {"H-T64000": (Decimal("64000"), top(yes_ask="0.94", yes_bid="0.92"))}
    sel = select_box(A, "M15", m15, ladder, p2)
    assert sel.hourly_limit == Decimal("0.95")   # 0.94 + 0.01
    assert sel.hourly_ask == Decimal("0.94")     # observed ask unchanged


# ---------------------------------------------------------------------------
# decide_box: entry window bounds
# ---------------------------------------------------------------------------
def _state(shakedown=False):
    st = BoxState.new(
        close_time=CT, anchor_A=A, m15_ticker="M15",
        strikes={"H-T64000": Decimal("64000")}, shakedown=shakedown,
    )
    return st


def _drive(st, now, *, hourly=("0.97", "0.95")):
    """Seed both legs fresh at ``now`` (below case) then tick, collecting all actions. The fire,
    when it happens, lands on the second BookUpdate (both legs present); the trailing ClockTick
    then returns nothing because the window has latched."""
    acts = []
    st, a = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), now)); acts += a
    st, a = decide_box(PARAMS, st, BookUpdate("H-T64000", top(yes_ask=hourly[0], yes_bid=hourly[1]), now)); acts += a
    st, a = decide_box(PARAMS, st, ClockTick(now)); acts += a
    return st, acts


def test_fires_at_window_open_t_minus_600():
    st = _state()
    st, acts = _drive(st, T - 600)
    assert [a.kind for a in acts] == [FIRE]
    assert acts[0].source == WIDE_BOX
    assert st.entered and st.fired_selection is not None


def test_no_fire_before_window_t_minus_601():
    st = _state()
    st, acts = _drive(st, T - 601)
    assert acts == []
    assert not st.entered


def test_no_fire_after_window_t_minus_59():
    st = _state()
    st, acts = _drive(st, T - 59)
    assert acts == []
    assert not st.entered


def test_fires_at_window_close_t_minus_60():
    st = _state()
    st, acts = _drive(st, T - 60)
    assert [a.kind for a in acts] == [FIRE]


def test_standdown_emitted_once_inside_cutoff():
    st = _state()
    st, acts = _drive(st, T - 0.5)  # t_minus 0.5 < 1
    assert [a.kind for a in acts] == [STAND_DOWN]
    assert st.standdown_emitted
    # a second event inside the cutoff does not re-emit
    st, acts2 = decide_box(PARAMS, st, ClockTick(T - 0.2))
    assert acts2 == []


# ---------------------------------------------------------------------------
# one-pair-per-hour latch
# ---------------------------------------------------------------------------
def test_one_pair_per_hour_latch():
    st = _state()
    st, acts = _drive(st, T - 300)
    assert [a.kind for a in acts] == [FIRE]
    # any later qualifying instant does nothing
    st, acts2 = _drive(st, T - 120)
    assert acts2 == []


# ---------------------------------------------------------------------------
# strike switching between instants before the fire
# ---------------------------------------------------------------------------
def test_strike_can_switch_between_instants_before_fire():
    # ladder with two strikes; at the first instant neither hourly ask is in-range (both blocked),
    # at a later instant a different strike becomes the pick and fires.
    st = BoxState.new(
        close_time=CT, anchor_A=A, m15_ticker="M15",
        strikes={"H-T64000": Decimal("64000"), "H-T63500": Decimal("63500")},
    )
    # instant 1 (T-600): only 64000 present but ask 0.995 > max -> no fire
    st, _ = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), T - 600))
    st, _ = decide_box(PARAMS, st, BookUpdate("H-T64000", top(yes_ask="0.995", yes_bid="0.99"), T - 600))
    st, acts = decide_box(PARAMS, st, ClockTick(T - 600))
    assert acts == [] and not st.entered
    # instant 2 (T-540): 63500 now present, mid 0.96 in-range -> fires on 63500
    st, _ = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), T - 540))
    acts2 = []
    st, a = decide_box(PARAMS, st, BookUpdate("H-T63500", top(yes_ask="0.97", yes_bid="0.95"), T - 540)); acts2 += a
    st, a = decide_box(PARAMS, st, BookUpdate("H-T64000", top(yes_ask="0.995", yes_bid="0.99"), T - 540)); acts2 += a
    st, a = decide_box(PARAMS, st, ClockTick(T - 540)); acts2 += a
    assert [a.kind for a in acts2] == [FIRE]
    assert st.fired_selection.strike_K == Decimal("63500")


# ---------------------------------------------------------------------------
# freshness fail-closed
# ---------------------------------------------------------------------------
def test_stale_leg_fails_closed():
    st = _state()
    # seed both legs at T-605, then evaluate at T-600 -> ages 5s > 1s freshness bound
    st, _ = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), T - 605))
    st, _ = decide_box(PARAMS, st, BookUpdate("H-T64000", top(yes_ask="0.97", yes_bid="0.95"), T - 605))
    st, acts = decide_box(PARAMS, st, ClockTick(T - 600))
    assert acts == [] and not st.entered


def test_suspect_leg_fails_closed():
    st = _state()
    now = T - 300
    st, _ = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), now))
    st, _ = decide_box(PARAMS, st, BookUpdate("H-T64000", top(yes_ask="0.97", yes_bid="0.95", suspect=True), now))
    st, acts = decide_box(PARAMS, st, ClockTick(now))
    assert acts == [] and not st.entered


def test_missing_leg_before_first_update_no_fire():
    st = _state()
    now = T - 300
    # only 15M seeded; hourly never updated -> no candidate/freshness -> no fire
    st, acts = decide_box(PARAMS, st, BookUpdate("M15", top(yes_ask="0.20", yes_bid="0.14"), now))
    assert acts == [] and not st.entered


# ---------------------------------------------------------------------------
# shakedown -> WOULD_FIRE
# ---------------------------------------------------------------------------
def test_shakedown_would_fire():
    st = _state(shakedown=True)
    st, acts = _drive(st, T - 300)
    assert [a.kind for a in acts] == [WOULD_FIRE]


# ---------------------------------------------------------------------------
# unrelated ticker ignored
# ---------------------------------------------------------------------------
def test_unrelated_ticker_ignored():
    st = _state()
    before = st
    st, acts = decide_box(PARAMS, st, BookUpdate("SOMETHING-ELSE", top(yes_ask="0.5", yes_bid="0.4"), T - 300))
    assert acts == []
    assert st.tops == before.tops == {}


# ---------------------------------------------------------------------------
# sha loader
# ---------------------------------------------------------------------------
def test_default_load_self_verifies():
    p = load_box_policy()
    assert p.sha256 == FROZEN_BOX_POLICY_SHA256
    assert p.roster_name == "box-v1"
    assert p.target_mid == Decimal("0.95")
    assert p.hourly_ask_min == Decimal("0.90")
    assert p.hourly_ask_max == Decimal("0.99")
    assert p.min15_ask == Decimal("0.85")
    assert p.max_spread == Decimal("0.10")
    assert p.limit_margin == Decimal("0.03")
    assert p.entry_start_s == 600 and p.entry_end_s == 60
    assert p.freshness_max_leg_age_s == 1.0
    assert p.no_orders_after_s_to_settle == 1 and p.contracts == 1
    assert p.pair_cost_max == Decimal("1.99")


def test_canonical_sha_key_order_invariant():
    from service.box import DEFAULT_BOX_POLICY_PATH
    obj = json.load(open(DEFAULT_BOX_POLICY_PATH, encoding="utf-8"))
    shuffled = dict(reversed(list(obj.items())))
    assert canonical_sha256(obj) == canonical_sha256(shuffled) == FROZEN_BOX_POLICY_SHA256


def test_wrong_expected_sha_refuses():
    with pytest.raises(BoxPolicyShaMismatch):
        load_box_policy(expected_sha="0" * 64)


def test_tampered_file_refuses_but_loads_without_check(tmp_path):
    from service.box import DEFAULT_BOX_POLICY_PATH
    obj = json.load(open(DEFAULT_BOX_POLICY_PATH, encoding="utf-8"))
    obj["target_mid"] = "0.90"  # tamper
    p = tmp_path / "tampered.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    with pytest.raises(BoxPolicyShaMismatch):
        load_box_policy(str(p))
    loaded = load_box_policy(str(p), expected_sha=None)
    assert loaded.target_mid == Decimal("0.90")
    assert loaded.sha256 != FROZEN_BOX_POLICY_SHA256
