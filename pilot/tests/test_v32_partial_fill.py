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
    OrderAmended,
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


def _params1(**over) -> V32Params:
    """The REAL frozen params with ``contracts`` forced to 1 -- the SIZE-1 REGRESSION suite must keep
    testing size 1 byte-identically even after AMENDMENT 1 (2026-09-20) raised the real file to
    ``contracts`` = 2. Add-only: the size-1 tests below route through this; the size-2 tests use the
    real value (``_params(contracts=2, ...)`` == the real file)."""
    return replace(load_v32_params(), contracts=1, **over)


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
    # the remainder keeps being requoted at count 1, now via AMEND-FIRST (rest_live carries an order_id,
    # so a same-bucket replace amends the order in place rather than cancel+create). The amend body carries
    # the still-resting REMAINDER count (1), NOT params.contracts (amend-first rebase, task point 5).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))     # keep Su fresh
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))  # dn drifts -> replace
    amend = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amend and amend[-1].count == 1   # remainder amended at the reduced size, never contracts=2
    assert st.amend_in_flight
    new_coid = amend[-1].updated_client_order_id
    st, _ = _feed(p, st, OrderAmended(order_id=oid, client_order_id=new_coid, price=amend[-1].price,
                                      server_ts=now + 1.05, remaining_count=Decimal(1),
                                      fill_count=Decimal(0), average_fill_price=None))
    assert not st.amend_in_flight
    # one order still resting (the same order_id) at count 1; the pre-amend lot is not re-booked.
    assert st.rest_live is not None and st.rest_live.order_id == oid and st.rest_live.count == 1
    assert st.rest_remaining == 1 and not st.rest_allotment_done
    assert len(st.rest_fills) == 1 and st.rest_booked_by_coid.get(new_coid) == 1


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
    # runs against the REAL frozen params -- after AMENDMENT 1 (2026-09-20) the shipped file IS contracts=2.
    p = _params()
    assert p.contracts == 2 and p.sha256 == load_v32_params().sha256
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
    p = _params1()  # contracts = 1 (forced; real file is 2 after AMENDMENT 1)
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
    p = _params1()  # contracts = 1 (forced; real file is 2 after AMENDMENT 1)
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
# AMEND-FIRST x PARTIAL-FILL interplay (amend-first rebase 2026-09-19): an amend rotates the coid while
# the order_id persists, so rest_booked_by_coid must follow the order across the amend (carry-forward),
# and an amend that crosses books its per-amend fill delta (Kalshi Amend Order V2: fill_count is the
# fills FROM THE AMEND, not cumulative).
# ===========================================================================
def test_amend_then_quote_end_cancel_books_missed_second_lot():
    """Task point 6: contracts=2, lot 1 fills on ws, the remainder is AMENDED (coid rotates coid_a->coid_b,
    order_id persists), then lot 2 fills at the venue but its ws fill is MISSED. The T-5 quote-end cancel
    eager-clears the amended order and its OrderCancelled reports the CUMULATIVE filled=2. Because
    ``_apply_amended`` carries ``rest_booked_by_coid`` FORWARD to the new coid, ``_apply_cancelled`` books
    EXACTLY ONE more lot (delta = 2 - 1) and hedges it -- never re-booking the pre-amend lot (which the
    old, un-carried arithmetic would: delta would read 2 and over-fill), never dropping lot 2 naked."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid_a, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid_a, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    assert st.rest_booked_by_coid.get(coid_a) == 1 and st.rest_live.count == 1
    # drift -> AMEND the remainder (amend-first); coid rotates to coid_b, order_id persists.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    amend = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amend and amend[-1].count == 1
    coid_b = amend[-1].updated_client_order_id
    st, _ = _feed(p, st, OrderAmended(order_id=oid, client_order_id=coid_b, price=amend[-1].price,
                                      server_ts=now + 1.05, remaining_count=Decimal(1),
                                      fill_count=Decimal(0), average_fill_price=None))
    # the pre-amend booking followed the order to the new coid (carry-forward); coid_a is gone.
    assert st.rest_booked_by_coid.get(coid_b) == 1 and coid_a not in st.rest_booked_by_coid
    # T-5 quote-end: the amended remainder is cancelled (rest_live eagerly nulled), cancel_ctx remembers it.
    st, acts = _feed(p, st, ClockTick(T - 200))
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert st.rest_live is None and oid in st.cancel_ctx
    # keep the wing books fresh at the cancel instant so the delta lot's wings can be taken.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), T - 199))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), T - 199))
    # the cancel confirm carries venue truth: both lots filled (cumulative 2).
    st, acts = _feed(p, st, OrderCancelled(oid, T - 199, filled_count_before_cancel=Decimal(2)))
    assert len(st.rest_fills) == 2, "exactly one more lot booked from the cumulative cancel (delta=1)"
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS], "lot 2 must get wings"
    assert st.rest_allotment_done
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["lots_filled"] == 2 and money["lots_unfilled_at_quote_end"] == 0


def test_amend_cross_books_per_amend_delta_and_latches_allotment():
    """Task point 7: an amend whose price crosses the book fills at the venue. Kalshi Amend Order V2
    reports fill_count / average_fill_price for the fills FROM THE AMEND ONLY (per-amend, verified against
    docs.kalshi.com), so that count IS the newly-filled delta. After a 1-of-2 ws fill, an amend that
    crosses and fills the remaining lot books ONE more batch at the amend's average_fill_price and latches
    ``rest_allotment_done`` -- total 2 lots, booked once (carried-forward 1 + amend delta 1), never doubled."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid_a, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, Fill(oid, coid_a, Decimal(1), Decimal("0.45"), "no", now + 0.1))  # lot 1 (ws)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    amend = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amend and amend[-1].count == 1
    coid_b = amend[-1].updated_client_order_id
    # keep strike books fresh so the amend-cross wings can be taken (t_to_close far above the T-1 s cutoff).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1.04))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 1.04))
    # the amend crossed and filled the remaining 1 lot (per-amend fill_count=1 at avg 0.44, remaining 0).
    st, acts = _feed(p, st, OrderAmended(order_id=oid, client_order_id=coid_b, price=amend[-1].price,
                                         server_ts=now + 1.05, remaining_count=Decimal(0),
                                         fill_count=Decimal(1), average_fill_price=Decimal("0.44")))
    assert len(st.rest_fills) == 2                      # lot1 (ws) + lot2 (amend cross)
    assert st.rest_fills[1].price == Decimal("0.44") and st.rest_fills[1].count == 1
    assert st.rest_booked_by_coid.get(coid_b) == 2     # carried-forward 1 + amend delta 1 (no double-book)
    assert st.rest_allotment_done and st.rest_live is None
    assert [a for a in acts if a.kind == ActionKind.TAKE_WINGS], "the amend-cross lot must get wings"


def test_partial_amend_cross_ws_echo_not_double_booked():
    """N1 (rebase review, must-fix before contracts=2 + proxy cap): a PARTIAL amend cross leaves the
    remainder resting under the new coid, so the venue may ALSO echo that crossed lot on the WS fill
    channel. The Amend V2 response carries NO trade_id, so the driver's trade-id dedup can't catch the
    echo; the core's amend_cross_pending guard must skip it so rest_fills / wing batches / booked do NOT
    double. A genuinely NEW WS fill of the last lot afterwards must still book (guard already consumed)."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid_a, oid = _bring_up_live_rest(p, st, now)      # full 2-lot allotment resting, no fill yet
    assert st.rest_live.count == 2 and st.rest_remaining is None
    # drift -> AMEND the full allotment (count 2, no fill yet); coid rotates to coid_b.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    amend = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amend and amend[-1].count == 2
    coid_b = amend[-1].updated_client_order_id
    # keep strike books fresh so the cross's wings can be taken.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1.04))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 1.04))
    # the amend crosses PARTIALLY: fills 1 of the 2 resting lots -> remaining 1, rest_live RETAINED.
    st, acts = _feed(p, st, OrderAmended(order_id=oid, client_order_id=coid_b, price=amend[-1].price,
                                         server_ts=now + 1.05, remaining_count=Decimal(1),
                                         fill_count=Decimal(1), average_fill_price=Decimal("0.44")))
    assert len(st.rest_fills) == 1 and len(st.wing_batches) == 1
    assert st.rest_booked_by_coid.get(coid_b) == 1 and st.rest_remaining == 1
    assert not st.rest_allotment_done and st.rest_live is not None
    assert st.amend_cross_pending.get(oid) == 1        # the echo guard armed for exactly this crossed lot
    # the venue ECHOES the crossed lot on the WS fill channel -> must be SKIPPED (no double-book, no batch).
    st, echo_acts = _feed(p, st, Fill(oid, coid_b, Decimal(1), Decimal("0.44"), "no", now + 1.1))
    assert len(st.rest_fills) == 1 and len(st.wing_batches) == 1, "the ws echo must not book again"
    assert st.rest_booked_by_coid.get(coid_b) == 1
    assert not [a for a in echo_acts if a.kind == ActionKind.TAKE_WINGS], "no wing batch for the echo"
    assert oid not in st.amend_cross_pending             # guard consumed
    # a GENUINELY NEW ws fill of the last resting lot must now book (guard already spent) + latch allotment.
    st, new_acts = _feed(p, st, Fill(oid, coid_b, Decimal(1), Decimal("0.45"), "no", now + 2.0))
    assert len(st.rest_fills) == 2 and len(st.wing_batches) == 2
    assert st.rest_booked_by_coid.get(coid_b) == 2 and st.rest_allotment_done and st.rest_live is None
    assert [a for a in new_acts if a.kind == ActionKind.TAKE_WINGS], "the real last lot must get wings"


def test_full_amend_cross_ws_echoes_book_nothing_extra():
    """N1 full-cross variant: an amend that crosses the FULL resting allotment (fill_count=2) books one
    batch of 2 and nulls rest_live (allotment done), so subsequent WS echoes of those lots find no tracked
    order and book nothing extra -- no pending guard is even needed (rest_live is None)."""
    p = _params(contracts=2, tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, coid_a, oid = _bring_up_live_rest(p, st, now)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _top("0.60", "0.61"), now + 1))
    amend = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert amend and amend[-1].count == 2
    coid_b = amend[-1].updated_client_order_id
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1.04))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76"), now + 1.04))
    # the amend crosses the FULL 2 lots at once.
    st, acts = _feed(p, st, OrderAmended(order_id=oid, client_order_id=coid_b, price=amend[-1].price,
                                         server_ts=now + 1.05, remaining_count=Decimal(0),
                                         fill_count=Decimal(2), average_fill_price=Decimal("0.44")))
    assert len(st.rest_fills) == 1 and st.rest_fills[0].count == 2
    assert st.rest_booked_by_coid.get(coid_b) == 2 and st.rest_allotment_done and st.rest_live is None
    assert not st.amend_cross_pending             # a full cross needs no echo guard (rest_live nulled)
    # two WS echoes of the crossed lots -> nothing extra (no tracked order to match).
    st, e1 = _feed(p, st, Fill(oid, coid_b, Decimal(1), Decimal("0.44"), "no", now + 1.1))
    st, e2 = _feed(p, st, Fill(oid, coid_b, Decimal(1), Decimal("0.44"), "no", now + 1.2))
    assert len(st.rest_fills) == 1 and len(st.wing_batches) == 1
    assert st.rest_booked_by_coid.get(coid_b) == 2
    assert not [a for a in (e1 + e2) if a.kind == ActionKind.TAKE_WINGS]


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
    p = _params1()  # contracts = 1 (forced; real file is 2 after AMENDMENT 1)
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
    p = _params1()  # contracts = 1 (forced; real file is 2 after AMENDMENT 1)
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


# ===========================================================================
# MONEY-MATH FEE SCALING (2026-09-20 fix). The venue charges the taker fee ONCE PER FILL
# (ceil(0.07*p*(1-p)*count)); the per-contract ``fee`` on a fill record must be scaled by count in
# ``realized_delta``'s cost, not added once. Reproduced on the first live size-2 set (04:00Z).
# ===========================================================================
from service.v32.ledger import v32_set_floor_dollars  # noqa: E402
from service._simlaw import fee as _law_fee            # noqa: E402


def test_size2_0400z_realized_delta_uses_total_fees():
    """REGRESSION — live ledger row close_time 2026-09-20T04:00:00Z (first size-2 set).

    rest NO 2@0.27 fee 0 (maker), wings YES 2@0.82 (per-contract fee 0.0103) + NO 2@0.77 (0.0124).
    The venue charged the per-FILL total fee (ceil(0.07*p*(1-p)*2)): YES 0.0207, NO 0.0248. So
    total cost = 2*(0.27+0.82+0.77) + 0.0207 + 0.0248 = 3.7655, floor = $4.00 (complete 3-leg pin x2),
    realized_delta = +0.2345 -- matching the live balance move 53.3069 -> 53.5414. The pre-fix cost
    added each per-contract fee ONCE (0.0103 + 0.0124), over-stating realized_delta by $0.0228 (the
    second lot's wing fees) to 0.2573."""
    p = _params(contracts=2)
    st = _completed_state(p, fills=[2])
    assert v32_set_floor_dollars(3, 2) == Decimal("4.00")  # geometry: 3 legs held x 2 lots -> $4 floor
    exec_stub = SimpleNamespace(counts={}, fills=[
        {"leg": "rest", "side": "no", "ticker": B_SD, "price": Decimal("0.27"), "count": 2,
         "fee": Decimal("0.000000")},
        {"leg": "wing", "side": BUY_YES, "ticker": "KXBTCD-26SEP2000-T80399.99",
         "price": Decimal("0.82"), "count": 2, "fee": Decimal("0.0103")},
        {"leg": "wing", "side": BUY_NO, "ticker": "KXBTCD-26SEP2000-T80499.99",
         "price": Decimal("0.77"), "count": 2, "fee": Decimal("0.0124")},
    ])
    money = R._compute_money_math(st, exec_stub, contracts=p.contracts)
    assert money["realized_delta"] == Decimal("0.2345")
    # unambiguous fee keys on every fill record: fee (per contract) + fee_total (all lots) + fee_source
    wf = {f["side"]: f for f in money["wing_fills"]}
    assert wf[BUY_YES]["fee"] == Decimal("0.0103")
    assert wf[BUY_YES]["fee_total"] == Decimal("0.0207") and wf[BUY_YES]["fee_source"] == "law_total"
    assert wf[BUY_NO]["fee"] == Decimal("0.0124")
    assert wf[BUY_NO]["fee_total"] == Decimal("0.0248") and wf[BUY_NO]["fee_source"] == "law_total"
    rest = money["fills"][0]
    assert rest["fee_total"] == Decimal(0) and rest["fee_source"] == "maker_zero"


def test_size1_single_lot_realized_delta_byte_identical_to_pre_fix():
    """At contracts=1 with a 1-lot set the fix is a NO-OP: at count 1 the per-contract fee already IS
    the venue per-fill total, so the cost (and thus realized_delta) is byte-identical to the pre-fix
    'per-contract fee added once' arithmetic. Guards the 7 size-1 history rows."""
    p = _params1()  # contracts = 1
    st = _completed_state(p, fills=[1])
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    # reproduce the EXACT pre-fix cost: sum(price*count) + per-contract fee added ONCE per fill.
    fills = _stub_executor(st).fills
    pre_fix_cost = sum(
        (Decimal(str(f["price"])) * Decimal(int(f["count"]))) + Decimal(str(f.get("fee") or 0))
        for f in fills
    )
    held = 3  # bucket-NO + both wings
    pre_fix_delta = v32_set_floor_dollars(held, 1) - pre_fix_cost
    assert money["realized_delta"] == pre_fix_delta
    # and every fee_total at count 1 equals its per-contract fee (source per_contract / maker_zero).
    for f in list(money["fills"]) + list(money["wing_fills"]):
        assert Decimal(str(f["fee_total"])) == Decimal(str(f.get("fee") or 0))
        assert f["fee_source"] in ("per_contract", "maker_zero")


def test_fill_total_fee_helper_scale_and_source():
    """``_fill_total_fee`` unit contract: maker (fee 0) -> (0, maker_zero); taker count 1 -> the
    per-contract fee itself (per_contract), which equals the frozen per-contract law; taker count>=2 ->
    the venue per-fill ceiling ``_fee_total`` (law_total), NOT per-contract*count."""
    # maker leg
    assert R._fill_total_fee({"price": Decimal("0.27"), "count": 2, "fee": Decimal(0)}) == (Decimal(0), "maker_zero")
    # taker count 1: per-contract fee IS the total, and equals the frozen law at count 1
    tot1, src1 = R._fill_total_fee({"price": Decimal("0.82"), "count": 1, "fee": Decimal("0.0104")})
    assert src1 == "per_contract" and tot1 == Decimal("0.0104")
    assert R._fee_total(Decimal("0.82"), 1) == _law_fee(Decimal("0.82"))
    # taker count 2: venue per-fill ceiling (ceil applied ONCE to the whole fill)
    tot2, src2 = R._fill_total_fee({"price": Decimal("0.82"), "count": 2, "fee": Decimal("0.0103")})
    assert src2 == "law_total" and tot2 == Decimal("0.0207")
    assert R._fee_total(Decimal("0.77"), 2) == Decimal("0.0248")
