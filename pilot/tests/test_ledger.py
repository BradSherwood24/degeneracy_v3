"""Tests for service/ledger.py — position accounting, fills record (F3), crash rebuild, S1 math."""

from __future__ import annotations

from decimal import Decimal

from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_REBALANCE_BUY,
    Intent,
    IntentLeg,
    fills_record,
    intent_to_record,
    new_ledger,
    rebuild_from_journal,
    record_intent,
    record_response,
    response_to_record,
    retries_for_side,
    to_window_fills,
)
from service.orders.envelope import OrderResponse, no_fill_response
from service.policy import SUB_DOLLAR_FLIP

CT = "2026-06-14T02:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTCD-LO"


def resp(cid, fill, price, fee, order_id="o"):
    return OrderResponse(
        client_order_id=cid, order_id=order_id, fill_count=Decimal(str(fill)),
        remaining_count=Decimal(0), average_fill_price=Decimal(str(price)),
        average_fee_paid=Decimal(str(fee)), ts_ms=1,
    )


def entry_intent(hi_cid="h", lo_cid="l", hi_price="0.57", lo_price="0.24", count=1):
    legs = (
        IntentLeg(HI, "no", "buy", count, Decimal(hi_price), hi_cid),
        IntentLeg(LO, "yes", "buy", count, Decimal(lo_price), lo_cid),
    )
    return Intent(window=CT, source=SUB_DOLLAR_FLIP, purpose=PURPOSE_ENTRY, legs=legs)


def base():
    return new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO)


def test_entry_captures_held_sides_and_positions():
    st = record_intent(base(), entry_intent())
    assert st.high_side == "no" and st.low_side == "yes"
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))
    assert st.net("high") == 1 and st.net("low") == 1
    assert st.is_balanced() and st.matched_pairs() == 1
    hi = st.position("high")
    assert hi.bought == 1 and hi.avg_buy_price == Decimal("0.57")
    assert hi.bought_cost == Decimal("0.58")  # 0.57 + 0.01 fee


def test_no_fill_response_moves_no_position():
    st = record_intent(base(), entry_intent())
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, no_fill_response("l", "http_429"))
    assert st.net("high") == 1 and st.net("low") == 0  # orphan
    assert not st.is_balanced()
    # both cids responded -> nothing in flight
    assert st.inflight_cids() == ()


def test_unknown_cid_response_recorded_but_no_position():
    st = record_intent(base(), entry_intent())
    st = record_response(st, resp("UNKNOWN", 1, "0.57", "0.01"))
    assert st.net("high") == 0 and st.net("low") == 0
    assert len(st.responses) == 1


def test_realized_min_positive_for_sub_dollar_pair():
    st = record_intent(base(), entry_intent())
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))
    # pair cost 0.58 + 0.25 = 0.83; worst-case payoff 1.00 -> realized_min 0.17 > 0
    assert st.realized_min() == Decimal("0.17")


def test_realized_min_negative_triggers_s1_condition():
    # fills whose actual pair cost exceeds $1 (fee/pricing wrong) -> realized_min < 0
    st = record_intent(base(), entry_intent(hi_price="0.60", lo_price="0.45"))
    st = record_response(st, resp("h", 1, "0.60", "0.05"))
    st = record_response(st, resp("l", 1, "0.45", "0.05"))
    # cost 0.65 + 0.50 = 1.15; realized_min = 1.00 - 1.15 = -0.15
    assert st.realized_min() == Decimal("-0.15")


def test_sell_down_reduces_net_and_recovers_proceeds():
    st = record_intent(base(), entry_intent(count=2))
    st = record_response(st, resp("h", 2, "0.57", "0.02"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))  # only 1 of low filled
    assert st.net("high") == 2 and st.net("low") == 1
    # sell 1 of the overfilled high (held NO) at bid; a sell of the held outcome
    sell = Intent(CT, SUB_DOLLAR_FLIP, "rebalance-sell",
                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.55"), "s1", reduce_only=True),))
    st = record_intent(st, sell)
    st = record_response(st, resp("s1", 1, "0.55", "0.01"))
    assert st.net("high") == 1 and st.net("low") == 1 and st.is_balanced()


def test_retries_for_side_counts_intents():
    st = record_intent(base(), entry_intent())
    assert retries_for_side(st, HI, PURPOSE_REBALANCE_BUY) == 0
    rb = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                (IntentLeg(HI, "no", "buy", 1, Decimal("0.55"), "rb1"),))
    st = record_intent(st, rb)
    st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                                  (IntentLeg(HI, "no", "buy", 1, Decimal("0.55"), "rb2"),)))
    assert retries_for_side(st, HI, PURPOSE_REBALANCE_BUY) == 2


# --- F3 fills record: per-leg ticker + price + fee, keyed to the paired tickers ---
def test_fills_record_carries_paired_tickers_price_and_fee():
    st = record_intent(base(), entry_intent())
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, resp("l", 1, "0.24", "0.02"))
    rec = fills_record(st)
    assert rec["filled"] is True and rec["imbalance"] is False
    tickers = {lg["ticker"] for lg in rec["legs"]}
    assert tickers == {HI, LO}  # keyed to the ACTUAL paired tickers -> parity always comparable
    hi_leg = next(lg for lg in rec["legs"] if lg["ticker"] == HI)
    assert hi_leg["avg_price"] == Decimal("0.57") and hi_leg["avg_fee"] == Decimal("0.01")
    # pair cost 0.58 + 0.26 = 0.84; realized_min 1.00 - 0.84 = 0.16
    assert rec["realized_payoff"] == Decimal("0.16")


def test_to_window_fills_adapter_maps_paired_tickers():
    st = record_intent(base(), entry_intent())
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))
    wf = to_window_fills(st)
    assert wf.filled and not wf.imbalance
    assert {lg.ticker for lg in wf.legs} == {HI, LO}
    hi = next(lg for lg in wf.legs if lg.ticker == HI)
    assert hi.avg_price == Decimal("0.57")  # comparable to sim.high_leg_price -> bin-5 legit


# --- crash-mid-order rebuild ---
def test_rebuild_from_journal_reconstructs_positions_and_flags_inflight():
    j = Journal()
    intent = entry_intent()
    j.append("order_intent", intent_to_record(intent), 0.0)
    j.append("kalshi_ws", {"noise": 1}, 0.1)  # ignored kind
    # only the HIGH leg's response was journaled before the crash -> LOW is in flight
    j.append("order_response", response_to_record(resp("h", 1, "0.57", "0.01")), 0.2)
    st = rebuild_from_journal(j.records(), CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.net("high") == 1 and st.net("low") == 0
    assert st.inflight_cids() == ("l",)  # the LOW leg intent had no response -> reconcile-first


def test_rebuild_round_trips_through_flush(tmp_path):
    j = Journal()
    intent = entry_intent()
    j.append("order_intent", intent_to_record(intent), 0.0)
    j.append("order_response", response_to_record(resp("h", 1, "0.57", "0.01")), 0.1)
    j.append("order_response", response_to_record(resp("l", 1, "0.24", "0.01")), 0.2)
    path = str(tmp_path / "w.jsonl")
    j.flush(path)
    from service.journal import load_journal

    reloaded = load_journal(path)
    st = rebuild_from_journal(reloaded.records(), CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.is_balanced() and st.matched_pairs() == 1
    assert st.realized_min() == Decimal("0.17")


def test_intent_record_round_trips_exchange_index():
    from service.ledger import intent_from_record
    legs = (
        IntentLeg(HI, "no", "buy", 1, Decimal("0.57"), "h", exchange_index=2),
        IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), "l", exchange_index=2),
    )
    intent = Intent(window=CT, source=SUB_DOLLAR_FLIP, purpose=PURPOSE_ENTRY, legs=legs)
    rec = intent_to_record(intent)
    assert all(lg["exchange_index"] == 2 for lg in rec["legs"])
    back = intent_from_record(rec)
    assert all(lg.exchange_index == 2 for lg in back.legs)


def test_rebuild_pre_fix_journal_without_exchange_index_key():
    # A journal written BEFORE this fix has order_intent leg dicts with NO exchange_index key at all.
    # Rebuild must still work (rebuild never dispatches); the legs default to exchange_index None.
    from service.ledger import intent_from_record
    pre_fix = {
        "window": CT, "source": SUB_DOLLAR_FLIP, "purpose": PURPOSE_ENTRY, "t_minus_s": 300.0,
        "legs": [
            {"ticker": HI, "side": "no", "action": "buy", "count": 1, "limit_price": "0.57",
             "client_order_id": "h", "reduce_only": None},   # NOTE: no "exchange_index" key
            {"ticker": LO, "side": "yes", "action": "buy", "count": 1, "limit_price": "0.24",
             "client_order_id": "l", "reduce_only": None},
        ],
    }
    intent = intent_from_record(pre_fix)
    assert all(lg.exchange_index is None for lg in intent.legs)
    j = Journal()
    j.append("order_intent", pre_fix, 0.0)
    j.append("order_response", response_to_record(resp("h", 1, "0.57", "0.01")), 0.1)
    j.append("order_response", response_to_record(resp("l", 1, "0.24", "0.01")), 0.2)
    st = rebuild_from_journal(j.records(), CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.is_balanced() and st.matched_pairs() == 1
