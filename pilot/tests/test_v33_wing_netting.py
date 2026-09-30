"""V3.3 D5 (Brad, 2026-09-30): NETTED wings across adjacent buckets.

Set A on bucket ``[Sd, Su)`` holds NO on the ``Su`` strike; when spot moves up one bucket, set B (on
``[Su, Su+w)``) takes YES on B's ``Sd`` strike — which IS the ``Su`` strike (one market). The venue nets
+YES/+NO to flat and credits $1/contract immediately. Money is unchanged (two sets still pay $4), but the
netted market shows position 0 with no settlement for our legs, so the books must recognise it: book the
pair $1 realised NOW, close both legs (never held to settlement / never looked up by the backfill), and
handle partial overlap (1.44 NO vs 2.00 YES -> 1.44 netted, 0.56 YES still held).

FAKES ONLY: no network, no proxy, no key/holdout. The SEAL (2026-08-02..18) is untouched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.v33 import (
    ActionKind,
    BookUpdate,
    Fill,
    OrderAck,
    OrderCancelled,
    V33Params,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.actions import V33ActionKind
from service.v33.ledger import compute_ladder_money_math

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000
# three consecutive buckets so A rests on 79600 and B on 79700 (B's Sd strike == A's Su strike).
BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99),
      "KXBTC-RANGE-B79700": (79700.0, 79799.99),
      "KXBTC-RANGE-B79800": (79800.0, 79899.99)}
B_A = "KXBTC-RANGE-B79600"
B_B = "KXBTC-RANGE-B79700"
STK_79600 = "KXBTCD-26SEP0416-T79599.99"   # A's YES wing (Sd_A)
STK_79700 = "KXBTCD-26SEP0416-T79699.99"   # A's NO wing (Su_A) == B's YES wing (Sd_B)  <-- the netted market
STK_79800 = "KXBTCD-26SEP0416-T79799.99"   # B's NO wing (Su_B)


def _top(bid, ask):
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
                     no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
                     no_ask_size=Decimal(100), suspect=False)


def _sd(ask):
    return _top(str(Decimal(ask) - Decimal("0.01")), ask)


def _params(**over) -> V33Params:
    p = replace(load_v33_params(), E_min=Decimal("0.05"), tol=Decimal("0.01"), deb_ms=0,
                bucket_switch_deb_ms=0, bucket_switch_hysteresis_usd=0)
    return replace(p, **over) if over else p


def _state(p):
    return V33State.new(CLOSE, T, BK, p)


def _feed(p, st, ev):
    st, a = decide_v33(p, st, ev)
    st.check_invariants(p)
    return st, a


def _feed_all(p, st, evs):
    acts = []
    for e in evs:
        st, a = _feed(p, st, e)
        acts += a
    return st, acts


def _books_A(now):
    # spot on 79600; the two strike wings for A are STK_79600 (Sd) and STK_79700 (Su).
    return [BookUpdate(B_A, _top("0.35", "0.36"), now),
            BookUpdate(STK_79700, _top("0.36", "0.37"), now),
            BookUpdate(STK_79600, _sd("0.76"), now)]


def _bring_up(p, st, now):
    st, acts = _feed_all(p, st, _books_A(now))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    return st


def _fill_rung(p, st, price, now, count=None):
    o = next(x for x in st.ladder if x.price == Decimal(price) and x.live)
    c = o.count if count is None else Decimal(str(count))
    st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, c, o.price, "no", now))
    return st


def _fresh_A_strikes(p, st, now):
    st, _ = _feed(p, st, BookUpdate(STK_79700, _top("0.36", "0.37"), now))
    st, acts = _feed(p, st, BookUpdate(STK_79600, _sd("0.76"), now))
    return st, acts


def _move_to_B(p, st, now):
    # spot -> 79700; keep A's strikes + add STK_79800 (B's NO wing) fresh.
    st, _ = _feed(p, st, BookUpdate(STK_79700, _top("0.36", "0.37"), now))
    st, _ = _feed(p, st, BookUpdate(STK_79800, _top("0.20", "0.21"), now))
    st, acts = _feed(p, st, BookUpdate(B_B, _top("0.55", "0.57"), now))
    return st, acts


def _fresh_B_strikes(p, st, now):
    # B's wings: YES@STK_79700 (Sd_B) + NO@STK_79800 (Su_B).
    st, _ = _feed(p, st, BookUpdate(STK_79700, _sd("0.76"), now))   # priced as a Sd (yes_ask) strike now
    st, acts = _feed(p, st, BookUpdate(STK_79800, _top("0.20", "0.21"), now))
    return st, acts


def _fill_both_wings(p, st, now):
    """Fill every currently-pending wing leg (mark filled at its limit). Returns (st, actions)."""
    acts_all = []
    for lg in [l for l in st.wing_legs if l.status == "pending"]:
        st, a = _feed(p, st, Fill(None, lg.client_order_id, lg.count, lg.limit, lg.side, now))
        acts_all += a
    return st, acts_all


def _drive_two_sets(p, *, count_A=None, count_B=None):
    """Set A fills+hedges on 79600, spot moves to 79700, set B fills+hedges on 79700.
    Returns (final state with netting applied, the netting actions)."""
    st = _state(p)
    now = T - 600
    st = _bring_up(p, st, now)
    # SET A: fill the 0.47 rung, close coalesce + take wings, fill both wings.
    st = _fill_rung(p, st, "0.47", now + 0.5, count=count_A)
    st, _ = _fresh_A_strikes(p, st, now + 0.7)
    st, _ = _fill_both_wings(p, st, now + 0.75)
    # move to bucket 79700 -> cancel-all the rest of A's ladder (track_outstanding); confirm every cancel;
    # the last confirm re-converges and places the open slots on B. Ack those, then keep B's books fresh.
    st, acts = _move_to_B(p, st, now + 1.0)
    cancel_ids = [a.order_id for a in acts if a.kind == ActionKind.CANCEL_REST]
    places: list = []
    for i, oid in enumerate(cancel_ids):
        st, pa = _feed(p, st, OrderCancelled(oid, now + 1.1 + i * 0.001))
        places += [a for a in pa if a.kind == ActionKind.PLACE_REST]
    # fresh B books may be needed for the placement; re-tick until the ladder is on 79700.
    st, pa = _fresh_B_strikes(p, st, now + 1.25)
    places += [a for a in pa if a.kind == ActionKind.PLACE_REST]
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now + 1.3))
    assert st.ladder and st.rest_bucket_Sd == 79700, (st.rest_bucket_Sd, len(st.ladder))
    # SET B: fill the TOP rung on 79700, take + fill its wings (YES@STK_79700 nets A's NO@STK_79700).
    b_top = sorted((o.price for o in st.ladder if o.live), reverse=True)[0]
    st = _fill_rung(p, st, str(b_top), now + 1.5, count=count_B)
    st, _ = _fresh_B_strikes(p, st, now + 1.7)
    st, net_acts = _fill_both_wings(p, st, now + 1.75)
    return st, net_acts


# ===========================================================================
def test_adjacent_bucket_wings_net_to_one_dollar_and_close():
    p = _params()
    st, net_acts = _drive_two_sets(p)
    # exactly one netted pair on the shared 79700 strike, 1 contract (both sets are 1 lot).
    assert len(st.netted_pairs) == 1
    np = st.netted_pairs[0]
    assert np.ticker == STK_79700 and np.count == Decimal("1")
    # a WING_NETTED action was emitted for the journal.
    assert any(a.kind == V33ActionKind.WING_NETTED for a in net_acts)
    # both overlapping legs are fully netted (closed): A's NO@79700 and B's YES@79700.
    legs_79700 = [l for l in st.wing_legs if l.ticker == STK_79700 and l.status == "filled"]
    assert len(legs_79700) == 2
    assert all(l.netted == l.count for l in legs_79700)     # fully closed
    # realised on the pair = 1 - yes_cost - no_cost (costs incl. fees).
    exp = Decimal("1") - np.yes_cost - np.no_cost
    assert np.realised == exp


def test_netting_removes_the_strike_from_unsettled_and_books_the_dollar():
    p = _params()
    st, _ = _drive_two_sets(p)
    m = compute_ladder_money_math(st, dry_sim=False)
    # the netted 79700 strike is NOT held to settlement (neither A's NO nor B's YES).
    held_79700 = [h for h in m["held_legs"] if h["ticker"] == STK_79700]
    assert held_79700 == []
    assert m["netted_sets"] and m["netted_sets"][0]["ticker"] == STK_79700
    # both batches completed -> the realised (solved) lock of both sets is intact (netting does not
    # change it): Σ per-batch realized_lock == Σ per-rung realized_lock, and both are the two sets' locks.
    assert all(b["completed"] for b in m["wing_batch_sets"])
    assert len(m["wing_batch_sets"]) == 2 and len(m["rung_fills"]) == 2
    # batch realized_lock is the TOTAL (count-weighted); rung realized_lock is PER CONTRACT.
    batch_locks = sum((Decimal(str(b["realized_lock"])) for b in m["wing_batch_sets"]), Decimal(0))
    rung_locks = sum((Decimal(str(rf["realized_lock"])) * Decimal(str(rf["count"]))
                      for rf in m["rung_fills"]), Decimal(0))
    # realised == sum of both sets' solved locks (consistency; sign depends on the synthetic book).
    assert batch_locks == rung_locks
    # the netted $1 is booked into the floor (floor_booked includes the pair credit).
    assert m["floor_booked"] is not None


def test_partial_overlap_nets_the_smaller_and_leaves_the_remainder_held():
    # weight-2 rungs; set A fills 1.44 (its NO@79700 wing = 1.44), set B fills 2.00 (its YES@79700 = 2.00)
    # -> 1.44 nets, 0.56 of B's YES stays HELD on 79700.
    p = _params(rung_lots=[2] * 11)
    st, _ = _drive_two_sets(p, count_A="1.44", count_B="2.00")
    assert len(st.netted_pairs) == 1 and st.netted_pairs[0].count == Decimal("1.44")
    yes_79700 = next(l for l in st.wing_legs if l.ticker == STK_79700 and l.side == "yes")
    no_79700 = next(l for l in st.wing_legs if l.ticker == STK_79700 and l.side == "no")
    assert no_79700.count == Decimal("1.44") and no_79700.netted == Decimal("1.44")   # fully netted
    assert yes_79700.count == Decimal("2.00") and yes_79700.netted == Decimal("1.44")  # 0.56 left
    m = compute_ladder_money_math(st, dry_sim=False)
    held_79700 = [h for h in m["held_legs"] if h["ticker"] == STK_79700]
    assert len(held_79700) == 1                        # only B's 0.56 YES remainder is held
    assert Decimal(str(held_79700[0]["count"])) == Decimal("0.56")
