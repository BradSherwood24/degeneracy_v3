"""service.v33 — the pure decision core for pilot V3.3 (rolling-ladder spot-bucket pump-fader).

Phase L1 ships the ladder core only: ``params`` (frozen policy + sha), ``events`` / ``actions``
(re-exports of the V3.2 vocabulary — the ladder needs no new event or action kind), and ``core``
(the pure ``decide_v33(params, state, event) -> (state, actions)``). Executor, ledger, report,
shadow and ceremony are Phases L2-L3. Nothing here reads a clock, the network, or disk.

Forked from ``service.v32`` (Brad's go 2026-09-22); the UNCHANGED pure law is imported read-only from
``service.v32.core`` so the two cores cannot drift.
"""

from __future__ import annotations

from service.v33.actions import ActionKind, LegOrder, V33Action, twin_kind
from service.v33.core import (
    BUY_NO,
    BUY_YES,
    CoalesceGroup,
    RestOrder,
    RollPending,
    RungFill,
    ShadowFill,
    ShadowSub,
    V33State,
    WingBatch,
    WingLeg,
    decide_v33,
    lock_value,
    solve_n,
    wing_cost,
)
from service.v33.events import (
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderAmended,
    OrderCancelled,
    Trade,
    classify_ticker,
    parse_bucket_ticker,
    parse_strike_ticker,
)
from service.v33.params import (
    FROZEN_V33_PARAMS_SHA256,
    V33Params,
    V33ParamsInvalid,
    V33ParamsShaMismatch,
    canonical_sha256,
    load_v33_params,
)

__all__ = [
    "ActionKind",
    "LegOrder",
    "V33Action",
    "twin_kind",
    "BookUpdate",
    "ClockTick",
    "Fill",
    "OrderAck",
    "OrderAmended",
    "OrderCancelled",
    "Trade",
    "classify_ticker",
    "parse_bucket_ticker",
    "parse_strike_ticker",
    "FROZEN_V33_PARAMS_SHA256",
    "V33Params",
    "V33ParamsInvalid",
    "V33ParamsShaMismatch",
    "canonical_sha256",
    "load_v33_params",
    "BUY_NO",
    "BUY_YES",
    "CoalesceGroup",
    "RestOrder",
    "RollPending",
    "RungFill",
    "ShadowFill",
    "ShadowSub",
    "V33State",
    "WingBatch",
    "WingLeg",
    "decide_v33",
    "lock_value",
    "solve_n",
    "wing_cost",
]
