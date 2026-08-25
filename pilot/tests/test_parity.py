"""Five-bin parity harness tests: the sim-side tape roster, the live-side journal replay, every
bin (1-5 + no-signal + both-fired + imbalance), and the F15 neutrality FAILURE when a documented
book-vs-prints delta flips a fire/no-fire decision."""

from __future__ import annotations

import csv
from decimal import Decimal

import pytest

from service._simlaw import TAPE_FIELDNAMES, close_epoch, load_ev_curve
from service.parity import (
    BIN_BOTH_DIFF_PRICE,
    BIN_BOTH_MATCH,
    BIN_BOTH_NO_FILL,
    BIN_LIVE_ONLY,
    BIN_SIM_ONLY,
    LABEL_BOTH_FIRED,
    LABEL_IMBALANCE,
    LABEL_NO_SIGNAL,
    LegFill,
    ParityWindowInput,
    SimEntry,
    WindowFills,
    assign_bin,
    live_entry_for_window,
    load_sim_window,
    run_parity,
    sim_entry_for_window,
)
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP, load_policy
from service.signal import WindowState

PARAMS = load_policy()
EV = load_ev_curve()
CT = "2026-06-14T02:00:00Z"
T = close_epoch(CT)
HI, LO = "HI-STRIKE", "LO-STRIKE"


# ---------------------------------------------------------------------------
# fixtures: tape rows + journal records
# ---------------------------------------------------------------------------
def make_row(direction, C, ev, quintile, t_minus, high_price, low_price, ha=0.0, la=0.0,
             pin=0, payoff="0.0"):
    return {
        "date": "2026-06-14", "close_time": CT, "direction": direction,
        "t_minus_s": f"{t_minus:.6f}", "fill_side": "HIGH", "G": "12.33",
        "sigma_hat": "49.542147", "g_over_sigma": "0.686486", "quintile": str(quintile),
        "high_leg_price": f"{Decimal(high_price):.4f}", "low_leg_price": f"{Decimal(low_price):.4f}",
        "high_leg_age_s": f"{ha:.3f}", "low_leg_age_s": f"{la:.3f}",
        "C": f"{Decimal(C):.4f}", "fair": "0.9", "fair_lin": "0.9",
        "ev": f"{Decimal(ev):.6f}", "ev_lin": "0.0",
        "hard_floor": ("1" if direction == "flip" else ""), "pin": str(pin),
        "payoff": f"{Decimal(payoff):.4f}", "dwell_s": "0.500",
    }


def snapshot_record(idx, market, yes_levels, no_levels, ts):
    return {
        "idx": idx, "kind": "kalshi_ws", "local_ts": float(ts),
        "obj": {
            "type": "orderbook_snapshot",
            "msg": {"market_ticker": market, "ts": ts,
                    "yes_dollars_fp": yes_levels, "no_dollars_fp": no_levels},
        },
    }


def journal_for(high_yes_bid=None, high_no_bid=None, low_yes_bid=None, low_no_bid=None,
                t_minus=300):
    """Build a two-leg snapshot journal. Asks derive as 1 - opposite bid:
      high no_ask  = 1 - high_yes_bid ; high yes_ask = 1 - high_no_bid
      low yes_ask  = 1 - low_no_bid  ; low no_ask  = 1 - low_yes_bid
    """
    ts = T - t_minus
    recs = []
    hy = [[str(high_yes_bid), 10]] if high_yes_bid is not None else []
    hn = [[str(high_no_bid), 10]] if high_no_bid is not None else []
    ly = [[str(low_yes_bid), 10]] if low_yes_bid is not None else []
    ln = [[str(low_no_bid), 10]] if low_no_bid is not None else []
    recs.append(snapshot_record(0, HI, hy, hn, ts))
    recs.append(snapshot_record(1, LO, ly, ln, ts))
    return recs


def state0(quintile=3, shakedown=True):
    return WindowState.new(
        close_time=CT, high_ticker=HI, low_ticker=LO, quintile=quintile,
        fair_strangle_q=EV.fair_for("strangle", quintile), shakedown=shakedown,
    )


# ---------------------------------------------------------------------------
# sim-side reader
# ---------------------------------------------------------------------------
def test_load_sim_window_streams_and_filters(tmp_path):
    p = tmp_path / "tape.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TAPE_FIELDNAMES)
        w.writeheader()
        w.writerow(make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24"))
        w.writerow({**make_row("flip", "0.90", "0.02", 3, 200, "0.6", "0.3"),
                    "close_time": "2026-06-14T03:00:00Z"})
    rows = load_sim_window(str(p), CT)
    assert len(rows) == 1 and rows[0]["close_time"] == CT


def test_sim_entry_sub_dollar_flip():
    rows = [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24")]
    e = sim_entry_for_window(rows, PARAMS)
    assert e.fired and e.source == SUB_DOLLAR_FLIP and e.C == Decimal("0.84")


def test_sim_entry_strangle_q0():
    rows = [
        make_row("flip", "1.20", "0.00", 0, 300, "0.6", "0.6"),   # sub does NOT qualify
        make_row("strangle", "0.92", "0.06", 0, 280, "0.50", "0.40"),
    ]
    e = sim_entry_for_window(rows, PARAMS)
    assert e.fired and e.source == Q1_STRANGLE


def test_sim_entry_stale_rows_do_not_fire():
    rows = [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24", ha=2.0, la=0.0)]
    assert sim_entry_for_window(rows, PARAMS).fired is False


def test_sim_entry_earliest_moment_wins_tiebreak_sub():
    # sub qualifies at t-300, strangle at t-320 (earlier). Earlier wins UNLESS tie -> here
    # strangle is earlier so strangle wins; make them tie to check sub priority.
    rows_tie = [
        make_row("flip", "0.84", "0.03", 0, 300, "0.57", "0.24"),
        make_row("strangle", "0.90", "0.08", 0, 300, "0.50", "0.40"),
    ]
    assert sim_entry_for_window(rows_tie, PARAMS).source == SUB_DOLLAR_FLIP
    rows_str_earlier = [
        make_row("flip", "0.84", "0.03", 0, 300, "0.57", "0.24"),
        make_row("strangle", "0.90", "0.08", 0, 320, "0.50", "0.40"),
    ]
    assert sim_entry_for_window(rows_str_earlier, PARAMS).source == Q1_STRANGLE


# ---------------------------------------------------------------------------
# live-side replay
# ---------------------------------------------------------------------------
def test_live_entry_fires_flip_from_book():
    # high no_ask 0.57 (yes_bid .43), low yes_ask 0.24 (no_bid .76) -> flip C 0.84
    recs = journal_for(high_yes_bid="0.43", low_no_bid="0.76")
    e = live_entry_for_window(recs, state0(), PARAMS)
    assert e.fired and e.source == SUB_DOLLAR_FLIP and e.C == Decimal("0.84")
    assert e.high_limit == Decimal("0.57") and e.low_limit == Decimal("0.24")


def test_live_entry_no_fire_when_book_cost_ge_one():
    # high no_ask 0.60 (yes_bid .40), low yes_ask 0.60 (no_bid .40) -> flip C 1.2336
    recs = journal_for(high_yes_bid="0.40", low_no_bid="0.40")
    assert live_entry_for_window(recs, state0(), PARAMS).fired is False


# ---------------------------------------------------------------------------
# binning — the five bins + labels
# ---------------------------------------------------------------------------
def _win(close_time, journal, sim_rows, quintile=3, fills=None):
    return ParityWindowInput(close_time, journal, state0(quintile), sim_rows, fills)


def test_bin_no_signal_both():
    w = _win(CT, journal_for(high_yes_bid="0.40", low_no_bid="0.40"),
             [make_row("flip", "1.20", "0.0", 3, 300, "0.6", "0.6")])
    rep = run_parity([w], PARAMS)
    assert rep["windows"][0]["bin"] == LABEL_NO_SIGNAL and rep["passed"]


def test_bin1_sim_only_is_neutrality_failure_book_vs_prints():
    # SIM prints say flip C 0.84 (<$1 -> fires); LIVE book says flip C 1.2336 (>=$1 -> no fire).
    # A documented book-vs-prints delta flipping the decision is an F15 FAILURE.
    w = _win(CT, journal_for(high_yes_bid="0.40", low_no_bid="0.40"),
             [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24")])
    rep = run_parity([w], PARAMS)
    assert rep["windows"][0]["bin"] == BIN_SIM_ONLY
    assert rep["windows"][0]["neutrality_ok"] is False
    assert rep["passed"] is False and rep["n_neutrality_failures"] == 1


def test_bin2_live_only_is_neutrality_failure():
    # LIVE book flip C 0.84 (fires); SIM has no qualifying row (C>=$1).
    w = _win(CT, journal_for(high_yes_bid="0.43", low_no_bid="0.76"),
             [make_row("flip", "1.20", "0.0", 3, 300, "0.6", "0.6")])
    rep = run_parity([w], PARAMS)
    assert rep["windows"][0]["bin"] == BIN_LIVE_ONLY
    assert rep["passed"] is False


def test_both_fired_pending_fills_shakedown():
    w = _win(CT, journal_for(high_yes_bid="0.43", low_no_bid="0.76"),
             [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24")])
    rep = run_parity([w], PARAMS)
    assert rep["windows"][0]["bin"] == LABEL_BOTH_FIRED
    assert rep["windows"][0]["neutrality_ok"] is True and rep["passed"]


def test_both_fired_source_mismatch_flagged():
    # SIM fires strangle (q0), LIVE fires sub-$1 flip (book flip C<1). Both fired, sources differ.
    journal = journal_for(high_yes_bid="0.43", low_no_bid="0.76")  # live flip C 0.84 -> sub
    sim_rows = [make_row("flip", "1.20", "0.0", 0, 300, "0.6", "0.6"),
                make_row("strangle", "0.90", "0.08", 0, 300, "0.50", "0.40")]
    w = _win(CT, journal, sim_rows, quintile=0)
    rep = run_parity([w], PARAMS)
    win = rep["windows"][0]
    assert win["bin"] == LABEL_BOTH_FIRED and win["source_match"] is False


# bins 3-5 + imbalance via assign_bin with a fills record (Phase 3 supplies fills)
def _sim_live_both():
    sim = SimEntry(fired=True, source=SUB_DOLLAR_FLIP, t_minus_s=300, C=Decimal("0.84"),
                   high_leg_price=Decimal("0.57"), low_leg_price=Decimal("0.24"), pin=0,
                   payoff=Decimal("0.16"))
    from service.parity import LiveEntry
    live = LiveEntry(fired=True, source=SUB_DOLLAR_FLIP, t_minus_s=300, C=Decimal("0.84"),
                     high_limit=Decimal("0.57"), low_limit=Decimal("0.24"))
    return sim, live


def test_bin3_both_fired_no_fill():
    sim, live = _sim_live_both()
    wp = assign_bin(CT, sim, live, WindowFills(filled=False), HI, LO)
    assert wp.bin == BIN_BOTH_NO_FILL and wp.neutrality_ok


def test_bin4_both_filled_diff_price():
    sim, live = _sim_live_both()
    fills = WindowFills(filled=True, legs=(
        LegFill(HI, "no", 1, Decimal("0.59")),   # 0.59 vs sim 0.57 -> slippage
        LegFill(LO, "yes", 1, Decimal("0.24")),
    ))
    wp = assign_bin(CT, sim, live, fills, HI, LO)
    assert wp.bin == BIN_BOTH_DIFF_PRICE
    assert wp.detail["price_deltas"]["high_delta"] == "0.02"


def test_bin5_both_filled_match():
    sim, live = _sim_live_both()
    fills = WindowFills(filled=True, realized_payoff=Decimal("0.16"), legs=(
        LegFill(HI, "no", 1, Decimal("0.57")),
        LegFill(LO, "yes", 1, Decimal("0.24")),
    ))
    wp = assign_bin(CT, sim, live, fills, HI, LO)
    assert wp.bin == BIN_BOTH_MATCH


def test_imbalance_bin():
    sim, live = _sim_live_both()
    wp = assign_bin(CT, sim, live, WindowFills(filled=True, imbalance=True), HI, LO)
    assert wp.bin == LABEL_IMBALANCE


def test_aggregate_passed_and_failures_mixed():
    good = _win(CT, journal_for(high_yes_bid="0.43", low_no_bid="0.76"),
                [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24")])
    bad = _win("2026-06-14T03:00:00Z", journal_for(high_yes_bid="0.40", low_no_bid="0.40"),
               [make_row("flip", "0.84", "0.03", 3, 300, "0.57", "0.24")])
    rep = run_parity([good, bad], PARAMS)
    assert rep["n_windows"] == 2 and rep["passed"] is False
    assert rep["n_neutrality_failures"] == 1
