"""V3.3 ledger money math (L2): rung/count-aware locks, the fee law x count, floor_booked, lock_solved
vs the integer label, dry_sim labelling, pending credit + settlement backfill (dry_sim excluded).
Constructs V33State records directly (no network, no core replay)."""

from __future__ import annotations

from dataclasses import replace as dr
from decimal import Decimal

from service._simlaw import fee as _fee
from service.v33 import V33State, load_v33_params
from service.v33.core import RungFill, WingBatch, WingLeg, lock_value
from service.v33.ledger import (
    _fee_total,
    build_v33_ledger_row,
    compute_ladder_money_math,
    v33_pending_credit,
    v33_set_floor_dollars,
    v33_settlement_backfill_sweep,
)

CLOSE = "2026-09-20T04:00:00Z"
CTS = 1789876800
B = "KXBTC-26SEP2000-B80450"
S_SD = "KXBTCD-26SEP2000-T80399.99"
S_SU = "KXBTCD-26SEP2000-T80499.99"
BK = {B: (80450.0, 80549.99)}
W = Decimal("1.4737")


def _filled_state(*, one_legged=False):
    """A ladder state with two coalesced rung fills (0.45, 0.44) and both wings filled."""
    p = load_v33_params()
    st = V33State.new(CLOSE, CTS, BK, p)
    rf0 = RungFill(rung=0, E_rung=Decimal("0.05"), price=Decimal("0.45"), count=1, server_ts=CTS - 500,
                   coid="v33-a", order_id="oa", W=W, n_top=Decimal("0.45"))
    rf1 = RungFill(rung=1, E_rung=Decimal("0.06"), price=Decimal("0.44"), count=1, server_ts=CTS - 500,
                   coid="v33-b", order_id="ob", W=W, n_top=Decimal("0.45"))
    batch = WingBatch(index=0, server_ts=CTS - 500, fills=(rf0, rf1), taken=True,
                      completed=not one_legged, one_legged=one_legged)
    yleg = WingLeg(S_SD, "yes", 2, Decimal("0.57"), "v33-wy",
                   status="filled", fill_price=Decimal("0.57"), fill_fee=_fee(Decimal("0.57")), batch=0)
    nleg = WingLeg(S_SU, "no", 2, Decimal("0.09"),
                   "v33-wn", status="filled" if not one_legged else "unfilled",
                   fill_price=Decimal("0.09") if not one_legged else None,
                   fill_fee=_fee(Decimal("0.09")) if not one_legged else None, batch=0)
    return dr(st, rest_fills=(rf0, rf1), wing_batches=(batch,), wing_legs=(yleg, nleg),
              rungs_filled=2, one_legged=one_legged, spot_Sd=80450, rest_bucket_Sd=80450,
              bucket_tickers={80450: B}, n_top=Decimal("0.45"))


# ---------------------------------------------------------------------------
# fee law x count
# ---------------------------------------------------------------------------
def test_fee_total_equals_per_contract_at_count_one():
    for p in ("0.45", "0.55", "0.09", "0.90"):
        assert _fee_total(Decimal(p), 1) == _fee(Decimal(p))


def test_fee_total_count_two_not_double_per_contract():
    p = Decimal("0.45")
    # the venue applies the $0.0001 ceiling ONCE to the whole fill, so 2 lots != 2x the rounded-up 1-lot.
    assert _fee_total(p, 2) != _fee(p) * 2
    # it is the ceiling of 0.07*p*(1-p)*2.
    import math
    from service._simlaw import fee_rate as FR
    raw = FR * p * (Decimal(1) - p) * Decimal(2) * Decimal(10000)
    assert _fee_total(p, 2) == Decimal(math.ceil(raw)) / Decimal(10000)


# ---------------------------------------------------------------------------
# money math
# ---------------------------------------------------------------------------
def test_money_math_no_fills_is_empty():
    st = V33State.new(CLOSE, CTS, BK, load_v33_params())
    m = compute_ladder_money_math(st, dry_sim=True)
    assert m["rung_fills"] == [] and m["floor_booked"] is None and m["realized_delta"] is None
    assert m["dry_sim"] is True and m["lots_filled"] == 0


def test_money_math_lock_solved_uses_W_at_fill_not_label():
    st = _filled_state()
    m = compute_ladder_money_math(st, dry_sim=True)
    assert len(m["rung_fills"]) == 2
    top = next(r for r in m["rung_fills"] if r["price"] == "0.45")
    # lock_solved = lock_value(price, W_at_fill) -- the price/W economic value, not E_rung (0.05).
    assert Decimal(top["lock_solved"]) == lock_value(Decimal("0.45"), W)
    assert Decimal(top["lock_solved"]) != Decimal(top["E_rung"])
    assert top["W_at_fill"] == str(W) and top["n_top"] == "0.45"


def test_money_math_realized_lock_uses_wings_paid():
    st = _filled_state()
    m = compute_ladder_money_math(st, dry_sim=True)
    w_paid = (Decimal("0.57") + _fee(Decimal("0.57"))) + (Decimal("0.09") + _fee(Decimal("0.09")))
    top = next(r for r in m["rung_fills"] if r["price"] == "0.45")
    assert Decimal(top["realized_lock"]) == lock_value(Decimal("0.45"), w_paid)


def test_money_math_floor_and_delta_count_aware():
    st = _filled_state()
    m = compute_ladder_money_math(st, dry_sim=True)
    # held = bucket-NO + 2 wings = 3 legs, at count 2 (the coalesced batch total) -> floor (3-1)*2 = 4.
    assert Decimal(m["floor_booked"]) == v33_set_floor_dollars(3, 2) == Decimal(4)
    # cost = rung rest cost (0.45+0.44, maker fee 0) + wing cost (price x count + fee_total per leg).
    cost = Decimal("0.45") + Decimal("0.44")
    cost += Decimal("0.57") * 2 + _fee_total(Decimal("0.57"), 2)
    cost += Decimal("0.09") * 2 + _fee_total(Decimal("0.09"), 2)
    assert Decimal(m["realized_delta"]) == Decimal(4) - cost
    assert m["dry_sim"] is True and m["realized_unsettled"] is False  # dry_sim never awaits settlement


def test_money_math_one_legged_no_realized_lock():
    st = _filled_state(one_legged=True)
    m = compute_ladder_money_math(st, dry_sim=False)
    assert m["one_legged"] is True
    assert all(r["realized_lock"] is None for r in m["rung_fills"])   # batch not completed
    assert m["realized_unsettled"] is True   # armed -> awaits settlement


# ---------------------------------------------------------------------------
# row + ladder summary
# ---------------------------------------------------------------------------
def test_row_carries_ladder_summary_and_dry_sim_flag():
    st = _filled_state()
    m = compute_ladder_money_math(st, dry_sim=True)
    row = build_v33_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None,
        params=load_v33_params(), state=st, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=2, bucket_count=1, journal_path=None, record_count=0, stand_down_reason=None,
        now=0.0, dry_sim=True, rung_fills=m["rung_fills"], wing_batch_sets=m["wing_batch_sets"],
        held_legs=m["held_legs"], floor_booked=m["floor_booked"], realized_delta=m["realized_delta"],
        realized_lock=m["realized_lock"], lots_filled=m["lots_filled"])
    assert row["roster"] == "DegeneracyV3_3" and row["dry_sim"] is True
    lad = row["ladder"]
    assert lad["rungs_filled"] == 2 and lad["contracts"] == 2
    assert lad["shallowest_margin_c"] == "5.00" and lad["deepest_margin_c"] == "6.00"
    assert Decimal(lad["ladder_lock"]) > 0


# ---------------------------------------------------------------------------
# pending credit + backfill exclude dry_sim
# ---------------------------------------------------------------------------
def _armed_row(unsettled=True, dry_sim=False):
    return {
        "close_time": CLOSE, "roster": "DegeneracyV3_3", "dry_sim": dry_sim,
        "realized_unsettled": unsettled,
        "wing_batch_sets": [{"index": 0, "fill_count": 2, "held_legs": 3, "completed": True}],
        "held_legs": [{"ticker": B, "side": "no", "count": 2},
                      {"ticker": S_SD, "side": "yes", "count": 2},
                      {"ticker": S_SU, "side": "no", "count": 2}],
        "unsettled_legs": [{"ticker": B, "side": "no", "count": 2},
                           {"ticker": S_SD, "side": "yes", "count": 2},
                           {"ticker": S_SU, "side": "no", "count": 2}],
    }


def test_pending_credit_counts_armed_not_dry_sim():
    rows = [_armed_row(dry_sim=False), _armed_row(dry_sim=True)]
    pess, opt = v33_pending_credit(rows, CLOSE[:10])
    # only the armed row: complete 3-leg pin at count 2 -> floor $4, upside $0 (min(3,2)*2 = 4).
    assert pess == Decimal(4) and opt == Decimal(4)


def test_floor_reconstructs_from_wing_batch_sets_when_no_explicit_floor():
    from service.v33.ledger import _v33_floor_booked_for_entry
    entry = {"wing_batch_sets": [{"held_legs": 3, "fill_count": 2},
                                 {"held_legs": 2, "fill_count": 1}]}
    # (3-1)*2 + (2-1)*1 = 4 + 1 = 5.
    assert _v33_floor_booked_for_entry(entry, []) == Decimal(5)


def test_explicit_floor_booked_wins_over_reconstruction():
    from service.v33.ledger import _v33_floor_booked_for_entry
    entry = {"floor_booked": "7", "wing_batch_sets": [{"held_legs": 3, "fill_count": 2}]}
    assert _v33_floor_booked_for_entry(entry, []) == Decimal(7)


def test_pending_credit_empty_when_no_unsettled():
    assert v33_pending_credit([_armed_row(unsettled=False)], CLOSE[:10]) == (Decimal(0), Decimal(0))


def test_backfill_note_records_source():
    armed = _armed_row(dry_sim=False)
    armed["floor_booked"] = "4"
    results = {B: "yes", S_SD: "yes", S_SU: "no"}
    bfs = v33_settlement_backfill_sweep([armed], lambda tk: results.get(tk), now=0.0)
    assert len(bfs) == 1 and "explicit" in bfs[0]["backfill_note"]


def test_backfill_skips_dry_sim_and_corrects_floor():
    armed = _armed_row(dry_sim=False)
    dry = _armed_row(dry_sim=True)
    dry["close_time"] = "2026-09-20T05:00:00Z"
    rows = [armed, dry]
    # a real pin: spot lands INSIDE bucket B (bucket resolves yes -> our NO leg loses), above Sd (YES@Sd
    # wins) and below Su (NO@Su wins) -> exactly 2 of 3 legs win -> payoff $2/contract = $4 for size 2.
    results = {B: "yes", S_SD: "yes", S_SU: "no"}
    bfs = v33_settlement_backfill_sweep(rows, lambda tk: results.get(tk), now=0.0)
    # ONLY the armed window gets a backfill row; the dry_sim row is skipped.
    assert len(bfs) == 1 and bfs[0]["backfill_of"] == CLOSE
    # complete set: settlement $4 nets the $4 floor -> corrected delta 0.
    assert Decimal(bfs[0]["realized_delta"]) == Decimal(0)
