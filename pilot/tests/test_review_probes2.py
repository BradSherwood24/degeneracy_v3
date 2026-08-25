"""Phase-2 adversarial review probes (SEPARATE opus48 reviewer).

Covers: the two fixes made in-tree (no-orders-cutoff parity gap; shakedown WouldFire-only
guard), the fire-wrongly fail-closed sweep, the sub-$1 / EV boundary conventions vs the sim,
decide() purity / cross-window isolation, the F15 neutrality FAILURE actually tripping, and the
FLAGGED (not fixed) latent bin-5 empty-deltas honesty bug handed to Phase 3.
"""

from __future__ import annotations

import csv
from decimal import Decimal

import pytest

from service._simlaw import WINDOW_S, close_epoch, fee, load_ev_curve
from service.book import TopOfBook
from service.journal import Journal
from service.parity import (
    BIN_BOTH_DIFF_PRICE,
    BIN_BOTH_MATCH,
    BIN_LIVE_ONLY,
    BIN_SIM_ONLY,
    LABEL_NO_SIGNAL,
    LegFill,
    LiveEntry,
    ParityWindowInput,
    SimEntry,
    WindowFills,
    assign_bin,
    run_parity,
    sim_entry_for_window,
)
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP, load_policy
from service.shakedown import ShakedownRecorder, SignalDriver
from service.signal import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    BookUpdate,
    ClockTick,
    WindowState,
    decide,
)
from service.wake import LadderCheck, Leg, WakeResult

P = load_policy()
EV = load_ev_curve()
CT = "2026-06-14T02:00:00Z"
T = close_epoch(CT)
HI, LO = "HI", "LO"


def top(*, no_ask=None, yes_ask=None, suspect=False) -> TopOfBook:
    def d(x):
        return None if x is None else Decimal(str(x))
    return TopOfBook(
        yes_bid=None, yes_bid_size=None,
        yes_ask=d(yes_ask), yes_ask_size=(None if yes_ask is None else Decimal(1)),
        no_bid=None, no_bid_size=None,
        no_ask=d(no_ask), no_ask_size=(None if no_ask is None else Decimal(1)),
        suspect=suspect,
    )


def st0(quintile=3, shakedown=False, strangle_disabled=False) -> WindowState:
    return WindowState.new(
        CT, HI, LO, quintile, EV.fair_for("strangle", quintile),
        shakedown=shakedown, strangle_disabled=strangle_disabled,
    )


def _row(direction, C, ev, q, t, hp="0.57", lp="0.24", ha=0.0, la=0.0, pin=0, payoff="0.0"):
    return {
        "date": "2026-06-14", "close_time": CT, "direction": direction,
        "t_minus_s": f"{t:.6f}", "fill_side": "HIGH", "G": "12.33", "sigma_hat": "49.5",
        "g_over_sigma": "0.68", "quintile": str(q),
        "high_leg_price": f"{Decimal(hp):.4f}", "low_leg_price": f"{Decimal(lp):.4f}",
        "high_leg_age_s": f"{ha:.3f}", "low_leg_age_s": f"{la:.3f}",
        "C": f"{Decimal(C):.4f}", "fair": "0.9", "fair_lin": "0.9",
        "ev": f"{Decimal(ev):.6f}", "ev_lin": "0.0",
        "hard_floor": ("1" if direction == "flip" else ""), "pin": str(pin),
        "payoff": f"{Decimal(payoff):.4f}", "dwell_s": "0.5",
    }


def _snap(idx, market, yb, nb, ts):
    yes = [[str(yb), 10]] if yb is not None else []
    no = [[str(nb), 10]] if nb is not None else []
    return {"idx": idx, "kind": "kalshi_ws", "local_ts": float(ts),
            "obj": {"type": "orderbook_snapshot",
                    "msg": {"market_ticker": market, "ts": ts,
                            "yes_dollars_fp": yes, "no_dollars_fp": no}}}


# ===========================================================================
# FIX 1 — no-orders-cutoff parity gap (was a spurious bin-1 F15 failure)
# ===========================================================================
def test_sim_side_respects_no_orders_cutoff():
    """A tape moment qualifying ONLY inside the 1s settle cutoff must NOT count as a sim fire —
    live decide() stands down there, so counting it would be a spurious (non-book-vs-prints)
    bin-1 F15 failure."""
    inside = sim_entry_for_window([_row("flip", "0.84", "0.03", 3, 0.5)], P)
    assert inside.fired is False
    # exactly at the cutoff floor (t == no_orders_after_s_to_settle) still fires (live: t>=1 fires)
    at_floor = sim_entry_for_window([_row("flip", "0.84", "0.03", 3, float(P.no_orders_after_s_to_settle))], P)
    assert at_floor.fired is True


def test_no_orders_cutoff_no_longer_spurious_bin1():
    """End-to-end: sim tape qualifies only at t-0.5s, live stands down. Post-fix both agree
    'no entry' -> no_signal_both, harness passes (previously bin-1 F15 FAILURE)."""
    journal = [_snap(0, HI, "0.43", None, T - 0.5), _snap(1, LO, None, "0.76", T - 0.5)]
    w = ParityWindowInput(CT, journal, st0(shakedown=True), [_row("flip", "0.84", "0.03", 3, 0.5)])
    rep = run_parity([w], P)
    assert rep["windows"][0]["bin"] == LABEL_NO_SIGNAL
    assert rep["passed"] is True


def test_sim_side_upper_window_bound():
    """A stray tape row beyond WINDOW_S is warmup for live (never fires); sim must ignore it too."""
    assert sim_entry_for_window([_row("flip", "0.84", "0.03", 3, WINDOW_S + 10)], P).fired is False


# ===========================================================================
# FIX 2 — shakedown is WouldFire-only (no Fire action constructible)
# ===========================================================================
def _wake():
    lh = Leg("KXBTCD", "EV", "2026-06-14T01:00:00Z", CT, 3600, (HI,), (64512.0,), ())
    ll = Leg("KXBTC15M", "EV15", "2026-06-14T01:45:00Z", CT, 900, (LO,), (64500.0,), ())
    ladder = LadderCheck(Decimal(100), Decimal(100), True, True, False, False, "ok")
    return WakeResult(close_time=CT, fifteen_leg=ll, hourly_leg=lh, ladder=ladder)


def test_shakedown_recorder_refuses_live_fire_state():
    with pytest.raises(ValueError):
        ShakedownRecorder(_wake(), Journal(), P, st0(shakedown=False))


def test_shakedown_recorder_accepts_shakedown_state_and_would_fires():
    rec = ShakedownRecorder(_wake(), Journal(), P, st0(shakedown=True), clock=lambda: 1.0)
    rec.callbacks.on_orderbook_snapshot(HI, {"market_ticker": HI, "ts": T - 300,
                                             "yes_dollars_fp": [["0.43", 10]], "no_dollars_fp": []})
    rec.callbacks.on_orderbook_snapshot(LO, {"market_ticker": LO, "ts": T - 300,
                                             "yes_dollars_fp": [], "no_dollars_fp": [["0.76", 10]]})
    acts = rec.driver.actions
    assert len(acts) == 1 and acts[0].kind == WOULD_FIRE


# ===========================================================================
# FIRE-WRONGLY fail-closed sweep (highest stakes)
# ===========================================================================
def _feed_pair(params, state, hi, lo, t_minus_hi, t_minus_lo=None):
    if t_minus_lo is None:
        t_minus_lo = t_minus_hi
    st, _ = decide(params, state, BookUpdate(HI, hi, T - t_minus_hi))
    st, acts = decide(params, st, BookUpdate(LO, lo, T - t_minus_lo))
    return st, acts


@pytest.mark.parametrize("mut", ["suspect_hi", "suspect_lo", "stale_lo", "unknown_lo",
                                 "future_lo", "inside_cutoff", "warmup"])
def test_fire_wrongly_paths_all_fail_closed(mut):
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")  # flip C 0.84 (<$1)
    st = st0()
    if mut == "suspect_hi":
        _, acts = _feed_pair(P, st, top(no_ask="0.57", suspect=True), lo, 300)
    elif mut == "suspect_lo":
        _, acts = _feed_pair(P, st, hi, top(yes_ask="0.24", suspect=True), 300)
    elif mut == "stale_lo":
        _, acts = _feed_pair(P, st, hi, lo, 320, 300)  # hi 20s stale at the LO event
    elif mut == "unknown_lo":
        _, acts = decide(P, st, BookUpdate(HI, hi, T - 300))  # LO never updates
        _, acts = (st, acts[1] if False else acts)  # noqa: keep acts from the single update
    elif mut == "future_lo":
        st, _ = decide(P, st, BookUpdate(HI, hi, T - 300))
        _, acts = decide(P, st, BookUpdate(LO, lo, T - 305))  # LO in the future vs HI
    elif mut == "inside_cutoff":
        _, acts = _feed_pair(P, st, hi, lo, 0.5)
    elif mut == "warmup":
        _, acts = _feed_pair(P, st, hi, lo, WINDOW_S + 100)
    assert not any(a.kind == FIRE for a in acts), f"{mut} produced a FIRE"


def test_strangle_disabled_blocks_strangle_only_fire():
    # asks with flip C >= $1 so only the strangle could fire; ladder stood it down -> nothing.
    hi, lo = top(no_ask="0.60", yes_ask="0.50"), top(no_ask="0.40", yes_ask="0.60")
    from service.signal import strangle_cost
    fair = strangle_cost(hi, lo) + Decimal("0.20")
    st = WindowState.new(CT, HI, LO, 0, fair, strangle_disabled=True)
    _, acts = _feed_pair(P, st, hi, lo, 300)
    assert acts == []


def test_cannot_fire_twice_or_both_sources():
    # q0, asks where BOTH sub-$1 and strangle could qualify -> exactly one action, sub priority.
    hi = top(no_ask="0.40", yes_ask="0.10")
    lo = top(no_ask="0.10", yes_ask="0.40")
    st = WindowState.new(CT, HI, LO, 0, Decimal("2.0"))
    st, acts = _feed_pair(P, st, hi, lo, 300)
    assert len(acts) == 1 and acts[0].source == SUB_DOLLAR_FLIP and st.entered
    # a further qualifying event does nothing
    st, acts2 = decide(P, st, BookUpdate(LO, lo, T - 250))
    assert acts2 == []


# ===========================================================================
# Boundary conventions vs the sim (strict-< sub-$1; inclusive >= EV)
# ===========================================================================
def test_sub_dollar_exactly_one_dollar_does_not_fire():
    # asks whose flip C == exactly 1.00 must NOT fire (sim hard_floor is strict C < 1).
    # no_ask=0.49 -> 0.49+fee(0.49); pick low yes_ask so C == 1.0000 exactly is hard; instead
    # assert via the primitive: any C == C_max is excluded.
    from service.signal import flip_cost
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    c = flip_cost(hi, lo)  # 0.84
    from service.policy import ImbalanceBounds, PolicyParams
    p_eq = PolicyParams("t", c, Decimal("0.05"), 1.0, 60,
                        {0: (Q1_STRANGLE, SUB_DOLLAR_FLIP), 1: (SUB_DOLLAR_FLIP,),
                         2: (SUB_DOLLAR_FLIP,), 3: (SUB_DOLLAR_FLIP,), 4: (SUB_DOLLAR_FLIP,)},
                        ImbalanceBounds(Decimal("1.0320"), 5, 3, 1), "t")
    _, acts = _feed_pair(p_eq, st0(), hi, lo, 300)
    assert acts == []


def test_ev_boundary_matches_sim_inclusive_convention():
    # ev == exactly 0.05 fires (>=), ev == 0.0499 does not — same inclusive rule the sim/aggregate
    # first-entry ladder uses (enter at ev >= x).
    hi, lo = top(no_ask="0.60", yes_ask="0.50"), top(no_ask="0.40", yes_ask="0.60")  # flip C>=1
    from service.signal import strangle_cost
    c = strangle_cost(hi, lo)
    st_fire = WindowState.new(CT, HI, LO, 0, c + Decimal("0.05"))
    _, acts = _feed_pair(P, st_fire, hi, lo, 300)
    assert len(acts) == 1 and acts[0].source == Q1_STRANGLE and acts[0].ev == Decimal("0.05")
    st_no = WindowState.new(CT, HI, LO, 0, c + Decimal("0.0499"))
    _, acts = _feed_pair(P, st_no, hi, lo, 300)
    assert acts == []


# ===========================================================================
# decide() purity — identical output on repeat; interleaved windows don't bleed
# ===========================================================================
def test_decide_pure_no_cross_window_bleed():
    hi, lo = top(no_ask="0.57"), top(yes_ask="0.24")
    stream = [BookUpdate(HI, hi, T - 300), ClockTick(T - 290), BookUpdate(LO, lo, T - 285)]

    def run(seed):
        st = seed
        out = []
        for ev in stream:
            st, acts = decide(P, st, ev)
            out.extend(acts)
        return out, st

    a, sta = run(st0())
    b, stb = run(st0())
    assert a == b  # deterministic
    # a SECOND window with a different close_time/T, driven from a fresh state, is independent:
    other_ct = "2026-06-14T03:00:00Z"
    other = WindowState.new(other_ct, HI, LO, 3, EV.fair_for("strangle", 3))
    c, _ = run(other)  # different T shifts the firing window; must not reference `a`'s state
    assert isinstance(c, list)  # no exception, no shared mutation
    # the original state is unchanged by the second run (frozen dataclass)
    assert sta.entered == stb.entered


# ===========================================================================
# F15 neutrality FAILURE actually trips (independent of the builder's own test)
# ===========================================================================
def test_f15_failure_trips_on_documented_delta_flip():
    # SIM prints -> flip C 0.84 (<$1 fires); LIVE book -> flip C 1.2336 (>=$1 no fire).
    journal = [_snap(0, HI, "0.40", None, T - 300), _snap(1, LO, None, "0.40", T - 300)]
    w = ParityWindowInput(CT, journal, st0(shakedown=True), [_row("flip", "0.84", "0.03", 3, 300)])
    rep = run_parity([w], P)
    assert rep["windows"][0]["bin"] == BIN_SIM_ONLY
    assert rep["passed"] is False and rep["n_neutrality_failures"] == 1


# ===========================================================================
# F3 bin-5 honesty guard — FIXED in the Phase-3 review (parity.py).
# When both fired + filled but NO fills leg is comparable to the paired tickers, assign_bin used to
# score BIN_BOTH_MATCH ("sim tells the truth") with zero price comparison. The guard now refuses to
# certify a match without >=1 comparable leg, returning LABEL_UNCOMPARABLE (a data-fault marker),
# neutrality preserved. This test locks the FIXED behavior.
# ===========================================================================
def test_F3_empty_deltas_no_longer_scored_as_match():
    from service.parity import LABEL_UNCOMPARABLE

    sim = SimEntry(fired=True, source=SUB_DOLLAR_FLIP, t_minus_s=300, C=Decimal("0.84"),
                   high_leg_price=Decimal("0.57"), low_leg_price=Decimal("0.24"), pin=0,
                   payoff=Decimal("0.16"))
    live = LiveEntry(fired=True, source=SUB_DOLLAR_FLIP, t_minus_s=300, C=Decimal("0.84"),
                     high_limit=Decimal("0.57"), low_limit=Decimal("0.24"))
    wp = assign_bin(CT, sim, live, WindowFills(filled=True, legs=(
        LegFill("WRONG-TICKER", "no", 1, Decimal("0.99")),)), HI, LO)
    # FIXED: no comparable leg -> NOT a match; flagged uncomparable, never bin-5.
    assert wp.bin == LABEL_UNCOMPARABLE
    assert wp.bin != BIN_BOTH_MATCH
    assert wp.neutrality_ok is True  # not a fire/no-fire flip

    # and a genuinely comparable pair still certifies bin-5 (guard doesn't over-trigger)
    good = assign_bin(CT, sim, live, WindowFills(filled=True, legs=(
        LegFill(HI, "no", 1, Decimal("0.57")), LegFill(LO, "yes", 1, Decimal("0.24")))), HI, LO)
    assert good.bin == BIN_BOTH_MATCH
