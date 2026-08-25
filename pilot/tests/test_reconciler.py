"""Tests for service/reconciler.py — imbalance protocol (pure) + positions polling shell (S3)."""

from __future__ import annotations

from decimal import Decimal

from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_REBALANCE_BUY,
    PURPOSE_REBALANCE_SELL,
    Intent,
    IntentLeg,
    new_ledger,
    record_intent,
    record_response,
)
from service.orders.envelope import OrderResponse, no_fill_response
from service.policy import SUB_DOLLAR_FLIP, load_policy
from service.reconciler import (
    Balanced,
    GiveUp,
    PositionsReconciler,
    RebalanceQuotes,
    RetryBuy,
    RideToSettlement,
    SellDown,
    detect_imbalance,
    diff_positions,
    expected_positions,
    parse_positions_response,
    propose_rebalance,
)

P = load_policy()
CT = "2026-06-14T02:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTCD-LO"
CEIL = P.imbalance.pair_cost_ceiling_sub1  # 1.0320


def resp(cid, fill, price, fee):
    return OrderResponse(cid, "o", Decimal(str(fill)), Decimal(0),
                         Decimal(str(price)), Decimal(str(fee)), 1)


def entry(count=1):
    legs = (
        IntentLeg(HI, "no", "buy", count, Decimal("0.57"), "h"),
        IntentLeg(LO, "yes", "buy", count, Decimal("0.24"), "l"),
    )
    return Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY, legs)


def ledger_with(hi_fill, lo_fill):
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), entry(count=max(hi_fill, lo_fill, 1)))
    if hi_fill > 0:
        st = record_response(st, resp("h", hi_fill, "0.57", "0.01"))
    else:
        st = record_response(st, no_fill_response("h", "http_429"))
    if lo_fill > 0:
        st = record_response(st, resp("l", lo_fill, "0.24", "0.01"))
    else:
        st = record_response(st, no_fill_response("l", "http_429"))
    return st


# --- detection ---
def test_balanced_pair_no_imbalance():
    st = ledger_with(1, 1)
    assert detect_imbalance(st) is None
    assert isinstance(propose_rebalance(st, P, 300, RebalanceQuotes(), CEIL), Balanced)


def test_orphan_detected_deficient_leg():
    st = ledger_with(1, 0)  # HIGH filled, LOW orphaned
    imb = detect_imbalance(st)
    assert imb.deficient == "low" and imb.overfilled == "high" and imb.deficit == 1


# --- orphan -> retry-buy path (within ceiling, using ACTUAL fees) ---
def test_orphan_retry_buys_deficient_leg_within_ceiling():
    st = ledger_with(1, 0)  # low deficient; low held side = yes, buy at low ask
    q = RebalanceQuotes(low_buy=Decimal("0.24"))
    prop = propose_rebalance(st, P, 300, q, CEIL)
    assert isinstance(prop, RetryBuy)
    assert prop.which == "low" and prop.ticker == LO and prop.side == "yes"
    assert prop.count == 1 and prop.limit_price == Decimal("0.24")


def test_partial_then_retry_then_selldown_rounds_down():
    # high 2, low 1 -> deficient low by 1; retry-buy low if within ceiling
    st = ledger_with(2, 1)
    q = RebalanceQuotes(low_buy=Decimal("0.24"), high_sell=Decimal("0.55"))
    prop = propose_rebalance(st, P, 300, q, CEIL)
    assert isinstance(prop, RetryBuy) and prop.count == 1


# --- ceiling exceeded mid-retry -> sell-down branch (NEVER a buy above the ceiling) ---
def test_ceiling_exceeded_forces_selldown_never_buys_above():
    st = ledger_with(1, 0)  # low deficient
    # low ask is expensive: buying it would push pair cost over $1.0320/pair
    q = RebalanceQuotes(low_buy=Decimal("0.60"), high_sell=Decimal("0.55"))
    prop = propose_rebalance(st, P, 300, q, CEIL)
    # cost so far = high 0.57+0.01 = 0.58; projected buy = 0.60+fee > ceiling*1 -> sell-down
    assert isinstance(prop, SellDown)
    assert prop.which == "high" and prop.count == 1  # sell the 1 filled high down to 0 (0:0)
    # assert the ceiling was actually the blocker (a cheaper ask WOULD have bought)
    cheap = propose_rebalance(st, P, 300, RebalanceQuotes(low_buy=Decimal("0.24"),
                                                          high_sell=Decimal("0.55")), CEIL)
    assert isinstance(cheap, RetryBuy)


def test_selldown_target_rounds_down_to_min():
    st = ledger_with(2, 1)  # after retries exhausted we sell high(2) down to low(1)
    # exhaust buy retries by recording 5 rebalance-buy intents on the low leg
    for i in range(P.imbalance.max_retries_per_side):
        st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                                      (IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), f"rb{i}"),)))
    q = RebalanceQuotes(low_buy=Decimal("0.24"), high_sell=Decimal("0.55"))
    prop = propose_rebalance(st, P, 300, q, CEIL)
    assert isinstance(prop, SellDown)
    assert prop.which == "high" and prop.count == 1  # 2 -> 1 (round down to min)


# --- retry budget exhaustion + sell-down impossible -> GiveUp = S2 ---
def test_giveup_s2_when_no_quotes_and_retries_exhausted():
    st = ledger_with(1, 0)
    for i in range(P.imbalance.max_retries_per_side):
        st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                                      (IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), f"rb{i}"),)))
    # no sell quote available for the overfilled high leg -> cannot sell-down either
    prop = propose_rebalance(st, P, 300, RebalanceQuotes(low_buy=Decimal("0.24")), CEIL)
    assert isinstance(prop, GiveUp) and prop.stop == "S2"


# --- 429 on leg-2 -> NoFill -> imbalance -> retry-buy (the orphan path) ---
def test_429_leg2_produces_orphan_handled_by_retry():
    st = ledger_with(1, 0)  # LOW was a 429 no-fill
    prop = propose_rebalance(st, P, 300, RebalanceQuotes(low_buy=Decimal("0.24")), CEIL)
    assert isinstance(prop, RetryBuy) and prop.which == "low"


# --- time bounds ---
def test_no_rebalance_inside_3s_is_selldown_only():
    st = ledger_with(1, 0)
    # inside 3s but outside 1s: buy is disallowed; only sell-down of overfilled high
    prop = propose_rebalance(st, P, 2.0, RebalanceQuotes(low_buy=Decimal("0.24"),
                                                         high_sell=Decimal("0.55")), CEIL)
    assert isinstance(prop, SellDown) and prop.which == "high"


def test_inside_1s_rides_to_settlement():
    st = ledger_with(1, 0)
    prop = propose_rebalance(st, P, 0.5, RebalanceQuotes(low_buy=Decimal("0.24"),
                                                        high_sell=Decimal("0.55")), CEIL)
    assert isinstance(prop, RideToSettlement) and prop.unresolved


def test_selldown_partial_iterates():
    # high 3, low 1; a first sell-down sells 2. Simulate a PARTIAL sell of 1 -> still imbalanced
    st = record_intent(new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO), entry(count=3))
    st = record_response(st, resp("h", 3, "0.57", "0.03"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))
    for i in range(P.imbalance.max_retries_per_side):  # exhaust buys -> force sell-down
        st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_BUY,
                                      (IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), f"rb{i}"),)))
    prop = propose_rebalance(st, P, 300, RebalanceQuotes(high_sell=Decimal("0.55")), CEIL)
    assert isinstance(prop, SellDown) and prop.count == 2  # 3 -> 1
    # record a PARTIAL sell of only 1
    st = record_intent(st, Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_SELL, (prop and
                       IntentLeg(HI, "no", "sell", prop.count, prop.limit_price, "s1"),)))
    st = record_response(st, resp("s1", 1, "0.55", "0.01"))  # only 1 of 2 filled
    prop2 = propose_rebalance(st, P, 300, RebalanceQuotes(high_sell=Decimal("0.55")), CEIL)
    assert isinstance(prop2, SellDown) and prop2.count == 1  # 2 -> 1, iterate


# --- positions polling shell (S3) ---
def test_expected_positions_signed_by_held_side():
    st = ledger_with(1, 1)
    exp = expected_positions(st)
    assert exp == {HI: Decimal(-1), LO: Decimal(1)}  # held NO = -1, held YES = +1


def test_phantom_fill_poll_mismatch_is_s3():
    st = ledger_with(1, 1)  # ledger: HI -1, LO +1

    def rest_get(path, params=None):
        # exchange shows an EXTRA position the ledger doesn't know about -> phantom
        return {"market_positions": [
            {"ticker": HI, "position": -1}, {"ticker": LO, "position": 1},
            {"ticker": "KXBTCD-GHOST", "position": 2},
        ]}

    rec = PositionsReconciler(rest_get)
    result = rec.tick(st)
    assert result.stop == "S3" and "KXBTCD-GHOST" in result.mismatches


def test_believed_fill_not_on_exchange_is_s3():
    st = ledger_with(1, 1)  # ledger thinks LO +1

    def rest_get(path, params=None):
        return {"market_positions": [{"ticker": HI, "position": -1}]}  # LO missing on exchange

    result = PositionsReconciler(rest_get).tick(st)
    assert result.stop == "S3" and LO in result.mismatches


def test_matching_positions_no_stop():
    st = ledger_with(1, 1)

    def rest_get(path, params=None):
        return {"market_positions": [{"ticker": HI, "position": "-1"}, {"ticker": LO, "position": "1"}]}

    assert PositionsReconciler(rest_get).tick(st).stop is None


def test_parse_positions_shapes_and_diff():
    assert parse_positions_response({"positions": [{"ticker": "A", "position": 2}]}) == {"A": Decimal(2)}
    assert diff_positions({"A": Decimal(1)}, {"A": Decimal(2)}) == ("A",)
    assert diff_positions({"A": Decimal(1)}, {"A": Decimal(1)}) == ()
