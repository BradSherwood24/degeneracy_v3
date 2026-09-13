"""actions.py — the decisions ``decide_v32`` emits (pure data; the driver/executor act on them).

WOULD_* twins: in shakedown the core emits WOULD_PLACE_REST / WOULD_CANCEL_REST / WOULD_TAKE_WINGS
with the SAME payloads instead of the order-emitting kinds, so a dry/shakedown run logs every
would-be create/cancel/take without any order existing (mirrors box.py's WOULD_FIRE vs FIRE). A
RETRY_WING in shakedown also downgrades to WOULD_TAKE_WINGS (a retry IS a wing take). STAND_DOWN is
order-free and has no twin.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal


class ActionKind(str, enum.Enum):
    """The kind of a ``V32Action``. String-valued so it journals/compares as a plain string."""

    PLACE_REST = "PLACE_REST"
    CANCEL_REST = "CANCEL_REST"
    TAKE_WINGS = "TAKE_WINGS"
    RETRY_WING = "RETRY_WING"
    STAND_DOWN = "STAND_DOWN"
    # shakedown twins
    WOULD_PLACE_REST = "WOULD_PLACE_REST"
    WOULD_CANCEL_REST = "WOULD_CANCEL_REST"
    WOULD_TAKE_WINGS = "WOULD_TAKE_WINGS"


# Order-emitting kind -> its shakedown twin (STAND_DOWN is order-free: no twin).
_WOULD_TWIN: dict[ActionKind, ActionKind] = {
    ActionKind.PLACE_REST: ActionKind.WOULD_PLACE_REST,
    ActionKind.CANCEL_REST: ActionKind.WOULD_CANCEL_REST,
    ActionKind.TAKE_WINGS: ActionKind.WOULD_TAKE_WINGS,
    ActionKind.RETRY_WING: ActionKind.WOULD_TAKE_WINGS,
}


def twin_kind(kind: ActionKind, shakedown: bool) -> ActionKind:
    """The kind to emit given the mode: the WOULD_* twin in shakedown, else the kind itself."""
    if not shakedown:
        return kind
    return _WOULD_TWIN.get(kind, kind)


@dataclass(frozen=True)
class LegOrder:
    """One taker leg of the pin completion: buy ``count`` of ``side`` on ``ticker`` at ``limit``
    (= observed ask + wing_margin, capped at the 0.99 buy ceiling)."""

    ticker: str
    side: str            # "yes" (low leg @ Sd) or "no" (high leg @ Su)
    action: str          # always "buy"
    count: int
    limit: Decimal


@dataclass(frozen=True)
class V32Action:
    """A decision emitted by ``decide_v32``. ``kind`` is an ActionKind (or its WOULD_* twin).

    Fields used per kind:
      * PLACE_REST / WOULD_PLACE_REST: ticker, side="no", action="buy", count, price=n,
        expiration_epoch, client_order_id.
      * CANCEL_REST / WOULD_CANCEL_REST: order_id (if known) or client_order_id.
      * TAKE_WINGS / WOULD_TAKE_WINGS: legs (2), count.
      * RETRY_WING: legs (1), count.
      * STAND_DOWN: reason.
    """

    kind: ActionKind
    ticker: str | None = None
    side: str | None = None
    action: str | None = None
    count: int = 0
    price: Decimal | None = None
    expiration_epoch: int | None = None
    client_order_id: str | None = None
    order_id: str | None = None
    legs: tuple[LegOrder, ...] = ()
    reason: str | None = None
    # informational (carried for the journal/report; never load-bearing for execution)
    lock: Decimal | None = field(default=None)
