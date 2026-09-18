"""PARTIAL-FILL WINGS (Brad 2026-09-18): on each rest fill EVENT take both wings sized to the fill;
the still-resting remainder keeps being requoted; each later fill spawns its own wing batch; a completed
SET = one rest-fill event with both wings filled; realized lock is per contract; the fill rate counts
set events. ``params.contracts`` = 1 must be byte-identical to the pre-partial build (the hard
acceptance test).

Pure-core tests drive ``decide_v32`` directly (no network/disk beyond the shipped policy); the
driver/ledger/report tests use the FrozenExecutor (fakes only). 2026-08-20..29 (holdout) and the
2026-08-02..18 seal are never touched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.v32 import (
    BUY_NO,
    BUY_YES,
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    V32Params,
    V32State,
    decide_v32,
    load_v32_params,
    lock_value,
)
from service.v32.report import build_falsifier_scoreboard
import service.run_v32 as R

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000

BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"    # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"    # -> 79700
STK_SU2 = "KXBTCD-26SEP0416-T79799.99"   # -> 79800 (Su of the 79700 spot bucket)
B_SD = "KXBTC-RANGE-B79600"
B_SU = "KXBTC-RANGE-B79700"


def _top(bid: str, ask: str, *, suspect: bool = False) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=suspect,
    )


def _params(**over) -> V32Params:
    p = load_v32_params()
    return replace(p, **over) if over else p


def _state(params: V32Params) -> V32State:
    return V32State.new(CLOSE, T, BK, params)


def _fresh_books(now: float):
    # yields W=1.4290, cap=0.64, desired_n=0.45 at E=0.10 (the golden-hour numbers).
    return [
        BookUpdate(B_SD, _top("0.35", "0.36"), now),
        BookUpdate(STK_SU, _top("0.36", "0.37"), now),
        BookUpdate(STK_SD, _top("0.75", "0.76"), now),
    ]


def _feed(params, st, event):
    return decide_v32(params, st, event)


def _feed_all(params, st, events):
    acts: list = []
    for e in events:
        st, a = _feed(params, st, e)
        acts += a
    return st, acts


def _bring_up_live_rest(params, st, now):
    """Place + ack a live rest at n=0.45 with count = params.contracts. Returns (st, coid, order_id)."""
    st, acts = _feed_all(params, st, _fresh_books(now))
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert place, "expected a PLACE_REST"
    coid = place[-1].client_order_id
    assert place[-1].count == params.contracts   # rests the full allotment
    st, _ = _feed(params, st, OrderAck(coid, "OID1", now))
    assert st.rest_live is not None and st.rest_live.price == Decimal("0.45")
    assert st.rest_live.count == params.contracts
    return st, coid, "OID1"


# ===========================================================================
# CORE: partial fill -> wings sized to the fill, remainder stays resting
# ===========================================================================
def test_partial_fill_wings_sized_to_fill_remainder_stays_resting():
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    # 1 of 2 fills -> take wings for count 1, remainder (1) stays resting at the SAME order.
    st, acts = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1 and tw[0].count == 1
    assert len(tw[0].legs) == 2 and all(l.count == 1 for l in tw[0].legs)
    # the second lot is NOT orphaned: rest_live is still tracked with remaining 1.
    assert st.rest_live is not None and st.rest_live.order_id == oid and st.rest_live.count == 1
    assert st.rest_remaining == 1 and not st.rest_allotment_done
    assert len(st.rest_fills) == 1 and st.rest_fills[0].count == 1
    assert len(st.wing_batches) == 1 and st.partial_fills == 1
    # the remainder keeps being requoted at count 1: a wing move re-solves n and replaces with count 1.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))     # keep Su fresh
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))  # dn drifts -> replace
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    st, _ = _feed(p, st, OrderCancelled(oid, now + 1.05, Decimal(0)))           # cancel confirmed
    st, acts = _feed_all(p, st, [
        BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1.1),
        BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1.1),
    ])
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert place and place[-1].count == 1   # remainder re-quoted at the reduced size


def test_second_fill_spawns_second_batch_and_allotment_done_stops_quoting():
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    # the remaining lot fills -> a SECOND wing batch (its own two legs) and the allotment is done.
    st, acts = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.2))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1 and tw[0].count == 1               # sized to the second fill
    assert len(st.wing_batches) == 2 and len(st.rest_fills) == 2
    # batch legs are distinct (independent client_order_ids per batch).
    b0 = [l for l in st.wing_legs if l.batch == 0]
    b1 = [l for l in st.wing_legs if l.batch == 1]
    assert len(b0) == 2 and len(b1) == 2
    assert {l.client_order_id for l in b0}.isdisjoint({l.client_order_id for l in b1})
    # allotment done -> quoting stops (one allotment per hour).
    assert st.rest_allotment_done and st.rest_remaining == 0
    assert st.rest_live is None and st.rest_pending is None and st.partial_fills == 1
    st, acts = _feed_all(p, st, _fresh_books(now + 0.3))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]


def test_both_batches_complete_counts_two_sets():
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.2))
    assert st.sets_done == 0
    # complete both batches' wings.
    for l in list(st.wing_legs):
        px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
        st, _ = _feed(p, st, Fill("f" + l.client_order_id, l.client_order_id, Decimal(1), px,
                                  l.side, now + 0.3))
    assert st.sets_done == 2 and not st.wings_needed and not st.one_legged
    assert all(b.completed for b in st.wing_batches)


# ===========================================================================
# CORE: cumulative -> delta booking (the nastiest bug class)
# ===========================================================================
def test_cumulative_delta_on_cancel_books_only_the_delta():
    # one lot booked via a ws Fill, then the cancel reports filled 2 (CUMULATIVE) -> book exactly 1 more.
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    assert len(st.rest_fills) == 1 and st.rest_booked_by_coid[coid] == 1
    st, acts = _feed(p, st, OrderCancelled(oid, now + 0.2, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2                     # exactly ONE more booked
    assert [rf.count for rf in st.rest_fills] == [1, 1]
    assert st.rest_booked_by_coid[coid] == 2
    assert st.rest_allotment_done                      # both lots now accounted
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS]  # wings for the delta lot


def test_cancel_full_fill_before_cancel_still_single_batch_at_contracts2():
    # a cancel that reports the WHOLE allotment filled at once (no prior ws lot) -> one batch of 2.
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, OrderCancelled(oid, now + 0.1, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 1 and st.rest_fills[0].count == 2
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert tw and tw[0].count == 2 and st.rest_allotment_done


def test_duplicate_wing_fill_multi_batch_no_double_count():
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.2))
    # complete batch 0 only.
    b0 = [l for l in st.wing_legs if l.batch == 0]
    for l in b0:
        px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
        st, _ = _feed(p, st, Fill("f" + l.client_order_id, l.client_order_id, Decimal(1), px,
                                  l.side, now + 0.3))
    assert st.sets_done == 1
    # a DUPLICATE fill of a batch-0 leg (fill channel + status poll) must not re-count the set.
    l = b0[0]
    px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
    st, _ = _feed(p, st, Fill("dup", l.client_order_id, Decimal(1), px, l.side, now + 0.4))
    assert st.sets_done == 1


# ===========================================================================
# CORE: quote-end cancel of the remainder; per-batch one-legged at cutoff
# ===========================================================================
def test_quote_end_cancels_partial_remainder():
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    assert st.rest_live is not None and st.rest_live.count == 1   # remainder still resting
    later = T - 200  # t_to_close 200 < quote_end_s 300 -> past window
    st, acts = _feed(p, st, ClockTick(later))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]  # remainder cancelled
    sd = [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert sd and sd[0].reason == "past_quote_end"
    assert st.rest_live is None


def test_per_batch_one_legged_at_cutoff():
    # batch 0 completes; batch 1's wings never complete -> only batch 1 is flagged one_legged.
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    b0 = [l for l in st.wing_legs if l.batch == 0]
    for l in b0:
        px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
        st, _ = _feed(p, st, Fill("f" + l.client_order_id, l.client_order_id, Decimal(1), px,
                                  l.side, now + 0.2))
    # second lot fills late; its strike feed dies before the wings complete.
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.3))
    # advance to the settle cutoff without completing batch 1.
    st, _ = _feed(p, st, ClockTick(server_ts=T - 0.5))
    assert st.wing_batches[0].completed and not st.wing_batches[0].one_legged
    assert not st.wing_batches[1].completed and st.wing_batches[1].one_legged
    assert st.one_legged is True and st.sets_done == 1


# ===========================================================================
# CONTRACTS=1 REGRESSION: single batch, mirrors byte-identical to the pre-partial build
# ===========================================================================
def test_contracts1_single_batch_mirrors():
    p = _params()  # contracts = 1
    assert p.contracts == 1
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, acts = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    tw = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(tw) == 1 and tw[0].count == 1 and tw[0].lock == Decimal("0.1036")
    # the scalar mirrors carry the single-set values the old build exposed.
    assert st.rest_fill is not None and st.rest_fill.count == 1 and st.wing_taken
    assert st.wings_needed and not st.one_legged
    assert len(st.wing_batches) == 1 and st.rest_allotment_done and st.rest_remaining == 0
    assert st.partial_fills == 0 and len(st.wing_legs) == 2 and st.wing_legs[0].batch == 0
    # complete both wings -> one set, byte-identical latch.
    yc, nc = st.wing_legs[0].client_order_id, st.wing_legs[1].client_order_id
    st, _ = _feed(p, st, Fill("Y1", yc, Decimal(1), Decimal("0.76"), "yes", now + 0.2))
    st, _ = _feed(p, st, Fill("N1", nc, Decimal(1), Decimal("0.64"), "no", now + 0.3))
    assert st.sets_done == 1 and not st.wings_needed
    # no second rest placed (one allotment per hour).
    st, acts = _feed_all(p, st, _fresh_books(now + 0.4))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]


# ===========================================================================
# LEDGER + REPORT: per-set money math and set counting (state built via the core)
# ===========================================================================
from types import SimpleNamespace  # noqa: E402

from service._simlaw import fee as _sfee  # noqa: E402
from service.v32.ledger import build_v32_ledger_row  # noqa: E402


def _completed_state(params, *, fills, complete_all=True):
    """Build a completed contracts=N window state via the core: place+ack, then ``fills`` (a list of
    per-fill lot counts), completing every batch's wings unless ``complete_all`` is False."""
    st = _state(params)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(params, st, now)
    ts = now
    for k in fills:
        ts += 0.1
        st, _ = _feed(params, st, Fill(oid, coid, Decimal(int(k)), Decimal("0.45"), "no", ts))
    if complete_all:
        ts += 0.1
        for l in list(st.wing_legs):
            px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
            st, _ = _feed(params, st, Fill("f" + l.client_order_id, l.client_order_id,
                                           Decimal(int(l.count)), px, l.side, ts))
    return st


def _stub_executor(state):
    """A minimal executor stub carrying a money-math ``fills`` list (rest + wing legs) so
    ``_compute_money_math`` runs its full (non-early-return) path; per-set slots come from ``state``."""
    fills: list = []
    for rf in state.rest_fills:
        fills.append({"leg": "rest", "side": "no", "ticker": B_SD, "price": rf.price,
                      "count": int(rf.count), "fee": Decimal(0)})
    for l in state.wing_legs:
        if l.status == "filled" and l.fill_price is not None:
            fills.append({"leg": "wing", "side": l.side, "ticker": l.ticker, "price": l.fill_price,
                          "count": int(l.count), "fee": _sfee(l.fill_price)})
    return SimpleNamespace(fills=fills, counts={})


def _row(state, params, money):
    return build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None, params=params,
        state=state, driver_counts={}, executor_counts={}, ws_counts={}, strike_count=2,
        strike_generations=1, bucket_count=2, bucket_generations=1, strike_lag_seconds=0.1,
        bucket_lag_seconds=0.1, journal_path=None, record_count=0, stand_down_reason=None, now=0.0,
        armed=True, **money,
    )


def test_contracts2_two_sets_money_math_and_scoreboard():
    # two 1-lot fills, both hedged -> two completed SETS, per-contract locks, scoreboard n=2.
    p = _params(contracts=2)
    st = _completed_state(p, fills=[1, 1])
    assert st.sets_done == 2 and st.rest_allotment_done
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["lots_filled"] == 2 and money["lots_unfilled_at_quote_end"] == 0
    assert money["partial_fills"] == 1 and len(money["wing_batch_sets"]) == 2
    assert all(b["completed"] for b in money["wing_batch_sets"])
    # each set's lock is PER CONTRACT (comparable to the size-1 history).
    expect = lock_value(Decimal("0.45"),
                        Decimal("0.76") + _sfee(Decimal("0.76")) + Decimal("0.64") + _sfee(Decimal("0.64")))
    assert [Decimal(b["realized_lock"]) for b in money["wing_batch_sets"]] == [expect, expect]
    row = _row(st, p, money)
    assert row["lots_filled"] == 2 and len(row["wing_batch_sets"]) == 2 and len(row["rest_fills"]) == 2
    sb = build_falsifier_scoreboard([row])
    assert sb["n"] == 2 and sb["fills_total"] == 2 and sb["one_legged"] == 0


def test_contracts1_ledger_row_additive_keys_single_set():
    # contracts=1: the new keys carry the single-set values; the scoreboard counts one set; and the
    # scalar realized_lock stays present (backward compatible), equal to the one set's per-contract lock.
    p = _params()  # contracts = 1
    st = _completed_state(p, fills=[1])
    assert st.sets_done == 1
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["lots_filled"] == 1 and money["lots_unfilled_at_quote_end"] == 0
    assert money["partial_fills"] == 0 and len(money["wing_batch_sets"]) == 1
    assert money["wing_batch_sets"][0]["completed"] is True
    assert money["realized_lock"] == Decimal(money["wing_batch_sets"][0]["realized_lock"])
    row = _row(st, p, money)
    sb = build_falsifier_scoreboard([row])
    assert sb["n"] == 1 and sb["fills_total"] == 1


def test_contracts2_one_set_one_legged_scoreboard():
    # one batch completes, the other is one-legged at the cutoff -> scoreboard: n=1 completed, 1 legged.
    p = _params(contracts=2)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))
    for l in [l for l in st.wing_legs if l.batch == 0]:
        px = Decimal("0.76") if l.side == BUY_YES else Decimal("0.64")
        st, _ = _feed(p, st, Fill("f" + l.client_order_id, l.client_order_id, Decimal(1), px, l.side,
                                  now + 0.2))
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.3))
    st, _ = _feed(p, st, ClockTick(server_ts=T - 0.5))   # cutoff: batch 1 one-legged
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    row = _row(st, p, money)
    sb = build_falsifier_scoreboard([row])
    assert sb["n"] == 1 and sb["one_legged"] == 1 and sb["fills_total"] == 2


# ===========================================================================
# B1 (BLOCKING, from the PR #62 review): a missed-WS lot caught only by an EAGER-CLEAR cancel
# (quote-end / bucket-change) must be booked + hedged from the cumulative OrderCancelled, never dropped
# naked to settlement.
# ===========================================================================
def test_quote_end_cancel_books_missed_second_lot():
    """contracts=2: lot 1 fills on ws; lot 2 fills at the venue but its ws fill is MISSED. The remainder
    is caught only by the T-5 quote-end cancel, whose OrderCancelled reports the CUMULATIVE filled=2. The
    core MUST book the delta lot (via cancel_ctx) and take its wings -- else lot 2 settles naked/unbooked.
    (Fresh strike books are fed at the cancel instant so the wings can be taken -- t_to_close ~200s is
    far above the T-1 s cutoff.)"""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    assert st.rest_live is not None and st.rest_live.count == 1
    # T-5 quote-end: the remainder is cancelled (rest_live eagerly nulled), cancel_ctx remembers it.
    st, acts = _feed(p, st, ClockTick(T - 200))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert st.rest_live is None and oid in st.cancel_ctx
    # keep the wing books fresh at the cancel instant (a live strike feed ticks continuously).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), T - 199))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), T - 199))
    # the executor's cancel confirm carries venue truth: both lots filled (cumulative 2).
    st, acts = _feed(p, st, OrderCancelled(oid, T - 199, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2, "lot 2 must be booked from the cumulative cancel"
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS], "lot 2 must get wings"
    assert st.rest_allotment_done
    # N3: the booked lot is NOT misclassified as an unfilled remainder.
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["lots_filled"] == 2 and money["lots_unfilled_at_quote_end"] == 0
    assert money["lots_filled"] + money["lots_unfilled_at_quote_end"] == p.contracts


def test_bucket_change_cancel_books_missed_second_lot():
    """B1 bucket-change variant: lot 1 fills on ws, then the spot bucket moves so the remainder is
    eagerly cancelled by the bucket-change branch; a later OrderCancelled(filled=2) must still book +
    hedge lot 2 via cancel_ctx (at the ORIGINAL resting price, not the drifted desired_n)."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)   # spot 79600
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    assert st.rest_live is not None and st.spot_Sd == 79600
    # the spot bucket moves to 79700 (its mid now the highest) -> the remainder on 79600 is eagerly
    # cancelled (bucket-change branch, or the stale-wing no-quote branch while 79700's strikes catch up);
    # either eager-clear path records cancel_ctx. Collect all ticks' actions.
    st, acts = _feed_all(p, st, [
        BookUpdate(B_SU, _top("0.50", "0.52"), now + 1),      # 79700 becomes spot
        BookUpdate(STK_SU, _top("0.75", "0.76"), now + 1),    # Sd=79700 yes_ask
        BookUpdate(STK_SU2, _top("0.36", "0.37"), now + 1),   # Su=79800 no_ask
    ])
    assert st.spot_Sd == 79700
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert st.rest_live is None and oid in st.cancel_ctx
    # the cancel confirm reports cumulative 2 -> lot 2 booked at the original resting price via cancel_ctx.
    st, acts = _feed(p, st, OrderCancelled(oid, now + 1.1, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2
    assert st.rest_fills[1].price == Decimal("0.45")   # original resting price, not desired_n
    assert st.rest_allotment_done


# ===========================================================================
# N1: the 1 s status poll backstops the missed 2nd lot at contracts>1 (delta-aware), and is a no-op at
# contracts=1 / for a duplicate cumulative report.
# ===========================================================================
def _shakedown_driver(params):
    import os as _os
    import tempfile
    from service.record_range import StreamJournal
    d = tempfile.mkdtemp()
    j = StreamJournal(_os.path.join(d, "w.jsonl"), flush_every=1)
    j.open()
    state = V32State.new(CLOSE, T, BK, params, shakedown=True)
    drv = R.V32Driver(params, state, j, R.FrozenExecutor(BK), clock=lambda: 0.0)
    return drv, j


def _place_rest_via_driver(drv, now):
    drv.on_book_update(B_SD, _top("0.35", "0.36"), now)
    drv.on_book_update(STK_SU, _top("0.36", "0.37"), now)
    drv.on_book_update(STK_SD, _top("0.75", "0.76"), now)
    assert drv.state.rest_live is not None
    return drv.state.rest_live.client_order_id, drv.state.rest_live.order_id


def test_poll_backstops_missed_second_lot_contracts2():
    p = _params(contracts=2, tol=Decimal("0.50"), deb_ms=100000)  # no drift-requote churn
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    n = drv.state.rest_live.price
    # lot 1 arrives on the ws channel; its wings synth-fill -> set 1.
    drv.on_fill(B_SD, {"client_order_id": coid, "order_id": oid, "trade_id": "t1", "count": 1,
                       "purchased_side": "no", "yes_price_dollars": str(Decimal(1) - n)}, now + 0.1)
    assert drv.state.rest_remaining == 1 and drv.state.sets_done == 1
    # lot 2 fills silently (ws MISSED); the next poll reports the CUMULATIVE filled 2 -> exactly one more.
    drv.on_poll_fill(oid, 2, now + 0.2)
    assert len(drv.state.rest_fills) == 2 and drv.state.rest_allotment_done
    assert drv.counts["rest_fill_poll"] == 1 and drv.state.sets_done == 2
    # a further poll reporting 2 again -> nothing (delta 0).
    drv.on_poll_fill(oid, 2, now + 0.3)
    assert len(drv.state.rest_fills) == 2 and drv.counts["rest_fill_poll"] == 1
    j.close()


def test_poll_noop_at_contracts1_after_ws_fill():
    # contracts=1 byte-identical: after the ws lot fully fills, a poll reporting the same total does
    # nothing (delta 0) -- no rest_fill_poll, no extra booking.
    p = _params()  # contracts = 1
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    n = drv.state.rest_live.price
    drv.on_fill(B_SD, {"client_order_id": coid, "order_id": oid, "trade_id": "t1", "count": 1,
                       "purchased_side": "no", "yes_price_dollars": str(Decimal(1) - n)}, now + 0.1)
    assert drv.state.sets_done == 1 and len(drv.state.rest_fills) == 1
    drv.on_poll_fill(oid, 1, now + 0.2)
    assert drv.counts.get("rest_fill_poll", 0) == 0 and len(drv.state.rest_fills) == 1
    j.close()


def test_poll_first_when_ws_missed_books_the_lot():
    # if the ws fill is missed entirely, the poll (delta over 0 booked) books the lot -- same as before.
    p = _params()  # contracts = 1
    drv, j = _shakedown_driver(p)
    now = T - 600
    coid, oid = _place_rest_via_driver(drv, now)
    drv.on_poll_fill(oid, 1, now + 0.2)
    assert drv.counts["rest_fill_poll"] == 1 and len(drv.state.rest_fills) == 1
    assert drv.state.sets_done == 1  # wings synth-completed
    j.close()


# ===========================================================================
# N2: book_late_rest_fill's coarse guard is defended by the cancel path (a distinct 2nd late lot is
# caught by the cumulative cancel, and the late-ws copy is a genuine duplicate this guard drops).
# ===========================================================================
def test_book_late_second_lot_caught_by_cancel_path():
    from service.v32 import book_late_rest_fill
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    # eager-clear (quote-end) then the cumulative cancel books lot 2 (the cancel path is the backstop).
    st, _ = _feed(p, st, ClockTick(T - 200))
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), T - 199))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), T - 199))
    st, _ = _feed(p, st, OrderCancelled(oid, T - 199, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2
    # a late-ws copy of lot 2 now reaches book_late_rest_fill -> genuine duplicate, dropped (no 3rd fill).
    st2, acts = book_late_rest_fill(p, st, price=Decimal("0.45"), count=1, server_ts=T - 198,
                                    bucket_Sd=79600)
    assert len(st2.rest_fills) == 2 and acts == []
