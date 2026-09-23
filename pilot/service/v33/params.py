"""params.py — the frozen V3.3 policy (rolling-ladder pump-fader).

Forked from ``service.v32.params`` (Phase L1, 2026-09-22). SAME discipline: the policy is a JSON file
whose *canonical* sha256 is PINNED in code; ``load_v33_params()`` self-verifies the shipped file and
refuses any drift (fail-closed / S5). Money values are Decimal (parsed from JSON strings so the decimal
is exact); times/counts are plain ints/floats/bools. A missing required key is a KeyError (never
defaulted) so a truncated policy fails closed.

WHAT CHANGED vs V3.2 (see pilot/build/v33_l1_core_build_report.md):
  * ``E`` (the single-rung edge) -> ``E_min`` (the LADDER TOP anchor). The ladder rests ``rungs``
    one-lot NO bids at consecutive cents from ``n_top = n(E_min, W)`` down; rung k has margin
    ``E_min + k`` cents.
  * ``contracts`` (order size) -> ``lots_per_rung`` (lots on EACH rung; 1). The whole allotment is
    ``rungs * lots_per_rung``.
  * NEW: ``rungs`` (ladder depth K), ``wing_coalesce_ms`` (rung fills within this of the first coalesce
    into one wing pair, Q2), ``refill_in_window`` (a filled rung is NOT re-placed inside the window, Q3).
  * ``E`` in ``shadow_Es`` invariant becomes: every shadow E must lie WITHIN the live ladder's margin
    range ``[E_min, E_min + (rungs-1)c]`` so the shadow tracks a rung the live policy actually rests.
  * ``max_sets_per_hour`` is K (one lot per rung, one allotment/hour); ``replace_rate_alarm_per_min``
    is per LADDER (the roll issues at most one amend per 1c W move).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# The shipped policy file (repo-relative to pilot/). Callers may pass an explicit path.
DEFAULT_V33_PARAMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "policy",
    "v33_params.json",
)

# The canonical sha256 of the shipped policy/v33_params.json (sha of json.dumps(sort_keys=True,
# separators=(",",":")).encode("utf-8")). Recompute + re-pin only when INTENTIONALLY re-freezing.
# L1 FREEZE (2026-09-22): the first V3.3 ladder policy (E_min 0.05, rungs 11, lots_per_rung 1).
# Round 2 (2026-09-22): added ``fast_shift_min_cents`` (the cap-crash / large-jump fast path, reviewer
# QUESTION #4) -> re-pinned.
FROZEN_V33_PARAMS_SHA256 = "c0201af78015e24fa7d8984d9f9747215330f5bee29fd2567290c2653c244a20"
# History (add-only law): the L1 Round-1 sha (before fast_shift_min_cents). Kept DEFINED so any R1
# ledger rows stay identifiable.
PREVIOUS_V33_PARAMS_SHA256_L1_R1 = "32d6cefcc16400420a6934a3f4ad44d34a2119ad5d83aec628596920c308d36f"

_CENT = Decimal("0.01")


class V33ParamsShaMismatch(Exception):
    """Raised when the loaded v33 policy's canonical sha != the expected sha (fail-closed)."""


class V33ParamsInvalid(Exception):
    """Raised when the loaded v33 policy violates a structural invariant (fail-closed)."""


def canonical_sha256(obj: dict[str, Any]) -> str:
    """sha256 of the canonical JSON encoding (sorted keys, tight separators, utf-8).

    Byte-identical scheme to ``service.v32.params.canonical_sha256`` / ``service.box.canonical_sha256``
    (a unit test pins the agreement) so every pilot shares one sha convention.
    """
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


@dataclass(frozen=True)
class V33Params:
    """The frozen V3.3 policy. Money is Decimal; times/counts are ints/floats; ``refill_in_window`` bool.

    Fields (only the ladder-specific ones are documented; the rest carry V3.2's meaning):
      * ``E_min``        ladder TOP edge: the top rung rests at n_top = largest whole-cent n with
                         n + fee(n) <= 2 - E_min - W, capped at no_ask(B) - 0.01, floored at n_min.
      * ``rungs``        ladder depth K: rest K one-lot NO bids at n_top, n_top-1c, ... n_top-(K-1)c
                         (truncated from the bottom at n_min; the top honours the post-only cap).
      * ``lots_per_rung`` lots on EACH rung (1). The hour's allotment is rungs * lots_per_rung.
      * ``tol`` / ``deb_ms`` roll gate: ``deb_ms`` debounces the START of a convergence (|n_top - anchor|
                         >= tol); once committed, each subsequent cent rolls as soon as the prior amend
                         acks (no re-debounce) while the sign is unchanged; a sign flip re-debounces
                         (Round 2 pacing, reviewer Q4/Q5). Param-driven so Brad can retune without code.
      * ``fast_shift_min_cents`` when |n_top - anchor| >= this AFTER the start debounce (e.g. a cap crash
                         stranding most of the ladder above a bound cap), shift the WHOLE ladder in ONE
                         step (cancel-all/place-all, like a bucket change) instead of crawling K rolls
                         (reviewer QUESTION #4; V3.2 repriced its single order in one step). Default 4.
      * ``wing_coalesce_ms`` rung fills landing within this of the first are coalesced into ONE wing
                         pair sized to the total filled (Q2). A later fill starts a new batch.
      * ``refill_in_window`` L1 supports only False (Q3 no-refill); ENFORCED in the loader — a future
                         True (re-place a filled rung after its wings book) is reserved for L2.
      * ``max_sets_per_hour`` stop quoting after this many completed sets (K).
      * ``replace_rate_alarm_per_min`` rolls (amends) in a trailing 60 s above this -> stand down.
      * ``shadow_Es``    the E ladder the in-process shadow re-solves each tick; each must lie in
                         [E_min, E_min + (rungs-1)c] so it tracks a rung the live ladder rests.
    """

    E_min: Decimal
    rungs: int
    lots_per_rung: int
    tol: Decimal
    deb_ms: int
    quote_start_s: int
    quote_end_s: int
    wing_margin: Decimal
    lock_floor: Decimal
    no_orders_after_s_to_settle: int
    freshness_max_age_s: float
    bucket_freshness_max_age_s: float
    wing_coalesce_ms: int
    refill_in_window: bool
    fast_shift_min_cents: int
    max_sets_per_hour: int
    n_min: Decimal
    replace_rate_alarm_per_min: int
    bucket_width: int
    shadow_Es: tuple[Decimal, ...]
    sha256: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def load_v33_params(
    path: str = DEFAULT_V33_PARAMS_PATH,
    expected_sha: str | None = FROZEN_V33_PARAMS_SHA256,
) -> V33Params:
    """Load + freeze the V3.3 policy.

    ``expected_sha`` defaults to the pinned FROZEN_V33_PARAMS_SHA256 so a plain call self-verifies the
    shipped file and refuses any drift. Pass ``expected_sha=None`` only from tooling that INTENDS to
    re-pin. A mismatch raises V33ParamsShaMismatch (S5 discipline); a missing key raises KeyError.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sha = canonical_sha256(raw)
    if expected_sha is not None and sha != expected_sha:
        raise V33ParamsShaMismatch(
            f"v33 params sha mismatch for {path}: got {sha}, expected {expected_sha} "
            f"(refusing to load a policy whose canonical sha does not match)"
        )
    E_min = Decimal(str(raw["E_min"]))
    rungs = int(raw["rungs"])
    if rungs < 1:
        raise V33ParamsInvalid(f"v33 policy at {path} is invalid: rungs={rungs} must be >= 1")
    # NIT #7 (reviewer, 2026-09-22): ``refill_in_window`` is a real, ENFORCED gate — L1 supports only the
    # DECIDED no-refill behaviour (Q3). A future ``true`` (re-place a filled rung's price after its wings
    # are booked, capped by max_sets_per_hour) is reserved for L2; fail closed here so the flag can never
    # be silently ignored.
    if bool(raw["refill_in_window"]):
        raise V33ParamsInvalid(
            f"v33 policy at {path} is invalid: refill_in_window=true is reserved for L2 (Q3 is "
            f"no-refill in L1); the core does not yet re-place filled rungs"
        )
    if int(raw["fast_shift_min_cents"]) < 2:
        raise V33ParamsInvalid(
            f"v33 policy at {path} is invalid: fast_shift_min_cents="
            f"{raw['fast_shift_min_cents']} must be >= 2 (a 1c move is always a single roll)"
        )
    shadow_Es = tuple(Decimal(str(x)) for x in raw["shadow_Es"])
    # Fail closed (ruling L-6, generalised for the ladder): every shadow E must lie WITHIN the live
    # ladder's margin range [E_min, E_min + (rungs-1)c], else the in-process shadow tracks a rung the
    # live ladder never rests and dry mode reports the wrong statistic.
    E_max = E_min + (rungs - 1) * _CENT
    for E in shadow_Es:
        if not (E_min <= E <= E_max):
            raise V33ParamsInvalid(
                f"v33 policy at {path} is invalid: shadow E={E} is outside the live ladder range "
                f"[{E_min}, {E_max}] (rungs={rungs}); the shadow must track a rung the ladder rests"
            )
    return V33Params(
        E_min=E_min,
        rungs=rungs,
        lots_per_rung=int(raw["lots_per_rung"]),
        tol=Decimal(str(raw["tol"])),
        deb_ms=int(raw["deb_ms"]),
        quote_start_s=int(raw["quote_start_s"]),
        quote_end_s=int(raw["quote_end_s"]),
        wing_margin=Decimal(str(raw["wing_margin"])),
        lock_floor=Decimal(str(raw["lock_floor"])),
        no_orders_after_s_to_settle=int(raw["no_orders_after_s_to_settle"]),
        freshness_max_age_s=float(raw["freshness_max_age_s"]),
        bucket_freshness_max_age_s=float(raw["bucket_freshness_max_age_s"]),
        wing_coalesce_ms=int(raw["wing_coalesce_ms"]),
        refill_in_window=bool(raw["refill_in_window"]),
        fast_shift_min_cents=int(raw["fast_shift_min_cents"]),
        max_sets_per_hour=int(raw["max_sets_per_hour"]),
        n_min=Decimal(str(raw["n_min"])),
        replace_rate_alarm_per_min=int(raw["replace_rate_alarm_per_min"]),
        bucket_width=int(raw["bucket_width"]),
        shadow_Es=shadow_Es,
        sha256=sha,
        raw=raw,
    )
