"""Phase-3 adversarial review probes (SEPARATE opus48 reviewer, 2026-08-21).

Locks the fixes made in this review and plants visible failing-forward markers for the findings
handed to Phase 4 / Brad:

  * R1 FIXED  — FEE_IS_TOTAL fail-closed direction (over-count at fill_count>=2).
  * R2 ADDED  — crash-mid-REBALANCE rebuild (PLAN Phase-3 exit fault case, previously untested).
  * R3 MARKER — S4 daily-loss and A4 guard-trip counters live on a PER-PROCESS StopState; in the
                process-per-window model they RESET every window, so as built they are per-window,
                not per-UTC-day. Documented here so Phase 4 wires a persisted UTC-day tally.
  * R4 MARKER — a stop-authorized flatten is dispatched WITHOUT the no-orders-to-settle cutoff
                (build_flatten_intent leaves t_minus_s=None and StopController.trip passes no
                t_minus), so a flatten can fire inside the 1s settle cutoff. PENDING-BRAD F8.
"""

from __future__ import annotations

from decimal import Decimal

from service import ledger as ledger_mod
from service.executor import Executor, ExecutorConfig
from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_REBALANCE_SELL,
    Intent,
    IntentLeg,
    new_ledger,
    rebuild_from_journal,
    record_intent,
    record_response,
)
from service.orders.envelope import OrderResponse
from service.policy import SUB_DOLLAR_FLIP
from service.stops import (
    S4_DAILY_LOSS,
    StopConfig,
    StopState,
    on_realized,
    record_guard_trip,
)

CT = "2026-06-14T02:00:00Z"
HI, LO = "KXBTCD-HI", "KXBTCD-LO"


def resp(cid, fill, price, fee, order_id="o"):
    return OrderResponse(cid, order_id, Decimal(str(fill)), Decimal(0),
                         Decimal(str(price)), Decimal(str(fee)), 1)


# --------------------------------------------------------------------------- #
# R1 — FEE_IS_TOTAL fail-closed: at fill_count>=2 fees are counted PER CONTRACT #
#      (multiplied), the more-halting direction the commission demands.         #
# --------------------------------------------------------------------------- #
def test_fee_is_total_default_is_fail_closed_multiply():
    assert ledger_mod.FEE_IS_TOTAL is False
    # a 2-contract buy leg with average_fee_paid "0.02": fail-closed cost counts 0.02*2 = 0.04.
    st = record_intent(
        new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO),
        Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY,
               (IntentLeg(HI, "no", "buy", 2, Decimal("0.57"), "h"),
                IntentLeg(LO, "yes", "buy", 2, Decimal("0.24"), "l"))),
    )
    st = record_response(st, resp("h", 2, "0.57", "0.02"))
    st = record_response(st, resp("l", 2, "0.24", "0.02"))
    hi = st.position("high")
    # notional 2*0.57 = 1.14; fee 0.02*2 = 0.04 -> cost 1.18 (NOT 1.16 which the old True default gave)
    assert hi.buy_fees == Decimal("0.04")
    assert hi.bought_cost == Decimal("1.18")
    # realized_min uses actual (multiplied) fees -> the safe, more-halting number.
    # matched 2 pairs * $1 = 2.00 ; cost = 1.18 (hi) + (0.48 + 0.04) (lo) = 1.70 -> realized 0.30
    assert st.realized_min() == Decimal("0.30")


def test_fee_single_contract_is_unchanged_by_the_flip():
    # at fill_count == 1 total == per-contract, so nothing changes at 1-pair size.
    st = record_intent(
        new_ledger(CT, SUB_DOLLAR_FLIP, HI, LO),
        Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY,
               (IntentLeg(HI, "no", "buy", 1, Decimal("0.57"), "h"),
                IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), "l"))),
    )
    st = record_response(st, resp("h", 1, "0.57", "0.01"))
    st = record_response(st, resp("l", 1, "0.24", "0.01"))
    assert st.realized_min() == Decimal("0.17")


# --------------------------------------------------------------------------- #
# R2 — crash mid-REBALANCE: an entry filled, a rebalance-sell intent journaled  #
#      but its response lost to the crash -> rebuild flags the sell cid inflight #
#      and positions reflect the pre-sell state (reconcile-first then decides).  #
# --------------------------------------------------------------------------- #
def test_crash_mid_rebalance_rebuild_flags_sell_inflight():
    j = Journal()
    entry = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_ENTRY,
                   (IntentLeg(HI, "no", "buy", 2, Decimal("0.57"), "h"),
                    IntentLeg(LO, "yes", "buy", 1, Decimal("0.24"), "l")))
    j.append("order_intent", ledger_mod.intent_to_record(entry), 0.0)
    j.append("order_response", ledger_mod.response_to_record(resp("h", 2, "0.57", "0.02")), 0.1)
    j.append("order_response", ledger_mod.response_to_record(resp("l", 1, "0.24", "0.01")), 0.2)
    # imbalance 2:1 -> a sell-down of the overfilled high leg is dispatched (intent journaled)...
    sell = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_SELL,
                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.55"), "s1", reduce_only=True),))
    j.append("order_intent", ledger_mod.intent_to_record(sell), 0.3)
    # ...CRASH before the sell response is journaled.
    st = rebuild_from_journal(j.records(), CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.net("high") == 2 and st.net("low") == 1     # pre-sell position preserved
    assert st.inflight_cids() == ("s1",)                   # the sell leg is in flight -> reconcile-first


def test_crash_mid_rebalance_via_executor_journal():
    """Same, driven through the real Executor: the sell intent is journaled BEFORE the POST, so a
    response lost to a crash still leaves the intent recoverable + flagged inflight."""
    j = Journal()

    def post(path, body):
        cid = body["client_order_id"]
        return type("R", (), {"status_code": 200, "json": lambda self: {"order": {
            "order_id": "o", "client_order_id": cid, "fill_count": "1", "remaining_count": "0",
            "average_fill_price": "0.55", "average_fee_paid": "0.01", "ts_ms": 1}}})()

    ex = Executor(j, ExecutorConfig(armed=True), post_fn=post)
    sell = Intent(CT, SUB_DOLLAR_FLIP, PURPOSE_REBALANCE_SELL,
                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.55"), "s1", reduce_only=True),),
                  t_minus_s=300)
    ex.execute(sell)
    # drop the sell response from the flushed journal (crash lost it)
    records = [r for r in j.records()
               if not (r["kind"] == "order_response" and r["obj"].get("client_order_id") == "s1")]
    st = rebuild_from_journal(records, CT, SUB_DOLLAR_FLIP, HI, LO)
    assert st.inflight_cids() == ("s1",)


# --------------------------------------------------------------------------- #
# R3 MARKER — S4 / A4 counters reset per window (per-process StopState).        #
# --------------------------------------------------------------------------- #
def test_MARKER_s4_daily_loss_resets_with_a_fresh_state_per_window():
    cfg = StopConfig()
    # window 1: accumulate -4.00 (no stop; cap is -5.00)
    s = StopState()
    s, tripped = on_realized(s, Decimal("-4.00"), cfg)
    assert not tripped
    # window 2 begins as a NEW process -> a FRESH StopState: the -4.00 is FORGOTTEN.
    s2 = StopState()
    s2, tripped2 = on_realized(s2, Decimal("-4.00"), cfg)
    # -8.00 across the UTC day should have tripped S4 ($5 cap); it does NOT, because the tally reset.
    assert not tripped2 and not s2.has(S4_DAILY_LOSS)
    # DOCUMENTED GAP: Phase 4 must persist the UTC-day realized tally (like the proxy OrderBudget).


def test_MARKER_a4_guard_trips_reset_with_a_fresh_state_per_window():
    cfg = StopConfig()
    s = StopState()
    for _ in range(4):
        s, note = record_guard_trip(s, cfg)
    assert note is None and s.guard_trips == 4      # 4 trips in window 1, no stand-down yet
    s2 = StopState()                                 # new window -> counter reset
    s2, note2 = record_guard_trip(s2, cfg)
    assert note2 is None and s2.guard_trips == 1     # the 5th trip of the day counts as the 1st
    # DOCUMENTED GAP: A4 ("5 guard trips in one UTC day") needs a persisted per-day counter.


# --------------------------------------------------------------------------- #
# R4 MARKER — a stop-authorized flatten dispatches without the settle cutoff.   #
# --------------------------------------------------------------------------- #
def test_MARKER_stop_flatten_dispatches_inside_settle_cutoff():
    posts = []

    def post(path, body):
        posts.append((path, body))
        return type("R", (), {"status_code": 200, "json": lambda self: {"order": {
            "order_id": "o", "client_order_id": body["client_order_id"], "fill_count": "1",
            "remaining_count": "0", "average_fill_price": "0.55", "average_fee_paid": "0.01",
            "ts_ms": 1}}})()

    ex = Executor(Journal(), ExecutorConfig(armed=False), post_fn=post)
    # a flatten intent as build_flatten_intent makes it: t_minus_s defaults to None
    flat = Intent(CT, SUB_DOLLAR_FLIP, "flatten",
                  (IntentLeg(HI, "no", "sell", 1, Decimal("0.55"), "f1", reduce_only=True),))
    assert flat.t_minus_s is None
    # StopController.trip calls execute(intent, stop_authorized=True) with NO t_minus -> cutoff skipped
    r = ex.execute(flat, stop_authorized=True)
    assert r.dispatched and len(posts) == 1
    # GAP: even deep inside the 1s settle cutoff a stop-flatten would POST. PENDING-BRAD F8 ruling;
    # Phase 4 should pass an event-derived t_minus (or the ruling must exempt flattens explicitly).
