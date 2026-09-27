"""actions.py -- the decisions ``decide_v33`` emits.

The V3.3 ladder core emits the V3.2 action vocabulary -- the roll is an ``AMEND_REST`` (of ONE rung), a
bucket change / quote-end / stand-down is ``CANCEL_REST`` (of every rung), a coalesced wing pair is
``TAKE_WINGS``, and the WOULD_* shakedown twins are unchanged. So this module re-exports
``service.v32.actions`` (Phase L1, 2026-09-22), with ``V33Action`` aliased to ``V32Action``.

PRINT-THROUGH WINGS (2026-09-26, Brad's idea) adds TWO V3.3-only action kinds for the stall policy of the
early-hedge trigger (the pre-emptive take itself reuses ``TAKE_WINGS``):

  * ``TAKE_BUCKET_NO`` -- the ``complete`` stall branch: buy the bucket-NO ourselves as an IOC taker at the
    current NO ask, completing the set the pre-taken wings are waiting on when a print-through trigger's
    rung never filled but the lock still clears the floor.
  * ``UNWIND_WINGS`` -- the ``unwind`` stall branch (and the partial-fill fail-closed branch): sell the
    pre-taken wings back IOC at the current bids and journal the round-trip cost.

These are frozen-V3.2-safe: they live in a SEPARATE enum (``V33ActionKind``) the V3.3 core/executor
understand, never touching the V3.2 ``ActionKind``. ``twin_kind`` is wrapped so the WOULD_* twins work for
both enums. ``V33Action`` is still ``V32Action`` (its ``kind`` field stores any string-enum value).
"""

from __future__ import annotations

import enum

from service.v32.actions import (  # noqa: F401
    ActionKind,
    LegOrder,
    V32Action,
    twin_kind as _v32_twin_kind,
)

# V3.3 uses the V3.2 action record unchanged.
V33Action = V32Action


class V33ActionKind(str, enum.Enum):
    """V3.3-only action kinds (print-through stall policy). String-valued so they journal/compare as
    plain strings, exactly like ``ActionKind``; they are DISTINCT values, so a V3.2 dispatch never
    matches one (the V3.3 executor intercepts them before delegating to the inherited dispatch)."""

    TAKE_BUCKET_NO = "TAKE_BUCKET_NO"          # complete: buy bucket-NO IOC taker to finish the set
    UNWIND_WINGS = "UNWIND_WINGS"              # unwind / fail-closed: sell the pre-taken wings back IOC
    WOULD_TAKE_BUCKET_NO = "WOULD_TAKE_BUCKET_NO"
    WOULD_UNWIND_WINGS = "WOULD_UNWIND_WINGS"


_V33_WOULD_TWIN: dict[V33ActionKind, V33ActionKind] = {
    V33ActionKind.TAKE_BUCKET_NO: V33ActionKind.WOULD_TAKE_BUCKET_NO,
    V33ActionKind.UNWIND_WINGS: V33ActionKind.WOULD_UNWIND_WINGS,
}


def twin_kind(kind, shakedown: bool):
    """The kind to emit given the mode: the WOULD_* twin in shakedown, else the kind itself. Handles both
    the inherited ``ActionKind`` (via the V3.2 map) and the V3.3-only ``V33ActionKind``."""
    if not shakedown:
        return kind
    if isinstance(kind, V33ActionKind):
        return _V33_WOULD_TWIN.get(kind, kind)
    return _v32_twin_kind(kind, shakedown)


__all__ = [
    "ActionKind",
    "V33ActionKind",
    "LegOrder",
    "V32Action",
    "V33Action",
    "twin_kind",
]
