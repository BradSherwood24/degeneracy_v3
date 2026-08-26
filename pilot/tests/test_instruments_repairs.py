"""Phase-3a instrument repairs — BUG-2 (realized booking + settlement backfill) and BUG-3 (stop
latching + S4-from-balance) unit tests. BUG-1 (units) is covered in test_orders_envelope /
test_executor / test_ledger. Ground truth is the real 2026-08-23/24 armed-span fills.
"""

from __future__ import annotations

import json
import os

import pytest
from decimal import Decimal

from service.ledger import (
    PURPOSE_ENTRY,
    Intent,
    IntentLeg,
    new_ledger,
    record_intent,
    record_response,
    settlement_payoff,
)
from service.orders.envelope import OrderResponse
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP

CT = "2026-08-24T05:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTC15M-LO"


def _resp(cid, fill, price, fee):
    return OrderResponse(cid, "o-" + cid, Decimal(str(fill)), Decimal(0),
                         Decimal(str(price)), Decimal(str(fee)), 1,
                         raw_reported_price=Decimal(str(price)))


from service.ledger import (  # noqa: E402
    intent_to_record,
    rebuild_from_journal,
    response_from_record,
    response_to_record,
)


# ===========================================================================
# BUG-1: rebuild normalizes LEGACY response records (no raw_reported_price key) using the intent
# leg's side; leaves NEW (already-normalized) records untouched. Ground truth: real 8/23 14:00Z.
# ===========================================================================
def _legacy_14z_records():
    entry = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY, (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.9700"), "buy-no"),
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.0250"), "buy-yes"),
    ))
    flat = Intent(CT, SUB_DOLLAR_FLIP, "rebalance-sell", (
        IntentLeg(HI, "no", "sell", 1, Decimal("0.9500"), "sell-no", reduce_only=True),
    ))
    # legacy response objects: RAW YES-space price, NO raw_reported_price key at all
    return [
        {"kind": "order_intent", "obj": intent_to_record(entry)},
        {"kind": "order_response", "obj": {"client_order_id": "buy-no", "order_id": "o1",
            "fill_count": "1.00", "remaining_count": "0.00", "average_fill_price": "0.0300",
            "average_fee_paid": "0.0021", "ts_ms": 1, "error": None, "no_fill": False}},
        {"kind": "order_response", "obj": {"client_order_id": "buy-yes", "order_id": "o2",
            "fill_count": "0.00", "remaining_count": "0.00", "average_fill_price": None,
            "average_fee_paid": None, "ts_ms": 1, "error": None, "no_fill": False}},
        {"kind": "order_intent", "obj": intent_to_record(flat)},
        {"kind": "order_response", "obj": {"client_order_id": "sell-no", "order_id": "o3",
            "fill_count": "1.00", "remaining_count": "0.00", "average_fill_price": "0.0500",
            "average_fee_paid": "0.0034", "ts_ms": 1, "error": None, "no_fill": False}},
    ]


def test_rebuild_normalizes_legacy_no_leg_and_realized_is_kalshis_number():
    st = rebuild_from_journal(_legacy_14z_records(), CT, SUB_DOLLAR_FLIP, HI, LO)
    hi = st.position("high")
    assert hi.avg_buy_price == Decimal("0.9700")            # 1 - 0.0300, not the phantom 0.03
    assert (hi.sold_notional / hi.sold) == Decimal("0.9500")  # sell normalized too
    assert st.realized_cashflow() == Decimal("-0.0255")     # Kalshi's realized


def test_rebuild_leaves_new_normalized_records_untouched():
    r = OrderResponse("buy-no", "o1", Decimal(1), Decimal(0), Decimal("0.9700"),
                      Decimal("0.0021"), 1, raw_reported_price=Decimal("0.0300"))
    entry = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY, (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.9700"), "buy-no"),
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.0250"), "buy-yes")))
    recs = [{"kind": "order_intent", "obj": intent_to_record(entry)},
            {"kind": "order_response", "obj": response_to_record(r)}]
    st = rebuild_from_journal(recs, CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.position("high").avg_buy_price == Decimal("0.9700")  # not flipped back to 0.03


def test_response_record_roundtrip_preserves_raw_reported_price():
    r = OrderResponse("c", "o", Decimal(1), Decimal(0), Decimal("0.9700"),
                      Decimal("0.0021"), 5, raw_reported_price=Decimal("0.0300"))
    back = response_from_record(response_to_record(r))
    assert back.average_fill_price == Decimal("0.9700")
    assert back.raw_reported_price == Decimal("0.0300")


# ===========================================================================
# BUG-1a: a normalized NO fill no longer trips a PHANTOM A1 slippage alarm.
# ===========================================================================
def test_no_phantom_a1_on_normalized_no_fill():
    from service.stops import StopConfig, check_slippage_alarms
    intent = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY,
                    (IntentLeg(HI, "no", "buy", 1, Decimal("0.9700"), "h"),))
    # normalized response: avg_fill in NO-space (0.97), raw venue value (0.03) preserved.
    r = OrderResponse("h", "o", Decimal(1), Decimal(0), Decimal("0.9700"), Decimal("0.0021"), 1,
                      raw_reported_price=Decimal("0.0300"))
    alarms = check_slippage_alarms(intent, (r,), StopConfig())
    assert alarms == ()   # |0.97 - 0.97| = 0, not the phantom |0.03 - 0.97| = 0.94


# ===========================================================================
# BUG-2: realized_cashflow / realized_at_close / unsettled_legs
# ===========================================================================
def _flip_entry(hi_price, lo_price):
    return Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY, (
        IntentLeg(HI, "no", "buy", 1, Decimal(hi_price), "h"),
        IntentLeg(LO, "yes", "buy", 1, Decimal(lo_price), "l"),
    ))


def test_flatten_roundtrip_realized_known_immediately():
    # 14Z: NO bought @0.97 (fee .0021), missed low leg, NO sold reduce-only @0.95 (fee .0034).
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), _flip_entry("0.9700", "0.0250"))
    st = record_response(st, _resp("h", 1, "0.9700", "0.0021"))
    st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, "rebalance-sell",
                                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.9500"), "s", reduce_only=True),)))
    st = record_response(st, _resp("s", 1, "0.9500", "0.0034"))
    assert st.matched_pairs() == 0
    assert st.realized_at_close() == Decimal("-0.0255")  # Kalshi's number; was booked 0 before
    assert st.unsettled_legs() == ()
    assert st.has_any_fill() is True


def test_naked_leg_books_outlay_and_pends_settlement():
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), _flip_entry("0.9700", "0.4800"))
    st = record_response(st, _resp("l", 1, "0.4800", "0.0175"))  # only YES leg filled -> naked
    assert st.realized_at_close() == Decimal("-0.4975")
    assert st.unsettled_legs() == ((LO, "yes", 1),)


def test_sub1_matched_pair_books_floor_and_settled():
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), _flip_entry("0.0080", "0.9900"))
    st = record_response(st, _resp("h", 1, "0.0080", "0.0006"))
    st = record_response(st, _resp("l", 1, "0.9900", "0.0007"))
    assert st.realized_at_close() == st.realized_min() == Decimal("0.0007")
    assert st.unsettled_legs() == ()


def test_strangle_matched_pair_no_floor_all_pending():
    st = new_ledger(CT, Q1_STRANGLE, HI, LO)
    st = record_intent(st, Intent(CT, Q1_STRANGLE, PURPOSE_ENTRY, (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.35"), "h"),
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.54"), "l"))))
    st = record_response(st, _resp("h", 1, "0.35", "0.01"))
    st = record_response(st, _resp("l", 1, "0.54", "0.01"))
    assert st.realized_at_close() == st.realized_cashflow() < 0  # conservative, never the win
    assert len(st.unsettled_legs()) == 2


def test_no_fill_window_books_zero():
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), _flip_entry("0.9700", "0.0250"))
    assert st.has_any_fill() is False
    assert st.realized_at_close() == Decimal(0)
    assert st.unsettled_legs() == ()


# ===========================================================================
# BUG-2 part c: settlement payoff
# ===========================================================================
def test_settlement_payoff_win_lose_and_failclosed():
    legs = [{"ticker": "T1", "side": "yes", "count": 1}, {"ticker": "T2", "side": "no", "count": 2}]
    assert settlement_payoff(legs, {"T1": "yes", "T2": "yes"}) == Decimal("1")  # T1 wins only
    assert settlement_payoff(legs, {"T1": "yes", "T2": "no"}) == Decimal("3")   # both win
    assert settlement_payoff(legs, {"T1": "no", "T2": "yes"}) == Decimal("0")   # both lose
    with pytest.raises(KeyError):
        settlement_payoff(legs, {"T1": "yes"})       # missing T2 result
    with pytest.raises(ValueError):
        settlement_payoff(legs, {"T1": "maybe", "T2": "no"})


# ===========================================================================
# BUG-2 part c: pilot_ledger backfill CLI
# ===========================================================================
def _ledger(tmp_path):
    return os.path.join(tmp_path, "ledger", "pilot_ledger.jsonl")


def test_backfill_appends_losing_payoff_zero(tmp_path):
    from service.pilot_ledger import append_entry, load_entries, main, s4_running_loss
    lp = _ledger(tmp_path)
    append_entry({"close_time": CT, "mode": "armed", "pairs": 1, "fires": 1, "realized_delta": "-0.4975",
                  "realized_unsettled": True,
                  "unsettled_legs": [{"ticker": LO, "side": "yes", "count": 1}]}, lp)
    rc = main(["--ledger", lp, "backfill", "--window", CT, "--result", f"{LO}=no"])
    assert rc == 0
    entries = load_entries(lp)
    bf = [e for e in entries if e.get("backfill_of") == CT]
    assert len(bf) == 1 and bf[0]["realized_delta"] == "0"
    # naked leg lost -> total stays the conservative close value
    assert s4_running_loss(entries) == Decimal("-0.4975")


def test_backfill_appends_winning_payoff(tmp_path):
    from service.pilot_ledger import append_entry, load_entries, main, s4_running_loss
    lp = _ledger(tmp_path)
    append_entry({"close_time": CT, "mode": "armed", "pairs": 1, "fires": 1, "realized_delta": "-0.4975",
                  "realized_unsettled": True,
                  "unsettled_legs": [{"ticker": LO, "side": "yes", "count": 1}]}, lp)
    assert main(["--ledger", lp, "backfill", "--window", CT, "--result", f"{LO}=yes"]) == 0
    # winning leg adds +1.00 -> total = -0.4975 + 1.00
    assert s4_running_loss(load_entries(lp)) == Decimal("0.5025")


def test_backfill_is_idempotent(tmp_path):
    from service.pilot_ledger import append_entry, main
    lp = _ledger(tmp_path)
    append_entry({"close_time": CT, "mode": "armed", "pairs": 1, "fires": 1, "realized_delta": "-0.4975",
                  "realized_unsettled": True,
                  "unsettled_legs": [{"ticker": LO, "side": "yes", "count": 1}]}, lp)
    assert main(["--ledger", lp, "backfill", "--window", CT, "--result", f"{LO}=no"]) == 0
    assert main(["--ledger", lp, "backfill", "--window", CT, "--result", f"{LO}=no"]) == 1  # refused


# ===========================================================================
# BUG-3: day-scoped guard file — stop latching + S4 balance baseline
# ===========================================================================
from service.stops import (  # noqa: E402
    DayGuard,
    balance_loss_dollars,
    day_guard_path,
    ensure_balance_start,
    latched_stop_kind,
    parse_balance_cents,
    read_day_guard,
    record_latched_stop,
    s4_balance_breached,
    _write_day_guard,
)


def test_day_guard_missing_is_empty_not_corrupt(tmp_path):
    g = read_day_guard(day_guard_path(str(tmp_path), "2026-08-24"), "2026-08-24")
    assert g.exists is False and g.corrupt is False
    assert g.latched == () and g.balance_start_cents is None


def test_day_guard_roundtrip_and_latched_kind(tmp_path):
    p = day_guard_path(str(tmp_path), "2026-08-24")
    _write_day_guard(p, DayGuard("2026-08-24", balance_start_cents=1000,
                                 latched=({"kind": "S2", "reason": "x", "window": CT, "ts": 1.0},)))
    g = read_day_guard(p, "2026-08-24")
    assert g.balance_start_cents == 1000
    assert latched_stop_kind(g) == "S2"


def test_day_guard_corrupt_fails_closed(tmp_path):
    p = day_guard_path(str(tmp_path), "2026-08-24")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert read_day_guard(p, "2026-08-24").corrupt is True
    # wrong-day content is also corrupt (fail closed)
    _write_day_guard(p, DayGuard("2026-08-23"))
    assert read_day_guard(p, "2026-08-24").corrupt is True


def test_record_latched_stop_appends_and_survives_corrupt(tmp_path):
    p = day_guard_path(str(tmp_path), "2026-08-24")
    record_latched_stop(p, "2026-08-24", "S1", "floor violated", CT, 2.0)
    assert latched_stop_kind(read_day_guard(p, "2026-08-24")) == "S1"
    # even over a corrupt file, a new latch is recorded (and the file becomes valid+latched)
    with open(p, "w", encoding="utf-8") as f:
        f.write("garbage")
    record_latched_stop(p, "2026-08-24", "S2", "imbalance", CT, 3.0)
    assert latched_stop_kind(read_day_guard(p, "2026-08-24")) == "S2"


def test_ensure_balance_start_first_wake_then_reuse_and_new_day(tmp_path):
    p24 = day_guard_path(str(tmp_path), "2026-08-24")
    start, first = ensure_balance_start(p24, "2026-08-24", 1000000, 1.0)
    assert start == 1000000 and first is True
    # same day, later wake: baseline is reused (a lower balance-now does NOT move it)
    start2, first2 = ensure_balance_start(p24, "2026-08-24", 990000, 2.0)
    assert start2 == 1000000 and first2 is False
    # a NEW UTC day is a fresh file -> fresh snapshot
    p25 = day_guard_path(str(tmp_path), "2026-08-25")
    start3, first3 = ensure_balance_start(p25, "2026-08-25", 990000, 3.0)
    assert start3 == 990000 and first3 is True


# ===========================================================================
# BUG-3: S4 balance parsing + breach (cents integers -> Decimal dollars)
# ===========================================================================
def test_parse_balance_cents_shapes_and_failclosed():
    assert parse_balance_cents({"balance": 100000}) == 100000
    assert parse_balance_cents({"available_balance": 5025}) == 5025
    assert parse_balance_cents({"nope": 1}) is None
    assert parse_balance_cents("garbage") is None
    assert parse_balance_cents({"balance": "not-int"}) is None


def test_balance_loss_dollars_cents_to_decimal():
    # start 1000000c, now 999700c -> loss $3.00 (exact Decimal, no float wobble)
    assert balance_loss_dollars(1000000, 999700) == Decimal("3.00")
    assert balance_loss_dollars(1000000, 1000500) == Decimal("-5.00")  # a gain


def test_s4_balance_breached_at_over_and_under_cap():
    cap = Decimal("3.00")
    assert s4_balance_breached(1000000, 999700, cap) == (True, Decimal("3.00"))   # at cap
    assert s4_balance_breached(1000000, 999699, cap)[0] is True                    # over cap
    assert s4_balance_breached(1000000, 999800, cap) == (False, Decimal("2.00"))  # under cap


# ===========================================================================
# BUG-3: StopController persists day-halting trips (S1-S4) to the guard file; alarms do not.
# ===========================================================================
class _FakeExec:
    def __init__(self):
        self.armed = True

    def set_armed(self, v):
        self.armed = v


def test_controller_trip_persists_day_halting_latch(tmp_path):
    from service.journal import Journal
    from service.stops import StopController
    p = day_guard_path(str(tmp_path), "2026-08-24")
    c = StopController(_FakeExec(), Journal(), latch_path=p, utc_day="2026-08-24", window=CT,
                       clock=lambda: 1.0)
    c.trip("S2", "imbalance unrestorable")
    assert latched_stop_kind(read_day_guard(p, "2026-08-24")) == "S2"


def test_latch_does_not_carry_to_next_utc_day(tmp_path):
    # A latch on day D lives in stops_D.json; day D+1 uses stops_(D+1).json -> a fresh, un-latched
    # guard. This is what makes "a stop halts the DAY" (not forever).
    record_latched_stop(day_guard_path(str(tmp_path), "2026-08-24"), "2026-08-24", "S2", "x", CT, 1.0)
    next_day = read_day_guard(day_guard_path(str(tmp_path), "2026-08-25"), "2026-08-25")
    assert next_day.exists is False and latched_stop_kind(next_day) is None


def test_controller_alarm_does_not_latch_the_day(tmp_path):
    from service.journal import Journal
    from service.stops import StopController
    p = day_guard_path(str(tmp_path), "2026-08-24")
    c = StopController(_FakeExec(), Journal(), latch_path=p, utc_day="2026-08-24", window=CT,
                       clock=lambda: 1.0)
    c.raise_alarm("A1", "slippage")
    # no day-halting stop was tripped -> no guard file / no latch
    assert latched_stop_kind(read_day_guard(p, "2026-08-24")) is None
