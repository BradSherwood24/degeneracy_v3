"""V3.2 Phase-3 money math + settlement backfill. FAKES ONLY — no network, no proxy, no holdout read."""

from __future__ import annotations

from decimal import Decimal

from service.v32.ledger import (
    build_v32_backfill_row,
    build_v32_ledger_row,
    v32_pending_credit,
    v32_set_floor_dollars,
    v32_settlement_backfill_sweep,
)
from service.v32.params import load_v32_params

CLOSE = "2026-09-13T20:00:00Z"
B = "KXBTC-26SEP1316-B68200"
S_SD = "KXBTCD-26SEP1316-T68199.99"
S_SU = "KXBTCD-26SEP1316-T68299.99"

# a complete pin held to settlement (bucket-NO + YES@Sd + NO@Su)
COMPLETE_LEGS = [
    {"ticker": B, "side": "no", "count": 1},
    {"ticker": S_SD, "side": "yes", "count": 1},
    {"ticker": S_SU, "side": "no", "count": 1},
]
# BTC settles in-bucket: bucket result "yes" (range hit), Sd "yes" (>=Sd), Su "no" (<Su)
IN_BUCKET_RESULTS = {B: "yes", S_SD: "yes", S_SU: "no"}


def test_set_floor_dollars_geometry():
    assert v32_set_floor_dollars(3) == Decimal(2)   # all three legs -> $2 at every settlement
    assert v32_set_floor_dollars(2) == Decimal(1)   # any two -> $1 floor
    assert v32_set_floor_dollars(1) == Decimal(0)   # a lone leg is directional
    assert v32_set_floor_dollars(0) == Decimal(0)


def test_complete_pin_pays_two_at_every_settlement():
    from service.ledger import settlement_payoff
    # every settlement region pays exactly $2 for the complete pin
    assert settlement_payoff(COMPLETE_LEGS, {B: "yes", S_SD: "yes", S_SU: "no"}) == Decimal(2)  # in
    assert settlement_payoff(COMPLETE_LEGS, {B: "no", S_SD: "no", S_SU: "no"}) == Decimal(2)     # below
    assert settlement_payoff(COMPLETE_LEGS, {B: "no", S_SD: "yes", S_SU: "yes"}) == Decimal(2)   # above


def test_build_backfill_row_nets_floor():
    entry = {"close_time": CLOSE, "unsettled_legs": COMPLETE_LEGS, "realized_unsettled": True}
    row = build_v32_backfill_row(entry, IN_BUCKET_RESULTS, Decimal(2), Decimal(2), now=1.0)
    assert row["backfill_of"] == CLOSE
    assert row["settlement_payoff"] == "2"
    assert row["realized_delta"] == "0"     # complete set: payoff $2 nets the $2 floor -> +0 correction
    assert row["realized_unsettled"] is False


def test_settlement_sweep_books_complete_set_and_is_idempotent():
    rows = [
        build_v32_ledger_row(
            close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None,
            params=load_v32_params(), state=None, driver_counts={}, executor_counts={}, ws_counts={},
            strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
            strike_lag_seconds=None, bucket_lag_seconds=None, journal_path="x", record_count=1,
            stand_down_reason=None, now=1.0, armed=True, held_legs=COMPLETE_LEGS,
            realized_unsettled=True,
        ),
    ]
    fetch = lambda tk: IN_BUCKET_RESULTS.get(tk)
    out = v32_settlement_backfill_sweep(rows, fetch, now=2.0)
    assert len(out) == 1 and Decimal(out[0]["settlement_payoff"]) == Decimal(2)
    assert Decimal(out[0]["realized_delta"]) == Decimal(0)   # complete set: nets the $2 floor
    # idempotent: once the backfill row is present, a re-sweep produces nothing
    assert v32_settlement_backfill_sweep(rows + out, fetch, now=3.0) == []


def test_settlement_sweep_waits_on_unfinalized_leg():
    rows = [build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None,
        params=load_v32_params(), state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path="x", record_count=1,
        stand_down_reason=None, now=1.0, armed=True, held_legs=COMPLETE_LEGS, realized_unsettled=True)]
    # the Su strike is not settled yet -> the whole window waits (no backfill row)
    fetch = lambda tk: {B: "yes", S_SD: "yes"}.get(tk)  # S_SU -> None
    assert v32_settlement_backfill_sweep(rows, fetch, now=2.0) == []


def test_one_legged_set_backfill_corrects_by_result():
    from service.ledger import settlement_payoff
    # two legs held (bucket-NO + YES@Sd), floor $1. If BTC settles in-bucket, YES@Sd pays $1,
    # bucket-NO pays $0 -> payoff $1 == floor -> +0. If BTC settles ABOVE (>=Su), both pay -> $2.
    two_legs = [{"ticker": B, "side": "no", "count": 1}, {"ticker": S_SD, "side": "yes", "count": 1}]
    assert settlement_payoff(two_legs, {B: "yes", S_SD: "yes"}) == Decimal(1)   # in bucket
    assert settlement_payoff(two_legs, {B: "no", S_SD: "yes"}) == Decimal(2)    # above bucket
    floor = v32_set_floor_dollars(len(two_legs))
    row = build_v32_backfill_row({"close_time": CLOSE, "unsettled_legs": two_legs},
                                 {B: "no", S_SD: "yes"}, Decimal(2), floor, now=1.0)
    assert row["realized_delta"] == "1"   # $2 settlement nets the $1 floor -> +$1 correction


def test_pending_credit_optimistic_bound():
    rows = [
        {"close_time": CLOSE, "realized_unsettled": True, "unsettled_legs": COMPLETE_LEGS},  # floor 2 -> 0
        {"close_time": CLOSE[:11] + "19:00:00Z", "realized_unsettled": True,
         "unsettled_legs": COMPLETE_LEGS[:2]},  # 2 legs -> floor 1 -> credit +1
    ]
    assert v32_pending_credit(rows, "2026-09-13") == Decimal(1)


def test_pending_credit_lone_leg_bounded_at_one_not_two():
    # A lone bucket-NO pays AT MOST $1, so its optimistic pending credit is $1, not $2. (An overstated
    # $2 would credit the banded S4 with money that can never arrive and could mask a real day loss.)
    rows = [{"close_time": CLOSE, "realized_unsettled": True,
             "unsettled_legs": [{"ticker": B, "side": "no", "count": 1}]}]  # floor 0, max payoff 1
    assert v32_pending_credit(rows, "2026-09-13") == Decimal(1)


def test_ledger_row_carries_money_math_slots():
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="armed", effective_mode="armed", degrade=None,
        params=load_v32_params(), state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path="x", record_count=1,
        stand_down_reason=None, now=1.0, armed=True,
        fills=[{"leg": "rest", "price": Decimal("0.45")}],
        wing_fills=[{"leg": "wing", "side": "yes"}], held_legs=COMPLETE_LEGS,
        realized_lock=Decimal("0.10"), one_legged=False, realized_unsettled=True,
        realized_delta=Decimal("-0.90"), rests_placed=5, rests_rejected=1, wing_batches=1,
        exec_price_mismatches=[{"order_id": "o1"}],
    )
    assert row["armed"] is True
    assert row["fills"] and row["wing_fills"]
    assert row["realized_lock"] == "0.10"
    assert row["one_legged"] is False
    assert row["rests_placed"] == 5 and row["rests_rejected"] == 1
    assert row["realized_unsettled"] is True and row["unsettled_legs"] == COMPLETE_LEGS
    assert row["exec_price_mismatches"] == [{"order_id": "o1"}]


def test_dry_row_shape_unchanged_by_phase3_defaults():
    # a dry/shakedown row (no money-math kwargs) keeps the Phase-2 shape the report expects
    row = build_v32_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None,
        params=load_v32_params(), state=None, driver_counts={}, executor_counts={}, ws_counts={},
        strike_count=2, strike_generations=1, bucket_count=2, bucket_generations=1,
        strike_lag_seconds=None, bucket_lag_seconds=None, journal_path="x", record_count=1,
        stand_down_reason=None, now=1.0,
    )
    assert row["armed"] is False and row["fills"] == [] and row["settlement"] is None
    assert row["realized_unsettled"] is False and row["unsettled_legs"] == []
