"""PRINT-THROUGH WINGS (Brad, 2026-09-26): the early-hedge trigger for the V3.3 ladder.

The feature ships OFF (``print_through`` false) -> the ladder is byte-identical (proved by the whole
existing suite passing unchanged). These tests ENABLE it via ``replace(params, print_through=True, ...)``
and prove:

  * the trigger fires only on a qualifying bucket YES print toward a resting rung's offer (within
    ``print_through_ticks``), not on a print away from it, and not when no rest is live;
  * the fill-after-trigger path attributes the pre-emptive wing batch ONCE (no coalesce, no second take);
  * a completed print-through set counts and the ledger attributes it;
  * STALL -> complete when the taker lock clears the floor; STALL -> unwind otherwise;
  * a partial wing fill fails closed (cancel the rests, unwind, stand down);
  * dry-mode simulates the trigger faithfully (the driver's dry_sim books the crossing rung after the
    pre-emptive take).

No network, no disk beyond the shipped policy. Holdout / seal are never touched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import fee
from service.book import TopOfBook
from service.v33 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    Trade,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import V33ActionKind
from service.v33.core import lock_value, print_through_summary

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
    p = load_v33_params()
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
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert places
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    assert all(o.live for o in st.ladder) and st.n_top == Decimal("0.50")
    return st


def _state(p):
    return V33State.new(CLOSE, T, BK, p)


def _wing_leg_coids(st, batch_index):
    return [lg.client_order_id for lg in st.wing_legs if lg.batch == batch_index]


def _fill_wings(p, st, batch_index, now):
    """Fill both pre-taken wing legs of a batch at their limits (simulate the IOC completing)."""
    for lg in [l for l in st.wing_legs if l.batch == batch_index and l.status == "pending"]:
        st, _ = _feed(p, st, Fill(None, lg.client_order_id, Decimal(lg.count), lg.limit, lg.side, now))
    return st


# ===========================================================================
# Trigger firing
# ===========================================================================
def test_trigger_fires_on_qualifying_print_toward_offer():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    # a print at 0.51 is within 1 tick of the top rung's offer (0.50); it reaches offers <= 0.52 ->
    # rungs at n 0.50/0.49/0.48 (offers 0.50/0.51/0.52).
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1
    assert len(st.print_through) == 1
    trig = st.print_through[0]
    assert trig.count == 3 and set(trig.rung_prices) == {Decimal("0.50"), Decimal("0.49"), Decimal("0.48")}
    assert takes[0].count == 3
    # the pre-emptive wing batch exists, taken, sized to 3, no rung fill yet (rungs still resting).
    b = st.wing_batches[0]
    assert b.print_through and b.taken and b.taken_count == 3 and b.total_count == 0
    assert st.rungs_filled == 0 and len(st.ladder) == 11   # nothing filled/removed on the print itself


def test_no_trigger_on_print_away_from_offer():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    # a print at 0.40 is >1 tick below every offer (min offer 0.50) -> no trigger.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.40"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert st.print_through == ()


def test_no_trigger_when_no_rest_live():
    p = _params()
    st = _state(p)  # no ladder placed
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.99"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert st.print_through == ()


def test_no_trigger_when_feature_off():
    p = replace(load_v33_params(), tol=Decimal("0.01"), deb_ms=0,
                freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)  # print_through False
    st = _bring_up(p, _state(p), T - 600)
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.99"), "yes", Decimal(5), T - 599))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert st.print_through == ()


def test_no_side_taker_does_not_trigger():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.99"), "no", Decimal(5), T - 599))
    assert st.print_through == ()


# ===========================================================================
# Attribution (no double take)
# ===========================================================================
def test_fill_after_trigger_attributes_batch_once():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    trig = st.print_through[0]
    covered = trig.rung_coids
    # fill one pre-hedged rung -> it attaches to the SAME batch; no new coalesce group, no second take.
    o = next(x for x in st.ladder if x.client_order_id == covered[0])
    st, acts = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", T - 598))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]   # NO second take
    assert st.coalesce_open is None                                    # not coalesced separately
    assert len(st.wing_batches) == 1
    assert st.wing_batches[0].total_count == 1                         # the fill attached to the batch
    assert st.rungs_filled == 1 and len(st.ladder) == 10


def test_completed_print_through_set_counts_and_books():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    # fill the wings (IOC completing at our early limits), then all 3 pre-hedged rungs.
    st = _fill_wings(p, st, b_idx, T - 598)
    for coid in st.print_through[0].rung_coids:
        o = next(x for x in st.ladder if x.client_order_id == coid)
        st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", T - 597))
    b = st.wing_batches[0]
    assert b.completed and st.sets_done == 3 and st.print_through[0].resolution == "filled"


# ===========================================================================
# Stall policy
# ===========================================================================
def test_stall_completes_when_lock_clears_floor():
    # completing via a bucket-NO TAKER at the current ask is costlier than the maker rung would have been,
    # so its lock is typically negative; a floor of -$0.50 admits it (the branch-selection is the point).
    p = _params(print_through_stall_ms=1500, print_through_min_lock_c=-50)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    st = _fill_wings(p, st, b_idx, T - 598.9)   # wings in hand, rungs NOT filled
    # advance past the stall window without any rung fill -> complete (bucket-NO taker at no_ask 0.65).
    st, acts = _feed(p, st, ClockTick(T - 597))   # ~1.9 s later > 1.5 s stall
    completes = [a for a in acts if a.kind == V33ActionKind.TAKE_BUCKET_NO]
    assert len(completes) == 1 and completes[0].count == 3
    trig = st.print_through[0]
    assert trig.resolved and trig.resolution == "complete" and trig.complete_price == Decimal("0.65")
    assert st.wing_batches[0].completed and st.sets_done == 3
    # the un-filled rests were cancelled first.
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]


def test_stall_unwinds_when_lock_below_floor():
    # an impossibly high min-lock floor forces the unwind branch on a stall with nothing filled.
    p = _params(print_through_stall_ms=1500, print_through_min_lock_c=100)
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    st = _fill_wings(p, st, b_idx, T - 598.9)
    st, acts = _feed(p, st, ClockTick(T - 597))
    assert [a for a in acts if a.kind == V33ActionKind.UNWIND_WINGS]
    assert not [a for a in acts if a.kind == V33ActionKind.TAKE_BUCKET_NO]
    trig = st.print_through[0]
    assert trig.resolved and trig.resolution == "unwind" and trig.roundtrip_cost is not None
    # the unwound batch + its legs are dropped (flat) so the ledger never counts them as held.
    assert st.wing_batches == () and st.wing_legs == ()
    assert st.sets_done == 0
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]


def test_policy_unwind_forces_unwind_even_when_lock_ok():
    p = _params(print_through_stall_ms=1500, print_through_policy="unwind")
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    st = _fill_wings(p, st, st.wing_batches[0].index, T - 598.9)
    st, acts = _feed(p, st, ClockTick(T - 597))
    assert [a for a in acts if a.kind == V33ActionKind.UNWIND_WINGS]
    assert st.print_through[0].resolution == "unwind"


# ===========================================================================
# Partial wing fill -> fail closed
# ===========================================================================
def test_partial_wing_fill_fails_closed():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    b_idx = st.wing_batches[0].index
    legs = [l for l in st.wing_legs if l.batch == b_idx]
    yes_leg = next(l for l in legs if l.side == "yes")
    no_leg = next(l for l in legs if l.side == "no")
    # yes wing fills; no wing comes back UNFILLED (count 0 -> ask moved past our tight limit).
    st, _ = _feed(p, st, Fill(None, yes_leg.client_order_id, Decimal(yes_leg.count),
                              yes_leg.limit, "yes", T - 598))
    st, acts = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", T - 598))
    # fail closed: cancel the pre-hedged rests, unwind the filled wing, stand down.
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert [a for a in acts if a.kind == V33ActionKind.UNWIND_WINGS]
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert st.stood_down and st.pt_stood_down
    assert st.print_through[0].resolution == "partial"


def test_stand_down_blocks_further_triggers():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    no_leg = next(l for l in st.wing_legs if l.side == "no")
    st, _ = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", T - 598))
    assert st.pt_stood_down
    # a second qualifying print does not fire a new trigger while stood down.
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.55"), "yes", Decimal(5), T - 597))
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]


def test_print_through_summary_shape():
    p = _params()
    st = _bring_up(p, _state(p), T - 600)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.51"), "yes", Decimal(5), T - 599))
    s = print_through_summary(st)
    assert len(s) == 1 and s[0]["count"] == 3 and s[0]["resolved"] is False
    assert s[0]["yes_print"] == "0.51" and s[0]["lock_at_trigger"] is not None


# ===========================================================================
# Golden fixture replays: a 10:00Z-style FAST sweep, and a SLOW-sweep STALL
# ===========================================================================
import json  # noqa: E402
import os  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v33",
                    "print_through_sweeps.json")


def _fixture():
    if not os.path.exists(_FIX):
        pytest.skip("print-through fixture absent")
    with open(_FIX, encoding="utf-8") as f:
        return json.load(f)


def test_fixture_not_holdout_or_seal():
    assert _fixture()["close_time"] == "2026-09-26T22:00:00Z"   # not 2026-08-* holdout/seal


def test_golden_fast_sweep_prehedges_and_completes_at_pre_jump_ask():
    """The 10:00Z-style FAST sweep: the FIRST print of the burst fires the wing take at the ask the sweep
    STARTED from (before the jump); the rung fills land right behind it and attach to the SAME batch."""
    fix = _fixture()
    p = _params(print_through_stall_ms=1500)
    st = _bring_up(p, _state(p), T - 600)
    t0 = T - 590
    prints = fix["fast_sweep"]["prints"]
    # the first print fires print-through; capture the wing limits it locked (the PRE-jump ask).
    dt, yp, _cnt = prints[0]
    st, acts = _feed(p, st, Trade(B_SD, Decimal(yp), "yes", Decimal(int(float(_cnt))), t0 + dt))
    assert len(st.print_through) == 1
    trig = st.print_through[0]
    early_yes_ask = trig.yes_ask_at_trigger
    b_idx = st.wing_batches[0].index
    early_limits = {lg.side: lg.limit for lg in st.wing_legs if lg.batch == b_idx}
    # the wings fill at the early ask (IOC completing), then the rungs the burst crosses fill right behind.
    st = _fill_wings(p, st, b_idx, t0 + prints[0][0] + 0.001)
    for dt, yp, cnt in prints:
        ypd = Decimal(yp)
        for o in [x for x in st.ladder if x.client_order_id in trig.rung_coids
                  and x.client_order_id not in st.print_through[0].filled_coids
                  and ypd + _EPS >= (_ONE - x.price)]:
            st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no",
                                      t0 + dt))
    # every pre-hedged rung the burst crossed filled and attached to the pre-emptive batch (ONE batch).
    assert len(st.wing_batches) == 1
    b = st.wing_batches[0]
    assert b.print_through and b.completed and b.total_count == trig.count
    assert st.sets_done == trig.count
    # the lock was struck at the early (pre-jump) ask, not a jumped one.
    assert early_limits["yes"] == early_yes_ask
    assert st.print_through[0].resolution == "filled"


def test_golden_slow_sweep_stalls_and_unwinds():
    """The SLOW sweep: the print climbs to within a tick of the top offer (fires print-through) then
    STALLS -- no crossing print -> the pre-emptive hedge unwinds (lock below the high floor)."""
    fix = _fixture()
    p = _params(print_through_stall_ms=int(fix["slow_sweep"]["stall_after_s"] * 1000) - 500,
                print_through_min_lock_c=100)   # high floor -> unwind on stall
    st = _bring_up(p, _state(p), T - 600)
    t0 = T - 590
    for dt, yp, cnt in fix["slow_sweep"]["prints"]:
        st, acts = _feed(p, st, Trade(B_SD, Decimal(yp), "yes", Decimal(int(float(cnt))), t0 + dt))
    # a trigger fired on the within-a-tick print (0.49 -> top offer 0.50) but no rung crossed.
    assert len(st.print_through) == 1 and st.rungs_filled == 0
    b_idx = st.wing_batches[0].index
    st = _fill_wings(p, st, b_idx, t0 + 0.31)     # wings filled, rungs still resting
    # nothing crosses; advance past the stall window.
    st, acts = _feed(p, st, ClockTick(t0 + float(fix["slow_sweep"]["stall_after_s"])))
    assert [a for a in acts if a.kind == V33ActionKind.UNWIND_WINGS]
    assert st.print_through[0].resolution == "unwind"
    assert st.wing_batches == ()   # unwound -> flat, nothing left as held


# ===========================================================================
# Dry-mode faithfulness (the driver simulates the trigger)
# ===========================================================================
def test_dry_mode_simulates_trigger_and_attributes():
    import service.run_v33 as RUN
    from service.run_v32 import FrozenExecutor

    class J:
        def __init__(self):
            self.recs = []

        def append(self, k, o, t):
            self.recs.append((k, o))

        def kinds(self):
            return [k for k, _ in self.recs]

    p = _params(print_through_stall_ms=1500)
    cts = T
    st = V33State.new(CLOSE, cts, BK, p, shakedown=True)   # DRY -> WOULD_* twins
    drv = RUN.V33Driver(p, st, J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    t0 = cts - 600
    for m, tb in ((B_SD, ("0.35", "0.36")), (STK_SU, ("0.36", "0.37")), (STK_SD, ("0.75", "0.76"))):
        drv.on_book_update(m, _top(*tb), t0)
    assert len(drv.state.ladder) == 11 and drv.state.n_top == Decimal("0.50")
    # a bucket YES print that crosses the WHOLE ladder (offers 0.50..0.60): print-through pre-hedges all 11,
    # then dry_sim fills them; the set completes on the (synthetic) wing fills. Sends nothing real.
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.65", "count_fp": "1.00"}, t0 + 1)
    assert len(drv.state.print_through) == 1 and drv.state.print_through[0].count == 11
    assert drv.state.rungs_filled == 11 and drv._dry_sim_fills == 11
    b = drv.state.wing_batches[0]
    assert b.print_through and b.completed and drv.state.sets_done == 11
    assert len(drv.state.wing_batches) == 1     # one pre-emptive batch, not coalesced batches
    kinds = set(drv.journal.kinds())
    assert "would_take_wings" in kinds and "dry_sim_fill" in kinds
    for real in ("take_wings", "place_rest", "print_through_complete", "print_through_unwind"):
        assert real not in kinds, f"dry emitted a REAL {real}"


# ===========================================================================
# Executor mechanics (fake writer): complete (bucket-NO taker) + unwind (sell) + twin refusal
# ===========================================================================
def _exec(enable=True):
    from collections import deque
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
            self.post_queue = deque()

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


def test_executor_take_bucket_no_sends_ioc_buy():
    from service.v33.actions import LegOrder, V33Action
    ex, w, j = _exec()
    a = V33Action(kind=V33ActionKind.TAKE_BUCKET_NO, ticker=B_SD, side="no", action="buy", count=3,
                  price=Decimal("0.65"), legs=(LegOrder(B_SD, "no", "buy", 3, Decimal("0.65")),))
    ex.on_action(a, None, T - 500)
    assert ex.pt_bucket_no_takes == 1
    assert "print_through_complete" in j.kinds()
    assert w.posts, "a bucket-NO IOC was posted"
    # chunked at wing_cap=2 -> two chunks (2 + 1) for count 3 -> a batch post.
    _, body = w.posts[0]
    assert "orders" in body or body.get("time_in_force") == "immediate_or_cancel"


def test_executor_unwind_sends_sells():
    from service.v33.actions import LegOrder, V33Action
    ex, w, j = _exec()
    legs = (LegOrder(STK_SD, "yes", "sell", 2, Decimal("0.40")),
            LegOrder(STK_SU, "no", "sell", 2, Decimal("0.30")))
    a = V33Action(kind=V33ActionKind.UNWIND_WINGS, legs=legs, count=2)
    ex.on_action(a, None, T - 500)
    assert ex.pt_unwinds == 1 and "print_through_unwind" in j.kinds()
    assert w.posts


def test_executor_refuses_would_twin():
    from service.v33.actions import V33Action
    ex, _, _ = _exec()
    with pytest.raises(AssertionError):
        ex.on_action(V33Action(kind=V33ActionKind.WOULD_TAKE_BUCKET_NO, ticker=B_SD, count=1),
                     None, T - 500)


# ===========================================================================
# Report section
# ===========================================================================
def test_report_print_through_section():
    from service.v33.report import build_print_through, _render_print_through

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
    # mean lock at trigger for the filled one: 0.18 / 3 contracts * 100 = 6.0c/contract.
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
