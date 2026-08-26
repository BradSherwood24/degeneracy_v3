"""Phase box-5 tests for the wide-box daily report (service/box_report.py).

Synthetic journals + ledger fixtures cover: both-filled pinned / not-pinned, one-legged
flattened / held-naked (+ settled), the level bump, the A5 threshold at 3/20 vs 2/20, R1/R2/R3
status transitions at their pins, S4 from a guard file, the empty day, and WOULD_FIRE excluded from
the fired set. Plus an end-to-end I/O round-trip through real files (no network, no sealed reads)."""

from __future__ import annotations

import json
import os
from decimal import Decimal

from service import box_report as br

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
_HOURLY = "KXBTCD-26AUG2605-T76499.99"
_M15 = "KXBTC15M-26AUG260500-00"


def _rec(kind, obj, idx=0, ts=1.0):
    return {"idx": idx, "kind": kind, "local_ts": ts, "obj": obj}


def _selection(hourly_ask="0.93", m15_ask="0.90", *, hourly_side="no", m15_side="yes",
               K="76500", A="76400", C="1.86", C_mid="1.88", implied_pin="0.88",
               hourly_limit="0.96", m15_limit="0.93", hourly_ask_size=10, m15_ask_size=8):
    return {
        "hourly_ticker": _HOURLY, "hourly_side": hourly_side, "hourly_ask": hourly_ask,
        "hourly_bid": "0.90", "hourly_mid": "0.915", "hourly_limit": hourly_limit,
        "hourly_ask_size": hourly_ask_size,
        "m15_ticker": _M15, "m15_side": m15_side, "m15_ask": m15_ask, "m15_bid": "0.88",
        "m15_limit": m15_limit, "m15_ask_size": m15_ask_size,
        "strike_K": K, "anchor_A": A, "C": C, "C_mid": C_mid, "implied_pin": implied_pin,
    }


def fire_journal(close_time, selection=None, t_minus_s=300.0, extra_records=None):
    """A minimal journal record list for one fired box window."""
    sel = selection or _selection()
    recs = [
        _rec("window_start", {"close_time": close_time, "pairs": 1}, idx=0),
        _rec("box_fire", {"kind": "FIRE", "source": br.WIDE_BOX, "count": 1,
                          "C": sel["C"], "t_minus_s": t_minus_s, "reason": None,
                          "legs": [], "selection": sel}, idx=1),
    ]
    if extra_records:
        recs.extend(extra_records)
    return recs


def would_fire_journal(close_time, selection=None):
    sel = selection or _selection()
    return [
        _rec("window_start", {"close_time": close_time, "pairs": 1}, idx=0),
        _rec("box_would_fire", {"kind": "WOULD_FIRE", "source": br.WIDE_BOX, "count": 1,
                                "C": sel["C"], "t_minus_s": 300.0, "reason": None,
                                "legs": [], "selection": sel}, idx=1),
    ]


def ledger_box_row(close_time, *, hourly_fill="0.93", m15_fill="0.90",
                   hourly_fee="0.01", m15_fee="0.01", filled=True, box_one_legged=False,
                   box_flatten_filled=None, realized_delta="-0.86", realized_unsettled=True,
                   floor_booked="1.00", one_leg_which="hourly"):
    """A box FIRE ledger row. For one-legged, only the surviving leg has a fill (or the flattened
    leg is booked as round-trip -> still one fill leg for reporting)."""
    fills = []
    if filled:
        fills = [
            {"ticker": _HOURLY, "side": "no", "count": 1, "avg_price": hourly_fill, "avg_fee": hourly_fee},
            {"ticker": _M15, "side": "yes", "count": 1, "avg_price": m15_fill, "avg_fee": m15_fee},
        ]
    elif box_one_legged:
        if one_leg_which == "hourly":
            fills = [{"ticker": _HOURLY, "side": "no", "count": 1,
                      "avg_price": hourly_fill, "avg_fee": hourly_fee}]
        else:
            fills = [{"ticker": _M15, "side": "yes", "count": 1,
                      "avg_price": m15_fill, "avg_fee": m15_fee}]
    return {
        "close_time": close_time, "strategy": "box", "mode": "armed", "fires": 1,
        "fired_source": br.WIDE_BOX, "filled": filled, "box_one_legged": box_one_legged,
        "box_flatten_filled": box_flatten_filled, "fills": fills,
        "realized_delta": realized_delta, "realized_unsettled": realized_unsettled,
        "floor_booked": floor_booked, "unsettled_legs": [], "slippage_abs_per_side": [],
        "s1_violation": False, "alarms": [], "stops": [],
    }


def backfill_row(close_time, *, settlement_payoff="2.00", realized_delta="1.00", floor_netted="1.00"):
    return {
        "close_time": close_time, "backfill_of": close_time, "fires": 0, "filled": False,
        "realized_delta": realized_delta, "settlement_payoff": settlement_payoff,
        "floor_netted": floor_netted, "realized_unsettled": False,
    }


def _one_window(journal, ledger):
    """Convenience: build_report over one journal + ledger, return the single fired window."""
    rep = br.build_report([("x", journal)], ledger, {})
    days = rep["days"]
    day = next(iter(days))
    return days[day]["fires"][0]


# ---------------------------------------------------------------------------
# Both-filled: pinned / not pinned
# ---------------------------------------------------------------------------
def test_both_filled_pinned():
    ct = "2026-08-26T05:00:00Z"
    j = fire_journal(ct)
    led = [ledger_box_row(ct, realized_delta="-0.86"), backfill_row(ct, settlement_payoff="2.00",
                                                                    realized_delta="1.00")]
    w = _one_window(j, led)
    assert w["fill_class"] == "both"
    assert w["outcome"] == "pinned"
    assert w["realized_settled"] is True
    # realized = close (-0.86) + backfill (+1.00) = +0.14  (=$2 pinned - $1.86 cost)
    assert w["realized"] == Decimal("0.14")
    # slippage: fill 0.93 vs decided ask 0.93 -> 0c
    assert w["hourly"]["slippage_cents"] == Decimal("0")
    assert w["side"] == "above"  # m15_side == yes


def test_both_filled_not_pinned():
    ct = "2026-08-26T06:00:00Z"
    j = fire_journal(ct)
    led = [ledger_box_row(ct, realized_delta="-0.86"),
           backfill_row(ct, settlement_payoff="1.00", realized_delta="0.00")]
    w = _one_window(j, led)
    assert w["outcome"] == "not_pinned"
    assert w["realized"] == Decimal("-0.86")


def test_both_filled_unsettled_when_no_backfill():
    ct = "2026-08-26T07:00:00Z"
    w = _one_window(fire_journal(ct), [ledger_box_row(ct)])
    assert w["fill_class"] == "both"
    assert w["outcome"] == "unsettled"
    assert w["realized_settled"] is False


# ---------------------------------------------------------------------------
# One-legged: flattened / held naked (+ settled)
# ---------------------------------------------------------------------------
def test_one_legged_flattened():
    ct = "2026-08-26T08:00:00Z"
    flat = [_rec("box_flatten", {"stage": "start", "ticker": _HOURLY}, idx=2),
            _rec("box_flatten", {"stage": "flat", "ticker": _HOURLY, "attempt": 1}, idx=3)]
    j = fire_journal(ct, extra_records=flat)
    led = [ledger_box_row(ct, filled=False, box_one_legged=True, box_flatten_filled=True,
                          realized_delta="-0.04", realized_unsettled=False)]
    w = _one_window(j, led)
    assert w["fill_class"] == "one-legged"
    assert w["flatten"]["outcome"] == "flattened"
    assert w["flatten"]["attempts"] == 1
    assert w["outcome"] == "flattened"
    assert w["realized"] == Decimal("-0.04")


def test_one_legged_held_naked():
    ct = "2026-08-26T09:00:00Z"
    flat = [_rec("box_flatten", {"stage": "start"}, idx=2),
            _rec("box_flatten", {"stage": "miss_retry", "attempt": 1}, idx=3),
            _rec("box_flatten", {"stage": "miss_retry", "attempt": 2}, idx=4),
            _rec("box_flatten", {"stage": "giveup_hold", "attempts": 3}, idx=5)]
    j = fire_journal(ct, extra_records=flat)
    led = [ledger_box_row(ct, filled=False, box_one_legged=True, box_flatten_filled=False,
                          realized_delta="-0.93", realized_unsettled=True)]
    w = _one_window(j, led)
    assert w["flatten"]["outcome"] == "held_naked"
    assert w["flatten"]["attempts"] == 3  # 2 miss_retry + 1 giveup_hold
    assert w["outcome"] == "held_naked"


def test_one_legged_held_then_settled():
    ct = "2026-08-26T10:00:00Z"
    flat = [_rec("box_flatten", {"stage": "start"}, idx=2),
            _rec("box_flatten", {"stage": "giveup_hold", "attempts": 3}, idx=5)]
    j = fire_journal(ct, extra_records=flat)
    led = [ledger_box_row(ct, filled=False, box_one_legged=True, box_flatten_filled=False,
                          realized_delta="-0.93", realized_unsettled=True),
           backfill_row(ct, settlement_payoff="1.00", realized_delta="1.00", floor_netted="0.00")]
    w = _one_window(j, led)
    assert w["outcome"] == "held_naked+settled"
    assert w["realized"] == Decimal("0.07")  # -0.93 + 1.00


# ---------------------------------------------------------------------------
# Level bump + A1
# ---------------------------------------------------------------------------
def test_level_bump_and_a1():
    ct = "2026-08-26T11:00:00Z"
    # decided asks 0.93 / 0.90; fills bump to 0.96 (hourly, +3c > 2c) and 0.905 (m15, +0.5c).
    j = fire_journal(ct)
    led = [ledger_box_row(ct, hourly_fill="0.96", m15_fill="0.905")]
    rep = br.build_report([("x", j)], led, {})
    agg = rep["cumulative"]["aggregates"]
    lb = agg["level_bumps"]
    assert lb["count"] == 2  # both legs filled above decided ask
    assert lb["distribution"]["max_cents"] == Decimal("3.000")
    a1 = agg["A1"]
    assert a1["flagged_count"] == 1  # only the +3c hourly leg exceeds 2c
    assert a1["status"] == br.STATUS_TRIPPED
    assert a1["running_mean_cents"] == Decimal("3.000")


def test_no_bump_when_fill_at_ask():
    ct = "2026-08-26T12:00:00Z"
    w = _one_window(fire_journal(ct), [ledger_box_row(ct, hourly_fill="0.93", m15_fill="0.90")])
    assert w["hourly"]["level_bump"] is False
    assert w["m15"]["level_bump"] is False


# ---------------------------------------------------------------------------
# A5 threshold at 3/20 vs 2/20
# ---------------------------------------------------------------------------
def _n_fires(n, one_legged_count):
    """Build n fired windows, the first ``one_legged_count`` of them one-legged."""
    journals = []
    ledger = []
    for i in range(n):
        ct = f"2026-08-26T{i:02d}:00:00Z"
        journals.append(("x", fire_journal(ct)))
        if i < one_legged_count:
            ledger.append(ledger_box_row(ct, filled=False, box_one_legged=True,
                                         box_flatten_filled=True, realized_delta="-0.04",
                                         realized_unsettled=False))
        else:
            ledger.append(ledger_box_row(ct))
            ledger.append(backfill_row(ct))
    return journals, ledger


def test_a5_tripped_at_3_of_20():
    journals, ledger = _n_fires(20, 3)
    agg = br.build_report(journals, ledger, {})["cumulative"]["aggregates"]
    a5 = agg["A5"]
    assert a5["considered"] == 20
    assert a5["one_legged"] == 3
    assert a5["rate"] == Decimal("3") / Decimal("20")
    assert a5["status"] == br.STATUS_TRIPPED


def test_a5_holding_at_2_of_20():
    journals, ledger = _n_fires(20, 2)
    a5 = br.build_report(journals, ledger, {})["cumulative"]["aggregates"]["A5"]
    assert a5["rate"] == Decimal("2") / Decimal("20")  # == 0.10, not > 0.10
    assert a5["status"] == br.STATUS_HOLDING


# ---------------------------------------------------------------------------
# R1 / R2 / R3 status transitions at the pins
# ---------------------------------------------------------------------------
def _both_filled(n, *, hourly_fill="0.93", m15_fill="0.90", realized_delta="0.05",
                 settlement="2.00", bf_realized="1.00"):
    journals, ledger = [], []
    for i in range(n):
        ct = f"2026-08-26T{i:03d}"  # unique window key
        journals.append(("x", fire_journal(ct)))
        ledger.append(ledger_box_row(ct, hourly_fill=hourly_fill, m15_fill=m15_fill,
                                     realized_delta=realized_delta))
        ledger.append(backfill_row(ct, settlement_payoff=settlement, realized_delta=bf_realized))
    return journals, ledger


def test_r1_not_yet_below_30():
    # 29 fills, each summed slip +2c -> mean > pin but gate not reached.
    j, led = _both_filled(29, hourly_fill="0.94", m15_fill="0.91")  # +1c each -> summed +2c
    r1 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R1"]
    assert r1["n_two_leg_fills"] == 29
    assert r1["status"].startswith("NOT YET (29/30)")


def test_r1_tripped_at_30_over_pin():
    j, led = _both_filled(30, hourly_fill="0.94", m15_fill="0.91")  # summed +2c > +1c pin
    r1 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R1"]
    assert r1["n_two_leg_fills"] == 30
    assert r1["mean_summed_slippage_cents"] == Decimal("2")
    assert r1["status"] == br.STATUS_TRIPPED


def test_r1_holding_at_30_under_pin():
    # fills exactly at the decided ask -> summed slip 0c <= +1c pin.
    j, led = _both_filled(30, hourly_fill="0.93", m15_fill="0.90")
    r1 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R1"]
    assert r1["mean_summed_slippage_cents"] == Decimal("0")
    assert r1["status"] == br.STATUS_HOLDING


def test_r2_tripped_below_minus3c():
    # realized -0.10/pair = -10c < -3c pin; settlement not pinned so realized = close only.
    j, led = _both_filled(60, realized_delta="-0.10", settlement="1.00", bf_realized="0.00")
    agg = br.build_report(j, led, {})["cumulative"]["aggregates"]
    r2 = agg["R2"]
    assert r2["n_fills"] == 60                       # all any-fill fires (here all two-leg)
    assert r2["n_pairs"] == 60
    assert r2["mean_realized_cents"] == Decimal("-10")   # per-FIRE (decisive)
    assert r2["per_pair_mean_realized_cents"] == Decimal("-10")  # two-leg-only, same when no one-legged
    assert r2["status"] == br.STATUS_TRIPPED
    assert r2["bootstrap_ci95_cents"] is not None


def test_r2_holding_above_minus3c():
    j, led = _both_filled(60, realized_delta="0.14", settlement="2.00", bf_realized="1.00")
    r2 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R2"]
    # realized = 0.14 + 1.00 = 1.14 -> +114c, well above -3c
    assert r2["mean_realized_cents"] == Decimal("114")
    assert r2["status"] == br.STATUS_HOLDING


def test_r2_not_yet_below_60():
    j, led = _both_filled(59, realized_delta="-0.10", settlement="1.00", bf_realized="0.00")
    r2 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R2"]
    assert r2["status"].startswith("NOT YET (59/60)")


def test_r2_status_on_per_fire_includes_one_legged():
    """Coordinator ruling: one-legged flatten losses are IN R2's status. Two-leg-only mean is
    HOLDING (-2c) but the per-fire mean including 20 one-legged -95c flatten losses is < -3c ->
    TRIPPED. The per_pair figure is still reported and still shows the HOLDING-side number."""
    journals, ledger = [], []
    for i in range(40):  # two-leg, realized -0.02 (-2c) -> per_pair alone would HOLD
        ct = f"2026-08-26T r2mix both {i:03d}"
        journals.append(("x", fire_journal(ct)))
        ledger.append(ledger_box_row(ct, realized_delta="-0.02", realized_unsettled=False))
    for i in range(20):  # one-legged flatten loss -0.95 (-95c)
        ct = f"2026-08-26T r2mix one {i:03d}"
        journals.append(("x", fire_journal(ct)))
        ledger.append(ledger_box_row(ct, filled=False, box_one_legged=True,
                                     box_flatten_filled=True, realized_delta="-0.95",
                                     realized_unsettled=False))
    r2 = br.build_report(journals, ledger, {})["cumulative"]["aggregates"]["R2"]
    assert r2["n_fills"] == 60          # 40 two-leg + 20 one-legged, all any-fill
    assert r2["n_pairs"] == 40          # two-leg only
    # per-fire mean = (40*-2 + 20*-95)/60 = -1980/60 = -33c
    assert r2["mean_realized_cents"] == Decimal("-33")
    assert r2["per_pair_mean_realized_cents"] == Decimal("-2")  # two-leg-only stays -2c (HOLDING-side)
    assert r2["status"] == br.STATUS_TRIPPED  # decided on the per-fire figure


def test_r2_one_legged_only_still_counts():
    """A run of only one-legged fires still advances R2 (any fill), and R1/R3 do not (need two legs)."""
    journals, ledger = [], []
    for i in range(60):
        ct = f"2026-08-26T r2one {i:03d}"
        journals.append(("x", fire_journal(ct)))
        ledger.append(ledger_box_row(ct, filled=False, box_one_legged=True,
                                     box_flatten_filled=True, realized_delta="-0.02",
                                     realized_unsettled=False))
    agg = br.build_report(journals, ledger, {})["cumulative"]["aggregates"]
    assert agg["R2"]["n_fills"] == 60
    assert agg["R2"]["n_pairs"] == 0
    assert agg["R2"]["mean_realized_cents"] == Decimal("-2")   # -2c > -3c pin
    assert agg["R2"]["per_pair_mean_realized_cents"] is None
    assert agg["R2"]["status"] == br.STATUS_HOLDING
    # R1 (slippage) and R3 (pin) need two legs -> still NOT YET at 0.
    assert agg["R1"]["status"].startswith("NOT YET")
    assert agg["R3"]["n_fills"] == 0


def test_r3_tripped_low_pin_rate():
    # 60 fills, 30 pinned / 30 not -> pin rate 0.50 < 0.80.
    journals, ledger = [], []
    for i in range(60):
        ct = f"2026-08-26T r3 {i:03d}"
        journals.append(("x", fire_journal(ct)))
        ledger.append(ledger_box_row(ct, realized_delta="0.05"))
        payoff = "2.00" if i < 30 else "1.00"
        ledger.append(backfill_row(ct, settlement_payoff=payoff, realized_delta="1.00" if i < 30 else "0.00"))
    r3 = br.build_report(journals, ledger, {})["cumulative"]["aggregates"]["R3"]
    assert r3["n_fills"] == 60
    assert r3["n_settled"] == 60
    assert r3["pin_rate"] == Decimal("30") / Decimal("60")
    assert r3["backtest_pin_rate"] == Decimal("0.90")
    assert r3["implied_pin_mean"] == Decimal("0.88")
    assert r3["status"] == br.STATUS_TRIPPED


def test_r3_holding_high_pin_rate():
    j, led = _both_filled(60, settlement="2.00", bf_realized="1.00")  # all pinned -> rate 1.0
    r3 = br.build_report(j, led, {})["cumulative"]["aggregates"]["R3"]
    assert r3["pin_rate"] == Decimal("1")
    assert r3["status"] == br.STATUS_HOLDING


def test_r4_confirmed_at_100_clean():
    # 100 pinned both-filled, realized positive -> no R1-R3 trip -> R4 confirmed.
    j, led = _both_filled(100, hourly_fill="0.93", m15_fill="0.90",
                          realized_delta="0.14", settlement="2.00", bf_realized="1.00")
    agg = br.build_report(j, led, {})["cumulative"]["aggregates"]
    r4 = agg["R4"]
    assert r4["n_fills"] == 100
    assert r4["confirmed"] is True
    assert r4["status"] == br.STATUS_TRIPPED


# ---------------------------------------------------------------------------
# S4 from a guard file
# ---------------------------------------------------------------------------
def test_s4_holding_under_cap():
    guard = {"utc_day": "2026-08-26", "balance_start_dollars": "100.00", "latched": []}
    s4rec = [_rec("s4_balance_check", {"balance_start_dollars": "100.00",
                                       "balance_now_dollars": "98.50", "loss_dollars": "1.50",
                                       "breached": False, "ledger_vs_balance_delta": "0.00"})]
    s4 = br.compute_s4("2026-08-26", guard, s4rec)
    assert s4["loss_dollars"] == Decimal("1.50")
    assert s4["status"] == br.STATUS_HOLDING
    assert s4["any_stop_latched"] is False


def test_s4_tripped_by_loss():
    guard = {"utc_day": "2026-08-26", "balance_start_dollars": "100.00", "latched": []}
    s4rec = [_rec("s4_balance_check", {"balance_now_dollars": "96.50", "loss_dollars": "3.50",
                                       "breached": True, "ledger_vs_balance_delta": "-0.10"})]
    s4 = br.compute_s4("2026-08-26", guard, s4rec)
    assert s4["status"] == br.STATUS_TRIPPED


def test_s4_tripped_by_latch():
    guard = {"utc_day": "2026-08-26", "balance_start_dollars": "100.00",
             "latched": [{"kind": "S4", "reason": "x", "window": "w", "ts": 1.0}]}
    s4 = br.compute_s4("2026-08-26", guard, [])
    assert s4["status"] == br.STATUS_TRIPPED
    assert s4["any_stop_latched"] is True
    assert "S4" in s4["latched_kinds"]


def test_s4_not_yet_no_data():
    s4 = br.compute_s4("2026-08-26", None, [])
    assert s4["status"].startswith("NOT YET")


# ---------------------------------------------------------------------------
# Empty day + WOULD_FIRE exclusion
# ---------------------------------------------------------------------------
def test_empty_report_renders():
    rep = br.build_report([], [], {})
    assert rep["days"] == {}
    assert rep["cumulative"]["aggregates"]["fires"] == 0
    text = br.render_text(rep)
    assert "no box windows found" in text


def test_would_fire_excluded_from_fills():
    ct = "2026-08-26T05:00:00Z"
    # A would-fire journal, and (defensively) even a ledger row present must not turn it into a fire.
    rep = br.build_report([("x", would_fire_journal(ct))], [ledger_box_row(ct)], {})
    day = rep["days"][ct[:10]]
    assert len(day["fires"]) == 0
    assert len(day["would_fires"]) == 1
    assert day["aggregates"]["fires"] == 0
    assert day["aggregates"]["two_leg_fills"] == 0
    wf = day["would_fires"][0]
    assert wf["hourly"]["decided_ask"] == Decimal("0.93")


def test_would_fire_has_no_fill_leg():
    ct = "2026-08-26T05:00:00Z"
    rep = br.build_report([("x", would_fire_journal(ct))], [], {})
    wf = rep["days"][ct[:10]]["would_fires"][0]
    assert wf["hourly"]["filled"] is False
    assert wf["fill_class"] == "none"


# ---------------------------------------------------------------------------
# Day filtering + cumulative spans all days
# ---------------------------------------------------------------------------
def test_only_day_filters_days_but_cumulative_spans_all():
    j1 = ("x", fire_journal("2026-08-26T05:00:00Z"))
    j2 = ("y", fire_journal("2026-08-27T05:00:00Z"))
    led = [ledger_box_row("2026-08-26T05:00:00Z"), backfill_row("2026-08-26T05:00:00Z"),
           ledger_box_row("2026-08-27T05:00:00Z"), backfill_row("2026-08-27T05:00:00Z")]
    rep = br.build_report([j1, j2], led, {}, only_day="2026-08-26")
    assert set(rep["days"]) == {"2026-08-26"}
    assert rep["cumulative"]["aggregates"]["fires"] == 2  # cumulative still spans both


# ---------------------------------------------------------------------------
# Slippage sign / candle-staleness
# ---------------------------------------------------------------------------
def test_slippage_signed_and_summed():
    ct = "2026-08-26T05:00:00Z"
    # hourly fill 0.94 (ask 0.93 -> +1c); m15 fill 0.895 (ask 0.90 -> -0.5c) => summed +0.5c
    w = _one_window(fire_journal(ct), [ledger_box_row(ct, hourly_fill="0.94", m15_fill="0.895")])
    assert w["hourly"]["slippage_cents"] == Decimal("1.00")
    assert w["m15"]["slippage_cents"] == Decimal("-0.500")
    assert w["summed_slip_cents"] == Decimal("0.500")


def test_candle_staleness_paid_vs_mid():
    ct = "2026-08-26T05:00:00Z"
    # C_mid 1.88; paid = 0.94+0.01 + 0.90+0.01 = 1.86 -> paid_vs_mid = (1.86-1.88)*100 = -2c
    w = _one_window(fire_journal(ct), [ledger_box_row(ct, hourly_fill="0.94", m15_fill="0.90")])
    assert w["C_paid"] == Decimal("1.86")
    assert w["staleness_paid_vs_mid_cents"] == Decimal("-2.0000")


def test_displayed_ask_size_carried():
    ct = "2026-08-26T05:00:00Z"
    w = _one_window(fire_journal(ct), [ledger_box_row(ct)])
    assert w["hourly"]["displayed_ask_size"] == 10
    assert w["m15"]["displayed_ask_size"] == 8


# ---------------------------------------------------------------------------
# End-to-end I/O round-trip (real files; no network, no sealed reads)
# ---------------------------------------------------------------------------
def test_io_roundtrip(tmp_path):
    jdir = tmp_path / "journals"
    jdir.mkdir()
    ct = "2026-08-26T05:00:00Z"
    # write a journal file named by the safe-close convention
    jpath = jdir / "20260826T050000Z.jsonl"
    with open(jpath, "w", encoding="utf-8") as f:
        for r in fire_journal(ct):
            f.write(json.dumps(r) + "\n")
        # a kalshi_ws frame to prove the fast-reject skips it
        f.write(json.dumps(_rec("kalshi_ws", {"junk": 1}, idx=99)) + "\n")

    ledpath = tmp_path / "pilot_ledger.jsonl"
    with open(ledpath, "w", encoding="utf-8") as f:
        f.write(json.dumps(ledger_box_row(ct)) + "\n")
        f.write(json.dumps(backfill_row(ct)) + "\n")

    ops = tmp_path / "ops"
    ops.mkdir()
    with open(ops / "stops_2026-08-26.json", "w", encoding="utf-8") as f:
        json.dump({"utc_day": "2026-08-26", "balance_start_dollars": "100.00", "latched": []}, f)

    out = tmp_path / "reports"
    rc = br.main(["--journal-dir", str(jdir), "--ledger", str(ledpath),
                  "--ops-dir", str(ops), "--out", str(out)])
    assert rc == 0
    assert (out / "box_2026-08-26.json").exists()
    assert (out / "box_cumulative.json").exists()
    with open(out / "box_2026-08-26.json", encoding="utf-8") as f:
        day = json.load(f)
    assert len(day["fires"]) == 1
    assert day["fires"][0]["outcome"] == "pinned"
    # Decimals serialized as strings
    assert day["fires"][0]["realized"] == "0.14"


def test_wide_box_constant_matches_source():
    # the report keeps a LOCAL WIDE_BOX to stay import-decoupled; it must not drift from the source.
    from service.box import WIDE_BOX as BOX_SRC
    from service.ledger import WIDE_BOX as LEDGER_SRC
    assert br.WIDE_BOX == BOX_SRC == LEDGER_SRC


def test_close_from_filename_roundtrip():
    p = "/some/dir/20260826T050000Z.jsonl"
    assert br._close_from_filename(p) == "2026-08-26T05:00:00Z"


def test_load_journal_file_fast_reject(tmp_path):
    p = tmp_path / "j.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(_rec("window_start", {"close_time": "2026-08-26T05:00:00Z"})) + "\n")
        for i in range(1000):
            f.write(json.dumps(_rec("kalshi_ws", {"i": i})) + "\n")
        f.write(json.dumps(_rec("box_fire", {"selection": _selection()})) + "\n")
    recs = br.load_journal_file(str(p))
    kinds = [r["kind"] for r in recs]
    assert "kalshi_ws" not in kinds
    assert "window_start" in kinds and "box_fire" in kinds
