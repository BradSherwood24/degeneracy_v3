"""Tests for service/stops.py — alarms, stops, position policy, arming (S5), controller."""

from __future__ import annotations

import os
from decimal import Decimal

from service.executor import Executor, ExecutorConfig
from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    Intent,
    IntentLeg,
    new_ledger,
    record_intent,
    record_response,
)
from service.orders.envelope import OrderResponse
from service.policy import Q1_STRANGLE, SUB_DOLLAR_FLIP
from service.stops import (
    A1_SLIPPAGE,
    S1_ARITH,
    S4_DAILY_LOSS,
    StopConfig,
    StopController,
    StopState,
    apply_stop,
    arming_check,
    check_s1,
    check_slippage_alarms,
    falsifier_is_frozen,
    health_has_caps,
    on_realized,
    position_policy,
)

CT = "2026-06-14T02:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTCD-LO"
CFG = StopConfig()

_FALSIFIER = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "ceremony", "falsifier.md")
)


def resp(cid, fill, price, fee):
    return OrderResponse(cid, "o", Decimal(str(fill)), Decimal(0),
                         Decimal(str(price)), Decimal(str(fee)), 1)


def flip_entry(count=1, hi_price="0.57", lo_price="0.24", source=SUB_DOLLAR_FLIP):
    legs = (
        IntentLeg(HI, "no", "buy", count, Decimal(hi_price), "h"),
        IntentLeg(LO, "yes", "buy", count, Decimal(lo_price), "l"),
    )
    return Intent(CT, source, PURPOSE_ENTRY, legs)


def filled_flip(hi_price="0.57", lo_price="0.24", hi_fee="0.01", lo_fee="0.01", source=SUB_DOLLAR_FLIP):
    st = record_intent(new_ledger(CT, source, HI, LO), flip_entry(hi_price=hi_price, lo_price=lo_price, source=source))
    st = record_response(st, resp("h", 1, hi_price, hi_fee))
    st = record_response(st, resp("l", 1, lo_price, lo_fee))
    return st


# --- A1 slippage ---
def test_a1_slippage_alarm_when_fill_far_from_limit():
    intent = flip_entry()
    responses = (resp("h", 1, "0.60", "0.01"), resp("l", 1, "0.24", "0.01"))  # high off by 3c
    alarms = check_slippage_alarms(intent, responses, CFG)
    assert len(alarms) == 1 and alarms[0].kind == A1_SLIPPAGE and alarms[0].detail["ticker"] == HI


def test_a1_no_alarm_within_2c():
    intent = flip_entry()
    responses = (resp("h", 1, "0.57", "0.01"), resp("l", 1, "0.25", "0.01"))  # 1c off
    assert check_slippage_alarms(intent, responses, CFG) == ()


# --- S1 arithmetic ---
def test_s1_clean_when_sub_dollar_pair_profitable():
    assert check_s1(filled_flip()) is None  # realized_min 0.17 >= 0


def test_s1_fires_when_realized_negative():
    st = filled_flip(hi_price="0.60", lo_price="0.45", hi_fee="0.05", lo_fee="0.05")
    reason = check_s1(st)  # cost 1.15 -> realized_min -0.15
    assert reason is not None and "floor violated" in reason


def test_s1_scoped_to_sub_dollar_only():
    # a strangle pair (not floor-protected) is never an S1 even if realized negative
    st = filled_flip(hi_price="0.60", lo_price="0.45", hi_fee="0.05", lo_fee="0.05", source=Q1_STRANGLE)
    assert check_s1(st) is None


# --- S4 daily loss ---
def test_s4_trips_at_daily_cap():
    st = StopState()
    st, tripped = on_realized(st, Decimal("-3.00"), CFG)
    assert not tripped and not st.is_stopped
    st, tripped = on_realized(st, Decimal("-2.50"), CFG)  # cumulative -5.50 <= -5.00
    assert tripped and st.has(S4_DAILY_LOSS)


# --- position policy (PENDING-BRAD F8 defaults) ---
def test_position_policy_holds_complete_flip_pair():
    st = filled_flip()  # 1:1 matched sub-$1 pair
    actions = position_policy(st, CFG)
    assert all(a.kind == "hold" for a in actions) and len(actions) == 2


def test_position_policy_flattens_unpaired_flip_overhang():
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), flip_entry(count=2))
    st = record_response(st, resp("h", 2, "0.57", "0.02"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))  # 2:1 -> 1 pair protected, 1 high overhang
    actions = position_policy(st, CFG)
    holds = [a for a in actions if a.kind == "hold"]
    flats = [a for a in actions if a.kind == "flatten"]
    assert any(a.ticker == HI and a.count == 1 for a in holds)   # matched pair held
    assert any(a.ticker == HI and a.count == 1 for a in flats)   # overhang flattened


def test_position_policy_flattens_all_strangle_legs():
    st = filled_flip(source=Q1_STRANGLE)  # strangle never floor-protected
    actions = position_policy(st, CFG)
    assert all(a.kind == "flatten" for a in actions) and len(actions) == 2


def test_position_policy_hold_when_flatten_disabled():
    cfg = StopConfig(flatten_unprotected_exposure=False)
    st = filled_flip(source=Q1_STRANGLE)
    actions = position_policy(st, cfg)
    assert all(a.kind == "hold" for a in actions)


# --- S5 arming ---
def test_falsifier_currently_draft_refuses_arming():
    # the real ceremony falsifier is STATUS: DRAFT -> must refuse
    assert falsifier_is_frozen(_FALSIFIER) is False
    health = {"orders_enabled": True, "caps": {"max_contracts_per_order": 2,
              "ticker_prefixes": ["KXBTCD"], "daily_order_budget": 100}}
    decision = arming_check(_FALSIFIER, health, policy_verified=True)
    assert not decision.armed
    assert any("FROZEN" in r for r in decision.reasons)


def test_arming_accepts_when_frozen_and_health_ok(tmp_path):
    fz = tmp_path / "falsifier.md"
    fz.write_text("# F\n\nSTATUS: FROZEN\n\nbody\n", encoding="utf-8")
    health = {"orders_enabled": True, "caps": {"max_contracts_per_order": 2,
              "ticker_prefixes": ["KXBTCD"], "daily_order_budget": 100}}
    d = arming_check(str(fz), health, policy_verified=True)
    assert d.armed and d.reasons == ()


def test_arming_refuses_on_each_failing_precondition(tmp_path):
    fz = tmp_path / "falsifier.md"
    fz.write_text("STATUS: FROZEN\n", encoding="utf-8")
    good_health = {"orders_enabled": True, "caps": {"max_contracts_per_order": 2,
                   "ticker_prefixes": ["KXBTCD"], "daily_order_budget": 100}}
    # policy not verified
    assert not arming_check(str(fz), good_health, policy_verified=False).armed
    # health missing orders_enabled
    assert not arming_check(str(fz), {"caps": good_health["caps"]}, policy_verified=True).armed
    # health missing caps
    assert not arming_check(str(fz), {"orders_enabled": True}, policy_verified=True).armed
    assert not health_has_caps({"orders_enabled": True})


# --- controller: freeze executor + dispatch flatten via stop-authorized path ---
class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _single_ok(path, body):
    return FakeResp(200, {"order": {"order_id": "o", "client_order_id": body["client_order_id"],
                                    "fill_count": str(body["count"]).split(".")[0],
                                    "remaining_count": "0", "average_fill_price": body["price"],
                                    "average_fee_paid": "0.01", "ts_ms": 1}})


def test_controller_trip_freezes_executor_and_flattens_strangle():
    posts = []
    def post(path, body):
        posts.append((path, body))
        return _single_ok(path, body)
    ex = Executor(Journal(), ExecutorConfig(armed=True), post_fn=post)
    ctrl = StopController(ex, Journal())
    st = filled_flip(source=Q1_STRANGLE)  # both legs unprotected -> flatten both
    actions = ctrl.trip(S1_ARITH, "test", ledger_state=st,
                        bids={HI: Decimal("0.55"), LO: Decimal("0.22")})
    assert ex.armed is False                       # frozen ALWAYS
    assert ctrl.state.has(S1_ARITH)
    assert len(posts) == 2                          # two flatten sells dispatched
    assert all(p[0] == "/trade-api/v2/portfolio/events/orders" for p in posts)
    assert all(a.kind == "flatten" for a in actions)


def test_controller_holds_complete_flip_pair_no_orders():
    posts = []
    ex = Executor(Journal(), ExecutorConfig(armed=True), post_fn=lambda p, b: posts.append(1) or FakeResp(200, {}))
    ctrl = StopController(ex, Journal())
    st = filled_flip()  # complete sub-$1 pair -> held, nothing flattened
    actions = ctrl.trip(S1_ARITH, "test", ledger_state=st, bids={HI: Decimal("0.55"), LO: Decimal("0.22")})
    assert ex.armed is False and posts == []       # frozen, but NO flatten (held to settlement)
    assert all(a.kind == "hold" for a in actions)


def test_controller_flatten_without_bid_holds_and_alerts():
    posts = []
    ex = Executor(Journal(), ExecutorConfig(armed=True), post_fn=lambda p, b: posts.append(1) or FakeResp(200, {}))
    ctrl = StopController(ex, Journal())
    st = filled_flip(source=Q1_STRANGLE)
    ctrl.trip(S1_ARITH, "test", ledger_state=st, bids={})  # no bids -> cannot price flatten
    assert posts == []  # never blind-sells
    assert any(n.kind == "A_FLATTEN_NO_BID" for n in ctrl.state.alarms)


def test_apply_stop_latches_and_is_idempotent():
    s = apply_stop(StopState(), S1_ARITH, "r1")
    s = apply_stop(s, S1_ARITH, "r2")  # same stop again
    assert s.tripped == (S1_ARITH,) and len(s.notifications) == 2
