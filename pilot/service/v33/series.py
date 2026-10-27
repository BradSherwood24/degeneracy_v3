"""series.py — the per-SERIES configuration the otherwise series-agnostic V3.3 window spine reads.

Brad (2026-10-26): "ETH live at 1 lot, sharing the Render worker with BTC." The BTC V3.3 ladder is
live-armed on Render; the ETH ladder must ARM through the SAME ``run_v33`` armed path (no forked/copied
armed logic), differing only in the handful of series-specific knobs captured here. ``run_v33.main`` takes
a ``V33SeriesSpec`` (default ``BTC_SPEC``) and reads every BTC-specific literal from it; ``BTC_SPEC``'s
values are the exact constants ``run_v33`` used before, so the BTC path is behaviour-identical.

Why the ETH roster needs a distinct spec (all of these are proven collision / correctness hazards, not
cosmetics — BTC and ETH windows run the SAME hour on ONE shared Kalshi account):

  * ``coid_prefix`` ``v33-`` (BTC) vs ``v33e-`` (ETH): identical client_order_ids would COLLIDE at the
    venue (duplicate-coid rejection). ``"v33e-".startswith("v33-")`` is False and vice versa, so neither
    roster's startup sweep / attribution can ever match the other's orders.
  * ``sweep_ticker_prefix`` / ``position_prefix`` ``KXBTC`` vs ``KXETH``: the boot/stale-order sweep and
    reconcile-first scope to THIS roster's markets only — a BTC order never gets cancelled by the ETH
    sweep, an ETH position never blocks BTC arming.
  * ``range_series`` / ``strike_series`` / ``strike_parse``: KXBTC/KXBTCD vs KXETH/KXETHD (the ETHD strike
    floor parser is prefix-bound; see ``service.v33.eth``).
  * ``needs_strike_floor_map``: ETH feeds the discovered floor map into the core (the BTC
    ``parse_strike_ticker`` prefix would reject KXETHD); BTC uses the default parser.
  * ``m15_series`` None for ETH (no KXETH15M exists) — the ETH roster records no 15M leg.
  * its OWN writable paths (journals/logs/ledger/mode) + its OWN day-guard prefix + its OWN falsifier,
    all already provided by ``service.paths`` / ``service.v33.stops`` / ``service.v33.eth``.

The SIBLING hooks (``sibling_ledger_path`` + the sibling guard resolver) wire the ACCOUNT-WIDE S4 (Brad's
one-account-S4 ruling): each roster adds the other's unsettled pending-settlement credit into its S4 band,
and an S4 latch on EITHER roster's guard stops BOTH. BTC gains only READS of the ETH ledger/guard, which
contribute nothing while the ETH roster has no armed unsettled rows / no S4 latch (today's reality), so
the BTC S4 is byte-identical until ETH actually arms.

No orders, no network, no key/holdout read here — pure configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from service.v32.events import parse_strike_ticker
from service.wake import FIFTEEN_SERIES

from service.paths import (
    falsifier_path_v33,
    falsifier_path_v33_eth,
    journal_dir_v33,
    journal_dir_v33_eth,
    ledger_path_v33,
    ledger_path_v33_eth,
    log_dir_v33,
    log_dir_v33_eth,
    mode_path_v33,
    mode_path_v33_eth,
    ops_dir_v33,
    ops_dir_v33_eth,
)
from service.v33.eth import (
    ETH_FIFTEEN_SERIES,
    ETH_RANGE_SERIES,
    ETH_ROSTER,
    ETH_STRIKE_SERIES,
    load_v33_eth_params,
    parse_eth_strike_ticker,
)
from service.v33.stops import _DAY_GUARD_PREFIX_V33, _DAY_GUARD_PREFIX_V33_ETH

# The BTC roster's literals (unchanged from run_v33's historic constants).
BTC_ROSTER = "DegeneracyV3_3"
BTC_RANGE_SERIES = "KXBTC"
BTC_STRIKE_SERIES = "KXBTCD"
BTC_COID_PREFIX = "v33-"
BTC_SWEEP_TICKER_PREFIX = "KXBTC"       # covers KXBTC- (range) + KXBTCD- (strikes) via startswith
BTC_POSITION_PREFIX = "KXBTC"

ETH_COID_PREFIX = "v33e-"
ETH_SWEEP_TICKER_PREFIX = "KXETH"       # covers KXETH- (range) + KXETHD- (strikes) via startswith
ETH_POSITION_PREFIX = "KXETH"


@dataclass(frozen=True)
class V33SeriesSpec:
    """Everything ``run_v33.main`` needs to run ONE series. ``BTC_SPEC`` reproduces the historic BTC path
    byte-for-byte; ``ETH_LIVE_SPEC`` is the ETH live roster. Path/loader fields are callables so the
    ``DV3_DATA_DIR`` relocation in ``service.paths`` is honoured at call time (never frozen into the spec)."""

    name: str                                   # "btc" | "eth" (logging / journal label)
    log_tag: str                                # the logger prefix ("V33" BTC -> "[V33]" byte-identical)
    roster: str                                 # the ledger/journal roster label
    ledger_series: str | None                   # the ledger ROW ``series`` label; None for BTC so the BTC
    #   row carries NO ``series`` key (byte-identical to the historic row); "KXETH" for the ETH roster.
    range_series: str                           # KXBTC | KXETH
    strike_series: str                          # KXBTCD | KXETHD
    strike_parse: Callable[[str], int | None]   # the strike-floor parser for this series
    coid_prefix: str                            # the client_order_id prefix the core mints
    sweep_ticker_prefix: str                    # boot/stale-order sweep ticker scope
    position_prefix: str                        # reconcile-first position scope
    needs_strike_floor_map: bool                # feed the discovered floor map into the core (ETH yes)
    m15_series: str | None                      # the co-settling 15-min series, or None (ETH)
    load_params: Callable[[], Any] | None       # the frozen params loader; None = use run_v33's default
    #   BTC loader call (kept literal so the historic monkeypatch + byte-identity hold). ETH supplies its own.
    # writable paths (callables; DV3_DATA_DIR-aware)
    journal_dir_fn: Callable[[], str]
    log_dir_fn: Callable[[], str]
    ledger_path_fn: Callable[[], str]
    mode_path_fn: Callable[[], str]
    falsifier_path_fn: Callable[[], str]
    guard_ops_dir_fn: Callable[[], str]
    guard_prefix: str                           # this roster's day-guard filename prefix
    # account-wide S4 sibling hooks (the OTHER roster, for pending-credit + S4 cross-stop)
    sibling_ledger_path_fn: Callable[[], str] | None
    sibling_guard_ops_dir_fn: Callable[[], str] | None
    sibling_guard_prefix: str | None

    @property
    def has_m15(self) -> bool:
        return self.m15_series is not None


BTC_SPEC = V33SeriesSpec(
    name="btc",
    log_tag="V33",
    roster=BTC_ROSTER,
    ledger_series=None,                 # BTC rows carry NO ``series`` key (historic byte-identity)
    range_series=BTC_RANGE_SERIES,
    strike_series=BTC_STRIKE_SERIES,
    strike_parse=parse_strike_ticker,
    coid_prefix=BTC_COID_PREFIX,
    sweep_ticker_prefix=BTC_SWEEP_TICKER_PREFIX,
    position_prefix=BTC_POSITION_PREFIX,
    needs_strike_floor_map=False,
    m15_series=FIFTEEN_SERIES,
    load_params=None,                   # BTC uses run_v33's literal load_v33_params() (byte-identical)
    journal_dir_fn=journal_dir_v33,
    log_dir_fn=log_dir_v33,
    ledger_path_fn=ledger_path_v33,
    mode_path_fn=mode_path_v33,
    falsifier_path_fn=falsifier_path_v33,
    guard_ops_dir_fn=ops_dir_v33,
    guard_prefix=_DAY_GUARD_PREFIX_V33,
    # BTC's sibling = the ETH live roster (account-wide S4).
    sibling_ledger_path_fn=ledger_path_v33_eth,
    sibling_guard_ops_dir_fn=ops_dir_v33_eth,
    sibling_guard_prefix=_DAY_GUARD_PREFIX_V33_ETH,
)


ETH_LIVE_SPEC = V33SeriesSpec(
    name="eth",
    log_tag="V33-ETH",
    roster=ETH_ROSTER,
    ledger_series=ETH_RANGE_SERIES,     # ETH rows self-label series="KXETH" (as the ETH dry roster does)
    range_series=ETH_RANGE_SERIES,
    strike_series=ETH_STRIKE_SERIES,
    strike_parse=parse_eth_strike_ticker,
    coid_prefix=ETH_COID_PREFIX,
    sweep_ticker_prefix=ETH_SWEEP_TICKER_PREFIX,
    position_prefix=ETH_POSITION_PREFIX,
    needs_strike_floor_map=True,
    m15_series=ETH_FIFTEEN_SERIES,      # None — KXETH15M does not exist
    load_params=load_v33_eth_params,
    journal_dir_fn=journal_dir_v33_eth,
    log_dir_fn=log_dir_v33_eth,
    ledger_path_fn=ledger_path_v33_eth,
    mode_path_fn=mode_path_v33_eth,
    falsifier_path_fn=falsifier_path_v33_eth,
    guard_ops_dir_fn=ops_dir_v33_eth,
    guard_prefix=_DAY_GUARD_PREFIX_V33_ETH,
    # ETH's sibling = the BTC roster (account-wide S4).
    sibling_ledger_path_fn=ledger_path_v33,
    sibling_guard_ops_dir_fn=ops_dir_v33,
    sibling_guard_prefix=_DAY_GUARD_PREFIX_V33,
)
