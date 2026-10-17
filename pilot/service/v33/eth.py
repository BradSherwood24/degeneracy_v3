"""eth.py — the ETH series spec + frozen ETH policy loader for the V3.3 DRY side-by-side.

Brad (2026-10-16/17): "add to the queue of work copying this strategy to the ETH markets ... setting up
something dry to record if there's any money there" / "Get dry ETH logging on laptop". This module holds
the SMALL amount of ETH-specific configuration the otherwise series-agnostic V3.3 core needs:

  * the ETH range + strike series tickers (``KXETH`` / ``KXETHD``; both ``exchange_index 2``, CF
    Benchmarks, hourly, fee ``quadratic`` multiplier 1 — the SAME exact-fee law as BTC, verified via a
    read-only proxy GET of ``/series/KXETH`` + ``/series/KXETHD`` on 2026-10-17);
  * a ``KXETHD``-bound strike-floor parser. The ``-T<threshold.99>`` sub-tick convention is identical to
    BTC (``round(x+0.01)``: ``KXETHD-26OCT1720-T1704.99`` -> 1705; verified against live metadata), so only
    the prefix differs;
  * the ETH policy file + its own canonical sha pin. The policy is BYTE-IDENTICAL to the BTC V3.3 policy
    EXCEPT the two geometry constants the ETH $5 bucket width forces:
      - ``bucket_width``: 100 -> 5 (ETH buckets are [F, F+5); strike spacing is $5, grids share origin, so
        the 3-leg set pays $2 everywhere exactly as on BTC);
      - ``bucket_switch_hysteresis_usd``: 15 -> 1. The loader validator requires ``2*hyst < bucket_width``
        (an inner window must remain), which $5 forces; 1 preserves a flap-defence proportional to BTC's
        15/100 (~15% of the bucket) while the time debounce (``bucket_switch_deb_ms`` 3000) is unchanged.
    Margins / rungs / lots / shadow_Es / n_min / lock_floor / wing_margin are UNCHANGED from BTC (8..18c,
    11 rungs, 1 lot). This is geometry scaling, NOT a strategy change — see the PR for the flag to Brad.

The ETH roster is DRY BY CONSTRUCTION (``run_v33_eth`` never arms and builds no write-capable executor), so
it needs no falsifier and no day-guard. The loader reuses ``service.v33.params.load_v33_params`` with the
ETH path + the ETH sha, so the ETH policy fails closed on drift exactly like the BTC policy (S5 discipline).
"""

from __future__ import annotations

import os

from service.v32.events import parse_strike_ticker
from service.v33.params import V33Params, load_v33_params

# ETH series tickers (verified via read-only proxy GETs 2026-10-17).
ETH_RANGE_SERIES = "KXETH"
ETH_STRIKE_SERIES = "KXETHD"
# ETH has NO 15-minute series (KXETH15M does not exist); the dry roster records no 15M leg.
ETH_FIFTEEN_SERIES = None
ETH_ROSTER = "DegeneracyV3_3_ETH"

# The shipped ETH policy file (repo-relative to pilot/).
DEFAULT_V33_ETH_PARAMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "policy",
    "v33_eth_params.json",
)

# The canonical sha256 of policy/v33_eth_params.json (same scheme as the BTC pin). FREEZE 2026-10-17:
# the first ETH dry policy = BTC V3.3 policy (sha f403708...) with bucket_width 5 + hysteresis 1.
FROZEN_V33_ETH_PARAMS_SHA256 = "7be646101cc6bbe99432ee630dfd1048729354b91f23116348faa1bda7e96969"


def parse_eth_strike_ticker(ticker: str) -> int | None:
    """``"KXETHD-26OCT1720-T1704.99"`` -> ``1705`` (the ``KXETHD``-bound strike-floor parser)."""
    return parse_strike_ticker(ticker, prefix=ETH_STRIKE_SERIES)


def load_v33_eth_params(
    path: str = DEFAULT_V33_ETH_PARAMS_PATH,
    expected_sha: str | None = FROZEN_V33_ETH_PARAMS_SHA256,
) -> V33Params:
    """Load + freeze the ETH V3.3 policy (reusing the BTC loader with the ETH path + ETH sha).

    A plain call self-verifies the shipped ETH file against ``FROZEN_V33_ETH_PARAMS_SHA256`` and refuses
    any drift (``V33ParamsShaMismatch``); every structural invariant (``2*hyst < bucket_width`` etc.) is
    enforced by the shared loader. Pass ``expected_sha=None`` only from tooling that INTENDS to re-pin.
    """
    return load_v33_params(path, expected_sha=expected_sha)
