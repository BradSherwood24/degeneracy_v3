"""Shakedown runner tests: SignalDriver drives decide() + journals WouldFire, and ShakedownRecorder
composes with the Phase-1 WindowRecorder (book folded + signal evaluated) WITHOUT modifying it."""

from __future__ import annotations

from decimal import Decimal

import pytest

from service._simlaw import close_epoch, load_ev_curve
from service.journal import Journal
from service.policy import SUB_DOLLAR_FLIP, load_policy
from service.quintile import QuintileResult
from service.shakedown import ShakedownRecorder, SignalDriver, build_shakedown_state
from service.signal import WOULD_FIRE, WindowState
from service.wake import LadderCheck, Leg, WakeResult

PARAMS = load_policy()
EV = load_ev_curve()
CT = "2026-06-14T02:00:00Z"
T = close_epoch(CT)
HI, LO = "HI-STRIKE", "LO-STRIKE"


def qresult(quintile=3):
    return QuintileResult(
        close_time=CT, T=T, anchor_A=64500.0, threshold_K=64512.0, G=Decimal("12.00"),
        sigma_hat=49.5, g_over_sigma=0.24, quintile=quintile, orientation="K_above_A",
        high_ticker=HI, low_ticker=LO,
    )


def snap(market, yes_levels, no_levels, ts):
    return {"market_ticker": market, "ts": ts,
            "yes_dollars_fp": yes_levels, "no_dollars_fp": no_levels}


def test_build_shakedown_state_seeds_signal_state():
    st = build_shakedown_state(qresult(0), EV, strangle_disabled=False)
    assert isinstance(st, WindowState)
    assert st.high_ticker == HI and st.quintile == 0 and st.shakedown is True
    assert st.fair_strangle_q == EV.fair_for("strangle", 0)


def test_signal_driver_would_fires_and_journals():
    from service.book import BookMirror
    j = Journal()
    st = build_shakedown_state(qresult(), EV, strangle_disabled=False)
    driver = SignalDriver(PARAMS, st, j, clock=lambda: 123.0)
    # high no_ask 0.57 (yes_bid .43); low yes_ask 0.24 (no_bid .76) -> flip C 0.84
    hb = BookMirror(); hb.apply_snapshot({"yes_dollars_fp": [["0.43", 10]], "no_dollars_fp": []})
    lb = BookMirror(); lb.apply_snapshot({"yes_dollars_fp": [], "no_dollars_fp": [["0.76", 10]]})
    driver.on_book_update(HI, hb.top_of_book(), T - 300)
    acts = driver.on_book_update(LO, lb.top_of_book(), T - 300)
    assert len(acts) == 1 and acts[0].kind == WOULD_FIRE and acts[0].source == SUB_DOLLAR_FLIP
    assert driver.entered and driver.fired_source == SUB_DOLLAR_FLIP
    # journaled as a would_fire record with Decimal-lossless fields
    recs = [r for r in j.records() if r["kind"] == "would_fire"]
    assert len(recs) == 1 and recs[0]["obj"]["C"] == Decimal("0.84")


def _wake_result():
    leg_hi = Leg("KXBTCD", "EV", "2026-06-14T01:00:00Z", CT, 3600, (HI,), (64512.0,), ())
    leg_lo = Leg("KXBTC15M", "EV15", "2026-06-14T01:45:00Z", CT, 900, (LO,), (64500.0,), ())
    ladder = LadderCheck(Decimal(100), Decimal(100), True, True, False, False, "ok")
    return WakeResult(close_time=CT, fifteen_leg=leg_lo, hourly_leg=leg_hi, ladder=ladder)


def test_shakedown_recorder_composes_without_modifying_recorder():
    j = Journal()
    st = build_shakedown_state(qresult(), EV, strangle_disabled=False)
    rec = ShakedownRecorder(_wake_result(), j, PARAMS, st, clock=lambda: 123.0)
    # Drive through the SAME callbacks the WS client would call (market-keyed).
    rec.callbacks.on_orderbook_snapshot(HI, snap(HI, [["0.43", 10]], [], T - 300))
    rec.callbacks.on_orderbook_snapshot(LO, snap(LO, [], [["0.76", 10]], T - 300))
    assert rec.driver.entered
    acts = rec.driver.actions
    assert len(acts) == 1 and acts[0].kind == WOULD_FIRE
    # the inherited book/replay machinery still ran: books built for both legs
    assert set(rec.books) == {HI, LO}
    assert rec.books[HI].top_of_book().no_ask == Decimal("0.57")


def test_shakedown_recorder_ignores_unrelated_ladder_strikes():
    j = Journal()
    st = build_shakedown_state(qresult(), EV, strangle_disabled=False)
    rec = ShakedownRecorder(_wake_result(), j, PARAMS, st, clock=lambda: 123.0)
    rec.callbacks.on_orderbook_snapshot("OTHER-STRIKE", snap("OTHER-STRIKE", [["0.1", 5]], [], T - 300))
    # book built (inherited), but the driver saw no paired-leg update -> not entered
    assert "OTHER-STRIKE" in rec.books and not rec.driver.entered
