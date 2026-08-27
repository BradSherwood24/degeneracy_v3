"""Fill-rule tests (STRICT trade-through + LENIENT queue, both legs), the fee/maker-mult
cost math, and the chase-vs-both-maker branch of the maker-flip policy."""

import os
import sys

_SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SIM_DIR)
for p in (_SIM_DIR, _REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

from replay import maker_flip as mf   # noqa: E402


def _rest_frame(ts=100, idx=1, dqH=0, dqL=0):
    # no_ask_H=0.46 -> qH=0.45 ; yes_ask_L=0.49 -> qL=0.48 ; C=0.9849 <= 1.0
    return ("B", ts, idx, 460, 490, 450, 480, dqH, dqL)


def test_strict_through_both_legs():
    events = [
        _rest_frame(),
        ("T", 200, 2, "H", "yes", 560, 1.0),   # 1-p=0.44 < 0.45 -> through our NO bid @0.45
        ("T", 250, 3, "L", "no", 470, 1.0),    # p=0.47 < 0.48 -> through our YES bid @0.48
    ]
    pol = mf._run_policy(events, theta=1.0, fill_rule="strict", t_start_ms=0, t_end_ms=10_000)
    assert pol["first"][0] == "H" and pol["first"][1] == 200 and pol["first"][2] == 450
    assert pol["other"] == (250, 480)
    assert pol["any_fill"] is True


def test_strict_does_not_fill_at_level_but_lenient_does():
    # taker_side "yes" at yes_price == 1 - q (at our level, NOT through)
    at_level = [_rest_frame(dqH=0), ("T", 200, 2, "H", "yes", 550, 1.0)]  # ypm==1000-450
    pol_s = mf._run_policy(at_level, 1.0, "strict", 0, 10_000)
    assert pol_s["first"] is None                     # strict needs a trade-through
    pol_l = mf._run_policy(at_level, 1.0, "lenient", 0, 10_000)
    assert pol_l["first"][0] == "H"                    # lenient: queue_ahead 0, one at-level print fills


def test_lenient_queue_needs_queue_ahead_plus_one():
    # queue_ahead = 2 contracts (dqH=200 hundredths). Need cumulative >= 3 at our level.
    ev = [
        _rest_frame(dqH=200),
        ("T", 150, 2, "H", "yes", 550, 1.0),   # cum=1 (<3) no fill
        ("T", 160, 3, "H", "yes", 550, 1.0),   # cum=2 (<3) no fill
        ("T", 170, 4, "H", "yes", 550, 1.5),   # cum=3.5 (>=3) FILL
    ]
    pol = mf._run_policy(ev, 1.0, "lenient", 0, 10_000)
    assert pol["first"] == ("H", 170, 450, 480)   # qB_rest (our L bid) = 480 at t_A


def test_lenient_L_leg_queue_and_theta_gate():
    # YES bid on L at 0.48; consumed by taker_side "no" prints at yes_price == 0.48
    ev = [_rest_frame(dqL=0), ("T", 120, 2, "L", "no", 480, 1.0)]
    pol = mf._run_policy(ev, 1.0, "lenient", 0, 10_000)
    assert pol["first"][0] == "L" and pol["first"][2] == 480
    # theta gate: with theta below C the bid is never rested -> no fill
    pol2 = mf._run_policy(ev, theta=0.90, fill_rule="lenient", t_start_ms=0, t_end_ms=10_000)
    assert pol2["first"] is None
    assert pol2["episodes"] == 0


def test_both_maker_cost_and_maker_mult():
    pol = {"first": ("H", 200, 450, 480), "other": (250, 480)}
    tl = ([100], [460], [490])
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    r = recs[0.10]                          # 200+100=300 >= t_B 250 -> both maker
    assert r["both_maker"] is True
    # mult 0.25: 0.45 + 0.25*fee(0.45) + 0.48 + 0.25*fee(0.48)
    exp025 = 0.45 + 0.25 * float(mf.fee(mf.mils_to_dollars(450))) \
        + 0.48 + 0.25 * float(mf.fee(mf.mils_to_dollars(480)))
    assert abs(r["costs"][0.25] - exp025) < 1e-9
    # mult 0.0: pure prices, no fees
    assert abs(r["costs"][0.0] - (0.45 + 0.48)) < 1e-12


def test_chase_branch_and_gap():
    # leg A = H @0.45 at t_A=200; leg B = L never makes; yes_ask_L rises 0.49 -> 0.57 after t_A.
    pol = {"first": ("H", 200, 450, 480), "other": None}
    tl = ([100, 210, 260], [460, 460, 460], [490, 570, 560])
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    r010 = recs[0.10]                       # t_target=300 -> ask_L=560 (last <=300 is ts260)
    assert r010["both_maker"] is False and r010["unhedged"] is False
    assert r010["ask_B_target"] == 560
    assert r010["ask_B_tA"] == 490          # ask_L as of t_A=200 (ts100 frame)
    # chase gap = 0.56 - 0.49 = 0.07 ; vs rest = 0.56 - (0.48 + 0.01) = 0.07
    assert abs(r010["chase_gap"] - 0.07) < 1e-9
    assert abs(r010["chase_gap_vs_rest"] - 0.07) < 1e-9
    # taker fee applied on the chase leg (full fee, not maker)
    exp = 0.45 + 0.25 * float(mf.fee(mf.mils_to_dollars(450))) \
        + 0.56 + float(mf.fee(mf.mils_to_dollars(560)))
    assert abs(r010["costs"][0.25] - exp) < 1e-9


def test_chase_unhedged_when_ask_absent():
    pol = {"first": ("L", 200, 480, 450), "other": None}
    # leg B = H ; no_ask_H (ask_H) is None at/after t_target -> unhedged
    tl = ([100, 300], [460, None], [490, 490])
    recs = {r["delta"]: r for r in mf._outcomes_for_config(pol, tl)}
    r = recs[0.50]                          # t_target=700 -> last frame ts300 ask_H=None
    assert r["unhedged"] is True
    assert r["costs"][0.25] is None and r["costs"][0.0] is None


def test_fee_import_matches_census():
    # the fee must be the frozen census fee, never retyped
    import census
    for p in (0.46, 0.49, 0.11, 0.57, 0.24):
        assert mf._fee_mils(int(round(p * 1000))) == float(census.fee(p))
