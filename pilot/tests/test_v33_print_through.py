"""PRINT-THROUGH WINGS (Brad, 2026-09-26): the early-hedge trigger for the V3.3 ladder.

The feature ships OFF (``print_through`` false) -> the ladder is byte-identical (proved by the whole
existing suite passing unchanged). These tests ENABLE it via ``replace(params, print_through=True, ...)``.

Round-2 semantics (after the adversarial review, findings F1-F7):
  * F5: a print pre-hedges a rung only when it is APPROACHING that rung's offer from BELOW, within
    ``print_through_ticks`` -- the print sits in [offer - ticks*1c, offer - 1c]. A lone deep print no
    longer pre-hedges the whole cheaper ladder.
  * F6: the pre-emptive trigger fires only inside the live quoting window, and only on a live-resting rung.
  * F2: a stall CANCELS the unfilled rests first and finalises (complete/unwind) only once those cancels
    confirm -- a racing fill attributes to the same batch (no second take, no double-buy).
  * F1: the fail-closed / unwind path sells ONLY the wing that actually filled (never a naked short).
  * F3/F4: a complete books the taker bucket-NO from the IOC response; a complete/unwind shortfall stands
    the window down.

No network, no disk beyond the shipped policy. Holdout / seal are never touched.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import fee  # noqa: F401
from service.book import TopOfBook
from service.v33 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import LegOrder, V33Action, V33ActionKind
from service.v33.core import lock_value, print_through_summary  # noqa: F401

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000
BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"   # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"   # -> 79700
B_SD = "KXBTC-RANGE-B79600"
_ONE = Decimal(1)
_CENT = Decimal("0.01")
_EPS = Decimal("0.000000001")


def _top(bid: str, ask: str) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=_ONE - ya, no_bid_size=Decimal(100), no_ask=_ONE - yb, no_ask_size=Decimal(100),
        suspect=False,
    )


def _sd(ask: str) -> TopOfBook:
    return _top(str(Decimal(ask) - _CENT), ask)


def _books(now: float, sd_ask: str = "0.76", *, b_bid: str = "0.35", su_bid: str = "0.36"):
    return [
        BookUpdate(B_SD, _top(b_bid, str(Decimal(b_bid) + _CENT)), now),
        BookUpdate(STK_SU, _top(su_bid, str(Decimal(su_bid) + _CENT)), now),
        BookUpdate(STK_SD, _sd(sd_ask), now),
    ]


def _params(**over):
    # L4 (2026-09-29): PIN E_min to 0.05 (controlled ladder). The shipped policy is E_min 0.08 (8..18c),
    # which lowers n_top 3c; these tests' hard-coded rung prices (n_top 0.50, offers 0.50/0.51/0.52, ...)
    # were authored around the 5..15c anchor and the mechanism is E_min-invariant. Shipped E_min asserted
    # in test_v33_params.py / test_v33_hardening.py.
    p = replace(load_v33_params(), E_min=Decimal("0.05"))
    base = dict(print_through=True, tol=Decimal("0.01"), deb_ms=0,
                freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)
    base.update(over)
    return replace(p, **base)


def _feed(p, st, ev):
    st, a = decide_v33(p, st, ev)
    st.check_invariants(p)
    return st, a


def _feed_all(p, st, events):
    acts = []
    for e in events:
        st, a = _feed(p, st, e)
        acts += a
    return st, acts


def _bring_up(p, st, now):
    st, acts = _feed_all(p, st, _books(now))
    places = [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.WOULD_PLACE_REST)]
    assert places
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    assert all(o.live for o in st.ladder) and st.n_top == Decimal("0.50")
    return st


def _state(p, *, shakedown=False):
    return V33State.new(CLOSE, T, BK, p, shakedown=shakedown)


def _fill_wings(p, st, batch_index, now):
    """Fill both pre-taken wing legs of a batch at their limits (simulate the IOC completing)."""
    for lg in [l for l in st.wing_legs if l.batch == batch_index and l.status == "pending"]:
        st, _ = _feed(p, st, Fill(None, lg.client_order_id, Decimal(lg.count), lg.limit, lg.side, now))
    return st


# ===========================================================================
# F5 -- the distance band: fire only when APPROACHING the offer from below within ticks
# ===========================================================================
def test_trigger_fires_one_tick_below_offer():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    # ticks=1: a print at 0.49 is exactly one tick below the top rung's offer (0.50 -> n 0.50). ONE rung.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1 and len(st.print_through) == 1
    assert st.print_through[0].rung_prices == (Decimal("0.50"),) and st.print_through[0].count == 1
    assert st.rungs_filled == 0 and len(st.ladder) == 11   # nothing filled/removed on the print itself


def test_trigger_band_widens_with_ticks():
    p = _params(print_through_ticks=3)
    st = _bring_up(p, _state(p), T - 600)
    # ticks=3, print 0.49 -> offers 0.50/0.51/0.52 (n 0.50/0.49/0.48) qualify; 0.53 (n 0.47) does NOT.
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    assert set(st.print_through[0].rung_prices) == {Decimal("0.50"), Decimal("0.49"), Decimal("0.48")}


def test_no_trigger_on_lone_deep_print_far_above_offer():
    # F5: a lone high print (0.65, far above every offer) no longer pre-hedges the whole cheaper ladder.
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.65"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and st.print_through == ()


def test_no_trigger_on_print_below_band():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    # 0.40 is >1 tick below every offer (min offer 0.50) -> no trigger.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.40"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and st.print_through == ()


def test_no_trigger_when_no_rest_live():
    p = _params()
    st = _state(p)  # no ladder placed
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and st.print_through == ()


def test_no_trigger_when_feature_off():
    # L4 (2026-09-29): PIN E_min 0.05 (controlled ladder, n_top 0.50) -- this builds params directly rather
    # than via _params(), so it needs the same pin; print_through stays False (feature-off check).
    p = replace(load_v33_params(), E_min=Decimal("0.05"), tol=Decimal("0.01"), deb_ms=0,
                freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)  # print_through False
    st = _bring_up(p, _state(p), T - 600)
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and st.print_through == ()


def test_no_side_taker_does_not_trigger():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "no", Decimal(5), T - 599))
    assert st.print_through == ()


# ===========================================================================
# F6 -- window / live-resting gate
# ===========================================================================
def test_no_trigger_after_quote_end():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    # a print AFTER quote-end (T - quote_end_s = T-300; use T-200 -> t_to_close 200 < 300) does not fire.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 200))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and st.print_through == ()


def test_no_trigger_before_quote_start():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    # a print BEFORE quote-start (T - quote_start_s = T-900; use T-950 -> t_to_close 950 > 900) does not fire.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 950))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]


# ===========================================================================
# Attribution (no double take)
# ===========================================================================
def test_fill_after_trigger_attributes_batch_once():
    p = _params(print_through_ticks=3)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    covered = st.print_through[0].rung_coids
    # fill one pre-hedged rung -> it attaches to the SAME batch; no new coalesce group, no second take.
    o = next(x for x in st.ladder if x.client_order_id == covered[0])
    st, acts = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", T - 598))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]   # NO second take
    assert st.coalesce_open is None and len(st.wing_batches) == 1
    assert st.wing_batches[0].total_count == 1
    assert st.rungs_filled == 1 and len(st.ladder) == 10


def test_completed_print_through_set_counts_and_books():
    p = _params(print_through_ticks=3)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    st = _fill_wings(p, st, b_idx, T - 598)
    for coid in st.print_through[0].rung_coids:
        o = next(x for x in st.ladder if x.client_order_id == coid)
        st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", T - 597))
    b = st.wing_batches[0]
    assert b.completed and st.sets_done == 3 and st.print_through[0].resolution == "filled"


# ===========================================================================
# F2 -- stall cancel-race: a fill racing the cancel attributes to the batch, no second take
# ===========================================================================
def test_stall_cancel_race_no_double_take():
    p = _params(print_through_ticks=1, print_through_stall_ms=1000, print_through_min_lock_c=-50)
    st = _bring_up(p, _state(p, shakedown=True), T - 600)   # shakedown so finalise resolves synchronously
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    trig = st.print_through[0]
    rung = next(o for o in st.ladder if o.client_order_id == trig.rung_coids[0])
    rung_oid = rung.order_id
    b_idx = st.wing_batches[0].index
    st = _fill_wings(p, st, b_idx, T - 598.9)
    # stall: begin cancels the unfilled rest, waits for the confirm (does NOT complete yet).
    st, acts = _feed(p, st, ClockTick(T - 597))
    assert st.print_through[0].stall_pending and not st.print_through[0].resolved
    assert [a for a in acts if a.kind == ActionKind.WOULD_CANCEL_REST]
    assert not [a for a in acts if a.kind in (V33ActionKind.WOULD_TAKE_BUCKET_NO,
                                              V33ActionKind.TAKE_BUCKET_NO)]   # NOT completed yet
    # now the pre-hedged rung FILLS in the race (its cancel confirm carries filled_before_cancel=1).
    st, acts2 = _feed(p, st, OrderCancelled(rung_oid, T - 596, filled_count_before_cancel=Decimal(1)))
    # the racing fill attributed to the SAME batch -> shortfall 0 -> resolved filled; NO taker, NO 2nd take.
    assert len(st.wing_batches) == 1
    assert st.wing_batches[0].completed and st.sets_done == 1
    assert st.print_through[0].resolution == "filled"
    assert st.rungs_filled == 1                     # exposure == the one real set (no double bucket-NO)
    assert not [a for a in acts2 if a.kind in (V33ActionKind.WOULD_TAKE_BUCKET_NO,
                                               ActionKind.TAKE_WINGS, ActionKind.WOULD_TAKE_WINGS)]


# ===========================================================================
# Stall policy (via the dry driver -- cancels auto-confirm through the FrozenExecutor)
# ===========================================================================
class _J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))

    def kinds(self):
        return [k for k, _ in self.recs]


def _dry(p):
    import service.run_v33 as RUN
    from service.run_v32 import FrozenExecutor
    st = V33State.new(CLOSE, T, BK, p, shakedown=True)
    drv = RUN.V33Driver(p, st, _J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    t0 = T - 600
    for m, tb in ((B_SD, ("0.35", "0.36")), (STK_SU, ("0.36", "0.37")), (STK_SD, ("0.75", "0.76"))):
        drv.on_book_update(m, _top(*tb), t0)
    assert len(drv.state.ladder) == 11 and drv.state.n_top == Decimal("0.50")
    return drv, t0


def test_stall_completes_when_lock_clears_floor():
    # completing via a bucket-NO TAKER at the ask is costlier than the maker rung; a -$0.50 floor admits it.
    drv, t0 = _dry(_params(print_through_ticks=1, print_through_stall_ms=1000,
                           print_through_min_lock_c=-50))
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.49", "count_fp": "1.00"}, t0 + 1)
    assert len(drv.state.print_through) == 1 and drv.state.rungs_filled == 0   # 0.49 does not cross
    drv.on_clock_tick(t0 + 3)   # 2 s > 1 s stall -> begin -> cancel auto-confirms -> finalise -> complete
    trig = drv.state.print_through[0]
    assert trig.resolved and trig.resolution == "complete" and trig.complete_price == Decimal("0.65")
    assert "would_print_through_complete" in drv.journal.kinds()
    assert drv.state.sets_done == 1


def test_stall_unwinds_when_lock_below_floor():
    drv, t0 = _dry(_params(print_through_ticks=1, print_through_stall_ms=1000,
                           print_through_min_lock_c=100))   # impossibly high floor -> unwind
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.49", "count_fp": "1.00"}, t0 + 1)
    drv.on_clock_tick(t0 + 3)
    trig = drv.state.print_through[0]
    assert trig.resolved and trig.resolution == "unwind" and trig.roundtrip_cost is not None
    assert "would_print_through_unwind" in drv.journal.kinds()
    assert drv.state.wing_batches == () and drv.state.sets_done == 0
    assert "print_through_complete" not in drv.journal.kinds()


def test_policy_unwind_forces_unwind():
    drv, t0 = _dry(_params(print_through_ticks=1, print_through_stall_ms=1000,
                           print_through_policy="unwind"))
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.49", "count_fp": "1.00"}, t0 + 1)
    drv.on_clock_tick(t0 + 3)
    assert drv.state.print_through[0].resolution == "unwind"


# ===========================================================================
# F1 -- partial wing fill fails closed and sells ONLY the filled wing (no naked short)
# ===========================================================================
def test_partial_wing_fill_fails_closed_sells_only_filled_leg():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    legs = [l for l in st.wing_legs if l.batch == b_idx]
    yes_leg = next(l for l in legs if l.side == "yes")
    no_leg = next(l for l in legs if l.side == "no")
    # YES wing fills; NO wing comes back UNFILLED (ask moved past our tight limit).
    st, _ = _feed(p, st, Fill(None, yes_leg.client_order_id, Decimal(yes_leg.count),
                              yes_leg.limit, "yes", T - 598))
    st, acts = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", T - 598))
    # fail closed: cancel the rest, stand down, and unwind ONLY the YES leg we actually hold.
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    unwinds = [a for a in acts if a.kind == V33ActionKind.UNWIND_WINGS]
    assert len(unwinds) == 1
    sell_legs = unwinds[0].legs
    # exactly ONE sell leg: the YES wing (the one that filled). NEVER the never-filled NO wing.
    assert len(sell_legs) == 1
    assert sell_legs[0].side == "yes" and sell_legs[0].action == "sell"
    assert sell_legs[0].count == int(yes_leg.count)
    assert st.stood_down and st.pt_stood_down and st.one_legged
    assert st.print_through[0].resolution == "partial"


def test_stand_down_blocks_further_triggers():
    p = _params(print_through_ticks=1)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    no_leg = next(l for l in st.wing_legs if l.side == "no")
    st, _ = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", T - 598))
    assert st.pt_stood_down
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.48"), "yes", Decimal(5), T - 597))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]


def test_print_through_summary_shape():
    p = _params(print_through_ticks=3)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.49"), "yes", Decimal(5), T - 599))
    s = print_through_summary(st)
    assert len(s) == 1 and s[0]["count"] == 3 and s[0]["resolved"] is False
    assert s[0]["yes_print"] == "0.49" and s[0]["lock_at_trigger"] is not None


# ===========================================================================
# Golden fixture replays: a FAST sweep (pre-hedge + complete at pre-jump ask), a SLOW STALL
# ===========================================================================
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v33",
                    "print_through_sweeps.json")


def _fixture():
    if not os.path.exists(_FIX):
        pytest.skip("print-through fixture absent")
    with open(_FIX, encoding="utf-8") as f:
        return json.load(f)


def test_fixture_not_holdout_or_seal():
    assert _fixture()["close_time"] == "2026-09-26T22:00:00Z"   # not 2026-08-* holdout/seal


def test_golden_fast_sweep_prehedges_at_pre_jump_ask_and_completes():
    """A FAST sweep climbs one tick at a time; each print pre-hedges the rung one tick above it at the ask
    the sweep started from, and the next print (the cross) fills that rung and attributes to the batch."""
    _fixture()
    p = _params(print_through_ticks=1, print_through_stall_ms=5000)
    st = _bring_up(p, _state(p), T - 600)
    early_yes_ask = None
    seq = [Decimal("0.49"), Decimal("0.50"), Decimal("0.51")]
    ts = T - 590
    for k, yp in enumerate(seq):
        st, acts = _feed(p, st, Trade(B_SD, yp, "yes", Decimal(50), ts + k * 0.02))
        if [a for a in acts if a.kind == ActionKind.TAKE_WINGS] and early_yes_ask is None:
            early_yes_ask = st.print_through[-1].yes_ask_at_trigger
        for b in list(st.wing_batches):
            st = _fill_wings(p, st, b.index, ts + k * 0.02 + 0.001)
        for o in [x for x in st.ladder if yp + _EPS >= (_ONE - x.price)]:
            st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no",
                                      ts + k * 0.02 + 0.002))
    filled = [t for t in st.print_through if t.resolution == "filled"]
    assert filled, "at least one pre-hedged rung filled behind the sweep"
    assert st.sets_done >= 1 and early_yes_ask == Decimal("0.76")


def test_golden_slow_sweep_stalls_and_unwinds():
    fix = _fixture()
    stall_ms = int(fix["slow_sweep"]["stall_after_s"] * 1000) - 500
    drv, t0 = _dry(_params(print_through_ticks=1, print_through_stall_ms=stall_ms,
                           print_through_min_lock_c=100))
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.49", "count_fp": "20.00"}, t0 + 1)
    assert len(drv.state.print_through) == 1 and drv.state.rungs_filled == 0
    drv.on_clock_tick(t0 + 1 + float(fix["slow_sweep"]["stall_after_s"]))   # trigger_ts + stall_after_s
    trig = drv.state.print_through[0]
    assert trig.resolved and trig.resolution == "unwind"
    assert drv.state.wing_batches == ()


# ===========================================================================
# Dry-mode faithfulness (the driver simulates the trigger)
# ===========================================================================
def test_dry_mode_simulates_trigger_and_attributes():
    drv, t0 = _dry(_params(print_through_ticks=1, print_through_stall_ms=5000))
    # print 0.49 pre-hedges the top rung (offer 0.50) but does NOT cross it (no dry_sim fill yet).
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.49", "count_fp": "1.00"}, t0 + 1)
    assert len(drv.state.print_through) == 1 and drv.state.print_through[0].count == 1
    assert drv.state.rungs_filled == 0 and drv._dry_sim_fills == 0
    # the crossing print at 0.50 fills the pre-hedged rung (attributes) -> the set completes.
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.50", "count_fp": "1.00"}, t0 + 2)
    assert drv._dry_sim_fills >= 1 and drv.state.sets_done >= 1
    filled = [t for t in drv.state.print_through if t.resolution == "filled"]
    assert filled
    kinds = set(drv.journal.kinds())
    assert "would_take_wings" in kinds and "dry_sim_fill" in kinds
    for real in ("take_wings", "place_rest", "print_through_complete", "print_through_unwind"):
        assert real not in kinds, f"dry emitted a REAL {real}"


# ===========================================================================
# Executor mechanics (fake writer): complete book-from-response, unwind reconcile, twin refusal
# ===========================================================================
def _exec(enable=True, *, post_queue=None):
    from service.proxy_writer import WriteResponse

    from service.v33.executor import V33LiveExecutor

    class FakeJournal:
        def __init__(self):
            self.records = []

        def append(self, k, o, t):
            self.records.append((k, o))

        def kinds(self):
            return [k for k, _ in self.records]

    class FakeWriter:
        def __init__(self):
            self.posts = []
            self.post_queue = deque(post_queue or [])

        def rest_post(self, path, body):
            self.posts.append((path, body))
            if self.post_queue:
                return self.post_queue.popleft()
            return WriteResponse(200, {"order": {"order_id": "oid-1",
                                                 "client_order_id": body.get("client_order_id"),
                                                 "fill_count": "3.00", "remaining_count": "0.00"}}, True)

        def rest_delete(self, path):
            return WriteResponse(200, {"reduced_by": "1.00"}, True)

        def rest_get(self, path, params=None):
            return {}

    w = FakeWriter()
    j = FakeJournal()
    ex = V33LiveExecutor(w, {B_SD: (79600.0, 79699.99)},
                         {B_SD: 2, STK_SD: 2, STK_SU: 2}, j, T, 300, k_rungs=11,
                         clock=lambda: 0.0, sleep=lambda _s: None, wing_cap=2,
                         enable_print_through=enable)
    return ex, w, j


def test_executor_take_bucket_no_books_from_response():
    from service.proxy_writer import WriteResponse
    # one batch of 2 chunks (wing_cap=2, count 3) returning 2 + 1 fills -> aggregate Fill of 3.
    resp = WriteResponse(200, {"orders": [
        {"order_id": "o1", "client_order_id": "v33-wc-1", "fill_count": "2.00", "yes_price": "35"},
        {"order_id": "o2", "client_order_id": "v33-wc-2", "fill_count": "1.00", "yes_price": "35"},
    ]}, True)
    ex, w, j = _exec(post_queue=[resp])
    a = V33Action(kind=V33ActionKind.TAKE_BUCKET_NO, ticker=B_SD, side="no", action="buy", count=3,
                  price=Decimal("0.65"), client_order_id="v33-cc-1",
                  legs=(LegOrder(B_SD, "no", "buy", 3, Decimal("0.65")),))
    events = ex.on_action(a, None, T - 500)
    assert ex.pt_bucket_no_takes == 1 and ex.pt_bucket_no_fills == 3
    assert "print_through_complete" in j.kinds()
    # book-from-response: one aggregate Fill for the core's complete_coid with the true filled count.
    assert len(events) == 1 and events[0].client_order_id == "v33-cc-1" and int(events[0].count) == 3


def test_executor_take_bucket_no_short_signals_core():
    from service.proxy_writer import WriteResponse
    resp = WriteResponse(200, {"orders": [
        {"order_id": "o1", "client_order_id": "v33-wc-1", "fill_count": "2.00", "yes_price": "35"},
        {"order_id": "o2", "client_order_id": "v33-wc-2", "fill_count": "0.00", "yes_price": "35"},
    ]}, True)
    ex, w, j = _exec(post_queue=[resp])
    a = V33Action(kind=V33ActionKind.TAKE_BUCKET_NO, ticker=B_SD, side="no", action="buy", count=3,
                  price=Decimal("0.65"), client_order_id="v33-cc-1",
                  legs=(LegOrder(B_SD, "no", "buy", 3, Decimal("0.65")),))
    events = ex.on_action(a, None, T - 500)
    assert ex.pt_bucket_no_fills == 2 and "print_through_complete_short" in j.kinds()
    assert int(events[0].count) == 2   # the core books 2 and unwinds the 1 un-hedged wing


def test_executor_unwind_short_stands_down():
    from service.proxy_writer import WriteResponse
    # sell 2 requested (one leg), venue only bought back 1 -> shortfall -> stand down.
    resp = WriteResponse(200, {"order": {"order_id": "o1", "client_order_id": "v33-wc-1",
                                         "fill_count": "1.00", "yes_price": "40"}}, True)
    ex, w, j = _exec(post_queue=[resp])
    a = V33Action(kind=V33ActionKind.UNWIND_WINGS, count=2,
                  legs=(LegOrder(STK_SD, "yes", "sell", 2, Decimal("0.40")),))
    ex.on_action(a, None, T - 500)
    assert ex.pt_unwinds == 1 and ex.pt_unwind_shortfalls == 1
    assert "print_through_unwind_short" in j.kinds()
    assert ex.stand_down_reason == "print_through_unwind_short"


def test_executor_refuses_would_twin():
    ex, _, _ = _exec()
    with pytest.raises(AssertionError):
        ex.on_action(V33Action(kind=V33ActionKind.WOULD_TAKE_BUCKET_NO, ticker=B_SD, count=1),
                     None, T - 500)


# ===========================================================================
# Report section
# ===========================================================================
def test_report_print_through_section():
    from service.v33.report import _render_print_through, build_print_through

    def _win(pt):
        return {"roster": "DegeneracyV3_3", "close_time": "2026-09-26T22:00:00Z", "mode": "armed",
                "effective_mode": "armed", "stand_down": False, "print_through": pt}

    rows = [
        _win([{"count": 3, "resolved": True, "resolution": "filled", "lock_at_trigger": "0.18"},
              {"count": 2, "resolved": True, "resolution": "unwind", "roundtrip_cost": "0.14"}]),
        _win([{"count": 4, "resolved": True, "resolution": "complete", "lock_at_completion": "-0.05"},
              {"count": 1, "resolved": True, "resolution": "partial"}]),
        _win([{"count": 2, "resolved": False, "resolution": None}]),
    ]
    pt = build_print_through(rows)
    assert pt["windows_with_triggers"] == 3 and pt["triggers"] == 5
    assert pt["contracts_prehedged"] == 12
    assert pt["resolutions"] == {"filled": 1, "complete": 1, "unwind": 1, "partial": 1, "open": 1}
    assert pt["mean_trigger_lock_c"] == Decimal("6")
    assert pt["mean_completion_lock_c"] == Decimal("-5")
    assert pt["unwind_roundtrip_cost"] == Decimal("0.14")
    out = "\n".join(_render_print_through(pt))
    assert "PRINT-THROUGH WINGS" in out and "filled=1" in out and "partial(fail-closed)=1" in out


def test_report_print_through_empty_when_absent():
    from service.v33.report import build_print_through
    rows = [{"roster": "DegeneracyV3_3", "close_time": "2026-09-26T22:00:00Z", "mode": "dry",
             "effective_mode": "dry", "stand_down": False}]
    pt = build_print_through(rows)
    assert pt["triggers"] == 0 and pt["windows_with_triggers"] == 0
