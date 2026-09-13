"""service.v32 — the pure decision core for pilot V3.2 (continuous-requote spot-bucket pump-fader).

Phase 1 ships the law only: ``params`` (frozen policy + sha), ``events`` (the event vocabulary +
ticker classifiers), ``actions`` (the emitted decisions + WOULD_* twins), and ``core`` (the pure
``decide_v32(params, state, event) -> (state, actions)``). Process spine, executor, ledger, and
ceremony are Phases 2-4. Nothing here reads a clock, the network, or disk.
"""

from __future__ import annotations

from service.v32.actions import ActionKind, LegOrder, V32Action, twin_kind
from service.v32.core import (
    BUY_NO,
    BUY_YES,
    RestFill,
    RestOrder,
    ShadowFill,
    ShadowSub,
    V32State,
    WingLeg,
    decide_v32,
    lock_value,
    solve_n,
    wing_cost,
)
from service.v32.events import (
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    Trade,
    classify_ticker,
    parse_bucket_ticker,
    parse_strike_ticker,
)
from service.v32.params import (
    FROZEN_V32_PARAMS_SHA256,
    V32Params,
    V32ParamsInvalid,
    V32ParamsShaMismatch,
    canonical_sha256,
    load_v32_params,
)

__all__ = [
    "ActionKind",
    "LegOrder",
    "V32Action",
    "twin_kind",
    "BookUpdate",
    "ClockTick",
    "Fill",
    "OrderAck",
    "OrderCancelled",
    "Trade",
    "classify_ticker",
    "parse_bucket_ticker",
    "parse_strike_ticker",
    "FROZEN_V32_PARAMS_SHA256",
    "V32Params",
    "V32ParamsInvalid",
    "V32ParamsShaMismatch",
    "canonical_sha256",
    "load_v32_params",
    "BUY_NO",
    "BUY_YES",
    "RestFill",
    "RestOrder",
    "ShadowFill",
    "ShadowSub",
    "V32State",
    "WingLeg",
    "decide_v32",
    "lock_value",
    "solve_n",
    "wing_cost",
]
