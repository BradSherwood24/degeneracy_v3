"""V3.3 L5 (2026-09-29): per-rung lot weights (``rung_lots``).

Covers the loader validation (absent -> uniform default; wrong length; negative; all-zero; exceeds the
wing-chunk hint), the pure-core mechanism (a weighted ladder rests the right lots at the right prices;
0-weight rungs are skipped without stalling the convergence of their neighbours; a roll KEEPS the moving
order's count; a partial fill of a weight-3 rung wings the filled lots and keeps the remainder resting; a
partial remainder is never rolled), the CONTRACT exposure invariant (filled + resting <= sum(rung_lots))
under partial fills and rolls, the dry-sim twin honouring per-rung counts, and the report allocation
table. Default rung_lots (absent) MUST reproduce the uniform ladder byte-for-byte -- proven here and by
the untouched golden suite.

No network, no disk beyond the shipped policy + tmp mutated copies. 2026-08-20..29 (holdout) and the
2026-08-02..18 seal are never touched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from service.book import TopOfBook
from service.run_v32 import FrozenExecutor
from service.v33 import (
    ActionKind,
    BookUpdate,
    Fill,
    OrderAck,
    OrderAmended,
    V33Params,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.core import (
    _allotment,
    _filled_contracts,
    _resting_contracts,
    _weight_of_rung,
)
from service.v33.params import DEFAULT_V33_PARAMS_PATH, V33ParamsInvalid
from service.v33.report import build_allocation_table
import service.run_v33 as RUN

CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000

BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"   # -> 79600
STK_SU = "KXBTCD-26SEP0416-T79699.99"   # -> 79700
B_SD = "KXBTC-RANGE-B79600"

# an illustrative NON-uniform weight vector (NOT a recommendation): 0 on the two shallowest rungs,
# ramping to 3 on the deepest -- "roughly 50% deep, 35% mid, 15% shallow, 0 shallowest" in spirit.
WEIGHTS = (0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3)   # sum = 20; max = 3 (<= hint 11)


def _top(bid: str, ask: str) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=False,
    )


def _params(weights=WEIGHTS, **over) -> V33Params:
    base = dict(tol=Decimal("0.01"), deb_ms=0,
                freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0)
    base.update(over)
    p = replace(load_v33_params(), **base)
    if weights is not None:
        p = replace(p, rung_lots=tuple(weights))
    return p


def _state(params: V33Params, *, shakedown: bool = False) -> V33State:
    return V33State.new(CLOSE, T, BK, params, shakedown=shakedown)


def _feed(params, st, event):
    st, a = decide_v33(params, st, event)
    st.check_invariants(params)
    return st, a


def _feed_all(params, st, events):
    acts: list = []
    for e in events:
        st, a = _feed(params, st, e)
        acts += a
    return st, acts


def _sd(ask: str) -> TopOfBook:
    return _top(str(Decimal(ask) - Decimal("0.01")), ask)


def _books(now: float, sd_ask: str = "0.76", *, b_bid: str = "0.35", su_bid: str = "0.36"):
    """Fresh in-window books. sd_ask=0.76 -> n_top=0.50, cap=0.64 (non-binding)."""
    return [
        BookUpdate(B_SD, _top(b_bid, str(Decimal(b_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SU, _top(su_bid, str(Decimal(su_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SD, _sd(sd_ask), now),
    ]


def _bring_up(params, st, now, sd_ask="0.76"):
    st, acts = _feed_all(params, st, _books(now, sd_ask))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    for a in places:
        st, _ = _feed(params, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    return st, places


def _by_price(st):
    return {o.price: o for o in st.ladder}


# =====================================================================================
# LOADER VALIDATION
# =====================================================================================
def _write_policy(tmp_path, **mut):
    with open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw.update(mut)
    q = tmp_path / "v33_params.json"
    q.write_text(json.dumps(raw), encoding="utf-8")
    return str(q)


def test_absent_rung_lots_defaults_to_uniform():
    p = load_v33_params()
    assert p.rung_lots == tuple([p.lots_per_rung] * p.rungs)
    assert _allotment(p) == p.rungs * p.lots_per_rung == 11


def test_present_rung_lots_loads(tmp_path):
    q = _write_policy(tmp_path, rung_lots=list(WEIGHTS))
    p = load_v33_params(q, expected_sha=None)
    assert p.rung_lots == WEIGHTS
    assert _allotment(p) == 20


def test_rung_lots_wrong_length_fails_closed(tmp_path):
    q = _write_policy(tmp_path, rung_lots=[1, 1, 1])          # != rungs (11)
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(q, expected_sha=None)


def test_rung_lots_negative_fails_closed(tmp_path):
    bad = list(WEIGHTS)
    bad[5] = -1
    q = _write_policy(tmp_path, rung_lots=bad)
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(q, expected_sha=None)


def test_rung_lots_all_zero_fails_closed(tmp_path):
    q = _write_policy(tmp_path, rung_lots=[0] * 11)
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(q, expected_sha=None)


def test_rung_lots_exceeds_hint_fails_closed(tmp_path):
    # a weight above max_contracts_per_order_hint (11) -> fail closed (the wing chunk cap can't cover it).
    bad = [1] * 11
    bad[10] = 12
    q = _write_policy(tmp_path, rung_lots=bad)
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(q, expected_sha=None)


def test_not_a_list_fails_closed(tmp_path):
    q = _write_policy(tmp_path, rung_lots=5)
    with pytest.raises(V33ParamsInvalid):
        load_v33_params(q, expected_sha=None)


def test_sha_unchanged_by_this_build():
    # the shipped JSON has NO rung_lots key, so its canonical sha is untouched by L5.
    p = load_v33_params()
    assert p.sha256 == "2e60980762ea6531b707c1c0bc93d69577fd3257295238e63f122d53afdd995e"


# =====================================================================================
# DEFAULT (uniform) == pre-L5 behaviour
# =====================================================================================
def test_uniform_default_places_11_single_lot_rungs():
    p = _params(weights=None)   # absent -> uniform
    st = _state(p)
    st, places = _bring_up(p, st, T - 600)
    assert len(places) == 11 and all(a.count == 1 for a in places)
    assert sorted((o.price for o in st.ladder), reverse=True)[0] == Decimal("0.50")
    assert all(o.count == 1 for o in st.ladder)
    assert _allotment(p) == 11


# =====================================================================================
# WEIGHTED LADDER placement
# =====================================================================================
def test_weighted_ladder_rests_right_counts_at_right_prices():
    p = _params()   # WEIGHTS
    st = _state(p)
    st, places = _bring_up(p, st, T - 600)
    bp = _by_price(st)
    # 0-weight rungs (k=0,1 -> 0.50, 0.49) are NEVER placed.
    assert Decimal("0.50") not in bp and Decimal("0.49") not in bp
    # placed rungs 0.48..0.40 carry their configured weights.
    expected = {Decimal("0.48"): 1, Decimal("0.47"): 1, Decimal("0.46"): 2, Decimal("0.45"): 2,
                Decimal("0.44"): 2, Decimal("0.43"): 3, Decimal("0.42"): 3, Decimal("0.41"): 3,
                Decimal("0.40"): 3}
    assert {pr: o.count for pr, o in bp.items()} == expected
    # the PLACE_REST actions carry the same counts (what the venue/executor sees).
    assert {a.price: a.count for a in places} == expected
    # exposure == the allotment exactly (nothing filled yet).
    assert _resting_contracts(st) == 20 == _allotment(p)


def test_zero_weight_rungs_do_not_stall_neighbour_convergence():
    p = _params()   # top two rungs weight 0
    st = _state(p)
    st, _ = _bring_up(p, st, T - 600)
    n0 = len(st.ladder)
    # a 1c up-move: the deepest order rolls to the new top target; the 0-weight slots stay empty and do
    # not become phantom targets that stall the roll.
    now = T - 590
    st, acts = _feed_all(p, st, _books(now, sd_ask="0.75"))   # n_top 0.50 -> 0.51
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1, "the roll must proceed across the 0-weight gap"
    # top two slots (0.51, 0.50) are still never placed after the roll acks.
    a = amends[0]
    st, _ = _feed(p, st, OrderAmended(a.order_id, a.updated_client_order_id, a.price, now + 0.001))
    bp = _by_price(st)
    assert Decimal("0.51") not in bp and Decimal("0.50") not in bp
    assert len(st.ladder) == n0


# =====================================================================================
# ROLL keeps count (the design decision)
# =====================================================================================
def test_roll_keeps_the_moving_order_count():
    p = _params()
    st = _state(p)
    st, _ = _bring_up(p, st, T - 600)
    mover = _by_price(st)[Decimal("0.40")]     # deepest, weight 3
    assert mover.count == 3
    now = T - 590
    st, acts = _feed_all(p, st, _books(now, sd_ask="0.75"))   # n_top up 1c -> 0.40 falls off, rolls up
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1
    a = amends[0]
    assert a.client_order_id == mover.client_order_id
    assert a.price == Decimal("0.49")          # new top target
    assert a.count == 3, "a roll re-prices but KEEPS the moving order's count (no re-size)"
    st, _ = _feed(p, st, OrderAmended(a.order_id, a.updated_client_order_id, a.price, now + 0.001))
    # smearing: the order sits at the new top (slot wants weight 1) still carrying 3.
    assert _by_price(st)[Decimal("0.49")].count == 3
    assert _resting_contracts(st) == 20 == _allotment(p)


# =====================================================================================
# PARTIAL FILL of a weight-3 rung: wing the filled lots, keep the remainder resting
# =====================================================================================
def test_partial_fill_wings_filled_and_keeps_remainder():
    p = _params()
    st = _state(p)
    st, _ = _bring_up(p, st, T - 600)
    rung = _by_price(st)[Decimal("0.43")]      # weight 3
    assert rung.count == 3
    now = T - 590
    # partial fill of 1 lot.
    st, fa = _feed(p, st, Fill(order_id=rung.order_id, client_order_id=rung.client_order_id,
                               count=Decimal(1), price=Decimal("0.43"), side="no", server_ts=now))
    # the remainder (2 lots) is still resting at 0.43.
    o = _by_price(st)[Decimal("0.43")]
    assert o.count == 2
    # exactly one rung fill booked, of the 1 filled lot.
    assert len(st.rest_fills) == 1 and st.rest_fills[0].count == 1
    assert st.rest_fills[0].weight == 3       # the configured weight travels on the fill
    # exposure conserved: 1 filled + 19 resting == 20 allotment.
    assert _filled_contracts(st) + _resting_contracts(st) == 20 == _allotment(p)
    # close the coalesce window and price the wings -> ONE take of 1 contract.
    st, wa1 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.20))
    st, wa2 = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 0.20))
    takes = [a for a in (wa1 + wa2) if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1 and takes[0].count == 1


def test_partial_remainder_is_never_rolled():
    p = _params()
    st = _state(p)
    st, _ = _bring_up(p, st, T - 600)
    deep = _by_price(st)[Decimal("0.40")]      # weight 3, deepest
    now = T - 590
    st, _ = _feed(p, st, Fill(order_id=deep.order_id, client_order_id=deep.client_order_id,
                              count=Decimal(1), price=Decimal("0.40"), side="no", server_ts=now))
    assert _by_price(st)[Decimal("0.40")].count == 2
    # n_top up 1c: 0.40 is now BELOW the placeable band (deepest target 0.41). Without the exclusion it
    # would be OUT and rolled; the partial remainder must instead simply stay put.
    st, acts = _feed_all(p, st, _books(now + 1, sd_ask="0.75"))
    amend_coids = {a.client_order_id for a in acts if a.kind == ActionKind.AMEND_REST}
    cancel_coids = {a.client_order_id for a in acts if a.kind == ActionKind.CANCEL_REST}
    assert deep.client_order_id not in amend_coids
    assert deep.client_order_id not in cancel_coids
    assert _by_price(st)[Decimal("0.40")].count == 2
    # exposure never exceeds the allotment (a new top create is budget-blocked: 20 already committed).
    assert _filled_contracts(st) + _resting_contracts(st) <= _allotment(p)


# =====================================================================================
# EXPOSURE INVARIANT under a partial fill + a subsequent roll
# =====================================================================================
def test_exposure_invariant_partial_then_roll():
    p = _params()
    st = _state(p)
    st, _ = _bring_up(p, st, T - 600)
    r = _by_price(st)[Decimal("0.44")]         # weight 2
    now = T - 590
    st, _ = _feed(p, st, Fill(order_id=r.order_id, client_order_id=r.client_order_id,
                              count=Decimal(1), price=Decimal("0.44"), side="no", server_ts=now))
    # drive a few n_top wobbles; check_invariants (incl. the contract exposure cap) runs after each event.
    for i, ask in enumerate(["0.75", "0.76", "0.77", "0.76"]):
        st, acts = _feed_all(p, st, _books(now + 2 + i, sd_ask=ask))
        for a in [x for x in acts if x.kind == ActionKind.AMEND_REST]:
            st, _ = _feed(p, st, OrderAmended(a.order_id, a.updated_client_order_id, a.price,
                                              now + 2 + i + 0.001))
        for a in [x for x in acts if x.kind == ActionKind.PLACE_REST]:
            st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID2-{a.client_order_id}", now + 2 + i))
    assert _filled_contracts(st) + _resting_contracts(st) <= _allotment(p)


# =====================================================================================
# DRY twin honours per-rung counts (the simulated fill path fills o.count == weight)
# =====================================================================================
class _J:
    def __init__(self):
        self.recs = []

    def append(self, k, o, t):
        self.recs.append((k, o))


def test_dry_sim_fills_full_rung_weight():
    p = _params()
    st = V33State.new(CLOSE, T, BK, p, shakedown=True)
    drv = RUN.V33Driver(p, st, _J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)
    now = T - 600
    for m, tb in ((B_SD, ("0.35", "0.36")), (STK_SU, ("0.36", "0.37")), (STK_SD, ("0.75", "0.76"))):
        drv.on_book_update(m, _top(*tb), now)
    # a YES print at 0.60 crosses the offers of rungs 0.48 (offer 0.52), 0.47 (0.53), .. up to 0.41
    # (offer 0.59) -- it does NOT reach 0.40 (offer 0.60 is not < 0.60). Each crossed rung fills its FULL
    # weight in one simulated Fill (the ideal-fill rule), so counts follow rung_lots.
    drv.on_trade(B_SD, {"taker_side": "yes", "yes_price_dollars": "0.595", "count_fp": "1.00"}, now + 1)
    fills = {rf.price: rf.count for rf in drv.state.rest_fills}
    assert fills[Decimal("0.48")] == 1 and fills[Decimal("0.46")] == 2 and fills[Decimal("0.41")] == 3
    # each dry_sim_fill journal record carries the full rung weight as its count.
    dry_counts = {o["n"]: o["count"] for k, o in drv.journal.recs if k == "dry_sim_fill"}
    assert dry_counts[Decimal("0.48")] == 1 and dry_counts[Decimal("0.41")] == 3


# =====================================================================================
# REPORT allocation table
# =====================================================================================
def test_allocation_table_by_placed_margin():
    # two DRY windows, each with rung fills at a couple of levels carrying weight + solved lock.
    rows = [
        {"schema": "v33_window", "close_time": "2026-09-28T20:00:00Z", "dry_sim": True,
         "effective_mode": "dry", "rung_fills": [
             {"E_rung": "0.14", "count": 3, "weight": 3, "lock_solved": "0.15"},
             {"E_rung": "0.14", "count": 3, "weight": 3, "lock_solved": "0.15"},
             {"E_rung": "0.05", "count": 1, "weight": 1, "lock_solved": "-0.02"},
         ]},
        {"schema": "v33_window", "close_time": "2026-09-29T20:00:00Z", "dry_sim": True,
         "effective_mode": "dry", "rung_fills": [
             {"E_rung": "0.14", "count": 3, "weight": 3, "lock_solved": "0.15"},
         ]},
    ]
    al = build_allocation_table(rows)
    dry = {d["level_c"]: d for d in al["dry"]}
    assert al["realised"] == []
    # 14c level: 3 fill events, 9 contracts, weight 3, all positive, total = 9 * 15c = 135c.
    d14 = dry[14]
    assert d14["fills"] == 3 and d14["contracts"] == 9 and d14["weight"] == 3
    assert d14["pct_positive"] == Decimal(100)
    assert d14["mean_lock_c"] == Decimal(15)
    assert d14["total_lock_c"] == Decimal(135)
    # 5c level: 1 contract, negative lock -> 0% positive.
    d5 = dry[5]
    assert d5["contracts"] == 1 and d5["weight"] == 1 and d5["pct_positive"] == Decimal(0)


def test_weight_helper_out_of_range():
    p = _params()
    assert _weight_of_rung(p, 0) == 0 and _weight_of_rung(p, 10) == 3
    assert _weight_of_rung(p, -1) == 0 and _weight_of_rung(p, 11) == 0
