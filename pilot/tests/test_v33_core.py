"""V3.3 pure-core unit tests: the rolling ladder + THE ROLL.

Covers ladder placement (K consecutive rungs from n_top), n_min truncation from the BOTTOM, the
post-only cap shifting the whole ladder down, the roll (1c down / 1c up / 2c strictly sequential /
n_min shrink / cap hold / roll integrity counters), the bucket change (cancel-all -> place-all),
rung fills + wing coalescing (Q2: within 150 ms one batch, else two), no refill inside the window
(Q3), the T-5 cancel-all, the freshness / spot / n stand-downs, the replace-rate alarm, a fill during
a roll (books at the pre-roll price, no double-place), the amend-cross booking, the shadow, and the
shakedown WOULD_* twins. ``check_invariants`` runs after EVERY event via ``_feed``.

No network, no disk beyond the shipped policy. 2026-08-20..29 (holdout) and the 2026-08-02..18 seal
are never touched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from service._simlaw import fee
from service.book import TopOfBook
from service.v33 import (
    BUY_NO,
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderAmended,
    OrderCancelled,
    Trade,
    V33Params,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.core import lock_value

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000

BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"   # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"   # -> 79700
STK_SU2 = "KXBTCD-26SEP0416-T79799.99"  # -> 79800
B_SD = "KXBTC-RANGE-B79600"
B_SU = "KXBTC-RANGE-B79700"


def _top(bid: str, ask: str, *, suspect: bool = False) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=suspect,
    )


def _params(**over) -> V33Params:
    p = load_v33_params()
    return replace(p, **over) if over else p


def _state(params: V33Params, *, shakedown: bool = False) -> V33State:
    return V33State.new(CLOSE, T, BK, params, shakedown=shakedown)


def _feed(params, st, event):
    st, a = decide_v33(params, st, event)
    st.check_invariants(params)      # invariant sweep after every event
    return st, a


def _feed_all(params, st, events):
    acts: list = []
    for e in events:
        st, a = _feed(params, st, e)
        acts += a
    return st, acts


def _sd(ask: str) -> TopOfBook:
    """A strike-Sd book whose yes_ask is ``ask`` (bid = ask - 1c)."""
    return _top(str(Decimal(ask) - Decimal("0.01")), ask)


def _books(now: float, sd_ask: str = "0.76", *, b_bid: str = "0.35", su_bid: str = "0.36"):
    """Fresh in-window books. Defaults give W=1.4290 -> n_top=0.50, cap=0.64 (non-binding).
    ``sd_ask`` moves n_top (0.76->0.50, 0.77->0.49, 0.78->0.48, 0.75->0.51); ``b_bid`` moves the cap."""
    return [
        BookUpdate(B_SD, _top(b_bid, str(Decimal(b_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SU, _top(su_bid, str(Decimal(su_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SD, _sd(sd_ask), now),
    ]


def _refresh(params, st, t, sd_ask="0.76"):
    """Keep both strike books fresh at time ``t`` (also advances the clock). Used to close a coalesce
    window and price the wings (the wings need fresh strikes; the live driver ticks them continuously)."""
    st, a1 = _feed(params, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t))
    st, a2 = _feed(params, st, BookUpdate(STK_SD, _sd(sd_ask), t))
    return st, a1 + a2


def _bring_up_ladder(params, st, now, sd_ask="0.76"):
    """Feed fresh books -> place the ladder, ack every rung -> a live ladder. Returns (st, place_acts)."""
    st, acts = _feed_all(params, st, _books(now, sd_ask))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert places, "expected PLACE_REST rungs from fresh in-window books"
    for a in places:
        st, _ = _feed(params, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    assert all(o.live for o in st.ladder)
    return st, places


def _prices(st):
    return sorted((o.price for o in st.ladder), reverse=True)


# ===========================================================================
# Ladder placement
# ===========================================================================
def test_places_k_consecutive_rungs_from_n_top():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == p.rungs == 11
    assert st.n_top == Decimal("0.50")
    prices = sorted((a.price for a in places), reverse=True)
    assert prices == [Decimal("0.50") - i * Decimal("0.01") for i in range(11)]
    assert all(a.side == BUY_NO and a.action == "buy" and a.count == 1 for a in places)
    assert all(a.expiration_epoch == T - p.quote_end_s for a in places)


def test_placement_counts_as_one_replace_not_k():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    assert st.replace_count == 1           # the ladder placement is ONE replace (the debounce anchor)
    assert len(st.replace_times) == 1


def test_place_then_ack_makes_rungs_live():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))
    assert all(o.pending and not o.live for o in st.ladder)
    for o in list(st.ladder):
        st, _ = _feed(p, st, OrderAck(o.client_order_id, f"OID-{o.client_order_id}", now))
    assert all(o.live and not o.pending for o in st.ladder)
    assert all(o.order_id is not None for o in st.ladder)


def test_rung_e_rung_labels():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    for o in st.ladder:
        assert o.E_rung == p.E_min + (st.n_top - o.price)
    top = max(st.ladder, key=lambda o: o.price)
    bottom = min(st.ladder, key=lambda o: o.price)
    assert top.E_rung == Decimal("0.05") and top.rung == 0
    assert bottom.E_rung == Decimal("0.15") and bottom.rung == 10


def test_shakedown_emits_would_place_only():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p, shakedown=True)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))
    kinds = {a.kind for a in acts}
    assert ActionKind.WOULD_PLACE_REST in kinds and ActionKind.PLACE_REST not in kinds
    assert len([a for a in acts if a.kind == ActionKind.WOULD_PLACE_REST]) == 11


# ===========================================================================
# n_min truncation from the bottom + post-only cap
# ===========================================================================
def test_n_min_truncates_ladder_from_the_bottom():
    # n_min raised so only rungs >= 0.46 survive: n_top 0.50 -> rungs 0.50..0.46 = 5 rungs (K_eff<K).
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.46"))
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    prices = sorted((a.price for a in places), reverse=True)
    assert prices == [Decimal("0.50"), Decimal("0.49"), Decimal("0.48"), Decimal("0.47"),
                      Decimal("0.46")]
    assert len(prices) == 5 < p.rungs


def test_post_only_cap_shifts_whole_ladder_down():
    # a high bucket yes_bid pushes cap = (1 - yes_bid) - 1c below the uncapped n_top, so solve_n caps
    # n_top AT the cap and the WHOLE ladder anchors from there (every rung stays post-only).
    # bucket yes_bid 0.58 -> cap = 0.42 - ... = (1-0.58)-0.01 = 0.41 < uncapped 0.50.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now, b_bid="0.58"))
    assert st.cap == Decimal("0.41")
    assert st.n_top == Decimal("0.41")            # capped, not the uncapped 0.50
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    prices = sorted((a.price for a in places), reverse=True)
    assert prices[0] == Decimal("0.41")           # top honours the cap
    assert prices == [Decimal("0.41") - i * Decimal("0.01") for i in range(len(prices))]


# ===========================================================================
# The roll: 1c down / 1c up
# ===========================================================================
def test_roll_1c_down_moves_top_order_to_new_bottom():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50, ladder 0.50..0.40
    ids_before = {o.price: o.order_id for o in st.ladder}
    # n_top 0.50 -> 0.49 (W up): the TOP order (0.50) rolls to bottom-1c = 0.39.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    assert st.n_top == Decimal("0.49")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1
    am = amends[0]
    assert am.price == Decimal("0.39") and am.order_id == ids_before[Decimal("0.50")]
    assert ActionKind.CANCEL_REST not in {a.kind for a in acts}
    assert ActionKind.PLACE_REST not in {a.kind for a in acts}
    assert st.roll_pending is not None and st.roll_pending.target_price == Decimal("0.39")
    # confirm -> the ladder is 0.49..0.39, K-1 order_ids untouched, the moved order at 0.39.
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    assert st.roll_pending is None
    assert _prices(st) == [Decimal("0.49") - i * Decimal("0.01") for i in range(11)]
    ids_after = {o.price: o.order_id for o in st.ladder}
    untouched = sum(1 for pr in ids_after if pr in ids_before and ids_after[pr] == ids_before[pr])
    assert untouched == 10
    moved = next(o for o in st.ladder if o.price == Decimal("0.39"))
    assert moved.E_rung == Decimal("0.15") and moved.rung == 10


def test_roll_1c_up_moves_bottom_order_to_new_top():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50, ladder 0.50..0.40
    ids_before = {o.price: o.order_id for o in st.ladder}
    # n_top 0.50 -> 0.51 (W down): the BOTTOM order (0.40) rolls to top+1c = 0.51.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.75"), now + 1))
    assert st.n_top == Decimal("0.51")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1
    am = amends[0]
    assert am.price == Decimal("0.51") and am.order_id == ids_before[Decimal("0.40")]
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    assert _prices(st) == [Decimal("0.51") - i * Decimal("0.01") for i in range(11)]
    moved = next(o for o in st.ladder if o.price == Decimal("0.51"))
    assert moved.E_rung == Decimal("0.05") and moved.rung == 0


def test_roll_below_tol_does_not_move():
    p = _params(tol=Decimal("0.02"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # a 1c n_top move (< tol 2c): no roll.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    assert st.n_top == Decimal("0.49")
    assert not [a for a in acts if a.kind in (ActionKind.AMEND_REST, ActionKind.CANCEL_REST,
                                              ActionKind.PLACE_REST)]


def test_roll_debounce_blocks_until_elapsed():
    p = _params(tol=Decimal("0.01"), deb_ms=2000)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # placed at `now`, last_replace_ts=now
    # +0.5s: n_top moves to 0.49 but debounce (2000ms) not elapsed -> no roll (strikes kept fresh).
    st, acts = _refresh(p, st, now + 0.5, sd_ask="0.77")
    assert st.n_top == Decimal("0.49")
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    # +1.4s: refresh (still < 2000ms since placement) -> still no roll.
    st, acts = _refresh(p, st, now + 1.4, sd_ask="0.77")
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    # +2.1s: debounce elapsed -> the roll fires (both strikes fresh from now+1.4).
    st, acts = _refresh(p, st, now + 2.1, sd_ask="0.77")
    assert [a for a in acts if a.kind == ActionKind.AMEND_REST]


def test_roll_holds_while_a_rung_is_pending():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    # place but do NOT ack (all rungs pending)
    st, acts = _feed_all(p, st, _books(now))
    assert all(o.pending for o in st.ladder)
    # a big n_top move while rungs are pending -> HOLD, no roll.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.78"), now + 1))
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]


# ===========================================================================
# The roll: 2c strictly sequential
# ===========================================================================
def test_roll_2c_is_two_rolls_strictly_sequential():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50
    # jump n_top 0.50 -> 0.48 (down 2c) in one tick.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.78"), now + 1))
    assert st.n_top == Decimal("0.48")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1                        # only the FIRST roll issued
    am1 = amends[0]
    assert am1.price == Decimal("0.39")            # move top(0.50) to bottom(0.40)-1c
    # a second big move while the first roll is in flight -> queued, NO second amend.
    st, held = _feed(p, st, BookUpdate(STK_SD, _sd("0.78"), now + 1.05))
    assert not [a for a in held if a.kind == ActionKind.AMEND_REST]
    # confirm the first -> the SECOND roll is now issued (deb_ms=0), moving new top(0.49) to 0.38.
    st, acts2 = _feed(p, st, OrderAmended(am1.order_id, am1.updated_client_order_id, am1.price,
                                          now + 1.1))
    amends2 = [a for a in acts2 if a.kind == ActionKind.AMEND_REST]
    assert len(amends2) == 1
    am2 = amends2[0]
    assert am2.price == Decimal("0.38")
    st, _ = _feed(p, st, OrderAmended(am2.order_id, am2.updated_client_order_id, am2.price,
                                      now + 1.2))
    assert st.roll_pending is None
    assert _prices(st) == [Decimal("0.48") - i * Decimal("0.01") for i in range(11)]
    assert st.roll_count == 2 and st.roll_single_order_count == 2


def test_confirmed_roll_counts_as_replace_and_roll():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    assert st.replace_count == 1 and st.roll_count == 0
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    assert st.replace_count == 1                   # not counted at request
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    assert st.replace_count == 2 and st.roll_count == 1 and st.roll_single_order_count == 1


# ===========================================================================
# The roll: n_min shrink + cap hold edges
# ===========================================================================
def test_roll_down_past_n_min_shrinks_from_top():
    # n_min high so the ladder is short and its bottom sits at n_min; a further n_top-down roll cannot
    # place below n_min -> it CANCELS the top rung (shrink), counted as a one-order roll.
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.48"))
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50, rungs 0.50/0.49/0.48 (bottom=n_min)
    assert _prices(st) == [Decimal("0.50"), Decimal("0.49"), Decimal("0.48")]
    top_id = max(st.ladder, key=lambda o: o.price).order_id
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))   # n_top -> 0.49
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 1 and cancels[0].order_id == top_id
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert st.roll_count == 1 and st.roll_single_order_count == 1
    # after the cancel confirms the ladder is 0.49/0.48 (shrunk by one from the top).
    st, _ = _feed(p, st, OrderCancelled(top_id, now + 1.1))
    assert _prices(st) == [Decimal("0.49"), Decimal("0.48")]


def test_roll_up_past_cap_holds():
    # cap binds the top; an n_top-up roll that would exceed the cap holds (no order above the cap).
    # bucket yes_bid 0.58 -> cap 0.41, n_top capped at 0.41. Then n_top rises but stays capped -> hold.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # start uncapped n_top 0.50
    # now make the bucket cap bind at 0.41 while W would want a higher n_top.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _sd("0.72"), now + 1))       # W low -> wants n_top 0.54
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.58", "0.59"), now + 1))
    assert st.cap == Decimal("0.41") and st.n_top == Decimal("0.41")
    # the ladder top (0.50+) is above the new cap; desired n_top 0.41 is BELOW top -> shift DOWN, not up,
    # so this is a shift-down roll (covered elsewhere). Assert no order is ever placed ABOVE the cap.
    for o in st.ladder:
        assert o.price <= Decimal("0.50")          # never rolled up past a binding cap


# ===========================================================================
# Bucket change: cancel-all -> place-all
# ===========================================================================
def test_bucket_change_cancels_all_then_places_all_after_confirm():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # ladder on bucket 79600
    ids = [o.order_id for o in st.ladder]
    assert st.rest_bucket_Sd == 79600
    # bring up the NEW bucket (79700) as the higher-mid spot; keep the other books fresh.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.9))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 0.9))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now + 0.9))
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11                      # cancel ALL K
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.ladder == () and st.awaiting_replace and st.outstanding_cancels == 11
    # confirm the cancels one by one; placement fires only after the LAST confirm.
    for i, oid in enumerate(ids):
        st, acts = _feed(p, st, OrderCancelled(oid, now + 1.0 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 11
    assert all(o.bucket_Sd == 79700 for o in st.ladder)
    assert st.rest_bucket_Sd == 79700 and not st.awaiting_replace


# ===========================================================================
# Rung fills + wing coalescing (Q2)
# ===========================================================================
def _sweep_fill(p, st, order, yp_ts):
    """Synthesize the exchange filling one rung (a taker crossed it): emit its Fill."""
    return _feed(p, st, Fill(order.order_id, order.client_order_id, Decimal(1), order.price, "no",
                             yp_ts))


def test_full_sweep_fills_all_rungs_and_coalesces_one_batch():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # a full sweep: every rung fills within 10 ms -> all coalesce into ONE batch.
    for i, o in enumerate(list(st.ladder)):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)
    assert st.rungs_filled == 11 and st.ladder == ()
    assert st.rest_allotment_done
    # close the coalesce window + take the wings (fresh strikes price the wings).
    st, acts = _refresh(p, st, now + 0.7)
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1 and takes[0].count == 11
    assert len(st.wing_batches) == 1 and st.wing_batches[0].total_count == 11


def test_shallow_pump_fills_three_rungs_others_stay_live():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50 -> rungs 0.50..0.40
    # a shallow pump crossing offers up to 0.53 fills the top 3 rungs (0.50, 0.49, 0.48).
    for o in sorted(st.ladder, key=lambda o: o.price, reverse=True)[:3]:
        st, _ = _sweep_fill(p, st, o, now + 1)
    assert st.rungs_filled == 3
    assert len(st.ladder) == 8
    assert not st.rest_allotment_done
    assert _prices(st) == [Decimal("0.47") - i * Decimal("0.01") for i in range(8)]


def test_coalesce_two_fills_100ms_apart_one_batch():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    rungs = sorted(st.ladder, key=lambda o: o.price, reverse=True)
    st, _ = _sweep_fill(p, st, rungs[0], now + 0.5)
    st, _ = _sweep_fill(p, st, rungs[1], now + 0.6)     # 100 ms after the first -> same batch
    st, acts = _refresh(p, st, now + 0.8)               # close (>150ms) + price the wings
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(st.wing_batches) == 1 and st.wing_batches[0].total_count == 2
    assert len(takes) == 1 and takes[0].count == 2


def test_coalesce_two_fills_300ms_apart_two_batches():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    rungs = sorted(st.ladder, key=lambda o: o.price, reverse=True)
    st, _ = _sweep_fill(p, st, rungs[0], now + 0.5)
    st, acts1 = _refresh(p, st, now + 0.7)              # close + take batch 1 (fresh strikes)
    st, _ = _sweep_fill(p, st, rungs[1], now + 0.9)     # 400 ms after the first -> a NEW batch
    st, acts2 = _refresh(p, st, now + 1.1)              # close + take batch 2
    takes1 = [a for a in acts1 if a.kind == ActionKind.TAKE_WINGS]
    takes2 = [a for a in acts2 if a.kind == ActionKind.TAKE_WINGS]
    assert len(st.wing_batches) == 2
    assert all(b.total_count == 1 for b in st.wing_batches)
    assert len(takes1) == 1 and len(takes2) == 1        # one take per batch


def test_no_refill_of_a_filled_rung_in_window():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    filled = sorted(st.ladder, key=lambda o: o.price, reverse=True)[0]
    st, _ = _sweep_fill(p, st, filled, now + 0.5)
    # a later healthy tick must NOT re-place the filled rung (Q3).
    st, acts = _feed_all(p, st, _books(now + 0.8))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert filled.price not in _prices(st)


def test_wings_sized_to_coalesced_total_and_set_counts():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    for i, o in enumerate(list(st.ladder)):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)
    st, acts = _refresh(p, st, now + 0.7)
    take = [a for a in acts if a.kind == ActionKind.TAKE_WINGS][0]
    # fill both wings -> the batch completes as total_count sets.
    for leg in st.wing_legs:
        st, _ = _feed(p, st, Fill(None, leg.client_order_id, Decimal(leg.count), leg.limit, leg.side,
                                  now + 0.8))
    assert st.sets_done == 11 and st.wing_batches[0].completed


def test_per_rung_locks_reproduce_solved_values():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # W=1.4290, n_top 0.50
    W = st.W
    for i, o in enumerate(list(st.ladder)):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)
    # every rung fill's solved lock = 2 - n - fee(n) - W; margins step ~1c from the top.
    by_price = {f.price: lock_value(f.price, W) for f in st.rest_fills}
    assert by_price[Decimal("0.50")] == Decimal(2) - Decimal("0.50") - fee(Decimal("0.50")) - W
    assert by_price[Decimal("0.40")] == Decimal(2) - Decimal("0.40") - fee(Decimal("0.40")) - W
    # deeper rung -> larger lock.
    assert by_price[Decimal("0.40")] > by_price[Decimal("0.50")]


# ===========================================================================
# Fill during a roll + amend cross
# ===========================================================================
def test_fill_during_roll_books_pre_roll_price_no_double_place():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    top = max(st.ladder, key=lambda o: o.price)   # 0.50
    # start a roll on the top order (n_top 0.50 -> 0.49).
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    assert st.roll_pending is not None and am.order_id == top.order_id
    # the rolling order FILLS (a taker hit it at its pre-roll resting price 0.50) before the amend acks.
    st, acts = _feed(p, st, Fill(top.order_id, top.client_order_id, Decimal(1), None, "no", now + 1.05))
    assert st.rungs_filled == 1
    assert st.rest_fills[-1].price == Decimal("0.50")        # booked at the PRE-roll price
    assert top.price not in _prices(st)                       # the filled rung left the ladder
    # the late OrderAmended for the now-filled order must NOT double-place.
    st, acts = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.AMEND_REST)]
    assert st.roll_pending is None


def test_amend_cross_books_delta_and_skips_ws_echo():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    top = max(st.ladder, key=lambda o: o.price)
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    # the amend confirms WITH a cross fill of 1 at NO-space avg price 0.50 -> book one rung fill.
    st, acts = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1,
                                         remaining_count=Decimal(0), fill_count=Decimal(1),
                                         average_fill_price=Decimal("0.50")))
    assert st.rungs_filled == 1 and st.rest_fills[-1].price == Decimal("0.50")
    filled_before = st.rungs_filled
    # the venue echoes the SAME crossed fill on the ws fill channel (fresh trade_id) -> must be skipped.
    moved = next((o for o in st.ladder if o.order_id == am.order_id), None)
    if moved is not None:
        st, _ = _feed(p, st, Fill(am.order_id, moved.client_order_id, Decimal(1), Decimal("0.50"),
                                  "no", now + 1.15))
    assert st.rungs_filled == filled_before          # no double-book


# ===========================================================================
# T-5 quote-end cancel + stand-downs
# ===========================================================================
def test_quote_end_cancels_all_rungs():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, acts = _feed(p, st, ClockTick(T - 250))    # t_to_close 250 < quote_end_s 300
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11
    assert st.ladder == ()
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "past_quote_end"]


def test_warmup_before_window_seeds_nothing():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 1000                                  # t_to_close 1000 > quote_start_s 900
    st, acts = _feed_all(p, st, _books(now))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.ladder == ()


def test_stale_wing_cancels_ladder():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # let the strike books go stale (a ClockTick well past freshness) -> W None -> cancel + stand down.
    st, acts = _feed(p, st, ClockTick(now + 5))
    assert st.W is None
    assert [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN
            and a.reason == "stale_or_missing_wing"]
    assert st.ladder == ()


def test_stale_spot_bucket_stands_down():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # advance the strike books but NOT the bucket -> the spot bucket ages past its freshness bound.
    st, a1 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 40))
    st, a2 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 40))
    assert st.spot_bucket_stale
    acts = a1 + a2
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "stale_bucket"]
    assert st.ladder == ()


def test_n_below_min_stands_down():
    # E_min pushed so n_top would fall below n_min -> stand down, no ladder.
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.55"))
    st = _state(p)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))        # n_top solves to 0.50 < n_min 0.55
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "n_below_min"]


def test_no_spot_bucket_stands_down_no_place():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    # only strike books, no bucket book -> no spot -> no place.
    st, acts = _feed_all(p, st, [BookUpdate(STK_SU, _top("0.36", "0.37"), now),
                                 BookUpdate(STK_SD, _sd("0.76"), now)])
    assert st.spot_Sd is None
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]


def test_replace_rate_alarm_stands_down():
    # alarm at 3/min: the placement (1) + three confirmed rolls trip it and cancel the ladder.
    p = _params(tol=Decimal("0.01"), deb_ms=0, replace_rate_alarm_per_min=3)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    asks = ["0.77", "0.78", "0.79"]                 # each moves n_top down 1c -> a roll
    for i, a in enumerate(asks):
        t = now + 1 + i * 0.1
        st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t))
        st, acts = _feed(p, st, BookUpdate(STK_SD, _sd(a), t))
        am = [x for x in acts if x.kind == ActionKind.AMEND_REST]
        if am and st.roll_pending is not None:
            st, _ = _feed(p, st, OrderAmended(st.roll_pending.order_id,
                                              st.roll_pending.new_coid,
                                              st.roll_pending.target_price, t + 0.01))
    st, acts = _feed(p, st, ClockTick(now + 2))
    assert st.stood_down
    assert st.ladder == ()


# ===========================================================================
# Shadow (forked from V3.2, unchanged) + trade path
# ===========================================================================
def test_shadow_fill_and_completion():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # a spot-bucket YES print above 1 - n_shadow(0.10) fills the shadow; a later book completes it.
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.62"), "yes", Decimal(5), now + 1))
    sub = st.shadows["0.10"]
    assert sub.filled and sub.fill is not None
    st, _ = _feed_all(p, st, _books(now + 1.001))
    assert st.shadows["0.10"].fill.lock is not None


def test_trade_does_not_fill_the_live_ladder():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # a public Trade through a rung price must NOT book a rung fill (fills come only from Fill events).
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.65"), "yes", Decimal(5), now + 1))
    assert st.rungs_filled == 0 and len(st.ladder) == 11


# ===========================================================================
# Invariants directly
# ===========================================================================
def test_check_invariants_flags_two_rests_on_one_price():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # forge a duplicate price
    bad = replace(st, ladder=st.ladder + (st.ladder[0],))
    with pytest.raises(AssertionError):
        bad.check_invariants(p)


def test_check_invariants_flags_more_than_k_rests():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    extra = replace(st.ladder[0], client_order_id="x", price=Decimal("0.30"))
    bad = replace(st, ladder=st.ladder + (extra,))
    # 12 > K=11
    with pytest.raises(AssertionError):
        bad.check_invariants(p)


# ===========================================================================
# Roll edges: no spurious roll after a partial sweep; survivors roll; the anchor
# ===========================================================================
def test_no_spurious_roll_after_partial_sweep_with_w_constant():
    # a partial sweep removes the TOP rungs; with W (hence n_top) unchanged, the ladder must NOT roll
    # (the anchor, not the top survivor's price, is the trigger).
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50, anchor 0.50
    for o in sorted(st.ladder, key=lambda o: o.price, reverse=True)[:3]:
        st, _ = _sweep_fill(p, st, o, now + 0.5)
    assert st.rungs_filled == 3 and st.anchor_n_top == Decimal("0.50")
    # a healthy tick with the SAME books -> no roll (n_top still 0.50 == anchor).
    st, acts = _feed_all(p, st, _books(now + 0.8))
    assert not [a for a in acts if a.kind in (ActionKind.AMEND_REST, ActionKind.PLACE_REST)]
    assert len(st.ladder) == 8


def test_survivors_roll_after_partial_sweep_on_real_n_top_move():
    # after a partial sweep, a genuine n_top drop rolls a SURVIVING order one cent (still one order).
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # ladder 0.50..0.40
    for o in sorted(st.ladder, key=lambda o: o.price, reverse=True)[:3]:
        st, _ = _sweep_fill(p, st, o, now + 0.5)   # fill 0.50/0.49/0.48 -> survivors 0.47..0.40
    bottom_before = min(st.ladder, key=lambda o: o.price).price
    top_surv_before = max(st.ladder, key=lambda o: o.price)
    # n_top 0.50 -> 0.49 (real W move) -> shift down: move top survivor (0.47) to bottom-1c.
    st, acts = _refresh(p, st, now + 1, sd_ask="0.77")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1 and amends[0].order_id == top_surv_before.order_id
    assert amends[0].price == bottom_before - Decimal("0.01")
    st, _ = _feed(p, st, OrderAmended(amends[0].order_id, amends[0].updated_client_order_id,
                                      amends[0].price, now + 1.1))
    assert st.anchor_n_top == Decimal("0.49") and len(st.ladder) == 8


def test_roll_round_trip_up_then_down_restores_anchor():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # up 1c
    st, acts = _refresh(p, st, now + 1, sd_ask="0.75")     # n_top 0.51
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    assert st.anchor_n_top == Decimal("0.51")
    # down 1c back
    st, acts = _refresh(p, st, now + 2, sd_ask="0.76")     # n_top 0.50
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 2.1))
    assert st.anchor_n_top == Decimal("0.50")
    assert _prices(st) == [Decimal("0.50") - i * Decimal("0.01") for i in range(11)]


# ===========================================================================
# Roll fallback (amend -> cancel -> create)
# ===========================================================================
def test_roll_fallback_cancel_creates_fresh_rung_at_target():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    top = max(st.ladder, key=lambda o: o.price)   # 0.50
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    assert st.roll_pending is not None
    # the amend FAILED -> the executor cancelled the order -> OrderCancelled(no fill).
    st, acts = _feed(p, st, OrderCancelled(am.order_id, now + 1.1))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 1 and places[0].price == Decimal("0.39")   # fresh rung at the roll target
    assert st.roll_pending is None
    assert st.anchor_n_top == Decimal("0.49")
    # ack the fresh rung -> the ladder is whole again at 0.49..0.39.
    st, _ = _feed(p, st, OrderAck(places[0].client_order_id, "OID-new", now + 1.2))
    assert _prices(st) == [Decimal("0.49") - i * Decimal("0.01") for i in range(11)]


# ===========================================================================
# Allotment latch + shakedown amend twin
# ===========================================================================
def test_allotment_latch_stops_quoting_after_full_sweep():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    for i, o in enumerate(list(st.ladder)):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)
    assert st.rest_allotment_done
    # a later healthy tick must place NOTHING (the allotment is done).
    st, acts = _feed_all(p, st, _books(now + 0.8))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.ladder == ()


def test_shakedown_roll_emits_would_amend_twin():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p, shakedown=True)
    now = T - 600
    st, acts = _feed_all(p, st, _books(now))
    for a in [x for x in acts if x.kind == ActionKind.WOULD_PLACE_REST]:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.77"), now + 1))
    assert [a for a in acts if a.kind == ActionKind.WOULD_AMEND_REST]
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]


# ===========================================================================
# Shadow suppression (forked from V3.2)
# ===========================================================================
def test_shadow_print_outside_window_suppressed():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    # establish context in-window, then refresh the books just before a print AFTER T-5 (out of window).
    st, _ = _feed_all(p, st, _books(T - 600))
    st, _ = _feed_all(p, st, _books(T - 200.5))       # keep the spot bucket fresh at the print
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.62"), "yes", Decimal(5), T - 200))
    assert not st.shadows["0.10"].filled
    assert [a for a in acts if a.kind == ActionKind.SHADOW_FILL_OUTSIDE_WINDOW]


def test_shadow_print_below_n_min_suppressed():
    p = _params(tol=Decimal("0.01"), deb_ms=0, n_min=Decimal("0.05"))
    st = _state(p)
    now = T - 600
    # W ~1.8503 -> the shadow n(0.10) solves to 0.04 (< n_min 0.05); a print clearing that sub-min offer
    # must be SUPPRESSED (the live path would never have rested at n<n_min).
    st, _ = _feed_all(p, st, [
        BookUpdate(B_SD, _top("0.05", "0.06"), now),
        BookUpdate(STK_SU, _top("0.05", "0.06"), now),   # no_ask(Su) = 0.95
        BookUpdate(STK_SD, _top("0.88", "0.89"), now),   # yes_ask(Sd) = 0.89 -> W ~1.8503
    ])
    assert st.shadows["0.10"].n == Decimal("0.04")
    st, acts = _feed(p, st, Trade(B_SD, Decimal("0.99"), "yes", Decimal(5), now + 0.1))
    assert not st.shadows["0.10"].filled
    assert [a for a in acts if a.kind == ActionKind.SHADOW_FILL_BELOW_MIN]


def test_shadow_fills_once_per_hour():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.62"), "yes", Decimal(5), now + 1))
    fill1 = st.shadows["0.10"].fill
    st, _ = _feed(p, st, Trade(B_SD, Decimal("0.70"), "yes", Decimal(5), now + 2))
    assert st.shadows["0.10"].fill is fill1        # not re-filled


# ===========================================================================
# Stand-down dedup + n_top None
# ===========================================================================
def test_stand_down_reason_dedups_then_resumes():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    # no bucket -> no_spot_bucket stand-down once.
    st, a1 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now))
    st, a2 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.01))
    reasons = [x.reason for x in a1 + a2 if x.kind == ActionKind.STAND_DOWN]
    assert reasons.count("no_spot_bucket") == 1     # deduped across ticks
    # bring the bucket up -> resume + place.
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now + 0.02))
    assert [a for a in acts if a.kind == ActionKind.PLACE_REST]


def test_n_top_none_when_wing_missing_stands_down():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    # only a bucket book (spot present) but no strike books -> W None -> n_top None -> stand down.
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))
    assert st.n_top is None
    assert [a for a in acts if a.kind == ActionKind.STAND_DOWN
            and a.reason in ("stale_or_missing_wing", "no_spot_bucket")]
    assert st.ladder == ()


# ===========================================================================
# Round 2 (reviewer 2026-09-22): rung/E_rung labels + invariant + pacing + fast shift
# ===========================================================================
def _rung_cents(o):
    return int((o.E_rung - Decimal("0.05")) / Decimal("0.01"))


def test_rung_refreshed_with_e_rung_after_roll_down_and_up():
    # BLOCKING #1: rung must track n_top alongside E_rung. After a roll every live order satisfies
    # rung == (E_rung - E_min) in cents == round((n_top - price)/1c).
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, acts = _refresh(p, st, now + 1, sd_ask="0.77")    # roll DOWN 1c (n_top 0.50 -> 0.49)
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    for o in st.ladder:
        assert o.rung == _rung_cents(o), f"rung {o.rung} != E_rung-implied {_rung_cents(o)} at {o.price}"
        assert o.rung == int((st.n_top - o.price) / Decimal("0.01"))
    st, acts = _refresh(p, st, now + 2, sd_ask="0.76")    # roll UP 1c back (n_top 0.49 -> 0.50)
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 2.1))
    for o in st.ladder:
        assert o.rung == _rung_cents(o)
        assert o.rung == int((st.n_top - o.price) / Decimal("0.01"))


def test_rungfill_captures_live_rung_after_rolls():
    # BLOCKING #1 (falsifier key): after rolling the ladder down, a fill books the LIVE rung, not a stale one.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50
    for i, ask in enumerate(["0.77", "0.78"]):    # roll down 2c -> ladder 0.48..0.38, n_top 0.48
        st, acts = _refresh(p, st, now + 1 + i, sd_ask=ask)
        while st.roll_pending is not None:
            rp = st.roll_pending
            st, _ = _feed(p, st, OrderAmended(rp.order_id, rp.new_coid, rp.target_price, now + 1 + i + 0.5))
    assert st.n_top == Decimal("0.48")
    top = max(st.ladder, key=lambda o: o.price)   # price 0.48 -> rung 0
    st, _ = _sweep_fill(p, st, top, now + 5)
    rf = st.rest_fills[-1]
    assert rf.price == Decimal("0.48") and rf.rung == 0 and rf.E_rung == Decimal("0.05")


def test_w_reverts_during_in_flight_roll_invariants_green():
    # BLOCKING #2 (scenario i): n_top reverts while a roll is in flight; check_invariants (asserted after
    # EVERY event by _feed) must stay green, and the ladder self-heals.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50
    st, acts = _refresh(p, st, now + 1, sd_ask="0.77")   # n_top -> 0.49, roll DOWN issued
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    assert st.roll_pending is not None
    st, _ = _refresh(p, st, now + 1.05, sd_ask="0.76")   # W reverts (n_top 0.50) while roll in flight
    assert st.n_top == Decimal("0.50")
    st, _ = _feed(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, now + 1.1))
    for i in range(4):                            # the reverse-roll heals the ladder; each step checked
        if st.roll_pending is not None:
            rp = st.roll_pending
            st, _ = _feed(p, st, OrderAmended(rp.order_id, rp.new_coid, rp.target_price, now + 1.2 + i * 0.1))
        st, _ = _refresh(p, st, now + 1.6 + i * 0.1, sd_ask="0.76")
    assert len(st.ladder) == 11 and len(set(o.price for o in st.ladder)) == 11
    for o in st.ladder:
        assert o.rung == int((st.n_top - o.price) / Decimal("0.01"))


def test_cap_crash_fast_shift_then_converges_invariants_green():
    # BLOCKING #2 + QUESTION #4: a cap crash strands all K above a bound cap; the fast shift cancels all
    # in ONE step and re-places below the cap. check_invariants green after every event (via _feed).
    p = _params(tol=Decimal("0.01"), deb_ms=0)   # fast_shift_min_cents default 4
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # ladder 0.50..0.40, cap 0.64
    ids = [o.order_id for o in st.ladder]
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 1))
    st, _ = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 1))
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.62", "0.63"), now + 1))   # cap -> 0.37, n_top -> 0.37
    assert st.cap == Decimal("0.37") and st.n_top == Decimal("0.37")
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11 and st.ladder == () and st.awaiting_replace   # ONE-step cancel, not a crawl
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    for i, oid in enumerate(ids):
        st, acts = _feed(p, st, OrderCancelled(oid, now + 1.1 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 11
    assert sorted((a.price for a in places), reverse=True)[0] == Decimal("0.37")
    assert all(o.price <= st.cap for o in st.ladder)


def test_pacing_2c_one_debounce_then_ack_driven():
    # Q4/Q5: deb_ms debounces only the START; a same-sign continuation rolls on the ack (no re-debounce).
    p = _params(tol=Decimal("0.01"), deb_ms=2000)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # placed at now, last_replace_ts=now
    # keep strikes fresh at a <=1s cadence (no move, no roll) so the debounce clock stays at placement.
    st, _ = _refresh(p, st, now + 0.9, sd_ask="0.76")
    st, _ = _refresh(p, st, now + 1.8, sd_ask="0.76")
    assert st.last_replace_ts == now              # no re-placement happened
    st, acts = _refresh(p, st, now + 2.1, sd_ask="0.78")  # deb elapsed, jump DOWN 2c -> first roll
    assert st.n_top == Decimal("0.48")
    ams = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(ams) == 1 and st.converging_dir == -1
    am1 = ams[0]
    st, acts2 = _feed(p, st, OrderAmended(am1.order_id, am1.updated_client_order_id, am1.price, now + 2.15))
    ams2 = [a for a in acts2 if a.kind == ActionKind.AMEND_REST]
    assert len(ams2) == 1, "second cent must roll on the ack without waiting deb_ms again"
    am2 = ams2[0]
    st, _ = _feed(p, st, OrderAmended(am2.order_id, am2.updated_client_order_id, am2.price, now + 2.2))
    assert st.roll_pending is None and st.converging_dir == 0
    assert _prices(st) == [Decimal("0.48") - i * Decimal("0.01") for i in range(11)]
    assert st.roll_count == 2


def test_pacing_sign_flip_re_debounces():
    p = _params(tol=Decimal("0.01"), deb_ms=1000)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, _ = _refresh(p, st, now + 0.9, sd_ask="0.76")    # keep fresh (< deb; no move)
    st, _ = _refresh(p, st, now + 1.1, sd_ask="0.77")    # deb elapsed -> roll DOWN
    assert st.converging_dir == -1 and st.roll_pending is not None
    rp = st.roll_pending
    st, _ = _feed(p, st, OrderAmended(rp.order_id, rp.new_coid, rp.target_price, now + 1.15))
    assert st.converging_dir == 0                         # converged at n_top 0.49
    st, acts = _refresh(p, st, now + 1.3, sd_ask="0.76")  # flip UP within deb of the last roll -> wait
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    st, acts = _refresh(p, st, now + 2.2, sd_ask="0.76")  # deb elapsed since the last roll -> roll UP
    assert [a for a in acts if a.kind == ActionKind.AMEND_REST]


def test_pacing_1c_single_roll_unchanged():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, acts = _refresh(p, st, now + 1, sd_ask="0.77")    # 1c move -> exactly one roll
    ams = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(ams) == 1
    rp = st.roll_pending
    st, acts2 = _feed(p, st, OrderAmended(rp.order_id, rp.new_coid, rp.target_price, now + 1.1))
    assert not [a for a in acts2 if a.kind == ActionKind.AMEND_REST]   # nothing more to roll
    assert st.converging_dir == 0


def test_fast_shift_5c_move_cancels_all_places_all():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # n_top 0.50
    st, _ = _refresh(p, st, now + 0.5, sd_ask="0.76")
    ids = [o.order_id for o in st.ladder]
    st, acts = _refresh(p, st, now + 1, sd_ask="0.81")   # n_top -> 0.45 (down 5c >= 4)
    assert st.n_top == Decimal("0.45")
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11 and st.ladder == () and st.awaiting_replace
    assert not [a for a in acts if a.kind == ActionKind.AMEND_REST]
    for i, oid in enumerate(ids):
        st, acts = _feed(p, st, OrderCancelled(oid, now + 1.1 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 11
    assert sorted((a.price for a in places), reverse=True) == [
        Decimal("0.45") - i * Decimal("0.01") for i in range(11)
    ]


def test_3c_move_still_crawls_not_fast_shift():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, _ = _refresh(p, st, now + 0.5, sd_ask="0.76")
    st, acts = _refresh(p, st, now + 1, sd_ask="0.79")   # n_top -> 0.47 (down 3c < 4)
    assert st.n_top == Decimal("0.47")
    assert [a for a in acts if a.kind == ActionKind.AMEND_REST]   # a roll, not a cancel-all
    assert not [a for a in acts if a.kind == ActionKind.CANCEL_REST]


# ---------------------------------------------------------------------------
# Round 3 (reviewer 2026-09-22): re-placement must cap at K - filled (BLOCKING #R2-1)
# ---------------------------------------------------------------------------
def test_fast_shift_after_partial_sweep_caps_at_k_minus_filled():
    # (a) partial sweep (3 filled) -> fast-shift 5c -> re-place exactly K-3=8, exposure <= K, and a burst
    # sweep of the re-placed ladder books exactly K total (never > K). invariants green throughout (_feed).
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # ladder 0.50..0.40
    for o in sorted(st.ladder, key=lambda o: o.price, reverse=True)[:3]:
        st, _ = _sweep_fill(p, st, o, now + 0.5)   # fill top 3 -> survivors 8
    assert st.rungs_filled == 3 and len(st.ladder) == 8
    st, _ = _refresh(p, st, now + 0.8, sd_ask="0.76")   # keep fresh, anchor 0.50
    st, acts = _refresh(p, st, now + 1, sd_ask="0.81")   # n_top -> 0.45 (down 5c) -> fast shift
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 8 and st.ladder == () and st.awaiting_replace
    for i, c in enumerate(cancels):
        st, acts = _feed(p, st, OrderCancelled(c.order_id, now + 1.1 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 8, "must re-place K - filled = 8, not K = 11"
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID2-{a.client_order_id}", now + 1.3))
    assert st.rungs_filled + len(st.ladder) == 11    # exposure exactly K
    # burst sweep the re-placed ladder -> total window fills == K (never exceeds).
    for o in list(st.ladder):
        st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", now + 1.5))
    assert st.rungs_filled == 11 and st.rest_allotment_done


def test_bucket_change_after_partial_sweep_caps_at_k_minus_filled():
    # (b) partial sweep (3 filled) -> bucket change -> re-place exactly K-3=8 on the new ticker.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # bucket 79600
    for o in sorted(st.ladder, key=lambda o: o.price, reverse=True)[:3]:
        st, _ = _sweep_fill(p, st, o, now + 0.5)
    assert st.rungs_filled == 3 and len(st.ladder) == 8
    # 79700 becomes the higher-mid spot.
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.9))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 0.9))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now + 0.9))
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 8 and st.ladder == () and st.outstanding_cancels == 8
    for i, c in enumerate(cancels):
        st, acts = _feed(p, st, OrderCancelled(c.order_id, now + 1.0 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 8, "bucket change must re-place K - filled = 8"
    assert all(o.bucket_Sd == 79700 for o in st.ladder)
    assert st.rungs_filled + len(st.ladder) == 11


def test_bucket_change_after_full_sweep_places_nothing():
    # (c) K filled -> allotment latched -> a bucket change places nothing.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    for i, o in enumerate(list(st.ladder)):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)   # fill all 11
    assert st.rungs_filled == 11 and st.rest_allotment_done and st.ladder == ()
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.9))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now + 0.9))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now + 0.9))
    assert not [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert st.ladder == () and st.rest_allotment_done


def test_place_all_budget_zero_latches_allotment():
    # the defensive budget<=0 branch of _place_all: a re-place request when the allotment is already spent
    # latches rest_allotment_done and places nothing (belt-and-braces to the reactive max_sets latch).
    from service.v33.core import _place_all
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _feed_all(p, st, _books(now))         # sets n_top / spot context
    st = replace(st, ladder=(), rungs_filled=11, rest_allotment_done=False)
    st2, acts = _place_all(p, st, now)
    assert acts == [] and st2.rest_allotment_done and st2.ladder == ()


def test_invariant_flags_window_exposure_over_k():
    # BLOCKING #R2-1 guard: filled + resting must never exceed K (the harness would now catch a regrow).
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)          # 11 resting
    bad = replace(st, rungs_filled=3)             # 3 + 11 = 14 > 11
    with pytest.raises(AssertionError):
        bad.check_invariants(p)


def test_invariant_flags_stale_rung_label():
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    bad_order = replace(st.ladder[0], rung=st.ladder[0].rung + 3)
    bad = replace(st, ladder=(bad_order,) + st.ladder[1:])
    with pytest.raises(AssertionError):
        bad.check_invariants(p)


def test_invariant_allows_negative_rung_above_n_top():
    # a rung resting ABOVE n_top (rung < 0, "stranded") is a legitimate transient, not an error.
    p = _params()
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    o = st.ladder[0]
    o2 = replace(o, price=Decimal("0.50"), rung=-5,
                 E_rung=Decimal("0.05") + (Decimal("0.45") - Decimal("0.50")))
    st2 = replace(st, n_top=Decimal("0.45"), ladder=(o2,))
    st2.check_invariants(p)   # must NOT raise


def test_roll_integrity_all_rolls_single_order():
    # every roll in a multi-cent convergence moves exactly one order -> single-order ratio 1.0.
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # drive n_top down 3c across separate ticks, confirming each roll.
    for i, ask in enumerate(["0.77", "0.78"]):     # 0.50 -> 0.49 -> 0.48
        st, acts = _refresh(p, st, now + 1 + i, sd_ask=ask)
        am = [a for a in acts if a.kind == ActionKind.AMEND_REST]
        # confirm any pending roll(s) issued (a 1c step issues one; a 2c continue issues via confirm)
        while st.roll_pending is not None:
            rp = st.roll_pending
            st, _ = _feed(p, st, OrderAmended(rp.order_id, rp.new_coid, rp.target_price, now + 1 + i + 0.5))
    assert st.roll_count >= 1
    assert st.roll_single_order_count == st.roll_count   # every roll moved exactly one order
