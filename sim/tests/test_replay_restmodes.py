"""Tests for the maker_flip rest_mode dimension (cancel / leave / requote).

Synthetic event streams assert the three state machines:
  * ``leave``   places both bids ONCE at the first crossing and never cancels / re-quotes;
  * ``requote`` replaces a leg only when C<=theta AND its target (ask-1c) moved, never cancels
    on C>theta, and RESETS queue position on a replace;
  * ``cancel``  (v1) is the ProcessPoolExecutor default and unchanged.
Also covers placement->fill timing / C-at-placement recording and the record-only ask_B size.
"""

import os
import sys

_SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SIM_DIR)
for p in (_SIM_DIR, _REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from replay import maker_flip as mf   # noqa: E402


def Bf(ts, naH, yaL, idx=1, dqH=0, dqL=0, aszH=0, aszL=0):
    """A full-width (11-field) book frame. naH=no_ask_H mils, yaL=yes_ask_L mils; q = ask-10."""
    return ("B", ts, idx, naH, yaL, naH - 10, yaL - 10, dqH, dqL, aszH, aszL)


def T(ts, leg, taker, ypm, cnt=1.0, idx=9):
    return ("T", ts, idx, leg, taker, ypm, cnt)


# C(0.46,0.49) = 0.9849 <= 1.0 ; the canonical crossing frame -> qH=450, qL=480.
def _C(naH, yaL):
    return (mf._dollars(naH) + mf._fee_mils(naH) + mf._dollars(yaL) + mf._fee_mils(yaL))


# ----------------------------------------------------------------------------- leave

def test_leave_never_cancels_and_fills_after_reversion():
    # Place at ts100 (C<=1); ts200 pushes C>1 (would cancel under v1); a through-print at
    # ts6200 still fills the LEFT-resting NO bid. leave catches it; cancel does not.
    events = [Bf(100, 460, 490), Bf(200, 600, 600), T(6200, "H", "yes", 560)]

    pol_c = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="cancel")
    assert pol_c["first"] is None                    # v1 cancels at C>1 -> no resting bid to hit
    assert pol_c["cancels"] >= 1

    pol_l = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="leave")
    assert pol_l["first"][0] == "H" and pol_l["first"][1] == 6200 and pol_l["first"][2] == 450
    assert pol_l["cancels"] == 0 and pol_l["replaces"] == 0
    assert pol_l["episodes"] == 2                     # both legs placed once
    # placement->fill timing and C-at-placement recorded
    assert pol_l["first_ttf_ms"] == 6100
    assert abs(pol_l["first_place_C"] - _C(460, 490)) < 1e-12


def test_leave_places_once_does_not_requote():
    # A second, better crossing at ts200 must NOT move the leave bids; a through-both print
    # fills at the ORIGINAL price 450 (not the ts200 target 460).
    events = [Bf(100, 460, 490), Bf(200, 470, 480), T(300, "H", "yes", 560)]
    pol = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="leave")
    assert pol["first"][2] == 450                    # first placement price, unchanged
    assert pol["replaces"] == 0 and pol["cancels"] == 0


def test_leave_only_places_when_both_legs_quotable():
    # C<=1 but yes_ask_L missing at ts100 -> C=inf, not a crossing; first quotable is ts200.
    ev = [("B", 100, 1, 460, None, 450, None, 0, 0, 0, 0),
          Bf(200, 460, 490),
          T(300, "H", "yes", 560)]
    pol = mf._run_policy(ev, 1.0, "strict", 0, 10_000, rest_mode="leave")
    assert pol["first"] is not None and pol["first"][1] == 300
    # placed at ts200, not ts100
    assert pol["first_place_C"] is not None


# --------------------------------------------------------------------------- requote

def test_requote_replaces_only_on_price_change():
    # ts200 crossing with moved targets -> both legs replaced; a through-both print fills at
    # the NEW price 460 (vs leave's 450). Two replaces (H and L both moved).
    events = [Bf(100, 460, 490), Bf(200, 470, 480), T(300, "H", "yes", 560)]
    pol = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="requote")
    assert pol["first"][2] == 460
    assert pol["replaces"] == 2 and pol["cancels"] == 0
    assert pol["episodes"] == 2                       # initial placement counts 2 episodes


def test_requote_no_replace_when_target_unchanged():
    events = [Bf(100, 460, 490), Bf(200, 460, 490)]   # identical targets -> queue kept
    pol = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="requote")
    assert pol["replaces"] == 0 and pol["cancels"] == 0 and pol["episodes"] == 2


def test_requote_never_cancels_above_theta():
    # ts200 C>theta must be a NO-OP (bids left resting); ts300 crossing moves them -> replace.
    events = [Bf(100, 460, 490), Bf(200, 600, 600), Bf(300, 470, 480), T(400, "H", "yes", 560)]
    pol = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="requote")
    assert pol["cancels"] == 0
    assert pol["replaces"] == 2                        # only the ts300 move
    assert pol["first"][2] == 460


def test_requote_resets_queue_on_replace_lenient():
    # Placement at ts100 with queue_ahead=2 on H. An at-level print at ts150 (cum=1) does not
    # fill. ts200 replaces H to a new level (queue reset to 0), so a single at-level print at
    # ts250 fills. Proves the replace resets both queue_ahead and the cumulative counter.
    events = [
        Bf(100, 460, 490, dqH=200),        # H bid @450, queue_ahead=2 ; L bid @480
        T(150, "H", "yes", 550, cnt=1.0),  # at level 450 (1-0.55=0.45): cum=1 < 3 -> no fill
        Bf(200, 470, 490, dqH=0),          # H target -> 460 (moved), L target 480 (same); reset
        T(250, "H", "yes", 540, cnt=1.0),  # at new level 460 (1-0.54=0.46): cum=1 >= 0+1 -> FILL
    ]
    pol = mf._run_policy(events, 1.0, "lenient", 0, 10_000, rest_mode="requote")
    assert pol["first"] == ("H", 250, 460, 480)        # filled at replaced level; L rest = 480
    assert pol["replaces"] == 1                         # only H moved


def test_requote_keeps_queue_when_unchanged_lenient():
    # Same level across frames -> queue NOT reset: with queue_ahead=2, need cum>=3 to fill.
    events = [
        Bf(100, 460, 490, dqH=200),
        Bf(200, 460, 490, dqH=200),        # identical target -> no replace, queue preserved
        T(250, "H", "yes", 550, cnt=1.0),  # cum=1 <3
        T(260, "H", "yes", 550, cnt=1.0),  # cum=2 <3
        T(270, "H", "yes", 550, cnt=1.0),  # cum=3 >=3 -> FILL
    ]
    pol = mf._run_policy(events, 1.0, "lenient", 0, 10_000, rest_mode="requote")
    assert pol["first"] == ("H", 270, 450, 480)
    assert pol["replaces"] == 0


# --------------------------------------------------------------------- cancel == v1

def test_cancel_mode_is_the_default():
    events = [Bf(100, 460, 490), T(200, "H", "yes", 560), T(250, "L", "no", 470)]
    default = mf._run_policy(events, 1.0, "strict", 0, 10_000)              # no rest_mode
    explicit = mf._run_policy(events, 1.0, "strict", 0, 10_000, rest_mode="cancel")
    assert default["first"] == explicit["first"] == ("H", 200, 450, 480)
    assert default["other"] == explicit["other"] == (250, 480)


# ------------------------------------------------------------- record-only ask_B size

def test_ask_B_sz_recorded_on_chase():
    pol = {"first": ("H", 200, 450, 480), "other": None}
    # 5-col timeline carries sizes: legB=L -> ask_Lsz column (index 4)
    tl = ([100, 260], [460, 460], [490, 570], [300, 300], [800, 800])
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    r = recs[0.10]                       # t_target=300 -> last frame ts260 -> ask_L size 800
    assert r["ask_B_sz"] == 800
    assert r["ask_B_target"] == 570


def test_ask_B_sz_none_on_short_timeline():
    pol = {"first": ("H", 200, 450, 480), "other": None}
    tl = ([100, 260], [460, 460], [490, 570])         # no size columns
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    assert recs[0.10]["ask_B_sz"] is None
    assert mf._asksz_at(tl, "L", 300) is None


def test_ask_B_sz_none_on_both_maker():
    pol = {"first": ("H", 200, 450, 480), "other": (250, 480)}
    tl = ([100], [460], [490], [300], [800])
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    assert recs[0.50]["both_maker"] is True and recs[0.50]["ask_B_sz"] is None
