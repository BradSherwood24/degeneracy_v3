"""GOLDEN PARITY for the V3.3 ladder core, against ONE real armed hour (close 2026-09-20T04:00:00Z).

The fixture ``fixtures/v33/golden_20260920T040000Z.json`` was extracted read-only from the LIVE-tree
journal ``journals_v32/20260920T040000Z.jsonl.gz`` using the SAME filter as the ideal study
``pilot/build/mc/v33_ladder_ideal.py``: the spot-bucket (B80450) YES trade PRINTS inside the quoting
window T-15..T-5, plus the sweep W and the per-rung n/W/lock the study's ``solve_n`` produced on THIS
window (``study_rungs``). 2026-09-20 is NOT in the 2026-08-20..29 holdout nor the 2026-08-02..18 seal.
The test opens ONLY the fixture (and, for cross-check, the persisted ``v33_ladder_ideal.json`` in the
LIVE tree) — no historical-data / sealed read at test time.

Goldens (PLAN_V33 sec 5, Phase L1 a-f):
  (a) a FULL-SWEEP replay fills all 11 rungs with the study's per-rung locks — with ONE documented,
      load-bearing divergence: the study SOLVES each rung independently and, at this W=1.4737, its two
      deepest rungs (E=14, E=15) COLLAPSE to the same n=0.36 (a duplicate price the ladder cannot rest).
      The core anchors at n_top and lays down K DISTINCT consecutive cents, so its 11th rung sits at a
      distinct 0.35 (lock +16.03c) instead of a second 0.36 (+15.01c). The core reproduces the study
      EXACTLY for the 10 shared rungs (0.45..0.36 -> +5.89c..+15.01c) and delivers a deeper 11th.
  (b) a shallow pump fills 3 rungs; the other 8 stay live.
  (c) a 1c W move rolls exactly ONE order (K-1 order_ids untouched).
  (d) a 2c move rolls two, strictly sequential (the second only after the first ack).
  (e) a bucket change cancels K and re-places K on the new ticker.
  (f) a fill during a roll books at the pre-roll price and does not double-place.

Goldens (c)-(f) here assert the mechanism on a controlled ladder (the same core, minimal synthetic
books); the richer parametric coverage lives in ``test_v33_core.py``. Golden (a) HOLDS the strike books
at the sweep-instant W=1.4737 (whole-cent books: yes_ask(Sd)=0.55, no_ask(Su)=0.90 -> W=1.4737 exactly)
and RELAXES the freshness bounds for the ~2 min print replay (the fixture carries prints, not a dense
book tape); freshness itself is covered by dedicated unit tests. The lock is print-independent
(lock = 2 - n - fee(n) - W), so a real sweep reproduces the study's locks regardless of which print
crosses each rung.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace as dreplace
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
    OrderAmended,
    OrderCancelled,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.core import lock_value

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "v33", "golden_20260920T040000Z.json")
# the persisted study output (read-only cross-check; may be absent in CI).
IDEAL_JSON = os.path.join(_HERE, "..", "build", "mc", "v33_ladder_ideal.json")

_ONE = Decimal(1)
_CENT = Decimal("0.01")

# whole-cent strike books reproducing the sweep W=1.4737 EXACTLY:
#   yes_ask(Sd) = 0.55, no_ask(Su) = 1 - yes_bid(Su) = 0.90
#   W = 0.55 + fee(0.55) + 0.90 + fee(0.90) = 0.55 + 0.0174 + 0.90 + 0.0063 = 1.4737
SWEEP_W = Decimal("1.4737")
BK = {"KXBTC-RANGE-B80400": (80400.0, 80499.99), "KXBTC-RANGE-B80500": (80500.0, 80599.99)}
B_SD = "KXBTC-RANGE-B80400"
STK_SD = "KXBTCD-26SEP2000-T80399.99"   # -> 80400
STK_SU = "KXBTCD-26SEP2000-T80499.99"   # -> 80500


def _load_fixture():
    if not os.path.exists(FIXTURE):
        pytest.skip("v33 golden fixture absent")
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _top(bid: str, ask: str) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=_ONE - ya, no_bid_size=Decimal(100), no_ask=_ONE - yb, no_ask_size=Decimal(100),
        suspect=False,
    )


def _sweep_books(now: float):
    # bucket yes_bid 0.10 -> cap = 0.89 (non-binding); strikes give W=1.4737, n_top solves to 0.45.
    return [
        BookUpdate(B_SD, _top("0.10", "0.11"), now),
        BookUpdate(STK_SU, _top("0.10", "0.11"), now),   # no_ask(Su) = 1 - 0.10 = 0.90
        BookUpdate(STK_SD, _top("0.54", "0.55"), now),   # yes_ask(Sd) = 0.55
    ]


def _sweep_params():
    # relax the freshness bounds for the multi-minute print replay (the fixture carries prints, not a
    # dense book tape); freshness has dedicated unit tests. n_top holds at the sweep-instant 0.45.
    return dreplace(
        load_v33_params(), tol=Decimal("0.01"), deb_ms=0,
        freshness_max_age_s=3600.0, bucket_freshness_max_age_s=3600.0,
    )


# ===========================================================================
# Fixture provenance
# ===========================================================================
def test_fixture_is_not_holdout_or_seal():
    fix = _load_fixture()
    assert fix["close_time"] == "2026-09-20T04:00:00Z"   # after 08-20..29 holdout and 08-02..18 seal


def test_sweep_books_reproduce_study_W():
    # the whole-cent books used by golden (a) yield exactly the study's sweep W.
    ya = Decimal("0.55")
    na = Decimal("0.90")
    W = ya + fee(ya) + na + fee(na)
    assert W == SWEEP_W == Decimal(str(_load_fixture()["sweep_W"]))


def test_study_rungs_match_persisted_ideal_json():
    # the fixture's study_rungs (re-derived here) equal the persisted v33_ladder_ideal.json rung_locks
    # for THIS window — the study's authoritative per-rung locks. (Skips if the LIVE artifact is absent.)
    fix = _load_fixture()
    if not os.path.exists(IDEAL_JSON):
        pytest.skip("persisted v33_ladder_ideal.json absent")
    with open(IDEAL_JSON, encoding="utf-8") as f:
        ideal = json.load(f)
    row = next((r for r in ideal if r.get("close") == "2026-09-20T04:00:00Z"), None)
    assert row is not None and row.get("rungs_filled") == list(range(5, 16))
    for E in range(5, 16):
        assert Decimal(str(fix["study_rungs"][str(E)]["lock"])) == Decimal(str(row["rung_locks"][str(E)]))


# ===========================================================================
# (a) full-sweep replay reproduces the study's per-rung locks
# ===========================================================================
def _bring_up_sweep_ladder(p, st, now):
    st, acts = _run(p, st, _sweep_books(now))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    for a in places:
        st, _ = _run1(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    return st, places


def _run1(p, st, ev):
    st, a = decide_v33(p, st, ev)
    st.check_invariants(p)
    return st, a


def _run(p, st, events):
    acts = []
    for e in events:
        st, a = _run1(p, st, e)
        acts += a
    return st, acts


def test_golden_a_full_sweep_fills_all_11_rungs_with_study_locks():
    fix = _load_fixture()
    p = _sweep_params()
    cts = fix["close_epoch"]
    st = V33State.new(fix["close_time"], cts, BK, p)
    t0 = cts - 900 + 1                                    # inside the quoting window
    st, places = _bring_up_sweep_ladder(p, st, t0)
    assert len(places) == 11 and st.n_top == Decimal("0.45")
    assert sorted((o.price for o in st.ladder), reverse=True) == [
        Decimal("0.45") - i * _CENT for i in range(11)
    ]

    # replay the REAL spot-bucket YES prints; synthesize a rung Fill for each rung a print crosses
    # (a YES taker at yp crosses our NO bid at n iff yp >= 1 - n). W is held at the sweep 1.4737.
    prints = sorted(fix["prints"], key=lambda r: r[0])
    for dt, yp_s, cnt_s in prints:
        ts = cts + float(dt)
        yp = Decimal(yp_s)
        for o in [x for x in st.ladder if yp >= _ONE - x.price]:
            st, _ = _run1(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", ts))
    assert st.rungs_filled == 11 and st.ladder == ()
    assert st.rest_allotment_done

    # realised per-rung locks (lock = 2 - n - fee(n) - W), keyed by the rung's resting n.
    locks = {f.price: lock_value(f.price, SWEEP_W) for f in st.rest_fills}
    assert set(locks) == {Decimal("0.45") - i * _CENT for i in range(11)}

    # the 10 SHARED rungs reproduce the study EXACTLY (n 0.45..0.36 -> E5..E14 locks).
    study = fix["study_rungs"]
    n_to_E = {Decimal(str(study[str(E)]["n"])): E for E in range(5, 15)}  # E5..E14, n 0.45..0.36
    for n, E in n_to_E.items():
        assert locks[n] == Decimal(str(study[str(E)]["lock"])), f"rung n={n} (E{E}) lock mismatch"
    # spot-check the endpoints.
    assert locks[Decimal("0.45")] == Decimal("0.0589")   # top rung E_min=5c
    assert locks[Decimal("0.36")] == Decimal("0.1501")   # study's collapsed E14==E15 price

    # the DOCUMENTED divergence: the core's distinct 11th rung at 0.35 (the study collapsed E15 onto
    # 0.36); its lock is 1c deeper than the study's +15.01c.
    assert locks[Decimal("0.35")] == Decimal("2") - Decimal("0.35") - fee(Decimal("0.35")) - SWEEP_W
    assert locks[Decimal("0.35")] == Decimal("0.1603")
    assert locks[Decimal("0.35")] - locks[Decimal("0.36")] == Decimal("0.0102")

    # wings: the total taken equals the total filled (11), across however many coalesced batches. Close
    # AFTER the last fill (the sweep is late in the window ~T-466s) and still inside it (T-400s).
    close_ts = cts - 400
    st, _ = _run1(p, st, ClockTick(close_ts))            # flush the last coalesce group + take wings
    total_taken = sum(b.total_count for b in st.wing_batches)
    assert total_taken == 11
    # the realistic sweep coalesced into 2 groups (a 3-rung print ~T-503s, then a 8-rung burst ~T-466s).
    assert len(st.wing_batches) == 2
    assert sorted(b.total_count for b in st.wing_batches) == [3, 8]


# ===========================================================================
# (b) shallow pump fills 3 rungs, 8 stay live
# ===========================================================================
def test_golden_b_shallow_pump_fills_three_rungs():
    fix = _load_fixture()
    p = _sweep_params()
    cts = fix["close_epoch"]
    st = V33State.new(fix["close_time"], cts, BK, p)
    t0 = cts - 900 + 1
    st, _ = _bring_up_sweep_ladder(p, st, t0)
    # a shallow pump that reaches only 0.57 crosses the top 3 rungs (offers 0.55/0.56/0.57 -> n 0.45/44/43).
    yp = Decimal("0.57")
    ts = cts - 500
    for o in [x for x in st.ladder if yp >= _ONE - x.price]:
        st, _ = _run1(p, st, Fill(o.order_id, o.client_order_id, Decimal(1), o.price, "no", ts))
    assert st.rungs_filled == 3
    assert len(st.ladder) == 8
    assert not st.rest_allotment_done
    assert sorted((o.price for o in st.ladder), reverse=True)[0] == Decimal("0.42")


# ===========================================================================
# (c) a 1c W move rolls exactly one order
# ===========================================================================
def test_golden_c_1c_move_rolls_one_order():
    p = _sweep_params()
    cts = 1_000_000
    st = V33State.new("C", cts, BK, p)
    t0 = cts - 600
    st, _ = _bring_up_sweep_ladder(p, st, t0)          # n_top 0.45
    ids_before = {o.price: o.order_id for o in st.ladder}
    # raise W by 1c-worth: yes_ask(Sd) 0.55 -> 0.56 lowers n_top to 0.44.
    st, acts = _run(p, st, [BookUpdate(STK_SU, _top("0.10", "0.11"), t0 + 1),
                            BookUpdate(STK_SD, _top("0.55", "0.56"), t0 + 1)])
    assert st.n_top == Decimal("0.44")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1
    am = amends[0]
    assert am.price == Decimal("0.34") and am.order_id == ids_before[Decimal("0.45")]
    st, _ = _run1(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, t0 + 1.1))
    ids_after = {o.price: o.order_id for o in st.ladder}
    untouched = sum(1 for pr in ids_after if pr in ids_before and ids_after[pr] == ids_before[pr])
    assert untouched == 10
    assert sorted((o.price for o in st.ladder), reverse=True) == [
        Decimal("0.44") - i * _CENT for i in range(11)
    ]


# ===========================================================================
# (d) a 2c move rolls two, strictly sequential
# ===========================================================================
def test_golden_d_2c_move_two_sequential_rolls():
    p = _sweep_params()
    cts = 1_000_000
    st = V33State.new("D", cts, BK, p)
    t0 = cts - 600
    st, _ = _bring_up_sweep_ladder(p, st, t0)          # n_top 0.45
    # jump n_top 0.45 -> 0.43 (yes_ask 0.55 -> 0.57) in one tick.
    st, acts = _run(p, st, [BookUpdate(STK_SU, _top("0.10", "0.11"), t0 + 1),
                            BookUpdate(STK_SD, _top("0.56", "0.57"), t0 + 1)])
    assert st.n_top == Decimal("0.43")
    amends = [a for a in acts if a.kind == ActionKind.AMEND_REST]
    assert len(amends) == 1 and amends[0].price == Decimal("0.34")   # move top(0.45) to bottom(0.35)-1c
    # a further move while the first roll is in flight -> NO second amend (queued).
    st, held = _run(p, st, [BookUpdate(STK_SD, _top("0.56", "0.57"), t0 + 1.05)])
    assert not [a for a in held if a.kind == ActionKind.AMEND_REST]
    am1 = amends[0]
    st, acts2 = _run1(p, st, OrderAmended(am1.order_id, am1.updated_client_order_id, am1.price,
                                          t0 + 1.1))
    amends2 = [a for a in acts2 if a.kind == ActionKind.AMEND_REST]
    assert len(amends2) == 1 and amends2[0].price == Decimal("0.33")   # the second roll, after the ack
    am2 = amends2[0]
    st, _ = _run1(p, st, OrderAmended(am2.order_id, am2.updated_client_order_id, am2.price, t0 + 1.2))
    assert st.roll_pending is None and st.roll_count == 2
    assert sorted((o.price for o in st.ladder), reverse=True) == [
        Decimal("0.43") - i * _CENT for i in range(11)
    ]


# ===========================================================================
# (e) bucket change cancels K and re-places K on the new ticker
# ===========================================================================
def test_golden_e_bucket_change_replaces_k():
    p = _sweep_params()
    cts = 1_000_000
    st = V33State.new("E", cts, BK, p)
    t0 = cts - 600
    st, _ = _bring_up_sweep_ladder(p, st, t0)          # ladder on bucket 80400
    ids = [o.order_id for o in st.ladder]
    assert st.rest_bucket_Sd == 80400
    # 80500 becomes the higher-mid spot; bring up its far strike (80600) and its bucket book.
    st, acts = _run(p, st, [
        BookUpdate(STK_SU, _top("0.10", "0.11"), t0 + 1),
        BookUpdate("KXBTCD-26SEP2000-T80599.99", _top("0.20", "0.21"), t0 + 1),
        BookUpdate("KXBTC-RANGE-B80500", _top("0.55", "0.57"), t0 + 1),
    ])
    cancels = [a for a in acts if a.kind == ActionKind.CANCEL_REST]
    assert len(cancels) == 11 and st.ladder == () and st.outstanding_cancels == 11
    for i, oid in enumerate(ids):
        st, acts = _run1(p, st, OrderCancelled(oid, t0 + 2 + i * 0.001))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert len(places) == 11 and all(o.bucket_Sd == 80500 for o in st.ladder)
    assert st.rest_bucket_Sd == 80500


# ===========================================================================
# (f) a fill during a roll books at the pre-roll price and does not double-place
# ===========================================================================
def test_golden_f_fill_during_roll():
    p = _sweep_params()
    cts = 1_000_000
    st = V33State.new("F", cts, BK, p)
    t0 = cts - 600
    st, _ = _bring_up_sweep_ladder(p, st, t0)          # n_top 0.45
    top = max(st.ladder, key=lambda o: o.price)        # 0.45
    st, acts = _run(p, st, [BookUpdate(STK_SU, _top("0.10", "0.11"), t0 + 1),
                            BookUpdate(STK_SD, _top("0.55", "0.56"), t0 + 1)])
    am = [a for a in acts if a.kind == ActionKind.AMEND_REST][0]
    assert st.roll_pending is not None and am.order_id == top.order_id
    # the rolling order fills at its PRE-roll resting price 0.45 before the amend acks.
    st, _ = _run1(p, st, Fill(top.order_id, top.client_order_id, Decimal(1), None, "no", t0 + 1.05))
    assert st.rungs_filled == 1 and st.rest_fills[-1].price == Decimal("0.45")
    assert top.price not in [o.price for o in st.ladder]
    # the late OrderAmended for the filled order does NOT double-place.
    st, acts = _run1(p, st, OrderAmended(am.order_id, am.updated_client_order_id, am.price, t0 + 1.1))
    assert not [a for a in acts if a.kind in (ActionKind.PLACE_REST, ActionKind.AMEND_REST)]
    assert st.roll_pending is None
