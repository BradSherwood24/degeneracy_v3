"""V3.2 count-aware settlement backfill + S4 pending-credit band (Fable 2026-09-20).

Regression suite for the bucket-leg drop + count-blind floor bug: the window row's held legs listed
ONLY the two wings (the guaranteed bucket-NO leg was dropped because ``state.spot_Sd`` is nulled at
close), and both the settlement backfill and the S4 pending credit netted a count-blind $1 floor. On
the live ledger a complete size-2 pin's backfill over-corrected by $3 (floor $1 vs the $4 actually
booked) and its S4 pending credit read ($1, $2) against a $4 guarantee -> false-latch risk.

FAKES ONLY -- no network, no proxy, no sealed/holdout read. State is built via the core (the real
transition functions), money math via ``run_v32._compute_money_math``, then the ledger/backfill/
pending/S4 functions are exercised directly.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import service.run_v32 as R
from service._simlaw import fee as _sfee
from service.book import TopOfBook
from service.v32 import (
    BUY_YES,
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    V32Params,
    V32State,
    decide_v32,
    load_v32_params,
)
from service.v32.ledger import (
    build_v32_backfill_row,
    build_v32_ledger_row,
    v32_pending_credit,
    v32_set_floor_dollars,
    v32_settlement_backfill_sweep,
)
from service.v32.stops import v32_s4_decision

CLOSE = "2026-09-20T04:00:00Z"
T = 1_000_000

BK = {"KXBTC-RANGE-B80400": (80400.0, 80499.99), "KXBTC-RANGE-B80500": (80500.0, 80599.99)}
B_SD = "KXBTC-RANGE-B80400"
STK_SD = "KXBTCD-26SEP2000-T80399.99"   # -> 80400 (YES@Sd wing)
STK_SU = "KXBTCD-26SEP2000-T80499.99"   # -> 80500 (NO@Su wing)


def _params(**over) -> V32Params:
    return replace(load_v32_params(), **over)


def _top(bid: str, ask: str) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=False,
    )


def _fresh_books(now: float):
    return [
        BookUpdate(B_SD, _top("0.35", "0.36"), now),
        BookUpdate(STK_SU, _top("0.36", "0.37"), now),
        BookUpdate(STK_SD, _top("0.75", "0.76"), now),
    ]


def _feed(params, st, event):
    return decide_v32(params, st, event)


def _bring_up_live_rest(params, st, now):
    st2 = st
    acts: list = []
    for e in _fresh_books(now):
        st2, a = _feed(params, st2, e)
        acts += a
    place = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert place, "expected a PLACE_REST"
    coid = place[-1].client_order_id
    st2, _ = _feed(params, st2, OrderAck(coid, "OID1", now))
    return st2, coid, "OID1"


def _build_completed(params, *, rest_price, rest_lots, yes_px, no_px, complete=True):
    """Bring up a live rest, fill ``rest_lots`` at ``rest_price`` (one batch), and (optionally) fill
    both wings at ``yes_px``/``no_px``. Returns the completed state."""
    st = V32State.new(CLOSE, T, BK, params)
    now = T - 600
    st, coid, oid = _bring_up_live_rest(params, st, now)
    st, _ = _feed(params, st, Fill(oid, coid, Decimal(int(rest_lots)), Decimal(str(rest_price)),
                                   "no", now + 0.1))
    if complete:
        for lg in list(st.wing_legs):
            px = Decimal(str(yes_px)) if lg.side == BUY_YES else Decimal(str(no_px))
            st, _ = _feed(params, st, Fill("f" + lg.client_order_id, lg.client_order_id,
                                           Decimal(int(lg.count)), px, lg.side, now + 0.2))
    else:
        st, _ = _feed(params, st, ClockTick(server_ts=T - 0.5))  # cutoff -> wings one-legged
    return st


def _stub_executor(state):
    """Executor stub carrying the money-math ``fills`` list (rest maker leg fee 0, wing taker legs at
    the census per-contract fee). The rest fill's ``ticker`` is the bucket-NO ticker -- the fix reads
    it here to recover the bucket leg when ``spot_Sd`` was nulled at close."""
    fills: list = []
    for rf in state.rest_fills:
        fills.append({"leg": "rest", "side": "no", "ticker": B_SD, "price": rf.price,
                      "count": int(rf.count), "fee": Decimal(0)})
    for lg in state.wing_legs:
        if lg.status == "filled" and lg.fill_price is not None:
            fills.append({"leg": "wing", "side": lg.side, "ticker": lg.ticker,
                          "price": lg.fill_price, "count": int(lg.count), "fee": _sfee(lg.fill_price)})
    return SimpleNamespace(fills=fills, counts={})


def _row(state, params, money):
    return build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None, params=params,
        state=state, driver_counts={}, executor_counts={}, ws_counts={}, strike_count=2,
        strike_generations=1, bucket_count=2, bucket_generations=1, strike_lag_seconds=0.1,
        bucket_lag_seconds=0.1, journal_path=None, record_count=0, stand_down_reason=None, now=0.0,
        armed=True, **money,
    )


def _held_by_side(held, side):
    return next(h for h in held if h["side"] == side)


# ---------------------------------------------------------------------------
# (a) the live 04:00Z size-2 set reconstructed: bucket leg listed, floor $4, backfill nets to $0
# ---------------------------------------------------------------------------
def test_a_live_04z_size2_bucket_leg_listed_floor4_backfill_zero():
    p = _params(contracts=2)
    st = _build_completed(p, rest_price="0.27", rest_lots=2, yes_px="0.82", no_px="0.77")
    # simulate the reset-at-close that nulls spot_Sd (the root cause): the bucket leg must STILL list.
    st = replace(st, spot_Sd=None)
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    held = money["held_legs"]
    # bucket-NO leg recovered from the rest fill ticker (spot_Sd nulled), count = the batch fill_count
    bucket = next(h for h in held if h["ticker"] == B_SD)
    assert bucket == {"ticker": B_SD, "side": "no", "count": 2}
    assert len(held) == 3  # bucket-NO + two wings (previously the bug listed only 2 wings)
    assert money["floor_booked"] == Decimal("4")             # v32_set_floor_dollars(3, 2)
    assert money["realized_delta"] == Decimal("0.2345")      # 4 - (0.54 + 1.6607 + 1.5648)
    row = _row(st, p, money)
    assert row["floor_booked"] == "4"
    assert row["realized_unsettled"] is True
    assert len(row["unsettled_legs"]) == 3

    # in-bucket settlement: bucket-NO loses ($0), both wings win ($2 each) -> payoff $4 nets the $4 floor
    yes_tk = _held_by_side(held, "yes")["ticker"]
    results = {B_SD: "yes", yes_tk: "yes", STK_SU: "no"}
    out = v32_settlement_backfill_sweep([row], lambda tk: results[tk], now=2.0)
    assert len(out) == 1
    bf = out[0]
    assert Decimal(bf["settlement_payoff"]) == Decimal("4")   # wings 2+2, bucket-NO 0
    assert Decimal(bf["floor_netted"]) == Decimal("4")        # count-aware floor actually booked
    assert Decimal(bf["realized_delta"]) == Decimal("0")      # complete pin corrects by $0 at any size
    assert bf["legs_priced"] == 3
    assert bf["backfill_note"] == "floor_booked (explicit)"


# ---------------------------------------------------------------------------
# (b) size-1 out-of-bucket set -> complete pin still corrects by $0 (bucket-NO $1 + one wing $1)
# ---------------------------------------------------------------------------
def test_b_size1_out_of_bucket_backfill_zero():
    p = _params(contracts=1)
    st = _build_completed(p, rest_price="0.30", rest_lots=1, yes_px="0.80", no_px="0.80")
    st = replace(st, spot_Sd=None)
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["floor_booked"] == Decimal("2")   # v32_set_floor_dollars(3, 1)
    row = _row(st, p, money)
    held = money["held_legs"]
    yes_tk = _held_by_side(held, "yes")["ticker"]
    # below the bucket: bucket-NO wins ($1), YES@Sd loses, NO@Su wins ($1) -> payoff $2 nets $2 floor
    results = {B_SD: "no", yes_tk: "no", STK_SU: "no"}
    out = v32_settlement_backfill_sweep([row], lambda tk: results[tk], now=2.0)
    assert len(out) == 1
    assert Decimal(out[0]["settlement_payoff"]) == Decimal("2")
    assert Decimal(out[0]["realized_delta"]) == Decimal("0")


# ---------------------------------------------------------------------------
# (c) one-legged size-2 row (rest filled 2, no wings) -> floor 0, backfill = true payoff - 0
# ---------------------------------------------------------------------------
def test_c_one_legged_size2_floor_zero_backfill_is_payoff():
    p = _params(contracts=2)
    st = _build_completed(p, rest_price="0.27", rest_lots=2, yes_px="0", no_px="0", complete=False)
    st = replace(st, spot_Sd=None)
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    held = money["held_legs"]
    assert held == [{"ticker": B_SD, "side": "no", "count": 2}]   # bucket-NO only
    assert money["floor_booked"] == Decimal("0")                  # v32_set_floor_dollars(1, 2)
    row = _row(st, p, money)

    # bucket-NO wins (spot outside the bucket): payoff 2 x count = $2, floor 0 -> +$2 correction
    out_win = v32_settlement_backfill_sweep([row], lambda tk: "no", now=2.0)
    assert Decimal(out_win[0]["settlement_payoff"]) == Decimal("2")
    assert Decimal(out_win[0]["realized_delta"]) == Decimal("2")
    # bucket-NO loses (spot in the bucket): payoff $0, floor 0 -> $0 correction
    out_lose = v32_settlement_backfill_sweep([row], lambda tk: "yes", now=2.0)
    assert Decimal(out_lose[0]["settlement_payoff"]) == Decimal("0")
    assert Decimal(out_lose[0]["realized_delta"]) == Decimal("0")


# ---------------------------------------------------------------------------
# (d) partial 1-of-2 row, one complete batch -> floor $2, complete pin corrects by $0
# ---------------------------------------------------------------------------
def test_d_partial_one_complete_batch_floor2_correction_zero():
    p = _params(contracts=2)
    st = _build_completed(p, rest_price="0.27", rest_lots=1, yes_px="0.82", no_px="0.77")
    st = replace(st, spot_Sd=None)
    money = R._compute_money_math(st, _stub_executor(st), contracts=p.contracts)
    assert money["lots_filled"] == 1 and money["lots_unfilled_at_quote_end"] == 1
    assert money["floor_booked"] == Decimal("2")   # one complete batch: v32_set_floor_dollars(3, 1)
    row = _row(st, p, money)
    held = money["held_legs"]
    yes_tk = _held_by_side(held, "yes")["ticker"]
    results = {B_SD: "yes", yes_tk: "yes", STK_SU: "no"}
    out = v32_settlement_backfill_sweep([row], lambda tk: results[tk], now=2.0)
    assert Decimal(out[0]["settlement_payoff"]) == Decimal("2")
    assert Decimal(out[0]["floor_netted"]) == Decimal("2")
    assert Decimal(out[0]["realized_delta"]) == Decimal("0")


# ---------------------------------------------------------------------------
# (e) v32_pending_credit count + bucket aware
# ---------------------------------------------------------------------------
UTC = "2026-09-20"
CT = "2026-09-20T04:00:00Z"


def test_e_pending_credit_complete_size2_via_batch_sets():
    # 04:00Z-shaped row: unsettled_legs list the wings only, but wing_batch_sets carries held_legs 3
    # at fill_count 2 -> the true guaranteed floor is $4 (was scored $1 by the count-blind band).
    row = {
        "close_time": CT, "realized_unsettled": True,
        "unsettled_legs": [{"ticker": STK_SD, "side": "yes", "count": 2},
                           {"ticker": STK_SU, "side": "no", "count": 2}],
        "wing_batch_sets": [{"held_legs": 3, "fill_count": 2, "completed": True}],
    }
    assert v32_pending_credit([row], UTC) == (Decimal("4.00"), Decimal("4.00"))


def test_e_pending_credit_two_leg_size2_subset():
    row = {
        "close_time": CT, "realized_unsettled": True,
        "unsettled_legs": [{"ticker": B_SD, "side": "no", "count": 2},
                           {"ticker": STK_SD, "side": "yes", "count": 2}],
    }
    assert v32_pending_credit([row], UTC) == (Decimal("2.00"), Decimal("4.00"))


def test_e_pending_credit_lone_size2_leg():
    row = {
        "close_time": CT, "realized_unsettled": True,
        "unsettled_legs": [{"ticker": B_SD, "side": "no", "count": 2}],
    }
    assert v32_pending_credit([row], UTC) == (Decimal("0"), Decimal("2.00"))


def test_e_pending_credit_old_shape_size1_uses_batch_sets_when_present():
    # old-shape row: 2 wing legs listed, NO floor_booked, but wing_batch_sets present (held_legs 3).
    # was (1.00, 2.00) under the count-blind legs-only band; now (2.00, 2.00) via wing_batch_sets.
    row = {
        "close_time": CT, "realized_unsettled": True,
        "unsettled_legs": [{"ticker": STK_SD, "side": "yes", "count": 1},
                           {"ticker": STK_SU, "side": "no", "count": 1}],
        "wing_batch_sets": [{"held_legs": 3, "fill_count": 1, "completed": True}],
    }
    assert v32_pending_credit([row], UTC) == (Decimal("2.00"), Decimal("2.00"))


def test_e_pending_credit_truly_old_row_without_batch_sets_unchanged():
    # a genuinely old row (no wing_batch_sets, count-1 legs) keeps the pre-count band exactly.
    row = {
        "close_time": CT, "realized_unsettled": True,
        "unsettled_legs": [{"ticker": STK_SD, "side": "yes", "count": 1},
                           {"ticker": STK_SU, "side": "no", "count": 1}],
    }
    assert v32_pending_credit([row], UTC) == (Decimal("1"), Decimal("2"))


# ---------------------------------------------------------------------------
# (f) v32_s4_decision false-latch regression: the new band clears; the old band showed ~$2.77 loss
# ---------------------------------------------------------------------------
def test_f_s4_no_false_latch_size2_pending():
    start = Decimal("53.31")
    balance_now = start - Decimal("3.7655")   # a complete size-2 set's cash outlay, pending settle
    # NEW count-aware band: (4.00, 4.00) -> the guaranteed $4 credit nets the outlay, no loss.
    new = v32_s4_decision(start, balance_now, (Decimal("4.00"), Decimal("4.00")))
    assert new.kind != "latch"
    assert new.loss_pessimistic <= 0 and new.loss_optimistic <= 0
    # OLD count-blind band (1.00, 2.00): the guaranteed credit is understated to $1 -> ~$2.77 apparent
    # loss (the false-latch risk: a larger set / lower balance would cross the $3.00 S4 cap spuriously).
    old = v32_s4_decision(start, balance_now, (Decimal("1.00"), Decimal("2.00")))
    assert old.loss_pessimistic > Decimal("2.7")
    assert old.loss_pessimistic - new.loss_pessimistic == Decimal("3.00")   # exactly the $3 under-credit


# ---------------------------------------------------------------------------
# floor helper reconstruction (used by the backfill for old-shape rows / the report reconciliation)
# ---------------------------------------------------------------------------
def test_build_backfill_row_additive_keys_present():
    entry = {"close_time": CT, "unsettled_legs": [{"ticker": B_SD, "side": "no", "count": 2}],
             "realized_unsettled": True, "floor_booked": "0"}
    row = build_v32_backfill_row(entry, {B_SD: "no"}, Decimal("2"), Decimal("0"), now=1.0,
                                 legs_priced=1, backfill_note="floor_booked (explicit)")
    assert row["legs_priced"] == 1 and row["backfill_note"] == "floor_booked (explicit)"
    assert Decimal(row["realized_delta"]) == Decimal("2")
