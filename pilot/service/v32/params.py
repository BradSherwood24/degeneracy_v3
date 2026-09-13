"""params.py — the frozen V3.2 policy (continuous-requote spot-bucket pump-fader).

House law (same discipline as ``box.load_box_policy`` / ``box.canonical_sha256``): the policy is a
JSON file whose *canonical* sha256 is PINNED in code. A plain ``load_v32_params()`` self-verifies the
shipped file and refuses any drift (fail-closed / S5). Money values are Decimal (parsed from JSON
strings so the decimal is exact); times/counts are plain ints/floats. A missing required key is a
KeyError (never defaulted) so a truncated policy fails closed rather than running on silent defaults.

The parameters here are INPUTS to the pure core (``decide_v32``): the requote policy (``tol``,
``deb_ms``) is provisional per PLAN_V32 "Requote policy" (still RESULTS_PLACEHOLDER); nothing in the
Phase-1 core depends on the final tuned numbers.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# The shipped policy file (repo-relative to pilot/). Callers may pass an explicit path.
DEFAULT_V32_PARAMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "policy",
    "v32_params.json",
)

# The canonical sha256 of the shipped policy/v32_params.json (sha of json.dumps(sort_keys=True,
# separators=(",",":")).encode("utf-8")). Recompute + re-pin only when INTENTIONALLY re-freezing.
FROZEN_V32_PARAMS_SHA256 = "69646917691ac1995b82f75bb0de2ca3796ee52b068ae283b1ee9df1455ceed1"


class V32ParamsShaMismatch(Exception):
    """Raised when the loaded v32 policy's canonical sha != the expected sha (fail-closed)."""


def canonical_sha256(obj: dict[str, Any]) -> str:
    """sha256 of the canonical JSON encoding (sorted keys, tight separators, utf-8).

    Byte-identical scheme to ``service.box.canonical_sha256`` (a unit test pins the agreement) so
    the two pilots share one sha convention.
    """
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


@dataclass(frozen=True)
class V32Params:
    """The frozen V3.2 policy. Money is Decimal; times/counts are ints/floats.

    Fields:
      * ``E``            edge parameter: rest at the largest whole-cent n with n+fee(n) <= 2 - E - W.
      * ``tol``          requote tolerance: replace only when |n_desired - n_resting| >= tol.
      * ``deb_ms``       requote debounce: and at least this many ms since the last replace.
      * ``quote_start_s``/``quote_end_s`` the quoting window T-900..T-300 (place only inside it).
      * ``contracts``    order size (1; proxy cap 2 stands).
      * ``wing_margin``  taker-completion limit margin over the observed ask.
      * ``lock_floor``   never take a wing leg that would push the set's lock below this (retry later).
      * ``no_orders_after_s_to_settle`` hard order cutoff (T-1s): no PLACE/TAKE/RETRY after it.
      * ``freshness_max_age_s`` a STRIKE (wing) book older than this is stale -> cancel, do not place.
      * ``max_sets_per_hour`` stop quoting after this many completed sets (1).
      * ``n_min``        below this solved n, stand down for the tick (no place).
      * ``replace_rate_alarm_per_min`` replaces in a trailing 60 s above this -> cancel + stand down.
      * ``bucket_width`` the $ width of a range bucket (100; $250/$500 hours stand down).
      * ``shadow_Es``    the E ladder the in-process shadow re-solves and scores every tick.
    """

    E: Decimal
    tol: Decimal
    deb_ms: int
    quote_start_s: int
    quote_end_s: int
    contracts: int
    wing_margin: Decimal
    lock_floor: Decimal
    no_orders_after_s_to_settle: int
    freshness_max_age_s: float
    max_sets_per_hour: int
    n_min: Decimal
    replace_rate_alarm_per_min: int
    bucket_width: int
    shadow_Es: tuple[Decimal, ...]
    sha256: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def load_v32_params(
    path: str = DEFAULT_V32_PARAMS_PATH,
    expected_sha: str | None = FROZEN_V32_PARAMS_SHA256,
) -> V32Params:
    """Load + freeze the V3.2 policy.

    ``expected_sha`` defaults to the pinned FROZEN_V32_PARAMS_SHA256 so a plain call self-verifies the
    shipped file and refuses any drift. Pass ``expected_sha=None`` only from tooling that INTENDS to
    re-pin. A mismatch raises V32ParamsShaMismatch (S5 discipline); a missing key raises KeyError.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sha = canonical_sha256(raw)
    if expected_sha is not None and sha != expected_sha:
        raise V32ParamsShaMismatch(
            f"v32 params sha mismatch for {path}: got {sha}, expected {expected_sha} "
            f"(refusing to load a policy whose canonical sha does not match)"
        )
    return V32Params(
        E=Decimal(str(raw["E"])),
        tol=Decimal(str(raw["tol"])),
        deb_ms=int(raw["deb_ms"]),
        quote_start_s=int(raw["quote_start_s"]),
        quote_end_s=int(raw["quote_end_s"]),
        contracts=int(raw["contracts"]),
        wing_margin=Decimal(str(raw["wing_margin"])),
        lock_floor=Decimal(str(raw["lock_floor"])),
        no_orders_after_s_to_settle=int(raw["no_orders_after_s_to_settle"]),
        freshness_max_age_s=float(raw["freshness_max_age_s"]),
        max_sets_per_hour=int(raw["max_sets_per_hour"]),
        n_min=Decimal(str(raw["n_min"])),
        replace_rate_alarm_per_min=int(raw["replace_rate_alarm_per_min"]),
        bucket_width=int(raw["bucket_width"]),
        shadow_Es=tuple(Decimal(str(x)) for x in raw["shadow_Es"]),
        sha256=sha,
        raw=raw,
    )
