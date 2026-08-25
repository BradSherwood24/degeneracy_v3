"""Signal-core tests: C/fee Decimal exactness, sub-$1 strict-< and EV-5 boundaries,
freshness/staleness fail-closed, first-entry race + window mutual exclusion, no-orders cutoff,
and golden decide() determinism."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import close_epoch, fee
from service.book import TopOfBook
from service.policy import ImbalanceBounds, PolicyParams, Q1_STRANGLE, SUB_DOLLAR_FLIP, load_policy
from service.signal import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    BookUpdate,
    ClockTick,
    WindowState,
    decide,
    flip_cost,
    strangle_cost,
)

CT = "2026-06-14T02:00:00Z"
T = close_epoch(CT)
PARAMS = load_policy()


def top(*, yes_ask=None, no_ask=None, suspect=False) -> TopOfBook:
    """Minimal top-of-book carrying just the asks decide() reads."""
    def d(x):
        return None if x is None else Decimal(str(x))
    return TopOfBook(
        yes_bid=None, yes_bid_size=None,
        yes_ask=d(yes_ask), yes_ask_size=(None if yes_ask is None else Decimal(1)),
        no_bid=None, no_bid_size=None,
        no_ask=d(no_ask), no_ask_size=(None if no_ask is None else Decimal(1)),
        suspect=suspect,
    )


def base_state(quintile=3, fair=Decimal("0.97854077253218884"), strangle_disabled=False,
               shakedown=False) -> WindowState:
    return WindowState.new(
        close_time=CT, high_ticker="HI", low_ticker="LO", quintile=quintile,
        fair_strangle_q=fair, strangle_disabled=strangle_disabled, shakedown=shakedown,
    )


def custom_params(sub_max="1.00", ev_min="0.05", fresh=1.0, no_orders=1) -> PolicyParams:
    return PolicyParams(
        roster_name="test", sub_dollar_C_max=Decimal(sub_max), q1_strangle_ev_min=Decimal(ev_min),
        freshness_max_leg_age_s=fresh, staleness_s=60,
        quintile_routing={0: (Q1_STRANGLE, SUB_DOLLAR_FLIP), 1: (SUB_DOLLAR_FLIP,),
                          2: (SUB_DOLLAR_FLIP,), 3: (SUB_DOLLAR_FLIP,), 4: (SUB_DOLLAR_FLIP,)},
        imbalance=ImbalanceBounds(Decimal("1.0320"), 5, 3, no_orders), sha256="test",
    )


def feed(st, params, market, top_ob, t_minus):
    """One BookUpdate at t_minus seconds before settlement."""
    return decide(params, st, BookUpdate(market, top_ob, T - t_minus))


# --- C / fee exactness -----------------------------------------------------
def test_fee_matches_audited_golden_literals():
    assert fee(Decimal("0.57")) == Decimal("0.0172")
    assert fee(Decimal("0.46")) == Decimal("0.0174")
    assert fee(Decimal("0.24")) == Decimal("0.0128")
    assert fee(Decimal("0.11")) == Decimal("0.0069")


def test_flip_and_strangle_cost_hand_computed():
    hi = top(yes_ask="0.60", no_ask="0.57")
    lo = top(yes_ask="0.24", no_ask="0.40")
    # flip = high NO-ask + low YES-ask (+fees): 0.57+0.0172 + 0.24+0.0128 = 0.8400
    assert flip_cost(hi, lo) == Decimal("0.8400")
    # strangle = high YES-ask + low NO-ask (+fees): 0.60+fee(.60) + 0.40+fee(.40)
    assert strangle_cost(hi, lo) == Decimal("0.60") + fee(Decimal("0.60")) + Decimal("0.40") + fee(Decimal("0.40"))


def test_missing_ask_gives_none_cost():
    assert flip_cost(top(no_ask=None), top(yes_ask="0.2")) is None
    assert strangle_cost(top(yes_ask="0.2"), top(no_ask=None)) is None


# --- sub-$1 strict-< boundary ----------------------------------------------
def test_sub_dollar_strict_less_than_boundary():
    hi = top(no_ask="0.57")
    lo = top(yes_ask="0.24")
    c = flip_cost(hi, lo)  # 0.8400
    # C == C_max -> NOT fire (strict <)
    p_eq = custom_params(sub_max=str(c))
    st = base_state()
    st, _ = feed(st, p_eq, "HI", hi, 300)
    st, acts = feed(st, p_eq, "LO", lo, 300)
    assert acts == []
    # C_max a hair above C -> fire
    p_gt = custom_params(sub_max=str(c + Decimal("0.0001")))
    st = base_state()
    st, _ = feed(st, p_gt, "HI", hi, 300)
    st, acts = feed(st, p_gt, "LO", lo, 300)
    assert len(acts) == 1 and acts[0].kind == FIRE and acts[0].source == SUB_DOLLAR_FLIP
    assert acts[0].C == c


def test_sub_dollar_fires_below_one_dollar():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert len(acts) == 1 and acts[0].source == SUB_DOLLAR_FLIP
    legs = {lg.ticker: lg for lg in acts[0].legs}
    assert legs["HI"].side == "no" and legs["HI"].limit_price == Decimal("0.57")
    assert legs["LO"].side == "yes" and legs["LO"].limit_price == Decimal("0.24")


# --- EV-5 boundary (strangle, q0) ------------------------------------------
def test_ev_min_boundary_exact_and_below():
    # asks chosen so flip C >= $1 (sub-$1 does NOT qualify) -> the strangle is isolated.
    hi = top(yes_ask="0.50", no_ask="0.60")
    lo = top(yes_ask="0.60", no_ask="0.40")
    c_str = strangle_cost(hi, lo)
    # fair set so ev == exactly 0.05 -> fires (>=)
    st = base_state(quintile=0, fair=c_str + Decimal("0.05"))
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert len(acts) == 1 and acts[0].source == Q1_STRANGLE and acts[0].ev == Decimal("0.05")
    # fair a hair lower so ev == 0.0499 -> no fire
    st = base_state(quintile=0, fair=c_str + Decimal("0.0499"))
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert acts == []


def test_strangle_only_in_q0():
    # flip C >= $1 (isolate the strangle); quintile 1 routes NO strangle -> nothing fires.
    hi = top(yes_ask="0.50", no_ask="0.60")
    lo = top(yes_ask="0.60", no_ask="0.40")
    st = base_state(quintile=1, fair=Decimal("2.0"))  # huge fair would make ev huge if evaluated
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert acts == []


def test_strangle_disabled_by_ladder_stands_down_strangle():
    hi = top(yes_ask="0.50", no_ask="0.60")  # flip C >= $1, so only the strangle could fire
    lo = top(yes_ask="0.60", no_ask="0.40")
    c_str = strangle_cost(hi, lo)
    st = base_state(quintile=0, fair=c_str + Decimal("0.10"), strangle_disabled=True)
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert acts == []


# --- freshness / staleness fail-closed -------------------------------------
def test_unknown_age_never_fires():
    # Only ONE leg ever updated -> the other leg's age is unknown -> stale -> no fire.
    hi = top(no_ask="0.57")
    st = base_state()
    st, acts = feed(st, PARAMS, "HI", hi, 300)
    assert acts == []
    assert st.low_ts is None


def test_stale_leg_blocks_fire():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    # HI updates at t-320, LO updates at t-300 -> HI age = 20s > 1s freshness -> no fire.
    st, _ = decide(PARAMS, st, BookUpdate("HI", hi, T - 320))
    st, acts = decide(PARAMS, st, BookUpdate("LO", lo, T - 300))
    assert acts == []


def test_suspect_book_blocks_fire():
    hi = top(no_ask="0.57", suspect=True)
    lo = top(yes_ask="0.24")
    st = base_state()
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert acts == []


def test_future_timestamp_is_failclosed():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    st, _ = decide(PARAMS, st, BookUpdate("HI", hi, T - 300))
    # LO stamped in the FUTURE relative to HI's now -> HI age negative -> unknown -> no fire
    st, acts = decide(PARAMS, st, BookUpdate("LO", lo, T - 305))
    assert acts == []


# --- first-entry race + window mutual exclusion ----------------------------
def test_sub_dollar_wins_same_event_tie():
    # In q0 both could qualify on the SAME event; sub-$1 flip has priority.
    hi = top(yes_ask="0.10", no_ask="0.40")  # flip C = 0.40+fee + (low yes) ; strangle C small too
    lo = top(yes_ask="0.40", no_ask="0.10")
    st = base_state(quintile=0, fair=Decimal("2.0"))
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert len(acts) == 1 and acts[0].source == SUB_DOLLAR_FLIP


def test_window_entered_once_then_done():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert len(acts) == 1 and st.entered
    # a further qualifying update produces nothing
    st, acts2 = feed(st, PARAMS, "LO", lo, 250)
    assert acts2 == []


# --- no-orders-after-settle cutoff -----------------------------------------
def test_no_orders_cutoff_stands_down():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    # First event already inside the cutoff (t-0.5s < 1s) -> StandDown emitted here.
    st, acts = decide(PARAMS, st, BookUpdate("HI", hi, T - 0.5))
    assert len(acts) == 1 and acts[0].kind == STAND_DOWN
    # StandDown emitted only once
    st, acts2 = decide(PARAMS, st, BookUpdate("LO", lo, T - 0.5))
    assert acts2 == []
    st, acts3 = decide(PARAMS, st, ClockTick(T - 0.4))
    assert acts3 == []


def test_warmup_before_window_does_not_fire():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state()
    # t_minus 1000 > WINDOW_S (900): seed only, no fire
    st, _ = feed(st, PARAMS, "HI", hi, 1000)
    st, acts = feed(st, PARAMS, "LO", lo, 1000)
    assert acts == []
    # inside the window it fires
    st, _ = feed(st, PARAMS, "HI", hi, 800)
    st, acts = feed(st, PARAMS, "LO", lo, 800)
    assert len(acts) == 1


def test_unrelated_leg_ignored():
    st = base_state()
    st2, acts = feed(st, PARAMS, "OTHER-STRIKE", top(no_ask="0.1"), 300)
    assert acts == [] and st2.high_top is None and st2.low_top is None


# --- shakedown mode --------------------------------------------------------
def test_shakedown_emits_would_fire_not_fire():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    st = base_state(shakedown=True)
    st, _ = feed(st, PARAMS, "HI", hi, 300)
    st, acts = feed(st, PARAMS, "LO", lo, 300)
    assert len(acts) == 1 and acts[0].kind == WOULD_FIRE


# --- golden determinism ----------------------------------------------------
def _run_stream(params, st0, stream):
    st = st0
    out = []
    for ev in stream:
        st, acts = decide(params, st, ev)
        out.extend(acts)
    return out


def test_decide_is_deterministic_over_identical_streams():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    stream = [
        BookUpdate("HI", top(no_ask="0.60"), T - 500),
        BookUpdate("LO", top(yes_ask="0.30"), T - 480),
        ClockTick(T - 460),
        BookUpdate("HI", hi, T - 300),
        BookUpdate("LO", lo, T - 300),
        BookUpdate("LO", lo, T - 200),
    ]
    a = _run_stream(PARAMS, base_state(), stream)
    b = _run_stream(PARAMS, base_state(), stream)
    assert a == b
    assert len([x for x in a if x.kind == FIRE]) == 1
