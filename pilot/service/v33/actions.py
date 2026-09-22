"""actions.py — the decisions ``decide_v33`` emits.

The V3.3 ladder core emits EXACTLY the V3.2 action vocabulary — the roll is an ``AMEND_REST`` (of ONE
rung), a bucket change / quote-end / stand-down is ``CANCEL_REST`` (of every rung), a coalesced wing
pair is ``TAKE_WINGS``, and the WOULD_* shakedown twins are unchanged. So this module re-exports
``service.v32.actions`` unchanged (Phase L1, 2026-09-22), with ``V33Action`` aliased to ``V32Action``.
Kept as its own module so a future V3.3-only action can be added here without touching the frozen core.
"""

from __future__ import annotations

from service.v32.actions import (  # noqa: F401
    ActionKind,
    LegOrder,
    V32Action,
    twin_kind,
)

# V3.3 uses the V3.2 action record unchanged.
V33Action = V32Action

__all__ = [
    "ActionKind",
    "LegOrder",
    "V32Action",
    "V33Action",
    "twin_kind",
]
