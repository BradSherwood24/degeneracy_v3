"""Policy loader — the FROZEN pilot roster as sha-pinned config.

Loads ``pilot/policy/policy_params.json`` into a ``PolicyParams`` with money as Decimal, and
computes the sha256 of the CANONICAL JSON (``json.dumps(sort_keys=True, separators=(",",":"))``)
so whitespace/key-order in the file cannot change the identity. Fail-closed (commission /
falsifier S5): if the caller passes an ``expected_sha`` and it does not match the loaded
canonical sha, the loader RAISES — the service refuses to run on a policy sha mismatch. Same
discipline as the sim's census-sha refusal (tape_sim.EVCurve).

The roster encoded here is the PILOT roster (differs from the sealed-read policy):
  * sub-$1 flip in ALL quintiles (first moment flip-C < $1.00, fees in);
  * Q1-strangle ONLY in quintile 0 (ev >= 5c on the strangle);
  * NO flip-EV anywhere.

Imbalance bounds (Phase 3 consumes them from THIS same frozen config): the sub-$1 pair-cost
ceiling ($1.0320 = $1.00 + train sub-$1 mean return), max 5 retries/side, no rebalance with
< 3s to settle, no orders at all with < 1s to settle.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Policy source identifiers (used across signal.py / parity.py / journals).
SUB_DOLLAR_FLIP = "sub$1-flip"
Q1_STRANGLE = "Q1-strangle"

_POLICY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy")
DEFAULT_POLICY_PATH = os.path.join(_POLICY_DIR, "policy_params.json")

# The canonical sha of the frozen roster shipped in policy/policy_params.json. Pinned so a
# caller can pass it as expected_sha without re-reading the file, and so an accidental edit to
# the JSON is caught by the default self-check below.
FROZEN_POLICY_SHA256 = "1b01fd98e1c76748261fbe80f961d9ae8a55853c7807de71af508eece8203656"


class PolicyShaMismatch(Exception):
    """Raised when the loaded policy's canonical sha != the expected sha (fail-closed / S5)."""


@dataclass(frozen=True)
class ImbalanceBounds:
    """Phase-3 imbalance-protocol bounds, carried frozen from the same config."""

    pair_cost_ceiling_sub1: Decimal
    max_retries_per_side: int
    no_rebalance_after_s_to_settle: int
    no_orders_after_s_to_settle: int


@dataclass(frozen=True)
class PolicyParams:
    """The frozen pilot roster. Money is Decimal; times are plain numbers (seconds)."""

    roster_name: str
    sub_dollar_C_max: Decimal
    q1_strangle_ev_min: Decimal
    freshness_max_leg_age_s: float
    staleness_s: int
    quintile_routing: dict[int, tuple[str, ...]]
    imbalance: ImbalanceBounds
    sha256: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # Convenience: the no-orders cutoff (seconds-to-settle floor) lives in imbalance bounds but is
    # consulted by signal.py on the hot path, so surface it directly.
    @property
    def no_orders_after_s_to_settle(self) -> int:
        return self.imbalance.no_orders_after_s_to_settle

    def sources_for_quintile(self, quintile: int) -> tuple[str, ...]:
        """The policy sources active in ``quintile`` per the frozen routing (empty tuple if the
        quintile is unknown — fail closed: an out-of-range quintile routes to no sources)."""
        return self.quintile_routing.get(quintile, ())


def canonical_sha256(obj: dict[str, Any]) -> str:
    """sha256 of the canonical JSON encoding (sorted keys, tight separators, utf-8)."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def _parse_routing(raw: dict[str, Any]) -> dict[int, tuple[str, ...]]:
    """Turn {"q0": [...], ...} into {0: (...), ...}. Fail-closed on a malformed key."""
    out: dict[int, tuple[str, ...]] = {}
    for key, sources in raw.items():
        if not (isinstance(key, str) and key.startswith("q") and key[1:].isdigit()):
            raise ValueError(f"malformed quintile_routing key {key!r} (expected 'qN')")
        out[int(key[1:])] = tuple(sources)
    return out


def load_policy(
    path: str = DEFAULT_POLICY_PATH,
    expected_sha: str | None = FROZEN_POLICY_SHA256,
) -> PolicyParams:
    """Load + freeze the pilot roster.

    ``expected_sha`` defaults to the pinned FROZEN_POLICY_SHA256, so a plain ``load_policy()``
    self-verifies the shipped file and refuses any drift. Pass ``expected_sha=None`` to load
    without the check (only the tooling that INTENDS to re-pin a new roster does this); pass a
    different sha to enforce a specific expected roster. A mismatch raises PolicyShaMismatch.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sha = canonical_sha256(raw)
    if expected_sha is not None and sha != expected_sha:
        raise PolicyShaMismatch(
            f"policy sha mismatch for {path}: got {sha}, expected {expected_sha} "
            f"(refusing to load a policy whose canonical sha does not match — falsifier S5)"
        )
    imb_raw = raw["imbalance"]
    imbalance = ImbalanceBounds(
        pair_cost_ceiling_sub1=Decimal(str(imb_raw["pair_cost_ceiling_sub1"])),
        max_retries_per_side=int(imb_raw["max_retries_per_side"]),
        no_rebalance_after_s_to_settle=int(imb_raw["no_rebalance_after_s_to_settle"]),
        no_orders_after_s_to_settle=int(imb_raw["no_orders_after_s_to_settle"]),
    )
    return PolicyParams(
        roster_name=str(raw.get("roster_name", "")),
        sub_dollar_C_max=Decimal(str(raw["sub_dollar_C_max"])),
        q1_strangle_ev_min=Decimal(str(raw["q1_strangle_ev_min"])),
        freshness_max_leg_age_s=float(raw["freshness_max_leg_age_s"]),
        staleness_s=int(raw["staleness_s"]),
        quintile_routing=_parse_routing(raw["quintile_routing"]),
        imbalance=imbalance,
        sha256=sha,
        raw=raw,
    )
