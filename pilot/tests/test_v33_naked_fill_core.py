"""2026-10-03 02:00Z Test Fire #2 NAKED FILL -- core gates A (hedge every owned fill) and B (cancel identity).

Incident (pilot/build/v33_naked_fill_2026_10_03.md): a stale-wing hold expired into a cancel-all, the core
resumed 230 ms later and placed 11 fresh rungs #23..#33 while the old DELETEs were unconfirmed. Nine of the
creates failed the venue pre-flight (``rest_invariant_violation``), each returning an id-less
``OrderCancelled``; the core matched each by ``None == None`` to the FIRST pending order and evicted #23 (and
then others) by POSITION. #23 and #27 passed the pre-flight, rested at the venue owned by nobody, and filled
(0.40 + 0.60 @0.22, 1 @0.18). ``_apply_fill`` found no ladder order and RETURNED: no wings, -$0.40.

The fixture ``fixtures/v33/naked_fill_20261003T020000Z.json`` carries the window's event ORDER (coids,
prices, the rejection order, the executor stand-down instant, the three ws fills) extracted read-only from
the journal; the books are synthetic whole-cent tops that solve the observed ladder top n_top = 0.22. FAKES
ONLY: no network, no proxy, no key, no holdout / seal read (the window is 2026-10-03, post-seal)."""

from __future__ import annotations

import json
import os
from dataclasses import replace as dr
from decimal import Decimal

from service.book import TopOfBook
from service.v33 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    OrderAck,
    OrderCancelled,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import V33ActionKind
from service.v33.core import RollPending
from service.v33.events import V33Fill

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v33", "naked_fill_20261003T020000Z.json")
_ONE = Decimal(1)


def _fix():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


FX = _fix()
B = FX["bucket_ticker"]
S_SD = FX["strike_sd"]
S_SU = FX["strike_su"]
BK = {B: tuple(FX["bucket_floor_cap"])}
T0 = FX["t0_epoch"]
COID = {int(p["coid"].rsplit("-", 1)[1]): p["coid"] for p in FX["places"]}


def _params():
    # the shipped policy (E_min 0.08, K=11, 1 lot/rung); only the start debounces are zeroed so the replay
    # places on the first fresh tick (their own tests cover them). Freshness stays at the shipped 1.0 s:
    # every step below re-sends the books, as the live feed did (0 ws drops in this hour).
    return dr(load_v33_params(), deb_ms=0, bucket_switch_deb_ms=0, bucket_switch_hysteresis_usd=0)


def _top(bid: str, ask: str) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(500), yes_ask=ya, yes_ask_size=Decimal(500),
                     no_bid=_ONE - ya, no_bid_size=Decimal(500), no_ask=_ONE - yb,
                     no_ask_size=Decimal(500), suspect=False)


def _books(ts: float):
    # yes_ask(Sd)=0.80, no_ask(Su)=1-0.14=0.86 -> W = 0.80+fee + 0.86+fee = 1.6797 -> n_top = 0.22 at E_min 8c
    # (0.22 + fee(0.22) = 0.2321 <= 2 - 0.08 - 1.6797 = 0.2403 < 0.23 + fee(0.23)). Bucket no_ask 0.35 -> cap 0.34.
    return [BookUpdate(B, _top("0.65", "0.67"), ts),
            BookUpdate(S_SD, _top("0.79", "0.80"), ts),
            BookUpdate(S_SU, _top("0.14", "0.15"), ts)]


def _run(p, st, events):
    acts = []
    for e in events:
        st, a = decide_v33(p, st, e)
        st.check_invariants(p)
        acts += a
    return st, acts


def _kinds(acts, kind):
    return [a for a in acts if a.kind == kind]


def _alarms(acts, name=None):
    return [a for a in acts if a.kind == V33ActionKind.ALARM and (name is None or a.reason == name)]


def _place_23_to_33(p):
    """The resume's ``_place_all``: 11 PENDING rungs #23..#33 at 0.22..0.12 (coid_seq continues at 22)."""
    st = dr(V33State.new(FX["close_time"], FX["close_epoch"], BK, p), coid_seq=22)
    t_place = T0 + FX["places"][0]["dt"]
    st, acts = _run(p, st, _books(t_place))
    places = _kinds(acts, ActionKind.PLACE_REST)
    return st, places, t_place


def _ws_fill(f, ts, *, source="ws"):
    return V33Fill(order_id=f["order_id"], client_order_id=f["coid"], count=Decimal(f["count"]),
                   price=Decimal(f["price"]), side="no", server_ts=ts, market_ticker=f["market"],
                   source=source)


def _fills_then_flush(p, st):
    """Drive the three venue fills in journal order, each on a fresh book tick, then a clock tick past the
    150 ms coalesce window so each closed batch takes its wings."""
    acts = []
    for f in FX["fills"]:
        ts = T0 + f["dt"]
        st, a = _run(p, st, _books(ts - 0.01) + [_ws_fill(f, ts)])
        acts += a
        st, a = _run(p, st, _books(ts + 0.2) + [ClockTick(ts + 0.2)])
        acts += a
    return st, acts


# ===========================================================================
# fixture provenance
# ===========================================================================
def test_fixture_is_the_02z_event_order():
    assert FX["close_time"] == "2026-10-03T02:00:00Z"            # post-seal, not the 08-20..29 holdout
    assert [p["coid"] for p in FX["places"]] == [COID[i] for i in range(23, 34)]
    assert [p["price"] for p in FX["places"]] == [f"0.{n:02d}" for n in range(22, 11, -1)]
    assert len(FX["rejections"]) == 9
    assert {r["coid"] for r in FX["rejections"]} == {COID[i] for i in (24, 25, 26, 28, 29, 30, 31, 32, 33)}
    assert FX["rejections"][0]["coid"] == COID[31]                 # #31 tripped the stand-down
    assert FX["executor_standdown_dt"] == FX["rejections"][0]["dt"]
    # the PRE-FIX fingerprint: the stood-down core's cancel list was #24..#33 -- #23 already evicted.
    assert COID[23] not in FX["precfix_core_cancel_list"] and len(FX["precfix_core_cancel_list"]) == 10
    assert [(f["coid"], f["count"], f["price"]) for f in FX["fills"]] == [
        (COID[23], "0.40", "0.22"), (COID[23], "0.60", "0.22"), (COID[27], "1", "0.18")]


# ===========================================================================
# GOLDEN: the 02:00Z event order, replayed through the fixed core
# ===========================================================================
def test_golden_02z_replay_fills_are_hedged_two_lots_on_the_buckets_wings():
    """Faithful order: the executor stand-down is applied by the driver (``_apply_executor_standdown``,
    journal idx 33998) BEFORE the first rejection is decided, then the nine rejections, then the three ws
    fills ~4.5 min later. Fixed behaviour: (B) the first rejection drops EXACTLY #31 -- the stood-down
    core's cancel-all then names all ten others INCLUDING #23 and #27 (the pre-fix list lacked #23); the
    other eight rejections touch nothing. (A) the fills on #23 / #27 -- orders the core has now cleared but
    the executor still owns -- are booked from the fill events and hedged: TAKE_WINGS totalling 2 lots on
    the fill bucket's two strikes, with the core stood down and its ladder empty."""
    p = _params()
    st, places, _ = _place_23_to_33(p)
    assert [a.client_order_id for a in places] == [COID[i] for i in range(23, 34)]
    assert [str(a.price) for a in places] == [f"0.{n:02d}" for n in range(22, 11, -1)]

    st = dr(st, stood_down=True)                                    # the driver's executor stand-down
    rej = FX["rejections"]
    st, a1 = _run(p, st, [OrderCancelled(order_id=None, server_ts=T0 + rej[0]["dt"],
                                         filled_count_before_cancel=Decimal(0),
                                         client_order_id=rej[0]["coid"])])
    cancelled = [a.client_order_id for a in _kinds(a1, ActionKind.CANCEL_REST)]
    assert sorted(cancelled) == sorted(COID[i] for i in range(23, 34) if i != 31)
    assert COID[23] in cancelled and COID[27] in cancelled       # the pre-fix list was missing #23
    assert sorted(cancelled) != sorted(FX["precfix_core_cancel_list"])
    assert st.ladder == () and not _kinds(a1, ActionKind.PLACE_REST)
    assert not _alarms(a1)

    rest = []
    for r in rej[1:]:
        st, a = _run(p, st, [OrderCancelled(order_id=None, server_ts=T0 + r["dt"],
                                            filled_count_before_cancel=Decimal(0),
                                            client_order_id=r["coid"])])
        rest += a
    assert not _kinds(rest, ActionKind.PLACE_REST) and not _alarms(rest)

    st, acts = _fills_then_flush(p, st)
    takes = _kinds(acts, ActionKind.TAKE_WINGS)
    assert sum(Decimal(str(t.count)) for t in takes) == Decimal(2), [str(t.count) for t in takes]
    for t in takes:
        assert [(lg.ticker, lg.side) for lg in t.legs] == [(S_SD, "yes"), (S_SU, "no")]
    hedged = _alarms(acts, "orphan_rung_fill_hedged")
    assert [(a.client_order_id, str(a.count)) for a in hedged] == [
        (COID[23], "0.40"), (COID[23], "0.60"), (COID[27], "1")]
    assert sum(f.count for f in st.rest_fills) == Decimal(2)
    assert all(f.bucket_ticker == B and f.bucket_Sd == 84600 for f in st.rest_fills)
    assert not st.bucket_unknown and st.stood_down                   # still stood down: hedged, not quoting
    assert not _kinds(acts, ActionKind.PLACE_REST) and not _kinds(acts, ActionKind.AMEND_REST)
    assert st.rest_booked_by_coid[COID[23]] == Decimal(1) and st.rest_booked_by_coid[COID[27]] == Decimal(1)


def test_nine_rejections_against_eleven_pending_leave_exactly_the_two_accepted():
    """B in isolation (the rejections decided before any stand-down): identity by coid, not position. #23
    and #27 (accepted) stay on the ladder; none of the nine rejected coids remains. Any slot the convergence
    re-fills is a FRESH coid (> #33), never a resurrected rejected one. The fills on #23 / #27 then book on
    the ladder path (no orphan alarm) and take 2 lots of wings."""
    p = _params()
    st, _, _ = _place_23_to_33(p)
    acts = []
    for r in FX["rejections"]:
        ts = T0 + r["dt"]
        st, a = _run(p, st, _books(ts) + [OrderCancelled(order_id=None, server_ts=ts,
                                                          filled_count_before_cancel=Decimal(0),
                                                          client_order_id=r["coid"])])
        acts += a
    coids = {o.client_order_id for o in st.ladder}
    assert COID[23] in coids and COID[27] in coids
    assert not coids & {r["coid"] for r in FX["rejections"]}
    refills = {o.client_order_id for o in st.ladder} - {COID[23], COID[27]}
    assert all(int(c.rsplit("-", 1)[1]) > 33 for c in refills)
    assert not _alarms(acts)
    # ack the two accepted with their venue ids, then the venue fills them.
    for f in (FX["fills"][0], FX["fills"][2]):
        st, _ = _run(p, st, [OrderAck(f["coid"], f["order_id"], T0 + FX["places"][0]["dt"] + 1)])
    st, acts = _fills_then_flush(p, st)
    assert not _alarms(acts, "orphan_rung_fill_hedged")
    assert sum(Decimal(str(t.count)) for t in _kinds(acts, ActionKind.TAKE_WINGS)) == Decimal(2)


# ===========================================================================
# A: the orphan branch directly
# ===========================================================================
def _healthy_empty(p, ts):
    """A STOOD-DOWN core with fresh books (n_top 0.22) and an EMPTY ladder -- the state the stand-down
    leaves behind (it cancelled everything it knew about)."""
    st = dr(V33State.new(FX["close_time"], FX["close_epoch"], BK, p), stood_down=True)
    st, _ = _run(p, st, _books(ts))
    assert st.ladder == () and st.n_top == Decimal("0.22")
    return st


def test_orphan_fill_stood_down_empty_ladder_books_and_takes_wings():
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    st, a = _run(p, st, _books(ts) + [_ws_fill({"order_id": "oid-x", "coid": "v33-ghost-1", "count": "1",
                                                 "price": "0.18", "market": B}, ts + 0.01)])
    assert [x.reason for x in _alarms(a)] == ["orphan_rung_fill_hedged"]
    assert len(st.rest_fills) == 1 and st.rest_fills[0].count == Decimal(1)
    assert st.rest_fills[0].bucket_Sd == 84600 and not st.rest_fills[0].orphan   # 0.18 is rung 4 @ 0.22
    st, a = _run(p, st, _books(ts + 0.3) + [ClockTick(ts + 0.3)])
    takes = _kinds(a, ActionKind.TAKE_WINGS)
    assert len(takes) == 1 and takes[0].count == Decimal(1)
    assert [(lg.ticker, lg.side) for lg in takes[0].legs] == [(S_SD, "yes"), (S_SU, "no")]
    assert not _kinds(a, ActionKind.PLACE_REST)                      # hedges, never quotes


def test_orphan_fill_off_the_ladder_geometry_is_labelled_orphan_and_still_hedged():
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    ms_before = st.margin_state
    st, a = _run(p, st, [_ws_fill({"order_id": "oid-y", "coid": "v33-ghost-2", "count": "0.44",
                                    "price": "0.30", "market": B}, ts + 0.01)])   # above n_top 0.22
    assert st.rest_fills[0].orphan and st.rest_fills[0].weight is None
    assert st.margin_state == ms_before                               # the array is untouched
    st, a = _run(p, st, _books(ts + 0.3) + [ClockTick(ts + 0.3)])
    assert [t.count for t in _kinds(a, ActionKind.TAKE_WINGS)] == [Decimal("0.44")]


def test_orphan_ws_echo_after_cancel_confirm_is_not_hedged_twice():
    """The cancel-confirm (cumulative 1) books + hedges the lot; the late ws echo of the SAME lot books 0."""
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    st, a = _run(p, st, [OrderCancelled(order_id="oid-z", server_ts=ts, filled_count_before_cancel=Decimal(1),
                                        client_order_id="v33-ghost-3", price=Decimal("0.20"),
                                        market_ticker=B)])
    assert [x.reason for x in _alarms(a)] == ["orphan_rung_fill_hedged"] and len(st.rest_fills) == 1
    st, a = _run(p, st, [_ws_fill({"order_id": "oid-z", "coid": "v33-ghost-3", "count": "1",
                                    "price": "0.20", "market": B}, ts + 0.5)])
    assert len(st.rest_fills) == 1 and not _alarms(a)


def test_orphan_poll_catchup_then_ws_echo_is_not_hedged_twice():
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    f = {"order_id": "oid-w", "coid": "v33-ghost-4", "price": "0.20", "market": B}
    st, _ = _run(p, st, [_ws_fill({**f, "count": "0.40"}, ts)])
    st, _ = _run(p, st, [_ws_fill({**f, "count": "0.60"}, ts + 1, source="poll")])   # cum 1 - booked 0.4
    st, a = _run(p, st, [_ws_fill({**f, "count": "0.60"}, ts + 2)])                 # late ws echo
    assert sum(x.count for x in st.rest_fills) == Decimal(1) and not _alarms(a)


def test_orphan_fill_missed_by_cancel_confirm_is_still_hedged():
    """An unreadable cancel-confirm status reports 0 filled; the ws fill that follows is real -> hedged."""
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    st, _ = _run(p, st, [OrderCancelled(order_id="oid-v", server_ts=ts, filled_count_before_cancel=Decimal(0),
                                        client_order_id="v33-ghost-5", price=Decimal("0.20"),
                                        market_ticker=B)])
    st, a = _run(p, st, [_ws_fill({"order_id": "oid-v", "coid": "v33-ghost-5", "count": "1",
                                    "price": "0.20", "market": B}, ts + 0.5)])
    assert len(st.rest_fills) == 1 and _alarms(a, "orphan_rung_fill_hedged")


def test_orphan_fill_on_a_non_bucket_market_is_alarmed_not_booked():
    p = _params()
    ts = T0 + 21.0
    st = _healthy_empty(p, ts)
    st, a = _run(p, st, [_ws_fill({"order_id": "oid-s", "coid": "v33-ghost-6", "count": "1",
                                    "price": "0.20", "market": S_SD}, ts)])
    assert [x.reason for x in _alarms(a)] == ["orphan_fill_not_bucket"]
    assert st.rest_fills == () and not st.bucket_unknown


def test_dry_sim_ladder_fill_path_unchanged_no_alarm():
    """A fill on a LIVE ladder rung (the dry-sim / normal path) books exactly as before: no orphan alarm."""
    p = _params()
    st, places, t = _place_23_to_33(p)
    st, _ = _run(p, st, [OrderAck(a.client_order_id, f"oid-{a.client_order_id}", t + 0.5) for a in places])
    o = st.ladder[0]
    st, a = _run(p, st, [V33Fill(order_id=o.order_id, client_order_id=o.client_order_id, count=Decimal(1),
                                 price=o.price, side="no", server_ts=t + 1, market_ticker=B)])
    assert not _alarms(a) and len(st.rest_fills) == 1 and not st.rest_fills[0].orphan


# ===========================================================================
# B: identity edge cases
# ===========================================================================
def test_cancel_with_no_identity_matches_nothing_and_alarms():
    p = _params()
    st, _, t = _place_23_to_33(p)
    before = st.ladder
    st, a = _run(p, st, [OrderCancelled(order_id=None, server_ts=t + 0.1,
                                        filled_count_before_cancel=Decimal(0))])
    assert st.ladder == before
    assert [x.reason for x in _alarms(a)] == ["cancel_unattributed"]


def test_pending_rejection_never_decrements_bucket_change_outstanding_cancels():
    p = _params()
    st, _, t = _place_23_to_33(p)
    st = dr(st, outstanding_cancels=3, awaiting_replace=True)
    st, _ = _run(p, st, [OrderCancelled(order_id=None, server_ts=t + 0.1,
                                        filled_count_before_cancel=Decimal(0), client_order_id=COID[30])])
    assert st.outstanding_cancels == 3
    assert COID[30] not in {o.client_order_id for o in st.ladder}


def test_stood_down_roll_fallback_does_not_replace():
    """C(iii) core side: an amend the stood-down executor refuses comes back as a plain cancel of the
    rolling order; the stood-down core drops it and does NOT place the fallback rung."""
    p = _params()
    st, places, t = _place_23_to_33(p)
    st, _ = _run(p, st, [OrderAck(a.client_order_id, f"oid-{a.client_order_id}", t + 0.5) for a in places])
    o = st.ladder[0]
    rp = RollPending(order_id=o.order_id, old_coid=o.client_order_id, new_coid="v33-roll-new",
                     target_price=Decimal("0.11"), target_margin=19, started_ts=t + 1)
    st = dr(st, rolls_in_flight=(rp,), stood_down=True)
    st, a = decide_v33(p, st, OrderCancelled(order_id=o.order_id, server_ts=t + 2,
                                             filled_count_before_cancel=Decimal(0),
                                             client_order_id=o.client_order_id))
    assert not _kinds(a, ActionKind.PLACE_REST)
    assert o.client_order_id not in {x.client_order_id for x in st.ladder}



def test_ack_path_cancel_of_an_uncounted_create_never_decrements_outstanding_cancels():
    """A create still in flight when the core cancelled it was PENDING (never counted); its later
    ack-path OrderCancelled(order_id, coid) must not resolve one of the COUNTED bucket-change cancels."""
    p = _params()
    st, _, t = _place_23_to_33(p)
    st = dr(st, ladder=(), outstanding_cancels=2, awaiting_replace=True)
    st, _ = _run(p, st, [OrderCancelled(order_id="oid-late", server_ts=t + 0.1,
                                        filled_count_before_cancel=Decimal(0), client_order_id=COID[30])])
    assert st.outstanding_cancels == 2
